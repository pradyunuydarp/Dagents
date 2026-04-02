"""Application orchestration for the Global Monitoring Agent."""

from __future__ import annotations

from agents.gma.domain.models import (
    AgentSnapshot,
    CommandStatus,
    DeploymentPlan,
    DeploymentSyncRequest,
    DeploymentSyncResponse,
    DesiredDeploymentRequest,
    DispatchedRun,
    FleetOverview,
    HeartbeatRequest,
    HeartbeatResponse,
    RegisterRequest,
    RegisterResponse,
    RunDispatchRequest,
    TelemetryAck,
    TelemetryEnvelope,
    TelemetrySummary,
)
from agents.gma.infrastructure.persistence import AgentRegistryRepository, ControlPlaneRepository, TelemetryRepository


class AggregationService:
    """Track LMAs and accumulate their telemetry."""

    def __init__(
        self,
        registry: AgentRegistryRepository,
        telemetry_repository: TelemetryRepository,
        control_plane: ControlPlaneRepository,
    ) -> None:
        self._registry = registry
        self._telemetry_repository = telemetry_repository
        self._control_plane = control_plane

    def register(self, request: RegisterRequest) -> RegisterResponse:
        self._registry.upsert(request)
        deployment = self._control_plane.get_deployment(request.agent.agent_id)
        return RegisterResponse(
            accepted=True,
            deployment_plan_id=deployment.plan_token if deployment else None,
            capabilities=request.capabilities,
        )

    def heartbeat(self, request: HeartbeatRequest) -> HeartbeatResponse:
        snapshot = self._registry.record_heartbeat(request)
        desired_state = "SYNC_REQUIRED" if self._has_pending_deployment(snapshot) else "ACTIVE"
        return HeartbeatResponse(ack=True, desired_state=desired_state)

    def ingest_telemetry(self, envelope: TelemetryEnvelope) -> TelemetryAck:
        ingestion_id = self._telemetry_repository.append(envelope)
        self._registry.record_ingestion(envelope.agent.agent_id, ingestion_id)
        return TelemetryAck(ack=True, ingestion_id=ingestion_id)

    def sync_deployment(self, request: DeploymentSyncRequest) -> DeploymentSyncResponse:
        snapshot = self._registry.record_sync(request)
        plan = self._control_plane.get_deployment(request.agent.agent_id)
        if plan is None:
            return DeploymentSyncResponse(
                up_to_date=True,
                plan_token="",
                config_digest="",
            )
        if (
            snapshot.current_bundle_id == plan.bundle_id
            and snapshot.current_bundle_version == plan.bundle_version
        ):
            return DeploymentSyncResponse(
                up_to_date=True,
                plan_token=plan.plan_token,
                config_digest=plan.config_digest,
                desired_bundle_id=plan.bundle_id,
                desired_bundle_version=plan.bundle_version,
                desired_bundle_uri=plan.bundle_uri,
            )
        return DeploymentSyncResponse(
            up_to_date=False,
            plan_token=plan.plan_token,
            config_digest=plan.config_digest,
            desired_bundle_id=plan.bundle_id,
            desired_bundle_version=plan.bundle_version,
            desired_bundle_uri=plan.bundle_uri,
        )

    def plan_deployment(self, request: DesiredDeploymentRequest) -> DeploymentPlan:
        plan = self._control_plane.upsert_deployment(request)
        self._registry.assign_desired_bundle(plan)
        return plan

    def dispatch_run(self, request: RunDispatchRequest) -> CommandStatus:
        snapshot = self._registry.get(request.agent_id)
        if snapshot is None:
            return CommandStatus(accepted=False, message=f"Agent {request.agent_id} is not registered")
        dispatched = self._control_plane.dispatch_run(request)
        return CommandStatus(
            accepted=True,
            message=f"Queued run {dispatched.correlation_id} for agent {dispatched.agent_id}",
        )

    def list_agents(self) -> list[AgentSnapshot]:
        return self._registry.list()

    def recent_telemetry(self, limit: int = 20) -> list[TelemetryEnvelope]:
        return self._telemetry_repository.list_recent(limit=limit)

    def telemetry_summary(self) -> list[TelemetrySummary]:
        return self._telemetry_repository.summarize()

    def list_deployments(self) -> list[DeploymentPlan]:
        return self._control_plane.list_deployments()

    def list_dispatched_runs(self, limit: int = 20) -> list[DispatchedRun]:
        return self._control_plane.list_runs(limit=limit)

    def overview(self) -> FleetOverview:
        snapshots = self._registry.list()
        telemetry_events = len(self._telemetry_repository.list_recent(limit=100_000))
        pending_deployments = sum(1 for snapshot in snapshots if self._has_pending_deployment(snapshot))
        active_agents = sum(1 for snapshot in snapshots if snapshot.status in {"ACTIVE", "SYNC_REQUIRED"})
        return FleetOverview(
            total_agents=len(snapshots),
            active_agents=active_agents,
            telemetry_events=telemetry_events,
            pending_deployments=pending_deployments,
            dispatched_runs=len(self._control_plane.list_runs(limit=100_000)),
        )

    @staticmethod
    def health_payload() -> dict[str, str]:
        return {"status": "ok", "agent_type": "GMA"}

    @staticmethod
    def _has_pending_deployment(snapshot: AgentSnapshot) -> bool:
        return bool(
            snapshot.desired_bundle_id
            and (
                snapshot.desired_bundle_id != snapshot.current_bundle_id
                or snapshot.desired_bundle_version != snapshot.current_bundle_version
            )
        )
