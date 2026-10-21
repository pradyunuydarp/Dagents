"""When a GMA recovers the rounds a restart left unfinished, and the age check before each dispatch.

A GMA recovers once (``GmaState.ensure_recovered``). Its lifespan tries when
uvicorn starts it, and logs a failure instead of raising, so the GMA still
serves. The round write routes (dispatch, posted results, ``:aggregate``,
``:discard``, ``:evaluate-release`` and the update upload) wait for it, which
covers a GMA run in process, where no lifespan runs, and a recovery that
failed. Reads, ``/health`` and approval never wait for it. Recovery runs on the
GMA's own handler threads, never on the event loop or a server thread, so an
upload that waits for it holds no server thread.

Most tests stand ``FederatedRoundController.recover`` in with a fake that
counts its calls, records its thread and can block or fail, so they need no
database and no planner. The age check tests run the real planner on the HTTP
engine with fixture LMAs: before each dispatch, the planner's recovery rule
decides about every round whose oldest stored update is older than the limit.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import socket
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from unittest import mock

import httpx
import uvicorn
from fastapi.testclient import TestClient

from dagents.common.application.federation import (
    FederatedRoundController,
    InMemoryRoundRepository,
    RecoveryReport,
)
from dagents.common.domain.errors import RoundConflict, StoreUnavailableError
from dagents.common.domain.federation import SiteResult, UpdateUpload
from dagents.common.infrastructure import dagents_runner
from dagents.common.infrastructure.agent_client import GMA_AGENT, AgentNetwork
from dagents.common.infrastructure.dagents_runner import PlannerUnavailableError
from dagents.gma import routes as gma_routes
from dagents.gma.app_factory import HANDLER_THREAD_NAME, GmaDeps, create_app
from dagents.gma.config import Settings
from dagents.gma.infrastructure.update_store import InMemoryUpdateStore
from dagents.tests.fastapi_routes import api_routes
from dagents.tests.fixtures.gma_fixtures import INITIAL_VERSION, register_models, toy_manifest, toy_registry
from dagents.tests.fixtures.toy_extension import ToyLinearKind
from dagents.tests.test_federation import SITES as SIMULATED_SITES
from dagents.tests.test_federation import ScriptedEngine, build_manifest, build_registration, build_worker
from dagents.tests.test_governance import dagentsc_available
from dagents.tests.test_round_recovery import UPDATE, Clock, Stores, StudyTestCase, became_aggregated
from dagents.tests.test_round_store import training_record

#: The ports the uvicorn test may bind.
PORTS = range(9615, 9635)

#: A stored round the write routes can refer to, and a round id no store holds.
STORED = "round-0042"
UNKNOWN = "round-none"

#: How long a test waits for something that should happen at once.
PROMPT_S = 10.0

#: The round write routes, as ``(method, path)``: each waits for recovery.
ROUND_WRITES = {
    ("POST", "/federation/rounds"),
    ("POST", "/api/v1/federation/rounds"),
    ("POST", "/federation/rounds/{round_id}/results"),
    ("POST", "/api/v1/federation/rounds/{round_id}/results"),
    ("POST", "/federation/rounds/{round_id}:aggregate"),
    ("POST", "/api/v1/federation/rounds/{round_id}:aggregate"),
    ("POST", "/federation/rounds/{round_id}:discard"),
    ("POST", "/api/v1/federation/rounds/{round_id}:discard"),
    ("POST", "/federation/rounds/{round_id}:evaluate-release"),
    ("POST", "/api/v1/federation/rounds/{round_id}:evaluate-release"),
    ("PUT", "/federation/rounds/{round_id}/updates/{site_id}"),
    ("PUT", "/api/v1/federation/rounds/{round_id}/updates/{site_id}"),
}


def result_body(round_id: str) -> dict[str, Any]:
    return SiteResult(
        round_id=round_id,
        site_id="hospital-a",
        job_digest="fnv1a:0123456789abcdef",
        participation="completed",
        code_verified=True,
        privacy_checks_passed=True,
        contributed_examples=30,
        metrics={"accuracy": 0.7},
    ).model_dump(mode="json")


def upload_body() -> dict[str, Any]:
    return UpdateUpload(job_digest="fnv1a:0123456789abcdef", update=dict(UPDATE)).model_dump(mode="json")


def round_writes() -> list[tuple[str, str, dict[str, Any], int]]:
    """One request per round write route: method, path, body, and the status it gets once recovery is done.

    None of them needs the planner: the dispatch reuses a stored round id, and
    the others name a round no store holds.
    """
    manifest = build_manifest(round_id=STORED).model_dump(mode="json")
    requests = []
    for prefix in ("", "/api/v1"):
        requests += [
            ("POST", f"{prefix}/federation/rounds", manifest, 409),
            ("POST", f"{prefix}/federation/rounds/{UNKNOWN}/results", result_body(UNKNOWN), 404),
            ("POST", f"{prefix}/federation/rounds/{UNKNOWN}:aggregate", {}, 404),
            ("POST", f"{prefix}/federation/rounds/{UNKNOWN}:discard", {"reason": "a test gives it up"}, 404),
            ("POST", f"{prefix}/federation/rounds/{UNKNOWN}:evaluate-release", {"candidate_version": "v2"}, 404),
            ("PUT", f"{prefix}/federation/rounds/{UNKNOWN}/updates/hospital-a", upload_body(), 404),
        ]
    return requests


def reads() -> list[tuple[str, str, int]]:
    """Reads, ``/health`` and approval: none of them waits for recovery."""
    requests = [("GET", "/health", 200), ("GET", "/api/v1/health", 200)]
    for prefix in ("", "/api/v1"):
        requests += [
            ("GET", f"{prefix}/federation/rounds", 200),
            ("GET", f"{prefix}/federation/rounds/{STORED}", 200),
            ("GET", f"{prefix}/federation/rounds/{UNKNOWN}/readiness", 404),
            ("GET", f"{prefix}/federation/sites", 200),
            ("GET", f"{prefix}/model-versions", 200),
            ("GET", f"{prefix}/governance/audit:summary", 200),
            ("POST", f"{prefix}/model-versions/v-none:approve", 404),
        ]
    return requests


class FakeRecovery:
    """Stands in for ``FederatedRoundController.recover``.

    It records each call's arguments and thread, can fail its next calls, and
    can block until a test releases it.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.threads: list[str] = []
        self.failures: list[BaseException] = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self._lock = threading.Lock()

    def block(self) -> None:
        """Make the next calls wait until :attr:`release` is set."""
        self.release.clear()

    def fail_next(self, *errors: BaseException) -> None:
        self.failures.extend(errors)

    def __call__(self, controller: FederatedRoundController, **arguments: Any) -> RecoveryReport:
        with self._lock:
            self.calls.append(arguments)
            self.threads.append(threading.current_thread().name)
            failure = self.failures.pop(0) if self.failures else None
        self.started.set()
        if not self.release.wait(PROMPT_S):
            raise AssertionError("the test never released the recovery")
        if failure is not None:
            raise failure
        return RecoveryReport(rounds=(), deleted_updates=())


class SurvivingUpdateStore(InMemoryUpdateStore):
    """A memory update store that says it keeps its updates after a restart, as a database store does."""

    survives_restart = True


def http_settings(**values: Any) -> Settings:
    """Settings for a GMA with the HTTP engine, the engine that reads and deletes stored updates.

    Nothing listens at the LMA's address; the tests that use these settings call no LMA.
    """
    return Settings(engine="http", site_endpoints={"hospital-a": "http://127.0.0.1:9"}, **values)


class RecoveryTestCase(unittest.TestCase):
    """A GMA with the simulator, one stored round, and a fake recovery."""

    def setUp(self) -> None:
        self.recovery = FakeRecovery()
        patcher = mock.patch.object(FederatedRoundController, "recover", autospec=True, side_effect=self.recovery)
        patcher.start()
        self.addCleanup(patcher.stop)

    def gma(self, settings: Settings | None = None, **deps: Any) -> Any:
        rounds = deps.pop("rounds", None) or InMemoryRoundRepository()
        if rounds.get(STORED) is None:
            rounds.create(training_record(status="collecting", round_id=STORED))
        app = create_app(settings or Settings(), GmaDeps(rounds=rounds, **deps))
        self.addCleanup(app.state.gma.close)
        return app

    def assert_on_handler_threads(self) -> None:
        self.assertTrue(self.recovery.threads)
        for name in self.recovery.threads:
            self.assertTrue(name.startswith(HANDLER_THREAD_NAME), f"recovery ran on {name!r}")


class RecoveryRouteTests(RecoveryTestCase):
    """Which routes wait for recovery, how often it runs, and what a failure answers."""

    def test_only_the_round_write_routes_wait_for_recovery(self) -> None:
        app = self.gma()
        waiting = set()
        for route in api_routes(app):
            calls = [dependency.call for dependency in route.dependant.dependencies]
            if gma_routes.require_recovered in calls:
                waiting.update((method, route.path) for method in route.methods)
        self.assertEqual(ROUND_WRITES, waiting)

    def test_each_round_write_runs_recovery_once_on_the_handler_threads(self) -> None:
        client = TestClient(self.gma())
        for _ in range(2):
            for method, path, body, status in round_writes():
                with self.subTest(method=method, path=path):
                    answer = client.request(method, path, json=body)
                    self.assertEqual(status, answer.status_code, answer.text)
        self.assertEqual(1, len(self.recovery.calls))
        self.assert_on_handler_threads()

    def test_reads_health_and_approval_never_run_or_wait_for_recovery(self) -> None:
        client = TestClient(self.gma())
        # A recovery that would block forever: a read that waited for it would never answer.
        self.recovery.block()
        for method, path, status in reads():
            with self.subTest(method=method, path=path):
                self.assertEqual(status, client.request(method, path).status_code)
        self.assertEqual([], self.recovery.calls)
        self.recovery.release.set()

    def test_a_failed_recovery_answers_503_on_round_writes_and_the_next_write_tries_again(self) -> None:
        secret = "postgresql://gma:s3cret@db.internal.invalid/coordinator"
        self.recovery.fail_next(
            StoreUnavailableError("coordinator: could not connect to the database"),
            PlannerUnavailableError("the planner could not be run"),
            RuntimeError(f"an error that quotes {secret}"),
        )
        client = TestClient(self.gma())
        writes = round_writes()
        for attempt, (method, path, body, _) in enumerate(writes[:3], start=1):
            with self.subTest(attempt=attempt):
                answer = client.request(method, path, json=body)
                self.assertEqual(503, answer.status_code, answer.text)
                self.assertEqual("recovery_failed", answer.json()["code"])
                self.assertIn("next round write tries again", answer.json()["detail"])
                self.assertNotIn("s3cret", answer.text)
                self.assertNotIn("db.internal.invalid", answer.text)
                self.assertEqual(attempt, len(self.recovery.calls))
                # Reads still answer while recovery fails.
                self.assertEqual(200, client.get("/api/v1/federation/rounds").status_code)
                self.assertEqual(200, client.get("/health").status_code)
        # The fourth write recovers, and later writes do not run it again.
        for method, path, body, status in writes[3:]:
            self.assertEqual(status, client.request(method, path, json=body).status_code)
        self.assertEqual(4, len(self.recovery.calls))
        self.assert_on_handler_threads()

    def test_the_cause_of_a_failed_recovery_is_named_by_its_code(self) -> None:
        self.recovery.fail_next(StoreUnavailableError("coordinator: the database operation failed"))
        answer = TestClient(self.gma()).post(f"/federation/rounds/{UNKNOWN}:aggregate")
        self.assertEqual((503, "recovery_failed"), (answer.status_code, answer.json()["code"]))
        self.assertIn("store_unavailable", answer.json()["detail"])

    def test_recovery_is_told_whether_the_updates_survive_a_restart_and_the_age_limit(self) -> None:
        # The age limit applies when the update store survives a restart, or when
        # GMA_UPDATE_MAX_AGE_SECONDS is set. A GMA that sets neither runs no age check.
        cases = (
            ("default memory update store, no setting", Settings(), {}, False, None),
            ("HTTP engine, default memory update store, no setting", http_settings(), {}, False, None),
            (
                "memory update store in GmaDeps, a limit of one minute",
                http_settings(update_max_age_seconds=60.0),
                {"updates": InMemoryUpdateStore()},
                False,
                60.0,
            ),
            (
                "update store that survives a restart, no setting",
                http_settings(),
                {"updates": SurvivingUpdateStore()},
                True,
                86400.0,
            ),
            (
                "update store that survives a restart, a limit of one minute",
                http_settings(update_max_age_seconds=60.0),
                {"updates": SurvivingUpdateStore()},
                True,
                60.0,
            ),
        )
        for name, settings, deps, kept, limit in cases:
            with self.subTest(name):
                self.recovery.calls.clear()
                app = self.gma(settings, **deps)
                # The controller checks ages before each dispatch with the same limit.
                self.assertEqual(limit, app.state.gma.update_age_limit_seconds)
                self.assertEqual(limit, app.state.gma.rounds._update_max_age_seconds)  # noqa: SLF001 - the wiring under test
                before = time.time()
                answer = TestClient(app).post(f"/federation/rounds/{UNKNOWN}:aggregate")
                after = time.time()
                self.assertEqual(404, answer.status_code, answer.text)
                [arguments] = self.recovery.calls
                self.assertEqual({"updates_kept", "now", "update_max_age_seconds"}, set(arguments))
                self.assertIs(kept, arguments["updates_kept"])
                self.assertEqual(limit, arguments["update_max_age_seconds"])
                self.assertTrue(before <= arguments["now"] <= after)

    def test_round_writes_that_arrive_together_run_recovery_once(self) -> None:
        client = TestClient(self.gma())
        self.recovery.block()
        writes = round_writes()
        with ThreadPoolExecutor(max_workers=len(writes)) as pool:
            futures = [pool.submit(client.request, method, path, json=body) for method, path, body, _ in writes]
            self.assertTrue(self.recovery.started.wait(PROMPT_S))
            # Every write waits while the one recovery runs.
            time.sleep(0.2)
            self.assertFalse(any(future.done() for future in futures))
            self.recovery.release.set()
            answers = [future.result(timeout=PROMPT_S) for future in futures]
        self.assertEqual([status for *_, status in writes], [answer.status_code for answer in answers])
        self.assertEqual(1, len(self.recovery.calls))
        self.assert_on_handler_threads()


class BlockingReadRepository(InMemoryRoundRepository):
    """A memory round store whose ``list`` waits until a test releases it, holding a server thread."""

    def __init__(self) -> None:
        super().__init__()
        self.listing = threading.Event()
        self.release = threading.Event()

    def list(self, limit: int = 20):  # noqa: ANN201 - same signature as the parent
        self.listing.set()
        if not self.release.wait(PROMPT_S * 3):
            raise AssertionError("the test never released the read")
        return super().list(limit)


class RecoveryThreadTests(RecoveryTestCase):
    """Recovery holds no server thread and never runs on the event loop."""

    def test_an_upload_that_waits_for_recovery_holds_no_server_thread(self) -> None:
        rounds = BlockingReadRepository()
        app = self.gma(rounds=rounds)
        # One server thread for the GMA's sync handlers, as a busy GMA would have left.
        network = AgentNetwork.in_process(thread_tokens=1, timeout_s=PROMPT_S * 3)
        self.addCleanup(network.close)
        network.add_app(GMA_AGENT, app)
        transport = network._transport  # noqa: SLF001 - raw requests through the in-process network

        def send(method: str, path: str, body: Any = None) -> Any:
            return transport.send(GMA_AGENT, method, path, None, body, {}, PROMPT_S * 3)

        self.recovery.block()
        with ThreadPoolExecutor(max_workers=3) as pool:
            # A read takes the only server thread and keeps it.
            read = pool.submit(send, "GET", "/api/v1/federation/rounds")
            self.assertTrue(rounds.listing.wait(PROMPT_S))
            upload = pool.submit(send, "PUT", f"/api/v1/federation/rounds/{UNKNOWN}/updates/hospital-a", upload_body())
            self.assertTrue(self.recovery.started.wait(PROMPT_S), "the upload did not start the recovery")
            # The event loop is free while recovery runs: a request that needs no thread answers.
            started = time.monotonic()
            self.assertEqual(404, send("GET", "/no-such-path").status)
            self.assertLess(time.monotonic() - started, 2.0)
            self.assertFalse(upload.done())
            self.recovery.release.set()
            # The upload finishes while the read still holds the only server thread.
            self.assertEqual(404, upload.result(timeout=PROMPT_S).status)
            self.assertFalse(read.done())
            rounds.release.set()
            self.assertEqual(200, read.result(timeout=PROMPT_S).status)
        self.assertEqual(1, len(self.recovery.calls))
        self.assert_on_handler_threads()


class LifespanRecoveryTests(RecoveryTestCase):
    """The lifespan recovers once and logs a failure instead of raising."""

    def test_the_lifespan_recovers_once_before_the_first_request(self) -> None:
        with TestClient(self.gma()) as client:
            self.assertEqual(1, len(self.recovery.calls))
            for method, path, body, status in round_writes():
                self.assertEqual(status, client.request(method, path, json=body).status_code)
        self.assertEqual(1, len(self.recovery.calls))
        self.assert_on_handler_threads()

    def test_a_failed_recovery_at_start_is_logged_and_the_gma_still_serves(self) -> None:
        self.recovery.fail_next(StoreUnavailableError("coordinator: could not connect to the database"))
        with self.assertLogs("dagents.gma.app_factory", logging.ERROR) as logs:
            with TestClient(self.gma()) as client:
                self.assertEqual(200, client.get("/health").status_code)
                self.assertEqual(200, client.get("/api/v1/federation/rounds").status_code)
                # The next round write tries again, and succeeds.
                answer = client.post(f"/federation/rounds/{UNKNOWN}:aggregate")
                self.assertEqual(404, answer.status_code, answer.text)
        self.assertEqual(2, len(self.recovery.calls))
        self.assertTrue(any("round writes answer 503" in line for line in logs.output), logs.output)

    def test_a_recovery_that_fails_after_the_start_wait_is_tried_again_by_the_next_round_write(self) -> None:
        app = self.gma()
        app.state.gma.start_recovery_wait_seconds = 0.3
        self.recovery.block()
        self.recovery.fail_next(StoreUnavailableError("coordinator: the database operation failed"))
        with self.assertLogs("dagents.gma.app_factory", logging.WARNING) as logs:
            with TestClient(app) as client:
                # The lifespan stopped waiting; the GMA serves while the recovery runs.
                self.assertTrue(any("before its recovery finished" in line for line in logs.output), logs.output)
                self.assertEqual(200, client.get("/health").status_code)
                self.recovery.release.set()
                deadline = time.monotonic() + PROMPT_S
                while not any("could not recover" in line for line in logs.output):
                    self.assertLess(time.monotonic(), deadline, "the recovery never ended")
                    time.sleep(0.02)
                self.assertFalse(app.state.gma.recovered)
                answer = client.post(f"/federation/rounds/{UNKNOWN}:aggregate")
                self.assertEqual(404, answer.status_code, answer.text)
        self.assertEqual(2, len(self.recovery.calls))
        self.assertTrue(app.state.gma.recovered)

    def test_a_recovery_that_fails_after_the_start_wait_leaves_no_unread_task_error(self) -> None:
        # Once the wait ends, nothing awaits the recovery's task. Its failure must
        # still be read, or asyncio reports "Task exception was never retrieved".
        state = self.gma().state.gma
        state.start_recovery_wait_seconds = 0.2
        self.recovery.block()
        self.recovery.fail_next(StoreUnavailableError("coordinator: the database operation failed"))

        async def start_then_fail() -> list[str]:
            reported: list[str] = []
            asyncio.get_running_loop().set_exception_handler(lambda _, context: reported.append(context["message"]))
            await state.recover_at_start()
            task = state._start_recovery  # noqa: SLF001 - the task under test
            self.assertIsNotNone(task)
            self.recovery.release.set()
            deadline = time.monotonic() + PROMPT_S
            while not task.done():
                self.assertLess(time.monotonic(), deadline, "the recovery never ended")
                await asyncio.sleep(0.01)
            state.stop_start_recovery()
            del task
            gc.collect()
            await asyncio.sleep(0)
            return reported

        with self.assertLogs("dagents.gma.app_factory", logging.WARNING):
            reported = asyncio.run(start_then_fail())
        self.assertEqual([], reported)
        self.assertFalse(state.recovered)

    def test_uvicorn_still_serves_health_when_recovery_fails_at_start(self) -> None:
        self.recovery.fail_next(PlannerUnavailableError("the planner could not be run"))
        app = self.gma()
        sock = bound_socket()
        self.addCleanup(sock.close)
        server = uvicorn.Server(uvicorn.Config(app, log_config=None, log_level="critical", lifespan="on"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        with self.assertLogs("dagents.gma.app_factory", logging.ERROR):
            thread.start()
            deadline = time.monotonic() + PROMPT_S
            while not server.started:
                self.assertTrue(thread.is_alive(), "uvicorn stopped: the lifespan raised")
                self.assertLess(time.monotonic(), deadline, "uvicorn did not start")
                time.sleep(0.02)
        try:
            url = f"http://127.0.0.1:{sock.getsockname()[1]}"
            with httpx.Client(base_url=url, trust_env=False, timeout=PROMPT_S) as client:
                self.assertEqual(200, client.get("/health").status_code)
                failed = client.post(f"/federation/rounds/{UNKNOWN}:aggregate")
                self.assertEqual(404, failed.status_code, failed.text)
        finally:
            server.should_exit = True
            thread.join(PROMPT_S)
        self.assertEqual(2, len(self.recovery.calls))

    def test_uvicorn_serves_health_once_the_start_wait_ends_while_recovery_still_runs(self) -> None:
        # A database that stops answering holds the recovery. The lifespan waits a
        # bounded time for it, then the GMA serves while the recovery goes on.
        app = self.gma()
        state = app.state.gma
        state.start_recovery_wait_seconds = 0.5
        self.recovery.block()
        sock = bound_socket()
        self.addCleanup(sock.close)
        server = uvicorn.Server(uvicorn.Config(app, log_config=None, log_level="critical", lifespan="on"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        try:
            with self.assertLogs("dagents.gma.app_factory", logging.ERROR) as logs:
                started = time.monotonic()
                thread.start()
                while not server.started:
                    self.assertTrue(thread.is_alive(), "uvicorn stopped: the lifespan raised")
                    self.assertLess(time.monotonic() - started, PROMPT_S, "uvicorn did not start within the wait")
                    time.sleep(0.02)
            self.assertGreaterEqual(time.monotonic() - started, 0.5)
            self.assertTrue(any("before its recovery finished" in line for line in logs.output), logs.output)
            self.assertTrue(self.recovery.started.is_set())
            self.assertFalse(state.recovered)

            url = f"http://127.0.0.1:{sock.getsockname()[1]}"
            with httpx.Client(base_url=url, trust_env=False, timeout=PROMPT_S) as client:
                self.assertEqual(200, client.get("/health").status_code)
                with ThreadPoolExecutor(max_workers=1) as pool:
                    # A round write waits for the recovery that is still running.
                    write = pool.submit(client.post, f"/federation/rounds/{UNKNOWN}:aggregate")
                    time.sleep(0.3)
                    self.assertFalse(write.done())
                    self.recovery.release.set()
                    answer = write.result(timeout=PROMPT_S)
                self.assertEqual(404, answer.status_code, answer.text)
            self.assertTrue(state.recovered)
        finally:
            self.recovery.release.set()
            server.should_exit = True
            thread.join(PROMPT_S)
        # The write waited for the one recovery the lifespan started; it did not run another.
        self.assertEqual(1, len(self.recovery.calls))
        self.assert_on_handler_threads()


def bound_socket() -> socket.socket:
    """A loopback socket bound to the first free port of :data:`PORTS`."""
    for port in PORTS:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            sock.close()
            continue
        return sock
    raise unittest.SkipTest(f"no free port in {PORTS.start}-{PORTS.stop - 1}")


# --------------------------------------------------------------------------- #
# The age check before each dispatch
# --------------------------------------------------------------------------- #


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class UpdateAgeCheckTests(StudyTestCase):
    """Before each dispatch, the planner decides about each round whose oldest stored update is older than the limit."""

    LIMIT = 60.0

    def setUp(self) -> None:
        self.extension = None
        # The update store records each update's arrival with this clock; the check reads the real one.
        self.clock = Clock(time.time())
        self.start(Stores(clock=self.clock))
        self.digests = register_models(self.gma)
        self.gma.refresh_sites()

    def start(self, stores: Stores) -> None:
        from dagents.tests.fixtures.gma_fixtures import build_study

        self.stores = stores
        self.study = build_study(registry=toy_registry(), stores=stores.deps(), update_max_age_seconds=self.LIMIT)
        self.addCleanup(self.study.close)
        self.gma = self.study.gma()

    def training(self, round_id: str, age: float = 0.0):  # noqa: ANN201 - a RoundRecord
        """Dispatch a training round whose updates arrive ``age`` seconds before now."""
        self.clock.now = time.time() - age
        try:
            return self.gma.dispatch_round(toy_manifest(round_id, "training", INITIAL_VERSION, self.digests[INITIAL_VERSION]))
        finally:
            self.clock.now = time.time()

    def recover_calls(self, planner: mock.MagicMock) -> list[dict[str, Any]]:
        return [call.args[1] for call in planner.call_args_list if call.args[0][:3] == ["federation", "round", "recover"]]

    def test_a_round_whose_oldest_update_is_older_than_the_limit_is_closed_before_the_next_dispatch(self) -> None:
        self.training("round-old", age=1000.0)
        self.assertEqual(["round-old"], self.stores.updates.pending_rounds())

        with mock.patch.object(dagents_runner, "run_dagentsc", wraps=dagents_runner.run_dagentsc) as planner:
            record = self.training("round-new")
        self.assertEqual("collecting", record.status)
        [asked] = self.recover_calls(planner)
        self.assertEqual(("collecting", "training", True, False), (
            asked["status"], asked["phase"], asked["updates_kept"], asked["candidate_registered"]))
        self.assertEqual(self.LIMIT, asked["update_max_age_seconds"])
        self.assertGreaterEqual(asked["oldest_update_age_seconds"], 1000.0)

        old = self.stores.rounds.stored("round-old")
        self.assertEqual("closed", old.status)
        self.assertIn("60 seconds", old.abandoned_reason)
        self.assertIn(("round-old", "closed"), self.stores.updates.discards)
        self.assertEqual(["round-new"], self.stores.updates.pending_rounds())
        # The update store refuses later uploads for the closed round.
        with self.assertRaises(RoundConflict) as refused:
            self.stores.updates.put(
                old, "site-a", UpdateUpload(job_digest=old.plan.round_digest, update=dict(UPDATE)), ToyLinearKind()
            )
        self.assertIn("discarded", str(refused.exception))

    def test_a_round_younger_than_the_limit_is_not_asked_about(self) -> None:
        self.training("round-young", age=10.0)
        with mock.patch.object(dagents_runner, "run_dagentsc", wraps=dagents_runner.run_dagentsc) as planner:
            self.training("round-next")
        self.assertEqual([], self.recover_calls(planner))
        self.assertEqual("collecting", self.stores.rounds.stored("round-young").status)
        self.assertEqual(["round-next", "round-young"], self.stores.updates.pending_rounds())

    def test_the_planner_keeps_an_old_round_whose_candidate_is_registered(self) -> None:
        self.training("round-registered", age=1000.0)
        # The aggregated write fails once: the candidate is registered, the round stays collecting.
        self.stores.rounds.fail_next(became_aggregated)
        self.assert_agent_error(lambda: self.gma.aggregate("round-registered"), 503, "store_unavailable")

        with mock.patch.object(dagents_runner, "run_dagentsc", wraps=dagents_runner.run_dagentsc) as planner:
            self.training("round-next")
        [asked] = self.recover_calls(planner)
        self.assertTrue(asked["candidate_registered"])
        stored = self.stores.rounds.stored("round-registered")
        self.assertEqual(("collecting", None), (stored.status, stored.abandoned_reason))
        # A retried aggregate finishes the round.
        self.gma.aggregate("round-registered")
        self.assertEqual("aggregated", self.stores.rounds.stored("round-registered").status)

    def test_an_old_round_whose_dispatch_stopped_is_closed_by_the_planner(self) -> None:
        self.training("round-cut", age=1000.0)
        # As a dispatch that stopped leaves it: dispatched, no results, its updates stored.
        self.stores.rounds.update(
            "round-cut", lambda record: record.model_copy(update={"status": "dispatched", "results": [], "readiness": None})
        )
        self.training("round-next")
        stored = self.stores.rounds.stored("round-cut")
        self.assertEqual("closed", stored.status)
        self.assertIn("dispatch", stored.abandoned_reason)
        self.assertEqual(["round-next"], self.stores.updates.pending_rounds())

    def test_old_updates_of_a_finished_round_are_deleted_before_the_next_dispatch(self) -> None:
        self.training("round-aggregated", age=1000.0)
        self.stores.updates.fail_next_discard = True
        with self.assertLogs("dagents.common.application.federation", "WARNING"):
            self.gma.aggregate("round-aggregated")
        self.assertEqual(["round-aggregated"], self.stores.updates.pending_rounds())

        self.training("round-next")
        self.assertEqual(["round-next"], self.stores.updates.pending_rounds())
        self.assertEqual("aggregated", self.stores.rounds.stored("round-aggregated").status)


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class NoAgeCheckByDefaultTests(unittest.TestCase):
    """A GMA that keeps its updates in memory and sets no age limit runs no age check, as before the limit existed."""

    def test_a_round_whose_updates_are_a_day_old_stays_collecting_and_still_aggregates(self) -> None:
        from dagents.tests.fixtures.gma_fixtures import build_study

        # GMA_ENGINE=http, every store in memory, none of the settings that keep round state.
        study = build_study(registry=toy_registry())
        self.addCleanup(study.close)
        gma = study.gma()
        digests = register_models(gma)
        gma.refresh_sites()
        state = study.gma_app.state.gma
        self.assertIsInstance(state.updates, InMemoryUpdateStore)
        self.assertIsNone(state.update_age_limit_seconds)

        # The updates of round-a arrive 25 hours before now, more than the 24-hour default limit.
        with mock.patch.object(state.updates, "_clock", lambda: time.time() - 25 * 3600):
            gma.dispatch_round(toy_manifest("round-a", "training", INITIAL_VERSION, digests[INITIAL_VERSION]))
        self.assertGreater(state.updates.pending_ages(time.time())["round-a"], 86400.0)

        with mock.patch.object(dagents_runner, "run_dagentsc", wraps=dagents_runner.run_dagentsc) as planner:
            gma.dispatch_round(toy_manifest("round-b", "training", INITIAL_VERSION, digests[INITIAL_VERSION]))
        self.assertNotIn(["federation", "round", "recover"], [call.args[0][:3] for call in planner.call_args_list])
        self.assertEqual("collecting", state.rounds.get_round("round-a").status)
        self.assertEqual("toy-linear+round-a", gma.aggregate("round-a").candidate_version)


class AgedEngine(ScriptedEngine):
    """The scripted simulator, reporting stored update ages a test sets."""

    def __init__(self) -> None:
        super().__init__()
        self.ages: dict[str, float] = {}

    def pending_update_ages(self, now: float) -> dict[str, float]:
        return dict(self.ages)


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class DispatchingRoundAgeTests(unittest.TestCase):
    """The age check never closes a round this process is still dispatching."""

    def setUp(self) -> None:
        self.engine = AgedEngine()
        self.controller = FederatedRoundController(engine=self.engine, update_max_age_seconds=60.0)
        for site_id in SIMULATED_SITES:
            self.controller.register_site(build_registration(site_id))
        self.manifest = build_manifest(round_id="round-a")
        digest = self.controller.digest(self.manifest)
        for site_id in SIMULATED_SITES:
            self.engine.register_worker(build_worker(site_id, expected_digest=digest))

    def test_a_round_in_dispatch_is_skipped_and_checked_once_its_dispatch_ends(self) -> None:
        reports: list[RecoveryReport] = []

        def during_dispatch(manifest, plan) -> None:  # noqa: ANN001 - the engine hook's signature
            # The round's updates are older than the limit while its jobs still run.
            self.engine.ages = {manifest.round_id: 1000.0}
            reports.append(self.controller.expire_updates(now=time.time(), update_max_age_seconds=60.0))

        self.engine.on_submit = during_dispatch
        with mock.patch.object(dagents_runner, "run_dagentsc", wraps=dagents_runner.run_dagentsc) as planner:
            record = self.controller.dispatch_round(self.manifest)
        self.assertEqual("collecting", record.status)
        self.assertEqual([RecoveryReport(rounds=(), deleted_updates=())], reports)
        self.assertNotIn(["federation", "round", "recover"], [call.args[0][:3] for call in planner.call_args_list])
        self.assertEqual([], self.engine.discarded)

        # The next dispatch checks the round, and the planner closes it.
        self.engine.on_submit = None
        self.controller.dispatch_round(build_manifest(round_id="round-b"))
        stored = self.controller.get_round("round-a")
        self.assertEqual("closed", stored.status)
        self.assertIn("60 seconds", stored.abandoned_reason)
        self.assertEqual(["round-a"], self.engine.discarded)

    def test_a_refused_second_dispatch_of_a_round_in_dispatch_leaves_it_in_dispatch(self) -> None:
        errors: list[BaseException] = []
        reports: list[RecoveryReport] = []

        def during_dispatch(manifest, plan) -> None:  # noqa: ANN001 - the engine hook's signature
            self.engine.on_submit = None
            try:
                self.controller.dispatch_round(manifest)
            except BaseException as error:  # noqa: BLE001 - checked below
                errors.append(error)
            # The refused dispatch did not end the first one's dispatch: the age check still skips the round.
            self.engine.ages = {manifest.round_id: 1000.0}
            reports.append(self.controller.expire_updates(now=time.time(), update_max_age_seconds=60.0))

        self.engine.on_submit = during_dispatch
        self.controller.dispatch_round(self.manifest)
        [error] = errors
        self.assertIsInstance(error, RoundConflict)
        self.assertEqual([RecoveryReport(rounds=(), deleted_updates=())], reports)
        self.assertEqual([], self.engine.discarded)
        self.assertEqual("collecting", self.controller.get_round("round-a").status)

    def test_a_controller_without_a_limit_runs_no_age_check(self) -> None:
        controller = FederatedRoundController(engine=self.engine)
        for site_id in SIMULATED_SITES:
            controller.register_site(build_registration(site_id))
        self.engine.ages = {"round-other": 1000.0}
        with mock.patch.object(FederatedRoundController, "expire_updates", autospec=True) as check:
            controller.dispatch_round(self.manifest)
        check.assert_not_called()

    def test_the_limit_must_be_a_number_of_seconds_above_zero(self) -> None:
        for limit in (0, -1.0, float("nan"), float("inf"), True, "60"):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                FederatedRoundController(engine=self.engine, update_max_age_seconds=limit)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
