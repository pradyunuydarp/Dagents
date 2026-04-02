"""Shared domain models for Dagents control-plane services."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class AgentIdentity(BaseModel):
    agent_id: str
    workspace_id: str
    name: str
    agent_type: str = "LMA"


class RegisterRequest(BaseModel):
    agent: AgentIdentity
    scope: dict[str, str] = Field(default_factory=dict)
    version: str
    capabilities: list[str] = Field(default_factory=list)


class RegisterResponse(BaseModel):
    accepted: bool
    deployment_plan_id: str | None = None
    capabilities: list[str] = Field(default_factory=list)


class HeartbeatRequest(BaseModel):
    agent: AgentIdentity
    status: str
    timestamp: int


class HeartbeatResponse(BaseModel):
    ack: bool
    desired_state: str


class TelemetryPoint(BaseModel):
    key: str
    value: float
    labels: dict[str, str] = Field(default_factory=dict)
    observed_at: int


class TelemetryEnvelope(BaseModel):
    agent: AgentIdentity
    metrics: list[TelemetryPoint] = Field(default_factory=list)
    pointers: dict[str, str] = Field(default_factory=dict)


class TelemetryAck(BaseModel):
    ack: bool
    ingestion_id: str


class DeploymentSyncRequest(BaseModel):
    agent: AgentIdentity
    bundle_id: str
    bundle_version: str


class DeploymentSyncResponse(BaseModel):
    up_to_date: bool
    plan_token: str
    config_digest: str
    desired_bundle_id: str | None = None
    desired_bundle_version: str | None = None
    desired_bundle_uri: str | None = None


class DeployBundleCommand(BaseModel):
    agent: AgentIdentity
    bundle_id: str
    bundle_version: str
    bundle_uri: str = ""
    signature: bytes = b""


class TriggerRunCommand(BaseModel):
    agent: AgentIdentity
    correlation_id: str
    bundle_id: str
    bundle_version: str
    scope: dict[str, str] = Field(default_factory=dict)


class CommandStatus(BaseModel):
    accepted: bool
    message: str


class RunRequest(BaseModel):
    correlation_id: str
    bundle_id: str
    bundle_version: str
    scope: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, str] = Field(default_factory=dict)


class BundleRecord(BaseModel):
    bundle_id: str
    bundle_version: str
    bundle_uri: str = ""
    signature: bytes = b""
    deployed_at: int
    config_digest: str = ""


class RunRecord(BaseModel):
    run_id: str
    correlation_id: str
    bundle_id: str
    bundle_version: str
    scope: dict[str, str] = Field(default_factory=dict)
    status: Literal["COMPLETED", "FAILED"] = "COMPLETED"
    started_at: int
    completed_at: int
    metrics_emitted: int
    pointers: dict[str, str] = Field(default_factory=dict)


class RunExecutionResponse(BaseModel):
    run: RunRecord
    telemetry: TelemetryEnvelope


class DesiredDeploymentRequest(BaseModel):
    agent_id: str
    bundle_id: str
    bundle_version: str
    bundle_uri: str = ""
    config: dict[str, str] = Field(default_factory=dict)


class DeploymentPlan(BaseModel):
    agent_id: str
    bundle_id: str
    bundle_version: str
    bundle_uri: str = ""
    config: dict[str, str] = Field(default_factory=dict)
    plan_token: str
    config_digest: str
    created_at: int


class RunDispatchRequest(BaseModel):
    agent_id: str
    correlation_id: str
    bundle_id: str
    bundle_version: str
    scope: dict[str, str] = Field(default_factory=dict)


class DispatchedRun(BaseModel):
    agent_id: str
    correlation_id: str
    bundle_id: str
    bundle_version: str
    scope: dict[str, str] = Field(default_factory=dict)
    dispatched_at: int
    status: str


class AgentSnapshot(BaseModel):
    agent: AgentIdentity
    scope: dict[str, str] = Field(default_factory=dict)
    version: str
    capabilities: list[str] = Field(default_factory=list)
    status: str = "REGISTERED"
    last_seen_at: int | None = None
    last_heartbeat_status: str | None = None
    last_ingestion_id: str | None = None
    desired_bundle_id: str | None = None
    desired_bundle_version: str | None = None
    current_bundle_id: str | None = None
    current_bundle_version: str | None = None


class TelemetrySummary(BaseModel):
    agent_id: str
    envelopes: int
    metric_points: int
    latest_observed_at: int | None = None
    metric_totals: dict[str, float] = Field(default_factory=dict)


class FleetOverview(BaseModel):
    total_agents: int
    active_agents: int
    telemetry_events: int
    pending_deployments: int
    dispatched_runs: int


class StepOutput(BaseModel):
    step_id: str
    status: str
    details: dict[str, Any] = Field(default_factory=dict)
