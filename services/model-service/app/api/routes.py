"""API routes for the model service."""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import TypeAdapter

from agents.common.domain.models import SourceSpec
from app.core.config import Settings, settings
from app.models import (
    ClassificationCheckResponse,
    DatasetCatalogResponse,
    ForecastingCheckResponse,
    HealthResponse,
    MLCheckRequest,
    ModelJobCatalogResponse,
    ModelJobResponse,
    RegressionCheckResponse,
    TrainRequest,
    TrainResponse,
)
from app.services.training_service import ModelTrainingService, training_service

router = APIRouter(prefix="/api/v1", tags=["model-service"])


def get_settings() -> Settings:
    return settings


def get_training_service() -> ModelTrainingService:
    return training_service


@router.get("/health", response_model=HealthResponse)
def health(runtime_settings: Settings = Depends(get_settings)) -> HealthResponse:
    return HealthResponse(**runtime_settings.as_health_payload())


@router.get("/datasets", response_model=DatasetCatalogResponse)
def datasets(service: ModelTrainingService = Depends(get_training_service)) -> DatasetCatalogResponse:
    return DatasetCatalogResponse(datasets=service.list_datasets())


@router.post("/train", response_model=TrainResponse)
def train(
    request: TrainRequest,
    service: ModelTrainingService = Depends(get_training_service),
) -> TrainResponse:
    return service.train(request)


@router.post("/checks/classification", response_model=ClassificationCheckResponse)
def classification_check(
    request: MLCheckRequest,
    service: ModelTrainingService = Depends(get_training_service),
) -> ClassificationCheckResponse:
    return service.classification_check(request)


@router.post("/checks/regression", response_model=RegressionCheckResponse)
def regression_check(
    request: MLCheckRequest,
    service: ModelTrainingService = Depends(get_training_service),
) -> RegressionCheckResponse:
    return service.regression_check(request)


@router.post("/checks/forecasting", response_model=ForecastingCheckResponse)
def forecasting_check(
    request: MLCheckRequest,
    service: ModelTrainingService = Depends(get_training_service),
) -> ForecastingCheckResponse:
    return service.forecasting_check(request)


@router.post("/model-jobs", response_model=ModelJobResponse, status_code=202)
def submit_model_job(
    request: TrainRequest,
    service: ModelTrainingService = Depends(get_training_service),
) -> ModelJobResponse:
    return service.submit_job(request)


@router.get("/model-jobs/{job_id}", response_model=ModelJobResponse)
def get_model_job(
    job_id: str,
    service: ModelTrainingService = Depends(get_training_service),
) -> ModelJobResponse:
    job = service.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Unknown job: {job_id}")
    return job


@router.get("/model-jobs", response_model=ModelJobCatalogResponse)
def list_model_jobs(
    limit: int = 20,
    service: ModelTrainingService = Depends(get_training_service),
) -> ModelJobCatalogResponse:
    return ModelJobCatalogResponse(jobs=service.list_jobs(limit=limit))


@router.post("/sources")
def register_source(payload: dict[str, Any], service: ModelTrainingService = Depends(get_training_service)):
    source = TypeAdapter(SourceSpec).validate_python(payload)
    return service.register_source(source)


@router.get("/sources")
def list_sources(service: ModelTrainingService = Depends(get_training_service)):
    return service.list_sources()


@router.get("/sources/{source_id}")
def get_source(source_id: str, service: ModelTrainingService = Depends(get_training_service)):
    source = service.get_source(source_id)
    if source is None:
        raise HTTPException(status_code=404, detail=f"Unknown source: {source_id}")
    return source


@router.post("/sources/{source_id}:validate")
def validate_source(source_id: str, service: ModelTrainingService = Depends(get_training_service)):
    try:
        return service.validate_source(source_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
