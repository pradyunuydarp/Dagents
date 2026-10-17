"""Concurrent requests: nothing deadlocks, and no app lock or store connection is held across an agent call.

In process, the hospitals' LMAs read the same store objects the app writes
through, and every agent's handlers share one pool of threads. Two rules keep
that safe:

- No app lock and no store connection is held while an agent is called. A
  write finishes and releases the store, then the app asks the LMA to read it
  again. The first test wraps every app lock and every store's lock or kept
  connection, and fails if any is held by the thread that calls an agent.
- The public edge caps the requests it works on at once (12 by default), and
  the agents get enough threads for that cap. Without the cap, 60 concurrent
  requests that each made nested agent calls deadlocked in the deploy
  measurements.

A pilot runs while entries and outcomes are posted from other threads, under a
watchdog, in memory and against Postgres. Sixty concurrent public requests go
through a real server on a loopback port. These tests need the planner; the
Postgres ones also need ``HEALTHCARE_DEMO_TEST_DATABASE_URL``.
"""

from __future__ import annotations

import socket
import threading
import time
import unittest
from typing import Any, Callable
from unittest import mock

import httpx
from fastapi.testclient import TestClient

from dagents.common.infrastructure.agent_client import AgentClient

import app.main as main
from app.domain.dataset import SITES, load_site_records
from app.services.agents import HealthcareDemo
from app.services.site_store import PostgresSiteStore

from tests.api_support import NO_HOURLY_CAPS, NO_LIMITS, default_hourly_caps, default_limits
from tests.support import HIGH_RISK_ENTRY, built_demo, dagentsc_available

SITE_IDS = [site.site_id for site in SITES]

#: Seconds a test may take before the watchdog calls it a deadlock.
WATCHDOG_SECONDS = 180.0


class TrackingLock:
    """A lock that knows which thread holds it. Behaves as ``threading.Lock`` or, with ``reentrant``, ``RLock``."""

    def __init__(self, name: str, reentrant: bool = False) -> None:
        self.name = name
        self._lock = threading.RLock() if reentrant else threading.Lock()
        self._owner: int | None = None
        self._depth = 0
        self.acquired = 0

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        got = self._lock.acquire(blocking, timeout)
        if got:
            self._owner = threading.get_ident()
            self._depth += 1
            self.acquired += 1
        return got

    def release(self) -> None:
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
        self._lock.release()

    def locked(self) -> bool:
        return self._owner is not None

    def held_here(self) -> bool:
        return self._owner == threading.get_ident()

    def __enter__(self) -> bool:
        return self.acquire()

    def __exit__(self, *exc_info: object) -> None:
        self.release()


def install_tracking_locks(test: unittest.TestCase, demo: HealthcareDemo) -> list[TrackingLock]:
    """Replace every app lock and every store lock or kept connection lock with a :class:`TrackingLock`.

    Call it after the services are built and before any request. The test
    puts the real locks back when it ends.
    """
    locks: list[TrackingLock] = []

    def track(owner: Any, attribute: str, name: str, reentrant: bool = False) -> None:
        lock = TrackingLock(name, reentrant)
        test.addCleanup(setattr, owner, attribute, getattr(owner, attribute))
        setattr(owner, attribute, lock)
        locks.append(lock)

    track(demo, "_lock", "the app services")
    track(demo.overview, "_lock", "the study overview")
    track(main.chart_service, "_lock", "the chart cache")
    for site_id, view in demo.views.items():
        track(view, "_lock", f"the view of {site_id}")
    for name, store in [*demo.stores.items(), ("coordinator", demo.coordinator_store)]:
        if store.lease is not None:
            # The kept connection: held while one operation runs on it.
            track(store.lease, "_lock", f"the connection of {name}", reentrant=True)
        else:
            track(store, "_lock", f"the store of {name}")
    return locks


class Watchdog:
    """Runs calls on daemon threads and fails the test when they do not finish in time."""

    def __init__(self, test: unittest.TestCase) -> None:
        self.test = test
        self.threads: list[threading.Thread] = []
        self.errors: list[BaseException] = []

    def start(self, target: Callable[[], None], name: str) -> None:
        def run() -> None:
            try:
                target()
            except BaseException as error:  # noqa: BLE001 - reported by join()
                self.errors.append(error)

        thread = threading.Thread(target=run, name=name, daemon=True)
        thread.start()
        self.threads.append(thread)

    def join(self, timeout: float = WATCHDOG_SECONDS) -> None:
        deadline = time.monotonic() + timeout
        for thread in self.threads:
            thread.join(max(deadline - time.monotonic(), 0.0))
        stuck = [thread.name for thread in self.threads if thread.is_alive()]
        self.test.assertEqual([], stuck, "these requests did not finish: a deadlock")
        if self.errors:
            raise self.errors[0]


class AppUnderTest(unittest.TestCase):
    """Puts one test's app services behind ``app.main`` and turns the limits off."""

    def make_demo(self) -> HealthcareDemo:
        return HealthcareDemo()

    def setUp(self) -> None:
        self.demo = self.make_demo()
        self.addCleanup(self.demo.close)
        self.agents = built_demo(self.demo)
        patcher = mock.patch.object(main, "demo", self.demo)
        patcher.start()
        self.addCleanup(patcher.stop)
        main.rate_limits.configure(NO_LIMITS)
        main.hourly_caps.configure(NO_HOURLY_CAPS)
        self.addCleanup(main.rate_limits.configure, default_limits())
        self.addCleanup(main.hourly_caps.configure, default_hourly_caps())
        main.chart_service.clear()
        self.client = TestClient(main.app)

    def ok(self, response: Any, status: int = 200) -> Any:
        self.assertEqual(status, response.status_code, response.text[:300])
        return response.json() if response.headers.get("content-type", "").startswith("application/json") else None


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class NoLockAcrossAgentCallsTests(AppUnderTest):
    """Every public route that calls an agent does so with no app lock and no store connection held."""

    def test_no_route_holds_a_lock_or_a_connection_while_it_calls_an_agent(self) -> None:
        locks = install_tracking_locks(self, self.demo)
        violations: list[str] = []
        calls: list[str] = []
        real = AgentClient.request

        def checked(client, method, path, *args, **kwargs):
            held = [lock.name for lock in locks if lock.held_here()]
            calls.append(f"{method} {path}")
            if held:
                violations.append(f"{method} {path} was called while holding {', '.join(held)}")
            return real(client, method, path, *args, **kwargs)

        with mock.patch.object(AgentClient, "request", checked):
            client = self.client
            self.ok(client.get("/api/v1/framework/status"))
            self.ok(client.get("/api/v1/overview"))
            self.ok(client.get("/api/v1/hospitals"))
            self.ok(client.get("/api/v1/models"))
            for site_id in SITE_IDS:
                self.ok(client.get(f"/api/v1/hospitals/{site_id}"))
                self.ok(client.get(f"/api/v1/hospitals/{site_id}/worklist?limit=5"))
                self.ok(client.get(f"/api/v1/hospitals/{site_id}/thresholds"))
                self.ok(client.get(f"/api/v1/hospitals/{site_id}/audit?limit=5"))
                self.ok(client.get(f"/api/v1/hospitals/{site_id}/entry-form"))
                record = self.ok(client.post(f"/api/v1/hospitals/{site_id}/encounters", json=HIGH_RISK_ENTRY), 201)
                encounter = record["record"]["encounter_id"]
                self.ok(client.post(f"/api/v1/hospitals/{site_id}/encounters/{encounter}/outcome",
                                    json={"poor_outcome_6m": True}))
                self.ok(client.get(f"/api/v1/hospitals/{site_id}/encounters?origin=entered"))
                self.ok(client.post("/api/v1/governance:probe", json={"site_id": site_id}))
                charts = self.ok(client.get(f"/api/v1/hospitals/{site_id}/charts"))["charts"]
                self.ok(client.get(charts[-1]["svg_path"]))
            self.ok(client.get("/api/v1/charts"))
            self.ok(client.get("/api/v1/charts/hospital-records.svg"))
            pilot = self.ok(client.post("/api/v1/pilot:run", json={"round_prefix": "api"}))
            self.ok(client.get("/api/v1/charts/training-curve.svg"))
            approval = client.post(f"/api/v1/models/{pilot['candidate']['candidate_version']}:approve")
            self.assertIn(approval.status_code, (200, 409), approval.text[:300])

        self.assertEqual([], violations)
        # The check saw the agents being called and the locks being used, so it would see a violation.
        self.assertGreater(len(calls), 50)
        self.assertTrue(all(lock.acquired > 0 for lock in locks if "store of" in lock.name or "view of" in lock.name),
                        [(lock.name, lock.acquired) for lock in locks])

    def test_the_check_finds_a_lock_held_across_an_agent_call(self) -> None:
        """A view that kept its lock while it asked its LMA would be caught."""
        locks = install_tracking_locks(self, self.demo)
        view = self.demo.hospital("uk-134")
        found: list[str] = []
        real = AgentClient.request

        def checked(client, method, path, *args, **kwargs):
            found.extend(lock.name for lock in locks if lock.held_here())
            return real(client, method, path, *args, **kwargs)

        with mock.patch.object(AgentClient, "request", checked):
            with view._lock:  # noqa: SLF001 - the mistake this check exists to catch
                view.summary()
        self.assertEqual(["the view of uk-134"], found)


class PilotWhileWritingContract:
    """A pilot runs while entries and outcomes are posted at every hospital. Subclasses supply the services."""

    ENTRIES_PER_HOSPITAL = 4
    OUTCOMES_PER_HOSPITAL = 2

    def test_a_pilot_runs_while_entries_and_outcomes_are_posted(self) -> None:
        test: AppUnderTest = self  # type: ignore[assignment]
        before = {site_id: test.demo.hospital(site_id).counts() for site_id in SITE_IDS}
        results: dict[str, Any] = {}
        statuses: list[tuple[str, int]] = []
        lock = threading.Lock()
        started = threading.Event()

        def record(label: str, response: Any) -> Any:
            with lock:
                statuses.append((label, response.status_code))
            return response

        def pilot() -> None:
            started.set()
            results["pilot"] = record("pilot", test.client.post("/api/v1/pilot:run", json={"round_prefix": "api"}))

        def writer(site_id: str) -> None:
            started.wait(10)
            entered = []
            for number in range(self.ENTRIES_PER_HOSPITAL):
                response = record(
                    "entry",
                    test.client.post(
                        f"/api/v1/hospitals/{site_id}/encounters",
                        json={**HIGH_RISK_ENTRY, "age": 60 + number},
                        headers={"Idempotency-Key": f"concurrent-{site_id}-{number}"},
                    ),
                )
                if response.status_code == 201:
                    entered.append(response.json()["record"]["encounter_id"])
            for encounter in entered[: self.OUTCOMES_PER_HOSPITAL]:
                record(
                    "outcome",
                    test.client.post(
                        f"/api/v1/hospitals/{site_id}/encounters/{encounter}/outcome", json={"poor_outcome_6m": False}
                    ),
                )

        def reader() -> None:
            started.wait(10)
            while "pilot" not in results:
                record("overview", test.client.get("/api/v1/overview"))
                record("dashboard", test.client.get("/api/v1/hospitals/it-002"))

        watchdog = Watchdog(test)
        watchdog.start(pilot, "pilot")
        for site_id in SITE_IDS:
            watchdog.start(lambda site_id=site_id: writer(site_id), f"writer {site_id}")
        watchdog.start(reader, "reader")
        watchdog.join()

        failed = [(label, status) for label, status in statuses if status >= 300]
        test.assertEqual([], failed)
        report = results["pilot"].json()
        test.assertIsNotNone(report["candidate"])
        for site_id in SITE_IDS:
            counts = test.demo.hospital(site_id).counts()
            test.assertEqual(before[site_id]["entered"] + self.ENTRIES_PER_HOSPITAL, counts["entered"], site_id)
            test.assertEqual(before[site_id]["labelled"] + self.OUTCOMES_PER_HOSPITAL, counts["labelled"], site_id)
            audit = test.ok(test.client.get(f"/api/v1/hospitals/{site_id}/audit?limit=500"))
            test.assertTrue(audit["chain_intact"], site_id)
            # The hospital's LMA sees every entry once the writes are done.
            summary = test.demo.hospital(site_id).summary()
            test.assertEqual(counts["total"], summary["record_count"], site_id)
        test.assertEqual([], test.agents["gma"].state.updates.pending_rounds())


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class MemoryPilotWhileWritingTests(PilotWhileWritingContract, AppUnderTest):
    pass


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class PostgresPilotWhileWritingTests(PilotWhileWritingContract, AppUnderTest):
    db: Any

    @classmethod
    def setUpClass(cls) -> None:
        from tests.test_site_isolation import SupabaseLikeDatabase, admin_dsn_or_skip

        cls.db = SupabaseLikeDatabase(admin_dsn_or_skip())
        cls.db.start()
        try:
            for site in SITES:
                store = PostgresSiteStore(site, cls.db.dsn)
                store.seed_dataset(load_site_records(site.site_id))
                store.close()
        except BaseException:
            cls.db.stop()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.stop()

    def make_demo(self) -> HealthcareDemo:
        return HealthcareDemo(database_url=self.db.dsn, environ={})

    def test_the_app_still_holds_one_connection_per_store(self) -> None:
        self.ok(self.client.post("/api/v1/pilot:run", json={"round_prefix": "api"}))
        for site_id, store in self.demo.stores.items():
            self.assertEqual(1, store.connections_opened, site_id)
        self.assertEqual(1, self.demo.coordinator_store.connections_opened)


def _bound_socket() -> socket.socket:
    """A listening TCP socket on a free loopback port, as ``test_network_mode.py`` uses.

    The server is handed the socket, so no port is fixed, no other process can
    take the port between choosing it and serving on it, and the test never
    skips for want of one.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    return sock


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class SixtyConcurrentRequestsTests(AppUnderTest):
    """Sixty public requests at once, each making nested agent calls, through a real server."""

    REQUESTS = 60

    def test_sixty_concurrent_public_requests_finish(self) -> None:
        import uvicorn

        sock = _bound_socket()
        self.addCleanup(sock.close)
        port = sock.getsockname()[1]
        # The server's own lifespan would stop these services; the test does.
        server = uvicorn.Server(uvicorn.Config(main.app, log_level="warning", lifespan="off"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, name="uvicorn", daemon=True)
        thread.start()
        self.addCleanup(thread.join, 10)
        self.addCleanup(setattr, server, "should_exit", True)
        deadline = time.monotonic() + 15
        while not server.started:
            self.assertLess(time.monotonic(), deadline, "the server did not start")
            time.sleep(0.05)

        # Each of these makes nested agent calls: the overview asks the GMA,
        # which asks every LMA; a dashboard asks its LMA, which asks the GMA
        # for the model in use; a probe runs the LMA's Guard.
        paths = [
            ("GET", "/api/v1/overview", None),
            ("GET", "/api/v1/hospitals/uk-134", None),
            ("GET", "/api/v1/hospitals/it-002", None),
            ("GET", "/api/v1/hospitals", None),
            ("POST", "/api/v1/governance:probe", {"site_id": "ch-245"}),
            ("GET", "/api/v1/framework/status", None),
        ]
        barrier = threading.Barrier(self.REQUESTS)
        statuses: list[int] = []
        lock = threading.Lock()
        base = f"http://127.0.0.1:{port}"

        def one(index: int) -> None:
            method, path, body = paths[index % len(paths)]
            with httpx.Client(base_url=base, timeout=120.0, trust_env=False) as http:
                barrier.wait(30)
                response = http.request(method, path, json=body)
            with lock:
                statuses.append(response.status_code)

        watchdog = Watchdog(self)
        started = time.monotonic()
        for index in range(self.REQUESTS):
            watchdog.start(lambda index=index: one(index), f"request {index}")
        watchdog.join(120.0)
        self.assertEqual([200] * self.REQUESTS, statuses, f"after {time.monotonic() - started:.1f} s")


if __name__ == "__main__":
    unittest.main()
