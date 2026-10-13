"""Each hospital's schema is reachable only as that hospital's role.

These tests apply the real migrations to a scratch PostgreSQL 16 database, as
a non-superuser that stands in for Supabase's ``postgres`` role, and then try
every path one party could use to read another party's rows.

They need an admin connection string in ``HEALTHCARE_DEMO_TEST_DATABASE_URL``
(a superuser, over TCP with a password, because the tests also log in as the
stand-in role). They create a scratch database and drop it afterwards. Roles
are shared by the whole cluster: the tests create the ones they need and drop
the ones that did not exist before. Without the variable they skip with a
message that starts with ``database test skipped``; CI fails on that message.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
from pathlib import Path
import secrets
import sys
import tempfile
from typing import Any, Iterator
import unittest
from unittest import mock

APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT / "backend"))
sys.path.insert(0, str(APP_ROOT.parents[1]))
sys.path.insert(0, str(APP_ROOT / "scripts"))

try:
    import psycopg
    from psycopg import errors, sql
    from psycopg.conninfo import make_conninfo
except ModuleNotFoundError:  # pragma: no cover - psycopg is in the app's requirements
    psycopg = None

from agents.common.infrastructure.postgres_roles import acting_as, connect  # noqa: E402

ADMIN_DSN_VARIABLE = "HEALTHCARE_DEMO_TEST_DATABASE_URL"
#: Every skip in the database tests starts with this, so CI can fail on it.
SKIP_PREFIX = "database test skipped: "

EMULATION_SQL = APP_ROOT / "supabase" / "emulate_supabase_roles.sql"
MIGRATION_0001 = APP_ROOT / "supabase" / "migrations" / "0001_stroke_encounters.sql"
MIGRATION_0002 = APP_ROOT / "supabase" / "migrations" / "0002_site_isolation.sql"
MIGRATION_0003 = APP_ROOT / "supabase" / "migrations" / "0003_coordinator_records.sql"
#: The coordinator's tables, from 0002 and 0003.
COORDINATOR_TABLES = ("coordinator.models", "coordinator.site_contributions", "coordinator.pilot_reports",
                      "coordinator.guard_audit", "coordinator.guard_audit_anchor")
#: Each hospital's tables, from 0002 and 0003.
SITE_TABLES = ("encounters", "guard_audit", "guard_audit_anchor")

SITE_ROLES = ("site_uk_134", "site_it_002", "site_ch_245")
COORDINATOR = "dagents_coordinator"
API_ROLES = ("anon", "authenticated", "service_role")
#: Roles the emulation and the migration create, in the order they can be dropped.
CREATED_ROLES = SITE_ROLES + (COORDINATOR, "supa_like") + API_ROLES


def admin_dsn_or_skip() -> str:
    """The admin connection string, or a skip that says how to provide one."""
    if psycopg is None:
        raise unittest.SkipTest(SKIP_PREFIX + "psycopg is not installed")
    dsn = os.environ.get(ADMIN_DSN_VARIABLE, "").strip()
    if not dsn:
        raise unittest.SkipTest(
            SKIP_PREFIX
            + f"set {ADMIN_DSN_VARIABLE} to a superuser connection string for a PostgreSQL 16 "
            "server, for example postgresql://postgres:postgres@localhost:5432/postgres"
        )
    return dsn


class SupabaseLikeDatabase:
    """A scratch database with Supabase's roles and, optionally, the migrations.

    ``dsn`` logs in as ``supa_like`` (NOSUPERUSER CREATEROLE BYPASSRLS), the
    stand-in for Supabase's ``postgres`` role.
    """

    def __init__(self, admin_dsn: str) -> None:
        self.admin_dsn = admin_dsn
        self.name = f"dagents_scratch_{secrets.token_hex(4)}"
        self.dsn = ""
        self._roles_before: set[str] = set()
        self._started = False

    def start(self, *, migrate: bool = True) -> "SupabaseLikeDatabase":
        try:
            admin = psycopg.connect(self.admin_dsn, autocommit=True, connect_timeout=10)
        except psycopg.OperationalError as error:
            raise unittest.SkipTest(SKIP_PREFIX + f"cannot connect with {ADMIN_DSN_VARIABLE}: {error}")
        with admin:
            version = int(admin.execute("SHOW server_version_num").fetchone()[0])
            if version < 160000:
                raise unittest.SkipTest(SKIP_PREFIX + f"needs PostgreSQL 16 or later, found {version}")
            self._roles_before = {row[0] for row in admin.execute("SELECT rolname FROM pg_roles")}
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(self.name)))
        self._started = True
        # Roles belong to the whole server, so every run shares supa_like. A
        # random password per run would lock out a suite running at the same
        # time; this one is the same for every run against this server and is
        # not stored anywhere.
        password = hashlib.sha256(f"supa_like:{self.admin_dsn}".encode()).hexdigest()[:32]
        with self.admin() as conn:
            conn.execute(EMULATION_SQL.read_text(encoding="utf-8"))
            conn.execute(sql.SQL("ALTER ROLE supa_like PASSWORD {}").format(sql.Literal(password)))
        self.dsn = make_conninfo(self.admin_dsn, dbname=self.name, user="supa_like", password=password)
        if migrate:
            self.migrate()
        return self

    def migrate(self) -> list[str]:
        """Apply every migration with the repository's own runner, as supa_like."""
        import apply_migrations

        with contextlib.redirect_stdout(io.StringIO()):
            return apply_migrations.apply(self.dsn)

    def admin(self) -> "psycopg.Connection":
        """An autocommit superuser connection to the scratch database."""
        return psycopg.connect(make_conninfo(self.admin_dsn, dbname=self.name), autocommit=True)

    def connect(self) -> "psycopg.Connection":
        """An autocommit connection as supa_like, as the app opens one."""
        return connect(self.dsn)

    def stop(self) -> None:
        if not self._started:
            return
        with psycopg.connect(self.admin_dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(self.name)))
            for role in CREATED_ROLES:
                if role in self._roles_before:
                    continue
                with contextlib.suppress(psycopg.Error):
                    admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
        self._started = False


@contextlib.contextmanager
def raw_role(conn: "psycopg.Connection", role: str) -> Iterator["psycopg.Connection"]:
    """SET LOCAL ROLE without the app's name check, to test roles the app refuses."""
    with conn.transaction():
        conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
        yield conn


def count_rows(conn: "psycopg.Connection", table: str) -> int:
    schema, name = table.split(".")
    return int(conn.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(schema, name))).fetchone()[0])


def insert_entry(conn: "psycopg.Connection", schema: str, encounter_id: str) -> None:
    conn.execute(
        sql.SQL("INSERT INTO {} (encounter_id, origin, age, sex) VALUES (%s, 'entered', 70, 'female')").format(
            sql.Identifier(schema, "encounters")
        ),
        (encounter_id,),
    )


class SiteIsolationTests(unittest.TestCase):
    db: SupabaseLikeDatabase

    @classmethod
    def setUpClass(cls) -> None:
        cls.db = SupabaseLikeDatabase(admin_dsn_or_skip())
        try:
            cls.db.start()
            with cls.db.connect() as conn:
                for index, role in enumerate(SITE_ROLES, start=1):
                    with acting_as(conn, role):
                        insert_entry(conn, role, f"{role.replace('site_', '').replace('_', '-')}-e{index}")
        except BaseException:
            cls.db.stop()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.stop()

    def setUp(self) -> None:
        self.conn = self.db.connect()
        self.addCleanup(self.conn.close)

    def assert_denied(self, role: str, statement: Any, params: tuple = (), *, raw: bool = False) -> None:
        scope = raw_role if raw else acting_as
        with self.assertRaises(errors.InsufficientPrivilege):
            with scope(self.conn, role):
                self.conn.execute(statement, params)
        # The failed transaction ended, and the role with it.
        self.assertEqual("supa_like", self.conn.execute("SELECT current_user").fetchone()[0])

    def test_each_site_reads_and_writes_its_own_rows(self) -> None:
        for role in SITE_ROLES:
            with self.subTest(role=role), acting_as(self.conn, role):
                self.assertEqual(1, count_rows(self.conn, f"{role}.encounters"))
                self.assertEqual(0, count_rows(self.conn, f"{role}.guard_audit"))

    def test_a_site_cannot_read_or_write_another_site(self) -> None:
        for reader in SITE_ROLES:
            for owner in SITE_ROLES:
                if reader == owner:
                    continue
                with self.subTest(reader=reader, owner=owner):
                    for table in SITE_TABLES:
                        self.assert_denied(reader, sql.SQL("SELECT * FROM {}").format(sql.Identifier(owner, table)))
                    self.assert_denied(
                        reader,
                        sql.SQL("INSERT INTO {} (encounter_id, origin) VALUES ('x-e9', 'entered')").format(
                            sql.Identifier(owner, "encounters")
                        ),
                    )

    def test_the_coordinator_cannot_read_any_site(self) -> None:
        for owner in SITE_ROLES:
            for table in SITE_TABLES:
                with self.subTest(owner=owner, table=table):
                    self.assert_denied(COORDINATOR, sql.SQL("SELECT * FROM {}").format(sql.Identifier(owner, table)))

    def test_a_site_cannot_read_the_coordinators_records(self) -> None:
        for table in ("coordinator.models", "coordinator.pilot_reports", "coordinator.guard_audit",
                      "coordinator.guard_audit_anchor"):
            for role in SITE_ROLES:
                with self.subTest(role=role, table=table):
                    self.assert_denied(role, f"SELECT * FROM {table}")
        self.assert_denied(
            SITE_ROLES[0], "INSERT INTO coordinator.pilot_reports (pilot_id, report) VALUES ('forged', '{}')"
        )

    def test_the_connecting_role_cannot_read_sites_without_switching(self) -> None:
        """Membership is granted WITH INHERIT FALSE, so supa_like holds no site rights."""
        for owner in SITE_ROLES:
            with self.subTest(owner=owner):
                with self.assertRaises(errors.InsufficientPrivilege), self.conn.transaction():
                    self.conn.execute(sql.SQL("SELECT * FROM {}").format(sql.Identifier(owner, "encounters")))
        with self.assertRaises(errors.InsufficientPrivilege), self.conn.transaction():
            self.conn.execute("SELECT * FROM coordinator.site_contributions")

    def test_the_data_api_roles_cannot_read_anything(self) -> None:
        tables = [f"{role}.{table}" for role in SITE_ROLES for table in SITE_TABLES] + list(COORDINATOR_TABLES)
        for api_role in API_ROLES:
            for table in tables:
                schema, name = table.split(".")
                with self.subTest(api_role=api_role, table=table):
                    self.assert_denied(
                        api_role, sql.SQL("SELECT * FROM {}").format(sql.Identifier(schema, name)), raw=True
                    )

    def test_the_data_api_roles_cannot_read_the_migration_ledger(self) -> None:
        """The emulation grants new public tables to the API roles, as Supabase does."""
        for api_role in API_ROLES:
            with self.subTest(api_role=api_role):
                with raw_role(self.conn, api_role):
                    try:
                        rows = self.conn.execute("SELECT * FROM public.schema_migrations").fetchall()
                    except errors.InsufficientPrivilege:
                        rows = []
                self.assertEqual([], rows)

    def test_row_level_security_is_on_for_every_table(self) -> None:
        rows = self.conn.execute(
            """
            SELECT n.nspname, c.relname, c.relrowsecurity, pg_get_userbyid(c.relowner)
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relkind = 'r' AND n.nspname = ANY(%s)
            ORDER BY 1, 2
            """,
            (list(SITE_ROLES) + ["coordinator"],),
        ).fetchall()
        self.assertEqual(14, len(rows), rows)
        for schema, table, rls, owner in rows:
            with self.subTest(table=f"{schema}.{table}"):
                self.assertTrue(rls)
                self.assertEqual(COORDINATOR if schema == "coordinator" else schema, owner)

    def test_the_v2_tables_are_kept_for_the_api_that_is_still_running(self) -> None:
        """The previous API reads them while the migrations and the seed run; a later migration drops them."""
        rows = self.conn.execute(
            "SELECT tablename, rowsecurity FROM pg_tables WHERE schemaname = 'public' "
            "AND tablename IN ('encounters', 'hospitals') ORDER BY 1"
        ).fetchall()
        self.assertEqual([("encounters", True), ("hospitals", True)], rows)

    def test_site_contributions_are_private_to_each_site(self) -> None:
        uk, italy = SITE_ROLES[0], SITE_ROLES[1]
        for role in (uk, italy):
            with acting_as(self.conn, role):
                self.conn.execute(
                    "INSERT INTO coordinator.site_contributions (round_id, phase, payload) "
                    "VALUES ('round-rls', 'analytics', '{\"n\": 1}')"
                )
        with acting_as(self.conn, uk):
            seen = self.conn.execute(
                "SELECT DISTINCT site_role FROM coordinator.site_contributions WHERE round_id = 'round-rls'"
            ).fetchall()
        self.assertEqual([(uk,)], seen)
        with acting_as(self.conn, COORDINATOR):
            everyone = self.conn.execute(
                "SELECT site_role FROM coordinator.site_contributions WHERE round_id = 'round-rls' ORDER BY 1"
            ).fetchall()
        self.assertEqual(sorted([(uk,), (italy,)]), everyone)
        # A site cannot write a row in another site's name, change rows or delete them.
        self.assert_denied(
            uk,
            "INSERT INTO coordinator.site_contributions (round_id, phase, site_role, payload) "
            "VALUES ('round-rls', 'analytics', %s, '{}')",
            (italy,),
        )
        self.assert_denied(uk, "UPDATE coordinator.site_contributions SET phase = 'x'")
        self.assert_denied(uk, "DELETE FROM coordinator.site_contributions")

    def test_a_forged_site_role_is_refused_by_the_policy_too(self) -> None:
        """The column grant stops this first; the RLS check is the second line."""
        with self.db.admin() as admin:
            admin.execute("GRANT INSERT (site_role) ON coordinator.site_contributions TO site_uk_134")
        try:
            with self.assertRaises(errors.InsufficientPrivilege) as caught:
                with acting_as(self.conn, SITE_ROLES[0]):
                    self.conn.execute(
                        "INSERT INTO coordinator.site_contributions (round_id, phase, site_role, payload) "
                        "VALUES ('round-forged', 'analytics', 'site_it_002', '{}')"
                    )
            self.assertIn("row-level security", str(caught.exception))
        finally:
            with self.db.admin() as admin:
                admin.execute("REVOKE INSERT (site_role) ON coordinator.site_contributions FROM site_uk_134")

    def test_check_constraints_refuse_values_outside_the_schema(self) -> None:
        role = SITE_ROLES[0]
        table = sql.Identifier(role, "encounters")
        bad_rows = {
            "systolic_bp out of range": ("uk-134-e90", "entered", None, {"systolic_bp": 500}),
            "unknown sex": ("uk-134-e91", "entered", None, {"sex": "x"}),
            "band does not match age": ("uk-134-e92", "entered", None, {"age": 70, "age_band": "50-59"}),
            "entered row with a source row": ("uk-134-e93", "entered", 7, {}),
            "dataset row without a source row": ("uk-134-00094", "dataset", None, {}),
            "dataset row with an entry id": ("uk-134-e95", "dataset", 95, {}),
        }
        for label, (encounter_id, origin, source_row, fields) in bad_rows.items():
            with self.subTest(label):
                columns = ["encounter_id", "origin", "source_row", *fields]
                values = [encounter_id, origin, source_row, *fields.values()]
                with self.assertRaises(errors.CheckViolation):
                    with acting_as(self.conn, role):
                        self.conn.execute(
                            sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                                table,
                                sql.SQL(", ").join(map(sql.Identifier, columns)),
                                sql.SQL(", ").join(sql.Placeholder() * len(values)),
                            ),
                            values,
                        )

    def test_the_migration_applies_twice(self) -> None:
        """Run the file again directly; the runner's ledger would skip it."""
        with self.conn.transaction():
            self.conn.execute(MIGRATION_0002.read_text(encoding="utf-8"))
        for role in SITE_ROLES:
            with acting_as(self.conn, role):
                self.assertEqual(1, count_rows(self.conn, f"{role}.encounters"))
        self.test_the_connecting_role_cannot_read_sites_without_switching()

    def test_the_coordinator_migration_applies_twice(self) -> None:
        anchor = "INSERT INTO {} (seq, digest, record_count) VALUES (1, repeat('a', 64), 1)"
        with acting_as(self.conn, COORDINATOR):
            self.conn.execute("INSERT INTO coordinator.pilot_reports (pilot_id, report) VALUES ('kept', '{}')")
            self.conn.execute(sql.SQL(anchor).format(sql.Identifier("coordinator", "guard_audit_anchor")))
        with acting_as(self.conn, SITE_ROLES[0]):
            self.conn.execute(sql.SQL(anchor).format(sql.Identifier(SITE_ROLES[0], "guard_audit_anchor")))
        try:
            with self.conn.transaction():
                self.conn.execute(MIGRATION_0003.read_text(encoding="utf-8"))
            with acting_as(self.conn, COORDINATOR):
                self.assertEqual(1, count_rows(self.conn, "coordinator.pilot_reports"))
                self.assertEqual(1, count_rows(self.conn, "coordinator.guard_audit_anchor"))
            with acting_as(self.conn, SITE_ROLES[0]):
                self.assertEqual(1, count_rows(self.conn, f"{SITE_ROLES[0]}.guard_audit_anchor"))
        finally:
            with acting_as(self.conn, COORDINATOR):
                self.conn.execute("DELETE FROM coordinator.pilot_reports WHERE pilot_id = 'kept'")
                self.conn.execute("DELETE FROM coordinator.guard_audit_anchor")
            with acting_as(self.conn, SITE_ROLES[0]):
                self.conn.execute(sql.SQL("DELETE FROM {}").format(sql.Identifier(SITE_ROLES[0], "guard_audit_anchor")))

    def test_each_audit_anchor_is_owned_by_its_chains_role(self) -> None:
        rows = self.conn.execute(
            "SELECT n.nspname, pg_get_userbyid(c.relowner), c.relrowsecurity FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.relkind = 'r' AND c.relname = 'guard_audit_anchor' "
            "ORDER BY 1"
        ).fetchall()
        self.assertEqual(
            sorted([(role, role, True) for role in SITE_ROLES] + [("coordinator", COORDINATOR, True)]), rows
        )

    def test_an_audit_anchor_refuses_malformed_rows(self) -> None:
        table = sql.Identifier(SITE_ROLES[1], "guard_audit_anchor")
        for values in (
            "(false, 1, repeat('a', 64), 1)",
            "(true, 0, repeat('a', 64), 0)",
            "(true, 3, 'not a digest', 3)",
            "(true, 3, repeat('a', 64), 4)",
            "(true, 3, repeat('A', 64), 3)",
        ):
            with self.subTest(values=values):
                with self.assertRaises(errors.CheckViolation):
                    with acting_as(self.conn, SITE_ROLES[1]):
                        self.conn.execute(
                            sql.SQL("INSERT INTO {} (id, seq, digest, record_count) VALUES " + values).format(table)
                        )

    def test_an_audit_anchor_holds_one_row(self) -> None:
        table = sql.Identifier(SITE_ROLES[2], "guard_audit_anchor")
        with self.assertRaises(errors.UniqueViolation):
            with acting_as(self.conn, SITE_ROLES[2]):
                for _ in range(2):
                    self.conn.execute(
                        sql.SQL("INSERT INTO {} (seq, digest, record_count) VALUES (1, repeat('b', 64), 1)").format(table)
                    )

    def test_the_coordinator_tables_refuse_malformed_rows(self) -> None:
        for statement in (
            "INSERT INTO coordinator.pilot_reports (pilot_id, report) VALUES ('', '{}')",
            "INSERT INTO coordinator.pilot_reports (pilot_id, report) VALUES ('p', '[]')",
        ):
            with self.subTest(statement=statement):
                with self.assertRaises(errors.CheckViolation):
                    with acting_as(self.conn, COORDINATOR):
                        self.conn.execute(statement)

    def test_the_coordinator_migration_needs_0002(self) -> None:
        with self.db.admin() as admin:
            admin.execute("ALTER SCHEMA coordinator RENAME TO coordinator_moved")
        try:
            with self.assertRaises(errors.RaiseException) as caught, self.conn.transaction():
                self.conn.execute(MIGRATION_0003.read_text(encoding="utf-8"))
            self.assertIn("apply 0002_site_isolation.sql first", str(caught.exception))
        finally:
            with self.db.admin() as admin:
                admin.execute("ALTER SCHEMA coordinator_moved RENAME TO coordinator")

    def test_the_runner_skips_applied_migrations(self) -> None:
        self.assertEqual([], self.db.migrate())

    def test_the_migration_adds_the_release_decision_columns_to_an_earlier_table(self) -> None:
        """An earlier version of 0002 created coordinator.models without them."""
        with self.db.admin() as admin:
            admin.execute("ALTER TABLE coordinator.models DROP COLUMN release_decision, DROP COLUMN decided_at")
        with self.conn.transaction():
            self.conn.execute(MIGRATION_0002.read_text(encoding="utf-8"))
        with self.conn.transaction():
            self.conn.execute(MIGRATION_0002.read_text(encoding="utf-8"))
        with self.db.admin() as admin:
            columns = {
                row[0]
                for row in admin.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'coordinator' AND table_name = 'models'"
                )
            }
            checks = sorted(
                row[0]
                for row in admin.execute(
                    "SELECT conname FROM pg_constraint WHERE conrelid = 'coordinator.models'::regclass "
                    "AND contype = 'c' AND pg_get_constraintdef(oid) LIKE '%release_decision%'"
                )
            )
        self.assertLessEqual({"release_decision", "decided_at"}, columns)
        self.assertEqual(["models_decision_has_time", "models_release_decision_check"], checks)
        with self.assertRaises(errors.CheckViolation):
            with acting_as(self.conn, COORDINATOR):
                self.conn.execute(
                    "INSERT INTO coordinator.models (version, kind, params, digest, status, release_decision) "
                    "VALUES ('v-check', 'logistic', '{}', 'd', 'candidate', '{\"action\": \"release\"}')"
                )

    def test_on_supabase_the_credential_reads_everything_but_site_roles_stay_isolated(self) -> None:
        """Supabase grants pg_read_all_data to its postgres role.

        The connecting role can then read every site without switching role,
        so the isolation applies to code that acts as a site, not to the
        credential. Acting as a site or the coordinator is still denied.
        """
        with self.db.admin() as admin:
            admin.execute("GRANT pg_read_all_data TO supa_like")
        try:
            with self.db.connect() as conn:
                with conn.transaction():
                    self.assertEqual(1, count_rows(conn, "site_uk_134.encounters"))
            self.assert_denied(SITE_ROLES[1], "SELECT * FROM site_uk_134.encounters")
            self.assert_denied(COORDINATOR, "SELECT * FROM site_uk_134.encounters")
        finally:
            with self.db.admin() as admin:
                admin.execute("REVOKE pg_read_all_data FROM supa_like")


#: What the previous API (v2) depends on: the public schema, its tables and the
#: roles that reach them. 0002 and 0003 must leave all of it as it was.
V2_DEPENDENCIES = {
    "public schema": "SELECT pg_get_userbyid(nspowner), nspacl::text FROM pg_namespace WHERE nspname = 'public'",
    "public tables": (
        "SELECT c.relname, pg_get_userbyid(c.relowner), c.relacl::text, c.relrowsecurity, c.relforcerowsecurity "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relname IN ('encounters', 'hospitals') ORDER BY 1"
    ),
    "public columns": (
        "SELECT table_name, column_name, data_type, is_nullable FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name IN ('encounters', 'hospitals') ORDER BY 1, ordinal_position"
    ),
    "public policies": "SELECT tablename, policyname FROM pg_policies WHERE schemaname = 'public' ORDER BY 1, 2",
    "default privileges in public": (
        "SELECT pg_get_userbyid(defaclrole), defaclobjtype, defaclacl::text FROM pg_default_acl "
        "WHERE defaclnamespace = 'public'::regnamespace OR pg_get_userbyid(defaclrole) = 'supa_like' ORDER BY 1, 2"
    ),
    "role attributes": (
        "SELECT rolname, rolsuper, rolinherit, rolcreaterole, rolbypassrls, rolcanlogin FROM pg_roles "
        "WHERE rolname IN ('supa_like', 'anon', 'authenticated', 'service_role') ORDER BY 1"
    ),
    "memberships of the API roles": (
        "SELECT u.rolname, r.rolname FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid "
        "JOIN pg_roles u ON u.oid = m.member WHERE u.rolname IN ('anon', 'authenticated', 'service_role') "
        "ORDER BY 1, 2"
    ),
    "v2 rows": "SELECT (SELECT count(*) FROM public.hospitals), (SELECT count(*) FROM public.encounters)",
}


class RolloutFromV2Tests(unittest.TestCase):
    """0002 and 0003 run on a database that holds v2, while the v2 API still serves it.

    The data workflow applies the migrations and the seed before the merge, so
    the published page keeps its data until the new API is deployed. This
    applies 0001 and a v2 row the way the previous runner did, records what v2
    depends on, applies the rest with the runner, and checks that none of it
    changed and that the login role still reads the v2 rows.
    """

    db: SupabaseLikeDatabase

    @classmethod
    def setUpClass(cls) -> None:
        import apply_migrations

        cls.db = SupabaseLikeDatabase(admin_dsn_or_skip())
        try:
            cls.db.start(migrate=False)
            # The previous runner: a migrations folder that holds only 0001.
            with tempfile.TemporaryDirectory() as folder:
                (Path(folder) / MIGRATION_0001.name).write_text(MIGRATION_0001.read_text(encoding="utf-8"))
                with mock.patch.object(apply_migrations, "MIGRATIONS", Path(folder)):
                    cls.first_run = cls.db.migrate()
            with cls.db.connect() as conn:
                conn.execute(
                    "INSERT INTO public.hospitals (site_id, display_name, seed, stroke_rate) "
                    "VALUES ('riverside-general', 'Riverside General', 7, 0.3)"
                )
                conn.execute(
                    "INSERT INTO public.encounters (encounter_id, site_id, nihss_total, age_band) "
                    "VALUES ('riverside-general-0001', 'riverside-general', 12, '70-79')"
                )
            cls.before = cls.v2_dependencies()
            cls.second_run = cls.db.migrate()
            cls.after = cls.v2_dependencies()
        except BaseException:
            cls.db.stop()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.stop()

    @classmethod
    def v2_dependencies(cls) -> dict[str, list[tuple]]:
        with cls.db.admin() as admin:
            return {name: [tuple(row) for row in admin.execute(query)] for name, query in V2_DEPENDENCIES.items()}

    def test_the_runner_applied_0001_then_the_rest(self) -> None:
        self.assertEqual([MIGRATION_0001.name], self.first_run)
        self.assertEqual([MIGRATION_0002.name, MIGRATION_0003.name], self.second_run)

    def test_nothing_v2_depends_on_changed(self) -> None:
        for name in V2_DEPENDENCIES:
            with self.subTest(name):
                self.assertEqual(self.before[name], self.after[name])
        self.assertEqual([(1, 1)], self.after["v2 rows"])

    def test_the_login_role_still_reads_the_v2_rows(self) -> None:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT encounter_id, nihss_total FROM public.encounters").fetchall()
        self.assertEqual([("riverside-general-0001", 12.0)], rows)

    def test_the_browser_roles_still_cannot_read_the_v2_rows(self) -> None:
        """0001 revokes anon and authenticated. service_role bypasses RLS on Supabase, before and after."""
        with self.db.connect() as conn:
            for role in ("anon", "authenticated"):
                with self.subTest(role=role), raw_role(conn, role):
                    try:
                        rows = conn.execute("SELECT * FROM public.encounters").fetchall()
                    except errors.InsufficientPrivilege:
                        rows = []
                    self.assertEqual([], rows)


#: The condition that picks the PostgreSQL 16 grant in 0002. The PostgreSQL 15
#: tests replace it with ``false``.
GRANT_OPTIONS_CONDITION = "current_setting('server_version_num')::int >= 160000"
#: The role lists in 0002 and 0003, which the PostgreSQL 15 tests rename.
SITES_LITERAL = "array['site_uk_134', 'site_it_002', 'site_ch_245']"
COORDINATOR_LITERAL = "coordinator_role constant text := 'dagents_coordinator'"


def replace_once(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise AssertionError(f"the migration should contain {old!r} exactly once")
    return text.replace(old, new)


class PostgresFifteenGrantTests(unittest.TestCase):
    """The PostgreSQL 15 branch of 0002, run on this server.

    PostgreSQL 15 has no per-grant INHERIT option, so 0002 grants each role
    with a plain GRANT there. These tests choose that branch, run it on the
    server the suite has, and check what it gives up: the connecting role
    inherits the site roles' rights. The site roles stay isolated from each
    other and from the coordinator.

    Roles and their memberships belong to the whole cluster. A plain GRANT on
    PostgreSQL 16 keeps an existing membership's options, so the branch runs
    with role names used by this class only, and drops them afterwards.
    """

    db: SupabaseLikeDatabase
    site_roles: tuple[str, ...]
    coordinator: str

    @classmethod
    def setUpClass(cls) -> None:
        suffix = secrets.token_hex(3)
        cls.site_roles = tuple(f"{role}_{suffix}" for role in SITE_ROLES)
        cls.coordinator = f"{COORDINATOR}_{suffix}"
        cls.db = SupabaseLikeDatabase(admin_dsn_or_skip())
        try:
            cls.db.start(migrate=False)
            with psycopg.connect(cls.db.dsn) as conn:
                for path in sorted(MIGRATION_0002.parent.glob("*.sql")):
                    if path == MIGRATION_0002:
                        conn.execute(cls.as_on_15())
                    elif path == MIGRATION_0003:
                        conn.execute(cls.coordinator_migration())
                    else:
                        conn.execute(path.read_text(encoding="utf-8"))
                conn.commit()
            with cls.db.connect() as conn:
                for index, role in enumerate(cls.site_roles, start=1):
                    with acting_as(conn, role):
                        insert_entry(conn, role, f"pg15-e{index}")
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def as_on_15(cls) -> str:
        """0002 with the PostgreSQL 15 grant chosen and this class's role names."""
        text = MIGRATION_0002.read_text(encoding="utf-8")
        text = replace_once(text, GRANT_OPTIONS_CONDITION, "false")
        text = replace_once(text, SITES_LITERAL, "array[" + ", ".join(f"'{role}'" for role in cls.site_roles) + "]")
        return replace_once(text, COORDINATOR_LITERAL, f"coordinator_role constant text := '{cls.coordinator}'")

    @classmethod
    def coordinator_migration(cls) -> str:
        """0003 with this class's role names."""
        text = MIGRATION_0003.read_text(encoding="utf-8")
        text = replace_once(text, SITES_LITERAL, "array[" + ", ".join(f"'{role}'" for role in cls.site_roles) + "]")
        return replace_once(text, COORDINATOR_LITERAL, f"coordinator_role constant text := '{cls.coordinator}'")

    @classmethod
    def tearDownClass(cls) -> None:
        started = cls.db._started
        cls.db.stop()
        if not started:
            return
        with psycopg.connect(cls.db.admin_dsn, autocommit=True) as admin:
            for role in cls.site_roles + (cls.coordinator,):
                with contextlib.suppress(psycopg.Error):
                    admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))

    def setUp(self) -> None:
        self.conn = self.db.connect()
        self.addCleanup(self.conn.close)

    def test_each_site_reads_its_own_rows(self) -> None:
        for role in self.site_roles:
            with self.subTest(role=role), acting_as(self.conn, role):
                self.assertEqual(1, count_rows(self.conn, f"{role}.encounters"))

    def test_the_site_roles_stay_isolated(self) -> None:
        for reader in self.site_roles + (self.coordinator,):
            for owner in self.site_roles:
                if reader == owner:
                    continue
                with self.subTest(reader=reader, owner=owner):
                    with self.assertRaises(errors.InsufficientPrivilege):
                        with acting_as(self.conn, reader):
                            self.conn.execute(sql.SQL("SELECT * FROM {}").format(sql.Identifier(owner, "encounters")))

    def test_the_connecting_role_inherits_the_site_roles(self) -> None:
        """What 15 gives up: the credential reads every site without switching."""
        with self.conn.transaction():
            for role in self.site_roles:
                self.assertEqual(1, count_rows(self.conn, f"{role}.encounters"))
        with self.db.admin() as admin:
            inheriting = {
                row[0]
                for row in admin.execute(
                    "SELECT r.rolname FROM pg_auth_members m "
                    "JOIN pg_roles r ON r.oid = m.roleid JOIN pg_roles u ON u.oid = m.member "
                    "WHERE u.rolname = 'supa_like' AND m.inherit_option AND m.set_option"
                )
            }
        self.assertLessEqual(set(self.site_roles) | {self.coordinator}, inheriting)

    def test_the_coordinator_owns_its_records_on_15_too(self) -> None:
        with self.db.admin() as admin:
            owners = dict(
                admin.execute(
                    "SELECT c.relname, pg_get_userbyid(c.relowner) FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'coordinator' AND c.relname IN ('pilot_reports', 'guard_audit', "
                    "'guard_audit_anchor')"
                ).fetchall()
            )
            anchors = dict(
                admin.execute(
                    "SELECT n.nspname, pg_get_userbyid(c.relowner) FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE c.relname = 'guard_audit_anchor' AND n.nspname = ANY(%s)",
                    (list(self.site_roles),),
                ).fetchall()
            )
        self.assertEqual(
            {"pilot_reports": self.coordinator, "guard_audit": self.coordinator,
             "guard_audit_anchor": self.coordinator},
            owners,
        )
        self.assertEqual({role: role for role in self.site_roles}, anchors)
        with self.assertRaises(errors.InsufficientPrivilege):
            with acting_as(self.conn, self.site_roles[0]):
                self.conn.execute("SELECT * FROM coordinator.guard_audit")

    def test_the_branch_applies_twice(self) -> None:
        with self.conn.transaction():
            self.conn.execute(self.as_on_15())
        for role in self.site_roles:
            with acting_as(self.conn, role):
                self.assertEqual(1, count_rows(self.conn, f"{role}.encounters"))

if __name__ == "__main__":
    unittest.main()
