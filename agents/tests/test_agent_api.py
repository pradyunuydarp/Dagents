"""API integration tests for the LMA and GMA FastAPI surfaces."""

from __future__ import annotations

import time
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient

from agents.gma.main import app as gma_app
from agents.lma.main import app as lma_app


def inline_source_payload(source_id: str) -> dict[str, object]:
    return {
        "source_id": source_id,
        "kind": "inline",
        "selection": {
            "records": [
                {"value": 1.2, "score": 0.1, "label": 0},
                {"value": 2.4, "score": 0.2, "label": 1},
                {"value": 2.8, "score": 0.3, "label": 1},
                {"value": 1.0, "score": 0.0, "label": 0},
            ]
        },
        "format": "rows",
        "schema_hint": {"value": "float", "score": "float", "label": "integer"},
        "batching": {"batch_size": 2, "max_records": 4},
        "checkpoint": {},
        "options": {},
    }


class AgentApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lma = TestClient(lma_app)
        self.gma = TestClient(gma_app)
        self.agent_id = f"lma-{uuid4().hex[:8]}"
        self.workspace_id = f"workspace-{uuid4().hex[:8]}"
        self.source_id = f"inline-source-{uuid4().hex[:8]}"
        self.registration = {
            "agent": {
                "agent_id": self.agent_id,
                "workspace_id": self.workspace_id,
                "name": "local-monitor",
                "agent_type": "LMA",
            },
            "scope": {"tenant": self.workspace_id},
            "version": "0.1.0",
            "capabilities": ["monitoring", "profiling", "classification"],
        }
        self.bundle = {
            "agent": self.registration["agent"],
            "bundle_id": f"bundle-{uuid4().hex[:6]}",
            "bundle_version": "1.0.0",
            "bundle_uri": "s3://dagents/test/bundle",
        }
        self.source = inline_source_payload(self.source_id)

    def test_lma_bundle_source_profile_and_model_endpoints(self) -> None:
        health = self.lma.get("/api/v1/health")
        self.assertEqual(health.status_code, 200)

        deploy = self.lma.post("/bundles/deploy", json=self.bundle)
        self.assertEqual(deploy.status_code, 200)
        self.assertTrue(deploy.json()["accepted"])

        run = self.lma.post(
            "/run",
            json={
                "correlation_id": f"corr-{uuid4().hex[:6]}",
                "bundle_id": self.bundle["bundle_id"],
                "bundle_version": self.bundle["bundle_version"],
                "scope": {"tenant": self.workspace_id},
                "metadata": {"workspace_id": self.workspace_id},
            },
        )
        self.assertEqual(run.status_code, 200)
        run_payload = run.json()
        self.assertEqual(run_payload["run"]["bundle_id"], self.bundle["bundle_id"])
        self.assertGreaterEqual(run_payload["run"]["metrics_emitted"], 1)

        register_source = self.lma.post("/api/v1/sources", json=self.source)
        self.assertEqual(register_source.status_code, 200)
        self.assertEqual(register_source.json()["source_id"], self.source_id)

        validate_source = self.lma.post(f"/api/v1/sources/{self.source_id}:validate")
        self.assertEqual(validate_source.status_code, 200)
        self.assertTrue(validate_source.json()["valid"])

        profile = self.lma.post(
            "/api/v1/datasets:profile",
            json={
                "scope_id": self.source_id,
                "dataset": {"source_id": self.source_id},
                "feature_fields": ["value", "score"],
                "label_field": "label",
                "batch_size": 2,
            },
        )
        self.assertEqual(profile.status_code, 200)
        profile_payload = profile.json()
        self.assertEqual(profile_payload["record_count"], 4)
        self.assertEqual(profile_payload["partition_count"], 2)

        model = self.lma.post(
            "/api/v1/model-jobs",
            json={
                "scope_id": self.source_id,
                "dataset": {"source_id": self.source_id},
                "feature_fields": ["value", "score"],
                "label_field": "label",
                "task_type": "classification",
                "model_family": "random_forest",
                "hyperparameters": {"batch_size": 2},
                "artifact_prefix": "artifacts/tests",
                "model_version": "api-test",
            },
        )
        self.assertEqual(model.status_code, 202)
        model_payload = model.json()
        self.assertEqual(model_payload["run"]["scope_kind"], "source")
        self.assertEqual(model_payload["run"]["record_count"], 4)

        listed = self.lma.get("/api/v1/model-jobs")
        self.assertEqual(listed.status_code, 200)
        runs = listed.json()
        self.assertTrue(any(item["scope_id"] == self.source_id for item in runs))

    def test_gma_control_plane_and_assimilated_model_endpoints(self) -> None:
        health = self.gma.get("/api/v1/health")
        self.assertEqual(health.status_code, 200)

        registration = self.gma.put(f"/api/v1/agents/{self.agent_id}/registration", json=self.registration)
        self.assertEqual(registration.status_code, 200)
        self.assertTrue(registration.json()["accepted"])

        desired = self.gma.put(
            f"/api/v1/agents/{self.agent_id}/desired-deployment",
            json={
                "agent_id": self.agent_id,
                "bundle_id": self.bundle["bundle_id"],
                "bundle_version": "1.2.0",
                "bundle_uri": self.bundle["bundle_uri"],
                "config": {"tenant": self.workspace_id},
            },
        )
        self.assertEqual(desired.status_code, 200)
        desired_payload = desired.json()
        self.assertEqual(desired_payload["bundle_version"], "1.2.0")

        heartbeat = self.gma.post(
            f"/api/v1/agents/{self.agent_id}/heartbeats",
            json={
                "agent": self.registration["agent"],
                "status": "ACTIVE",
                "timestamp": int(time.time()),
            },
        )
        self.assertEqual(heartbeat.status_code, 200)
        self.assertEqual(heartbeat.json()["desired_state"], "SYNC_REQUIRED")

        sync = self.gma.post(
            f"/api/v1/agents/{self.agent_id}/deployment-sync",
            json={
                "agent": self.registration["agent"],
                "bundle_id": self.bundle["bundle_id"],
                "bundle_version": self.bundle["bundle_version"],
            },
        )
        self.assertEqual(sync.status_code, 200)
        sync_payload = sync.json()
        self.assertFalse(sync_payload["up_to_date"])
        self.assertEqual(sync_payload["desired_bundle_version"], "1.2.0")

        telemetry = self.gma.post(
            f"/api/v1/agents/{self.agent_id}/telemetry",
            json={
                "agent": self.registration["agent"],
                "metrics": [
                    {"key": "local_checks_total", "value": 2, "observed_at": int(time.time())},
                    {"key": "records_scanned_total", "value": 4, "observed_at": int(time.time())},
                ],
                "pointers": {"tenant": self.workspace_id},
            },
        )
        self.assertEqual(telemetry.status_code, 200)
        self.assertTrue(telemetry.json()["ack"])

        agents = self.gma.get("/api/v1/agents")
        self.assertEqual(agents.status_code, 200)
        self.assertTrue(any(item["agent"]["agent_id"] == self.agent_id for item in agents.json()))

        agent = self.gma.get(f"/api/v1/agents/{self.agent_id}")
        self.assertEqual(agent.status_code, 200)
        self.assertEqual(agent.json()["agent"]["workspace_id"], self.workspace_id)

        register_source = self.gma.post("/api/v1/sources", json=self.source)
        self.assertEqual(register_source.status_code, 200)

        profile = self.gma.post(
            "/api/v1/datasets:profile",
            json={
                "scope_id": f"assim-{self.source_id}",
                "scope_kind": "assimilated",
                "dataset": {"source_id": self.source_id},
                "feature_fields": ["value", "score"],
                "label_field": "label",
                "batch_size": 2,
            },
        )
        self.assertEqual(profile.status_code, 200)
        profile_payload = profile.json()
        self.assertEqual(profile_payload["scope_kind"], "assimilated")
        self.assertEqual(profile_payload["record_count"], 4)

        model = self.gma.post(
            "/api/v1/model-jobs",
            json={
                "scope_id": f"assim-{self.source_id}",
                "scope_kind": "assimilated",
                "dataset": {"source_id": self.source_id},
                "feature_fields": ["value", "score"],
                "label_field": "label",
                "task_type": "classification",
                "model_family": "xgboost",
                "hyperparameters": {"batch_size": 2},
                "artifact_prefix": "artifacts/tests",
                "model_version": "api-test",
            },
        )
        self.assertEqual(model.status_code, 202)
        model_payload = model.json()
        self.assertEqual(model_payload["run"]["scope_kind"], "assimilated")
        self.assertEqual(model_payload["run"]["record_count"], 4)

        overview = self.gma.get("/overview")
        self.assertEqual(overview.status_code, 200)
        overview_payload = overview.json()
        self.assertGreaterEqual(overview_payload["total_agents"], 1)
        self.assertGreaterEqual(overview_payload["telemetry_events"], 1)


if __name__ == "__main__":
    unittest.main()
