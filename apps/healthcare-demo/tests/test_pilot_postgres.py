"""The federated pilot against Postgres: one schema and role per hospital.

Two setups, both on a scratch database with the real migrations:

- The demo's deployment: one connection string. Every store operation acts as
  the hospital's role or as ``dagents_coordinator``. Each hospital's LMA reads
  its store and keeps its audit chain and job log as the hospital's role; the
  GMA keeps the model registry and its audit chain as ``dagents_coordinator``.
  The pilot gives the same numbers as with the in-memory stores, and the
  registry, contributions and audit chains end up in the right schemas.
- A deployment where each party has its own login: each hospital's login may
  act only as that hospital, and the coordinator's login only as the
  coordinator. The pilot still runs, while the coordinator's login cannot read
  any hospital's table. This shows that the coordinator path needs no access
  to hospital records.

They need ``HEALTHCARE_DEMO_TEST_DATABASE_URL`` (see ``test_site_isolation.py``)
and the planner.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
import secrets
import sys
import unittest

APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT / "backend"))
sys.path.insert(0, str(APP_ROOT.parents[1]))

from dagents.common.infrastructure.postgres_roles import acting_as, connect  # noqa: E402

from app.domain.dataset import SITES, load_site_records  # noqa: E402
from app.domain.model_kinds import INITIAL_VERSION, SEED_VERSION  # noqa: E402
from app.services.agents import HealthcareDemo  # noqa: E402
from app.services.overview import ApprovalRefused  # noqa: E402
from app.services.site_store import PostgresSiteStore  # noqa: E402
from tests.support import dagentsc_available, gated_extension  # noqa: E402
from tests.test_site_isolation import SupabaseLikeDatabase, admin_dsn_or_skip  # noqa: E402

try:
    from psycopg import errors, sql
    from psycopg.conninfo import make_conninfo
except ModuleNotFoundError:  # pragma: no cover - psycopg is in the app's requirements
    errors = sql = make_conninfo = None

TRAINING_ROUNDS = 2


def _lenient_gates() -> list:
    """One gate the candidate clears, so the planner decides ``release``."""
    from dagents.common.domain.federation import GateComparison, ReleaseGate

    return [ReleaseGate(gate_id="discrimination", metric="auc", comparison=GateComparison(kind="at_least", value=0.5))]


def _gma_audit(demo: HealthcareDemo) -> dict:
    """The GMA's release audit chain, newest first, as its audit route answers."""
    return demo.agents.gma().request("GET", "/api/v1/governance/audit", params={"limit": 50}).body


def _seeded_database() -> SupabaseLikeDatabase:
    db = SupabaseLikeDatabase(admin_dsn_or_skip())
    try:
        db.start()
        for site in SITES:
            PostgresSiteStore(site, db.dsn).seed_dataset(load_site_records(site.site_id))
    except BaseException:
        db.stop()
        raise
    return db


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class PostgresPilotTests(unittest.TestCase):
    db: SupabaseLikeDatabase
    report: dict
    consortium: HealthcareDemo

    @classmethod
    def setUpClass(cls) -> None:
        cls.db = _seeded_database()
        try:
            cls.consortium = HealthcareDemo(database_url=cls.db.dsn, environ={})
            cls.report = cls.consortium.pilot.run_pilot("postgres", training_rounds=TRAINING_ROUNDS)
        except BaseException:
            cls.consortium.close()
            cls.db.stop()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls.consortium.close()
        cls.db.stop()

    def test_the_pilot_gives_the_same_numbers_as_the_in_memory_stores(self) -> None:
        """Both store implementations hold the same records, so every metric matches."""
        demo = HealthcareDemo()
        self.addCleanup(demo.close)
        memory = demo.pilot.run_pilot("memory", training_rounds=TRAINING_ROUNDS)
        self.assertEqual(memory["baseline_metrics"], self.report["baseline_metrics"])
        self.assertEqual(memory["candidate"]["metrics"], self.report["candidate"]["metrics"])
        self.assertEqual(memory["candidate"]["artifact_digest"], self.report["candidate"]["artifact_digest"])
        self.assertEqual(
            [point["loss"] for point in memory["training_curve"]],
            [point["loss"] for point in self.report["training_curve"]],
        )
        self.assertEqual(memory["release"]["gates"]["action"], self.report["release"]["gates"]["action"])

    def test_the_registry_is_stored_in_the_coordinator_schema(self) -> None:
        with self.db.admin() as conn:
            rows = dict(conn.execute("SELECT version, status FROM coordinator.models").fetchall())
        self.assertEqual("released", rows[SEED_VERSION])
        self.assertEqual("superseded", rows[INITIAL_VERSION])
        candidate = self.report["candidate"]
        self.assertEqual(candidate["status"], rows[candidate["candidate_version"]])
        self.assertEqual(2 + TRAINING_ROUNDS, len(rows))

    def test_each_hospital_wrote_its_contributions_as_itself(self) -> None:
        rounds = 3 + TRAINING_ROUNDS
        with self.db.admin() as conn:
            rows = conn.execute(
                "SELECT site_role, count(*) FROM coordinator.site_contributions GROUP BY site_role"
            ).fetchall()
        self.assertEqual({site.role: rounds for site in SITES}, dict(rows))
        for site in SITES:
            # The hospital's LMA reads its job log back from the same table, as the hospital's role.
            self.assertEqual(rounds, len(self.consortium.hospital(site.site_id).lma().job_log(50)))

    def test_each_audit_chain_is_in_its_hospitals_schema(self) -> None:
        rounds = 3 + TRAINING_ROUNDS
        with self.db.admin() as conn:
            for site in SITES:
                count = conn.execute(f"SELECT count(*) FROM {site.schema}.guard_audit").fetchone()[0]
                self.assertEqual(rounds * 3, count, site.site_id)
        for site_id, summary in self.report["site_audits"].items():
            self.assertEqual({"site_id", "chain_intact", "record_count"}, set(summary), site_id)
            self.assertTrue(summary["chain_intact"], site_id)
            self.assertEqual(rounds * 3, summary["record_count"], site_id)
            records = self.consortium.hospital(site_id).audit_trail(limit=500)["records"]
            self.assertEqual(rounds * 3, len(records), site_id)

    def test_the_release_audit_chain_is_in_the_coordinator_schema(self) -> None:
        with self.db.admin() as conn:
            rows = conn.execute("SELECT boundary, audit_id FROM coordinator.guard_audit ORDER BY seq").fetchall()
        self.assertEqual([("before_release", self.report["release"]["guard"]["audit_id"])], [tuple(r) for r in rows])
        self.assertEqual({"chain_intact": True, "record_count": 1}, self.report["coordinator_audit"])
        self.assertEqual("rounds", self.report["release"]["metrics_source"])
        self.assertEqual(SEED_VERSION, self.report["release"]["baseline_version"])

    def test_the_pilot_report_is_stored_in_the_coordinator_schema(self) -> None:
        with self.db.admin() as conn:
            rows = conn.execute("SELECT pilot_id FROM coordinator.pilot_reports").fetchall()
        self.assertEqual([self.report["pilot_id"]], [row[0] for row in rows])

    def test_the_release_decision_is_stored_with_the_candidate(self) -> None:
        candidate = self.report["candidate"]["candidate_version"]
        gates = self.report["release"]["gates"]
        with self.db.admin() as conn:
            rows = conn.execute(
                "SELECT version, release_decision, decided_at FROM coordinator.models "
                "WHERE release_decision IS NOT NULL"
            ).fetchall()
        self.assertEqual([candidate], [row[0] for row in rows], "only the final candidate has a decision")
        _, decision, decided_at = rows[0]
        self.assertEqual(gates["action"], decision["action"])
        self.assertEqual(gates["gate_results"], decision["gate_results"])
        self.assertEqual(self.report["release"]["guard"]["permitted"], decision["guard_permitted"])
        self.assertEqual(SEED_VERSION, decision["baseline_version"])
        self.assertEqual("rounds", decision["metrics_source"])
        self.assertIsNotNone(decided_at)


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class PostgresApprovalAfterRestartTests(unittest.TestCase):
    """An approval reads the release decision from the database, so it works after a restart."""

    def test_a_new_process_approves_a_candidate_the_last_one_tested(self) -> None:
        db = _seeded_database()
        self.addCleanup(db.stop)
        first = HealthcareDemo(database_url=db.dsn, environ={}, extension=gated_extension(_lenient_gates()))
        try:
            report = first.pilot.run_pilot("restart", training_rounds=1)
        finally:
            first.close()
        version = report["candidate"]["candidate_version"]
        self.assertEqual("release", report["release"]["gates"]["action"])

        # A restarted API: new stores, a new GMA, the same database.
        restarted = HealthcareDemo(database_url=db.dsn, environ={})
        self.addCleanup(restarted.close)
        self.assertEqual(SEED_VERSION, restarted.overview.model_in_use()["version"])
        listed = {model["version"]: model for model in restarted.overview.models(50)["models"]}
        self.assertEqual("release", listed[version]["release_decision"]["action"])
        self.assertTrue(listed[version]["release_decision"]["guard_permitted"])
        approved = restarted.overview.approve(version)
        self.assertEqual(SEED_VERSION, approved["previous"])
        self.assertEqual(version, restarted.overview.model_in_use()["version"])
        with db.admin() as conn:
            statuses = dict(conn.execute("SELECT version, status FROM coordinator.models").fetchall())
        self.assertEqual("released", statuses[version])
        self.assertEqual("superseded", statuses[SEED_VERSION])


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class PostgresRestartTests(unittest.TestCase):
    """What a restarted API reads back from Postgres: the last pilot, the release audit chain, the baseline.

    Two pilots run with one gate the candidate clears, so both candidates get a
    ``release`` decision against the seed rule. Each test then builds new app
    services and agents on the same database, as a restarted API would.
    """

    db: SupabaseLikeDatabase
    first: dict
    second: dict

    @classmethod
    def setUpClass(cls) -> None:
        cls.db = _seeded_database()
        try:
            consortium = HealthcareDemo(database_url=cls.db.dsn, environ={}, extension=gated_extension(_lenient_gates()))
            try:
                cls.first = consortium.pilot.run_pilot("restart-a", training_rounds=1)
                cls.second = consortium.pilot.run_pilot("restart-b", training_rounds=1)
            finally:
                consortium.close()
        except BaseException:
            cls.db.stop()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.stop()

    def restarted(self) -> HealthcareDemo:
        consortium = HealthcareDemo(database_url=self.db.dsn, environ={})
        self.addCleanup(consortium.close)
        return consortium

    def test_a_new_process_shows_the_last_pilots_charts(self) -> None:
        from app.services.charts_service import ChartService

        consortium = self.restarted()
        self.assertEqual(self.second["pilot_id"], consortium.overview.last_pilot()["pilot_id"])
        self.assertEqual(self.second["training_curve"], consortium.overview.last_pilot()["training_curve"])
        charts = {chart["chart_id"]: chart for chart in ChartService().consortium_list(consortium.overview)}
        for chart_id in ("site-auc", "training-curve", "release-gates"):
            self.assertTrue(charts[chart_id]["available"], charts[chart_id]["reason"])

    def test_the_release_audit_chain_survives_a_restart(self) -> None:
        audit = _gma_audit(self.restarted())
        records = audit["records"]
        self.assertEqual(["before_release", "before_release"], [record["boundary"] for record in records])
        self.assertEqual(self.second["release"]["guard"]["audit_id"], records[0]["audit_id"])
        self.assertTrue(audit["chain_intact"])

    def test_a_candidate_checked_against_an_older_model_cannot_be_approved(self) -> None:
        older = self.first["candidate"]["candidate_version"]
        newer = self.second["candidate"]["candidate_version"]
        consortium = self.restarted()
        gma = consortium.agents.gma()
        for version in (older, newer):
            self.assertEqual(SEED_VERSION, gma.get_model_version(version).release_decision.baseline_version)
        consortium.overview.approve(newer)
        with self.assertRaises(ApprovalRefused):
            self.restarted().overview.approve(older)
        self.assertEqual(newer, self.restarted().overview.model_in_use()["version"])


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class SeparateLoginTests(unittest.TestCase):
    """Each party has its own login, as in a real deployment."""

    db: SupabaseLikeDatabase
    logins: dict[str, str]

    @classmethod
    def setUpClass(cls) -> None:
        cls.db = _seeded_database()
        cls.logins = {}
        suffix = secrets.token_hex(3)
        try:
            with cls.db.admin() as conn:
                for role in [site.role for site in SITES] + ["dagents_coordinator"]:
                    login = f"t_{role}_{suffix}"
                    password = secrets.token_hex(12)
                    conn.execute(
                        sql.SQL("CREATE ROLE {} LOGIN NOINHERIT PASSWORD {}").format(
                            sql.Identifier(login), sql.Literal(password)
                        )
                    )
                    conn.execute(
                        sql.SQL("GRANT {} TO {} WITH INHERIT FALSE, SET TRUE").format(
                            sql.Identifier(role), sql.Identifier(login)
                        )
                    )
                    cls.logins[role] = make_conninfo(
                        cls.db.admin_dsn, dbname=cls.db.name, user=login, password=password
                    )
        except BaseException:
            cls._drop_logins()
            cls.db.stop()
            raise

    @classmethod
    def _drop_logins(cls) -> None:
        with connect(cls.db.admin_dsn) as conn:
            for dsn in cls.logins.values():
                user = dict(part.split("=", 1) for part in dsn.split())["user"]
                with contextlib.suppress(Exception):
                    conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(user)))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.stop()
        cls._drop_logins()

    def environ(self) -> dict[str, str]:
        return {
            f"HEALTHCARE_DEMO_SITE_{site.site_id.upper().replace('-', '_')}_DATABASE_URL": self.logins[site.role]
            for site in SITES
        }

    def test_the_pilot_runs_when_the_coordinator_cannot_read_any_hospital(self) -> None:
        consortium = HealthcareDemo(database_url=self.logins["dagents_coordinator"], environ=self.environ())
        self.addCleanup(consortium.close)
        overview = consortium.overview.overview()
        self.assertEqual([524, 292, 198], [hospital["record_count"] for hospital in overview["hospitals"]])
        report = consortium.pilot.run_pilot("logins", training_rounds=1)
        self.assertIsNotNone(report["candidate"])
        rounds = report["rounds"]
        for round_report in [rounds["analytics"], rounds["evaluation"], *rounds["training"]]:
            self.assertEqual({"completed"}, {result["participation"] for result in round_report["results"]})

        with connect(self.logins["dagents_coordinator"]) as conn:
            for site in SITES:
                table = sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(site.schema, "encounters"))
                with self.subTest(site=site.site_id):
                    with self.assertRaises(errors.InsufficientPrivilege):
                        with acting_as(conn, "dagents_coordinator"):
                            conn.execute(table)
                    with self.assertRaises(errors.InsufficientPrivilege):
                        with acting_as(conn, site.role):
                            pass
                    with self.assertRaises(errors.InsufficientPrivilege):
                        conn.execute(table)

    def test_a_hospital_login_cannot_act_as_another_hospital(self) -> None:
        uk, italy = SITES[0], SITES[1]
        with connect(self.logins[uk.role]) as conn:
            with acting_as(conn, uk.role):
                self.assertEqual(524, conn.execute("SELECT count(*) FROM site_uk_134.encounters").fetchone()[0])
            with self.assertRaises(errors.InsufficientPrivilege):
                with acting_as(conn, italy.role):
                    pass
            with self.assertRaises(errors.InsufficientPrivilege):
                with acting_as(conn, "dagents_coordinator"):
                    pass


if __name__ == "__main__":
    unittest.main()
