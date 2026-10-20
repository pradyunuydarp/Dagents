"""Role-scoped database access: acting_as, the source adapter's role option, and
the Postgres audit sink.

acting_as must cost two round trips besides the caller's queries (the opening
message and COMMIT) and keep every safety check; the audit sink must append with
one statement once it knows where the chain ends, and fall back to locking when
another writer got there first.

The checks on names and connection state need no database. The rest run
against a real PostgreSQL 16 server, because the point is what the server
allows. They need a superuser connection string in ``DAGENTS_TEST_DATABASE_URL``
(over TCP with a password, because the tests also log in as a role they
create). They create a scratch database and roles with random names, and drop
them afterwards. Without the variable they skip with a message that starts with
``database test skipped``.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import time
from types import SimpleNamespace
from typing import Any
import unittest
import uuid

try:
    import psycopg
    from psycopg import errors, sql
    from psycopg.conninfo import make_conninfo
    from psycopg.pq import TransactionStatus
except ModuleNotFoundError:  # pragma: no cover - the agents' requirements install it
    psycopg = None

from dagents.common.domain.governance import AuditRecord
from dagents.common.domain.sources import (
    ConnectionRef,
    DatasetInput,
    PostgresSelection,
    PostgresSourceSpec,
)
from dagents.common.infrastructure.postgres_roles import (
    RoleScopeError,
    acting_as,
    connect,
    validate_role_name,
    validate_setting_name,
)
from dagents.common.infrastructure.sources import (
    DefaultSourceResolver,
    InMemoryConnectionResolver,
    PostgresSourceAdapter,
    _postgres_connection_args,
)

DSN_VARIABLE = "DAGENTS_TEST_DATABASE_URL"
SKIP_PREFIX = "database test skipped: "


class RoleNameTests(unittest.TestCase):
    def test_plain_names_are_accepted(self) -> None:
        for name in ("tenant_a", "site_uk_134", "dagents_coordinator", "a", "a" * 63):
            with self.subTest(name=name):
                self.assertEqual(name, validate_role_name(name))

    def test_names_that_could_change_the_statement_are_refused(self) -> None:
        for name in ("", "A", "1tenant", "tenant-a", "tenant a", 'a"; RESET ROLE; --', "a;b", "a" * 64, None, 5):
            with self.subTest(name=name):
                with self.assertRaises(RoleScopeError):
                    validate_role_name(name)  # type: ignore[arg-type]

    def test_reserved_and_platform_roles_are_refused(self) -> None:
        for name in ("postgres", "public", "none", "current_user", "pg_read_all_data", "pg_monitor",
                     "anon", "authenticated", "service_role", "supabase_admin", "pgbouncer"):
            with self.subTest(name=name):
                with self.assertRaises(RoleScopeError):
                    validate_role_name(name)


#: Table names every Postgres store accepts: ``table`` or ``schema.table``, each
#: part a lower-case identifier of at most 63 characters.
GOOD_TABLES = (
    "audit",
    "a.audit",
    "_private",
    "coordinator.models",
    "site_uk_134.site_jobs",
    "a1.b2",
    "_a._b",
    "a" * 63,
    f"{'s' * 63}.{'t' * 63}",
)

#: Table names every Postgres store refuses. They include the names the
#: audit, model and job log tests refused before the stores shared one check.
BAD_TABLES = (
    "",
    "Audit",
    "Anchor",
    "A.models",
    "A.jobs",
    "a.b.c",
    "a;drop",
    "models; DROP TABLE x",
    "jobs; DROP TABLE x",
    "1audit",
    "a.1b",
    ".audit",
    "audit.",
    "a..b",
    "a" * 64,
    f"a.{'b' * 64}",
    "a b",
    "a-b",
    "audit\n",
    " audit",
    'a"b',
    "ä",
)


@unittest.skipIf(psycopg is None, SKIP_PREFIX + "psycopg is not installed")
class TableNameTests(unittest.TestCase):
    """One check for every store's ``schema.table`` name, so no store accepts a name another refuses."""

    def test_a_good_name_is_quoted_part_by_part(self) -> None:
        from dagents.common.infrastructure.postgres_roles import table_identifier

        for table in GOOD_TABLES:
            with self.subTest(table=table):
                self.assertEqual(sql.Identifier(*table.split(".")), table_identifier(table, "audit table"))

    def test_a_bad_name_is_refused_with_the_table_it_was_for(self) -> None:
        from dagents.common.infrastructure.postgres_roles import table_identifier

        for table in (*BAD_TABLES, None, 5):
            with self.subTest(table=table):
                with self.assertRaises(ValueError) as raised:
                    table_identifier(table, "models table")  # type: ignore[arg-type]
                self.assertIn("the models table must be 'schema.table' in lower case", str(raised.exception))

    def stores(self) -> dict[str, Any]:
        """Build each Postgres store with a table name. Building one opens no connection."""
        from dagents.common.infrastructure.postgres_audit import PostgresAuditSink
        from dagents.gma.infrastructure.postgres_models import PostgresModelStore
        from dagents.lma.infrastructure.job_log import PostgresJobLog

        no_connection = SimpleNamespace()
        return {
            "audit table": lambda table: PostgresAuditSink(lambda: None, "alpha", table),
            "audit anchor table": lambda table: PostgresAuditSink(lambda: None, "alpha", "a.audit", anchor_table=table),
            "models table": lambda table: PostgresModelStore(no_connection, "alpha", table),  # type: ignore[arg-type]
            "job log table": lambda table: PostgresJobLog(no_connection, "alpha", table),  # type: ignore[arg-type]
        }

    def test_every_store_accepts_and_refuses_the_same_names(self) -> None:
        for what, build in self.stores().items():
            for table in GOOD_TABLES:
                with self.subTest(store=what, table=table):
                    build(table)
            for table in BAD_TABLES:
                with self.subTest(store=what, table=table):
                    with self.assertRaises(ValueError) as raised:
                        build(table)
                    self.assertIn(f"the {what} must be 'schema.table' in lower case", str(raised.exception))

    def test_the_stores_keep_no_copy_of_the_check(self) -> None:
        from dagents.common.infrastructure import postgres_audit
        from dagents.gma.infrastructure import postgres_models
        from dagents.lma.infrastructure import job_log

        for module in (postgres_audit, postgres_models, job_log):
            with self.subTest(module=module.__name__):
                self.assertFalse(hasattr(module, "_table_identifier"))
                self.assertFalse(hasattr(module, "_IDENTIFIER"))


class FakeConnection:
    """Just enough of a connection to reach acting_as's own checks."""

    def __init__(self, autocommit: bool, status: Any) -> None:
        self.autocommit = autocommit
        self.info = SimpleNamespace(transaction_status=status)
        self.executed: list[Any] = []

    def transaction(self) -> Any:  # pragma: no cover - reached only if a check fails
        raise AssertionError("acting_as opened a transaction it should have refused")

    def execute(self, *args: Any) -> Any:  # pragma: no cover
        self.executed.append(args)
        raise AssertionError("acting_as ran SQL it should have refused")


@unittest.skipIf(psycopg is None, SKIP_PREFIX + "psycopg is not installed")
class ActingAsChecksTests(unittest.TestCase):
    def test_a_connection_that_is_not_autocommit_is_refused(self) -> None:
        with self.assertRaises(RoleScopeError):
            with acting_as(FakeConnection(False, TransactionStatus.IDLE), "tenant_a"):
                pass

    def test_a_connection_inside_a_transaction_is_refused(self) -> None:
        with self.assertRaises(RoleScopeError):
            with acting_as(FakeConnection(True, TransactionStatus.INTRANS), "tenant_a"):
                pass

    def test_a_bad_name_is_refused_before_any_sql(self) -> None:
        conn = FakeConnection(True, TransactionStatus.IDLE)
        with self.assertRaises(RoleScopeError):
            with acting_as(conn, "tenant_a; RESET ROLE"):
                pass
        self.assertEqual([], conn.executed)

    def test_bad_settings_are_refused_before_any_sql(self) -> None:
        bad = [
            {"role": "postgres"},
            {"session_authorization": "postgres"},
            {"Statement_Timeout": "1s"},
            {"statement_timeout = 0; RESET ROLE; --": "1s"},
            {"a.b.c": "1"},
            {"statement_timeout": True},
            {"statement_timeout": None},
            {"statement_timeout": ["1s"]},
        ]
        for settings in bad:
            with self.subTest(settings=settings):
                conn = FakeConnection(True, TransactionStatus.IDLE)
                with self.assertRaises(RoleScopeError):
                    with acting_as(conn, "tenant_a", settings):
                        pass
                self.assertEqual([], conn.executed)

    def test_setting_names_are_checked(self) -> None:
        for name in ("statement_timeout", "lock_timeout", "app.site", "dagents.test_marker"):
            with self.subTest(name=name):
                self.assertEqual(name, validate_setting_name(name))
        for name in ("role", "session_authorization", "", "a-b", "a.b.c", 'x"y', None):
            with self.subTest(name=name):
                with self.assertRaises(RoleScopeError):
                    validate_setting_name(name)  # type: ignore[arg-type]


class StatementCounter:
    """A connection that records the text of every statement sent through it.

    Each ``execute`` without parameters is one simple-query message, so the
    number of entries is the number of round trips.
    """

    def __init__(self, conn: Any, *, current_user: str | None = None) -> None:
        self._conn = conn
        self._fake_user = current_user
        self.sent: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    def _record(self, query: Any) -> None:
        self.sent.append(query if isinstance(query, str) else query.as_string(self._conn))

    def execute(self, query: Any, params: Any = None, **kwargs: Any) -> Any:
        self._record(query)
        return self._conn.execute(query, params, **kwargs)

    def cursor(self, *args: Any, **kwargs: Any) -> Any:
        counter = self
        inner = self._conn.cursor(*args, **kwargs)

        class Cursor:
            def __enter__(self) -> Any:
                inner.__enter__()
                return self

            def __exit__(self, *exc: Any) -> Any:
                return inner.__exit__(*exc)

            def __getattr__(self, name: str) -> Any:
                return getattr(inner, name)

            def execute(self, query: Any, params: Any = None, **kw: Any) -> Any:
                counter._record(query)
                inner.execute(query, params, **kw)
                return self

            def fetchone(self) -> Any:
                row = inner.fetchone()
                return (counter._fake_user,) if counter._fake_user is not None else row

        return Cursor()


def postgres_source(table: str, role: str | None = None, rows: list[dict] | None = None) -> PostgresSourceSpec:
    options: dict[str, Any] = {}
    if role is not None:
        options["role"] = role
    return PostgresSourceSpec(
        source_id="scoped",
        connection_ref=ConnectionRef(connection_id="db", options=options),
        selection=PostgresSelection(table=table, columns=["id", "note"], order_by=["id"]),
        options={"rows": rows} if rows is not None else {},
    )


class AdapterRoleOptionTests(unittest.TestCase):
    def test_role_is_not_passed_to_the_driver(self) -> None:
        self.assertEqual({"host": "db"}, _postgres_connection_args({"host": "db", "role": "tenant_a"}))

    def test_a_bad_role_is_refused_even_with_mock_rows(self) -> None:
        adapter = PostgresSourceAdapter()
        source = postgres_source("t", rows=[{"id": 1}])
        with self.assertRaises(RoleScopeError):
            adapter.scan(source, {"role": "postgres"})
        report = adapter.validate(source, {"role": "Tenant A"})
        self.assertFalse(report.valid)
        self.assertIn("refusing role name", " ".join(report.errors))

    def test_without_a_role_nothing_changes(self) -> None:
        adapter = PostgresSourceAdapter()
        batches = adapter.scan(postgres_source("t", rows=[{"id": 1}]), {})
        self.assertEqual([{"id": 1}], batches[0].records)
        self.assertTrue(adapter.validate(postgres_source("t", rows=[]), {"role": "tenant_a"}).valid)


class ScratchDatabase:
    """A scratch database, a CREATEROLE login role, and two NOLOGIN tenant roles.

    The login role creates the tenant roles and grants them to itself WITH
    INHERIT FALSE, SET TRUE, as a service's connecting role would.
    """

    def __init__(self, admin_dsn: str) -> None:
        token = secrets.token_hex(4)
        self.admin_dsn = admin_dsn
        self.name = f"dagents_roles_{token}"
        self.owner = f"dgt_{token}_owner"
        self.alpha = f"dgt_{token}_alpha"
        self.beta = f"dgt_{token}_beta"
        self.dsn = ""
        self._created = False

    def start(self) -> None:
        try:
            admin = psycopg.connect(self.admin_dsn, autocommit=True, connect_timeout=10)
        except psycopg.OperationalError as error:
            raise unittest.SkipTest(SKIP_PREFIX + f"cannot connect with {DSN_VARIABLE}: {error}")
        password = secrets.token_hex(16)
        with admin:
            version = int(admin.execute("SHOW server_version_num").fetchone()[0])
            if version < 160000:
                raise unittest.SkipTest(SKIP_PREFIX + f"needs PostgreSQL 16 or later, found {version}")
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(self.name)))
            self._created = True
            admin.execute(
                sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER CREATEROLE PASSWORD {}").format(
                    sql.Identifier(self.owner), sql.Literal(password)
                )
            )
            admin.execute(
                sql.SQL("GRANT CREATE, CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(self.name), sql.Identifier(self.owner)
                )
            )
        self.dsn = make_conninfo(self.admin_dsn, dbname=self.name, user=self.owner, password=password)
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            for tenant in (self.alpha, self.beta):
                ident = sql.Identifier(tenant)
                conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN NOINHERIT").format(ident))
                conn.execute(sql.SQL("GRANT {} TO current_user WITH INHERIT FALSE, SET TRUE").format(ident))
                conn.execute(sql.SQL("CREATE SCHEMA {} AUTHORIZATION {}").format(ident, ident))
                with conn.transaction():
                    conn.execute(sql.SQL("SET LOCAL ROLE {}").format(ident))
                    conn.execute(sql.SQL("CREATE TABLE {} (id int PRIMARY KEY, note text)").format(
                        sql.Identifier(tenant, "items")))
                    conn.execute(sql.SQL(
                        "CREATE TABLE {} (seq bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, "
                        "audit_id text NOT NULL UNIQUE, recorded_at bigint NOT NULL, boundary text NOT NULL, "
                        "decision text NOT NULL, permitted boolean NOT NULL, record jsonb NOT NULL, "
                        "previous_digest text NOT NULL UNIQUE, digest text NOT NULL UNIQUE)"
                    ).format(sql.Identifier(tenant, "audit")))
                    conn.execute(sql.SQL(
                        "CREATE TABLE {} (id boolean PRIMARY KEY DEFAULT true CHECK (id), seq bigint NOT NULL, "
                        "digest text NOT NULL, record_count bigint NOT NULL, "
                        "verified_at timestamptz NOT NULL DEFAULT now())"
                    ).format(sql.Identifier(tenant, "audit_anchor")))
                    conn.execute(sql.SQL("INSERT INTO {} VALUES (1, %s), (2, %s)").format(
                        sql.Identifier(tenant, "items")), (f"{tenant} one", f"{tenant} two"))
                    conn.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM PUBLIC").format(ident))

    def stop(self) -> None:
        if not self._created:
            return
        with psycopg.connect(self.admin_dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(self.name)))
            for role in (self.alpha, self.beta, self.owner):
                with contextlib.suppress(psycopg.Error):
                    admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
        self._created = False


def admin_dsn_or_skip() -> str:
    if psycopg is None:
        raise unittest.SkipTest(SKIP_PREFIX + "psycopg is not installed")
    dsn = os.environ.get(DSN_VARIABLE, "").strip()
    if not dsn:
        raise unittest.SkipTest(
            SKIP_PREFIX + f"set {DSN_VARIABLE} to a superuser connection string for a PostgreSQL 16 server"
        )
    return dsn


def audit_record(request_id: str) -> AuditRecord:
    return AuditRecord(
        audit_id=str(uuid.uuid4()),
        recorded_at=int(time.time()),
        boundary="before_send",
        request_id=request_id,
        requester_id="coordinator",
        decision="narrow",
        permitted=True,
        granularity="model_update",
        filtering_score=0.5,
        withheld_fields=["exact_value"],
        obligations=["record_privacy_accounting"],
    )


class DatabaseTestCase(unittest.TestCase):
    db: ScratchDatabase

    @classmethod
    def setUpClass(cls) -> None:
        cls.db = ScratchDatabase(admin_dsn_or_skip())
        try:
            cls.db.start()
        except BaseException:
            cls.db.stop()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.stop()

    def setUp(self) -> None:
        self.conn = connect(self.db.dsn)
        self.addCleanup(self.conn.close)

    def current_user(self) -> str:
        return self.conn.execute("SELECT current_user").fetchone()[0]

    def items(self, tenant: str) -> Any:
        return sql.SQL("SELECT id, note FROM {} ORDER BY id").format(sql.Identifier(tenant, "items"))


class ActingAsTests(DatabaseTestCase):
    def test_the_block_runs_as_the_role_and_the_role_ends_with_it(self) -> None:
        with acting_as(self.conn, self.db.alpha):
            self.assertEqual(self.db.alpha, self.current_user())
            self.assertEqual(self.db.owner, self.conn.execute("SELECT session_user").fetchone()[0])
            self.assertEqual(2, len(self.conn.execute(self.items(self.db.alpha)).fetchall()))
        self.assertEqual(self.db.owner, self.current_user())
        self.assertEqual(TransactionStatus.IDLE, self.conn.info.transaction_status)

    def test_an_error_rolls_back_and_ends_the_role(self) -> None:
        insert = sql.SQL("INSERT INTO {} VALUES (3, 'kept?')").format(sql.Identifier(self.db.alpha, "items"))
        with self.assertRaises(ZeroDivisionError):
            with acting_as(self.conn, self.db.alpha):
                self.conn.execute(insert)
                raise ZeroDivisionError
        self.assertEqual(self.db.owner, self.current_user())
        with acting_as(self.conn, self.db.alpha):
            self.assertEqual([1, 2], [row[0] for row in self.conn.execute(self.items(self.db.alpha))])

    def test_one_tenant_cannot_read_another(self) -> None:
        with self.assertRaises(errors.InsufficientPrivilege):
            with acting_as(self.conn, self.db.alpha):
                self.conn.execute(self.items(self.db.beta))
        self.assertEqual(self.db.owner, self.current_user())
        self.assertEqual(TransactionStatus.IDLE, self.conn.info.transaction_status)

    def test_the_login_role_holds_no_tenant_rights_without_switching(self) -> None:
        with self.assertRaises(errors.InsufficientPrivilege):
            self.conn.execute(self.items(self.db.alpha))

    def test_a_real_connection_outside_autocommit_is_refused(self) -> None:
        with psycopg.connect(self.db.dsn) as conn:
            with self.assertRaises(RoleScopeError):
                with acting_as(conn, self.db.alpha):
                    pass

    def test_nesting_is_refused(self) -> None:
        """Inside a transaction, BEGIN only warns, and the inner role would outlive the inner block."""
        with acting_as(self.conn, self.db.alpha):
            with self.assertRaises(RoleScopeError):
                with acting_as(self.conn, self.db.beta):
                    pass

    def test_a_role_that_does_not_exist_fails_inside_the_transaction(self) -> None:
        with self.assertRaises(psycopg.Error):
            with acting_as(self.conn, "dgt_no_such_role"):
                pass
        self.assertEqual(self.db.owner, self.current_user())
        self.assertEqual(TransactionStatus.IDLE, self.conn.info.transaction_status)

    def assert_back_to_the_login_role(self) -> None:
        self.assertEqual(TransactionStatus.IDLE, self.conn.info.transaction_status)
        self.assertEqual(self.db.owner, self.current_user())

    def test_the_opening_is_one_message_and_commit_the_only_other(self) -> None:
        counter = StatementCounter(self.conn)
        with acting_as(counter, self.db.alpha, {"statement_timeout": "1234ms"}):
            pass
        self.assertEqual(2, len(counter.sent), counter.sent)
        opening, commit = counter.sent
        self.assertEqual(
            f'BEGIN; SET LOCAL ROLE "{self.db.alpha}"; SET LOCAL "statement_timeout" = \'1234ms\'; '
            "SELECT current_user",
            opening,
        )
        self.assertEqual("COMMIT", commit)
        # A block with one query costs that query plus the same two.
        counter = StatementCounter(self.conn)
        with acting_as(counter, self.db.alpha):
            counter.execute(self.items(self.db.alpha)).fetchall()
        self.assertEqual(3, len(counter.sent))
        self.assert_back_to_the_login_role()

    def test_settings_apply_inside_the_transaction_and_end_with_it(self) -> None:
        default = self.conn.execute("SHOW statement_timeout").fetchone()[0]
        settings = {"statement_timeout": "1234ms", "dagents.test_marker": "it's alpha"}
        with acting_as(self.conn, self.db.alpha, settings):
            self.assertEqual("1234ms", self.conn.execute("SHOW statement_timeout").fetchone()[0])
            self.assertEqual(
                "it's alpha", self.conn.execute("SELECT current_setting('dagents.test_marker')").fetchone()[0]
            )
        self.assertEqual(default, self.conn.execute("SHOW statement_timeout").fetchone()[0])
        self.assertIn(
            self.conn.execute("SELECT current_setting('dagents.test_marker', true)").fetchone()[0], ("", None)
        )
        self.assert_back_to_the_login_role()

    def test_the_statement_timeout_setting_is_enforced(self) -> None:
        with self.assertRaises(errors.QueryCanceled):
            with acting_as(self.conn, self.db.alpha, {"statement_timeout": "50ms"}):
                self.conn.execute("SELECT pg_sleep(1)")
        self.assert_back_to_the_login_role()

    def test_a_wrong_current_user_stops_the_block_before_it_runs(self) -> None:
        counter = StatementCounter(self.conn, current_user=self.db.beta)
        with self.assertRaises(RoleScopeError):
            with acting_as(counter, self.db.alpha):
                self.fail("the block ran although current_user was wrong")
        self.assertEqual("ROLLBACK", counter.sent[-1])
        self.assert_back_to_the_login_role()

    def test_a_caught_error_inside_the_block_rolls_everything_back(self) -> None:
        insert = sql.SQL("INSERT INTO {} VALUES (4, 'kept?')").format(sql.Identifier(self.db.alpha, "items"))
        with self.assertRaises(RoleScopeError):
            with acting_as(self.conn, self.db.alpha):
                self.conn.execute(insert)
                with contextlib.suppress(errors.UndefinedTable):
                    self.conn.execute("SELECT * FROM no_such_table")
        self.assert_back_to_the_login_role()
        with acting_as(self.conn, self.db.alpha):
            self.assertEqual([1, 2], [row[0] for row in self.conn.execute(self.items(self.db.alpha))])

    def test_a_block_that_ends_the_transaction_itself_is_reported(self) -> None:
        with self.assertRaises(RoleScopeError):
            with acting_as(self.conn, self.db.alpha):
                self.conn.execute("COMMIT")
        self.assert_back_to_the_login_role()


class ConnectTests(DatabaseTestCase):
    def test_connections_are_autocommit_without_prepared_statements(self) -> None:
        self.assertTrue(self.conn.autocommit)
        self.assertIsNone(self.conn.prepare_threshold)
        self.assertEqual("10", self.conn.info.get_parameters()["connect_timeout"])

    def test_a_mapping_of_keywords_works(self) -> None:
        params = psycopg.conninfo.conninfo_to_dict(self.db.dsn)
        with connect({**params, "connect_timeout": 5}) as conn:
            self.assertTrue(conn.autocommit)
            self.assertEqual("5", conn.info.get_parameters()["connect_timeout"])


class AdapterRoleTests(DatabaseTestCase):
    def resolver(self, role: str | None) -> tuple[DefaultSourceResolver, PostgresSourceSpec]:
        params = psycopg.conninfo.conninfo_to_dict(self.db.dsn)
        resolver = DefaultSourceResolver(connections=InMemoryConnectionResolver({"db": params}))
        return resolver, postgres_source(f"{self.db.alpha}.items", role=role)

    def test_the_adapter_reads_as_the_role(self) -> None:
        resolver, source = self.resolver(self.db.alpha)
        batches = resolver.materialize(DatasetInput(source=source))
        self.assertEqual([1, 2], [record["id"] for record in batches[0].records])

    def test_the_adapter_as_another_role_is_denied(self) -> None:
        resolver, source = self.resolver(self.db.beta)
        with self.assertRaises(errors.InsufficientPrivilege):
            resolver.materialize(DatasetInput(source=source))

    def test_the_adapter_without_a_role_runs_as_the_login_role(self) -> None:
        resolver, source = self.resolver(None)
        with self.assertRaises(errors.InsufficientPrivilege):
            resolver.materialize(DatasetInput(source=source))


class PostgresAuditSinkTests(DatabaseTestCase):
    def sink(self, role: str, table: str | None = None, *, anchor: bool = False) -> Any:
        from dagents.common.infrastructure.postgres_audit import PostgresAuditSink

        return PostgresAuditSink(
            lambda: connect(self.db.dsn),
            role,
            f"{self.db.alpha}.audit" if table is None else table,
            anchor_table=f"{self.db.alpha}.audit_anchor" if anchor else None,
        )

    def setUp(self) -> None:
        super().setUp()
        with acting_as(self.conn, self.db.alpha):
            self.conn.execute(sql.SQL("TRUNCATE {}, {} RESTART IDENTITY").format(
                sql.Identifier(self.db.alpha, "audit"), sql.Identifier(self.db.alpha, "audit_anchor")))

    def run_as_alpha(self, statement: str, params: tuple = ()) -> Any:
        """Run one statement on alpha's audit tables: ``{audit}`` and ``{anchor}`` name them."""
        query = sql.SQL(statement).format(
            audit=sql.Identifier(self.db.alpha, "audit"), anchor=sql.Identifier(self.db.alpha, "audit_anchor")
        )
        with acting_as(self.conn, self.db.alpha):
            cursor = self.conn.execute(query, params)
            return cursor.fetchall() if cursor.description else None

    def stored_anchor(self) -> tuple | None:
        rows = self.run_as_alpha("SELECT seq, digest, record_count FROM {anchor}")
        return tuple(rows[0]) if rows else None

    def test_records_are_chained_and_listed_newest_first(self) -> None:
        sink = self.sink(self.db.alpha)
        first = sink.append(audit_record("r1"))
        second = sink.append(audit_record("r2"))
        third = sink.append(audit_record("r3"))
        self.assertEqual("", first.previous_digest)
        self.assertEqual(first.digest, second.previous_digest)
        self.assertEqual([third, second], sink.list_recent(2))
        self.assertEqual([], sink.list_recent(0))
        self.assertTrue(sink.verify_chain())

    def test_the_digests_match_the_in_memory_log(self) -> None:
        from dagents.common.application.ethical_guard import InMemoryAuditLog

        record = audit_record("same")
        self.assertEqual(InMemoryAuditLog().append(record), self.sink(self.db.alpha).append(record))

    def test_an_edited_or_removed_record_breaks_the_chain(self) -> None:
        sink = self.sink(self.db.alpha)
        for request in ("r1", "r2", "r3"):
            sink.append(audit_record(request))
        table = sql.Identifier(self.db.alpha, "audit")
        with acting_as(self.conn, self.db.alpha):
            self.conn.execute(sql.SQL("DELETE FROM {} WHERE seq = 2").format(table))
        self.assertFalse(sink.verify_chain())

    def test_count_is_the_number_of_stored_records(self) -> None:
        sink = self.sink(self.db.alpha)
        self.assertEqual(0, sink.count())
        for request in ("r1", "r2", "r3"):
            sink.append(audit_record(request))
        self.assertEqual(3, sink.count())
        self.assertEqual(3, self.sink(self.db.alpha).count(), "the count comes from the table")

    def test_a_second_check_reads_only_the_records_added_since_the_first(self) -> None:
        sink = self.sink(self.db.alpha)
        for request in ("r1", "r2", "r3", "r4"):
            sink.append(audit_record(request))
        self.assertTrue(sink.verify_chain())
        self.assertEqual(4, sink.rows_read)
        sink.append(audit_record("r5"))
        sink.append(audit_record("r6"))
        self.assertTrue(sink.verify_chain())
        # The last record checked, which anchors the check, and the two new ones.
        self.assertEqual(3, sink.rows_read)
        self.assertTrue(sink.verify_chain())
        self.assertEqual(1, sink.rows_read)
        self.assertTrue(sink.verify_chain(full=True))
        self.assertEqual(6, sink.rows_read)

    def test_a_record_edited_after_the_last_check_breaks_the_chain(self) -> None:
        sink = self.sink(self.db.alpha)
        for request in ("r1", "r2"):
            sink.append(audit_record(request))
        self.assertTrue(sink.verify_chain())
        sink.append(audit_record("r3"))
        sink.append(audit_record("r4"))
        table = sql.Identifier(self.db.alpha, "audit")
        with acting_as(self.conn, self.db.alpha):
            self.conn.execute(
                sql.SQL("UPDATE {} SET record = jsonb_set(record, '{{requester_id}}', '\"someone\"') WHERE seq = 3")
                .format(table)
            )
        self.assertFalse(sink.verify_chain())
        # A failed check is not remembered, so the next one reads everything.
        self.assertFalse(sink.verify_chain())
        self.assertEqual(4, sink.rows_read)

    def test_an_edit_to_the_last_checked_record_breaks_the_chain(self) -> None:
        """The record the next check starts from is hashed again, not trusted."""
        sink = self.sink(self.db.alpha)
        for request in ("r1", "r2"):
            sink.append(audit_record(request))
        self.assertTrue(sink.verify_chain())
        table = sql.Identifier(self.db.alpha, "audit")
        with acting_as(self.conn, self.db.alpha):
            self.conn.execute(
                sql.SQL("UPDATE {} SET record = jsonb_set(record, '{{requester_id}}', '\"someone\"') WHERE seq = 2")
                .format(table)
            )
        sink.append(audit_record("r3"))
        self.assertFalse(sink.verify_chain())

    def test_a_full_check_finds_an_edit_to_a_record_checked_before(self) -> None:
        """The default check does not re-hash checked records; full=True does."""
        sink = self.sink(self.db.alpha)
        for request in ("r1", "r2", "r3"):
            sink.append(audit_record(request))
        self.assertTrue(sink.verify_chain())
        self.run_as_alpha("UPDATE {audit} SET record = jsonb_set(record, '{{requester_id}}', '\"someone\"') "
                          "WHERE seq = 2")
        self.assertTrue(sink.verify_chain())
        self.assertFalse(sink.verify_chain(full=True))
        self.assertFalse(self.sink(self.db.alpha).verify_chain(), "a new sink's first check reads everything")

    def test_removing_a_record_checked_before_is_caught_by_the_default_check(self) -> None:
        """The count of records up to the anchor must not change."""
        for removed in (1, 2):
            with self.subTest(removed=removed):
                self.run_as_alpha("TRUNCATE {audit} RESTART IDENTITY")
                sink = self.sink(self.db.alpha)
                for request in ("r1", "r2", "r3", "r4"):
                    sink.append(audit_record(request))
                self.assertTrue(sink.verify_chain())
                self.run_as_alpha("DELETE FROM {audit} WHERE seq = %s", (removed,))
                self.assertFalse(sink.verify_chain())
                self.assertFalse(sink.verify_chain(), "a failed check is not remembered")

    def test_a_record_inserted_before_the_last_check_is_caught_by_the_default_check(self) -> None:
        sink = self.sink(self.db.alpha)
        for request in ("r1", "r2", "r3"):
            sink.append(audit_record(request))
        self.assertTrue(sink.verify_chain())
        self.run_as_alpha(
            "INSERT INTO {audit} (seq, audit_id, recorded_at, boundary, decision, permitted, record, "
            "previous_digest, digest) OVERRIDING SYSTEM VALUE "
            "VALUES (0, 'planted', 0, 'before_send', 'permit', true, '{{}}', 'p', 'q')"
        )
        self.assertFalse(sink.verify_chain())

    def test_removing_the_last_checked_record_falls_back_to_a_full_check(self) -> None:
        """A full check would accept this chain, so the default check must too."""
        sink = self.sink(self.db.alpha)
        first = sink.append(audit_record("r1"))
        sink.append(audit_record("r2"))
        self.assertTrue(sink.verify_chain())
        with acting_as(self.conn, self.db.alpha):
            self.conn.execute(sql.SQL("DELETE FROM {} WHERE seq = 2").format(sql.Identifier(self.db.alpha, "audit")))
        third = sink.append(audit_record("r3"))
        self.assertEqual(first.digest, third.previous_digest)
        self.assertTrue(sink.verify_chain())
        self.assertTrue(sink.verify_chain(full=True))

    # --- The anchor stored in the database --------------------------------------------

    def test_a_check_stores_its_anchor(self) -> None:
        sink = self.sink(self.db.alpha, anchor=True)
        self.assertTrue(sink.verify_chain())
        self.assertIsNone(self.stored_anchor(), "an empty chain has nothing to anchor")
        written = [sink.append(audit_record(f"r{number}")) for number in range(3)]
        self.assertTrue(sink.verify_chain())
        self.assertEqual((3, written[-1].digest, 3), self.stored_anchor())

    def test_a_new_sink_continues_from_the_stored_anchor(self) -> None:
        """A restarted API reads only the anchor and the records after it."""
        first = self.sink(self.db.alpha, anchor=True)
        for number in range(5):
            first.append(audit_record(f"r{number}"))
        self.assertTrue(first.verify_chain())
        self.assertEqual(5, first.rows_read)
        restarted = self.sink(self.db.alpha, anchor=True)
        restarted.append(audit_record("r5"))
        restarted.append(audit_record("r6"))
        self.assertTrue(restarted.verify_chain())
        self.assertEqual(3, restarted.rows_read, "the anchor and the two new records")
        self.assertEqual(7, self.stored_anchor()[2])
        again = self.sink(self.db.alpha, anchor=True)
        self.assertTrue(again.verify_chain())
        self.assertEqual(1, again.rows_read)
        self.assertTrue(again.verify_chain(full=True))
        self.assertEqual(7, again.rows_read)

    def test_the_stored_anchor_is_read_and_checked_in_one_transaction(self) -> None:
        sink = self.sink(self.db.alpha, anchor=True)
        for number in range(3):
            sink.append(audit_record(f"r{number}"))
        self.assertTrue(sink.verify_chain())
        restarted = self.sink(self.db.alpha, anchor=True)
        connections: list[Any] = []
        real = restarted._connect  # noqa: SLF001 - counting connections is the point

        def counting() -> Any:
            connections.append(1)
            return real()

        restarted._connect = counting  # noqa: SLF001
        self.assertTrue(restarted.verify_chain())
        self.assertEqual(1, len(connections))
        self.assertEqual(2, restarted.statements, "read the anchor, then the records from it")

    def test_a_repeated_check_reads_the_stored_anchor_once(self) -> None:
        sink = self.sink(self.db.alpha, anchor=True)
        for number in range(3):
            sink.append(audit_record(f"r{number}"))
        self.assertTrue(sink.verify_chain())
        before = sink.statements
        self.assertTrue(sink.verify_chain())
        self.assertEqual(1, sink.statements - before, "nothing changed: one statement, no write")
        sink.append(audit_record("r3"))
        before = sink.statements
        self.assertTrue(sink.verify_chain())
        self.assertEqual(2, sink.statements - before, "the check, then the new anchor")
        self.assertEqual(4, self.stored_anchor()[2])

    def test_without_an_anchor_table_a_new_sink_reads_everything(self) -> None:
        first = self.sink(self.db.alpha, anchor=True)
        for number in range(4):
            first.append(audit_record(f"r{number}"))
        self.assertTrue(first.verify_chain())
        plain = self.sink(self.db.alpha)
        self.assertTrue(plain.verify_chain())
        self.assertEqual(4, plain.rows_read)

    def test_a_stored_anchor_that_does_not_match_is_not_trusted(self) -> None:
        """A wrong digest, a wrong count or a missing record: the new sink checks everything."""
        sink = self.sink(self.db.alpha, anchor=True)
        written = [sink.append(audit_record(f"r{number}")) for number in range(4)]
        self.assertTrue(sink.verify_chain())
        good = self.stored_anchor()
        for update, params in (
            ("UPDATE {anchor} SET digest = %s", ("f" * 64,)),
            ("UPDATE {anchor} SET record_count = 3", ()),
            ("UPDATE {anchor} SET seq = 99", ()),
            ("UPDATE {anchor} SET seq = 2, digest = %s", (written[-1].digest,)),
        ):
            with self.subTest(update=update):
                self.run_as_alpha(update, params)
                restarted = self.sink(self.db.alpha, anchor=True)
                self.assertTrue(restarted.verify_chain())
                self.assertEqual(4, restarted.rows_read, "a full check")
                self.assertEqual(good, self.stored_anchor(), "the full check stored the right anchor")

    def test_an_edited_anchor_record_breaks_the_chain_for_a_new_sink(self) -> None:
        sink = self.sink(self.db.alpha, anchor=True)
        for number in range(3):
            sink.append(audit_record(f"r{number}"))
        self.assertTrue(sink.verify_chain())
        self.run_as_alpha("UPDATE {audit} SET record = jsonb_set(record, '{{requester_id}}', '\"someone\"') "
                          "WHERE seq = 3")
        self.assertFalse(self.sink(self.db.alpha, anchor=True).verify_chain())

    def test_a_record_removed_before_the_stored_anchor_is_caught_by_a_new_sink(self) -> None:
        sink = self.sink(self.db.alpha, anchor=True)
        for number in range(4):
            sink.append(audit_record(f"r{number}"))
        self.assertTrue(sink.verify_chain())
        self.run_as_alpha("DELETE FROM {audit} WHERE seq = 2")
        restarted = self.sink(self.db.alpha, anchor=True)
        self.assertFalse(restarted.verify_chain())
        self.assertIsNone(self.stored_anchor(), "a failed check removes the stored anchor")
        self.assertFalse(self.sink(self.db.alpha, anchor=True).verify_chain())

    def test_after_a_full_check_fails_the_default_check_does_not_pass(self) -> None:
        """An edit before the anchor is found only by a full check, and then stays found."""
        sink = self.sink(self.db.alpha, anchor=True)
        for number in range(4):
            sink.append(audit_record(f"r{number}"))
        self.assertTrue(sink.verify_chain())
        self.run_as_alpha("UPDATE {audit} SET record = jsonb_set(record, '{{requester_id}}', '\"someone\"') "
                          "WHERE seq = 1")
        self.assertTrue(sink.verify_chain(), "the default check does not re-hash checked records")
        self.assertFalse(sink.verify_chain(full=True))
        self.assertFalse(sink.verify_chain())
        self.assertFalse(self.sink(self.db.alpha, anchor=True).verify_chain())

    def test_another_role_cannot_read_the_stored_anchor(self) -> None:
        sink = self.sink(self.db.alpha, anchor=True)
        sink.append(audit_record("r1"))
        self.assertTrue(sink.verify_chain())
        with self.assertRaises(errors.InsufficientPrivilege):
            with acting_as(self.conn, self.db.beta):
                self.conn.execute(sql.SQL("SELECT * FROM {}").format(sql.Identifier(self.db.alpha, "audit_anchor")))

    def test_bad_anchor_table_names_are_refused(self) -> None:
        from dagents.common.infrastructure.postgres_audit import PostgresAuditSink

        for table in ("Anchor", "a.b.c", "a;drop"):
            with self.subTest(table=table):
                with self.assertRaises(ValueError):
                    PostgresAuditSink(lambda: connect(self.db.dsn), self.db.alpha, "a.audit", anchor_table=table)

    def test_the_chain_cannot_fork(self) -> None:
        sink = self.sink(self.db.alpha)
        first = sink.append(audit_record("r1"))
        sink.append(audit_record("r2"))
        with self.assertRaises(errors.UniqueViolation):
            with acting_as(self.conn, self.db.alpha):
                self.conn.execute(
                    sql.SQL(
                        "INSERT INTO {} (audit_id, recorded_at, boundary, decision, permitted, record, "
                        "previous_digest, digest) VALUES ('x', 0, 'before_send', 'deny', false, '{{}}', %s, 'y')"
                    ).format(sql.Identifier(self.db.alpha, "audit")),
                    (first.digest,),
                )

    def test_another_role_cannot_read_the_chain(self) -> None:
        self.sink(self.db.alpha).append(audit_record("r1"))
        with self.assertRaises(errors.InsufficientPrivilege):
            self.sink(self.db.beta, f"{self.db.alpha}.audit").list_recent()

    def test_bad_table_names_are_refused(self) -> None:
        for table in ("Audit", "a.b.c", "a;drop", ""):
            with self.subTest(table=table):
                with self.assertRaises(ValueError):
                    self.sink(self.db.alpha, table)

    def test_bad_setting_names_are_refused(self) -> None:
        from dagents.common.infrastructure.postgres_audit import PostgresAuditSink

        with self.assertRaises(RoleScopeError):
            PostgresAuditSink(lambda: connect(self.db.dsn), self.db.alpha, "a.audit", settings={"role": "x"})

    def test_after_the_first_append_an_append_is_one_statement(self) -> None:
        sink = self.sink(self.db.alpha)
        sink.append(audit_record("r1"))
        # The first append locks the table and reads the last digest (one
        # message), then inserts.
        self.assertEqual(2, sink.statements)
        sink.append(audit_record("r2"))
        sink.append(audit_record("r3"))
        self.assertEqual(4, sink.statements)
        self.assertTrue(sink.verify_chain())

    def test_the_settings_go_into_every_operation(self) -> None:
        from dagents.common.infrastructure.postgres_audit import PostgresAuditSink

        counters: list[StatementCounter] = []

        @contextlib.contextmanager
        def counting_connection() -> Any:
            with connect(self.db.dsn) as conn:
                counters.append(StatementCounter(conn))
                yield counters[-1]

        sink = PostgresAuditSink(
            counting_connection, self.db.alpha, f"{self.db.alpha}.audit", settings={"statement_timeout": "4321ms"}
        )
        sink.append(audit_record("r1"))
        sink.append(audit_record("r2"))
        sink.list_recent(1)
        sink.verify_chain()
        self.assertEqual(4, len(counters))
        for counter in counters:
            self.assertIn("SET LOCAL \"statement_timeout\" = '4321ms'", counter.sent[0])

    def test_two_sinks_on_one_table_keep_one_chain(self) -> None:
        first, second = self.sink(self.db.alpha), self.sink(self.db.alpha)
        written = []
        for number in range(6):
            sink = first if number % 2 == 0 else second
            written.append(sink.append(audit_record(f"r{number}")))
        for earlier, later in zip(written, written[1:]):
            self.assertEqual(earlier.digest, later.previous_digest)
        self.assertTrue(first.verify_chain())
        self.assertEqual(written[::-1], first.list_recent(10))

    def test_an_append_after_the_last_record_was_removed_links_to_the_new_last(self) -> None:
        sink = self.sink(self.db.alpha)
        first = sink.append(audit_record("r1"))
        sink.append(audit_record("r2"))
        with acting_as(self.conn, self.db.alpha):
            self.conn.execute(sql.SQL("DELETE FROM {} WHERE seq = 2").format(sql.Identifier(self.db.alpha, "audit")))
        third = sink.append(audit_record("r3"))
        self.assertEqual(first.digest, third.previous_digest)
        self.assertTrue(sink.verify_chain())

    def test_a_writer_that_extends_the_same_record_first_makes_the_sink_retry(self) -> None:
        import threading

        from dagents.common.infrastructure.postgres_audit import PostgresAuditSink

        sink = self.sink(self.db.alpha)
        first = sink.append(audit_record("r1"))
        # Another writer extends r1 and has not committed yet.
        other = PostgresAuditSink._chain(audit_record("other"), first.digest)
        outcome: dict[str, Any] = {}

        def append_in_thread() -> None:
            try:
                outcome["record"] = sink.append(audit_record("r2"))
            except BaseException as error:  # noqa: BLE001 - checked below
                outcome["error"] = error

        with connect(self.db.dsn) as writer:
            with acting_as(writer, self.db.alpha):
                writer.execute(
                    sql.SQL(
                        "INSERT INTO {} (audit_id, recorded_at, boundary, decision, permitted, record, "
                        "previous_digest, digest) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
                    ).format(sql.Identifier(self.db.alpha, "audit")),
                    PostgresAuditSink._values(other),
                )
                thread = threading.Thread(target=append_in_thread)
                thread.start()
                # The sink's INSERT waits on the uncommitted row with the same
                # previous_digest until this transaction commits.
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    waiting = self.conn.execute(
                        "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                        "AND datname = current_database()"
                    ).fetchone()[0]
                    if waiting:
                        break
                    time.sleep(0.02)
                else:
                    self.fail("the sink's append never waited for the other writer")
        thread.join(10)
        self.assertNotIn("error", outcome)
        self.assertEqual(other.digest, outcome["record"].previous_digest)
        self.assertTrue(sink.verify_chain())
        self.assertEqual(3, len(sink.list_recent(10)))


if __name__ == "__main__":
    unittest.main()
