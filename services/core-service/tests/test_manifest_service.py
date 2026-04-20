"""Unit tests for Kubernetes manifest generation."""

from __future__ import annotations

import unittest

from app.models import (
    WorkloadComponent,
    WorkloadCompileRequest,
    WorkloadEnvironmentVariable,
    WorkloadManifestRequest,
    WorkloadPort,
)
from app.services.manifest_service import ManifestService


class ManifestServiceTests(unittest.TestCase):
    def test_generates_deployment_and_service_yaml(self) -> None:
        service = ManifestService()
        response = service.generate(
            WorkloadManifestRequest(
                namespace="watchdog",
                components=[
                    WorkloadComponent(
                        name="lma",
                        image="ghcr.io/example/lma:latest",
                        replicas=2,
                        ports=[WorkloadPort(name="http", container_port=8010)],
                        env=[WorkloadEnvironmentVariable(name="MODEL_SCOPE", value="source")],
                        args=["--run-mode", "source"],
                    )
                ],
            )
        )

        self.assertEqual(response.namespace, "watchdog")
        self.assertEqual(len(response.manifests), 1)
        self.assertIn("kind: Deployment", response.manifests[0].deployment_yaml)
        self.assertIn("ghcr.io/example/lma:latest", response.combined_yaml)
        self.assertIn("kind: Service", response.combined_yaml)

    def test_compiles_and_stores_workload_plan(self) -> None:
        service = ManifestService()
        plan = service.compile(
            WorkloadCompileRequest(
                namespace="watchdog",
                components=[
                    WorkloadComponent(
                        name="nightly-rollup",
                        image="ghcr.io/example/gma:latest",
                        kind="CronJob",
                        schedule="0 0 * * *",
                    )
                ],
            )
        )

        self.assertTrue(plan.plan_id.startswith("workload-plan-"))
        self.assertIn("kind: CronJob", plan.combined_yaml)
        self.assertIsNotNone(service.get_plan(plan.plan_id))

    def test_generates_component_level_resources(self) -> None:
        service = ManifestService()
        plan = service.compile(
            WorkloadCompileRequest(
                namespace="watchdog",
                include_services=False,
                include_config_maps=False,
                components=[
                    WorkloadComponent(
                        name="scheduler",
                        image="ghcr.io/example/scheduler:latest",
                        kind="CronJob",
                        schedule="*/5 * * * *",
                        generated_resources=["ConfigMap", "ServiceAccount"],
                        service_account_name="scheduler-runner",
                        config_map_data={"mode": "scheduled", "team": "ml"},
                    )
                ],
            )
        )

        self.assertIn("kind: CronJob", plan.combined_yaml)
        self.assertIn("kind: ConfigMap", plan.combined_yaml)
        self.assertIn("kind: ServiceAccount", plan.combined_yaml)
        self.assertIn("serviceAccountName: scheduler-runner", plan.combined_yaml)
        self.assertIn('mode: "scheduled"', plan.combined_yaml)


if __name__ == "__main__":
    unittest.main()
