"""Each Postgres store reuses one connection, and no role carries over between operations.

The stores run on the framework's kept connection
(``agents.common.infrastructure.postgres_lease.KeptConnection``). Each store
keeps one autocommit connection and runs every operation in its own
transaction that starts with ``SET LOCAL ROLE``. These tests check what that
reuse must not change:

- after every operation, including one that failed inside its transaction,
  the connection is idle and ``current_user`` is the login role again;
- the statement timeout set inside an operation does not stay on the session;
- a reused connection acting as one hospital still cannot read another;
- a dead connection is replaced, and ``close()`` releases it.

They also check that the app keeps four connections in all: one per hospital
store and one for the coordinator store. The framework's job log, model
store and audit chains run on those same connections, and a pilot run
through the public API opens no more. They need
``HEALTHCARE_DEMO_TEST_DATABASE_URL`` (see ``test_site_isolation.py``); the
pilot test also needs the planner.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

APP_ROOT = Path(__file__).resolve().parents[1]
# The framework is two directories up in the Dagents tree, and at DAGENTS_HOME
# after scripts/extract_repo.sh moved the app out of it.
FRAMEWORK_ROOT = Path(os.environ.get("DAGENTS_HOME") or APP_ROOT.parents[1])
sys.path.insert(0, str(APP_ROOT / "backend"))
sys.path.insert(0, str(FRAMEWORK_ROOT))

from agents.common.domain.federation import ModelStatusChange, ModelVersion  # noqa: E402
from agents.common.domain.site import JobLogEntry  # noqa: E402
from agents.gma.infrastructure.postgres_models import PostgresModelStore  # noqa: E402
from agents.lma.infrastructure.job_log import PostgresJobLog  # noqa: E402
from app.domain.dataset import SITES, load_site_records, site_for  # noqa: E402
from app.services.site_source import StoreSiteSource  # noqa: E402
from app.services.site_store import (  # noqa: E402
    COORDINATOR_MODELS_TABLE,
    COORDINATOR_ROLE,
    SITE_JOB_LOG_TABLE,
    MemoryCoordinatorStore,
    MemorySiteStore,
    PostgresCoordinatorStore,
    PostgresSiteStore,
    RecordRejected,
    StoreNotSetUpError,
    StoreUnavailableError,
)
from tests.support import HIGH_RISK_ENTRY, dagentsc_available  # noqa: E402
from tests.test_site_isolation import SupabaseLikeDatabase, admin_dsn_or_skip  # noqa: E402

try:
    import psycopg
    from psycopg import errors
    from psycopg.pq import TransactionStatus
except ModuleNotFoundError:  # pragma: no cover - psycopg is in the app's requirements
    psycopg = errors = TransactionStatus = None

UK = site_for("uk-134")
IT = site_for("it-002")

#: Connections the app keeps: one per hospital store and one for the coordinator store.
KEPT_CONNECTIONS = len(SITES) + 1


def _seeded_database() -> SupabaseLikeDatabase:
    db = SupabaseLikeDatabase(admin_dsn_or_skip())
    try:
        db.start()
        for site in SITES:
            store = PostgresSiteStore(site, db.dsn)
            store.seed_dataset(load_site_records(site.site_id))
            store.close()
    except BaseException:
        db.stop()
        raise
    return db


def _session_state(store) -> tuple[str, str, str, str]:
    """``(current_user, session_user, role setting, statement_timeout)`` outside any transaction."""
    with store.lease.lease() as conn:
        assert conn.info.transaction_status == TransactionStatus.IDLE
        row = conn.execute(
            "SELECT current_user AS cu, session_user AS su, current_setting('role') AS r, "
            "current_setting('statement_timeout') AS t"
        ).fetchone()
    return row["cu"], row["su"], row["r"], row["t"]


def _backend_pid(store) -> int:
    with store.lease.lease() as conn:
        return int(conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"])


def _job_entry(round_id: str) -> JobLogEntry:
    return JobLogEntry(
        round_id=round_id,
        phase="analytics",
        participation="completed",
        contributed_examples=24,
        metrics={"record_count": 24.0},
    )


def _new_model(version: str, status: str) -> tuple[list[ModelStatusChange], ModelVersion]:
    new = ModelVersion(version=version, kind="points_rule", digest=f"digest-{version}", status=status, params={})
    return [ModelStatusChange(version=version, from_status=None, to_status=status)], new


class PostgresConnectionReuseTests(unittest.TestCase):
    db: SupabaseLikeDatabase
    login: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.db = _seeded_database()
        with cls.db.connect() as conn:
            cls.login = conn.execute("SELECT session_user").fetchone()[0]
            cls.default_timeout = conn.execute("SHOW statement_timeout").fetchone()[0]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.stop()

    def store(self, site=UK, **kwargs) -> PostgresSiteStore:
        store = PostgresSiteStore(site, self.db.dsn, **kwargs)
        self.addCleanup(store.close)
        return store

    def coordinator(self) -> PostgresCoordinatorStore:
        coordinator = PostgresCoordinatorStore(self.db.dsn)
        self.addCleanup(coordinator.close)
        return coordinator

    def assert_clean_session(self, store) -> None:
        self.assertEqual(
            (self.login, self.login, "none", self.default_timeout),
            _session_state(store),
            "a role or setting from the last operation is still on the session",
        )

    def open_connections(self) -> int:
        with self.db.admin() as admin:
            return admin.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                "AND usename = %s AND pid <> pg_backend_pid()",
                (self.login,),
            ).fetchone()[0]

    def wait_for_open_connections(self, expected: int) -> int:
        for _ in range(50):
            count = self.open_connections()
            if count == expected:
                return count
            time.sleep(0.05)
        return count

    def terminate(self, pid: int) -> None:
        with self.db.admin() as admin:
            admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
            for _ in range(50):
                if admin.execute("SELECT count(*) FROM pg_stat_activity WHERE pid = %s", (pid,)).fetchone()[0] == 0:
                    return
                time.sleep(0.05)
        self.fail(f"backend {pid} did not stop")

    def test_one_connection_serves_every_operation(self) -> None:
        store = self.store()
        before = store.count()
        store.records()
        entry = store.append(HIGH_RISK_ENTRY, "reuse-1")
        store.set_outcome(entry["encounter_id"], True)
        store.list_entered(5)
        sink = store.audit_sink()
        sink.list_recent(5)
        sink.verify_chain()
        self.assertEqual(before["total"] + 1, store.count()["total"])
        self.assertEqual(1, store.connections_opened)
        self.assert_clean_session(store)

    def test_rows_are_dicts_again_after_the_audit_sink_reads_tuples(self) -> None:
        store = self.store()
        store.audit_sink().verify_chain()
        self.assertIsInstance(store.records()[0], dict)

    def test_an_error_inside_an_operation_leaves_no_role_behind(self) -> None:
        store = self.store()
        dataset_row = store.records()[0]["encounter_id"]
        # Raised inside the transaction, after a query ran as the hospital.
        with self.assertRaises(RecordRejected):
            store.set_outcome(dataset_row, True)
        self.assert_clean_session(store)
        # The next operation runs as the hospital again, on the same connection.
        with store.lease.acting_as(UK.role) as conn:
            self.assertEqual(UK.role, conn.execute("SELECT current_user AS u").fetchone()["u"])
        self.assertGreater(store.count()["total"], 0)
        self.assertEqual(1, store.connections_opened)

    def test_a_reused_connection_still_cannot_read_another_site(self) -> None:
        store = self.store()
        store.count()
        # The store reports a refused grant as a database that is not set up
        # for this version; the database's own error is the cause.
        with self.assertRaises(StoreNotSetUpError) as caught:
            with store.lease.acting_as(UK.role) as conn:
                conn.execute("SELECT count(*) FROM site_it_002.encounters")
        self.assertIsInstance(caught.exception.__cause__, errors.InsufficientPrivilege)
        self.assert_clean_session(store)
        # Outside an operation the login role holds no site privileges either.
        with self.assertRaises(StoreNotSetUpError) as caught:
            with store.lease.lease() as conn:
                conn.execute("SELECT count(*) FROM site_uk_134.encounters")
        self.assertIsInstance(caught.exception.__cause__, errors.InsufficientPrivilege)
        self.assert_clean_session(store)
        self.assertEqual(1, store.connections_opened)

    def test_a_missing_table_keeps_the_connection(self) -> None:
        store = self.store()
        store.count()
        with self.assertRaises(StoreNotSetUpError) as caught:
            with store.lease.acting_as(UK.role) as conn:
                conn.execute("SELECT count(*) FROM site_uk_134.no_such_table")
        self.assertIsInstance(caught.exception.__cause__, errors.UndefinedTable)
        self.assertIn("hospital uk-134", str(caught.exception))
        self.assert_clean_session(store)
        self.assertGreater(store.count()["total"], 0)
        self.assertEqual(1, store.connections_opened)

    def test_a_hospital_lease_also_serves_its_audit_chain_job_log_and_source(self) -> None:
        """The hospital's LMA writes its job log and audit chain on the store's own connection."""
        store = self.store()
        source = StoreSiteSource(store)
        job_log = PostgresJobLog(store.lease, UK.role, SITE_JOB_LOG_TABLE)
        source.version()
        source.records()
        job_log.append(_job_entry("reuse-round"))
        store.audit_sink().verify_chain()
        store.append(HIGH_RISK_ENTRY, "reuse-job-log")
        self.assertEqual(["reuse-round"], [entry.round_id for entry in job_log.recent(5)])
        self.assertEqual(1, store.connections_opened)
        self.assert_clean_session(store)

    def test_the_coordinator_lease_also_serves_the_model_store(self) -> None:
        """The GMA keeps its model versions and audit chain on the coordinator store's connection."""
        coordinator = self.coordinator()
        models = PostgresModelStore(coordinator.lease, COORDINATOR_ROLE, COORDINATOR_MODELS_TABLE)
        coordinator.save_pilot_report("reuse-pilot", {"pilot_id": "reuse-pilot"})
        changes, new = _new_model("reuse-model-v1", "candidate")
        models.apply(changes, new=new)
        coordinator.audit_sink().count()
        self.assertEqual("reuse-model-v1", models.get("reuse-model-v1").version)
        self.assertEqual({"pilot_id": "reuse-pilot"}, coordinator.latest_pilot_report())
        self.assertEqual(1, coordinator.connections_opened)
        self.assert_clean_session(coordinator)

    def test_the_coordinator_role_cannot_read_a_hospital_schema(self) -> None:
        """The GMA's lease acts as the coordinator, which has no grant on any hospital schema."""
        coordinator = self.coordinator()
        with self.assertRaises(StoreNotSetUpError) as caught:
            with coordinator.lease.acting_as(COORDINATOR_ROLE) as conn:
                conn.execute("SELECT count(*) FROM site_uk_134.encounters")
        self.assertIsInstance(caught.exception.__cause__, errors.InsufficientPrivilege)
        self.assert_clean_session(coordinator)

    def test_entries_and_lma_reads_at_once_take_turns_and_change_the_version(self) -> None:
        """The app writes entries while the hospital's LMA reads and logs jobs; one connection serves both."""
        store = self.store(IT)
        source = StoreSiteSource(store)
        job_log = PostgresJobLog(store.lease, IT.role, SITE_JOB_LOG_TABLE)
        start = source.version()
        failures: list[BaseException] = []

        def enter(number: int) -> None:
            try:
                store.append(HIGH_RISK_ENTRY, f"mixed-{number}")
            except BaseException as error:  # noqa: BLE001 - reported below
                failures.append(error)

        def read(number: int) -> None:
            try:
                source.version()
                source.records()
                job_log.append(_job_entry(f"mixed-{number}"))
            except BaseException as error:  # noqa: BLE001 - reported below
                failures.append(error)

        threads = [threading.Thread(target=enter, args=(number,)) for number in range(6)]
        threads += [threading.Thread(target=read, args=(number,)) for number in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertFalse(any(thread.is_alive() for thread in threads), "an operation never finished")
        self.assertEqual([], failures)
        self.assertNotEqual(start, source.version())
        self.assertEqual(1, store.connections_opened)
        self.assert_clean_session(store)

    def test_four_connections_serve_three_hospitals_and_the_coordinator(self) -> None:
        opened = {"count": 0}
        real = psycopg.connect

        def counting(*args, **kwargs):
            opened["count"] += 1
            return real(*args, **kwargs)

        with mock.patch.object(psycopg, "connect", counting):
            stores = [self.store(site) for site in SITES]
            coordinator = self.coordinator()
            for store in stores:
                source = StoreSiteSource(store)
                source.version()
                source.records()
                PostgresJobLog(store.lease, store.role, SITE_JOB_LOG_TABLE).append(_job_entry("four-round"))
                store.audit_sink().verify_chain()
                store.list_entered(5)
            PostgresModelStore(coordinator.lease, COORDINATOR_ROLE, COORDINATOR_MODELS_TABLE).list(5)
            coordinator.audit_sink().verify_chain()
            coordinator.latest_pilot_report()
        self.assertEqual(KEPT_CONNECTIONS, opened["count"])

    def test_concurrent_operations_take_turns_on_one_connection(self) -> None:
        store = self.store(IT)
        before = store.count()["entered"]
        failures: list[BaseException] = []
        ids: list[str] = []

        def enter(number: int) -> None:
            try:
                ids.append(store.append(HIGH_RISK_ENTRY, f"thread-{number}")["encounter_id"])
                store.count()
            except BaseException as error:  # noqa: BLE001 - reported below
                failures.append(error)

        threads = [threading.Thread(target=enter, args=(number,)) for number in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([], failures)
        self.assertEqual(8, len(set(ids)))
        self.assertEqual(before + 8, store.count()["entered"])
        self.assertEqual(1, store.connections_opened)
        self.assert_clean_session(store)

    def test_a_nested_operation_gets_its_own_connection(self) -> None:
        store = self.store()
        store.count()
        with store.lease.acting_as(UK.role) as conn:
            nested = store.count()
            self.assertEqual(UK.role, conn.execute("SELECT current_user AS u").fetchone()["u"])
        self.assertGreater(nested["total"], 0)
        self.assertEqual(2, store.connections_opened)
        self.assert_clean_session(store)

    def test_a_dead_connection_is_replaced_by_the_liveness_check(self) -> None:
        store = self.store(liveness_idle_seconds=0)
        store.count()
        self.terminate(_backend_pid(store))
        self.assertGreater(store.count()["total"], 0)
        self.assertEqual(2, store.connections_opened)
        self.assert_clean_session(store)

    def test_without_the_check_a_dead_connection_fails_once_then_recovers(self) -> None:
        store = self.store(liveness_idle_seconds=3600)
        store.count()
        self.terminate(_backend_pid(store))
        with self.assertRaises(StoreUnavailableError) as raised:
            store.count()
        self.assertIn("hospital uk-134", str(raised.exception))
        self.assertNotIn(self.db.dsn, str(raised.exception))
        self.assertGreater(store.count()["total"], 0)
        self.assertEqual(2, store.connections_opened)

    def test_a_statement_timeout_is_reported_and_does_not_stay(self) -> None:
        store = self.store(statement_timeout_ms=50)
        with self.assertRaises(StoreUnavailableError):
            with store.lease.acting_as(UK.role) as conn:
                conn.execute("SELECT pg_sleep(1)")
        self.assertGreater(store.count()["total"], 0)
        self.assert_clean_session(store)

    def test_close_releases_the_connection_and_the_next_operation_reopens_it(self) -> None:
        store = self.store()
        store.count()
        pid = _backend_pid(store)
        store.close()
        with self.db.admin() as admin:
            for _ in range(50):
                if admin.execute("SELECT count(*) FROM pg_stat_activity WHERE pid = %s", (pid,)).fetchone()[0] == 0:
                    break
                time.sleep(0.05)
            else:
                self.fail("close() left the connection open")
        self.assertGreater(store.count()["total"], 0)
        self.assertEqual(2, store.connections_opened)

    def test_closing_the_stores_releases_every_connection(self) -> None:
        # Other tests' stores are closed by their cleanups.
        self.assertEqual(0, self.wait_for_open_connections(0))
        stores = [PostgresSiteStore(site, self.db.dsn) for site in SITES]
        coordinator = PostgresCoordinatorStore(self.db.dsn)
        try:
            for store in stores:
                store.count()
                store.audit_sink().count()
            coordinator.latest_pilot_report()
            self.assertEqual(KEPT_CONNECTIONS, self.open_connections())
        finally:
            for store in [*stores, coordinator]:
                store.close()
        self.assertEqual(0, self.wait_for_open_connections(0))

    def test_a_store_on_a_borrowed_lease_leaves_the_lease_open(self) -> None:
        """An LMA started on its own owns the lease; the store it builds on it must not close it."""
        owner = self.store()
        owner.count()
        borrower = PostgresSiteStore(UK, lease=owner.lease)
        self.assertIs(owner.lease, borrower.lease)
        self.assertEqual(owner.count(), borrower.count())
        borrower.close()
        pid = _backend_pid(owner)
        self.assertEqual(1, owner.connections_opened, "closing the borrower closed the owner's connection")
        self.assertEqual(pid, _backend_pid(owner))


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class PostgresPilotConnectionTests(unittest.TestCase):
    """A pilot run through the public API keeps one connection per store, however many operations it runs.

    The app runs in its own process, configured only through the environment,
    as it is deployed, so the test counts every connection it opens: the
    stores', and any the agents open.
    """

    SCRIPT = (
        "import json, psycopg\n"
        "opened = []\n"
        "real = psycopg.connect\n"
        "def counting(*args, **kwargs):\n"
        "    opened.append(1)\n"
        "    return real(*args, **kwargs)\n"
        "psycopg.connect = counting\n"
        "from fastapi.testclient import TestClient\n"
        "from app.main import app\n"
        "with TestClient(app) as client:\n"
        "    status = client.get('/api/v1/framework/status').json()\n"
        "    pilot = client.post('/api/v1/pilot:run', json={'round_prefix': 'api'})\n"
        "    body = pilot.json()\n"
        "print(json.dumps({'pilot_status': pilot.status_code, 'has_release': bool(body.get('release')),\n"
        "                  'cohort_source': status.get('cohort_source'), 'connections': len(opened)}))\n"
    )

    @classmethod
    def setUpClass(cls) -> None:
        cls.db = _seeded_database()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.stop()

    def test_a_pilot_opens_one_connection_per_store(self) -> None:
        env = {
            key: value
            for key, value in os.environ.items()
            if not (key.startswith("HEALTHCARE_DEMO_") or key == "PYTHONPATH")
        }
        env["HEALTHCARE_DEMO_DATABASE_URL"] = self.db.dsn
        env["PYTHONPATH"] = os.pathsep.join([str(FRAMEWORK_ROOT), str(APP_ROOT / "backend")])
        result = subprocess.run(
            [sys.executable, "-c", self.SCRIPT],
            cwd=APP_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=600,
        )
        self.assertEqual(0, result.returncode, result.stderr[-3000:])
        report = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual("postgres", report["cohort_source"])
        self.assertEqual(200, report["pilot_status"], result.stderr[-3000:])
        self.assertTrue(report["has_release"])
        # Three hospital stores and the coordinator store.
        self.assertLessEqual(report["connections"], KEPT_CONNECTIONS)


class MemoryStoreCloseTests(unittest.TestCase):
    def test_closing_a_memory_store_changes_nothing(self) -> None:
        store = MemorySiteStore(UK, load_site_records("uk-134")[:3])
        store.close()
        self.assertEqual(3, store.count()["total"])
        coordinator = MemoryCoordinatorStore()
        coordinator.save_pilot_report("ui-1", {"n": 1})
        coordinator.close()
        self.assertEqual({"n": 1}, coordinator.latest_pilot_report())


if __name__ == "__main__":
    unittest.main()
