"""FastAPI entrypoint for the Local Monitoring Agent."""

from fastapi import FastAPI

from agents.lma.config import settings
from agents.lma.di import build_monitoring_service
from agents.lma.domain.models import DeployBundleCommand, RunExecutionResponse, RunRequest


service = build_monitoring_service()

app = FastAPI(title=settings.app_name, version="0.1.0")


@app.get("/health")
def health() -> dict[str, object]:
    return service.health_payload()


@app.get("/bundles")
def list_bundles():
    return service.list_bundles()


@app.post("/bundles/deploy")
def deploy_bundle(command: DeployBundleCommand):
    return service.deploy_bundle(command)


@app.get("/runs")
def list_runs(limit: int = 20):
    return service.list_runs(limit=limit)


@app.get("/telemetry")
def list_telemetry(limit: int = 20):
    return service.recent_telemetry(limit=limit)


@app.post("/run", response_model=RunExecutionResponse)
def run_monitoring(request: RunRequest) -> RunExecutionResponse:
    return service.run(request)
