"""Route-level tests for the core-service manifest APIs."""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from app.main import app


class ManifestApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_generates_manifest_bundle_via_api(self) -> None:
        response = self.client.post(
            "/api/v1/manifests/pods",
            json={
                "namespace": "dagents-test",
                "include_services": True,
                "components": [
                    {
                        "name": "lma-api",
                        "image": "ghcr.io/example/lma:latest",
                        "kind": "Deployment",
                        "replicas": 2,
                        "ports": [{"name": "http", "container_port": 8010}],
                        "env": [{"name": "MODEL_SCOPE", "value": "source"}],
                        "args": ["--run-mode", "source"],
                    }
                ],
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["namespace"], "dagents-test")
        self.assertEqual(len(payload["manifests"]), 1)
        self.assertIn("kind: Deployment", payload["combined_yaml"])
        self.assertIn("kind: Service", payload["combined_yaml"])
        self.assertIn("MODEL_SCOPE", payload["combined_yaml"])

    def test_compiles_workload_plan_and_fetches_it_by_plan_id(self) -> None:
        compile_response = self.client.post(
            "/api/v1/workloads:compile",
            json={
                "namespace": "dagents-compile",
                "include_services": True,
                "include_config_maps": True,
                "components": [
                    {
                        "name": "aggregate-rollup",
                        "image": "ghcr.io/example/gma:latest",
                        "kind": "CronJob",
                        "schedule": "0 * * * *",
                        "args": ["--run-mode", "aggregate"],
                    },
                    {
                        "name": "source-batch",
                        "image": "ghcr.io/example/lma:latest",
                        "kind": "Job",
                        "args": ["--trigger", "batch"],
                    },
                ],
            },
        )

        self.assertEqual(compile_response.status_code, 200)
        compiled = compile_response.json()
        self.assertTrue(compiled["plan_id"].startswith("workload-plan-"))
        self.assertEqual(compiled["namespace"], "dagents-compile")
        self.assertEqual(len(compiled["manifests"]), 2)
        self.assertIn("kind: CronJob", compiled["combined_yaml"])
        self.assertIn("kind: Job", compiled["combined_yaml"])
        self.assertIn("kind: ConfigMap", compiled["combined_yaml"])

        lookup = self.client.get(f"/api/v1/workload-plans/{compiled['plan_id']}")
        self.assertEqual(lookup.status_code, 200)
        fetched = lookup.json()
        self.assertEqual(fetched["plan_id"], compiled["plan_id"])
        self.assertEqual(fetched["combined_yaml"], compiled["combined_yaml"])

    def test_compiles_per_component_generated_resources(self) -> None:
        response = self.client.post(
            "/api/v1/workloads:compile",
            json={
                "namespace": "dagents-generated",
                "include_services": False,
                "include_config_maps": False,
                "components": [
                    {
                        "name": "router",
                        "image": "ghcr.io/example/router:latest",
                        "kind": "Deployment",
                        "ports": [{"name": "http", "container_port": 8080}],
                        "generated_resources": ["Service", "ConfigMap", "ServiceAccount"],
                        "service_type": "ClusterIP",
                        "config_map_data": {"router_mode": "edge"},
                    }
                ],
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("kind: Service", payload["combined_yaml"])
        self.assertIn("kind: ConfigMap", payload["combined_yaml"])
        self.assertIn("kind: ServiceAccount", payload["combined_yaml"])
        self.assertIn('router_mode: "edge"', payload["combined_yaml"])
        self.assertIn("kind: ServiceAccount", payload["manifests"][0]["service_account_yaml"])

    def test_generates_a_service_account_only_when_the_component_lists_one(self) -> None:
        # `service_account_name` alone refers to an account that already exists,
        # so the pod is bound to it and no ServiceAccount object is rendered.
        response = self.client.post(
            "/api/v1/workloads:compile",
            json={
                "namespace": "dagents-identity",
                "components": [
                    {
                        "name": "existing-identity",
                        "image": "ghcr.io/example/worker:latest",
                        "service_account_name": "robot",
                    },
                    {
                        "name": "generated-identity",
                        "image": "ghcr.io/example/worker:latest",
                        "generated_resources": ["ServiceAccount"],
                        "service_account_name": "robot-generated",
                    },
                ],
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        existing, generated = payload["manifests"]
        self.assertIn("serviceAccountName: robot\n", existing["deployment_yaml"])
        self.assertIsNone(existing["service_account_yaml"])
        self.assertIn("serviceAccountName: robot-generated", generated["deployment_yaml"])
        self.assertIn("name: robot-generated", generated["service_account_yaml"])
        self.assertEqual(payload["combined_yaml"].count("kind: ServiceAccount"), 1)

    def test_returns_not_found_for_unknown_plan(self) -> None:
        response = self.client.get("/api/v1/workload-plans/workload-plan-missing")
        self.assertEqual(response.status_code, 404)
        self.assertIn("Unknown workload plan", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
