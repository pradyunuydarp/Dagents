"""Unit tests for LMA and GMA services."""

from __future__ import annotations

import time
import unittest

from agents.common.domain.models import (
    AgentIdentity,
    DeployBundleCommand,
    DeploymentSyncRequest,
    DesiredDeploymentRequest,
    HeartbeatRequest,
    RegisterRequest,
    RunDispatchRequest,
    RunRequest,
    TelemetryEnvelope,
    TelemetryPoint,
)
from agents.gma.application.aggregation_service import AggregationService
from agents.gma.infrastructure.persistence import (
    InMemoryAgentRegistryRepository,
    InMemoryControlPlaneRepository,
    InMemoryTelemetryRepository,
)
from agents.lma.adapters.runner import InMemoryMonitoringRunner
from agents.lma.application.monitoring_service import MonitoringService
from agents.lma.infrastructure.messaging import InMemoryTelemetryPublisher
from agents.lma.infrastructure.state import InMemoryBundleRepository, InMemoryRunHistoryRepository


class MonitoringServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = MonitoringService(
            runner=InMemoryMonitoringRunner(),
            publisher=InMemoryTelemetryPublisher(),
            bundles=InMemoryBundleRepository(),
            runs=InMemoryRunHistoryRepository(),
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


class AggregationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = AggregationService(
            registry=InMemoryAgentRegistryRepository(),
            telemetry_repository=InMemoryTelemetryRepository(),
            control_plane=InMemoryControlPlaneRepository(),
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


if __name__ == "__main__":
    unittest.main()
