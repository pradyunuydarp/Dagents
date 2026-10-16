"""One kept Postgres connection, shared by a store's operations, each acting as one role.

``KeptConnection`` keeps one autocommit connection and runs one operation at a
time on it. These tests check what that reuse must not change:

- after every operation, including one that failed inside its transaction,
  the connection is idle and ``current_user`` is the login role again;
- the statement timeout set inside an operation does not stay on the session;
- a reused connection acting as one tenant still cannot read another;
- a dead connection is replaced, and ``close()`` releases it;
- an error never carries the connection string, its password or its host.

They also check that ``PostgresAuditSink`` can take its connections from the
lease, and that the framework's Postgres source adapter reads in one
transaction that acts as the role with a statement timeout.

The tests in ``KeptConnectionOfflineTests`` need no database server and never
skip. The rest use the scratch database from ``test_postgres_roles.py`` and
skip with ``database test skipped`` when ``DAGENTS_TEST_DATABASE_URL`` is not
set.
"""

from __future__ import annotations

import gc
import socket
import threading
import time
from typing import Any
import unittest

try:
    import psycopg
    from psycopg import errors, sql
    from psycopg.conninfo import conninfo_to_dict
    from psycopg.pq import TransactionStatus
except ModuleNotFoundError:  # pragma: no cover - the agents' requirements install it
    psycopg = None

from agents.common.domain.errors import StoreNotSetUpError, StoreUnavailableError
from agents.common.domain.sources import ConnectionRef, DatasetInput, PostgresSelection, PostgresSourceSpec
from agents.common.infrastructure.postgres_lease import (
    DEFAULT_LIVENESS_IDLE_SECONDS,
    DEFAULT_STATEMENT_TIMEOUT_MS,
    KeptConnection,
)
from agents.common.infrastructure.sources import (
    DefaultSourceResolver,
    InMemoryConnectionResolver,
    PostgresSourceAdapter,
)
from agents.tests.test_postgres_roles import (
    SKIP_PREFIX,
    ScratchDatabase,
    admin_dsn_or_skip,
    audit_record,
)

SECRET = "s3cret-lease-pw"


def closed_loopback_port() -> int:
    """A port in 9800-9899 on 127.0.0.1 that nothing listens on right now."""
    for port in range(9800, 9900):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise unittest.SkipTest("no free port in 9800-9899 to point a refused connection at")


@unittest.skipIf(psycopg is None, SKIP_PREFIX + "psycopg is not installed")
class KeptConnectionOfflineTests(unittest.TestCase):
    """What the lease checks and reports before any server answers."""

    def test_a_connection_string_is_required(self) -> None:
        for dsn in ("", "   ", None, 5):
            with self.subTest(dsn=dsn):
                with self.assertRaises(ValueError):
                    KeptConnection(dsn)  # type: ignore[arg-type]

    def test_the_defaults_match_the_stores(self) -> None:
        lease = KeptConnection("postgresql://u@127.0.0.1/db")
        self.assertEqual(15_000, DEFAULT_STATEMENT_TIMEOUT_MS)
        self.assertEqual(30.0, DEFAULT_LIVENESS_IDLE_SECONDS)
        self.assertEqual({"statement_timeout": "15000ms"}, lease.settings)
        self.assertEqual(0, lease.connections_opened)
        lease.settings["statement_timeout"] = "0"
        self.assertEqual({"statement_timeout": "15000ms"}, lease.settings, "settings must be a copy")

    def test_the_repr_names_the_store_and_not_the_connection(self) -> None:
        dsn = f"postgresql://owner:{SECRET}@db.internal.example:6543/app"
        text = repr(KeptConnection(dsn, name="tenant a"))
        self.assertIn("tenant a", text)
        for secret in (dsn, SECRET, "db.internal.example", "6543"):
            self.assertNotIn(secret, text)

    def assert_no_secret(self, error: BaseException, *secrets: str) -> None:
        text = str(error)
        for secret in secrets:
            self.assertNotIn(secret, text, f"the error message holds {secret!r}: {text!r}")

    def test_a_refused_connection_is_reported_without_the_connection_string(self) -> None:
        port = closed_loopback_port()
        dsn = f"postgresql://owner:{SECRET}@127.0.0.1:{port}/app?connect_timeout=2"
        lease = KeptConnection(dsn, name="tenant a")
        with self.assertRaises(StoreUnavailableError) as caught:
            with lease.acting_as("tenant_a"):
                self.fail("the block ran without a connection")
        self.assertNotIsInstance(caught.exception, StoreNotSetUpError)
        message = str(caught.exception)
        self.assertTrue(message.startswith("tenant a: could not connect to the database"), message)
        self.assert_no_secret(caught.exception, dsn, SECRET, "127.0.0.1", str(port), "owner")
        self.assertEqual(0, lease.connections_opened)
        # The driver's own error stays available to the process as the cause.
        self.assertIsInstance(caught.exception.__cause__, psycopg.OperationalError)

    def test_a_malformed_connection_string_is_not_echoed(self) -> None:
        dsn = f"host=127.0.0.1 password={SECRET} {SECRET}-trailing"
        lease = KeptConnection(dsn)
        with self.assertRaises(StoreUnavailableError) as caught:
            with lease.lease():
                pass
        self.assertIn("connection string is not valid", str(caught.exception))
        self.assert_no_secret(caught.exception, dsn, SECRET)
        # The driver's message quotes the string, so it is not kept as context.
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertIsNone(caught.exception.__cause__)

    def test_a_hint_is_added_to_a_connection_failure_and_scrubbed(self) -> None:
        port = closed_loopback_port()
        dsn = f"postgresql://owner:{SECRET}@127.0.0.1:{port}/app?connect_timeout=2"
        seen: list[str] = []

        def hint(connection_string: str) -> str:
            seen.append(connection_string)
            return f"\n\nUse the pooler instead of {connection_string} (password {SECRET})."

        with self.assertRaises(StoreUnavailableError) as caught:
            with KeptConnection(dsn, hint=hint).lease():
                pass
        self.assertEqual([dsn], seen)
        self.assertIn("Use the pooler instead of", str(caught.exception))
        self.assert_no_secret(caught.exception, dsn, SECRET)

    def test_a_driver_error_without_a_sqlstate_loses_its_text(self) -> None:
        """libpq writes the host and port into its own messages; the server's messages have a SQLSTATE."""
        from agents.common.infrastructure.postgres_lease import describe_database_error

        dsn = f"postgresql://owner:{SECRET}@db.internal.example:6543/app"
        error = psycopg.OperationalError(f'connection to server at "db.internal.example", port 6543 failed: {dsn}')
        text = describe_database_error(error, dsn)
        self.assertEqual("OperationalError", text)

    def test_a_server_error_keeps_its_text_without_the_secrets(self) -> None:
        from agents.common.infrastructure.postgres_lease import describe_database_error

        dsn = f"postgresql://owner:{SECRET}@db.internal.example:6543/app"
        error = errors.UndefinedTable(f"relation {dsn} does not exist; password {SECRET}")
        text = describe_database_error(error, dsn)
        self.assertIn("UndefinedTable", text)
        self.assertIn("does not exist", text)
        for secret in (dsn, SECRET):
            self.assertNotIn(secret, text)


class LeaseDatabaseTestCase(unittest.TestCase):
    """A scratch database from test_postgres_roles.py and a lease on it per test."""

    db: ScratchDatabase

    @classmethod
    def setUpClass(cls) -> None:
        cls.db = ScratchDatabase(admin_dsn_or_skip())
        try:
            cls.db.start()
        except BaseException:
            cls.db.stop()
            raise
        cls.password = conninfo_to_dict(cls.db.dsn)["password"]
        with psycopg.connect(cls.db.dsn, autocommit=True) as conn:
            cls.default_timeout = conn.execute("SHOW statement_timeout").fetchone()[0]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.stop()

    def lease(self, **kwargs: Any) -> KeptConnection:
        kwargs.setdefault("name", "tenant alpha")
        lease = KeptConnection(self.db.dsn, **kwargs)
        self.addCleanup(lease.close)
        return lease

    def items(self, tenant: str) -> Any:
        return sql.SQL("SELECT id, note FROM {} ORDER BY id").format(sql.Identifier(tenant, "items"))

    def session_state(self, lease: KeptConnection) -> tuple[str, str, str, str]:
        """``(current_user, session_user, role setting, statement_timeout)`` outside any transaction."""
        with lease.lease() as conn:
            self.assertEqual(TransactionStatus.IDLE, conn.info.transaction_status)
            row = conn.execute(
                "SELECT current_user AS cu, session_user AS su, current_setting('role') AS r, "
                "current_setting('statement_timeout') AS t"
            ).fetchone()
        return row["cu"], row["su"], row["r"], row["t"]

    def assert_clean_session(self, lease: KeptConnection) -> None:
        self.assertEqual(
            (self.db.owner, self.db.owner, "none", self.default_timeout),
            self.session_state(lease),
            "a role or setting from the last operation is still on the session",
        )

    def backend_pid(self, lease: KeptConnection) -> int:
        with lease.lease() as conn:
            return int(conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"])

    def wait_until_gone(self, pid: int) -> bool:
        with psycopg.connect(self.db.admin_dsn, autocommit=True) as admin:
            for _ in range(50):
                if admin.execute("SELECT count(*) FROM pg_stat_activity WHERE pid = %s", (pid,)).fetchone()[0] == 0:
                    return True
                time.sleep(0.05)
        return False

    def terminate(self, pid: int) -> None:
        with psycopg.connect(self.db.admin_dsn, autocommit=True) as admin:
            admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
        self.assertTrue(self.wait_until_gone(pid), f"backend {pid} did not stop")

    def assert_no_secret(self, error: BaseException) -> None:
        text = str(error)
        for secret in (self.db.dsn, self.password):
            self.assertNotIn(secret, text, f"the error message holds a secret: {text!r}")


class KeptConnectionPostgresTests(LeaseDatabaseTestCase):
    def test_one_connection_serves_every_operation(self) -> None:
        lease = self.lease()
        for _ in range(3):
            with lease.acting_as(self.db.alpha) as conn:
                self.assertEqual(self.db.alpha, conn.execute("SELECT current_user AS u").fetchone()["u"])
                self.assertEqual(2, len(conn.execute(self.items(self.db.alpha)).fetchall()))
        self.assertEqual(1, lease.connections_opened)
        self.assert_clean_session(lease)

    def test_rows_are_dicts_unless_the_lease_asks_for_tuples(self) -> None:
        lease = self.lease()
        with lease.lease_tuples() as conn:
            self.assertIsInstance(conn.execute("SELECT 1 AS one").fetchone(), tuple)
        with lease.lease() as conn:
            self.assertEqual({"one": 1}, conn.execute("SELECT 1 AS one").fetchone())
        self.assertEqual(1, lease.connections_opened)

    def test_the_statement_timeout_applies_inside_and_ends_with_the_operation(self) -> None:
        lease = self.lease(statement_timeout_ms=1234)
        self.assertEqual({"statement_timeout": "1234ms"}, lease.settings)
        with lease.acting_as(self.db.alpha) as conn:
            self.assertEqual("1234ms", conn.execute("SHOW statement_timeout").fetchone()["statement_timeout"])
        with lease.acting_as(self.db.alpha, {"lock_timeout": "250ms"}) as conn:
            row = conn.execute("SELECT current_setting('lock_timeout') AS l, current_setting('statement_timeout') AS t")
            self.assertEqual({"l": "250ms", "t": "1234ms"}, row.fetchone())
        self.assert_clean_session(lease)

    def test_an_error_inside_an_operation_leaves_no_role_behind(self) -> None:
        lease = self.lease()
        insert = sql.SQL("INSERT INTO {} VALUES (9, 'kept?')").format(sql.Identifier(self.db.alpha, "items"))
        with self.assertRaises(ZeroDivisionError):
            with lease.acting_as(self.db.alpha) as conn:
                conn.execute(insert)
                raise ZeroDivisionError
        self.assert_clean_session(lease)
        with lease.acting_as(self.db.alpha) as conn:
            self.assertEqual([1, 2], [row["id"] for row in conn.execute(self.items(self.db.alpha))])
        self.assertEqual(1, lease.connections_opened)

    def test_a_reused_connection_still_cannot_read_another_tenant(self) -> None:
        lease = self.lease()
        with lease.acting_as(self.db.alpha) as conn:
            conn.execute(self.items(self.db.alpha)).fetchall()
        with self.assertRaises(StoreNotSetUpError) as caught:
            with lease.acting_as(self.db.alpha) as conn:
                conn.execute(self.items(self.db.beta))
        self.assertIsInstance(caught.exception.__cause__, errors.InsufficientPrivilege)
        self.assertTrue(str(caught.exception).startswith("tenant alpha: "), str(caught.exception))
        self.assert_no_secret(caught.exception)
        self.assert_clean_session(lease)
        # Outside an operation the login role holds no tenant privileges either.
        with self.assertRaises(StoreNotSetUpError) as caught:
            with lease.lease() as conn:
                conn.execute(self.items(self.db.alpha))
        self.assertIsInstance(caught.exception.__cause__, errors.InsufficientPrivilege)
        self.assert_clean_session(lease)
        self.assertEqual(1, lease.connections_opened)

    def test_a_missing_table_is_not_set_up_and_keeps_the_connection(self) -> None:
        lease = self.lease()
        with self.assertRaises(StoreNotSetUpError) as caught:
            with lease.acting_as(self.db.alpha) as conn:
                conn.execute(sql.SQL("SELECT * FROM {}").format(sql.Identifier(self.db.alpha, "no_such_table")))
        self.assertIsInstance(caught.exception.__cause__, errors.UndefinedTable)
        message = str(caught.exception)
        self.assertIn("not set up", message)
        self.assertIn("no_such_table", message, "the server's message names the missing table")
        self.assert_clean_session(lease)
        self.assertEqual(1, lease.connections_opened)

    def test_a_role_that_does_not_exist_is_not_set_up(self) -> None:
        lease = self.lease()
        with self.assertRaises(StoreNotSetUpError):
            with lease.acting_as("dgt_no_such_role"):
                self.fail("the block ran as a role that does not exist")
        self.assert_clean_session(lease)
        self.assertEqual(1, lease.connections_opened)

    def test_other_database_errors_pass_through_unchanged(self) -> None:
        """The audit sink catches UniqueViolation itself, so the lease must not map it."""
        lease = self.lease()
        insert = sql.SQL("INSERT INTO {} VALUES (1, 'duplicate')").format(sql.Identifier(self.db.alpha, "items"))
        with self.assertRaises(errors.UniqueViolation):
            with lease.acting_as(self.db.alpha) as conn:
                conn.execute(insert)
        self.assert_clean_session(lease)
        self.assertEqual(1, lease.connections_opened)

    def test_concurrent_operations_take_turns_on_one_connection(self) -> None:
        lease = self.lease()
        failures: list[BaseException] = []
        users: list[str] = []

        def operate() -> None:
            try:
                with lease.acting_as(self.db.alpha) as conn:
                    users.append(conn.execute("SELECT current_user AS u, pg_sleep(0.01)").fetchone()["u"])
            except BaseException as error:  # noqa: BLE001 - reported below
                failures.append(error)

        threads = [threading.Thread(target=operate) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual([], failures)
        self.assertEqual([self.db.alpha] * 8, users)
        self.assertEqual(1, lease.connections_opened)
        self.assert_clean_session(lease)

    def test_a_nested_operation_gets_its_own_connection(self) -> None:
        lease = self.lease()
        with lease.acting_as(self.db.alpha) as outer:
            with lease.acting_as(self.db.beta) as inner:
                self.assertIsNot(outer, inner)
                self.assertEqual(self.db.beta, inner.execute("SELECT current_user AS u").fetchone()["u"])
            self.assertEqual(self.db.alpha, outer.execute("SELECT current_user AS u").fetchone()["u"])
        self.assertEqual(2, lease.connections_opened)
        self.assert_clean_session(lease)
        # The nested connection was closed; the kept one serves the next operation.
        with lease.acting_as(self.db.alpha):
            pass
        self.assertEqual(2, lease.connections_opened)

    def test_a_dead_connection_is_replaced_by_the_liveness_check(self) -> None:
        lease = self.lease(liveness_idle_seconds=0)
        self.terminate(self.backend_pid(lease))
        with lease.acting_as(self.db.alpha) as conn:
            self.assertEqual(2, len(conn.execute(self.items(self.db.alpha)).fetchall()))
        self.assertEqual(2, lease.connections_opened)
        self.assert_clean_session(lease)

    def test_without_the_check_a_dead_connection_fails_once_then_recovers(self) -> None:
        lease = self.lease(liveness_idle_seconds=3600)
        self.terminate(self.backend_pid(lease))
        with self.assertRaises(StoreUnavailableError) as caught:
            with lease.acting_as(self.db.alpha) as conn:
                conn.execute(self.items(self.db.alpha))
        self.assertNotIsInstance(caught.exception, StoreNotSetUpError)
        self.assertTrue(str(caught.exception).startswith("tenant alpha: "), str(caught.exception))
        self.assert_no_secret(caught.exception)
        with lease.acting_as(self.db.alpha) as conn:
            self.assertEqual(2, len(conn.execute(self.items(self.db.alpha)).fetchall()))
        self.assertEqual(2, lease.connections_opened)

    def test_a_statement_timeout_is_reported_and_does_not_stay(self) -> None:
        lease = self.lease(statement_timeout_ms=50)
        with self.assertRaises(StoreUnavailableError) as caught:
            with lease.acting_as(self.db.alpha) as conn:
                conn.execute("SELECT pg_sleep(1)")
        self.assertIsInstance(caught.exception.__cause__, errors.QueryCanceled)
        self.assertIn("statement timeout", str(caught.exception))
        self.assert_clean_session(lease)

    def test_close_releases_the_connection_and_the_next_operation_reopens_it(self) -> None:
        lease = self.lease()
        pid = self.backend_pid(lease)
        lease.close()
        self.assertTrue(self.wait_until_gone(pid), "close() left the connection open")
        with lease.acting_as(self.db.alpha):
            pass
        self.assertEqual(2, lease.connections_opened)

    def test_a_dropped_lease_closes_its_connection(self) -> None:
        lease = KeptConnection(self.db.dsn)
        pid = self.backend_pid(lease)
        del lease
        gc.collect()
        self.assertTrue(self.wait_until_gone(pid), "a dropped lease left its connection open")


class LeaseErrorTextPostgresTests(LeaseDatabaseTestCase):
    """A server message that quotes the connection string is scrubbed before it is shown."""

    def raise_in_server(self, conn: Any, sqlstate: str, message: str) -> None:
        quoted = message.replace("'", "''")
        conn.execute(
            f"DO $body$ BEGIN RAISE EXCEPTION USING ERRCODE = '{sqlstate}', MESSAGE = '{quoted}'; END $body$"
        )

    def test_a_not_set_up_error_that_quotes_the_secrets_is_scrubbed(self) -> None:
        lease = self.lease()
        with self.assertRaises(StoreNotSetUpError) as caught:
            with lease.acting_as(self.db.alpha) as conn:
                self.raise_in_server(conn, "42P01", f"relation {self.db.dsn} does not exist ({self.password})")
        self.assertIsInstance(caught.exception.__cause__, errors.UndefinedTable)
        self.assertIn("does not exist", str(caught.exception))
        self.assert_no_secret(caught.exception)
        self.assert_clean_session(lease)

    def test_an_unavailable_error_that_quotes_the_secrets_is_scrubbed(self) -> None:
        lease = self.lease()
        with self.assertRaises(StoreUnavailableError) as caught:
            with lease.acting_as(self.db.alpha) as conn:
                self.raise_in_server(conn, "57014", f"canceled near {self.db.dsn} with {self.password}")
        self.assertIsInstance(caught.exception.__cause__, errors.QueryCanceled)
        self.assert_no_secret(caught.exception)
        self.assertIn("canceled near", str(caught.exception))

    def test_a_driver_error_raised_inside_an_operation_loses_its_text(self) -> None:
        lease = self.lease()
        with self.assertRaises(StoreUnavailableError) as caught:
            with lease.lease():
                raise psycopg.OperationalError(f"consuming input failed for {self.db.dsn}")
        self.assertIn("OperationalError", str(caught.exception))
        self.assertNotIn("consuming input failed", str(caught.exception))
        self.assert_no_secret(caught.exception)
        # A failed connection is discarded, so the next operation opens another.
        with lease.lease():
            pass
        self.assertEqual(2, lease.connections_opened)


class LeaseAuditSinkPostgresTests(LeaseDatabaseTestCase):
    """``PostgresAuditSink`` takes its connections from the lease and opens none of its own."""

    def sink(self, lease: KeptConnection, factory: Any) -> Any:
        from agents.common.infrastructure.postgres_audit import PostgresAuditSink

        return PostgresAuditSink(
            factory,
            self.db.alpha,
            f"{self.db.alpha}.audit",
            settings=lease.settings,
            anchor_table=f"{self.db.alpha}.audit_anchor",
        )

    def test_the_sink_appends_and_verifies_through_the_lease(self) -> None:
        for name in ("lease", "lease_tuples"):
            with self.subTest(factory=name):
                lease = self.lease()
                sink = self.sink(lease, getattr(lease, name))
                before = sink.count()
                for number in range(3):
                    sink.append(audit_record(f"{name}-{number}"))
                self.assertEqual(before + 3, sink.count())
                self.assertTrue(sink.verify_chain())
                self.assertEqual(3, len(sink.list_recent(3)))
                self.assertEqual(1, lease.connections_opened)
                self.assert_clean_session(lease)
                with lease.lease() as conn:
                    self.assertIsInstance(conn.execute("SELECT 1 AS one").fetchone(), dict)

    def test_two_sinks_on_two_leases_keep_one_chain(self) -> None:
        first, second = self.lease(), self.lease()
        sink_a, sink_b = self.sink(first, first.lease), self.sink(second, second.lease_tuples)
        for number in range(3):
            sink_a.append(audit_record(f"a-{number}"))
            sink_b.append(audit_record(f"b-{number}"))
        self.assertTrue(sink_a.verify_chain(full=True))
        self.assertTrue(sink_b.verify_chain(full=True))


class SourceAdapterPostgresTests(LeaseDatabaseTestCase):
    """The Postgres source adapter reads in one transaction, as the role, with a statement timeout."""

    def read(self, query: str, role: str | None = None, **adapter_options: Any) -> list[dict[str, Any]]:
        params = conninfo_to_dict(self.db.dsn)
        adapter = PostgresSourceAdapter(**adapter_options)
        resolver = DefaultSourceResolver(
            connections=InMemoryConnectionResolver({"db": params}), adapters=[adapter]
        )
        options = {"role": role} if role is not None else {}
        source = PostgresSourceSpec(
            source_id="probe",
            connection_ref=ConnectionRef(connection_id="db", options=options),
            selection=PostgresSelection(sql=query),
        )
        batches = resolver.materialize(DatasetInput(source=source))
        return [record for batch in batches for record in batch.records]

    def test_the_query_runs_as_the_role_with_the_default_timeout(self) -> None:
        rows = self.read(
            "SELECT current_user AS u, current_setting('statement_timeout') AS t", role=self.db.alpha
        )
        self.assertEqual([{"u": self.db.alpha, "t": "15s"}], rows)

    def test_the_timeout_can_be_set_and_applies_without_a_role(self) -> None:
        rows = self.read(
            "SELECT current_user AS u, current_setting('statement_timeout') AS t", statement_timeout_ms=1234
        )
        self.assertEqual([{"u": self.db.owner, "t": "1234ms"}], rows)

    def test_a_slow_query_is_cancelled(self) -> None:
        with self.assertRaises(errors.QueryCanceled):
            self.read("SELECT pg_sleep(1)", role=self.db.alpha, statement_timeout_ms=50)

    def test_null_values_can_be_left_out_of_the_records(self) -> None:
        query = "SELECT 1 AS a, NULL::int AS b"
        self.assertEqual([{"a": 1, "b": None}], self.read(query))
        self.assertEqual([{"a": 1}], self.read(query, omit_null_keys=True))

    def test_another_tenant_is_still_refused(self) -> None:
        with self.assertRaises(errors.InsufficientPrivilege):
            self.read(f"SELECT * FROM {self.db.beta}.items", role=self.db.alpha)


if __name__ == "__main__":
    unittest.main()
