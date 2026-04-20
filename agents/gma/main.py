"""FastAPI entrypoint for the Global Monitoring Agent."""

from typing import Any

from fastapi import FastAPI
from pydantic import TypeAdapter

from agents.common.domain.models import SourceSpec
from agents.gma.config import settings
from agents.gma.di import build_aggregation_service
from agents.gma.domain.models import (
    AgentIdentity,
    DatasetProfileRequest,
    DeploymentSyncRequest,
    DesiredDeploymentRequest,
    HeartbeatRequest,
    ModelExecutionRequest,
    RegisterRequest,
    RunDispatchRequest,
    TelemetryEnvelope,
)


service = build_aggregation_service()

app = FastAPI(title=settings.app_name, version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    """Return the raw health payload used by the GMA's internal checks."""
    return service.health_payload()


@app.get("/api/v1/health")
def health_v1() -> dict[str, str]:
    """Versioned alias for the GMA health endpoint."""
    return health()


@app.post("/register")
def register(request: RegisterRequest):
    """Register one LMA with the aggregate control plane."""
    return service.register(request)


@app.put("/api/v1/agents/{agent_id}/registration")
def register_v1(agent_id: str, request: RegisterRequest):
    """Versioned registration endpoint keyed by path-level `agent_id`.

    The path id wins over any mismatched id in the request body so route-level
    identity stays authoritative.
    """
    payload = request if request.agent.agent_id == agent_id else request.model_copy(
        update={"agent": AgentIdentity(**{**request.agent.model_dump(), "agent_id": agent_id})}
    )
    return register(payload)


@app.post("/heartbeat")
def heartbeat(request: HeartbeatRequest):
    """Ingest one heartbeat update from a registered LMA."""
    return service.heartbeat(request)


@app.post("/api/v1/agents/{agent_id}/heartbeats")
def heartbeat_v1(agent_id: str, request: HeartbeatRequest):
    """Versioned heartbeat endpoint keyed by path-level `agent_id`."""
    payload = request if request.agent.agent_id == agent_id else request.model_copy(
        update={"agent": AgentIdentity(**{**request.agent.model_dump(), "agent_id": agent_id})}
    )
    return heartbeat(payload)


@app.post("/telemetry")
def telemetry(request: TelemetryEnvelope):
    """Ingest one telemetry envelope into the aggregate repository."""
    return service.ingest_telemetry(request)


@app.post("/api/v1/agents/{agent_id}/telemetry")
def telemetry_v1(agent_id: str, request: TelemetryEnvelope):
    """Versioned telemetry endpoint keyed by path-level `agent_id`."""
    payload = request if request.agent.agent_id == agent_id else request.model_copy(
        update={"agent": AgentIdentity(**{**request.agent.model_dump(), "agent_id": agent_id})}
    )
    return telemetry(payload)


@app.post("/deployments/sync")
def sync_deployment(request: DeploymentSyncRequest):
    """Compare an LMA's current bundle with the desired deployment plan."""
    return service.sync_deployment(request)


@app.post("/api/v1/agents/{agent_id}/deployment-sync")
def sync_deployment_v1(agent_id: str, request: DeploymentSyncRequest):
    """Versioned deployment-sync endpoint keyed by path-level `agent_id`."""
    payload = request if request.agent.agent_id == agent_id else request.model_copy(
        update={"agent": AgentIdentity(**{**request.agent.model_dump(), "agent_id": agent_id})}
    )
    return sync_deployment(payload)


@app.post("/deployments/plan")
def plan_deployment(request: DesiredDeploymentRequest):
    """Store the desired deployment state for one LMA."""
    return service.plan_deployment(request)


@app.put("/api/v1/agents/{agent_id}/desired-deployment")
def plan_deployment_v1(agent_id: str, request: DesiredDeploymentRequest):
    """Versioned desired-deployment endpoint keyed by path-level `agent_id`."""
    payload = request if request.agent_id == agent_id else request.model_copy(update={"agent_id": agent_id})
    return plan_deployment(payload)


@app.get("/deployments")
def list_deployments():
    """List current desired deployment plans known to the GMA."""
    return service.list_deployments()


@app.post("/runs/dispatch")
def dispatch_run(request: RunDispatchRequest):
    """Record one aggregate run dispatch for downstream execution systems."""
    return service.dispatch_run(request)


@app.get("/runs/dispatch")
def list_dispatched_runs(limit: int = 20):
    """List recent run dispatches issued by the GMA."""
    return service.list_dispatched_runs(limit=limit)


@app.get("/agents")
def list_agents():
    """List agents currently registered with the GMA."""
    return service.list_agents()


@app.get("/api/v1/agents")
def list_agents_v1():
    """Versioned alias for agent listing."""
    return list_agents()


@app.get("/api/v1/agents/{agent_id}")
def get_agent_v1(agent_id: str):
    """Fetch one registered agent snapshot by id."""
    return service.get_agent(agent_id)


@app.post("/datasets/profile")
def profile_assimilated_dataset(request: DatasetProfileRequest):
    """Profile an assimilated dataset before aggregate model selection."""
    return service.profile_assimilated_dataset(request)


@app.post("/api/v1/datasets:profile")
def profile_assimilated_dataset_v1(request: DatasetProfileRequest):
    """Versioned alias for assimilated dataset profiling."""
    return profile_assimilated_dataset(request)


@app.post("/models/run")
def run_assimilated_model(request: ModelExecutionRequest):
    """Execute an aggregate model run synchronously."""
    return service.run_assimilated_model(request)


@app.post("/api/v1/model-jobs", status_code=202)
def run_assimilated_model_v1(request: ModelExecutionRequest):
    """Versioned alias that presents aggregate execution as a model-job API."""
    return run_assimilated_model(request)


@app.get("/models/runs")
def list_model_runs(limit: int = 20):
    """List recent aggregate model runs tracked by the GMA."""
    return service.list_model_runs(limit=limit)


@app.get("/api/v1/model-jobs")
def list_model_runs_v1(limit: int = 20):
    """Versioned alias for aggregate model-job listing."""
    return list_model_runs(limit=limit)


@app.post("/api/v1/sources")
def register_source(payload: dict[str, Any]):
    """Register a reusable source definition for aggregate workflows."""
    source = TypeAdapter(SourceSpec).validate_python(payload)
    return service.register_source(source)


@app.get("/api/v1/sources")
def list_sources():
    """List all sources registered with the GMA."""
    return service.list_sources()


@app.get("/api/v1/sources/{source_id}")
def get_source(source_id: str):
    """Fetch one source registered with the GMA."""
    return service.get_source(source_id)


@app.post("/api/v1/sources/{source_id}:validate")
def validate_source(source_id: str):
    """Run shared source validation for a registered source id."""
    return service.validate_source(source_id)

@app.get("/telemetry/recent")
def recent_telemetry(limit: int = 20):
    """List recent telemetry envelopes received from registered agents."""
    return service.recent_telemetry(limit=limit)


@app.get("/telemetry/summary")
def telemetry_summary():
    """Return aggregate telemetry rollups by agent."""
    return service.telemetry_summary()


@app.get("/overview")
def overview():
    """Return a fleet-level summary of agent and deployment state."""
    return service.overview()
