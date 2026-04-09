"""API routes for the pipeline service."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import TypeAdapter

from agents.common.domain.models import SourceSpec
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


@router.get("/pipeline-definitions", response_model=PipelineCatalogResponse)
def list_pipeline_definitions(service: PipelineService = Depends(get_pipeline_service)) -> PipelineCatalogResponse:
    return list_pipelines(service)


@router.post("/pipelines", response_model=PipelineDefinition)
def register_pipeline(
    definition: PipelineDefinition,
    service: PipelineService = Depends(get_pipeline_service),
) -> PipelineDefinition:
    return service.register_pipeline(definition)


@router.post("/pipeline-definitions", response_model=PipelineDefinition)
def register_pipeline_definition(
    definition: PipelineDefinition,
    service: PipelineService = Depends(get_pipeline_service),
) -> PipelineDefinition:
    return register_pipeline(definition, service)


@router.get("/pipeline-definitions/{pipeline_id}", response_model=PipelineDefinition)
def get_pipeline_definition(
    pipeline_id: str,
    service: PipelineService = Depends(get_pipeline_service),
) -> PipelineDefinition:
    definition = service._definitions.get(pipeline_id)
    if definition is None:
        raise HTTPException(status_code=404, detail=f"Unknown pipeline: {pipeline_id}")
    return definition


@router.post("/pipeline-definitions/{pipeline_id}:validate", response_model=PipelineDefinition)
def validate_pipeline_definition(
    pipeline_id: str,
    service: PipelineService = Depends(get_pipeline_service),
) -> PipelineDefinition:
    definition = service._definitions.get(pipeline_id)
    if definition is None:
        raise HTTPException(status_code=404, detail=f"Unknown pipeline: {pipeline_id}")
    service._engine.validate(definition)
    return definition


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


@router.post("/pipeline-runs", response_model=PipelineRunResponse, status_code=202)
def submit_pipeline_run(
    request: PipelineRunRequest,
    service: PipelineService = Depends(get_pipeline_service),
) -> PipelineRunResponse:
    try:
        return service.submit_pipeline_run(request)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs", response_model=PipelineRunCatalogResponse)
def list_runs(limit: int = 20, service: PipelineService = Depends(get_pipeline_service)) -> PipelineRunCatalogResponse:
    return PipelineRunCatalogResponse(runs=service.list_runs(limit=limit))


@router.get("/pipeline-runs", response_model=PipelineRunCatalogResponse)
def list_pipeline_runs(limit: int = 20, service: PipelineService = Depends(get_pipeline_service)) -> PipelineRunCatalogResponse:
    return list_runs(limit, service)


@router.get("/runs/{run_id}", response_model=PipelineRunResponse)
def get_run(run_id: str, service: PipelineService = Depends(get_pipeline_service)) -> PipelineRunResponse:
    run = service.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"Unknown run: {run_id}")
    return run


@router.get("/pipeline-runs/{run_id}", response_model=PipelineRunResponse)
def get_pipeline_run(run_id: str, service: PipelineService = Depends(get_pipeline_service)) -> PipelineRunResponse:
    return get_run(run_id, service)


@router.post("/sources")
def register_source(payload: dict[str, Any], service: PipelineService = Depends(get_pipeline_service)):
    source = TypeAdapter(SourceSpec).validate_python(payload)
    return service._source_resolver.register(source)


@router.get("/sources")
def list_sources(service: PipelineService = Depends(get_pipeline_service)):
    return service._source_resolver.list()


@router.get("/sources/{source_id}")
def get_source(source_id: str, service: PipelineService = Depends(get_pipeline_service)):
    source = service._source_resolver.get(source_id)
    if source is None:
        raise HTTPException(status_code=404, detail=f"Unknown source: {source_id}")
    return source


@router.post("/sources/{source_id}:validate")
def validate_source(source_id: str, service: PipelineService = Depends(get_pipeline_service)):
    try:
        return service._source_resolver.validate(source_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
