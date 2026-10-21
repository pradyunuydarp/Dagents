"""The NL2SQL demo across real service processes, with the real planner.

`services/nl2sql-demo/tests` checks the backend with the framework calls
replaced by stubs, so a wrong route in the orchestrator still passes there:
the trace step turns into a warning, and the backend still returns SQL. This
test starts core-service, pipeline-service, model-service, the LMA, the GMA
and the NL2SQL backend as uvicorn processes on loopback ports the system
picks, sends each bundled sample through `POST /api/v1/generate`, and checks
what every framework call returned. Then it stops pipeline-service and checks
that only its step turns into a warning.

core-service renders the same plan with the planner or with its Python
fallback, so it runs the planner through a script that records each command
and its exit status, and the test checks that record.

It needs the planner and torch (model-service imports torch when it starts).
Without either it skips; the `nl2sql-e2e` CI job requires it to run.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from typing import Any
from unittest import mock

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
DUNE_BUILT_PLANNER = REPO_ROOT / "bindings" / "ocaml" / "_build" / "default" / "bin" / "dagentsc.exe"
BUNDLED_PLANNER = REPO_ROOT / "dagents" / "_bin" / "dagentsc"

#: name, uvicorn target, PYTHONPATH entries besides the root, health route, and
#: the variable that gives the backend the service's address.
SERVICES: tuple[tuple[str, str, tuple[str, ...], str, str | None], ...] = (
    ("core-service", "app.main:app", ("services/core-service",), "/api/v1/health", "CORE_SERVICE_PUBLIC_URL"),
    ("pipeline-service", "app.main:app", ("services/pipeline-service",), "/api/v1/health", "PIPELINE_SERVICE_PUBLIC_URL"),
    ("model-service", "app.main:app", ("services/model-service",), "/api/v1/health", "MODEL_SERVICE_PUBLIC_URL"),
    ("lma", "dagents.lma.main:app", (), "/health", "LMA_PUBLIC_URL"),
    ("gma", "dagents.gma.main:app", (), "/health", "GMA_PUBLIC_URL"),
    ("nl2sql-demo-backend", "app.main:app", ("services/nl2sql-demo/backend",), "/api/v1/health", None),
)
BACKEND = "nl2sql-demo-backend"
#: The five services the backend's status check calls, by the names it reports.
STATUS_NAMES = ("core-service", "pipeline-service", "model-service", "lma", "gma")

#: The trace of one generate request, in order. Six planner steps, fifteen
#: service calls, then the status check.
TRACE_STEPS = (
    "Dagents SourceSpec validation",
    "Dagents extraction planning",
    "Dagents schema contract validation",
    "Dagents quality rules",
    "Dagents pipeline DAG planning",
    "Dagents model route planning",
    "core-service catalog and topology",
    "core-service workload compile",
    "pipeline-service register and run",
    "model-service catalog",
    "GMA register NL2SQL LMA",
    "GMA LMA heartbeat",
    "LMA source registration and validation",
    "GMA source registration and validation",
    "LMA source profile",
    "LMA source model job",
    "GMA assimilated profile",
    "GMA aggregate model job",
    "GMA desired deployment and sync",
    "GMA aggregate run dispatch",
    "GMA fleet overview",
    "Dagents service status",
)

#: Seconds to wait for every service to answer its health route. Importing
#: torch in model-service is the slow part on a cold runner.
START_TIMEOUT_S = 90.0

#: Prints the workload request the backend sends to core-service, from the
#: backend's own code and settings, without sending it.
PRINT_COMPILE_REQUEST = """
import json
from app.core.config import settings
from app.services.dagents_orchestrator import DagentsOrchestrator

class PrintRequest(DagentsOrchestrator):
    def _post_json(self, url, payload):
        print(json.dumps(payload))
        return {}

PrintRequest(settings)._compile_demo_workload()
"""

#: The script core-service runs as its planner. It runs the real planner and
#: then appends "<exit status> <arguments>" to a log, so the test can tell a
#: plan the planner rendered from one core-service's Python fallback rendered.
#: The runner kills the script's whole process group on a timeout, so the
#: planner cannot outlive it.
PLANNER_RECORDER = """#!/bin/sh
{planner} "$@"
status=$?
printf '%s %s\\n' "$status" "$*" >> {log}
exit "$status"
"""


def planner_path() -> str | None:
    """The planner the services will run, as an absolute path, or None.

    The same order as the runner (`DAGENTSC_BIN`, the bundled planner, PATH),
    then the dune build, as the agent tests do.
    """
    configured = os.environ.get("DAGENTSC_BIN")
    if configured:
        found = shutil.which(configured)
        return os.path.abspath(found) if found else None
    for candidate in (BUNDLED_PLANNER, DUNE_BUILT_PLANNER):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    on_path = shutil.which("dagentsc")
    return os.path.abspath(on_path) if on_path else None


def torch_installed() -> bool:
    # find_spec, not import: importing torch takes seconds, and the test process
    # does not need it.
    return importlib.util.find_spec("torch") is not None


@unittest.skipUnless(
    planner_path() is not None,
    "dagentsc binary is not available; build it or set DAGENTSC_BIN to run the NL2SQL stack",
)
@unittest.skipUnless(
    torch_installed(),
    "torch is not installed; model-service imports it when it starts, so the NL2SQL stack cannot start",
)
class Nl2SqlStackTests(unittest.TestCase):
    """One generate request reaches every framework service, and each call works."""

    processes: dict[str, subprocess.Popen[bytes]]
    urls: dict[str, str]
    logs: dict[str, Path]
    planner: str
    environment: dict[str, str]

    @classmethod
    def setUpClass(cls) -> None:
        cls.planner = planner_path() or ""
        cls.processes, cls.logs = {}, {}
        cls.workdir = Path(tempfile.mkdtemp(prefix="nl2sql-stack-"))
        cls.client = httpx.Client(timeout=60.0, trust_env=False)
        # unittest does not call tearDownClass when setUpClass raises, so a
        # failure here, or Ctrl-C, stops every service already started.
        try:
            cls._start_services()
            cls._wait_until_healthy()
            cls.overview_before = cls._get(f"{cls.urls['gma']}/overview")
            cls.samples = cls._get(f"{cls.urls[BACKEND]}/api/v1/samples")
            cls.answers = [cls._generate(sample) for sample in cls.samples]
            cls.core_planner_calls = cls._core_planner_calls()
        except BaseException:
            cls._stop_all()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls._stop_all()

    # -- helpers ------------------------------------------------------------

    @classmethod
    def _start_services(cls) -> None:
        models_dir = cls.workdir / "models"  # empty: the backend offers only its heuristic adapter
        models_dir.mkdir()
        cls.core_planner_log = cls.workdir / "core-service-planner.log"
        recorder = cls.workdir / "core-service-planner"
        recorder.write_text(
            PLANNER_RECORDER.format(planner=shlex.quote(cls.planner), log=shlex.quote(str(cls.core_planner_log))),
            encoding="utf-8",
        )
        recorder.chmod(0o755)
        sockets: list[socket.socket] = []
        try:
            for _ in SERVICES:
                bound = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sockets.append(bound)
                bound.bind(("127.0.0.1", 0))
                bound.listen(64)
            cls.urls = {
                name: f"http://127.0.0.1:{bound.getsockname()[1]}" for (name, *_), bound in zip(SERVICES, sockets)
            }
            # A developer's own agent settings or proxy must not reach the children.
            base = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith(("LMA_", "GMA_")) and not key.lower().endswith("_proxy")
            }
            base.update(
                DAGENTSC_BIN=cls.planner,
                NO_PROXY="127.0.0.1,localhost",
                no_proxy="127.0.0.1,localhost",
                PYTHONDONTWRITEBYTECODE="1",
                PYTHONUNBUFFERED="1",
                NL2SQL_MODELS_DIR=str(models_dir),
                NL2SQL_MODEL_CACHE_DIR=str(cls.workdir / "model-cache"),
                NL2SQL_REQUEST_TIMEOUT_SECONDS="30",
            )
            for name, _, _, _, variable in SERVICES:
                if variable:
                    base[variable] = cls.urls[name]
            cls.environment = base
            for (name, target, paths, _, _), bound in zip(SERVICES, sockets):
                log = cls.workdir / f"{name}.log"
                cls.logs[name] = log
                env = dict(base, PYTHONPATH=os.pathsep.join(str(REPO_ROOT / p) for p in (".", *paths)))
                if name == "core-service":
                    env["DAGENTSC_BIN"] = str(recorder)
                with log.open("wb") as handle:
                    cls.processes[name] = subprocess.Popen(
                        [sys.executable, "-m", "uvicorn", target, "--fd", str(bound.fileno()), "--log-level", "warning"],
                        cwd=cls.workdir,
                        env=env,
                        stdout=handle,
                        stderr=subprocess.STDOUT,
                        pass_fds=(bound.fileno(),),
                        # Its own process group, so teardown stops everything it started.
                        start_new_session=True,
                    )
        finally:
            # Each child holds its own copy of its socket.
            for bound in sockets:
                bound.close()

    @classmethod
    def _stop_all(cls) -> None:
        """Stop every service started so far and remove the folder. Safe to call twice."""
        cls.client.close()
        for process in cls.processes.values():
            cls._stop(process)
        shutil.rmtree(cls.workdir, ignore_errors=True)

    @classmethod
    def _core_planner_calls(cls) -> list[tuple[str, str]]:
        """Each planner command core-service has run so far, as (exit status, arguments)."""
        if not cls.core_planner_log.exists():
            return []
        lines = cls.core_planner_log.read_text(encoding="utf-8").splitlines()
        return [(status, arguments) for status, _, arguments in (line.partition(" ") for line in lines)]

    @staticmethod
    def _stop(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=10)

    @classmethod
    def _log_tails(cls) -> str:
        parts = []
        for name, log in cls.logs.items():
            text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
            if text.strip():
                parts.append(f"--- {name} ---\n" + "\n".join(text.splitlines()[-15:]))
        return "\n".join(parts) or "(no service logged anything)"

    @classmethod
    def _wait_until_healthy(cls) -> None:
        deadline = time.monotonic() + START_TIMEOUT_S
        waiting = [(name, health) for name, _, _, health, _ in SERVICES]
        while waiting:
            for name, process in cls.processes.items():
                if process.poll() is not None:
                    raise RuntimeError(f"{name} exited while starting.\n{cls._log_tails()}")
            still = []
            for name, health in waiting:
                try:
                    if cls.client.get(cls.urls[name] + health, timeout=2.0).status_code != 200:
                        still.append((name, health))
                except httpx.HTTPError:
                    still.append((name, health))
            waiting = still
            if waiting and time.monotonic() > deadline:
                names = ", ".join(name for name, _ in waiting)
                raise RuntimeError(f"{names} did not answer within {START_TIMEOUT_S:.0f} s.\n{cls._log_tails()}")
            if waiting:
                time.sleep(0.2)

    @classmethod
    def _get(cls, url: str) -> Any:
        response = cls.client.get(url)
        if response.status_code != 200:
            raise AssertionError(f"GET {url} returned {response.status_code}: {response.text}\n{cls._log_tails()}")
        return response.json()

    @classmethod
    def _generate(cls, sample: dict[str, Any]) -> dict[str, Any]:
        body = {
            "question": sample["question"],
            "tables": sample["tables"],
            "model_id": "heuristic_demo",
            "use_dagents_services": True,
        }
        response = cls.client.post(f"{cls.urls[BACKEND]}/api/v1/generate", json=body)
        if response.status_code != 200:
            raise AssertionError(
                f"generate for {sample['sample_id']} returned {response.status_code}: {response.text}\n{cls._log_tails()}"
            )
        return response.json()

    def payload(self, answer: dict[str, Any], step_name: str) -> Any:
        """What one trace step returned, after checking that the step succeeded."""
        [step] = [step for step in answer["dagents_trace"] if step["name"] == step_name]
        self.assertEqual("ok", step["status"], f"{step_name}: {step['detail']}\n{self._log_tails()}")
        return step["payload"]

    @staticmethod
    def column_count(sample: dict[str, Any]) -> int:
        return sum(len(table["columns"]) for table in sample["tables"])

    def run_planner(self, command: list[str], payload: dict[str, Any]) -> Any:
        """Ask the planner directly, through the runner every service uses."""
        previous = os.environ.get("DAGENTSC_BIN")
        os.environ["DAGENTSC_BIN"] = self.planner
        try:
            from dagents.common.infrastructure.dagents_runner import run_dagentsc

            return run_dagentsc(command, payload)
        finally:
            if previous is None:
                os.environ.pop("DAGENTSC_BIN", None)
            else:
                os.environ["DAGENTSC_BIN"] = previous

    # -- the checks -----------------------------------------------------------

    def test_1_the_status_check_reaches_every_service(self) -> None:
        status = self._get(f"{self.urls[BACKEND]}/api/v1/dagents/status")
        self.assertEqual(
            {name: "ok" for name in STATUS_NAMES},
            {service["name"]: service["status"] for service in status["services"]},
            self._log_tails(),
        )

    def test_2_each_sample_returns_sql_and_a_trace_of_ok_steps(self) -> None:
        self.assertGreaterEqual(len(self.samples), 3)
        for sample, answer in zip(self.samples, self.answers):
            with self.subTest(sample=sample["sample_id"]):
                self.assertTrue(answer["sql"].startswith("SELECT"), answer["sql"])
                self.assertEqual("heuristic_demo", answer["model_id"])
                for table in sample["tables"]:
                    self.assertIn(f"CREATE TABLE {table['name']} (", answer["schema_ddl"])
                self.assertIn(sample["question"], answer["prompt"])
                trace = answer["dagents_trace"]
                self.assertEqual(list(TRACE_STEPS), [step["name"] for step in trace])
                warnings = {step["name"]: step["detail"] for step in trace if step["status"] != "ok"}
                self.assertEqual({}, warnings, self._log_tails())
                services = self.payload(answer, "Dagents service status")["services"]
                self.assertEqual(["ok"] * len(STATUS_NAMES), [service["status"] for service in services])

    def test_3_pipeline_service_ran_the_schema_profile(self) -> None:
        for sample, answer in zip(self.samples, self.answers):
            with self.subTest(sample=sample["sample_id"]):
                payload = self.payload(answer, "pipeline-service register and run")
                self.assertEqual("nl2sql-demo-schema-flow", payload["registered"]["pipeline_id"])
                run = payload["run"]
                self.assertEqual("completed", run["status"], run.get("error"))
                self.assertEqual(["completed"] * 4, [step["status"] for step in run["step_results"]])
                final = run["final_payload"]
                self.assertEqual(self.column_count(sample), final["schema_profile"]["record_count"])
                self.assertEqual(self.column_count(sample), final["schema_summary"]["column_count"])
                self.assertEqual(sample["question"], final["context"]["question"])

    def test_4_core_service_compiled_the_workload_with_the_planner(self) -> None:
        compiled = self.payload(self.answers[-1], "core-service workload compile")
        [manifest] = compiled["manifests"]
        self.assertEqual("nl2sql-demo-backend", manifest["component_name"])
        self.assertEqual("Deployment", manifest["kind"])
        self.assertIn("kind: Service\n", manifest["service_yaml"])
        self.assertIn("kind: ConfigMap\n", manifest["config_map_yaml"])
        # The same answer the planner gives for the request the backend sends.
        printed = subprocess.run(
            [sys.executable, "-c", PRINT_COMPILE_REQUEST],
            cwd=self.workdir,
            env=dict(
                self.environment,
                PYTHONPATH=os.pathsep.join(str(REPO_ROOT / p) for p in (".", "services/nl2sql-demo/backend")),
            ),
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        request = json.loads(printed.stdout)
        planned = self.run_planner(["manifest", "compile", "--input", "-", "--output", "json"], request)
        self.assertEqual(planned, compiled)
        # The Python fallback renders the same plan for this request, so check
        # that core-service ran the planner, and that it succeeded, for the
        # compile of every sample.
        compiles = [status for status, arguments in self.core_planner_calls if arguments.startswith("manifest compile ")]
        self.assertEqual(["0"] * len(self.samples), compiles, self.core_planner_calls)
        # core-service stored the plan it returned.
        self.assertEqual(compiled, self._get(f"{self.urls['core-service']}/api/v1/workload-plans/nl2sql-demo"))

    def test_5_the_lma_and_gma_profiled_the_schema_and_the_gma_queued_the_run(self) -> None:
        for sample, answer in zip(self.samples, self.answers):
            with self.subTest(sample=sample["sample_id"]):
                for name in ("LMA source registration and validation", "GMA source registration and validation"):
                    self.assertTrue(self.payload(answer, name)["validation"]["valid"], name)
                lma = self.payload(answer, "LMA source profile")
                self.assertEqual(("source", self.column_count(sample)), (lma["scope_kind"], lma["record_count"]))
                gma = self.payload(answer, "GMA assimilated profile")
                self.assertEqual(("assimilated", self.column_count(sample)), (gma["scope_kind"], gma["record_count"]))
                for name in ("LMA source model job", "GMA aggregate model job"):
                    profile = self.payload(answer, name)["dataset_profile"]
                    self.assertEqual(self.column_count(sample), profile["record_count"], name)
                self.assertTrue(self.payload(answer, "GMA register NL2SQL LMA")["accepted"])
                self.assertTrue(self.payload(answer, "GMA LMA heartbeat")["ack"])
                self.assertTrue(self.payload(answer, "GMA desired deployment and sync")["sync"]["up_to_date"])
                dispatch = self.payload(answer, "GMA aggregate run dispatch")
                self.assertTrue(dispatch["accepted"], dispatch)
                self.assertIn("nl2sql-lma", dispatch["message"])

    def test_6_the_gma_counts_the_nl2sql_lma(self) -> None:
        self.assertEqual(0, self.overview_before["total_agents"])
        overview = self._get(f"{self.urls['gma']}/overview")
        self.assertEqual(1, overview["total_agents"])
        self.assertGreaterEqual(overview["dispatched_runs"], len(self.samples))
        agent = self._get(f"{self.urls['gma']}/api/v1/agents/nl2sql-lma")
        self.assertEqual("nl2sql-lma", agent["agent"]["agent_id"])
        self.assertEqual("READY", agent["status"])
        self.assertIn("model_execution", agent["capabilities"])
        self.assertEqual(1, self.payload(self.answers[-1], "GMA fleet overview")["total_agents"])

    def test_7_a_stopped_service_turns_only_its_step_into_a_warning(self) -> None:
        # Last, because it stops a service the other checks use.
        self._stop(self.processes["pipeline-service"])
        status = self._get(f"{self.urls[BACKEND]}/api/v1/dagents/status")
        self.assertEqual(
            {name: ("unavailable" if name == "pipeline-service" else "ok") for name in STATUS_NAMES},
            {service["name"]: service["status"] for service in status["services"]},
        )
        answer = self._generate(self.samples[0])
        self.assertTrue(answer["sql"].startswith("SELECT"), answer["sql"])
        self.assertEqual(list(TRACE_STEPS), [step["name"] for step in answer["dagents_trace"]])
        warnings = [step["name"] for step in answer["dagents_trace"] if step["status"] != "ok"]
        self.assertEqual(["pipeline-service register and run"], warnings, self._log_tails())


class StackStartCleanupTests(unittest.TestCase):
    """When a service fails to start, setUpClass stops the ones it already started.

    The services run in sessions of their own, so nothing else stops them, and
    unittest does not call tearDownClass when setUpClass raises. Each start
    here is a waiting Python process instead of a service, so this test needs
    neither the planner nor torch.
    """

    @staticmethod
    def kill_what_is_left(processes: list[subprocess.Popen[bytes]]) -> None:
        for process in processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()

    def test_a_failed_start_stops_the_services_already_started(self) -> None:
        start = subprocess.Popen
        for failure in (OSError("the third service could not start"), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__):
                started: list[subprocess.Popen[bytes]] = []
                self.addCleanup(self.kill_what_is_left, started)

                def start_two_then_fail(
                    command: list[str],
                    started: list[subprocess.Popen[bytes]] = started,
                    failure: BaseException = failure,
                    **options: Any,
                ) -> subprocess.Popen[bytes]:
                    if len(started) == 2:
                        raise failure
                    process = start([sys.executable, "-c", "import time; time.sleep(120)"], **options)
                    started.append(process)
                    return process

                class Probe(Nl2SqlStackTests):
                    pass

                with mock.patch.object(subprocess, "Popen", start_two_then_fail):
                    with self.assertRaises(type(failure)):
                        Probe.setUpClass()
                self.addCleanup(shutil.rmtree, Probe.workdir, True)
                self.assertEqual(2, len(started))
                self.assertEqual([], [process.pid for process in started if process.poll() is None])
                self.assertFalse(Probe.workdir.exists())


if __name__ == "__main__":
    unittest.main()
