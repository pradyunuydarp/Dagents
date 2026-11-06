"""The published layout keeps an approved model when the API process restarts.

The published demo runs one API process with each hospital's LMA and the GMA
inside it, on Postgres. ``test_pilot_postgres`` builds new app services on the
same database to show what a restart reads back. This test restarts the real
process instead, and talks to it only over HTTP:

1. It seeds a scratch database and starts ``tests/lenient_gates_server.py`` on
   it as a uvicorn process. That server has one release gate a trained
   candidate passes, so the pilot's gates decide ``release``. The test runs a
   pilot and approves its candidate through the public API.
2. It stops the process with SIGTERM. Uvicorn runs the app's lifespan on the
   way out, which stops the agents and closes the stores. The process must
   stop on its own within 30 seconds, with no error in its log. The test
   cannot see whether the lifespan closed the connections: the operating
   system closes them when the process exits.
3. It starts a new process on the same database, on a new socket, and checks
   through that process's public API:

   - the approved candidate is the model in use: in the model list, on the
     overview, on every hospital page and in every worklist assessment;
   - the charts of the last pilot are available and draw;
   - approving the same candidate again is refused with a 409;
   - a new pilot runs, compares its candidate with the approved model, and
     finds the release audit chain intact, holding the records written before
     the restart.

No answer and no log names the database, its password or an address. The
agents' tokens are made inside the process and never leave it, so no answer or
log holds a bearer token either.

It needs ``HEALTHCARE_DEMO_TEST_DATABASE_URL`` (see ``test_site_isolation.py``)
and the planner. Without the database it skips with a message that starts with
``database test skipped``.

Two smaller classes need neither. ``LenientServerDatabaseTests`` checks that
the server refuses to start on a database that is not a test's scratch
database, because a release it approves passed only the test gate.
``ProcessGroupTests`` checks that the cleanup ends what a server left behind
and never signals a process this test has already collected.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
from typing import Any
import unittest
from unittest import mock
import urllib.error
import urllib.request

APP_ROOT = Path(__file__).resolve().parents[1]
# The Dagents checkout: two levels up inside the Dagents repository, or
# DAGENTS_HOME after scripts/extract_repo.sh moved the app into its own.
FRAMEWORK_ROOT = Path(os.environ.get("DAGENTS_HOME") or APP_ROOT.parents[1])
sys.path.insert(0, str(APP_ROOT / "backend"))
sys.path.insert(0, str(FRAMEWORK_ROOT))

from app.domain.dataset import SITES, load_site_records  # noqa: E402
from app.services.site_store import PostgresSiteStore  # noqa: E402
from tests.lenient_gates_server import database_refusal  # noqa: E402
from tests.support import dagentsc_available  # noqa: E402
from tests.test_site_isolation import SupabaseLikeDatabase, admin_dsn_or_skip  # noqa: E402

try:
    from psycopg.conninfo import conninfo_to_dict
except ModuleNotFoundError:  # pragma: no cover - psycopg is in the app's requirements
    conninfo_to_dict = None

SERVER = APP_ROOT / "tests" / "lenient_gates_server.py"
#: Seconds the API may take to answer its health check after it starts.
START_SECONDS = 90
#: Seconds one request may take. A pilot is the longest.
REQUEST_SECONDS = 600
#: Seconds the API may take to stop after SIGTERM: its lifespan stops the agents and closes the stores.
STOP_SECONDS = 30
#: The consortium charts that show the last pilot.
PILOT_CHARTS = ("site-auc", "training-curve", "release-gates")
#: Variables the API keeps from this environment. No HEALTHCARE_DEMO_ setting
#: of this machine leaks in.
KEPT_VARIABLES = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT", "DAGENTSC_BIN", "MPLCONFIGDIR")

#: Calls this test makes go straight to loopback, never through a proxy.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_call(method: str, url: str, body: Any = None, *, timeout: float = REQUEST_SECONDS) -> tuple[int, Any, str]:
    """One HTTP request. Returns ``(status, parsed JSON body or None, raw text)``."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            status, raw, kind = response.status, response.read(), response.headers.get("content-type", "")
    except urllib.error.HTTPError as error:
        status, raw, kind = error.code, error.read(), error.headers.get("content-type", "")
    text = raw.decode("utf-8", errors="replace")
    parsed = json.loads(text) if kind.startswith("application/json") and text else None
    return status, parsed, text


def _seeded_database() -> SupabaseLikeDatabase:
    """A scratch database with the migrations, and each hospital's records loaded as that hospital."""
    db = SupabaseLikeDatabase(admin_dsn_or_skip())
    try:
        db.start()
        for site in SITES:
            store = PostgresSiteStore(site, db.dsn)
            try:
                store.seed_dataset(load_site_records(site.site_id))
            finally:
                store.close()
    except BaseException:
        db.stop()
        raise
    return db


class _ProcessGroup:
    """A process that leads a session of its own, and every process it started.

    Give it a process started with ``start_new_session=True``, so its group
    holds the planner runs it starts as well.
    """

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self.process = process

    def stop(self) -> bool:
        """Send SIGTERM and wait. True when the process stopped on its own within ``STOP_SECONDS``."""
        if self.process.returncode is not None:
            return True
        if self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=STOP_SECONDS)
            except subprocess.TimeoutExpired:
                self.kill()
                return False
        # The process was collected just now. A planner run it started can
        # outlive it, and while such a run holds the group, the group's number
        # cannot go to another process.
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return True

    def kill(self) -> None:
        """Kill the process's whole group, unless this test has already collected the process.

        Until the process is collected, its number, and so its group's, cannot
        go to another process. After that it can, and other jobs run on this
        machine too.
        """
        if self.process.returncode is not None:
            return
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            self.process.wait(timeout=STOP_SECONDS)
        except subprocess.TimeoutExpired:  # pragma: no cover - SIGKILL cannot be ignored
            pass


class _ApiProcess(_ProcessGroup):
    """``tests/lenient_gates_server.py`` as a process of its own, serving on a socket this test bound.

    The process leads a new session, so :meth:`stop` and :meth:`kill` reach
    everything it started, the planner runs included.
    """

    def __init__(self, name: str, database_url: str, log_dir: Path) -> None:
        self.name = name
        self.log_path = log_dir / f"{name}.log"
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        self.port = sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        env = {name: os.environ[name] for name in KEPT_VARIABLES if name in os.environ}
        if os.sep in env.get("DAGENTSC_BIN", ""):
            env["DAGENTSC_BIN"] = os.path.abspath(env["DAGENTSC_BIN"])
        env.update(
            {
                "PYTHONPATH": os.pathsep.join(str(path) for path in (FRAMEWORK_ROOT, APP_ROOT / "backend")),
                "DAGENTS_HOME": str(FRAMEWORK_ROOT),
                "PYTHONUNBUFFERED": "1",
                "NO_PROXY": "127.0.0.1,localhost",
                "HEALTHCARE_DEMO_DATABASE_URL": database_url,
                # The published layout: the agents inside the API process.
                "HEALTHCARE_DEMO_AGENTS": "in-process",
                # The limits are tested in test_rate_limits.py.
                "HEALTHCARE_DEMO_RATE_LIMIT_PILOTS": "0",
                "HEALTHCARE_DEMO_PILOTS_PER_HOUR": "0",
                "HEALTHCARE_DEMO_RATE_LIMIT_APPROVALS": "0",
            }
        )
        with self.log_path.open("wb") as log:
            process = subprocess.Popen(
                [sys.executable, str(SERVER), "--fd", str(sock.fileno())],
                cwd=str(APP_ROOT / "backend"),
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                pass_fds=(sock.fileno(),),
                start_new_session=True,
            )
        super().__init__(process)
        # The child holds its own copy of the socket now.
        sock.close()

    def log(self) -> str:
        return self.log_path.read_text(encoding="utf-8", errors="replace") if self.log_path.exists() else ""

    def wait_healthy(self) -> str:
        """Wait until the health check answers 200, and return that answer."""
        deadline = time.monotonic() + START_SECONDS
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise AssertionError(f"{self.name} exited with {self.process.returncode}:\n{self.log()[-3000:]}")
            try:
                status, _, text = http_call("GET", f"{self.url}/api/v1/health", timeout=3)
            except OSError:
                status, text = None, ""
            if status == 200:
                return text
            time.sleep(0.25)
        raise AssertionError(f"{self.name} did not answer its health check:\n{self.log()[-3000:]}")


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class ApiRestartTests(unittest.TestCase):
    """An approval, a restart of the API process, and what the new process serves."""

    db: SupabaseLikeDatabase
    log_dir: tempfile.TemporaryDirectory[str]
    processes: list[_ApiProcess]
    #: Every answer the API gave, with the request that got it.
    responses: list[tuple[str, str]]
    before: str
    pilot: dict[str, Any]
    approved: dict[str, Any]
    stopped_cleanly: bool
    first_log: str
    models: dict[str, Any]
    overview: dict[str, Any]
    hospitals: dict[str, dict[str, Any]]
    worklists: dict[str, dict[str, Any]]
    charts: dict[str, dict[str, Any]]
    chart_answers: dict[str, tuple[int, str]]
    again: tuple[int, Any]
    second_pilot: tuple[int, Any]

    @classmethod
    def setUpClass(cls) -> None:
        cls.processes = []
        cls.responses = []
        cls.log_dir = tempfile.TemporaryDirectory(prefix="healthcare-restart-")
        cls.db = None  # type: ignore[assignment]
        try:
            cls.db = _seeded_database()
            cls._before_the_restart()
            cls._after_the_restart()
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def _call(cls, process: _ApiProcess, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        """A call to the API. Every answer is kept for the address, password and token check."""
        status, parsed, _ = cls._raw_call(process, method, path, body)
        return status, parsed

    @classmethod
    def _raw_call(cls, process: _ApiProcess, method: str, path: str, body: Any = None) -> tuple[int, Any, str]:
        """:meth:`_call`, with the raw text of the answer as well, for answers that are not JSON."""
        status, parsed, text = http_call(method, process.url + path, body)
        cls.responses.append((f"{method} {path} on {process.name}", text))
        return status, parsed, text

    @classmethod
    def _ok(cls, process: _ApiProcess, method: str, path: str, body: Any = None) -> Any:
        status, parsed = cls._call(process, method, path, body)
        if status != 200:
            raise AssertionError(f"{method} {path} returned {status}: {str(parsed)[:500]}\n{process.log()[-2000:]}")
        return parsed

    @classmethod
    def _start(cls, name: str) -> _ApiProcess:
        process = _ApiProcess(name, cls.db.dsn, Path(cls.log_dir.name))
        cls.processes.append(process)
        cls.responses.append((f"GET /api/v1/health on {name}", process.wait_healthy()))
        return process

    @classmethod
    def _before_the_restart(cls) -> None:
        first = cls._start("api-first")
        cls.before = cls._ok(first, "GET", "/api/v1/models")["model_in_use"]["version"]
        cls.pilot = cls._ok(first, "POST", "/api/v1/pilot:run", {"round_prefix": "api"})
        version = cls.pilot["candidate"]["candidate_version"]
        cls.approved = cls._ok(first, "POST", f"/api/v1/models/{version}:approve")
        cls.stopped_cleanly = first.stop()
        cls.first_log = first.log()

    @classmethod
    def _after_the_restart(cls) -> None:
        second = cls._start("api-second")
        # Everything the restart must keep is read before the new pilot, which
        # replaces the last pilot.
        cls.models = cls._ok(second, "GET", "/api/v1/models")
        cls.overview = cls._ok(second, "GET", "/api/v1/overview")
        cls.hospitals = {}
        cls.worklists = {}
        for site in SITES:
            cls.hospitals[site.site_id] = cls._ok(second, "GET", f"/api/v1/hospitals/{site.site_id}")
            cls.worklists[site.site_id] = cls._ok(second, "GET", f"/api/v1/hospitals/{site.site_id}/worklist?limit=5")
        cls.charts = {chart["chart_id"]: chart for chart in cls._ok(second, "GET", "/api/v1/charts")["charts"]}
        cls.chart_answers = {}
        for chart_id in PILOT_CHARTS:
            status, _, text = cls._raw_call(second, "GET", f"/api/v1/charts/{chart_id}.svg")
            cls.chart_answers[chart_id] = (status, text)
        version = cls.pilot["candidate"]["candidate_version"]
        cls.again = cls._call(second, "POST", f"/api/v1/models/{version}:approve")
        cls.second_pilot = cls._call(second, "POST", "/api/v1/pilot:run", {"round_prefix": "api"})

    @classmethod
    def tearDownClass(cls) -> None:
        for process in getattr(cls, "processes", []):
            process.kill()
        if getattr(cls, "db", None) is not None:
            cls.db.stop()
        if getattr(cls, "log_dir", None) is not None:
            cls.log_dir.cleanup()

    @property
    def version(self) -> str:
        return self.pilot["candidate"]["candidate_version"]

    def test_the_first_process_released_the_candidate_before_it_stopped(self) -> None:
        self.assertEqual("release", self.pilot["release"]["gates"]["action"], self.pilot["release"]["gates"])
        self.assertEqual(self.version, self.approved["released"]["version"])
        self.assertEqual(self.before, self.approved["previous"])

    def test_the_first_process_stops_on_sigterm_without_an_error(self) -> None:
        """A lifespan that hangs or raises on the way out fails this; one that skips closing does not."""
        self.assertTrue(self.stopped_cleanly, f"the API did not stop within {STOP_SECONDS} s of SIGTERM")
        for word in ("Traceback", "ERROR", "Exception"):
            self.assertNotIn(word, self.first_log, self.first_log[-3000:])

    def test_the_approved_candidate_is_the_model_in_use_after_the_restart(self) -> None:
        self.assertNotEqual(self.before, self.version)
        self.assertEqual(self.version, self.models["model_in_use"]["version"])
        listed = {model["version"]: model["status"] for model in self.models["models"]}
        self.assertEqual("released", listed[self.version])
        self.assertEqual("superseded", listed[self.before])
        self.assertEqual(self.version, self.overview["current_model"]["version"])

    def test_every_hospital_scores_with_the_approved_candidate_after_the_restart(self) -> None:
        for site in SITES:
            with self.subTest(site_id=site.site_id):
                self.assertEqual(self.version, self.hospitals[site.site_id]["current_model"]["version"])
                rows = self.worklists[site.site_id]["worklist"]
                self.assertTrue(rows)
                self.assertEqual({self.version}, {row["assessment"]["model_version"] for row in rows})

    def test_the_last_pilots_charts_answer_after_the_restart(self) -> None:
        for chart_id in PILOT_CHARTS:
            with self.subTest(chart_id=chart_id):
                self.assertTrue(self.charts[chart_id]["available"], self.charts[chart_id]["reason"])
                status, text = self.chart_answers[chart_id]
                self.assertEqual(200, status, text[:300])
                self.assertIn("<svg", text)

    def test_the_released_candidate_cannot_be_approved_again(self) -> None:
        status, body = self.again
        self.assertEqual(409, status, body)
        self.assertIn(f"{self.version} is released, not a candidate", body["detail"])

    def test_a_new_pilot_runs_against_the_approved_model(self) -> None:
        status, report = self.second_pilot
        self.assertEqual(200, status, str(report)[:500])
        self.assertEqual(self.version, report["model_in_use"]["version"])
        self.assertNotEqual(self.version, report["candidate"]["candidate_version"])
        # The release audit chain holds the records from before the restart, and still verifies.
        before, after = self.pilot["coordinator_audit"], report["coordinator_audit"]
        self.assertTrue(after["chain_intact"], after)
        self.assertGreater(after["record_count"], before["record_count"])

    def test_no_answer_or_log_names_the_database_or_an_address_or_holds_a_token(self) -> None:
        details = conninfo_to_dict(self.db.dsn)
        secrets = {
            "the connection string": self.db.dsn,
            "the database password": details["password"],
            "the database name": self.db.name,
        }
        host, port = details.get("host"), details.get("port")
        if host and port:
            secrets["the database server's address"] = f"{host}:{port}"
        for process in self.processes:
            secrets[f"the address of {process.name}"] = f"127.0.0.1:{process.port}"
        texts = {f"answer {index} to {request}": text for index, (request, text) in enumerate(self.responses)}
        texts.update({f"the log of {process.name}": process.log() for process in self.processes})
        for where, text in texts.items():
            for name, value in secrets.items():
                with self.subTest(where=where, secret=name):
                    self.assertNotIn(value, text, f"{where} holds {name}")
            with self.subTest(where=where, secret="a bearer token"):
                self.assertNotIn("Bearer", text)


class LenientServerDatabaseTests(unittest.TestCase):
    """The lenient server runs only on in-memory stores or a scratch database that a test made.

    It approves releases under a gate the app never uses. A shell that names
    another database, such as a developer's local demo database, must not
    receive such a release. These tests need no database and no planner.
    """

    def test_a_database_that_is_not_a_scratch_database_is_refused(self) -> None:
        for name in ("HEALTHCARE_DEMO_DATABASE_URL", "HEALTHCARE_DEMO_SITE_UK_134_DATABASE_URL"):
            for value in (
                "postgresql://someone:pw-not-shown@127.0.0.1:5432/postgres",
                "postgresql://someone:pw-not-shown@127.0.0.1:5432",
                "host=127.0.0.1 dbname=stroke password=pw-not-shown",
                "postgresql://someone:pw-not-shown@127.0.0.1:5432/stroke_dagents_scratch_0123abcd",
                "not a connection string",
            ):
                with self.subTest(name=name, value=value):
                    reason = database_refusal({name: value})
                    self.assertIsNotNone(reason)
                    self.assertIn(name, reason)
                    # The reason is printed, so it names the setting and never its value.
                    self.assertNotIn("pw-not-shown", reason)
                    self.assertNotIn("127.0.0.1", reason)

    def test_in_memory_stores_and_scratch_databases_are_accepted(self) -> None:
        scratch = "postgresql://supa_like:pw@127.0.0.1:5432/dagents_scratch_0123abcd"
        for environ in (
            {},
            {"HEALTHCARE_DEMO_DATABASE_URL": ""},
            {"HEALTHCARE_DEMO_DATABASE_URL": "  "},
            {"HEALTHCARE_DEMO_DATABASE_URL": scratch, "HEALTHCARE_DEMO_SITE_UK_134_DATABASE_URL": scratch},
            {"HEALTHCARE_DEMO_TEST_DATABASE_URL": "postgresql://admin:pw@127.0.0.1:5432/postgres"},
        ):
            with self.subTest(environ=sorted(environ)):
                self.assertIsNone(database_refusal(environ))

    def test_the_server_stops_before_it_serves_on_another_database(self) -> None:
        with tempfile.TemporaryDirectory(prefix="healthcare-lenient-") as folder:
            env = {name: os.environ[name] for name in KEPT_VARIABLES if name in os.environ}
            env.update(
                {
                    "PYTHONPATH": os.pathsep.join(str(path) for path in (FRAMEWORK_ROOT, APP_ROOT / "backend")),
                    "DAGENTS_HOME": str(FRAMEWORK_ROOT),
                    # Port 9 has no server: the refusal comes before any connection.
                    "HEALTHCARE_DEMO_DATABASE_URL": "postgresql://someone:pw-not-shown@127.0.0.1:9/postgres",
                    "HEALTHCARE_DEMO_AGENTS": "in-process",
                }
            )
            process = subprocess.Popen(
                [sys.executable, str(SERVER), "--port-file", str(Path(folder) / "api.port")],
                cwd=str(APP_ROOT / "backend"),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                output, _ = process.communicate(timeout=START_SECONDS)
            except subprocess.TimeoutExpired:
                output = None
            finally:
                _ProcessGroup(process).kill()
                if process.stdout is not None:
                    process.stdout.close()
        self.assertIsNotNone(output, "the server kept running on a database that is not a scratch database")
        text = output.decode("utf-8", errors="replace")
        self.assertNotEqual(0, process.returncode, text)
        self.assertIn("HEALTHCARE_DEMO_DATABASE_URL", text)
        self.assertIn("dagents_scratch_", text)
        self.assertNotIn("pw-not-shown", text)


def _running(pid: int) -> bool:
    """Whether a process exists and has not exited. A zombie has exited."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return False
    return stat.rsplit(")", 1)[-1].split()[0] != "Z"


@unittest.skipUnless(Path("/proc/self/stat").exists(), "needs /proc to see whether a process runs")
class ProcessGroupTests(unittest.TestCase):
    """The cleanup signals only process groups this test started and still holds.

    Once a process is collected, its number can belong to another process,
    and this machine runs other jobs' processes too.
    """

    def test_kill_sends_no_signal_to_a_process_already_collected(self) -> None:
        group = _ProcessGroup(subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True))
        group.process.wait(timeout=STOP_SECONDS)
        with mock.patch("os.killpg") as killpg:
            group.kill()
        killpg.assert_not_called()

    def test_stop_also_ends_a_process_the_leader_left_in_its_group(self) -> None:
        """A planner run can outlive the server. stop() ends it while the group still exists."""
        with tempfile.TemporaryDirectory(prefix="healthcare-group-") as folder:
            pid_file = Path(folder) / "child.pid"
            # The child ignores SIGTERM, so only a SIGKILL to the group ends it.
            child = textwrap.dedent(
                f"""
                import os, signal, time
                from pathlib import Path
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                Path({str(pid_file) + ".partial"!r}).write_text(str(os.getpid()))
                Path({str(pid_file) + ".partial"!r}).replace({str(pid_file)!r})
                time.sleep(120)
                """
            )
            leader = f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {child!r}]); time.sleep(120)"
            group = _ProcessGroup(subprocess.Popen([sys.executable, "-c", leader], start_new_session=True))
            self.addCleanup(group.kill)
            deadline = time.monotonic() + START_SECONDS
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(pid_file.exists(), "the child did not start")
            child_pid = int(pid_file.read_text())

            def end_the_child() -> None:
                # Only while it still runs this test's code: its command line names the folder.
                try:
                    command = Path(f"/proc/{child_pid}/cmdline").read_bytes()
                except OSError:
                    return
                if _running(child_pid) and folder.encode() in command:
                    os.kill(child_pid, signal.SIGKILL)

            self.addCleanup(end_the_child)
            self.assertTrue(group.stop(), "the leader did not stop on SIGTERM")
            deadline = time.monotonic() + 10
            while _running(child_pid) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(_running(child_pid), "the child the leader started still runs after stop()")


if __name__ == "__main__":
    unittest.main()
