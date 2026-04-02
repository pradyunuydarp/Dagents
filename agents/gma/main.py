"""FastAPI entrypoint for the Global Monitoring Agent."""

from fastapi import FastAPI

from agents.gma.config import settings
from agents.gma.di import build_aggregation_service
from agents.gma.domain.models import (
    DeploymentSyncRequest,
    DesiredDeploymentRequest,
    HeartbeatRequest,
    RegisterRequest,
    RunDispatchRequest,
    TelemetryEnvelope,
)


service = build_aggregation_service()

app = FastAPI(title=settings.app_name, version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return service.health_payload()


@app.post("/register")
def register(request: RegisterRequest):
    return service.register(request)


@app.post("/heartbeat")
def heartbeat(request: HeartbeatRequest):
    return service.heartbeat(request)


@app.post("/telemetry")
def telemetry(request: TelemetryEnvelope):
    return service.ingest_telemetry(request)


@app.post("/deployments/sync")
def sync_deployment(request: DeploymentSyncRequest):
    return service.sync_deployment(request)


@app.post("/deployments/plan")
def plan_deployment(request: DesiredDeploymentRequest):
    return service.plan_deployment(request)


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


@app.get("/telemetry/recent")
def recent_telemetry(limit: int = 20):
    return service.recent_telemetry(limit=limit)


@app.get("/telemetry/summary")
def telemetry_summary():
    return service.telemetry_summary()


@app.get("/overview")
def overview():
    return service.overview()
