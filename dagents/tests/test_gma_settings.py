"""The settings that keep a GMA's round state in Postgres, and how the app builds its stores from them.

The settings are ``GMA_ROUND_TABLE``, ``GMA_ENROLMENT_TABLE``,
``GMA_UPDATE_TABLE``, ``GMA_UPDATE_ROLE`` and ``GMA_UPDATE_MAX_AGE_SECONDS``
(``dagents/gma/config.py``). Most tests here need no database: they read the
settings, check each refusal, check that stores handed in ``GmaDeps`` take
precedence, and check that building the app opens no connection.
``PostgresSettingsWiringTests`` builds a GMA from the settings against a
scratch database (``DAGENTS_TEST_DATABASE_URL``) and checks that it really
keeps its rounds, enrolments and updates in Postgres, on one connection.
"""

from __future__ import annotations

from typing import Any
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from dagents.common.application.ethical_guard import InMemoryAuditLog
from dagents.common.application.federation import InMemoryRoundRepository, InMemorySiteRegistry
from dagents.common.infrastructure.postgres_lease import KeptConnection
from dagents.common.infrastructure.postgres_roles import RoleScopeError
from dagents.gma.app_factory import GmaDeps, create_app
from dagents.gma.config import DEFAULT_UPDATE_MAX_AGE_SECONDS, Settings
from dagents.gma.infrastructure.postgres_models import InMemoryModelStore
from dagents.gma.infrastructure.update_store import InMemoryUpdateStore
from dagents.tests.test_federation import SITES, build_manifest, build_registration
from dagents.tests.test_governance import dagentsc_available

#: A connection string whose password and host must never appear in a message.
#: Nothing listens there, and no test here connects to it.
DSN = "postgresql://gma_login:s3cret-pass-word@db.internal.invalid:5432/coordinator"
SECRETS = (DSN, "s3cret-pass-word", "db.internal.invalid")

#: Where the kept connection opens a connection. A test patches it to prove that none is opened.
CONNECT = "dagents.common.infrastructure.postgres_lease.connect"

TOY_EXTENSION = "dagents.tests.fixtures.toy_extension:ToyExtension"


def every_table(**changes: Any) -> Settings:
    """Settings with every coordinator table, the update table and its role set, changed by ``changes``.

    The engine is the HTTP engine, which the update table needs. Nothing
    listens at the LMA's address, and no test here calls it.
    """
    values: dict[str, Any] = {
        "engine": "http",
        "site_endpoints": {"site-a": "http://127.0.0.1:9600"},
        "database_url": DSN,
        "database_role": "coord",
        "model_table": "coord.models",
        "audit_table": "coord.audit",
        "audit_anchor_table": "coord.audit_anchor",
        "round_table": "coord.rounds",
        "enrolment_table": "coord.site_enrolments",
        "update_table": "coord_updates.updates",
        "update_role": "coord_updates",
        "update_max_age_seconds": 3600.0,
    }
    values.update(changes)
    return Settings(**values)


def no_connection(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("building the GMA opened a database connection")


class DurableStateSettingsTests(unittest.TestCase):
    """The five settings are read from the environment, with their defaults."""

    def test_each_setting_is_read_from_its_variable(self) -> None:
        settings = Settings.from_env(
            {
                "GMA_ROUND_TABLE": " coord.rounds ",
                "GMA_ENROLMENT_TABLE": "coord.site_enrolments",
                "GMA_UPDATE_TABLE": "coord_updates.updates",
                "GMA_UPDATE_ROLE": "coord_updates",
                "GMA_UPDATE_MAX_AGE_SECONDS": "3600",
            }
        )
        self.assertEqual("coord.rounds", settings.round_table)
        self.assertEqual("coord.site_enrolments", settings.enrolment_table)
        self.assertEqual("coord_updates.updates", settings.update_table)
        self.assertEqual("coord_updates", settings.update_role)
        self.assertEqual(3600.0, settings.update_max_age_seconds)
        self.assertEqual(3600.0, settings.update_age_limit_seconds)

    def test_without_the_variables_nothing_is_kept_and_the_age_limit_is_a_day(self) -> None:
        self.assertEqual(86400.0, DEFAULT_UPDATE_MAX_AGE_SECONDS)
        for settings in (Settings.from_env({}), Settings(), Settings.from_env({"GMA_UPDATE_MAX_AGE_SECONDS": " "})):
            with self.subTest(settings=settings):
                self.assertIsNone(settings.round_table)
                self.assertIsNone(settings.enrolment_table)
                self.assertIsNone(settings.update_table)
                self.assertIsNone(settings.update_role)
                # Not set: the limit is the default, and nothing refuses it.
                self.assertIsNone(settings.update_max_age_seconds)
                self.assertEqual(86400.0, settings.update_age_limit_seconds)

    def test_the_age_limit_must_be_a_number_of_seconds_above_zero(self) -> None:
        for raw in ("0", "-60", "nan", "inf", "-inf", "a day"):
            with self.subTest(value=raw), self.assertRaises(ValueError) as caught:
                Settings.from_env({"GMA_UPDATE_TABLE": "coord_updates.updates", "GMA_UPDATE_MAX_AGE_SECONDS": raw})
            self.assertIn("GMA_UPDATE_MAX_AGE_SECONDS", str(caught.exception))
        for value in (True, "60", 0, float("nan")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Settings(update_max_age_seconds=value)  # type: ignore[arg-type]
        self.assertEqual(0.5, Settings(update_max_age_seconds=0.5).update_age_limit_seconds)
        self.assertEqual(60.0, Settings(update_max_age_seconds=60).update_age_limit_seconds)

    def test_repr_still_hides_the_connection_string(self) -> None:
        text = repr(every_table())
        for secret in SECRETS:
            self.assertNotIn(secret, text)
        self.assertIn("coord_updates.updates", text)


class DurableStateCheckTests(unittest.TestCase):
    """``create_app`` refuses settings that cannot work, with a message that names the settings and no secret."""

    def refusal(self, settings: Settings, deps: GmaDeps | None = None) -> str:
        with mock.patch(CONNECT, side_effect=no_connection), self.assertRaises(ValueError) as caught:
            create_app(settings, deps)
        message = str(caught.exception)
        for secret in SECRETS:
            self.assertNotIn(secret, message)
        return message

    def test_the_round_table_needs_the_model_and_audit_tables(self) -> None:
        cases = (
            ({"model_table": None}, ["GMA_MODEL_TABLE"], ["GMA_AUDIT_TABLE"]),
            ({"audit_table": None, "audit_anchor_table": None}, ["GMA_AUDIT_TABLE"], ["GMA_MODEL_TABLE"]),
            (
                {"model_table": None, "audit_table": None, "audit_anchor_table": None},
                ["GMA_MODEL_TABLE", "GMA_AUDIT_TABLE"],
                [],
            ),
        )
        for changes, named, not_named in cases:
            with self.subTest(changes=changes):
                message = self.refusal(every_table(**changes))
                self.assertTrue(message.startswith("GMA_ROUND_TABLE needs "), message)
                for name in named:
                    self.assertIn(name, message)
                for name in not_named:
                    self.assertNotIn(name, message)

    def test_a_model_store_and_an_audit_sink_in_gma_deps_stand_for_their_tables(self) -> None:
        deps = GmaDeps(model_store=InMemoryModelStore(), audit=InMemoryAuditLog())
        settings = every_table(model_table=None, audit_table=None, audit_anchor_table=None)
        with mock.patch(CONNECT, side_effect=no_connection):
            app = create_app(settings, deps)
        self.addCleanup(app.state.gma.close)
        self.assertEqual("postgres", app.state.gma.round_store.kind)
        self.assertIs(deps.model_store, app.state.gma.models._store)  # noqa: SLF001 - the wiring under test

    def test_the_update_table_needs_the_round_table(self) -> None:
        message = self.refusal(every_table(round_table=None))
        self.assertTrue(message.startswith("GMA_UPDATE_TABLE needs GMA_ROUND_TABLE"), message)

    def test_the_update_table_needs_a_round_store_that_survives_a_restart(self) -> None:
        # GMA_ROUND_TABLE is set, but the round store in GmaDeps takes its place and keeps nothing.
        message = self.refusal(every_table(), GmaDeps(rounds=InMemoryRoundRepository()))
        self.assertIn("GMA_UPDATE_TABLE", message)
        self.assertIn("survives a restart", message)

    def test_a_round_store_in_gma_deps_that_survives_a_restart_stands_for_the_round_table(self) -> None:
        from dagents.gma.infrastructure.postgres_rounds import PostgresRoundRepository

        lease = KeptConnection(DSN, name="given")
        self.addCleanup(lease.close)
        rounds = PostgresRoundRepository(lease, "coord", "coord.rounds")
        deps = GmaDeps(coordinator_lease=lease, rounds=rounds)
        with mock.patch(CONNECT, side_effect=no_connection):
            app = create_app(every_table(database_url=None, round_table=None), deps)
        self.addCleanup(app.state.gma.close)
        self.assertIs(rounds, app.state.gma.round_store)
        self.assertEqual("postgres", app.state.gma.updates.kind)

    def test_the_update_table_needs_its_own_role(self) -> None:
        message = self.refusal(every_table(update_role=None))
        self.assertTrue(message.startswith("GMA_UPDATE_TABLE needs GMA_UPDATE_ROLE"), message)

    def test_the_update_role_must_differ_from_the_coordinator_role(self) -> None:
        message = self.refusal(every_table(update_role="coord"))
        self.assertTrue(message.startswith("GMA_UPDATE_ROLE must differ from GMA_DATABASE_ROLE"), message)

    def test_the_update_role_needs_the_update_table(self) -> None:
        message = self.refusal(Settings(update_role="coord_updates"))
        self.assertEqual("GMA_UPDATE_ROLE needs GMA_UPDATE_TABLE", message)

    def test_the_age_limit_needs_the_update_table(self) -> None:
        for settings in (
            Settings(update_max_age_seconds=60.0),
            every_table(update_table=None, update_role=None),
            Settings.from_env({"GMA_UPDATE_MAX_AGE_SECONDS": "60"}),
        ):
            with self.subTest(settings=settings):
                message = self.refusal(settings)
                self.assertTrue(message.startswith("GMA_UPDATE_MAX_AGE_SECONDS needs GMA_UPDATE_TABLE"), message)

    def test_an_update_store_in_gma_deps_stands_for_the_update_table_under_the_age_limit(self) -> None:
        updates = InMemoryUpdateStore()
        settings = Settings(engine="http", site_endpoints={"site-a": "http://127.0.0.1:9600"}, update_max_age_seconds=60.0)
        with mock.patch(CONNECT, side_effect=no_connection):
            app = create_app(settings, GmaDeps(updates=updates))
        self.addCleanup(app.state.gma.close)
        self.assertIs(updates, app.state.gma.updates)
        self.assertEqual(60.0, app.state.gma.settings.update_age_limit_seconds)
        self.assertEqual(60.0, app.state.gma.update_age_limit_seconds)

    def test_the_update_table_needs_the_http_engine(self) -> None:
        # The simulator neither reads nor deletes stored updates, so they would stay in the table.
        env = {
            "GMA_DATABASE_URL": DSN,
            "GMA_DATABASE_ROLE": "coord",
            "GMA_MODEL_TABLE": "coord.models",
            "GMA_AUDIT_TABLE": "coord.audit",
            "GMA_ROUND_TABLE": "coord.rounds",
            "GMA_UPDATE_TABLE": "coord_updates.updates",
            "GMA_UPDATE_ROLE": "coord_updates",
        }
        for settings in (every_table(engine="in-process", site_endpoints={}), Settings.from_env(env)):
            with self.subTest(engine=settings.engine):
                message = self.refusal(settings)
                self.assertEqual(
                    "GMA_UPDATE_TABLE needs GMA_ENGINE=http, the engine that reads and deletes stored updates", message
                )

    def test_an_update_store_that_survives_a_restart_needs_the_http_engine(self) -> None:
        class SurvivingUpdateStore(InMemoryUpdateStore):
            survives_restart = True

        message = self.refusal(Settings(), GmaDeps(updates=SurvivingUpdateStore()))
        self.assertTrue(message.startswith("an update store that survives a restart needs GMA_ENGINE=http"), message)
        # A memory store handed in GmaDeps keeps working with the simulator.
        with mock.patch(CONNECT, side_effect=no_connection):
            app = create_app(Settings(), GmaDeps(updates=InMemoryUpdateStore()))
        app.state.gma.close()

    def test_the_age_limit_needs_the_http_engine(self) -> None:
        message = self.refusal(Settings(update_max_age_seconds=60.0), GmaDeps(updates=InMemoryUpdateStore()))
        self.assertEqual(
            "GMA_UPDATE_MAX_AGE_SECONDS needs GMA_ENGINE=http, the engine that reads and deletes stored updates",
            message,
        )

    def test_each_table_needs_a_database(self) -> None:
        cases = (
            (Settings(enrolment_table="coord.site_enrolments", database_role="coord"), ["GMA_ENROLMENT_TABLE"]),
            (
                every_table(database_url=None),
                ["GMA_MODEL_TABLE", "GMA_AUDIT_TABLE", "GMA_ROUND_TABLE", "GMA_ENROLMENT_TABLE", "GMA_UPDATE_TABLE"],
            ),
        )
        for settings, named in cases:
            with self.subTest(named=named):
                message = self.refusal(settings)
                self.assertTrue(message.endswith(" GMA_DATABASE_URL"), message)
                for name in named:
                    self.assertIn(name, message)
        self.assertEqual("GMA_ENROLMENT_TABLE needs GMA_DATABASE_URL", self.refusal(cases[0][0]))

    def test_each_table_needs_the_coordinator_role(self) -> None:
        message = self.refusal(Settings(database_url=DSN, enrolment_table="coord.site_enrolments"))
        self.assertTrue(message.startswith("GMA_ENROLMENT_TABLE needs GMA_DATABASE_ROLE"), message)
        message = self.refusal(every_table(database_role=None))
        for name in ("GMA_ROUND_TABLE", "GMA_ENROLMENT_TABLE", "GMA_UPDATE_TABLE"):
            self.assertIn(name, message)
        self.assertIn("need GMA_DATABASE_ROLE", message)

    def test_a_given_lease_stands_for_the_database_url(self) -> None:
        lease = KeptConnection(DSN, name="given")
        self.addCleanup(lease.close)
        with mock.patch(CONNECT, side_effect=no_connection):
            app = create_app(every_table(database_url=None), GmaDeps(coordinator_lease=lease))
        self.addCleanup(app.state.gma.close)
        self.assertIs(lease, app.state.gma.coordinator_lease)

    def test_a_table_name_that_is_not_schema_table_is_refused(self) -> None:
        for changes in ({"round_table": "Rounds"}, {"enrolment_table": "a.b.c"}, {"update_table": "x;drop"}):
            with self.subTest(changes=changes):
                self.refusal(every_table(**changes))
        with mock.patch(CONNECT, side_effect=no_connection), self.assertRaises(RoleScopeError):
            create_app(every_table(update_role="Not A Role"))


class DurableStateBuildTests(unittest.TestCase):
    """Which store the app builds for each setting, on which connection, and that building opens none."""

    def build(self, settings: Settings, deps: GmaDeps | None = None) -> Any:
        with mock.patch(CONNECT, side_effect=no_connection) as connect:
            app = create_app(settings, deps)
        self.addCleanup(app.state.gma.close)
        self.assertEqual(0, connect.call_count)
        return app.state.gma

    def assert_one_lease(self, state: Any, lease: KeptConnection) -> None:
        # Private attributes: the wiring under test.
        self.assertIs(lease, state.coordinator_lease)
        self.assertIs(lease, state.round_store._lease)  # noqa: SLF001
        self.assertIs(lease, state.enrolments._lease)  # noqa: SLF001
        self.assertIs(lease, state.updates._lease)  # noqa: SLF001
        self.assertIs(lease, state.models._store._lease)  # noqa: SLF001
        self.assertIs(lease, state.governance._audit._connect.__self__)  # noqa: SLF001

    def test_create_app_with_every_table_set_opens_no_connection(self) -> None:
        settings = every_table(engine="http", site_endpoints={"site-a": "http://127.0.0.1:9600"})
        state = self.build(settings)
        self.assertEqual(0, state.coordinator_lease.connections_opened)
        self.assertEqual("postgres", state.round_store.kind)
        self.assertEqual("postgres", state.enrolments.kind)
        self.assertEqual("postgres", state.updates.kind)
        self.assertEqual("postgres", state.models._store.kind)  # noqa: SLF001
        self.assertEqual({"table": "coord.rounds", "role": "coord"}, state.round_store.location)
        self.assertEqual({"table": "coord.site_enrolments", "role": "coord"}, state.enrolments.location)
        self.assertEqual({"table": "coord_updates.updates", "role": "coord_updates"}, state.updates.location)
        self.assert_one_lease(state, state.coordinator_lease)
        # The HTTP engine knows that round ids stay used after a restart.
        self.assertIs(True, state.engine._round_ids_survive_restart)  # noqa: SLF001

    def test_every_store_is_built_on_the_lease_in_gma_deps(self) -> None:
        lease = KeptConnection(DSN, name="given")
        self.addCleanup(lease.close)
        state = self.build(every_table(database_url=None), GmaDeps(coordinator_lease=lease))
        self.assert_one_lease(state, lease)
        self.assertEqual(0, lease.connections_opened)

    def test_closing_the_app_closes_the_lease_it_built_but_not_a_given_one(self) -> None:
        with mock.patch.object(KeptConnection, "close", autospec=True) as close:
            built = self.build(every_table())
            built.close()
        close.assert_called_once_with(built.coordinator_lease)
        lease = KeptConnection(DSN, name="given")
        self.addCleanup(lease.close)
        with mock.patch.object(KeptConnection, "close", autospec=True) as close:
            given = self.build(every_table(database_url=None), GmaDeps(coordinator_lease=lease))
            given.close()
        close.assert_not_called()

    def test_stores_in_gma_deps_take_precedence_over_the_settings(self) -> None:
        deps = GmaDeps(
            model_store=InMemoryModelStore(),
            audit=InMemoryAuditLog(),
            rounds=InMemoryRoundRepository(),
            enrolments=InMemorySiteRegistry(),
            updates=InMemoryUpdateStore(),
        )
        # Every table is set but every store comes in GmaDeps, so no database is needed and none is opened.
        state = self.build(every_table(database_url=None, database_role=None), deps)
        self.assertIs(deps.rounds, state.round_store)
        self.assertIs(deps.enrolments, state.enrolments)
        self.assertIs(deps.updates, state.updates)
        self.assertIs(deps.model_store, state.models._store)  # noqa: SLF001
        self.assertIs(deps.audit, state.governance._audit)  # noqa: SLF001
        self.assertIsNone(state.coordinator_lease)

    def test_a_store_in_gma_deps_replaces_only_its_own_table(self) -> None:
        enrolments = InMemorySiteRegistry()
        state = self.build(every_table(), GmaDeps(enrolments=enrolments))
        self.assertIs(enrolments, state.enrolments)
        self.assertEqual("postgres", state.round_store.kind)
        self.assertEqual("postgres", state.updates.kind)
        self.assertIsNotNone(state.coordinator_lease)

    def test_without_tables_every_store_is_in_memory_and_no_lease_is_made(self) -> None:
        state = self.build(Settings())
        self.assertIsInstance(state.round_store, InMemoryRoundRepository)
        self.assertIsInstance(state.enrolments, InMemorySiteRegistry)
        self.assertIsInstance(state.updates, InMemoryUpdateStore)
        self.assertIsNone(state.coordinator_lease)

    def test_the_round_and_enrolment_tables_need_no_update_table(self) -> None:
        # Nor the HTTP engine: the simulator keeps its rounds and enrolments in the tables too.
        state = self.build(
            every_table(
                engine="in-process", site_endpoints={}, update_table=None, update_role=None, update_max_age_seconds=None
            )
        )
        self.assertEqual("postgres", state.round_store.kind)
        self.assertEqual("postgres", state.enrolments.kind)
        self.assertIsInstance(state.updates, InMemoryUpdateStore)

    @unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
    def test_the_controller_uses_the_stores_the_app_holds(self) -> None:
        rounds, enrolments = InMemoryRoundRepository(), InMemorySiteRegistry()
        app = create_app(Settings(), GmaDeps(rounds=rounds, enrolments=enrolments))
        self.addCleanup(app.state.gma.close)
        self.assertIs(rounds, app.state.gma.round_store)
        with TestClient(app) as client:
            for site_id in SITES:
                registration = build_registration(site_id).model_dump(mode="json")
                self.assertEqual(200, client.put(f"/api/v1/federation/sites/{site_id}", json=registration).status_code)
            answer = client.post("/api/v1/federation/rounds", json=build_manifest().model_dump(mode="json"))
            self.assertEqual(202, answer.status_code, answer.text)
        self.assertEqual(list(SITES), [site.site_id for site in enrolments.list()])
        self.assertIsNotNone(rounds.get("round-0042"))


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class PostgresSettingsWiringTests(unittest.TestCase):
    """A GMA built from the settings keeps its rounds, enrolments and updates in Postgres, on one connection.

    The tables are created in a scratch database from the package's DDL
    constants: the coordinator tables as tenant alpha, the coordinator role,
    and the updates table as tenant beta, the update role. A second GMA built
    from the same settings, as after a restart, reads what the first stored.
    """

    @classmethod
    def setUpClass(cls) -> None:
        from psycopg import sql

        from dagents.gma.infrastructure.postgres_rounds import ENROLMENT_TABLE_DDL, ROUND_TABLE_DDL, UPDATE_TABLE_DDL
        from dagents.tests.test_model_registry import MODELS_TABLE
        from dagents.tests.test_postgres_roles import ScratchDatabase, admin_dsn_or_skip
        from dagents.tests.test_round_store import create_table

        cls.sql = sql
        cls.db = ScratchDatabase(admin_dsn_or_skip())
        cls.role, cls.update_role = cls.db.alpha, cls.db.beta
        try:
            cls.db.start()
            lease = KeptConnection(cls.db.dsn, name="setup")
            try:
                create_table(lease, cls.role, ROUND_TABLE_DDL, f"{cls.role}.rounds")
                create_table(lease, cls.role, ENROLMENT_TABLE_DDL, f"{cls.role}.site_enrolments")
                create_table(lease, cls.update_role, UPDATE_TABLE_DDL, f"{cls.update_role}.updates")
                with lease.acting_as(cls.role) as conn:
                    conn.execute(sql.SQL(MODELS_TABLE).format(table=sql.Identifier(cls.role, "models")))
            finally:
                lease.close()
        except BaseException:
            cls.db.stop()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.stop()

    def setUp(self) -> None:
        sql = self.sql
        lease = KeptConnection(self.db.dsn, name="truncate")
        try:
            for owner, tables in (
                (self.role, ("rounds", "site_enrolments", "models", "audit", "audit_anchor")),
                (self.update_role, ("updates",)),
            ):
                with lease.acting_as(owner) as conn:
                    for table in tables:
                        truncate = sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE")
                        conn.execute(truncate.format(sql.Identifier(owner, table)))
        finally:
            lease.close()

    def settings(self, *, updates: bool = True) -> Settings:
        """The settings, as ``GMA_*`` variables set them: every table, or every table but the updates.

        The update table needs the HTTP engine. Nothing listens at its LMA's
        address, and no test here dispatches through it.
        """
        role = self.role
        environ = {
            "GMA_EXTENSIONS": TOY_EXTENSION,
            "GMA_DATABASE_URL": self.db.dsn,
            "GMA_DATABASE_ROLE": role,
            "GMA_MODEL_TABLE": f"{role}.models",
            "GMA_AUDIT_TABLE": f"{role}.audit",
            "GMA_AUDIT_ANCHOR_TABLE": f"{role}.audit_anchor",
            "GMA_ROUND_TABLE": f"{role}.rounds",
            "GMA_ENROLMENT_TABLE": f"{role}.site_enrolments",
        }
        if updates:
            environ.update(
                {
                    "GMA_ENGINE": "http",
                    "GMA_SITE_ENDPOINTS": "site-a=http://127.0.0.1:9",
                    "GMA_UPDATE_TABLE": f"{self.update_role}.updates",
                    "GMA_UPDATE_ROLE": self.update_role,
                    "GMA_UPDATE_MAX_AGE_SECONDS": "3600",
                }
            )
        return Settings.from_env(environ)

    def gma(self, *, updates: bool = True) -> Any:
        app = create_app(self.settings(updates=updates))
        self.addCleanup(app.state.gma.close)
        return app

    def test_the_round_state_stores_are_the_postgres_ones(self) -> None:
        state = self.gma().state.gma
        self.assertEqual("postgres", state.round_store.kind)
        self.assertEqual("postgres", state.enrolments.kind)
        self.assertEqual("postgres", state.updates.kind)
        self.assertEqual({"table": f"{self.update_role}.updates", "role": self.update_role}, state.updates.location)
        self.assertEqual(0, state.coordinator_lease.connections_opened)

    def test_enrolments_and_rounds_outlive_the_gma_and_every_store_shares_one_connection(self) -> None:
        # The simulator dispatches the round; it keeps its rounds and enrolments in the tables.
        first = self.gma(updates=False)
        with TestClient(first) as client:
            for site_id in SITES:
                registration = build_registration(site_id).model_dump(mode="json")
                answer = client.put(f"/api/v1/federation/sites/{site_id}", json=registration)
                self.assertEqual(200, answer.status_code, answer.text)
            dispatched = client.post("/api/v1/federation/rounds", json=build_manifest().model_dump(mode="json"))
            self.assertEqual(202, dispatched.status_code, dispatched.text)
            self.assertEqual(200, client.get("/api/v1/model-versions").status_code)
            self.assertEqual(200, client.get("/api/v1/governance/audit:summary").status_code)
            # Every store used the one kept connection.
            self.assertEqual(1, first.state.gma.coordinator_lease.connections_opened)
        self.assertNotIn(self.db.dsn, dispatched.text)

        second = self.gma(updates=False)
        with TestClient(second) as client:
            sites = client.get("/api/v1/federation/sites").json()
            self.assertEqual(list(SITES), [site["site_id"] for site in sites])
            rounds = client.get("/api/v1/federation/rounds").json()
            self.assertEqual(["round-0042"], [item["manifest"]["round_id"] for item in rounds])

    def test_the_update_store_shares_the_one_connection_and_its_gate_row_outlives_the_gma(self) -> None:
        first = self.gma()
        with TestClient(first) as client:
            for path in (
                "/api/v1/federation/sites",
                "/api/v1/federation/rounds",
                "/api/v1/model-versions",
                "/api/v1/governance/audit:summary",
            ):
                answer = client.get(path)
                self.assertEqual(200, answer.status_code, answer.text)
                self.assertNotIn(self.db.dsn, answer.text)
            state = first.state.gma
            # The update store works on its own table, as its own role.
            self.assertEqual([], state.updates.pending_rounds())
            self.assertEqual(0, state.updates.discard("round-never-run"))
            self.assertEqual({}, state.updates.pending_ages(0.0))
            # Every store, the recovery at start among them, used the one kept connection.
            self.assertTrue(state.recovered)
            self.assertEqual(1, state.coordinator_lease.connections_opened)

        second = self.gma()
        with TestClient(second):
            # The discard's gate row outlived the first GMA.
            self.assertEqual(0, second.state.gma.updates.discard("round-never-run"))
            self.assertEqual(1, self.gate_rows("round-never-run"))

    def gate_rows(self, round_id: str) -> int:
        """How many discarded gate rows the updates table holds for ``round_id``, read as the update role."""
        sql = self.sql
        lease = KeptConnection(self.db.dsn, name="read")
        try:
            with lease.acting_as(self.update_role) as conn:
                query = sql.SQL("SELECT count(*) AS n FROM {} WHERE round_id = %s AND site_id = '' AND discarded").format(
                    sql.Identifier(self.update_role, "updates")
                )
                return conn.execute(query, (round_id,)).fetchone()["n"]
        finally:
            lease.close()


if __name__ == "__main__":
    unittest.main()
