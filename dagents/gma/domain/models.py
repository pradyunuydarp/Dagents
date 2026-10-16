"""Typed domain models for the Global Monitoring Agent."""

from dagents.common.domain.models import (
    AgentIdentity,
    AgentSnapshot,
    CommandStatus,
    DatasetProfile,
    DatasetProfileRequest,
    DeploymentPlan,
    DeploymentSyncRequest,
    DeploymentSyncResponse,
    DesiredDeploymentRequest,
    DispatchedRun,
    FleetOverview,
    HeartbeatRequest,
    HeartbeatResponse,
    ModelExecutionRequest,
    ModelExecutionResponse,
    ModelRunRecord,
    RegisterRequest,
    RegisterResponse,
    RunDispatchRequest,
    TelemetryAck,
    TelemetryEnvelope,
    TelemetryPoint,
    TelemetrySummary,
)
from pydantic import ConfigDict, Field

from dagents.common.domain.base import DagentsModel


class DiscardRequest(DagentsModel):
    """Why a round is given up. The GMA stores the reason on the round."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(
        min_length=1,
        max_length=500,
        pattern=r"\S",
        description="Why the round is given up, in words. 1 to 500 characters, not only spaces. The first reason given is kept.",
    )


__all__ = [
    "AgentIdentity",
    "AgentSnapshot",
    "CommandStatus",
    "DatasetProfile",
    "DatasetProfileRequest",
    "DeploymentPlan",
    "DeploymentSyncRequest",
    "DeploymentSyncResponse",
    "DesiredDeploymentRequest",
    "DiscardRequest",
    "DispatchedRun",
    "FleetOverview",
    "HeartbeatRequest",
    "HeartbeatResponse",
    "ModelExecutionRequest",
    "ModelExecutionResponse",
    "ModelRunRecord",
    "RegisterRequest",
    "RegisterResponse",
    "RunDispatchRequest",
    "TelemetryAck",
    "TelemetryEnvelope",
    "TelemetryPoint",
    "TelemetrySummary",
]
