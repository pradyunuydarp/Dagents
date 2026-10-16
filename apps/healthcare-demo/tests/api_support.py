"""Helpers for the API tests: fresh app services behind the app, and no rate limits.

``app.main`` builds one process-wide ``HealthcareDemo``: the hospitals' stores,
the Dagents agents (three LMAs and a GMA, in process), the hospital views, the
study and the overview. Each API test class swaps in its own, so entries and
pilots in one class do not change what another class sees, and turns the
per-client limits and the hourly caps off unless the class tests them.
"""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Any
import unittest
from unittest import mock
from xml.etree import ElementTree

APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT / "backend"))
sys.path.insert(0, str(APP_ROOT.parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

import app.main as main  # noqa: E402
from app.services.agents import HealthcareDemo  # noqa: E402

NO_LIMITS = {"entries": 0, "probes": 0, "pilots": 0, "approvals": 0}
NO_HOURLY_CAPS = {"probes": 0, "pilots": 0}


def default_hourly_caps() -> dict[str, int]:
    return {"probes": main.settings.probes_per_site_per_hour, "pilots": main.settings.pilots_per_hour}


def default_limits() -> dict[str, int]:
    return {
        "entries": main.settings.rate_limit_entries_per_minute,
        "probes": main.settings.rate_limit_probes_per_minute,
        "pilots": main.settings.rate_limit_pilots_per_minute,
        "approvals": main.settings.rate_limit_approvals_per_minute,
    }


def served_to(app: Any, host: str, port: int = 50000) -> Any:
    """``app`` as uvicorn serves it to a client at ``host``.

    ``TestClient`` reports its client as ``testclient``, which is not an IP
    address. The route that adds a hospital in process mode checks the TCP
    peer, so its tests wrap the app to set the ASGI scope's ``client``.
    """

    async def wrapped(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] in ("http", "websocket"):
            scope = {**scope, "client": (host, port)}
        await app(scope, receive, send)

    return wrapped


def without_calls(body: Any) -> Any:
    """A JSON object without its ``framework_calls`` field, for tests that compare a whole body."""
    if isinstance(body, dict):
        return {key: value for key, value in body.items() if key != "framework_calls"}
    return body


class ApiTestCase(unittest.TestCase):
    """A test class with its own app services behind ``app.main``.

    Subclasses may override :meth:`make_demo` and :attr:`limits`.
    """

    client: TestClient
    demo: HealthcareDemo
    limits: dict[str, int] = NO_LIMITS

    @classmethod
    def make_demo(cls) -> HealthcareDemo:
        return HealthcareDemo()

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.demo = cls.make_demo()
        cls._patcher = mock.patch.object(main, "demo", cls.demo)
        cls._patcher.start()
        main.rate_limits.configure(cls.limits)
        # Off by default: one test process runs more pilots than an hour allows.
        main.hourly_caps.configure(NO_HOURLY_CAPS)
        main.chart_service.clear()
        cls.client = TestClient(main.app, raise_server_exceptions=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._patcher.stop()
        main.rate_limits.configure(default_limits())
        main.hourly_caps.configure(default_hourly_caps())
        cls.demo.close()
        super().tearDownClass()

    def ok(self, response: Any, status: int = 200) -> Any:
        """The JSON body, after checking the status."""
        self.assertEqual(status, response.status_code, response.text[:500])
        return response.json()


def parse_svg(test: unittest.TestCase, response: Any) -> ElementTree.Element:
    """Check that a response is an SVG document and return its root element."""
    test.assertEqual(200, response.status_code, response.text[:300])
    test.assertTrue(response.headers["content-type"].startswith("image/svg+xml"), response.headers["content-type"])
    root = ElementTree.fromstring(response.content)
    test.assertEqual("{http://www.w3.org/2000/svg}svg", root.tag)
    test.assertIsNotNone(root.find("{http://www.w3.org/2000/svg}title"))
    test.assertNotIn(b"<script", response.content)
    return root
