"""Adding Hospital 290 to a split run of real processes, as ``scripts/run_split_demo.sh --deploy`` does.

``SplitDeployTests`` starts core-service, the Dagents GMA, the three hospital
LMAs and the app as real processes on free ports
(:class:`tests.split_support.SplitProcesses` with ``deploy=True``), and calls
only the app, as the frontend does. ``POST /api/v1/deployments`` then starts
Hospital 290's LMA as a child process of the app, with the Compose file
core-service's planner rendered. The tests run in alphabetical order, which is
the order of the scenario:

1. While another program holds the hospital's port, the ``start`` step fails
   and no later step runs.
2. The hospital joins: every step is done, its page comes from its own LMA
   with its 263 records, the GMA lists its address as joined, the overview
   lists four hospitals, and a pilot includes it.
3. A second request is refused with 409, because the hospital is in the
   consortium and its LMA answers.
4. When the app stops, the hospital's LMA stops with it.

The class needs the planner, and skips with the planner message without it.
With the planner, a process that does not start fails the class.

``SplitScriptDeployTests`` runs the script's ``split_start`` with a stand-in
for each process, and checks that the script gives each process the same
settings as ``SplitProcesses``, and that it stops before it starts anything
when the hospital's port already answers. It needs no planner.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
import time
import unittest

import httpx

from tests.split_support import (
    CORE_SERVICE_DIR,
    JOINING_SITE,
    REPO_ROOT,
    SITE_IDS,
    SplitProcesses,
    port_answers,
)
from tests.support import dagentsc_available

APP_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = APP_ROOT / "scripts" / "run_split_demo.sh"

SITE_ID = JOINING_SITE.site_id
SERVICE = f"lma-{SITE_ID}"
STEPS = ["plan", "start", "answer", "attach", "enrol", "join", "check"]

#: Seconds to wait for a pilot: every round runs at every hospital over HTTP.
PILOT_SECONDS = 600.0

#: Seconds to wait for a request to add the hospital. The app waits up to
#: HEALTHCARE_DEMO_DEPLOY_TIMEOUT_SECONDS (90) for the new LMA to answer.
DEPLOY_SECONDS = 180.0

#: Seconds the hospital's LMA has to stop after the app stops.
STOP_SECONDS = 20.0


def _rounds(report: dict) -> list[dict]:
    rounds = report["rounds"]
    return [rounds["analytics"], rounds["evaluation"], *rounds["training"], rounds["candidate_validation"]]


def _steps(deployment: dict) -> dict[str, dict]:
    return {step["step"]: step for step in deployment["steps"]}


@unittest.skipUnless(
    dagentsc_available(),
    "dagentsc binary is not available, so core-service cannot render the Compose file and the GMA cannot plan a round",
)
class SplitDeployTests(unittest.TestCase):
    """core-service, the GMA, three LMAs and the app with the deployer on, started once for the class."""

    split: SplitProcesses
    client: httpx.Client

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        log_dir = tempfile.TemporaryDirectory(prefix="split-deploy-")
        cls.addClassCleanup(log_dir.cleanup)
        cls.split = SplitProcesses(Path(log_dir.name), deploy=True)
        # Registered before start(), so a process that started is stopped even
        # when another one fails to start, and so is the hospital's LMA.
        cls.addClassCleanup(cls.split.stop)
        cls.split.start()
        cls.client = httpx.Client(base_url=cls.split.app_url, timeout=60.0)
        cls.addClassCleanup(cls.client.close)

    def logs(self) -> str:
        names = ("app", "gma", "core-service", SERVICE)
        return "".join(f"\n--- {name}\n{self.split.log(name)}" for name in names)

    def get(self, path: str) -> dict:
        response = self.client.get(path)
        self.assertEqual(200, response.status_code, f"{path}: {response.text[:500]}{self.logs()}")
        return response.json()

    def deploy(self) -> httpx.Response:
        return self.client.post("/api/v1/deployments", json={"site_id": SITE_ID}, timeout=DEPLOY_SECONDS)

    def test_1_a_held_port_fails_the_start_step_and_nothing_after_it_runs(self) -> None:
        view = self.get("/api/v1/deployments")
        self.assertTrue(view["available"], view["reason"])
        self.assertEqual("process", view["applier"])

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
            holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            holder.bind(("127.0.0.1", self.split.joining_port))
            holder.listen()
            response = self.deploy()

        self.assertEqual(200, response.status_code, f"{response.text[:1000]}{self.logs()}")
        deployment = response.json()
        self.assertEqual("failed", deployment["status"])
        self.assertEqual(STEPS, [step["step"] for step in deployment["steps"]])
        steps = _steps(deployment)
        self.assertEqual("done", steps["plan"]["status"], steps["plan"]["detail"])
        self.assertEqual("failed", steps["start"]["status"])
        self.assertEqual(
            f"Port {self.split.joining_port} answers, but not for a service this app started. Stop what is using "
            "it, or change HEALTHCARE_DEMO_JOINING_LMA_URLS.",
            steps["start"]["detail"],
        )
        for name in STEPS[2:]:
            with self.subTest(step=name):
                self.assertEqual("not_run", steps[name]["status"])
        self.assertIsNone(deployment["next_round"])

        # Nothing joined, and the app lists the attempt.
        view = self.get("/api/v1/deployments")
        hospital = view["hospitals"][0]
        self.assertFalse(hospital["in_consortium"])
        self.assertEqual("not_started", hospital["service"])
        self.assertEqual([deployment["deployment_id"]], [item["deployment_id"] for item in view["deployments"]])
        self.assertEqual(SITE_IDS, [item["site_id"] for item in self.get("/api/v1/overview")["hospitals"]])

    def test_2_the_hospital_joins_and_the_next_pilot_includes_it(self) -> None:
        response = self.deploy()
        self.assertEqual(200, response.status_code, f"{response.text[:1000]}{self.logs()}")
        deployment = response.json()
        steps = _steps(deployment)
        for name in STEPS:
            with self.subTest(step=name):
                self.assertEqual("done", steps[name]["status"], f"{steps[name]['detail']}{self.logs()}")
        self.assertEqual("joined", deployment["status"])
        self.assertEqual({"selected": True, "reason": None}, deployment["next_round"])
        self.assertEqual("process", deployment["applier"])
        self.assertFalse(deployment["adopted"])
        self.assertRegex(deployment["compose_yaml"], rf'(?m)^services:\n  "?{re.escape(SERVICE)}"?:')

        # The hospital's page comes from its own LMA, with its own records.
        dashboard = self.get(f"/api/v1/hospitals/{SITE_ID}")
        self.assertEqual(SITE_ID, dashboard["site_id"])
        self.assertEqual(JOINING_SITE.expected_rows, dashboard["counts"]["total"])
        own = httpx.get(f"{self.split.joining_url}/api/v1/hospitals/{SITE_ID}", timeout=30.0)
        self.assertEqual(200, own.status_code, own.text[:500])

        # The GMA holds the address as one that joined at run time.
        addresses = httpx.get(f"{self.split.gma_url}/api/v1/federation/site-addresses", timeout=10.0).json()
        self.assertIn(
            {"site_id": SITE_ID, "url": self.split.joining_url, "source": "joined"},
            addresses["addresses"],
        )

        overview = self.get("/api/v1/overview")
        self.assertEqual([*SITE_IDS, SITE_ID], [hospital["site_id"] for hospital in overview["hospitals"]])
        view = self.get("/api/v1/deployments")
        self.assertTrue(view["hospitals"][0]["in_consortium"])
        self.assertEqual("running", view["hospitals"][0]["service"])

        pilot = self.client.post("/api/v1/pilot:run", json={"round_prefix": "api"}, timeout=PILOT_SECONDS)
        self.assertEqual(200, pilot.status_code, f"{pilot.text[:1000]}{self.logs()}")
        report = pilot.json()
        for round_report in _rounds(report):
            with self.subTest(round=round_report["round_id"]):
                results = {result["site_id"]: result for result in round_report["results"]}
                self.assertIn(SITE_ID, results, f"the round left {SITE_ID} out: {round_report['plan']}")
                self.assertEqual("completed", results[SITE_ID]["participation"], results[SITE_ID])
        self.assertIn(SITE_ID, report["site_audits"])

    def test_3_a_second_request_gets_409_while_the_hospital_answers(self) -> None:
        response = self.deploy()
        self.assertEqual(409, response.status_code, response.text[:500])
        self.assertEqual(f"{JOINING_SITE.display_name} is already in the consortium.", response.json()["detail"])

    def test_4_the_hospital_stops_when_the_app_stops(self) -> None:
        self.assertTrue(
            port_answers(self.split.joining_port),
            f"the hospital's LMA does not answer on port {self.split.joining_port}, so this test cannot check "
            f"that it stops with the app{self.logs()}",
        )
        self.split.stop_process("app")
        deadline = time.monotonic() + STOP_SECONDS
        while port_answers(self.split.joining_port) and time.monotonic() < deadline:
            time.sleep(0.25)
        self.assertFalse(
            port_answers(self.split.joining_port),
            f"the hospital's LMA still answers {STOP_SECONDS:.0f} s after the app stopped{self.logs()}",
        )


#: Runs ``split_start`` from ``scripts/run_split_demo.sh`` with a stand-in for
#: ``split_spawn`` that writes each process's name, directory and arguments to
#: ``$2``, and none for the health checks. ``$1`` is the script, ``$3`` the log
#: directory.
SCRIPT_HARNESS = r"""
source "$1"
record="$2"
split_spawn() {
  local name="$1" log="$2" dir="$3"
  shift 3
  { printf 'spawn\t%s\t%s\n' "${name}" "${dir}"; printf 'arg\t%s\n' "$@"; } >>"${record}"
  SPLIT_PIDS+=(0)
  SPLIT_NAMES+=("${name}")
}
split_wait_for() { :; }
PYTHON=python3
split_start "$3"
"""

_ASSIGNMENT = re.compile(r"^([A-Z_][A-Z0-9_]*)=(.*)$", re.DOTALL)


def parse_spawns(text: str) -> dict[str, tuple[Path, dict[str, str], list[str]]]:
    """``{name: (directory, settings, arguments after the program)}`` from the harness's record."""
    spawned: dict[str, tuple[Path, dict[str, str], list[str]]] = {}
    current: list[str] = []
    for line in text.splitlines():
        kind, _, rest = line.partition("\t")
        if kind == "spawn":
            name, directory = rest.split("\t")
            current = []
            spawned[name] = (Path(directory), {}, current)
        elif kind == "arg":
            current.append(rest)
    parsed: dict[str, tuple[Path, dict[str, str], list[str]]] = {}
    for name, (directory, _, arguments) in spawned.items():
        settings: dict[str, str] = {}
        index = 0
        while index < len(arguments) and (match := _ASSIGNMENT.match(arguments[index])):
            settings[match.group(1)] = match.group(2)
            index += 1
        parsed[name] = (directory, settings, arguments[index + 1 :])
    return parsed


@unittest.skipUnless(shutil.which("bash"), "bash is needed to run the script")
class SplitScriptDeployTests(unittest.TestCase):
    """``run_split_demo.sh`` and :class:`SplitProcesses` start the same processes with the same settings."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory(prefix="split-script-deploy-")
        self.addCleanup(directory.cleanup)
        self.log_dir = Path(directory.name)
        self.record = self.log_dir / "spawned.tsv"

    def run_script(self, split: SplitProcesses, *, deploy: bool) -> subprocess.CompletedProcess[str]:
        env = {key: value for key, value in os.environ.items() if not key.startswith(("HEALTHCARE_DEMO_", "GMA_", "LMA_"))}
        env.update(
            SPLIT_DEPLOY="1" if deploy else "0",
            DAGENTS_HOME=str(REPO_ROOT),
            GMA_API_PORT=str(split.gma_port),
            HEALTHCARE_DEMO_API_PORT=str(split.app_port),
            HEALTHCARE_DEMO_INTERNAL_PORT=str(split.internal_port),
            HEALTHCARE_DEMO_LMA_PORTS=" ".join(str(split.lma_ports[site_id]) for site_id in SITE_IDS),
        )
        if deploy:
            env.update(CORE_SERVICE_PORT=str(split.core_port), HEALTHCARE_DEMO_JOINING_PORT=str(split.joining_port))
        return subprocess.run(
            ["bash", "-c", SCRIPT_HARNESS, "harness", str(SCRIPT), str(self.record), str(self.log_dir)],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def script_spawns(self, split: SplitProcesses, *, deploy: bool) -> dict[str, tuple[Path, dict[str, str], list[str]]]:
        result = self.run_script(split, deploy=deploy)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return parse_spawns(self.record.read_text(encoding="utf-8"))

    @staticmethod
    def fixture_spawns(split: SplitProcesses) -> dict[str, tuple[Path, dict[str, str], list[str]]]:
        return {spec.name: (spec.cwd, spec.settings, spec.arguments()) for spec in split.specs()}

    def assert_layout(self, spawned: dict, split: SplitProcesses, *, deploy: bool) -> None:
        """The assertions both implementations must pass."""
        names = ["gma", *(f"lma-{site_id}" for site_id in SITE_IDS), "app"]
        self.assertEqual((["core-service"] if deploy else []) + names, list(spawned))
        for site_id in SITE_IDS:
            self.assertEqual("off", spawned[f"lma-{site_id}"][1]["HEALTHCARE_DEMO_DEPLOYER"], site_id)
        gma = spawned["gma"][1]
        app = spawned["app"][1]
        joining = f"{SITE_ID}={split.joining_url}"
        if not deploy:
            self.assertNotIn("GMA_FEDERATION_JOINABLE_SITES", gma)
            self.assertEqual("off", app["HEALTHCARE_DEMO_DEPLOYER"])
            for name in ("HEALTHCARE_DEMO_JOINING_LMA_URLS", "CORE_SERVICE_PUBLIC_URL", "HEALTHCARE_DEMO_DEPLOY_DIR"):
                self.assertNotIn(name, app)
            return
        directory, settings, arguments = spawned["core-service"]
        self.assertEqual(CORE_SERVICE_DIR, directory)
        self.assertEqual(os.pathsep.join([str(REPO_ROOT), str(CORE_SERVICE_DIR)]), settings["PYTHONPATH"])
        self.assertEqual(
            ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(split.core_port)], arguments
        )
        self.assertEqual(joining, gma["GMA_FEDERATION_JOINABLE_SITES"])
        self.assertEqual(
            {
                "HEALTHCARE_DEMO_DEPLOYER": "process",
                "HEALTHCARE_DEMO_JOINING_LMA_URLS": joining,
                "CORE_SERVICE_PUBLIC_URL": split.core_url,
                "HEALTHCARE_DEMO_DEPLOY_DIR": str(self.log_dir / "deploy"),
            },
            {name: app.get(name) for name in (
                "HEALTHCARE_DEMO_DEPLOYER",
                "HEALTHCARE_DEMO_JOINING_LMA_URLS",
                "CORE_SERVICE_PUBLIC_URL",
                "HEALTHCARE_DEMO_DEPLOY_DIR",
            )},
        )
        # The hospital's address is on this machine, and on none of the ports the run uses.
        used = {split.gma_port, split.app_port, split.internal_port, split.core_port, *split.lma_ports.values()}
        self.assertNotIn(split.joining_port, used)
        self.assertTrue(split.joining_url.startswith("http://127.0.0.1:"))

    def test_with_deploy_both_start_core_service_and_turn_the_deployer_on(self) -> None:
        split = SplitProcesses(self.log_dir, deploy=True)
        for source, spawned in (
            ("run_split_demo.sh", self.script_spawns(split, deploy=True)),
            ("SplitProcesses", self.fixture_spawns(split)),
        ):
            with self.subTest(source=source):
                self.assert_layout(spawned, split, deploy=True)

    def test_without_deploy_both_keep_the_deployer_off(self) -> None:
        split = SplitProcesses(self.log_dir, deploy=False)
        for source, spawned in (
            ("run_split_demo.sh", self.script_spawns(split, deploy=False)),
            ("SplitProcesses", self.fixture_spawns(split)),
        ):
            with self.subTest(source=source):
                self.assert_layout(spawned, split, deploy=False)

    def test_the_script_starts_nothing_when_the_joining_port_answers(self) -> None:
        split = SplitProcesses(self.log_dir, deploy=True)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as holder:
            holder.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            holder.bind(("127.0.0.1", split.joining_port))
            holder.listen()
            result = self.run_script(split, deploy=True)
        self.assertEqual(1, result.returncode, result.stdout + result.stderr)
        self.assertIn(f"Port {split.joining_port} already answers", result.stderr)
        self.assertFalse(self.record.exists(), "the script started a process before it checked the port")


if __name__ == "__main__":
    unittest.main()
