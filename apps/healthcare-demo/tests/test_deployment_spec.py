"""The Compose request the app sends when a hospital joins, and the file core-service renders from it.

The app never writes Compose. It sends a workload spec
(:func:`app.services.deployment.lma_compose_request`) to core-service's
``POST /api/v1/manifests/compose``, where the OCaml manifest compiler renders
it. The new hospital's LMA is a stock Dagents LMA (``agents.lma.main:app``)
started from ``LMA_*`` settings. Its secrets (its tokens, its database address
and the GMA's address) are in an env file the file names, so the request and
the rendered file hold no token, no connection string and no agent address.

These tests send the request to a real core-service, started as its own
process from the Dagents checkout, with the real planner, and check that the
appliers accept the file it renders. core-service is a separate process
because its package is also named ``app``, like this app's backend.
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
import re
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
    CommandResult,
    DockerComposeApplier,
    LocalProcessApplier,
    compose_service,
)

from app.core.config import settings as app_settings  # noqa: E402
from app.domain.dataset import JOINING_SITES  # noqa: E402
from app.services.agents import lma_environment  # noqa: E402
from app.services.deployment import (  # noqa: E402
    COMPOSE_SERVICES,
    env_file_path,
    framework_root,
    hidden_values,
    lma_compose_request,
    rendered_service_mismatch,
    shown_compose_file,
)
from tests.support import dagentsc_available  # noqa: E402

CORE_SERVICE_DIR = FRAMEWORK_ROOT / "services" / "core-service"
PLANNER_SKIP = "dagentsc binary is not available, so core-service cannot render the Compose file"

SITE = JOINING_SITES[0]
PROCESS_URL = "http://127.0.0.1:8085"
COMPOSE_URL = "http://lma-pl-290:8085"
HEALTH_CHECK = {
    "command": [
        "python",
        "-c",
        "import os, urllib.request; "
        "urllib.request.urlopen('http://127.0.0.1:%s/api/v1/health' % os.environ['PORT'], timeout=3)",
    ],
    "interval_seconds": 5,
    "timeout_seconds": 5,
    "retries": 12,
    "start_period_seconds": 10,
}
#: Names and values that must never be in a request or a rendered file: a
#: token's variable, a connection string, the GMA's address, an agent's ``host:port``.
SECRET_SHAPES = re.compile(
    r"(SITE|COORDINATOR|GMA|ADMIN)_TOKEN|DATABASE_URL|GMA_ENDPOINT|postgres(ql)?://|127\.0\.0\.1:\d+|lma-pl-290:\d+"
)


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
                if httpx.get(f"{self.url}/api/v1/health", timeout=1.0, trust_env=False).status_code == 200:
                    return self
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        self.stop()
        raise RuntimeError(f"core-service did not start:\n{self.log_tail()}")

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


ENV_DIR = Path(tempfile.mkdtemp(prefix="deploy-spec-env-"))


def process_settings(**overrides: object):
    """The app's settings as ``run_local_demo.sh --agents network --deploy`` sets them."""
    values: dict[str, object] = {
        "agents_mode": "network",
        "deployer": "process",
        "deploy_env_dir": str(ENV_DIR),
        "deploy_image": "dagents/healthcare-demo-backend:local",
    }
    return dataclasses.replace(app_settings, **{**values, **overrides})


def compose_settings(**overrides: object):
    """The app's settings as ``docker-compose.deploy.yml`` sets them, with a local env folder."""
    values: dict[str, object] = {
        "agents_mode": "network",
        "deployer": "compose",
        "deploy_env_dir": str(ENV_DIR),
        "deploy_image": "dagents/healthcare-demo-backend:local",
        "deploy_network": "healthcare-demo_default",
    }
    return dataclasses.replace(app_settings, **{**values, **overrides})


def requests() -> dict[str, dict]:
    return {
        "process": lma_compose_request(SITE, PROCESS_URL, process_settings(), "process"),
        "compose": lma_compose_request(SITE, COMPOSE_URL, compose_settings(), "compose"),
    }


def expected_env(port: int) -> list[dict[str, str]]:
    env = {**lma_environment(SITE, database=True), "PORT": str(port)}
    return [{"name": name, "value": value} for name, value in env.items()]


class ComposeRequestShapeTests(unittest.TestCase):
    """The exact request in each mode. Pure: no core-service, no planner."""

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
                        "args": [
                            sys.executable, "-m", "uvicorn", "agents.lma.main:app",
                            "--host", "127.0.0.1", "--port", "8085", "--log-level", "warning",
                        ],
                        "env_files": [str(ENV_DIR / "lma-pl-290.env")],
                        "env": expected_env(8085),
                        "ports": [],
                        "health_check": HEALTH_CHECK,
                        "depends_on": [],
                    }
                ],
            },
            requests()["process"],
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
                        "args": [
                            "uvicorn", "agents.lma.main:app", "--host", "0.0.0.0", "--port", "8085",
                            "--log-level", "warning",
                        ],
                        "env_files": [str(ENV_DIR / "lma-pl-290.env")],
                        "env": expected_env(8085),
                        "ports": [{"container_port": 8085}],
                        "health_check": HEALTH_CHECK,
                        "depends_on": [],
                    }
                ],
            },
            requests()["compose"],
        )

    def test_the_port_comes_from_the_address(self) -> None:
        component = lma_compose_request(SITE, "http://127.0.0.1:9123", process_settings(), "process")["components"][0]
        self.assertEqual(["--port", "9123"], component["args"][-4:-2])
        self.assertIn({"name": "PORT", "value": "9123"}, component["env"])

    def test_the_env_file_is_in_the_env_folder_or_else_the_deploy_folder(self) -> None:
        self.assertEqual(ENV_DIR / "lma-pl-290.env", env_file_path(SITE, process_settings()))
        deploy = Path(tempfile.mkdtemp(prefix="deploy-spec-dir-"))
        self.assertEqual(
            deploy / "lma-pl-290.env", env_file_path(SITE, process_settings(deploy_env_dir="", deploy_dir=str(deploy)))
        )

    def test_no_request_holds_a_secret_or_an_agent_address(self) -> None:
        for mode, request in requests().items():
            with self.subTest(mode=mode):
                names = [item["name"] for item in request["components"][0]["env"]]
                text = repr(request).replace(str(ENV_DIR), "<env>")
                self.assertIsNone(SECRET_SHAPES.search(text), text)
                self.assertNotIn("LMA_SITE_TOKEN", names)
                self.assertTrue(names, "the request carries the LMA's settings")

    def test_the_process_runs_from_the_framework_checkout(self) -> None:
        self.assertTrue((framework_root() / "agents" / "lma" / "main.py").is_file())

    def test_the_new_service_name_differs_from_every_service_of_the_demo(self) -> None:
        import yaml

        document = yaml.safe_load((APP_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
        self.assertLessEqual(set(document["services"]), set(COMPOSE_SERVICES))
        self.assertIn("core-service", COMPOSE_SERVICES)
        self.assertNotIn("lma-pl-290", COMPOSE_SERVICES)


class RenderedServiceMismatchTests(unittest.TestCase):
    """The app compares the parts of the rendered service that decide what runs with what it asked for."""

    def service(self, mode: str) -> dict:
        request = requests()[mode]
        component = request["components"][0]
        service = {
            "image": component["image"],
            "command": list(component["args"]),
            "env_file": list(component["env_files"]),
            "environment": {item["name"]: item["value"] for item in component["env"]},
            "healthcheck": {"test": ["CMD", *component["health_check"]["command"]], "interval": "5s"},
            "networks": [request["network"]],
            "restart": "unless-stopped",
        }
        if mode == "compose":
            service["expose"] = ["8085"]
        return service

    def test_a_matching_service_passes(self) -> None:
        for mode in ("process", "compose"):
            with self.subTest(mode=mode):
                self.assertIsNone(rendered_service_mismatch(self.service(mode), requests()[mode]))

    def test_each_changed_part_is_named(self) -> None:
        changes = {
            "the command": lambda s: s.update(command=[sys.executable, "-c", "print(1)"]),
            "the environment": lambda s: s["environment"].update(LMA_SITE_ID="uk-134"),
            "the env_file": lambda s: s.update(env_file=["/etc/other.env"]),
            "the image": lambda s: s.update(image="example/other:1"),
            "the healthcheck": lambda s: s["healthcheck"].update(test=["CMD", "true"]),
            "the networks": lambda s: s.update(networks=["other"]),
            "the key ports": lambda s: s.update(ports=["0.0.0.0:8085:8085"]),
            "the key privileged": lambda s: s.update(privileged=True),
        }
        for words, change in changes.items():
            with self.subTest(change=words):
                service = self.service("process")
                change(service)
                self.assertEqual(words, rendered_service_mismatch(service, requests()["process"]))
        compose = self.service("compose")
        compose["expose"] = ["9999"]
        self.assertEqual("the expose", rendered_service_mismatch(compose, requests()["compose"]))
        process = self.service("process")
        process["environment"]["PRICE"] = "$5"
        self.assertEqual("the environment", rendered_service_mismatch(process, requests()["process"]))


def planner_style_file(port: str, env_dir: str = "") -> str:
    """A process-mode file written the way the manifest compiler writes it: every scalar in double quotes."""
    env_dir = env_dir or str(ENV_DIR)
    return (
        "services:\n"
        '  "lma-pl-290":\n'
        '    image: "registry.example:1' + port + '/healthcare-demo-backend:local"\n'
        f'    command: ["{sys.executable}", "-m", "uvicorn", "agents.lma.main:app", "--host", "127.0.0.1", '
        f'"--port", "{port}", "--log-level", "warning"]\n'
        "    env_file:\n"
        f'      - "{env_dir}/lma-pl-290.env"\n'
        "    environment:\n"
        '      "LMA_SITE_ID": "pl-290"\n'
        f'      "PORT": "{port}"\n'
        "    networks:\n"
        '      - "default"\n'
        "networks:\n"
        '  "default": {}\n'
    )


class ShownComposeFileTests(unittest.TestCase):
    """What the deployment answers show of the file: no port, interpreter path or folder of this machine.

    Any page can read ``GET /api/v1/deployments``, so the answers show the file
    with ``<port>``, ``<python>`` and ``<env folder>`` in place of those values.
    The applier still gets the file exactly as core-service returned it.
    """

    maxDiff = None

    def test_the_port_the_python_and_the_env_folder_are_replaced(self) -> None:
        hidden = hidden_values("http://127.0.0.1:9123", process_settings())
        shown = shown_compose_file(planner_style_file("9123"), hidden)
        self.assertIsNotNone(shown)
        assert shown is not None
        service = compose_service(shown, "lma-pl-290")
        self.assertEqual(
            ["<python>", "-m", "uvicorn", "agents.lma.main:app", "--host", "127.0.0.1", "--port", "<port>",
             "--log-level", "warning"],
            service["command"],
        )
        self.assertEqual(["<env folder>/lma-pl-290.env"], service["env_file"])
        self.assertEqual({"LMA_SITE_ID": "pl-290", "PORT": "<port>"}, service["environment"])
        # A longer number that holds the port's digits is kept.
        self.assertEqual("registry.example:19123/healthcare-demo-backend:local", service["image"])
        # Nothing else changes.
        self.assertEqual(
            planner_style_file("9123")
            .replace(sys.executable, "<python>")
            .replace(str(ENV_DIR), "<env folder>")
            .replace('"9123"', '"<port>"'),
            shown,
        )
        self.assertFalse(hidden.shown_in(shown))

    def test_a_value_the_file_writes_escaped_is_never_shown(self) -> None:
        # Compose writes `$` as `$$`, so the folder's text is not in the file
        # as it is. The app then shows no file rather than the folder.
        folder = "/tmp/env$folder"
        hidden = hidden_values("http://127.0.0.1:9123", process_settings(deploy_env_dir=folder))
        escaped = planner_style_file("9123", env_dir="/tmp/env$$folder")
        self.assertIsNone(shown_compose_file(escaped, hidden))

    def test_a_file_that_does_not_parse_is_not_shown(self) -> None:
        hidden = hidden_values("http://127.0.0.1:9123", process_settings())
        self.assertIsNone(shown_compose_file("services: [\n", hidden))

    def test_the_root_folder_is_never_a_placeholder(self) -> None:
        # A log file at the root would otherwise turn every "/" into one.
        hidden = hidden_values("http://127.0.0.1:9123", process_settings(), ["/"])
        self.assertEqual("GET /api/v1/health", hidden.hide("GET /api/v1/health"))

    def test_details_show_no_port_or_folder(self) -> None:
        hidden = hidden_values("http://127.0.0.1:9123", process_settings(deploy_dir="/srv/deploy"), ["/run/logs"])
        self.assertEqual(
            "Bind on ('127.0.0.1', <port>) failed; see <deploy folder>/lma-pl-290.log, <deploy folder>/x and "
            "<env folder>/lma-pl-290.env. Process 19123 started.",
            hidden.hide(
                f"Bind on ('127.0.0.1', 9123) failed; see /srv/deploy/lma-pl-290.log, /run/logs/x and "
                f"{ENV_DIR}/lma-pl-290.env. Process 19123 started."
            ),
        )


_core: CoreServiceProcess | None = None
_rendered: dict[str, dict] = {}


def rendered(mode: str) -> dict:
    """core-service's answer to the request of ``mode``, sent once per run."""
    if mode not in _rendered:
        assert _core is not None
        response = httpx.post(
            f"{_core.url}/api/v1/manifests/compose", json=requests()[mode], timeout=30, trust_env=False
        )
        assert response.status_code == 200, response.text
        _rendered[mode] = response.json()
    return _rendered[mode]


@unittest.skipUnless(dagentsc_available(), PLANNER_SKIP)
class ComposeRequestTests(unittest.TestCase):
    """A real core-service with the planner renders both requests, and the appliers accept the files."""

    @classmethod
    def setUpClass(cls) -> None:
        global _core
        _core = CoreServiceProcess().start()
        (ENV_DIR / "lma-pl-290.env").write_text("LMA_SITE_TOKEN=x\n", encoding="utf-8")

    @classmethod
    def tearDownClass(cls) -> None:
        global _core
        if _core is not None:
            _core.close()
            _core = None
        _rendered.clear()

    def test_core_service_renders_both_without_an_issue(self) -> None:
        for mode in ("process", "compose"):
            with self.subTest(mode=mode):
                answer = rendered(mode)
                self.assertEqual([], answer["issues"])
                self.assertEqual(["lma-pl-290"], answer["services"])
                self.assertIsInstance(answer["compose_yaml"], str)

    def test_each_rendered_file_matches_the_request_the_app_sent(self) -> None:
        for mode, request in requests().items():
            with self.subTest(mode=mode):
                service = compose_service(rendered(mode)["compose_yaml"], "lma-pl-290")
                self.assertIsNone(rendered_service_mismatch(service, request))

    def test_no_rendered_file_holds_a_secret_or_an_agent_address(self) -> None:
        for mode in ("process", "compose"):
            with self.subTest(mode=mode):
                text = rendered(mode)["compose_yaml"].replace(str(ENV_DIR), "<env>")
                self.assertIsNone(SECRET_SHAPES.search(text), text)

    def test_the_answers_show_each_file_without_the_port_the_python_or_the_env_folder(self) -> None:
        for mode, url, settings in (
            ("process", PROCESS_URL, process_settings()),
            ("compose", COMPOSE_URL, compose_settings()),
        ):
            with self.subTest(mode=mode):
                hidden = hidden_values(url, settings)
                shown = shown_compose_file(rendered(mode)["compose_yaml"], hidden)
                self.assertIsNotNone(shown, "the planner's file could not be shown")
                assert shown is not None
                self.assertFalse(hidden.shown_in(shown), shown)
                self.assertNotIn("8085", shown)
                self.assertNotIn(str(ENV_DIR), shown)
                self.assertNotIn(sys.executable, shown)
                service = compose_service(shown, "lma-pl-290")
                self.assertEqual("<port>", service["environment"]["PORT"])
                self.assertEqual(["<env folder>/lma-pl-290.env"], service["env_file"])
                self.assertEqual(["--port", "<port>"], service["command"][-4:-2])
                expected_program = "<python>" if mode == "process" else "uvicorn"
                self.assertEqual(expected_program, service["command"][0])

    def test_the_process_applier_accepts_the_process_mode_file(self) -> None:
        compose_yaml = rendered("process")["compose_yaml"]
        with tempfile.TemporaryDirectory() as workdir:
            LocalProcessApplier(
                Path(workdir), cwd=framework_root(), programs={sys.executable}, base_env={}, env_dirs=[ENV_DIR]
            ).check(compose_yaml, "lma-pl-290")
        service = compose_service(compose_yaml, "lma-pl-290")
        self.assertEqual(sys.executable, service["command"][0])
        self.assertEqual("pl-290", service["environment"]["LMA_SITE_ID"])
        self.assertNotIn("ports", service)
        self.assertNotIn("expose", service)

    def test_the_compose_applier_accepts_the_compose_mode_file(self) -> None:
        calls: list[list[str]] = []

        def runner(args: list[str], *, timeout: float) -> CommandResult:
            calls.append(args)
            return CommandResult(0, "", "")

        with tempfile.TemporaryDirectory() as workdir:
            DockerComposeApplier(Path(workdir), runner=runner, env_dirs=[ENV_DIR]).check(
                rendered("compose")["compose_yaml"], "lma-pl-290"
            )
        self.assertEqual(1, len(calls))

    def test_the_compose_mode_file_joins_the_running_demos_network_unpublished(self) -> None:
        import yaml

        document = yaml.safe_load(rendered("compose")["compose_yaml"])
        self.assertEqual({"healthcare-demo_default": {"external": True}}, document["networks"])
        service = document["services"]["lma-pl-290"]
        self.assertEqual(["healthcare-demo_default"], service["networks"])
        self.assertNotIn("ports", service)
        self.assertEqual(["8085"], [str(port) for port in service["expose"]])
        self.assertEqual([str(ENV_DIR / "lma-pl-290.env")], service["env_file"])

    def test_docker_compose_accepts_the_compose_mode_file(self) -> None:
        if shutil.which("docker") is None:
            self.skipTest("docker is not installed, so docker compose cannot check the file")
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
            DockerComposeApplier(Path(workdir), env_dirs=[ENV_DIR]).check(compose_yaml, "lma-pl-290")


if __name__ == "__main__":
    unittest.main()
