"""Tests for the `/api/v1/model-families` endpoint.

A consumer backend should be able to ask what this deployment can run. Before
this endpoint the only way to find out was to send a training request with a
family name and see whether it came back with an error, which is a poor contract
for a framework whose whole pitch is that consumers call it instead of
reimplementing it.
"""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from app.main import app
from app.ml.inventory import ANOMALY_DETECTION, CLASSIFICATION, EMBEDDING


class ModelFamiliesApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)
        response = self.client.get("/api/v1/model-families")
        self.assertEqual(200, response.status_code)
        self.payload = response.json()

    def test_it_reports_the_implemented_families_per_task(self) -> None:
        implemented = self.payload["implemented"]
        self.assertIn("random_forest", implemented[CLASSIFICATION])
        self.assertIn("autoencoder", implemented[ANOMALY_DETECTION])
        self.assertIn("transformer", implemented[EMBEDDING])

    def test_it_names_the_provider_behind_each_family(self) -> None:
        """Which provider serves a family changes what deploying it costs."""
        providers = {
            (capability["family"], capability["task"]): capability["provider"]
            for capability in self.payload["capabilities"]
        }
        self.assertEqual("pytorch", providers[("autoencoder", ANOMALY_DETECTION)])
        self.assertEqual("scikit_learn", providers[("random_forest", CLASSIFICATION)])
        self.assertEqual("huggingface", providers[("transformer", EMBEDDING)])

    def test_it_flags_what_needs_downloading(self) -> None:
        by_key = {
            (capability["family"], capability["task"]): capability
            for capability in self.payload["capabilities"]
        }
        hub_model = by_key[("transformer", EMBEDDING)]
        self.assertTrue(hub_model["requires_download"])
        self.assertTrue(hub_model["default_checkpoint"])
        self.assertTrue(hub_model["extra_requirements"])
        self.assertFalse(by_key[("autoencoder", ANOMALY_DETECTION)]["requires_download"])

    def test_it_reports_the_gaps_rather_than_hiding_them(self) -> None:
        """The planner can route to families this runtime cannot run. Say so."""
        gaps = {(gap["family"], gap["task"]) for gap in self.payload["gaps"]}
        self.assertIn(("xgboost", CLASSIFICATION), gaps)
        for gap in self.payload["gaps"]:
            with self.subTest(family=gap["family"], task=gap["task"]):
                self.assertEqual("planned", gap["status"])
                self.assertTrue(gap["notes"], "a gap has to say what it would take")

    def test_a_planned_family_is_rejected_with_an_actionable_message(self) -> None:
        """The endpoint and the check path must agree about what is runnable."""
        response = self.client.post(
            "/api/v1/checks/classification",
            json={
                "dataset": {
                    "inline_records": [
                        {"f1": 0.0, "label": 0},
                        {"f1": 1.0, "label": 1},
                        {"f1": 0.1, "label": 0},
                        {"f1": 0.9, "label": 1},
                    ]
                },
                "feature_fields": ["f1"],
                "label_field": "label",
                "model_family": "xgboost",
            },
        )
        self.assertEqual(400, response.status_code)
        detail = response.json()["detail"]
        self.assertIn("xgboost", detail)
        self.assertIn("planned", detail)


if __name__ == "__main__":
    unittest.main()
