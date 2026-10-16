"""The HTTP API against Postgres stores: one schema and role per hospital.

A record entered through the API must land in its hospital's schema and in no
other, its outcome must update that row, and a Guard probe at a hospital must
extend that hospital's audit table. The stores reuse one connection each, and
each hospital's LMA and the GMA run on those same connections, so these tests
also show that the role switches back after every request.

They need ``HEALTHCARE_DEMO_TEST_DATABASE_URL`` (see ``test_site_isolation.py``).
The pages that show the model in use need the planner, because the GMA checks
every model version with it.
"""

from __future__ import annotations

import unittest

from tests.api_support import ApiTestCase, main
from tests.support import HIGH_RISK_ENTRY, dagentsc_available
from tests.test_site_isolation import MIGRATION_0001, SupabaseLikeDatabase, admin_dsn_or_skip

ORIGIN = "https://example.github.io"

from app.domain.dataset import SITES, load_site_records
from app.services.agents import HealthcareDemo
from app.services.site_store import PostgresSiteStore


def _entered(db: SupabaseLikeDatabase, schema: str) -> list[tuple]:
    with db.admin() as conn:
        return conn.execute(
            f"SELECT encounter_id, origin, age, age_band, poor_outcome_6m, client_request_id "
            f"FROM {schema}.encounters WHERE origin = 'entered' ORDER BY encounter_id"
        ).fetchall()


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class PostgresApiTests(ApiTestCase):
    db: SupabaseLikeDatabase

    @classmethod
    def make_demo(cls) -> HealthcareDemo:
        cls.db = SupabaseLikeDatabase(admin_dsn_or_skip())
        try:
            cls.db.start()
            for site in SITES:
                store = PostgresSiteStore(site, cls.db.dsn)
                store.seed_dataset(load_site_records(site.site_id))
                store.close()
        except BaseException:
            cls.db.stop()
            raise
        return HealthcareDemo(database_url=cls.db.dsn, environ={})

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            super().tearDownClass()
        finally:
            cls.db.stop()

    def test_the_status_and_overview_report_the_schemas(self) -> None:
        status = self.ok(self.client.get("/api/v1/framework/status"))
        self.assertEqual(("postgres", "schema-per-site"), (status["cohort_source"], status["store_layout"]))
        hospitals = {h["site_id"]: h for h in self.ok(self.client.get("/api/v1/overview"))["hospitals"]}
        for site in SITES:
            expected = {"kind": "postgres", "schema": site.schema, "role": site.role}
            self.assertEqual(expected, hospitals[site.site_id]["store"])
            reported = hospitals[site.site_id]
            self.assertEqual(site.expected_rows, reported["record_count"] - reported["entered_count"])

    def test_an_entry_lands_in_its_hospitals_schema_and_nowhere_else(self) -> None:
        before = {site.schema: _entered(self.db, site.schema) for site in SITES}
        response = self.client.post(
            "/api/v1/hospitals/it-002/encounters", json=HIGH_RISK_ENTRY, headers={"Idempotency-Key": "pg-entry-1"}
        )
        record = self.ok(response, 201)["record"]
        it_rows = _entered(self.db, "site_it_002")
        self.assertEqual(len(before["site_it_002"]) + 1, len(it_rows))
        stored = next(row for row in it_rows if row[0] == record["encounter_id"])
        self.assertEqual(("entered", 93, "90+", None, "pg-entry-1"), stored[1:])
        self.assertEqual(before["site_uk_134"], _entered(self.db, "site_uk_134"))
        self.assertEqual(before["site_ch_245"], _entered(self.db, "site_ch_245"))

        # A retry with the same key adds nothing.
        again = self.client.post(
            "/api/v1/hospitals/it-002/encounters", json=HIGH_RISK_ENTRY, headers={"Idempotency-Key": "pg-entry-1"}
        )
        self.assertEqual(record["encounter_id"], self.ok(again, 201)["record"]["encounter_id"])
        self.assertEqual(it_rows, _entered(self.db, "site_it_002"))

        # The outcome updates the same row.
        path = f"/api/v1/hospitals/it-002/encounters/{record['encounter_id']}/outcome"
        self.ok(self.client.post(path, json={"poor_outcome_6m": False}))
        stored = next(row for row in _entered(self.db, "site_it_002") if row[0] == record["encounter_id"])
        self.assertIs(False, stored[4])

        # The hospital's views and the coordinator's count include it.
        listed = self.ok(self.client.get("/api/v1/hospitals/it-002/encounters?origin=entered&limit=20"))
        self.assertIn(record["encounter_id"], [row["encounter_id"] for row in listed["encounters"]])
        hospitals = {h["site_id"]: h for h in self.ok(self.client.get("/api/v1/overview"))["hospitals"]}
        self.assertEqual(292 + len(it_rows), hospitals["it-002"]["record_count"])

    def test_charts_draw_from_the_hospitals_schema(self) -> None:
        charts = self.ok(self.client.get("/api/v1/hospitals/ch-245/charts"))["charts"]
        self.assertTrue(all(chart["available"] for chart in charts))
        response = self.client.get(charts[0]["svg_path"] + "?theme=dark")
        self.assertEqual(200, response.status_code)
        self.assertTrue(response.headers["content-type"].startswith("image/svg+xml"))

    def test_each_store_kept_one_connection(self) -> None:
        """The hospital's LMA reads the store, and keeps its audit chain and job log, on the store's connection."""
        self.ok(self.client.get("/api/v1/overview"))
        self.ok(self.client.get("/api/v1/hospitals/uk-134"))
        self.ok(self.client.post("/api/v1/governance:probe", json={"site_id": "ch-245", "granularity": "row"}))
        self.ok(self.client.get("/api/v1/models"))
        for site in SITES:
            store = self.demo.hospital(site.site_id).store
            self.assertEqual(1, store.connections_opened, site.site_id)
        self.assertEqual(1, self.demo.coordinator_store.connections_opened)

    def test_a_probe_extends_only_the_chosen_hospitals_audit_table(self) -> None:
        def audit_rows(schema: str) -> int:
            with self.db.admin() as conn:
                return conn.execute(f"SELECT count(*) FROM {schema}.guard_audit").fetchone()[0]

        before = {site.schema: audit_rows(site.schema) for site in SITES}
        payload = self.ok(
            self.client.post(
                "/api/v1/governance:probe",
                json={"site_id": "ch-245", "verified": True, "granularity": "row", "cohort_size": 25},
            )
        )
        self.assertTrue(payload["permitted"])
        # The coordinator's page asks this, so the answer counts the records it would release.
        self.assertNotIn("payload", payload)
        self.assertEqual(5, payload["released_count"])
        after = {site.schema: audit_rows(site.schema) for site in SITES}
        self.assertEqual(before["site_ch_245"] + 1, after["site_ch_245"])
        self.assertEqual(before["site_uk_134"], after["site_uk_134"])
        self.assertEqual(before["site_it_002"], after["site_it_002"])
        audit = self.ok(self.client.get("/api/v1/hospitals/ch-245/audit?limit=1"))
        self.assertTrue(audit["chain_intact"])
        self.assertEqual(payload["audit_id"], audit["records"][0]["audit_id"])


class UnmigratedDatabaseApiTests(ApiTestCase):
    """A database where only 0001 ran, as the real project is before the v3 data workflow runs.

    Every data endpoint answers a JSON 503 that says the database is not set up
    for this version, with the CORS headers and X-Dagents-App, so the page
    shows the message at once instead of retrying.
    """

    db: SupabaseLikeDatabase

    @classmethod
    def make_demo(cls) -> HealthcareDemo:
        cls.db = SupabaseLikeDatabase(admin_dsn_or_skip())
        try:
            cls.db.start(migrate=False)
            with cls.db.connect() as conn:
                conn.execute(MIGRATION_0001.read_text(encoding="utf-8"))
        except BaseException:
            cls.db.stop()
            raise
        return HealthcareDemo(database_url=cls.db.dsn, environ={})

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            super().tearDownClass()
        finally:
            cls.db.stop()

    def assert_not_set_up(self, response) -> None:
        self.assertEqual(503, response.status_code, response.text[:300])
        self.assertEqual({"detail": main.DATABASE_NOT_SET_UP_MESSAGE}, response.json())
        self.assertEqual("healthcare-demo", response.headers.get("x-dagents-app"))
        self.assertEqual("*", response.headers.get("access-control-allow-origin"))
        for secret in ("supa_like", "site_uk_134", "relation", "psycopg", self.db.name):
            self.assertNotIn(secret, response.text)

    def test_data_endpoints_say_the_database_is_not_set_up(self) -> None:
        paths = (
            "/api/v1/overview",
            "/api/v1/hospitals",
            "/api/v1/hospitals/uk-134",
            "/api/v1/hospitals/uk-134/worklist",
            "/api/v1/hospitals/uk-134/audit",
            "/api/v1/hospitals/uk-134/charts",
            "/api/v1/charts",
            "/api/v1/models",
        )
        for path in paths:
            with self.subTest(path=path), self.assertLogs("healthcare_demo", level="ERROR") as logged:
                self.assert_not_set_up(self.client.get(path, headers={"Origin": ORIGIN}))
            self.assertIn("apply the migrations", "\n".join(logged.output))

    def test_an_entry_says_the_database_is_not_set_up(self) -> None:
        with self.assertLogs("healthcare_demo", level="ERROR"):
            self.assert_not_set_up(
                self.client.post("/api/v1/hospitals/uk-134/encounters", json=HIGH_RISK_ENTRY, headers={"Origin": ORIGIN})
            )

    def test_the_status_still_answers(self) -> None:
        response = self.client.get("/api/v1/framework/status", headers={"Origin": ORIGIN})
        status = self.ok(response)
        self.assertEqual("postgres", status["cohort_source"])
        self.assertEqual((False, None), (status["ready"], status["hospitals"]))
        for secret in ("supa_like", "127.0.0.1", self.db.name):
            self.assertNotIn(secret, response.text)


if __name__ == "__main__":
    unittest.main()
