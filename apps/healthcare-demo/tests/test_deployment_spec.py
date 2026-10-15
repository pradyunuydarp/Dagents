"""The Compose request the app sends when a hospital joins, and the file core-service renders from it.

The app never writes Compose. It sends a workload spec
(:func:`app.services.deployment.lma_compose_request`) to core-service's
``POST /api/v1/manifests/compose``, where the OCaml manifest compiler renders
it. These tests send the request to a real core-service, started as its own
process from the Dagents checkout (``DAGENTS_HOME``, or the repository this
app sits in), with the real planner, and check that the appliers accept the
file it renders.

core-service is a separate process because its package is also named ``app``,
like this app's backend, so the two cannot be imported into one process.
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT / "backend"))
FRAMEWORK_ROOT = Path(os.environ.get("DAGENTS_HOME") or APP_ROOT.parents[1]).resolve()
sys.path.insert(0, str(FRAMEWORK_ROOT))

import httpx  # noqa: E402

from agents.common.infrastructure.compose_apply import (  # noqa: E402
    DockerComposeApplier,
    LocalProcessApplier,
    compose_service,
)

from app.core.config import settings as app_settings  # noqa: E402
from app.domain.dataset import JOINING_SITES  # noqa: E402
from app.services.deployment import BACKEND_DIR, COMPOSE_SERVICES, lma_compose_request  # noqa: E402
from tests.support import dagentsc_available  # noqa: E402

CORE_SERVICE_DIR = FRAMEWORK_ROOT / "services" / "core-service"
PLANNER_SKIP = "dagentsc binary is not available, so core-service cannot render the Compose file"

SITE = JOINING_SITES[0]
PROCESS_URL = "http://127.0.0.1:8085"
COMPOSE_URL = "http://lma-pl-290:8085"
LMA_ENVIRONMENT = [
    {"name": "HEALTHCARE_DEMO_ROLE", "value": "lma"},
    {"name": "HEALTHCARE_DEMO_SITE_ID", "value": "pl-290"},
    {"name": "HEALTHCARE_DEMO_API_PORT", "value": "8085"},
]
NO_CLIENT_LIMITS = [
    {"name": "HEALTHCARE_DEMO_RATE_LIMIT_ENTRIES", "value": "0"},
    {"name": "HEALTHCARE_DEMO_RATE_LIMIT_PROBES", "value": "0"},
    {"name": "HEALTHCARE_DEMO_RATE_LIMIT_PILOTS", "value": "0"},
    {"name": "HEALTHCARE_DEMO_RATE_LIMIT_APPROVALS", "value": "0"},
]
HEALTH_CHECK = {
    "command": [
        "python",
        "-c",
        "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8085/api/v1/health', timeout=2)",
    ],
    "interval_seconds": 5,
    "timeout_seconds": 3,
    "retries": 12,
    "start_period_seconds": 10,
}


def free_port() -> int:
    """A TCP port on 127.0.0.1 that nothing listens on now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class CoreServiceProcess:
    """core-service from the Dagents checkout, as its own process on a free port of 127.0.0.1.

    It inherits ``DAGENTSC_BIN``, so it renders with the OCaml planner.
    """

    def __init__(self) -> None:
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self._process: subprocess.Popen[bytes] | None = None
        self._log = tempfile.TemporaryFile()

    def start(self, timeout: float = 60.0) -> "CoreServiceProcess":
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join([str(FRAMEWORK_ROOT), str(CORE_SERVICE_DIR)])
        self._process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=CORE_SERVICE_DIR,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=self._log,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                break
            try:
                if httpx.get(f"{self.url}/api/v1/health", timeout=1.0).status_code == 200:
                    return self
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        self.stop()
        raise RuntimeError(f"core-service did not start at {self.url}:\n{self.log_tail()}")

    def log_tail(self) -> str:
        self._log.seek(0)
        return self._log.read().decode("utf-8", "replace")[-3000:]

    def stop(self) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
        self._process = None

    def close(self) -> None:
        self.stop()
        self._log.close()


def compose_settings():
    """The app's settings as ``docker-compose.deploy.yml`` sets them."""
    return dataclasses.replace(
        app_settings,
        role="app",
        deployer="compose",
        joining_lma_urls=f"pl-290={COMPOSE_URL}",
        coordinator_url="http://healthcare-demo-backend:8081",
        deploy_image="dagents/healthcare-demo-backend:local",
        deploy_network="healthcare-demo_default",
    )


def process_settings():
    """The app's settings as ``run_split_demo.sh --deploy`` sets them."""
    return dataclasses.replace(
        app_settings,
        role="app",
        deployer="process",
        joining_lma_urls=f"pl-290={PROCESS_URL}",
        internal_port=8081,
        deploy_image="dagents/healthcare-demo-backend:local",
    )


_core: CoreServiceProcess | None = None
_rendered: dict[str, dict] = {}


def setUpModule() -> None:
    global _core
    if not dagentsc_available():
        raise unittest.SkipTest(PLANNER_SKIP)
    _core = CoreServiceProcess().start()


def tearDownModule() -> None:
    if _core is not None:
        _core.close()


def rendered(mode: str) -> dict:
    """core-service's answer to the request of ``mode``, sent once per module."""
    if mode not in _rendered:
        assert _core is not None
        if mode == "process":
            request = lma_compose_request(SITE, PROCESS_URL, process_settings(), "process")
        else:
            request = lma_compose_request(SITE, COMPOSE_URL, compose_settings(), "compose")
        response = httpx.post(f"{_core.url}/api/v1/manifests/compose", json=request, timeout=30)
        assert response.status_code == 200, response.text
        _rendered[mode] = response.json()
    return _rendered[mode]


class ComposeRequestTests(unittest.TestCase):
    """The exact request in each mode, and what core-service renders from it."""

    maxDiff = None

    def test_the_process_mode_request(self) -> None:
        self.assertEqual(
            {
                "placement": "own",
                "network": "default",
                "external_network": False,
                "existing_services": [],
                "components": [
                    {
                        "name": "lma-pl-290",
                        "image": "dagents/healthcare-demo-backend:local",
                        "kind": "Deployment",
                        "args": [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8085"],
                        "env": [
                            *LMA_ENVIRONMENT,
                            {"name": "HEALTHCARE_DEMO_COORDINATOR_URL", "value": "http://127.0.0.1:8081"},
                            *NO_CLIENT_LIMITS,
                        ],
                        "ports": [{"container_port": 8085, "host_port": 8085, "host_ip": "127.0.0.1"}],
                        "health_check": HEALTH_CHECK,
                        "depends_on": [],
                    }
                ],
            },
            lma_compose_request(SITE, PROCESS_URL, process_settings(), "process"),
        )

    def test_the_compose_mode_request(self) -> None:
        self.assertEqual(
            {
                "placement": "own",
                "network": "healthcare-demo_default",
                "external_network": True,
                "existing_services": list(COMPOSE_SERVICES),
                "components": [
                    {
                        "name": "lma-pl-290",
                        "image": "dagents/healthcare-demo-backend:local",
                        "kind": "Deployment",
                        "args": ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8085"],
                        "env": [
                            *LMA_ENVIRONMENT,
                            {"name": "HEALTHCARE_DEMO_COORDINATOR_URL", "value": "http://healthcare-demo-backend:8081"},
                            *NO_CLIENT_LIMITS,
                        ],
                        "ports": [{"container_port": 8085, "host_port": 8085, "host_ip": "127.0.0.1"}],
                        "health_check": HEALTH_CHECK,
                        "depends_on": [],
                    }
                ],
            },
            lma_compose_request(SITE, COMPOSE_URL, compose_settings(), "compose"),
        )

    def test_the_port_comes_from_the_address(self) -> None:
        request = lma_compose_request(SITE, "http://127.0.0.1:9123", process_settings(), "process")
        component = request["components"][0]
        self.assertEqual(["--port", "9123"], component["args"][-2:])
        self.assertEqual([{"container_port": 9123, "host_port": 9123, "host_ip": "127.0.0.1"}], component["ports"])
        self.assertIn({"name": "HEALTHCARE_DEMO_API_PORT", "value": "9123"}, component["env"])
        self.assertIn("http://127.0.0.1:9123/api/v1/health", component["health_check"]["command"][2])

    def test_core_service_renders_both_without_an_issue(self) -> None:
        for mode in ("process", "compose"):
            with self.subTest(mode=mode):
                answer = rendered(mode)
                self.assertEqual([], answer["issues"])
                self.assertEqual(["lma-pl-290"], answer["services"])
                self.assertIsInstance(answer["compose_yaml"], str)

    def test_the_process_applier_accepts_the_process_mode_file(self) -> None:
        compose_yaml = rendered("process")["compose_yaml"]
        with tempfile.TemporaryDirectory() as workdir:
            applier = LocalProcessApplier(Path(workdir), cwd=BACKEND_DIR, programs={sys.executable}, base_env={})
            applier.check(compose_yaml, "lma-pl-290")
        service = compose_service(compose_yaml, "lma-pl-290")
        self.assertEqual(sys.executable, service["command"][0])
        self.assertEqual("lma", service["environment"]["HEALTHCARE_DEMO_ROLE"])
        self.assertEqual("http://127.0.0.1:8081", service["environment"]["HEALTHCARE_DEMO_COORDINATOR_URL"])

    def test_the_compose_mode_file_joins_the_running_demos_network(self) -> None:
        import yaml

        document = yaml.safe_load(rendered("compose")["compose_yaml"])
        self.assertEqual({"healthcare-demo_default": {"external": True}}, document["networks"])
        service = document["services"]["lma-pl-290"]
        self.assertEqual(["healthcare-demo_default"], service["networks"])
        self.assertEqual(["127.0.0.1:8085:8085"], service["ports"])
        self.assertEqual(
            ["CMD", "python", "-c", HEALTH_CHECK["command"][2]],
            service["healthcheck"]["test"],
        )
        self.assertEqual("http://healthcare-demo-backend:8081", service["environment"]["HEALTHCARE_DEMO_COORDINATOR_URL"])
        self.assertNotIn("env_file", service)

    def test_the_new_service_name_differs_from_every_service_of_the_demo(self) -> None:
        import yaml

        for name in ("docker-compose.yml",):
            with self.subTest(file=name):
                document = yaml.safe_load((APP_ROOT / name).read_text(encoding="utf-8"))
                self.assertLessEqual(set(document["services"]), set(COMPOSE_SERVICES))
        self.assertNotIn("lma-pl-290", COMPOSE_SERVICES)


class ComposeConfigTests(unittest.TestCase):
    """``docker compose config`` accepts the compose-mode file. It reads the file only; no daemon is needed."""

    def setUp(self) -> None:
        if shutil.which("docker") is None:
            self.skipTest("docker is not installed, so docker compose cannot check the file")
        probe = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True, timeout=60)
        if probe.returncode != 0:
            self.skipTest("docker compose is not available, so it cannot check the file")

    def test_docker_compose_accepts_the_rendered_file(self) -> None:
        compose_yaml = rendered("compose")["compose_yaml"]
        with tempfile.TemporaryDirectory() as workdir:
            path = Path(workdir) / "lma-pl-290.yml"
            path.write_text(compose_yaml, encoding="utf-8")
            result = subprocess.run(
                ["docker", "compose", "--file", str(path), "config", "--quiet", "lma-pl-290"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            # The Compose applier runs the same check before it starts anything.
            DockerComposeApplier(Path(workdir)).check(compose_yaml, "lma-pl-290")


if __name__ == "__main__":
    unittest.main()
