"""In-memory persistence for the Global Monitoring Agent."""

from __future__ import annotations

import hashlib
import time
from typing import Protocol

from agents.gma.domain.models import (
    AgentSnapshot,
    DeploymentPlan,
    DeploymentSyncRequest,
    DesiredDeploymentRequest,
    DispatchedRun,
    HeartbeatRequest,
    RegisterRequest,
    RunDispatchRequest,
    TelemetryEnvelope,
    TelemetrySummary,
)


class AgentRegistryRepository(Protocol):
    def upsert(self, request: RegisterRequest) -> AgentSnapshot:
        """Register or update an LMA."""

    def record_heartbeat(self, request: HeartbeatRequest) -> AgentSnapshot:
        """Record an LMA heartbeat."""

    def record_sync(self, request: DeploymentSyncRequest) -> AgentSnapshot:
        """Record the agent's current deployment state."""

    def record_ingestion(self, agent_id: str, ingestion_id: str) -> AgentSnapshot | None:
        """Attach the last telemetry ingestion id to the agent."""

    def assign_desired_bundle(self, plan: DeploymentPlan) -> AgentSnapshot | None:
        """Update the desired bundle for an agent."""

    def get(self, agent_id: str) -> AgentSnapshot | None:
        """Load one agent snapshot."""

    def list(self) -> list[AgentSnapshot]:
        """List registered agents."""


class TelemetryRepository(Protocol):
    def append(self, envelope: TelemetryEnvelope) -> str:
        """Persist one telemetry envelope and return an ingestion id."""

    def list_recent(self, limit: int = 20) -> list[TelemetryEnvelope]:
        """Return the most recent telemetry envelopes."""

    def summarize(self) -> list[TelemetrySummary]:
        """Return telemetry rollups grouped by agent."""


class ControlPlaneRepository(Protocol):
    def upsert_deployment(self, request: DesiredDeploymentRequest) -> DeploymentPlan:
        """Store the desired deployment plan for one agent."""

    def get_deployment(self, agent_id: str) -> DeploymentPlan | None:
        """Return the desired deployment plan for one agent."""

    def list_deployments(self) -> list[DeploymentPlan]:
        """List desired deployment plans."""

    def dispatch_run(self, request: RunDispatchRequest) -> DispatchedRun:
        """Record one dispatched run request."""

    def list_runs(self, limit: int = 20) -> list[DispatchedRun]:
        """List recent run dispatches."""


class InMemoryAgentRegistryRepository:
    """Simple in-memory LMA registry."""

    def __init__(self) -> None:
        self.agents: dict[str, AgentSnapshot] = {}

    def upsert(self, request: RegisterRequest) -> AgentSnapshot:
        current = self.agents.get(request.agent.agent_id)
        snapshot = AgentSnapshot(
            agent=request.agent,
            scope=request.scope,
            version=request.version,
            capabilities=request.capabilities,
            status=current.status if current else "REGISTERED",
            last_seen_at=current.last_seen_at if current else None,
            last_heartbeat_status=current.last_heartbeat_status if current else None,
            last_ingestion_id=current.last_ingestion_id if current else None,
            desired_bundle_id=current.desired_bundle_id if current else None,
            desired_bundle_version=current.desired_bundle_version if current else None,
            current_bundle_id=current.current_bundle_id if current else None,
            current_bundle_version=current.current_bundle_version if current else None,
        )
        self.agents[request.agent.agent_id] = snapshot
        return snapshot

    def record_heartbeat(self, request: HeartbeatRequest) -> AgentSnapshot:
        current = self.agents.get(request.agent.agent_id)
        if current is None:
            current = AgentSnapshot(
                agent=request.agent,
                scope={},
                version="unknown",
                capabilities=[],
            )
        updated = current.model_copy(
            update={
                "status": request.status,
                "last_seen_at": request.timestamp,
                "last_heartbeat_status": request.status,
            }
        )
        self.agents[request.agent.agent_id] = updated
        return updated

    def record_sync(self, request: DeploymentSyncRequest) -> AgentSnapshot:
        current = self.agents.get(request.agent.agent_id)
        if current is None:
            current = AgentSnapshot(
                agent=request.agent,
                scope={},
                version="unknown",
                capabilities=[],
            )
        updated = current.model_copy(
            update={
                "current_bundle_id": request.bundle_id,
                "current_bundle_version": request.bundle_version,
            }
        )
        self.agents[request.agent.agent_id] = updated
        return updated

    def record_ingestion(self, agent_id: str, ingestion_id: str) -> AgentSnapshot | None:
        current = self.agents.get(agent_id)
        if current is None:
            return None
        updated = current.model_copy(update={"last_ingestion_id": ingestion_id})
        self.agents[agent_id] = updated
        return updated

    def assign_desired_bundle(self, plan: DeploymentPlan) -> AgentSnapshot | None:
        current = self.agents.get(plan.agent_id)
        if current is None:
            return None
        updated = current.model_copy(
            update={
                "desired_bundle_id": plan.bundle_id,
                "desired_bundle_version": plan.bundle_version,
            }
        )
        self.agents[plan.agent_id] = updated
        return updated

    def get(self, agent_id: str) -> AgentSnapshot | None:
        return self.agents.get(agent_id)

    def list(self) -> list[AgentSnapshot]:
        return sorted(self.agents.values(), key=lambda agent: agent.agent.agent_id)


class InMemoryTelemetryRepository:
    """Simple in-memory telemetry store."""

    def __init__(self) -> None:
        self.events: list[TelemetryEnvelope] = []

    def append(self, envelope: TelemetryEnvelope) -> str:
        self.events.append(envelope)
        return f"telemetry-{len(self.events)}"

    def list_recent(self, limit: int = 20) -> list[TelemetryEnvelope]:
        return list(reversed(self.events[-limit:]))

    def summarize(self) -> list[TelemetrySummary]:
        grouped: dict[str, TelemetrySummary] = {}
        for envelope in self.events:
            summary = grouped.setdefault(
                envelope.agent.agent_id,
                TelemetrySummary(agent_id=envelope.agent.agent_id, envelopes=0, metric_points=0),
            )
            summary.envelopes += 1
            summary.metric_points += len(envelope.metrics)
            for metric in envelope.metrics:
                summary.metric_totals[metric.key] = summary.metric_totals.get(metric.key, 0.0) + metric.value
                summary.latest_observed_at = max(
                    summary.latest_observed_at or metric.observed_at,
                    metric.observed_at,
                )
        return sorted(grouped.values(), key=lambda item: item.agent_id)


class InMemoryControlPlaneRepository:
    """Tracks desired deployments and dispatched runs."""

    def __init__(self) -> None:
        self._deployments: dict[str, DeploymentPlan] = {}
        self._runs: list[DispatchedRun] = []

    def upsert_deployment(self, request: DesiredDeploymentRequest) -> DeploymentPlan:
        created_at = int(time.time())
        payload = f"{request.agent_id}:{request.bundle_id}:{request.bundle_version}:{request.bundle_uri}:{request.config}"
        config_digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        plan = DeploymentPlan(
            agent_id=request.agent_id,
            bundle_id=request.bundle_id,
            bundle_version=request.bundle_version,
            bundle_uri=request.bundle_uri,
            config=request.config,
            config_digest=config_digest,
            plan_token=f"plan-{config_digest[:12]}",
            created_at=created_at,
        )
        self._deployments[request.agent_id] = plan
        return plan

    def get_deployment(self, agent_id: str) -> DeploymentPlan | None:
        return self._deployments.get(agent_id)

    def list_deployments(self) -> list[DeploymentPlan]:
        return sorted(self._deployments.values(), key=lambda item: item.agent_id)

    def dispatch_run(self, request: RunDispatchRequest) -> DispatchedRun:
        dispatched = DispatchedRun(
            agent_id=request.agent_id,
            correlation_id=request.correlation_id,
            bundle_id=request.bundle_id,
            bundle_version=request.bundle_version,
            scope=request.scope,
            dispatched_at=int(time.time()),
            status="QUEUED",
        )
        self._runs.append(dispatched)
        return dispatched

    def list_runs(self, limit: int = 20) -> list[DispatchedRun]:
        return list(reversed(self._runs[-limit:]))
