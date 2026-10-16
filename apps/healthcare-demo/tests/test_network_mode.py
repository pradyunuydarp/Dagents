"""The demo works the same when the Dagents agents run as their own processes.

In network mode (``HEALTHCARE_DEMO_AGENTS=network``) the GMA, one LMA per
hospital and the demo API run as five uvicorn processes, and the API calls the
agents over HTTP with the same client code it uses in process. This test starts
the five processes on loopback, against one scratch PostgreSQL database with the
real migrations, and checks through the public API:

- the status report: the agents run as their own processes, the records are in
  Postgres, and every agent answers;
- the Guard probe, which the hospital's LMA answers;
- a pilot, which gives the same numbers as a pilot with the agents in process
  on the same database, and as the recorded pilot (``test_pilot_parity.py``);
- an entry and its outcome, which the hospital's LMA reads at once;
- the hospital page, and the refusal to approve a rejected candidate;
- that each agent refuses a call without the right token, and that no response
  and no log holds a token or an agent's address.

:class:`NetworkApprovalTests` starts the five processes again with a release
gate a trained candidate passes, and approves the candidate: the GMA releases
it, and each hospital's LMA process then reads it from the GMA and scores with
it.

Each process gets a socket that this test bound to a free loopback port, so no
port is fixed and no port can be taken between choosing it and serving on it.
The tokens are new for each run and reach the processes only through their
environment.

It needs ``HEALTHCARE_DEMO_TEST_DATABASE_URL`` (see ``test_site_isolation.py``)
and the planner. Without the database it skips with a message that starts with
``database test skipped``.

:class:`NetworkSettingsTests` needs neither. It reads the start scripts, the
compose file and ``env/.env.healthcare-demo`` as text and checks that every
hospital has an LMA with a port, and that the ports come only from the env
file.
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any
import unittest
import urllib.error
import urllib.request

APP_ROOT = Path(__file__).resolve().parents[1]
# The Dagents checkout: two levels up inside the Dagents repository, or
# DAGENTS_HOME after scripts/extract_repo.sh moved the app into its own.
FRAMEWORK_ROOT = Path(os.environ.get("DAGENTS_HOME") or APP_ROOT.parents[1])
sys.path.insert(0, str(APP_ROOT / "backend"))
sys.path.insert(0, str(FRAMEWORK_ROOT))

from app.core.config import lma_token_variable, lma_url_variable, settings  # noqa: E402
from app.domain.dataset import SITES, load_site_records  # noqa: E402
from app.services.agents import HealthcareDemo, gma_environment, lma_environment  # noqa: E402
from app.services.site_store import PostgresSiteStore  # noqa: E402
from tests import test_pilot_parity as parity  # noqa: E402
from tests.support import HIGH_RISK_ENTRY, dagentsc_available  # noqa: E402
from tests.test_site_isolation import SupabaseLikeDatabase, admin_dsn_or_skip  # noqa: E402

#: Seconds a process may take to answer its health check.
START_SECONDS = 90
#: Seconds one public request may take. A pilot is the longest.
REQUEST_SECONDS = 600
#: Seconds a process may take to stop after SIGTERM before it is killed.
STOP_SECONDS = 20

#: Runs one ASGI app with uvicorn on a socket the parent bound and passed down.
#: ``socket.socket(fileno=...)`` reads the family and type from the descriptor.
SERVE = (
    "import socket, sys, uvicorn; "
    "sock = socket.socket(fileno=int(sys.argv[2])); "
    "uvicorn.Server(uvicorn.Config(sys.argv[1], log_level='warning')).run(sockets=[sock])"
)

#: Variables the processes keep from this environment. Every other one is left
#: out, so no LMA_, GMA_ or HEALTHCARE_DEMO_ setting of the machine leaks in.
KEPT_VARIABLES = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT", "DAGENTSC_BIN")


def _site_map(values: dict[str, str]) -> str:
    return ",".join(f"{site_id}={value}" for site_id, value in sorted(values.items()))


def _bound_socket() -> socket.socket:
    """A listening TCP socket on a free loopback port."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    return sock


class _Process:
    """One uvicorn process serving on a socket this test bound."""

    def __init__(
        self, name: str, app: str, environment: dict[str, str], log_dir: Path, paths: tuple[Path, ...] = ()
    ) -> None:
        self.name = name
        self.paths = paths
        self.sock = _bound_socket()
        self.port = self.sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.app = app
        self.environment = environment
        self.log_path = log_dir / f"{name}.log"
        self.process: subprocess.Popen[bytes] | None = None

    def start(self, extra: dict[str, str]) -> None:
        env = {name: os.environ[name] for name in KEPT_VARIABLES if name in os.environ}
        if os.sep in env.get("DAGENTSC_BIN", ""):
            # The processes run from the framework's root, not from this directory.
            env["DAGENTSC_BIN"] = os.path.abspath(env["DAGENTSC_BIN"])
        env.update(
            {
                "PYTHONPATH": os.pathsep.join(
                    [str(FRAMEWORK_ROOT), str(APP_ROOT / "backend"), *(str(path) for path in self.paths)]
                ),
                "PYTHONUNBUFFERED": "1",
                # Every call here stays on loopback.
                "NO_PROXY": "127.0.0.1,localhost",
            }
        )
        env.update(self.environment)
        env.update(extra)
        with self.log_path.open("wb") as log:
            self.process = subprocess.Popen(
                [sys.executable, "-c", SERVE, self.app, str(self.sock.fileno())],
                cwd=str(FRAMEWORK_ROOT),
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                pass_fds=(self.sock.fileno(),),
            )
        # The child holds its own copy of the socket now.
        self.sock.close()

    def log(self) -> str:
        return self.log_path.read_text(encoding="utf-8", errors="replace") if self.log_path.exists() else ""

    def wait_healthy(self) -> None:
        deadline = time.monotonic() + START_SECONDS
        while time.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                raise AssertionError(f"{self.name} exited with {self.process.returncode}:\n{self.log()[-3000:]}")
            try:
                status, _ = http_call("GET", f"{self.url}/api/v1/health", timeout=3)
            except OSError:
                status = None
            if status == 200:
                return
            time.sleep(0.25)
        raise AssertionError(f"{self.name} did not answer its health check:\n{self.log()[-3000:]}")

    def stop(self) -> None:
        self.sock.close()
        if self.process is None or self.process.poll() is not None:
            return
        self.process.send_signal(signal.SIGTERM)
        try:
            self.process.wait(timeout=STOP_SECONDS)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=STOP_SECONDS)


#: Calls this test makes go straight to loopback, never through a proxy.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_call(
    method: str,
    url: str,
    body: Any = None,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = REQUEST_SECONDS,
) -> tuple[int, Any]:
    """One HTTP request. Returns ``(status, parsed JSON body or text)``."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    text = raw.decode("utf-8", errors="replace")
    try:
        return status, json.loads(text) if text else None
    except json.JSONDecodeError:
        return status, text


class _AgentProcesses:
    """The GMA, three LMAs and the API as five processes, against one seeded Postgres database.

    :meth:`start_cluster` starts them; :meth:`tearDownClass` stops them and
    drops the database. ``agent_settings`` are added to the GMA's and each
    LMA's settings, and ``paths`` to every process's ``PYTHONPATH``.
    """

    db: SupabaseLikeDatabase
    log_dir: tempfile.TemporaryDirectory[str]
    gma: _Process
    lmas: dict[str, _Process]
    api: _Process
    tokens: dict[str, str]
    responses: list[Any]

    @classmethod
    def start_cluster(cls, agent_settings: dict[str, str] | None = None, paths: tuple[Path, ...] = ()) -> None:
        cls.db = SupabaseLikeDatabase(admin_dsn_or_skip())
        cls.log_dir = tempfile.TemporaryDirectory(prefix="healthcare-network-")
        cls.lmas = {}
        cls.responses = []
        cls.db.start()
        for site in SITES:
            store = PostgresSiteStore(site, cls.db.dsn)
            try:
                store.seed_dataset(load_site_records(site.site_id))
            finally:
                store.close()
        cls._start_processes(agent_settings or {}, paths)

    @classmethod
    def log_path(cls) -> Path:
        return Path(cls.log_dir.name)

    @classmethod
    def _start_processes(cls, agent_settings: dict[str, str], paths: tuple[Path, ...]) -> None:
        log_dir = cls.log_path()
        token = lambda: secrets.token_urlsafe(32)  # noqa: E731 - a one-line factory
        site_ids = [site.site_id for site in SITES]
        lma_site = {site_id: token() for site_id in site_ids}
        lma_coordinator = {site_id: token() for site_id in site_ids}
        gma_site = {site_id: token() for site_id in site_ids}
        admin = token()
        cls.tokens = {"gma admin": admin}
        cls.tokens.update({f"lma site {site_id}": value for site_id, value in lma_site.items()})
        cls.tokens.update({f"lma coordinator {site_id}": value for site_id, value in lma_coordinator.items()})
        cls.tokens.update({f"gma site {site_id}": value for site_id, value in gma_site.items()})

        cls.gma = _Process(
            "gma", "agents.gma.main:app", {**gma_environment(database=True), **agent_settings}, log_dir, paths
        )
        for site in SITES:
            cls.lmas[site.site_id] = _Process(
                f"lma-{site.site_id}",
                "agents.lma.main:app",
                {**lma_environment(site, database=True), **agent_settings},
                log_dir,
                paths,
            )
        cls.api = _Process(
            "api",
            "app.main:app",
            {
                "HEALTHCARE_DEMO_AGENTS": "network",
                # The tests make a pilot or two, a few probes and approvals;
                # the limits are tested in test_rate_limits.py.
                "HEALTHCARE_DEMO_RATE_LIMIT_PILOTS": "0",
                "HEALTHCARE_DEMO_PILOTS_PER_HOUR": "0",
                "HEALTHCARE_DEMO_RATE_LIMIT_APPROVALS": "0",
            },
            log_dir,
            paths,
        )

        cls.gma.start(
            {
                "GMA_DATABASE_URL": cls.db.dsn,
                "GMA_SITE_ENDPOINTS": _site_map({site_id: lma.url for site_id, lma in cls.lmas.items()}),
                "GMA_LMA_TOKENS": _site_map(lma_coordinator),
                "GMA_ADMIN_TOKEN": admin,
                "GMA_SITE_TOKENS": _site_map(gma_site),
            }
        )
        for site_id, lma in cls.lmas.items():
            lma.start(
                {
                    "LMA_DATABASE_URL": cls.db.dsn,
                    "LMA_GMA_ENDPOINT": cls.gma.url,
                    "LMA_GMA_TOKEN": gma_site[site_id],
                    "LMA_SITE_TOKEN": lma_site[site_id],
                    "LMA_COORDINATOR_TOKEN": lma_coordinator[site_id],
                }
            )
        api_extra = {
            "HEALTHCARE_DEMO_DATABASE_URL": cls.db.dsn,
            "HEALTHCARE_DEMO_GMA_URL": cls.gma.url,
            "HEALTHCARE_DEMO_GMA_TOKEN": admin,
        }
        for site_id, lma in cls.lmas.items():
            api_extra[lma_url_variable(site_id)] = lma.url
            api_extra[lma_token_variable(site_id)] = lma_site[site_id]
        for process in (cls.gma, *cls.lmas.values()):
            process.wait_healthy()
        cls.api.start(api_extra)
        cls.api.wait_healthy()

    @classmethod
    def public(cls, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        """A call to the demo API. Every answer is kept for the token and address checks."""
        status, answer = http_call(method, cls.api.url + path, body)
        cls.responses.append(answer)
        return status, answer

    @classmethod
    def tearDownClass(cls) -> None:
        for process in (getattr(cls, "api", None), *getattr(cls, "lmas", {}).values(), getattr(cls, "gma", None)):
            if process is not None:
                process.stop()
        if getattr(cls, "db", None) is not None:
            cls.db.stop()
        if getattr(cls, "log_dir", None) is not None:
            cls.log_dir.cleanup()

    def processes(self) -> list[_Process]:
        return [self.api, self.gma, *self.lmas.values()]

    def assertNoAddressOrToken(self, text: str, where: str) -> None:
        for process in self.processes():
            self.assertNotIn(f"127.0.0.1:{process.port}", text, f"{where} names the address of {process.name}")
        for name, value in self.tokens.items():
            self.assertNotIn(value, text, f"{where} holds the {name} token")


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class NetworkModeTests(_AgentProcesses, unittest.TestCase):
    """The GMA, three LMAs and the API as five processes, against one Postgres database."""

    status: dict[str, Any]
    probe: dict[str, Any]
    report: dict[str, Any]
    in_process_report: dict[str, Any]

    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.start_cluster()
            cls.status = cls.public("GET", "/api/v1/framework/status")[1]
            probe_body = {"site_id": SITES[1].site_id, "record_limit": 5}
            cls.probe = cls.public("POST", "/api/v1/governance:probe", probe_body)[1]
            status, cls.report = cls.public("POST", "/api/v1/pilot:run", {"round_prefix": "api"})
            if status != 200:
                raise AssertionError(f"the pilot over the network failed with {status}: {str(cls.report)[:500]}")
            cls.in_process_report = cls._in_process_pilot()
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def _in_process_pilot(cls) -> dict[str, Any]:
        """A pilot with the agents built in this process, on the same database."""
        demo = HealthcareDemo(
            settings=dataclasses.replace(settings, agents_mode="in-process"),
            database_url=cls.db.dsn,
            environ={},
        )
        try:
            return demo.ready().pilot.run_pilot("api")
        finally:
            demo.close()

    # --- The status report and the probe --------------------------------------------------

    def test_the_status_reports_agents_in_their_own_processes_reading_postgres(self) -> None:
        self.assertEqual("network", self.status["agents"]["mode"])
        self.assertEqual("http", self.status["agents"]["engine"])
        self.assertEqual("postgres", self.status["cohort_source"])
        self.assertTrue(self.status["ready"], self.status)
        lmas = {lma["site_id"]: lma for lma in self.status["agents"]["lmas"]}
        self.assertEqual({site.site_id for site in SITES}, set(lmas))
        for site_id, lma in lmas.items():
            with self.subTest(site_id=site_id):
                self.assertTrue(lma["reachable"])
                self.assertEqual("postgres", lma["source_kind"])
        services = {service["name"]: service["status"] for service in self.status["services"]}
        expected = {"gma", *(f"lma-{site.site_id}" for site in SITES)}
        self.assertEqual({name: "ok" for name in expected}, {name: services.get(name) for name in expected})

    def test_the_probe_is_answered_by_the_hospitals_lma(self) -> None:
        self.assertTrue(self.probe["permitted"], self.probe)
        self.assertEqual(SITES[1].site_id, self.probe["site_id"])
        calls = [(call["service"], call["site_id"], call["path"]) for call in self.probe["framework_calls"]]
        self.assertIn(("lma", SITES[1].site_id, "/api/v1/governance/restrictions:probe"), calls)

    # --- The pilot -------------------------------------------------------------------------

    def test_the_pilot_equals_a_pilot_with_the_agents_in_process_on_the_same_database(self) -> None:
        found = parity.differences(parity.parity_view(self.in_process_report), parity.parity_view(self.report))
        self.assertEqual([], found[:20], f"{len(found)} values differ between the two pilots")

    def test_the_pilot_equals_the_recorded_pilot(self) -> None:
        found = parity.differences(parity.load_fixture(), parity.parity_view(self.report))
        self.assertEqual([], found[:20], f"{len(found)} values differ from the recorded pilot")

    def test_the_pilot_lists_the_gma_calls_and_the_gmas_calls_to_each_lma(self) -> None:
        calls = self.report["framework_calls"]
        self.assertIn(
            ("gma", "POST", "/api/v1/federation/rounds", "app"),
            {(call["service"], call["method"], call["path"], call["via"]) for call in calls},
        )
        # The GMA sends each hospital one jobs:run call per round, over HTTP here.
        executed = {
            call["site_id"] for call in calls if call["via"] == "gma" and call["path"] == "/api/v1/federation/jobs:run"
        }
        self.assertEqual({site.site_id for site in SITES}, executed)
        for call in calls:
            with self.subTest(call=call):
                self.assertTrue(call["path"].startswith("/api/v1/"), call)
                self.assertNotIn("http", json.dumps(call))

    def test_the_release_decision_counts_metrics_from_the_rounds(self) -> None:
        """The GMA's engine collected the sites' results, so an approval may rely on them."""
        self.assertEqual("rounds", self.report["release"]["metrics_source"])

    def test_each_lma_process_kept_its_job_log_and_audit_chain_as_its_hospital(self) -> None:
        rounds = self.report["rounds"]
        reports = [rounds["analytics"], rounds["evaluation"], *rounds["training"], rounds["candidate_validation"]]
        expected = {
            (site.role, item["round_id"])
            for item in reports
            if item is not None
            for result in item["results"]
            for site in SITES
            if site.site_id == result["site_id"]
        }
        self.assertTrue(expected)
        with self.db.admin() as conn:
            rows = set(conn.execute("SELECT site_role, round_id FROM coordinator.site_contributions").fetchall())
        self.assertEqual(set(), expected - rows, "a hospital's LMA did not log a job of the pilot as itself")
        for site in SITES:
            with self.subTest(site_id=site.site_id):
                audit = self.report["site_audits"][site.site_id]
                self.assertTrue(audit["chain_intact"], audit)
                with self.db.admin() as conn:
                    stored = conn.execute(f"SELECT count(*) FROM {site.schema}.guard_audit").fetchone()[0]
                self.assertGreater(audit["record_count"], 0)
                self.assertGreaterEqual(stored, audit["record_count"])

    def test_every_hospital_page_is_answered_by_its_own_lma(self) -> None:
        for site in SITES:
            with self.subTest(site_id=site.site_id):
                status, page = self.public("GET", f"/api/v1/hospitals/{site.site_id}")
                self.assertEqual(200, status, page)
                self.assertEqual(self.report["model_in_use"]["version"], page["current_model"]["version"])
                self.assertTrue(page["contributions"], "the hospital page lists no job of the pilot")
                agents = {(call["service"], call["site_id"]) for call in page["framework_calls"]}
                self.assertEqual({("lma", site.site_id)}, agents)

    # --- Records, the hospital page and approval -----------------------------------------

    def test_an_entry_and_its_outcome_reach_the_hospitals_lma_at_once(self) -> None:
        site_id = SITES[2].site_id
        status, before = self.public("GET", f"/api/v1/hospitals/{site_id}")
        self.assertEqual(200, status, before)

        status, entry = self.public("POST", f"/api/v1/hospitals/{site_id}/encounters", HIGH_RISK_ENTRY)
        self.assertEqual(201, status, entry)
        self.assertEqual(before["counts"]["total"] + 1, entry["counts"]["total"])
        self.assertEqual(before["profile"]["record_count"] + 1, entry["profile"]["record_count"])
        paths = {(call["service"], call["path"]) for call in entry["framework_calls"]}
        self.assertIn(("lma", "/api/v1/site/source:refresh"), paths)

        encounter_id = entry["record"]["encounter_id"]
        status, outcome = self.public(
            "POST", f"/api/v1/hospitals/{site_id}/encounters/{encounter_id}/outcome", {"poor_outcome_6m": True}
        )
        self.assertEqual(200, status, outcome)
        self.assertEqual(before["counts"]["labelled"] + 1, outcome["counts"]["labelled"])
        missing_before = before["profile"]["fields"]["poor_outcome_6m"]["missing"]
        self.assertEqual(missing_before, outcome["profile"]["fields"]["poor_outcome_6m"]["missing"])

        status, after = self.public("GET", f"/api/v1/hospitals/{site_id}")
        self.assertEqual(200, status, after)
        self.assertEqual(outcome["counts"], after["counts"])
        self.assertEqual(after["counts"]["total"], after["profile"]["record_count"])
        self.assertIn(encounter_id, {row["encounter_id"] for row in self.entered(site_id)})

    def entered(self, site_id: str) -> list[dict[str, Any]]:
        status, body = self.public("GET", f"/api/v1/hospitals/{site_id}/encounters")
        self.assertEqual(200, status, body)
        return body["encounters"]

    def test_a_rejected_candidate_cannot_be_approved(self) -> None:
        candidate = self.report["candidate"]
        self.assertEqual("rejected", candidate["status"])
        status, body = self.public("POST", f"/api/v1/models/{candidate['candidate_version']}:approve")
        self.assertEqual(409, status, body)
        status, models = self.public("GET", "/api/v1/models")
        self.assertEqual(200, status, models)
        self.assertEqual(self.report["model_in_use"]["version"], models["model_in_use"]["version"])

    # --- Tokens and addresses ----------------------------------------------------------------

    def test_each_agent_refuses_a_call_without_the_right_token(self) -> None:
        site_id = SITES[0].site_id
        lma = self.lmas[site_id]
        status, _ = http_call("GET", f"{self.gma.url}/api/v1/model-versions", timeout=10)
        self.assertEqual(401, status, "the GMA answered the study operator's route without a token")
        site_token = {"Authorization": f"Bearer {self.tokens[f'lma site {site_id}']}"}
        status, _ = http_call("GET", f"{lma.url}/api/v1/federation/registration", headers=site_token, timeout=10)
        self.assertEqual(403, status, "a hospital view's token reached the LMA's coordinator route")
        status, _ = http_call("GET", f"{lma.url}/api/v1/site", headers=site_token, timeout=10)
        self.assertEqual(200, status, "the hospital view's token is refused on its own route")

    def test_no_response_or_log_holds_a_token_or_an_agent_address(self) -> None:
        for index, answer in enumerate(self.responses):
            self.assertNoAddressOrToken(json.dumps(answer), f"response {index}")
        for process in self.processes():
            self.assertNoAddressOrToken(process.log(), f"the log of {process.name}")


#: An extension the agents of :class:`NetworkApprovalTests` load instead of the
#: app's: the same condition pack, with one release gate that a trained
#: candidate passes. The test writes it to a scratch folder on the processes'
#: ``PYTHONPATH``, so no module of the app or its tests changes.
LENIENT_EXTENSION = """\
from agents.common.domain.federation import GateComparison, ReleaseGate
from app.domain.conditions import STROKE_CONDITION
from app.extension import HealthcareStrokeExtension

GATE = ReleaseGate(gate_id="discrimination", metric="auc", comparison=GateComparison(kind="at_least", value=0.5))


class LenientGatesExtension(HealthcareStrokeExtension):
    def condition_packs(self):
        return [STROKE_CONDITION.model_copy(update={"release_gates": [GATE]})]
"""


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class NetworkApprovalTests(_AgentProcesses, unittest.TestCase):
    """An approval across the five processes.

    The study operator approves through the demo API; the API calls the GMA's
    approval route with the admin token; the GMA's lifecycle rule releases the
    candidate; each LMA process then reads the released model from the GMA
    with its own site token, and its hospital's page scores with it.
    """

    report: dict[str, Any]
    models_before: dict[str, Any]

    @classmethod
    def setUpClass(cls) -> None:
        folder = tempfile.TemporaryDirectory(prefix="healthcare-network-extension-")
        cls.extension_dir = folder
        try:
            Path(folder.name, "healthcare_lenient_gates.py").write_text(LENIENT_EXTENSION, encoding="utf-8")
            extension = "healthcare_lenient_gates:LenientGatesExtension"
            cls.start_cluster(
                {"LMA_EXTENSIONS": extension, "GMA_EXTENSIONS": extension}, paths=(Path(folder.name),)
            )
            status, cls.report = cls.public("POST", "/api/v1/pilot:run", {"round_prefix": "api"})
            if status != 200:
                raise AssertionError(f"the pilot over the network failed with {status}: {str(cls.report)[:500]}")
            # Read before any test approves the candidate.
            cls.models_before = cls.public("GET", "/api/v1/models")[1]
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            super().tearDownClass()
        finally:
            cls.extension_dir.cleanup()

    def test_the_gates_pass_and_the_candidate_waits_for_a_person(self) -> None:
        release = self.report["release"]
        self.assertEqual("release", release["gates"]["action"], release["gates"])
        self.assertEqual("rounds", release["metrics_source"])
        self.assertEqual("candidate", self.report["candidate"]["status"])
        # Until a person approves it, the model in use stays the one the gates compared it with.
        self.assertEqual(self.report["model_in_use"]["version"], self.models_before["model_in_use"]["version"])

    def test_an_approval_releases_the_candidate_at_the_gma_and_every_lma(self) -> None:
        version = self.report["candidate"]["candidate_version"]
        status, approved = self.public("POST", f"/api/v1/models/{version}:approve")
        self.assertEqual(200, status, approved)
        self.assertEqual(version, approved["released"]["version"])
        self.assertEqual(self.report["model_in_use"]["version"], approved["previous"])
        calls = {(call["service"], call["method"], call["path"]) for call in approved["framework_calls"]}
        self.assertIn(("gma", "POST", "/api/v1/model-versions/{version}:approve"), calls)

        status, models = self.public("GET", "/api/v1/models")
        self.assertEqual(200, status, models)
        self.assertEqual(version, models["model_in_use"]["version"])
        for site in SITES:
            with self.subTest(site_id=site.site_id):
                status, page = self.public("GET", f"/api/v1/hospitals/{site.site_id}")
                self.assertEqual(200, status, page)
                self.assertEqual(version, page["current_model"]["version"])
                agents = {(call["service"], call["site_id"]) for call in page["framework_calls"]}
                self.assertEqual({("lma", site.site_id)}, agents)
                status, worklist = self.public("GET", f"/api/v1/hospitals/{site.site_id}/worklist?limit=3")
                self.assertEqual(200, status, worklist)
                self.assertTrue(worklist["worklist"])

        status, again = self.public("POST", f"/api/v1/models/{version}:approve")
        self.assertEqual(409, status, again)

    def test_no_response_or_log_holds_a_token_or_an_agent_address(self) -> None:
        for index, answer in enumerate(self.responses):
            self.assertNoAddressOrToken(json.dumps(answer), f"response {index}")
        for process in self.processes():
            self.assertNoAddressOrToken(process.log(), f"the log of {process.name}")


ENV_FILE = APP_ROOT / "env" / ".env.healthcare-demo"
COMPOSE = APP_ROOT / "docker-compose.yml"
SCRIPTS = (APP_ROOT / "scripts" / "run_local_demo.sh", APP_ROOT / "scripts" / "run_frontend_demo.sh")


def _site_key(site_id: str) -> str:
    return site_id.upper().replace("-", "_")


def _port_settings() -> dict[str, str]:
    """Every ``HEALTHCARE_DEMO_*_PORT`` the env file sets, with its value."""
    found = re.findall(r"^(HEALTHCARE_DEMO_[A-Z0-9_]*_PORT)=(\S*)\s*$", ENV_FILE.read_text(encoding="utf-8"), re.M)
    return dict(found)


def _without_comments(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


class NetworkSettingsTests(unittest.TestCase):
    """Network mode is configured in one place: each port is set once, in the env file."""

    def test_the_env_file_gives_the_gma_and_each_hospitals_lma_a_port_of_its_own(self) -> None:
        ports = _port_settings()
        expected = {"HEALTHCARE_DEMO_API_PORT", "HEALTHCARE_DEMO_UI_PORT", "HEALTHCARE_DEMO_GMA_PORT"}
        expected |= {f"HEALTHCARE_DEMO_LMA_{_site_key(site.site_id)}_PORT" for site in SITES}
        self.assertEqual(set(), expected - set(ports), "a port is missing from the env file")
        for name in expected:
            with self.subTest(name=name):
                self.assertRegex(ports[name], r"^[0-9]+$")
        values = [ports[name] for name in expected]
        self.assertEqual(len(values), len(set(values)), f"two services share a port: {sorted(values)}")

    def test_the_start_scripts_write_no_port(self) -> None:
        ports = _port_settings()
        for path in SCRIPTS:
            text = _without_comments(path.read_text(encoding="utf-8"))
            for name, value in ports.items():
                with self.subTest(script=path.name, name=name):
                    self.assertIsNone(
                        re.search(rf"(?<![0-9]){value}(?![0-9])", text), f"{path.name} writes {value} ({name})"
                    )

    def test_the_compose_file_takes_the_agent_and_api_ports_from_the_env_file(self) -> None:
        """The UI's container port is the one its dev script binds, so only the host side is checked for it."""
        text = _without_comments(COMPOSE.read_text(encoding="utf-8"))
        ports = {name: value for name, value in _port_settings().items() if name != "HEALTHCARE_DEMO_UI_PORT"}
        for name, value in ports.items():
            with self.subTest(name=name):
                self.assertIn("${" + name, text, f"the compose file does not read {name}")
                self.assertIsNone(re.search(rf"(?<![0-9]){value}(?![0-9])", text), f"the compose file writes {value}")
        self.assertIn("${HEALTHCARE_DEMO_UI_PORT", text)

    def test_the_compose_file_runs_one_lma_per_hospital(self) -> None:
        text = COMPOSE.read_text(encoding="utf-8")
        services = set(re.findall(r"^  ([a-z0-9-]+):\s*$", text, re.M))
        self.assertIn("gma", services)
        self.assertEqual(
            {f"lma-{site.site_id}" for site in SITES},
            {name for name in services if name.startswith("lma-")},
        )
        for site in SITES:
            with self.subTest(site_id=site.site_id):
                self.assertIn(f"/run/agent-settings/lma-{site.site_id}.env", text)
                self.assertIn(f"${{HEALTHCARE_DEMO_LMA_{_site_key(site.site_id)}_PORT}}", text)

    def test_no_file_holds_a_token(self) -> None:
        """Tokens are made for each run; none is written in a script, the compose file or the env file."""
        assignment = re.compile(r"^\s*-?\s*(?:export\s+)?[A-Z_]*TOKENS?[=:]\s*['\"]?([A-Za-z0-9_-]{16,})", re.M)
        for path in (*SCRIPTS, COMPOSE, ENV_FILE):
            with self.subTest(path=path.name):
                self.assertEqual([], assignment.findall(path.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
