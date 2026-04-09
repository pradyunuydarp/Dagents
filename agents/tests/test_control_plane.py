"""Unit tests for LMA and GMA services."""

from __future__ import annotations

import time
from tempfile import TemporaryDirectory
import unittest

from agents.common.domain.models import (
    AgentIdentity,
    DatasetProfileRequest,
    DeployBundleCommand,
    DeploymentSyncRequest,
    DesiredDeploymentRequest,
    HeartbeatRequest,
    ModelExecutionRequest,
    RegisterRequest,
    RunDispatchRequest,
    RunRequest,
    TelemetryEnvelope,
    TelemetryPoint,
    ObjectStorageSourceSpec,
    ConnectionRef,
    ObjectStorageSelection,
)
from agents.gma.application.aggregation_service import AggregationService
from agents.common.infrastructure.sources import DefaultSourceResolver
from agents.gma.infrastructure.persistence import (
    InMemoryAgentRegistryRepository,
    InMemoryControlPlaneRepository,
    InMemoryModelRunRepository,
    InMemoryTelemetryRepository,
)
from agents.lma.adapters.runner import InMemoryMonitoringRunner
from agents.lma.application.monitoring_service import MonitoringService
from agents.lma.infrastructure.messaging import InMemoryTelemetryPublisher
from agents.lma.infrastructure.state import (
    InMemoryBundleRepository,
    InMemoryModelRunRepository as InMemoryLocalModelRunRepository,
    InMemoryRunHistoryRepository,
)


class MonitoringServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = MonitoringService(
            runner=InMemoryMonitoringRunner(),
            publisher=InMemoryTelemetryPublisher(),
            bundles=InMemoryBundleRepository(),
            runs=InMemoryRunHistoryRepository(),
            model_runs=InMemoryLocalModelRunRepository(),
        )

    def test_deploys_bundle_and_records_run(self) -> None:
        agent = AgentIdentity(agent_id="lma-1", workspace_id="alpha", name="local-agent")
        deploy = self.service.deploy_bundle(
            command=DeployBundleCommand(
                agent=agent,
                bundle_id="bundle-a",
                bundle_version="1.0.0",
                bundle_uri="s3://bundles/a",
            )
        )
        self.assertTrue(deploy.accepted)

        result = self.service.run(
            RunRequest(
                correlation_id="corr-1",
                bundle_id="bundle-a",
                bundle_version="1.0.0",
                scope={"tenant": "alpha"},
                metadata={"workspace_id": "alpha"},
            )
        )

        self.assertEqual(result.run.bundle_id, "bundle-a")
        self.assertEqual(result.run.metrics_emitted, 2)
        self.assertEqual(len(self.service.list_bundles()), 1)
        self.assertEqual(len(self.service.list_runs()), 1)
        self.assertEqual(len(self.service.recent_telemetry()), 1)

    def test_profiles_and_runs_source_model(self) -> None:
        profile = self.service.profile_source_dataset(
            DatasetProfileRequest(
                scope_id="source-a",
                records=[
                    {"value": 1.2, "score": 0.1, "label": 0},
                    {"value": 2.4, "score": 0.2, "label": 1},
                ],
                feature_fields=["value", "score"],
                label_field="label",
            )
        )
        self.assertEqual(profile.scope_kind, "source")
        self.assertEqual(profile.record_count, 2)

        result = self.service.run_source_model(
            ModelExecutionRequest(
                scope_id="source-a",
                records=[
                    {"value": 1.2, "score": 0.1, "label": 0},
                    {"value": 2.4, "score": 0.2, "label": 1},
                ],
                feature_fields=["value", "score"],
                label_field="label",
                model_family="random_forest",
                task_type="classification",
                hyperparameters={"batch_size": 1},
            )
        )
        self.assertEqual(result.run.agent_role, "LMA")
        self.assertEqual(result.run.scope_kind, "source")
        self.assertEqual(len(self.service.list_model_runs()), 1)

    def test_profiles_source_backed_dataset_from_object_storage_adapter(self) -> None:
        with TemporaryDirectory() as temp_dir:
            dataset_path = f"{temp_dir}/source.json"
            with open(dataset_path, "w", encoding="utf-8") as handle:
                handle.write('[{"value": 1.0, "score": 0.2}, {"value": 2.0, "score": 0.4}]')

            source_resolver = DefaultSourceResolver()
            self.service = MonitoringService(
                runner=InMemoryMonitoringRunner(),
                publisher=InMemoryTelemetryPublisher(),
                bundles=InMemoryBundleRepository(),
                runs=InMemoryRunHistoryRepository(),
                model_runs=InMemoryLocalModelRunRepository(),
                source_resolver=source_resolver,
            )
            source_resolver.register(
                ObjectStorageSourceSpec(
                    source_id="object-source",
                    connection_ref=ConnectionRef(connection_id="local-files"),
                    selection=ObjectStorageSelection(uri=dataset_path),
                    format="json",
                )
            )

            profile = self.service.profile_source_dataset(
                DatasetProfileRequest(
                    scope_id="source-b",
                    dataset={"source_id": "object-source"},
                )
            )
            self.assertEqual(profile.record_count, 2)
            self.assertEqual(profile.partition_count, 1)


class AggregationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = AggregationService(
            registry=InMemoryAgentRegistryRepository(),
            telemetry_repository=InMemoryTelemetryRepository(),
            control_plane=InMemoryControlPlaneRepository(),
            model_runs=InMemoryModelRunRepository(),
        )
        self.agent = AgentIdentity(
            agent_id="lma-1",
            workspace_id="alpha",
            name="local-agent",
        )

    def test_tracks_registration_sync_and_telemetry(self) -> None:
        registration = self.service.register(
            RegisterRequest(
                agent=self.agent,
                scope={"tenant": "alpha"},
                version="0.1.0",
                capabilities=["monitoring", "triage"],
            )
        )
        self.assertTrue(registration.accepted)

        plan = self.service.plan_deployment(
            DesiredDeploymentRequest(
                agent_id=self.agent.agent_id,
                bundle_id="bundle-a",
                bundle_version="1.2.0",
                bundle_uri="s3://bundles/a",
                config={"tenant": "alpha"},
            )
        )
        self.assertEqual(plan.bundle_id, "bundle-a")

        heartbeat = self.service.heartbeat(
            HeartbeatRequest(
                agent=self.agent,
                status="ACTIVE",
                timestamp=int(time.time()),
            )
        )
        self.assertEqual(heartbeat.desired_state, "SYNC_REQUIRED")

        sync = self.service.sync_deployment(
            DeploymentSyncRequest(
                agent=self.agent,
                bundle_id="bundle-a",
                bundle_version="1.0.0",
            )
        )
        self.assertFalse(sync.up_to_date)
        self.assertEqual(sync.desired_bundle_version, "1.2.0")

        ack = self.service.ingest_telemetry(
            TelemetryEnvelope(
                agent=self.agent,
                metrics=[
                    TelemetryPoint(key="local_checks_total", value=2, observed_at=int(time.time())),
                    TelemetryPoint(key="metadata_fields_total", value=1, observed_at=int(time.time())),
                ],
                pointers={"scope": "{'tenant': 'alpha'}"},
            )
        )
        self.assertTrue(ack.ack)

        dispatch = self.service.dispatch_run(
            RunDispatchRequest(
                agent_id=self.agent.agent_id,
                correlation_id="corr-1",
                bundle_id="bundle-a",
                bundle_version="1.2.0",
                scope={"tenant": "alpha"},
            )
        )
        self.assertTrue(dispatch.accepted)

        overview = self.service.overview()
        self.assertEqual(overview.total_agents, 1)
        self.assertEqual(overview.telemetry_events, 1)
        self.assertEqual(overview.pending_deployments, 1)
        self.assertEqual(len(self.service.telemetry_summary()), 1)

    def test_profiles_and_runs_assimilated_model(self) -> None:
        self.service.register(
            RegisterRequest(
                agent=self.agent,
                scope={"tenant": "alpha"},
                version="0.1.0",
                capabilities=["monitoring"],
            )
        )

        profile = self.service.profile_assimilated_dataset(
            DatasetProfileRequest(
                scope_id="tenant-rollup",
                scope_kind="assimilated",
                extraction_strategy="time_series",
                records=[
                    {"timestamp": "2026-01-01T00:00:00Z", "errors": 2, "latency_ms": 130},
                    {"timestamp": "2026-01-01T01:00:00Z", "errors": 4, "latency_ms": 180},
                ],
            )
        )
        self.assertEqual(profile.scope_kind, "assimilated")
        self.assertIn("gru", profile.suggested_models)

        result = self.service.run_assimilated_model(
            ModelExecutionRequest(
                scope_id="tenant-rollup",
                scope_kind="assimilated",
                task_type="forecasting",
                model_family="gru",
                records=[
                    {"timestamp": "2026-01-01T00:00:00Z", "errors": 2, "latency_ms": 130},
                    {"timestamp": "2026-01-01T01:00:00Z", "errors": 4, "latency_ms": 180},
                ],
                hyperparameters={"batch_size": 2},
            )
        )
        self.assertEqual(result.run.agent_role, "GMA")
        self.assertEqual(result.run.scope_kind, "assimilated")
        self.assertEqual(len(self.service.list_model_runs()), 1)


if __name__ == "__main__":
    unittest.main()
