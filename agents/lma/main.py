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
    """Return the raw health payload used by the LMA's internal health checks."""
    return service.health_payload()


@app.get("/api/v1/health")
def health_v1() -> dict[str, object]:
    """Versioned alias for the health endpoint used by newer clients."""
    return health()


@app.get("/bundles")
def list_bundles():
    """List monitoring bundles currently known to the local agent."""
    return service.list_bundles()


@app.post("/bundles/deploy")
def deploy_bundle(command: DeployBundleCommand):
    """Store or update a bundle that future LMA runs may execute."""
    return service.deploy_bundle(command)


@app.get("/runs")
def list_runs(limit: int = 20):
    """List recent local monitoring runs."""
    return service.list_runs(limit=limit)


@app.post("/datasets/profile")
def profile_source_dataset(request: DatasetProfileRequest):
    """Profile source-scoped data before a model run is selected."""
    return service.profile_source_dataset(request)


@app.post("/api/v1/datasets:profile")
def profile_source_dataset_v1(request: DatasetProfileRequest):
    """Versioned alias for source dataset profiling."""
    return profile_source_dataset(request)


@app.post("/models/run", response_model=ModelExecutionResponse)
def run_source_model(request: ModelExecutionRequest) -> ModelExecutionResponse:
    """Execute a source-scoped model run synchronously."""
    return service.run_source_model(request)


@app.post("/api/v1/model-jobs", response_model=ModelExecutionResponse, status_code=202)
def run_source_model_v1(request: ModelExecutionRequest) -> ModelExecutionResponse:
    """Versioned alias that presents source execution as a model-job API."""
    return run_source_model(request)


@app.get("/models/runs")
def list_model_runs(limit: int = 20):
    """List recent source-level model runs executed by the LMA."""
    return service.list_model_runs(limit=limit)


@app.get("/api/v1/model-jobs")
def list_model_runs_v1(limit: int = 20):
    """Versioned alias for listing source-level model jobs."""
    return list_model_runs(limit=limit)


@app.post("/api/v1/sources")
def register_source(payload: dict[str, Any]):
    """Validate and register a reusable source definition at the agent edge."""
    source = TypeAdapter(SourceSpec).validate_python(payload)
    return service.register_source(source)


@app.get("/api/v1/sources")
def list_sources():
    """List all sources registered with this LMA instance."""
    return service.list_sources()


@app.get("/api/v1/sources/{source_id}")
def get_source(source_id: str):
    """Fetch one source definition registered with this LMA instance."""
    return service.get_source(source_id)


@app.post("/api/v1/sources/{source_id}:validate")
def validate_source(source_id: str):
    """Run shared source validation for a registered source id."""
    return service.validate_source(source_id)


@app.get("/telemetry")
def list_telemetry(limit: int = 20):
    """List recent telemetry envelopes emitted by local monitoring runs."""
    return service.recent_telemetry(limit=limit)


@app.post("/run", response_model=RunExecutionResponse)
def run_monitoring(request: RunRequest) -> RunExecutionResponse:
    """Execute a legacy monitoring run over a deployed bundle."""
    return service.run(request)
