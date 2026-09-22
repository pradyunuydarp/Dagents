"""Tests for the demo backend's HTTP surface."""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from tests.support import dagentsc_available


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from app.main import app

        cls.client = TestClient(app)

    def test_health_states_that_the_data_is_synthetic(self) -> None:
        """The disclaimer is part of the contract, not decoration."""
        payload = self.client.get("/api/v1/health").json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["data"], "synthetic-only")

    def test_legacy_and_versioned_health_agree(self) -> None:
        self.assertEqual(self.client.get("/health").json(), self.client.get("/api/v1/health").json())

    def test_the_extension_endpoint_names_the_boundary(self) -> None:
        """What the framework owns and what the app owns, stated explicitly."""
        payload = self.client.get("/api/v1/extension").json()
        self.assertTrue(payload["boundary"]["framework_owns"])
        self.assertTrue(payload["boundary"]["app_owns"])
        packs = payload["registry"]["condition_packs"]
        self.assertEqual([pack["condition_id"] for pack in packs], ["suspected_stroke"])

    def test_conditions_declare_what_is_not_implemented(self) -> None:
        """One platform, separate applications. Only one is built, and it says so."""
        payload = self.client.get("/api/v1/conditions").json()
        self.assertEqual(len(payload["implemented"]), 1)
        self.assertTrue(payload["planned"])
        self.assertTrue(all(entry["status"] == "not implemented" for entry in payload["planned"]))
        self.assertTrue(payload["implemented"][0]["limitations"])

    def test_assessing_a_record_returns_its_basis(self) -> None:
        response = self.client.post(
            "/api/v1/triage:assess",
            json={
                "record": {
                    "nihss_total": 20,
                    "face_droop": True,
                    "arm_weakness": True,
                    "speech_difficulty": True,
                    "last_known_well_minutes": 30,
                }
            },
        )
        payload = response.json()
        self.assertEqual(payload["priority"], "urgent")
        self.assertTrue(payload["basis"])

    def test_ingesting_a_fhir_bundle_reports_unmapped_codes(self) -> None:
        bundle = {
            "resourceType": "Bundle",
            "entry": [
                {"resource": {"resourceType": "Encounter", "id": "e1", "age": 71}},
                {
                    "resource": {
                        "resourceType": "Observation",
                        "encounter": {"reference": "Encounter/e1"},
                        "code": {"coding": [{"code": "00000-0"}]},
                        "valueQuantity": {"value": 3, "unit": "x"},
                    }
                },
            ],
        }
        payload = self.client.post("/api/v1/ingest:fhir", json={"bundle": bundle}).json()
        self.assertEqual(payload["record_count"], 1)
        self.assertIn("00000-0", payload["unmapped_codes"])

    def test_the_guard_probe_changes_with_the_requester(self) -> None:
        """The decision is a planner lookup, so changing an input changes it."""
        verified = self.client.post("/api/v1/governance:probe", json={"verified": True, "granularity": "row"}).json()
        unverified = self.client.post(
            "/api/v1/governance:probe", json={"verified": False, "granularity": "table"}
        ).json()
        self.assertTrue(verified["permitted"])
        self.assertFalse(unverified["permitted"])
        self.assertGreater(unverified["plan"]["filtering_score"], verified["plan"]["filtering_score"])

    def test_an_unknown_hospital_is_a_404(self) -> None:
        self.assertEqual(self.client.get("/api/v1/hospitals/nowhere/worklist").status_code, 404)

    def test_the_framework_trace_runs_every_planner_step(self) -> None:
        payload = self.client.get("/api/v1/framework/trace?limit=20").json()
        names = [step["name"] for step in payload["trace"]]
        self.assertIn("Feature contract validation", names)
        self.assertIn("Ethical-Restriction Rails (egress)", names)
        self.assertTrue(all(step["status"] == "ok" for step in payload["trace"]), names)

    def test_running_a_pilot_returns_the_full_evidence(self) -> None:
        payload = self.client.post("/api/v1/pilot:run", json={"round_prefix": "api"}).json()
        self.assertIn("analytics", payload["rounds"])
        self.assertIn("candidate_validation", payload["rounds"])
        self.assertIsNotNone(payload["release"])
        self.assertTrue(payload["notice"].startswith("All patient data"))
        for audit in payload["site_audits"].values():
            self.assertTrue(audit["chain_intact"])


if __name__ == "__main__":
    unittest.main()
