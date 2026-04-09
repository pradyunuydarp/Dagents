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
    return service.health_payload()


@app.get("/api/v1/health")
def health_v1() -> dict[str, str]:
    return health()


@app.post("/register")
def register(request: RegisterRequest):
    return service.register(request)


@app.put("/api/v1/agents/{agent_id}/registration")
def register_v1(agent_id: str, request: RegisterRequest):
    payload = request if request.agent.agent_id == agent_id else request.model_copy(
        update={"agent": AgentIdentity(**{**request.agent.model_dump(), "agent_id": agent_id})}
    )
    return register(payload)


@app.post("/heartbeat")
def heartbeat(request: HeartbeatRequest):
    return service.heartbeat(request)


@app.post("/api/v1/agents/{agent_id}/heartbeats")
def heartbeat_v1(agent_id: str, request: HeartbeatRequest):
    payload = request if request.agent.agent_id == agent_id else request.model_copy(
        update={"agent": AgentIdentity(**{**request.agent.model_dump(), "agent_id": agent_id})}
    )
    return heartbeat(payload)


@app.post("/telemetry")
def telemetry(request: TelemetryEnvelope):
    return service.ingest_telemetry(request)


@app.post("/api/v1/agents/{agent_id}/telemetry")
def telemetry_v1(agent_id: str, request: TelemetryEnvelope):
    payload = request if request.agent.agent_id == agent_id else request.model_copy(
        update={"agent": AgentIdentity(**{**request.agent.model_dump(), "agent_id": agent_id})}
    )
    return telemetry(payload)


@app.post("/deployments/sync")
def sync_deployment(request: DeploymentSyncRequest):
    return service.sync_deployment(request)


@app.post("/api/v1/agents/{agent_id}/deployment-sync")
def sync_deployment_v1(agent_id: str, request: DeploymentSyncRequest):
    payload = request if request.agent.agent_id == agent_id else request.model_copy(
        update={"agent": AgentIdentity(**{**request.agent.model_dump(), "agent_id": agent_id})}
    )
    return sync_deployment(payload)


@app.post("/deployments/plan")
def plan_deployment(request: DesiredDeploymentRequest):
    return service.plan_deployment(request)


@app.put("/api/v1/agents/{agent_id}/desired-deployment")
def plan_deployment_v1(agent_id: str, request: DesiredDeploymentRequest):
    payload = request if request.agent_id == agent_id else request.model_copy(update={"agent_id": agent_id})
    return plan_deployment(payload)


@app.get("/deployments")
def list_deployments():
    return service.list_deployments()


@app.post("/runs/dispatch")
def dispatch_run(request: RunDispatchRequest):
    return service.dispatch_run(request)


@app.get("/runs/dispatch")
def list_dispatched_runs(limit: int = 20):
    return service.list_dispatched_runs(limit=limit)


@app.get("/agents")
def list_agents():
    return service.list_agents()


@app.get("/api/v1/agents")
def list_agents_v1():
    return list_agents()


@app.get("/api/v1/agents/{agent_id}")
def get_agent_v1(agent_id: str):
    return service.get_agent(agent_id)


@app.post("/datasets/profile")
def profile_assimilated_dataset(request: DatasetProfileRequest):
    return service.profile_assimilated_dataset(request)


@app.post("/api/v1/datasets:profile")
def profile_assimilated_dataset_v1(request: DatasetProfileRequest):
    return profile_assimilated_dataset(request)


@app.post("/models/run")
def run_assimilated_model(request: ModelExecutionRequest):
    return service.run_assimilated_model(request)


@app.post("/api/v1/model-jobs", status_code=202)
def run_assimilated_model_v1(request: ModelExecutionRequest):
    return run_assimilated_model(request)


@app.get("/models/runs")
def list_model_runs(limit: int = 20):
    return service.list_model_runs(limit=limit)


@app.get("/api/v1/model-jobs")
def list_model_runs_v1(limit: int = 20):
    return list_model_runs(limit=limit)


@app.post("/api/v1/sources")
def register_source(payload: dict[str, Any]):
    source = TypeAdapter(SourceSpec).validate_python(payload)
    return service.register_source(source)


@app.get("/api/v1/sources")
def list_sources():
    return service.list_sources()


@app.get("/api/v1/sources/{source_id}")
def get_source(source_id: str):
    return service.get_source(source_id)


@app.post("/api/v1/sources/{source_id}:validate")
def validate_source(source_id: str):
    return service.validate_source(source_id)

@app.get("/telemetry/recent")
def recent_telemetry(limit: int = 20):
    return service.recent_telemetry(limit=limit)


@app.get("/telemetry/summary")
def telemetry_summary():
    return service.telemetry_summary()


@app.get("/overview")
def overview():
    return service.overview()
