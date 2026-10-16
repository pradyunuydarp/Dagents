"""A split run of the healthcare demo inside one test process.

A split run has five processes: three hospital LMAs (``HEALTHCARE_DEMO_ROLE=lma``),
the Dagents GMA (``agents.gma.main``) and the app (``HEALTHCARE_DEMO_ROLE=app``).
Here each runs as its own FastAPI app in this process, and :class:`HostRouter`
plays the network: a request to ``http://lma-uk-134/...`` reaches that LMA's
app through a ``TestClient``. The app's internal listener (``internal_app``)
is a host of its own, ``http://healthcare-app-internal``, which the LMAs call.

``app.main`` reads its settings when it is imported, so :func:`load_main` runs
``main.py`` once per process under a module name of its own, with the role's
settings. The app's module-level ``app.main`` (the combined process the other
tests use) is not touched.

:class:`SplitProcesses` runs the same split as real processes instead, each
with ``uvicorn`` on a free port of 127.0.0.1, as ``scripts/run_split_demo.sh``
does. With ``deploy=True`` it also starts core-service and lets the app add
Hospital 290, as the script's ``--deploy`` does.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import itertools
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
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
from app.domain.dataset import JOINING_SITES, SITES  # noqa: E402
from app.services.roles import build_app_consortium, build_site_node  # noqa: E402

MAIN_PATH = APP_ROOT / "backend" / "app" / "main.py"

#: Where each process listens on the test "network".
APP_URL = "http://healthcare-app"
APP_INTERNAL_URL = "http://healthcare-app-internal"
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
    ``internal_client`` for the app's internal listener, ``gma_client``).
    Call :meth:`close` when done.
    """

    def __init__(self) -> None:
        import agents.gma.main as gma_main

        self.router = HostRouter()
        lma_urls = ",".join(f"{site_id}={url}" for site_id, url in LMA_URLS.items())

        self.lmas: dict[str, ModuleType] = {}
        for site_id, url in LMA_URLS.items():
            module = load_main("lma", site_id=site_id, coordinator_url=APP_INTERNAL_URL)
            module.consortium = build_site_node(module.settings, client=self.router)  # type: ignore[arg-type]
            self.lmas[site_id] = module
            self.router.attach(url, module.app)

        self.app = load_main("app", gma_url=GMA_URL, lma_urls=lma_urls)
        self.app.consortium = build_app_consortium(
            self.app.settings,
            gma_client=self.router,  # type: ignore[arg-type]
            lma_clients={site_id: self.router for site_id in LMA_URLS},  # type: ignore[misc]
        )
        self.router.attach(APP_URL, self.app.app)
        self.router.attach(APP_INTERNAL_URL, self.app.internal_app)

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
    def internal_client(self) -> TestClient:
        return self.router.apps[httpx.URL(APP_INTERNAL_URL).host]

    @property
    def gma_client(self) -> TestClient:
        return self.router.apps[httpx.URL(GMA_URL).host]

    def close(self) -> None:
        for patch in reversed(self._patches):
            patch.stop()
        for module in (*self.lmas.values(), self.app):
            module.consortium.close()


# --- The split run as real processes ------------------------------------------------

REPO_ROOT = APP_ROOT.parents[1]
BACKEND = APP_ROOT / "backend"
CORE_SERVICE_DIR = REPO_ROOT / "services" / "core-service"

SITE_IDS = [site.site_id for site in SITES]

#: The hospital a run with ``deploy=True`` can add, as ``--deploy`` does.
JOINING_SITE = JOINING_SITES[0]

#: Seconds to wait for every process to answer its health check.
STARTUP_SECONDS = 90.0

#: Settings of the test process's environment that would change what a child
#: process does. Each child sets what it needs.
_CLEARED = ("HEALTHCARE_DEMO_", "GMA_", "LMA_")

#: Every request reaches a hospital from the app or the GMA, so its per-client
#: limits are off. The app keeps them.
NO_CLIENT_LIMITS = {
    "HEALTHCARE_DEMO_RATE_LIMIT_ENTRIES": "0",
    "HEALTHCARE_DEMO_RATE_LIMIT_PROBES": "0",
    "HEALTHCARE_DEMO_RATE_LIMIT_PILOTS": "0",
    "HEALTHCARE_DEMO_RATE_LIMIT_APPROVALS": "0",
}


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def port_answers(port: int, timeout: float = 0.5) -> bool:
    """Whether something accepts a TCP connection at 127.0.0.1:``port``."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def child_environment(**settings: str) -> dict[str, str]:
    """The test's environment without the demo's and the agents' settings, plus ``settings``.

    ``DAGENTSC_BIN`` is kept, so every process finds the planner the test found.
    """
    env = {key: value for key, value in os.environ.items() if not key.startswith(_CLEARED)}
    env.update(settings)
    return env


def _children(pid: int) -> list[int]:
    """The processes ``pid`` started, read from ``/proc``. Empty where ``/proc`` is missing."""
    found: list[int] = []
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            text = stat.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # The command name is in parentheses and may hold spaces. After it come
        # the state and the parent's id.
        fields = text.rsplit(")", 1)[-1].split()
        if len(fields) > 1 and fields[1] == str(pid):
            found.append(int(stat.parent.name))
    return found


def deployed_pids(deploy_dir: Path) -> list[int]:
    """The process ids in ``deploy_dir/*.pid`` that still lead their own process group.

    The app's process applier writes one for each hospital it started.
    """
    found: list[int] = []
    for path in sorted(deploy_dir.glob("*.pid")):
        try:
            pid = int(path.read_text(encoding="utf-8").strip())
            if os.getpgid(pid) == pid:
                found.append(pid)
        except (OSError, ValueError):
            continue
    return found


@dataclasses.dataclass(frozen=True)
class ProcessSpec:
    """One process of a split run: ``python -m uvicorn <module>`` on 127.0.0.1.

    ``settings`` are the variables set for it; the rest of its environment is
    the test's, without the demo's and the agents' settings.
    """

    name: str
    cwd: Path
    module: str
    port: int
    settings: dict[str, str]
    health_url: str

    def arguments(self) -> list[str]:
        """The arguments after the Python program."""
        return ["-m", "uvicorn", self.module, "--host", "127.0.0.1", "--port", str(self.port)]


class SplitProcesses:
    """Starts the processes of a split run and stops them.

    Without ``deploy``: the Dagents GMA, one LMA per hospital in ``SITES`` and
    the app, as ``scripts/run_split_demo.sh`` starts them.

    With ``deploy``: also core-service, and the settings of
    ``run_split_demo.sh --deploy``. The GMA accepts Hospital 290's address
    (``GMA_FEDERATION_JOINABLE_SITES``), and the app has
    ``HEALTHCARE_DEMO_DEPLOYER=process``, the same address in
    ``HEALTHCARE_DEMO_JOINING_LMA_URLS``, core-service's address and a deploy
    directory under ``log_dir``. ``POST /api/v1/deployments`` then starts the
    hospital's LMA as a child process of the app, on ``joining_port``.
    """

    def __init__(self, log_dir: Path, *, deploy: bool = False) -> None:
        self.log_dir = log_dir
        self.deploy = deploy
        self.gma_port = free_port()
        self.app_port = free_port()
        self.internal_port = free_port()
        self.lma_ports = {site_id: free_port() for site_id in SITE_IDS}
        self.core_port = free_port() if deploy else 0
        self.joining_port = free_port() if deploy else 0
        self.app_url = f"http://127.0.0.1:{self.app_port}"
        self.internal_url = f"http://127.0.0.1:{self.internal_port}"
        self.gma_url = f"http://127.0.0.1:{self.gma_port}"
        self.core_url = f"http://127.0.0.1:{self.core_port}"
        self.joining_url = f"http://127.0.0.1:{self.joining_port}"
        self.deploy_dir = log_dir / "deploy"
        self.processes: dict[str, subprocess.Popen] = {}
        self._logs: list = []
        # Processes a stopped process had started, killed in stop() if they outlive it.
        self._descendants: set[int] = set()

    def lma_url(self, site_id: str) -> str:
        return f"http://127.0.0.1:{self.lma_ports[site_id]}"

    def specs(self) -> list[ProcessSpec]:
        """The processes :meth:`start` starts, in order, with the settings each one gets.

        ``scripts/run_split_demo.sh`` starts the same processes;
        ``test_split_deploy.py`` compares the two.
        """
        sites = ",".join(f"{site_id}={self.lma_url(site_id)}" for site_id in SITE_IDS)
        joining = f"{JOINING_SITE.site_id}={self.joining_url}"
        pythonpath = os.pathsep.join([str(REPO_ROOT), str(BACKEND)])
        gma_deploy: dict[str, str] = {}
        app_deploy = {"HEALTHCARE_DEMO_DEPLOYER": "off"}
        specs: list[ProcessSpec] = []
        if self.deploy:
            gma_deploy = {"GMA_FEDERATION_JOINABLE_SITES": joining}
            app_deploy = {
                "HEALTHCARE_DEMO_DEPLOYER": "process",
                "HEALTHCARE_DEMO_JOINING_LMA_URLS": joining,
                "CORE_SERVICE_PUBLIC_URL": self.core_url,
                "HEALTHCARE_DEMO_DEPLOY_DIR": str(self.deploy_dir),
            }
            specs.append(
                ProcessSpec(
                    "core-service",
                    CORE_SERVICE_DIR,
                    "app.main:app",
                    self.core_port,
                    {"PYTHONPATH": os.pathsep.join([str(REPO_ROOT), str(CORE_SERVICE_DIR)])},
                    f"{self.core_url}/api/v1/health",
                )
            )
        specs.append(
            ProcessSpec(
                "gma",
                REPO_ROOT,
                "agents.gma.main:app",
                self.gma_port,
                {
                    "PYTHONPATH": str(REPO_ROOT),
                    "GMA_API_HOST": "127.0.0.1",
                    "GMA_API_PORT": str(self.gma_port),
                    "GMA_FEDERATION_SITES": sites,
                    **gma_deploy,
                },
                f"{self.gma_url}/health",
            )
        )
        for site_id in SITE_IDS:
            specs.append(
                ProcessSpec(
                    f"lma-{site_id}",
                    BACKEND,
                    "app.main:app",
                    self.lma_ports[site_id],
                    {
                        "PYTHONPATH": pythonpath,
                        "HEALTHCARE_DEMO_ROLE": "lma",
                        "HEALTHCARE_DEMO_SITE_ID": site_id,
                        "HEALTHCARE_DEMO_COORDINATOR_URL": self.internal_url,
                        "HEALTHCARE_DEMO_API_PORT": str(self.lma_ports[site_id]),
                        "HEALTHCARE_DEMO_DEPLOYER": "off",
                        **NO_CLIENT_LIMITS,
                    },
                    f"{self.lma_url(site_id)}/api/v1/health",
                )
            )
        specs.append(
            ProcessSpec(
                "app",
                BACKEND,
                "app.main:app",
                self.app_port,
                {
                    "PYTHONPATH": pythonpath,
                    "HEALTHCARE_DEMO_ROLE": "app",
                    "HEALTHCARE_DEMO_API_PORT": str(self.app_port),
                    "HEALTHCARE_DEMO_INTERNAL_HOST": "127.0.0.1",
                    "HEALTHCARE_DEMO_INTERNAL_PORT": str(self.internal_port),
                    "GMA_PUBLIC_URL": self.gma_url,
                    "HEALTHCARE_DEMO_LMA_URLS": sites,
                    **app_deploy,
                    # The script keeps the app's per-client limits. A test sends
                    # many requests from one client, so here they are off.
                    **NO_CLIENT_LIMITS,
                },
                f"{self.app_url}/api/v1/health",
            )
        )
        return specs

    def start(self) -> None:
        """Start every process, then wait until each answers its health check."""
        specs = self.specs()
        for spec in specs:
            log = open(self.log_dir / f"{spec.name}.log", "wb")  # noqa: SIM115 - closed in stop()
            self._logs.append(log)
            self.processes[spec.name] = subprocess.Popen(
                [sys.executable, *spec.arguments()],
                cwd=spec.cwd,
                env=child_environment(**spec.settings),
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        deadline = time.monotonic() + STARTUP_SECONDS
        for spec in specs:
            self._wait(spec.name, spec.health_url, deadline)

    def _wait(self, name: str, url: str, deadline: float) -> None:
        while True:
            if self.processes[name].poll() is not None:
                raise RuntimeError(f"{name} exited with {self.processes[name].returncode}:\n{self.log(name)}")
            try:
                if httpx.get(url, timeout=2.0).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError(f"{name} did not answer {url} in {STARTUP_SECONDS:.0f} s:\n{self.log(name)}")
            time.sleep(0.25)

    def log(self, name: str, lines: int = 40) -> str:
        """The last lines of a process's log. ``lma-pl-290`` is in the deploy directory."""
        path = self.log_dir / f"{name}.log"
        if not path.exists():
            path = self.deploy_dir / f"{name}.log"
        text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        return "\n".join(text.splitlines()[-lines:])

    def stop_process(self, name: str) -> int | None:
        """Stop one process with SIGTERM, as Ctrl-C would, and return its exit status.

        What it started is remembered, so :meth:`stop` kills anything it left
        running. This method does not, so a test can check what the process
        stops by itself.
        """
        process = self.processes[name]
        if process.poll() is None:
            self._descendants.update(_children(process.pid))
            process.terminate()
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        return process.returncode

    def stop(self) -> None:
        for process in self.processes.values():
            if process.poll() is None:
                self._descendants.update(_children(process.pid))
                process.terminate()
        for process in self.processes.values():
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        # A child the app started and did not stop, such as the LMA of a
        # hospital that joined, must not outlive the test.
        for pid in self._descendants:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        self._descendants.clear()
        # The joined LMA runs in its own session, so it is no longer the app's
        # child once the app has died. run_split_demo.sh finds it the same way.
        for pid in deployed_pids(self.deploy_dir):
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        for log in self._logs:
            log.close()
        self._logs.clear()
