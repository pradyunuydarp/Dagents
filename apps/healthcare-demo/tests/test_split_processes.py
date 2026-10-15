"""The demo split into processes: the Dagents GMA, one LMA per hospital, and the app.

These tests start the five processes as ``scripts/run_split_demo.sh`` does,
each with ``uvicorn`` on a free port, and call only the app, as the frontend
does. They check that the app lists the hospitals the LMAs report, that a
hospital's page comes from its LMA, that the routes the LMAs call are on the
app's internal port and not on its public one, and, with the planner, that a pilot runs
every round at all three hospitals through the GMA, that training results
carry a digest of each update and never the update, and that each hospital
keeps a record of what it sent.

Each hospital keeps its records in memory, loaded from the committed extract.

``SplitScriptSignalTests`` runs the script's own ``split_main`` with stand-in
processes and checks that a signal sent to the script stops them and ends it.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest

import httpx

from app.domain.dataset import SITES

from tests.support import dagentsc_available

APP_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = APP_ROOT.parents[1]
BACKEND = APP_ROOT / "backend"

SITE_IDS = [site.site_id for site in SITES]
SITE_SIZES = {site.site_id: site.expected_rows for site in SITES}

#: Seconds to wait for every process to answer its health check.
STARTUP_SECONDS = 90.0

#: Seconds to wait for a pilot: every round runs at every hospital over HTTP.
PILOT_SECONDS = 600.0

#: The keys a site result may have. An update vector is not one of them.
RESULT_KEYS = {
    "round_id",
    "site_id",
    "job_digest",
    "participation",
    "code_verified",
    "privacy_checks_passed",
    "contributed_examples",
    "update_norm",
    "update_digest",
    "metrics",
    "local_evidence_pointer",
}

#: Settings of the test process's environment that would change what a child
#: process does. Each child sets what it needs.
_CLEARED = ("HEALTHCARE_DEMO_", "GMA_", "LMA_")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def child_environment(**settings: str) -> dict[str, str]:
    """The test's environment without the demo's and the agents' settings, plus ``settings``.

    ``DAGENTSC_BIN`` is kept, so every process finds the planner the test found.
    """
    env = {key: value for key, value in os.environ.items() if not key.startswith(_CLEARED)}
    env.update(settings)
    return env


class SplitProcesses:
    """Starts the five processes and stops them."""

    def __init__(self, log_dir: Path) -> None:
        self.log_dir = log_dir
        self.gma_port = free_port()
        self.app_port = free_port()
        self.internal_port = free_port()
        self.lma_ports = {site_id: free_port() for site_id in SITE_IDS}
        self.app_url = f"http://127.0.0.1:{self.app_port}"
        self.internal_url = f"http://127.0.0.1:{self.internal_port}"
        self.processes: dict[str, subprocess.Popen] = {}
        self._logs: list = []

    def lma_url(self, site_id: str) -> str:
        return f"http://127.0.0.1:{self.lma_ports[site_id]}"

    def _spawn(self, name: str, cwd: Path, module: str, port: int, env: dict[str, str]) -> None:
        log = open(self.log_dir / f"{name}.log", "wb")  # noqa: SIM115 - closed in stop()
        self._logs.append(log)
        self.processes[name] = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", module, "--host", "127.0.0.1", "--port", str(port)],
            cwd=cwd,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )

    def start(self) -> None:
        sites = ",".join(f"{site_id}={self.lma_url(site_id)}" for site_id in SITE_IDS)
        pythonpath = os.pathsep.join([str(REPO_ROOT), str(BACKEND)])
        no_client_limits = {
            "HEALTHCARE_DEMO_RATE_LIMIT_ENTRIES": "0",
            "HEALTHCARE_DEMO_RATE_LIMIT_PROBES": "0",
            "HEALTHCARE_DEMO_RATE_LIMIT_PILOTS": "0",
            "HEALTHCARE_DEMO_RATE_LIMIT_APPROVALS": "0",
        }
        self._spawn(
            "gma",
            REPO_ROOT,
            "agents.gma.main:app",
            self.gma_port,
            child_environment(
                PYTHONPATH=str(REPO_ROOT),
                GMA_API_HOST="127.0.0.1",
                GMA_API_PORT=str(self.gma_port),
                GMA_FEDERATION_SITES=sites,
            ),
        )
        for site_id in SITE_IDS:
            self._spawn(
                f"lma-{site_id}",
                BACKEND,
                "app.main:app",
                self.lma_ports[site_id],
                child_environment(
                    PYTHONPATH=pythonpath,
                    HEALTHCARE_DEMO_ROLE="lma",
                    HEALTHCARE_DEMO_SITE_ID=site_id,
                    HEALTHCARE_DEMO_COORDINATOR_URL=self.internal_url,
                    **no_client_limits,
                ),
            )
        self._spawn(
            "app",
            BACKEND,
            "app.main:app",
            self.app_port,
            child_environment(
                PYTHONPATH=pythonpath,
                HEALTHCARE_DEMO_ROLE="app",
                HEALTHCARE_DEMO_INTERNAL_HOST="127.0.0.1",
                HEALTHCARE_DEMO_INTERNAL_PORT=str(self.internal_port),
                GMA_PUBLIC_URL=f"http://127.0.0.1:{self.gma_port}",
                HEALTHCARE_DEMO_LMA_URLS=sites,
                **no_client_limits,
            ),
        )
        health = {
            "gma": f"http://127.0.0.1:{self.gma_port}/health",
            **{f"lma-{site_id}": f"{self.lma_url(site_id)}/api/v1/health" for site_id in SITE_IDS},
            "app": f"{self.app_url}/api/v1/health",
        }
        deadline = time.monotonic() + STARTUP_SECONDS
        for name, url in health.items():
            self._wait(name, url, deadline)

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
        path = self.log_dir / f"{name}.log"
        text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        return "\n".join(text.splitlines()[-lines:])

    def stop(self) -> None:
        for process in self.processes.values():
            if process.poll() is None:
                process.terminate()
        for process in self.processes.values():
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        for log in self._logs:
            log.close()


def _rounds(report: dict) -> list[dict]:
    rounds = report["rounds"]
    return [rounds["analytics"], rounds["evaluation"], *rounds["training"], rounds["candidate_validation"]]


class SplitProcessTests(unittest.TestCase):
    """The five processes, started once for the class."""

    split: SplitProcesses
    client: httpx.Client

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        log_dir = tempfile.TemporaryDirectory(prefix="split-processes-")
        cls.addClassCleanup(log_dir.cleanup)
        cls.split = SplitProcesses(Path(log_dir.name))
        # Registered before start(), so a process that started is stopped even
        # when another one fails to start.
        cls.addClassCleanup(cls.split.stop)
        cls.split.start()
        cls.client = httpx.Client(base_url=cls.split.app_url, timeout=60.0)
        cls.addClassCleanup(cls.client.close)

    def get(self, path: str) -> dict:
        response = self.client.get(path)
        self.assertEqual(200, response.status_code, f"{path}: {response.text[:500]}\n{self.split.log('app')}")
        return response.json()

    def test_the_overview_lists_the_hospitals_the_lmas_report(self) -> None:
        overview = self.get("/api/v1/overview")
        hospitals = overview["hospitals"]
        self.assertEqual(SITE_IDS, [hospital["site_id"] for hospital in hospitals])
        for hospital in hospitals:
            with self.subTest(site=hospital["site_id"]):
                self.assertEqual(SITE_SIZES[hospital["site_id"]], hospital["record_count"])
        # The combined app's engines are named "in-process-...".
        self.assertFalse(overview["engine"].startswith("in-process"), "the rounds must run through the GMA")

    def test_a_hospital_page_comes_from_its_own_lma(self) -> None:
        for site_id in SITE_IDS:
            with self.subTest(site=site_id):
                dashboard = self.get(f"/api/v1/hospitals/{site_id}")
                self.assertEqual(site_id, dashboard["site_id"])
                self.assertEqual(SITE_SIZES[site_id], dashboard["counts"]["total"])
        # Each LMA holds one hospital.
        response = httpx.get(f"{self.split.lma_url('uk-134')}/api/v1/hospitals/it-002", timeout=10.0)
        self.assertEqual(404, response.status_code)
        self.assertEqual(404, self.client.get("/api/v1/hospitals/xx-999").status_code)

    def test_the_app_serves_the_routes_the_lmas_call_on_its_internal_port_only(self) -> None:
        model_in_use = "/api/v1/registry/model-in-use"
        self.assertEqual(404, self.client.get(model_in_use).status_code)
        self.assertEqual(404, self.client.post("/api/v1/federation/rounds/r-1/updates", json={}).status_code)
        response = httpx.get(f"{self.split.internal_url}{model_in_use}", timeout=10.0)
        self.assertEqual(200, response.status_code, response.text[:500])
        self.assertIn("params", response.json())
        self.assertEqual(404, httpx.get(f"{self.split.internal_url}/api/v1/overview", timeout=10.0).status_code)

    @unittest.skipUnless(dagentsc_available(), "planner (dagentsc) is not available, so no round can be planned")
    def test_a_pilot_runs_every_round_at_every_hospital_through_the_gma(self) -> None:
        response = self.client.post("/api/v1/pilot:run", json={"round_prefix": "api"}, timeout=PILOT_SECONDS)
        self.assertEqual(
            200,
            response.status_code,
            f"{response.text[:1000]}\n--- app\n{self.split.log('app')}\n--- gma\n{self.split.log('gma')}",
        )
        report = response.json()
        self.assertFalse(report["engine"].startswith("in-process"), report["engine"])
        self.assertTrue(report["rounds"]["training"], "the pilot ran no training round")

        for round_report in _rounds(report):
            with self.subTest(round=round_report["round_id"]):
                results = round_report["results"]
                self.assertEqual(sorted(SITE_IDS), sorted(result["site_id"] for result in results))
                for result in results:
                    self.assertEqual("completed", result["participation"], result)
                    self.assertLessEqual(set(result), RESULT_KEYS, "a result carries more than the contract allows")
                    if round_report["phase"] == "training":
                        self.assertRegex(result["update_digest"], r"^sha256:[0-9a-f]{64}$")
                        self.assertIsInstance(result["update_norm"], float)
                    else:
                        self.assertIsNone(result["update_digest"])
        self.assertNotRegex(response.text, re.compile(r'"update"\s*:'), "an update vector appears in the report")

        # Each hospital keeps what it sent in each round, in its own process.
        round_ids = {round_report["round_id"] for round_report in _rounds(report)}
        for site_id in SITE_IDS:
            with self.subTest(site=site_id):
                contributions = self.get(f"/api/v1/hospitals/{site_id}")["contributions"]
                sent = {entry["round_id"] for entry in contributions} & round_ids
                self.assertTrue(sent, f"{site_id} shows no contribution to the pilot's rounds")


#: Runs ``split_main`` from ``scripts/run_split_demo.sh`` with two ``sleep``
#: processes in place of the five services, so the script's own signal handling
#: is tested without the planner. ``$1`` is the script.
SCRIPT_HARNESS = r"""
source "$1"
split_preflight() { :; }
split_start() {
  split_spawn one "$1/one.log" . sleep 300
  split_spawn two "$1/two.log" . sleep 300
  SPLIT_APP_URL=http://127.0.0.1:1
}
split_main
"""


@unittest.skipUnless(shutil.which("bash") and shutil.which("pgrep"), "bash and pgrep are needed to run the script")
class SplitScriptSignalTests(unittest.TestCase):
    """``run_split_demo.sh`` stops its processes and exits on a signal sent to it alone."""

    SCRIPT = APP_ROOT / "scripts" / "run_split_demo.sh"

    def run_until_ready(self) -> tuple[subprocess.Popen, list[int]]:
        output = tempfile.NamedTemporaryFile(prefix="split-script-", suffix=".log")  # noqa: SIM115
        self.addCleanup(output.close)
        process = subprocess.Popen(
            ["bash", "-c", SCRIPT_HARNESS, "harness", str(self.SCRIPT)],
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.addCleanup(self._kill_group, process)
        deadline = time.monotonic() + 30
        while b"Ctrl-C stops all five." not in Path(output.name).read_bytes():
            self.assertIsNone(process.poll(), Path(output.name).read_text(errors="replace"))
            self.assertLess(time.monotonic(), deadline, "the script did not start its processes")
            time.sleep(0.1)
        children = subprocess.run(
            ["pgrep", "-P", str(process.pid), "-f", "^sleep 300$"], capture_output=True, text=True, check=False
        ).stdout.split()
        self.assertEqual(2, len(children), children)
        return process, [int(pid) for pid in children]

    @staticmethod
    def _kill_group(process: subprocess.Popen) -> None:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=10)

    def assert_stopped(self, pid: int) -> None:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        self.fail(f"process {pid} is still running")

    def test_a_signal_to_the_script_stops_its_processes_and_exits(self) -> None:
        for name, number, status in (("TERM", signal.SIGTERM, 143), ("INT", signal.SIGINT, 130)):
            with self.subTest(signal=name):
                process, children = self.run_until_ready()
                os.kill(process.pid, number)
                self.assertEqual(status, process.wait(timeout=20))
                for child in children:
                    self.assert_stopped(child)


if __name__ == "__main__":
    unittest.main()
