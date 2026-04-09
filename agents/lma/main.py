"""FastAPI entrypoint for the Local Monitoring Agent."""

from typing import Any

from fastapi import FastAPI
from pydantic import TypeAdapter

from agents.common.domain.models import SourceSpec
from agents.lma.config import settings
from agents.lma.di import build_monitoring_service
from agents.lma.domain.models import (
    DatasetProfileRequest,
    DeployBundleCommand,
    ModelExecutionRequest,
    ModelExecutionResponse,
    RunExecutionResponse,
    RunRequest,
)


service = build_monitoring_service()

app = FastAPI(title=settings.app_name, version="0.1.0")


@app.get("/health")
def health() -> dict[str, object]:
    return service.health_payload()


@app.get("/api/v1/health")
def health_v1() -> dict[str, object]:
    return health()


@app.get("/bundles")
def list_bundles():
    return service.list_bundles()


@app.post("/bundles/deploy")
def deploy_bundle(command: DeployBundleCommand):
    return service.deploy_bundle(command)


@app.get("/runs")
def list_runs(limit: int = 20):
    return service.list_runs(limit=limit)


@app.post("/datasets/profile")
def profile_source_dataset(request: DatasetProfileRequest):
    return service.profile_source_dataset(request)


@app.post("/api/v1/datasets:profile")
def profile_source_dataset_v1(request: DatasetProfileRequest):
    return profile_source_dataset(request)


@app.post("/models/run", response_model=ModelExecutionResponse)
def run_source_model(request: ModelExecutionRequest) -> ModelExecutionResponse:
    return service.run_source_model(request)


@app.post("/api/v1/model-jobs", response_model=ModelExecutionResponse, status_code=202)
def run_source_model_v1(request: ModelExecutionRequest) -> ModelExecutionResponse:
    return run_source_model(request)


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


@app.get("/telemetry")
def list_telemetry(limit: int = 20):
    return service.recent_telemetry(limit=limit)


@app.post("/run", response_model=RunExecutionResponse)
def run_monitoring(request: RunRequest) -> RunExecutionResponse:
    return service.run(request)
