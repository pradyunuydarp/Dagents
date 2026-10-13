"""Each Postgres store reuses one connection, and no role carries over between operations.

The stores keep one autocommit connection each and run every operation in its
own transaction that starts with ``SET LOCAL ROLE``. These tests check what
that reuse must not change:

- after every operation, including one that failed inside its transaction,
  the connection is idle and ``current_user`` is the login role again;
- the statement timeout set inside an operation does not stay on the session;
- a reused connection acting as one hospital still cannot read another;
- a dead connection is replaced, and ``close()`` releases it.

They also check that a pilot opens one connection per store, not one per
operation. They need ``HEALTHCARE_DEMO_TEST_DATABASE_URL`` (see
``test_site_isolation.py``); the pilot test also needs the planner.
"""

from __future__ import annotations

from pathlib import Path
import sys
import threading
import time
import unittest

APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT / "backend"))
sys.path.insert(0, str(APP_ROOT.parents[1]))

from app.domain.dataset import SITES, load_site_records, site_for  # noqa: E402
from app.services.consortium import Consortium  # noqa: E402
from app.services.site_store import (  # noqa: E402
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
    with store._lease() as conn:
        assert conn.info.transaction_status == TransactionStatus.IDLE
        row = conn.execute(
            "SELECT current_user AS cu, session_user AS su, current_setting('role') AS r, "
            "current_setting('statement_timeout') AS t"
        ).fetchone()
    return row["cu"], row["su"], row["r"], row["t"]


def _backend_pid(store) -> int:
    with store._lease() as conn:
        return int(conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"])


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

    def assert_clean_session(self, store) -> None:
        self.assertEqual(
            (self.login, self.login, "none", self.default_timeout),
            _session_state(store),
            "a role or setting from the last operation is still on the session",
        )

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
        with store._acting_as(UK.role) as conn:
            self.assertEqual(UK.role, conn.execute("SELECT current_user AS u").fetchone()["u"])
        self.assertGreater(store.count()["total"], 0)
        self.assertEqual(1, store.connections_opened)

    def test_a_reused_connection_still_cannot_read_another_site(self) -> None:
        store = self.store()
        store.count()
        # The store reports a refused grant as a database that is not set up
        # for this version; the database's own error is the cause.
        with self.assertRaises(StoreNotSetUpError) as caught:
            with store._acting_as(UK.role) as conn:
                conn.execute("SELECT count(*) FROM site_it_002.encounters")
        self.assertIsInstance(caught.exception.__cause__, errors.InsufficientPrivilege)
        self.assert_clean_session(store)
        # Outside an operation the login role holds no site privileges either.
        with self.assertRaises(StoreNotSetUpError) as caught:
            with store._lease() as conn:
                conn.execute("SELECT count(*) FROM site_uk_134.encounters")
        self.assertIsInstance(caught.exception.__cause__, errors.InsufficientPrivilege)
        self.assert_clean_session(store)
        self.assertEqual(1, store.connections_opened)

    def test_a_missing_table_keeps_the_connection(self) -> None:
        store = self.store()
        store.count()
        with self.assertRaises(StoreNotSetUpError) as caught:
            with store._acting_as(UK.role) as conn:
                conn.execute("SELECT count(*) FROM site_uk_134.no_such_table")
        self.assertIsInstance(caught.exception.__cause__, errors.UndefinedTable)
        self.assertIn("hospital uk-134", str(caught.exception))
        self.assert_clean_session(store)
        self.assertGreater(store.count()["total"], 0)
        self.assertEqual(1, store.connections_opened)

    def test_the_coordinator_store_switches_roles_per_operation(self) -> None:
        coordinator = PostgresCoordinatorStore(self.db.dsn, SITES)
        self.addCleanup(coordinator.close)
        round_id = "reuse-round"
        coordinator.add_contribution(UK, round_id, "analytics", {"n": 1})
        coordinator.list_models()
        coordinator.add_contribution(IT, round_id, "analytics", {"n": 2})
        shown = coordinator.contributions(round_id)
        self.assertEqual(["uk-134", "it-002"], [row["site_id"] for row in shown])
        # Read as the hospital: row level security shows only its own rows.
        own = [row for row in coordinator.site_contributions(UK) if row["round_id"] == round_id]
        self.assertEqual(["uk-134"], [row["site_id"] for row in own])
        self.assertEqual(1, coordinator.connections_opened)
        self.assert_clean_session(coordinator)

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
        with store._acting_as(UK.role) as conn:
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
        self.assertGreater(store.count()["total"], 0)
        self.assertEqual(2, store.connections_opened)

    def test_a_statement_timeout_is_reported_and_does_not_stay(self) -> None:
        store = self.store(statement_timeout_ms=50)
        with self.assertRaises(StoreUnavailableError):
            with store._acting_as(UK.role) as conn:
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

    def test_closing_the_consortium_closes_every_store(self) -> None:
        consortium = Consortium(database_url=self.db.dsn, environ={})
        consortium.overview()
        with self.db.admin() as admin:
            def open_connections() -> int:
                return admin.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                    "AND usename = %s AND pid <> pg_backend_pid()",
                    (self.login,),
                ).fetchone()[0]

            self.assertGreaterEqual(open_connections(), len(SITES))
            # Other tests' stores are closed by their cleanups; this test's
            # consortium holds the rest.
            consortium.close()
            for _ in range(50):
                if open_connections() == 0:
                    break
                time.sleep(0.05)
            self.assertEqual(0, open_connections())
        self.assertEqual(3, len(consortium.overview()["hospitals"]))
        consortium.close()


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class PostgresPilotConnectionTests(unittest.TestCase):
    """A pilot opens one connection per store, however many operations it runs."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.db = _seeded_database()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.stop()

    def test_a_pilot_opens_one_connection_per_store(self) -> None:
        opened = {"count": 0}
        real = psycopg.connect

        def counting(*args, **kwargs):
            opened["count"] += 1
            return real(*args, **kwargs)

        consortium = Consortium(database_url=self.db.dsn, environ={})
        self.addCleanup(consortium.close)
        psycopg.connect = counting
        try:
            report = consortium.run_pilot("connections", training_rounds=2)
        finally:
            psycopg.connect = real
        self.assertIsNotNone(report["release"])
        # Three hospital stores and the coordinator store.
        self.assertLessEqual(opened["count"], len(SITES) + 1)


class MemoryStoreCloseTests(unittest.TestCase):
    def test_closing_a_memory_store_changes_nothing(self) -> None:
        store = MemorySiteStore(UK, load_site_records("uk-134")[:3])
        store.close()
        self.assertEqual(3, store.count()["total"])
        coordinator = MemoryCoordinatorStore()
        coordinator.close()
        self.assertEqual([], coordinator.list_models())


if __name__ == "__main__":
    unittest.main()
