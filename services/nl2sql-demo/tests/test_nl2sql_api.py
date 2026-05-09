"""API tests for the Dagents NL2SQL demo backend."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

BACKEND_ROOT = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from app.main import app  # noqa: E402
from app.services.dagents_orchestrator import DagentsOrchestrator  # noqa: E402


class NL2SQLApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_health_samples_and_models(self) -> None:
        self.assertEqual(self.client.get("/api/v1/health").status_code, 200)
        samples = self.client.get("/api/v1/samples")
        self.assertEqual(samples.status_code, 200)
        self.assertGreaterEqual(len(samples.json()), 1)
        models = self.client.get("/api/v1/models")
        self.assertEqual(models.status_code, 200)
        self.assertTrue(any(model["model_id"] == "heuristic_demo" for model in models.json()))

    @patch("app.services.dagents_orchestrator.DagentsOrchestrator.backend_status")
    @patch("app.services.dagents_orchestrator.validate_dataset_source")
    @patch("app.services.dagents_orchestrator.compile_dataset_extraction")
    @patch("app.services.dagents_orchestrator.validate_dataset_schema")
    @patch("app.services.dagents_orchestrator.evaluate_dataset_quality")
    @patch("app.services.dagents_orchestrator.run_dagentsc")
    @patch("app.services.dagents_orchestrator.DagentsOrchestrator._append_service_trace")
    def test_generate_uses_dagents_trace_and_returns_sql(
        self,
        append_service_trace,
        run_dagentsc,
        evaluate_quality,
        validate_schema,
        compile_extraction,
        validate_source,
        backend_status,
    ) -> None:
        append_service_trace.return_value = None
        validate_source.return_value = {"valid": True}
        compile_extraction.return_value = {"partition_strategy": "single"}
        validate_schema.return_value = {"valid": True}
        evaluate_quality.return_value = {"blocking": False}
        run_dagentsc.side_effect = [
            {"pipeline_id": "nl2sql-generation", "steps": []},
            {"selected_model": "transformer"},
        ]
        backend_status.return_value.model_dump.return_value = {"services": []}

        response = self.client.post(
            "/api/v1/generate",
            json={
                "question": "How many critical incidents are open?",
                "model_id": "heuristic_demo",
                "tables": [
                    {
                        "name": "incidents",
                        "columns": [
                            {"name": "incident_id", "dtype": "TEXT"},
                            {"name": "severity", "dtype": "TEXT"},
                            {"name": "status", "dtype": "TEXT"},
                        ],
                    }
                ],
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("SELECT", payload["sql"])
        self.assertTrue(payload["used_fallback"])
        self.assertEqual(payload["fallback_detail"], "The deterministic heuristic adapter was selected.")
        self.assertGreaterEqual(len(payload["dagents_trace"]), 1)

    def test_service_trace_uses_lma_gma_control_plane(self) -> None:
        class StubSettings:
            core_service_url = "http://core-service"
            pipeline_service_url = "http://pipeline-service"
            model_service_url = "http://model-service"
            lma_url = "http://lma"
            gma_url = "http://gma"
            api_port = 8070
            default_model_id = "heuristic_demo"
            request_timeout_seconds = 1.0

        class StubOrchestrator(DagentsOrchestrator):
            def _get_json(self, url):
                return {"url": url}

            def _post_json(self, url, payload):
                return {"url": url, "payload": payload}

            def _put_json(self, url, payload):
                return {"url": url, "payload": payload}

        orchestrator = StubOrchestrator(StubSettings())
        trace = []
        records = [{"table_name": "orders", "column_name": "order_id", "dtype": "INTEGER"}]

        orchestrator._append_service_trace(trace, records, "Show all orders")

        steps = {step.name: step for step in trace}
        required_steps = {
            "core-service catalog and topology",
            "core-service workload compile",
            "pipeline-service register and run",
            "model-service catalog",
            "GMA register NL2SQL LMA",
            "GMA LMA heartbeat",
            "LMA source registration and validation",
            "GMA source registration and validation",
            "LMA source profile",
            "LMA source model job",
            "GMA assimilated profile",
            "GMA aggregate model job",
            "GMA desired deployment and sync",
            "GMA aggregate run dispatch",
            "GMA fleet overview",
        }
        self.assertTrue(required_steps.issubset(steps))
        self.assertTrue(all(step.status == "ok" for step in trace))
        registration = steps["GMA register NL2SQL LMA"].payload
        self.assertEqual(registration["payload"]["agent"]["agent_id"], "nl2sql-lma")
        self.assertIn("model_execution", registration["payload"]["capabilities"])
        lma_model_job = steps["LMA source model job"].payload
        self.assertEqual(lma_model_job["payload"]["scope_kind"], "source")
        gma_model_job = steps["GMA aggregate model job"].payload
        self.assertEqual(gma_model_job["payload"]["scope_kind"], "assimilated")


if __name__ == "__main__":
    unittest.main()
