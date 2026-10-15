"""A split run of the healthcare demo inside one test process.

A split run has five processes: three hospital LMAs (``HEALTHCARE_DEMO_ROLE=lma``),
the Dagents GMA (``agents.gma.main``) and the app (``HEALTHCARE_DEMO_ROLE=app``).
Here each runs as its own FastAPI app in this process, and :class:`HostRouter`
plays the network: a request to ``http://lma-uk-134/...`` reaches that LMA's
app through a ``TestClient``.

``app.main`` reads its settings when it is imported, so :func:`load_main` runs
``main.py`` once per process under a module name of its own, with the role's
settings. The app's module-level ``app.main`` (the combined process the other
tests use) is not touched.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import itertools
from pathlib import Path
import sys
from types import ModuleType
from typing import Any
from unittest import mock

APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT / "backend"))
sys.path.insert(0, str(APP_ROOT.parents[1]))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from agents.common.application.federation import FederatedRoundController  # noqa: E402
from agents.common.application.governance_service import GovernanceService  # noqa: E402
from agents.common.infrastructure.federation_http import HttpFederationEngine  # noqa: E402

from app.core import config  # noqa: E402
from app.domain.dataset import SITES  # noqa: E402
from app.services.roles import build_app_consortium, build_site_node  # noqa: E402

MAIN_PATH = APP_ROOT / "backend" / "app" / "main.py"

#: Where each process listens on the test "network".
APP_URL = "http://healthcare-app"
GMA_URL = "http://dagents-gma"
LMA_URLS = {site.site_id: f"http://lma-{site.site_id}" for site in SITES}

NO_LIMITS = {
    "rate_limit_entries_per_minute": 0,
    "rate_limit_probes_per_minute": 0,
    "rate_limit_pilots_per_minute": 0,
    "rate_limit_approvals_per_minute": 0,
    "probes_per_site_per_hour": 0,
    "pilots_per_hour": 0,
}

_loaded = itertools.count(1)


def load_main(role: str, **overrides: Any) -> ModuleType:
    """Run ``app/main.py`` as a new module with ``role`` and these settings."""
    settings = dataclasses.replace(config.settings, role=role, **{**NO_LIMITS, **overrides})
    name = f"split_main_{role}_{next(_loaded)}"
    spec = importlib.util.spec_from_file_location(name, MAIN_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    with mock.patch.object(config, "settings", settings):
        spec.loader.exec_module(module)
    return module


class HostRouter:
    """A small network: each host is an app in this process.

    It has the methods the HTTP clients in the demo and the framework call
    (``request``, ``get``, ``post``, ``put``). A host that is not attached,
    or is marked down, fails like a refused connection.
    """

    def __init__(self) -> None:
        self.apps: dict[str, TestClient] = {}
        self.down: set[str] = set()

    def attach(self, url: str, app: Any) -> None:
        self.apps[httpx.URL(url).host] = TestClient(app, raise_server_exceptions=False)

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        host = httpx.URL(url).host
        if host in self.down or host not in self.apps:
            raise httpx.ConnectError(f"cannot connect to {host}")
        return self.apps[host].request(method, url, **kwargs)

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("PUT", url, **kwargs)


class SplitRun:
    """Three LMAs, the Dagents GMA and the app, wired through one :class:`HostRouter`.

    Attributes: ``lmas`` (site id to the LMA's ``main`` module), ``app`` (the
    app's ``main`` module), ``gma`` (``agents.gma.main``), ``router``, and a
    ``TestClient`` for each (``lma_client(site_id)``, ``app_client``,
    ``gma_client``). Call :meth:`close` when done.
    """

    def __init__(self) -> None:
        import agents.gma.main as gma_main

        self.router = HostRouter()
        lma_urls = ",".join(f"{site_id}={url}" for site_id, url in LMA_URLS.items())

        self.lmas: dict[str, ModuleType] = {}
        for site_id, url in LMA_URLS.items():
            module = load_main("lma", site_id=site_id, coordinator_url=APP_URL)
            module.consortium = build_site_node(module.settings, client=self.router)  # type: ignore[arg-type]
            self.lmas[site_id] = module
            self.router.attach(url, module.app)

        self.app = load_main("app", gma_url=GMA_URL, lma_urls=lma_urls, coordinator_url=APP_URL)
        self.app.consortium = build_app_consortium(
            self.app.settings,
            gma_client=self.router,  # type: ignore[arg-type]
            lma_clients={site_id: self.router for site_id in LMA_URLS},  # type: ignore[misc]
        )
        self.router.attach(APP_URL, self.app.app)

        # The Dagents GMA, with GMA_FEDERATION_SITES naming the three LMAs.
        self.gma = gma_main
        self.gma_rounds = FederatedRoundController(engine=HttpFederationEngine(LMA_URLS, client=self.router))  # type: ignore[arg-type]
        self.gma_governance = GovernanceService()
        self._patches = [
            mock.patch.object(gma_main, "rounds", self.gma_rounds),
            mock.patch.object(gma_main, "governance", self.gma_governance),
        ]
        for patch in self._patches:
            patch.start()
        self.router.attach(GMA_URL, gma_main.app)

    def lma_client(self, site_id: str) -> TestClient:
        return self.router.apps[httpx.URL(LMA_URLS[site_id]).host]

    @property
    def app_client(self) -> TestClient:
        return self.router.apps[httpx.URL(APP_URL).host]

    @property
    def gma_client(self) -> TestClient:
        return self.router.apps[httpx.URL(GMA_URL).host]

    def close(self) -> None:
        for patch in reversed(self._patches):
            patch.stop()
        for module in (*self.lmas.values(), self.app):
            module.consortium.close()
