"""API routes for the pipeline service."""

from fastapi import APIRouter, Depends, HTTPException

from app.core.config import Settings, settings
from app.models import (
    HealthResponse,
    PipelineCatalogResponse,
    PipelineDefinition,
    PipelineRunCatalogResponse,
    PipelineRunRequest,
    PipelineRunResponse,
)
from app.services.pipeline_service import PipelineService, pipeline_service

router = APIRouter(prefix="/api/v1", tags=["pipeline-service"])


def get_settings() -> Settings:
    return settings


def get_pipeline_service() -> PipelineService:
    return pipeline_service


@router.get("/health", response_model=HealthResponse)
def health(runtime_settings: Settings = Depends(get_settings)) -> HealthResponse:
    return HealthResponse(**runtime_settings.as_health_payload())


@router.get("/pipelines", response_model=PipelineCatalogResponse)
def list_pipelines(service: PipelineService = Depends(get_pipeline_service)) -> PipelineCatalogResponse:
    return service.list_pipelines()


@router.post("/pipelines", response_model=PipelineDefinition)
def register_pipeline(
    definition: PipelineDefinition,
    service: PipelineService = Depends(get_pipeline_service),
) -> PipelineDefinition:
    return service.register_pipeline(definition)


@router.post("/pipelines/{pipeline_id}/runs", response_model=PipelineRunResponse)
def run_pipeline(
    pipeline_id: str,
    request: PipelineRunRequest,
    service: PipelineService = Depends(get_pipeline_service),
) -> PipelineRunResponse:
    try:
        return service.run_pipeline(pipeline_id, request)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs", response_model=PipelineRunCatalogResponse)
def list_runs(limit: int = 20, service: PipelineService = Depends(get_pipeline_service)) -> PipelineRunCatalogResponse:
    return PipelineRunCatalogResponse(runs=service.list_runs(limit=limit))


@router.get("/runs/{run_id}", response_model=PipelineRunResponse)
def get_run(run_id: str, service: PipelineService = Depends(get_pipeline_service)) -> PipelineRunResponse:
    run = service.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Unknown run: {run_id}")
    return run
