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
    """Dependency hook that exposes the process-wide model-service settings."""
    return settings


def get_training_service() -> ModelTrainingService:
    """Dependency hook that exposes the shared training service instance."""
    return training_service


@router.get("/health", response_model=HealthResponse)
def health(runtime_settings: Settings = Depends(get_settings)) -> HealthResponse:
    """Return service health and runtime metadata for health checks."""
    return HealthResponse(**runtime_settings.as_health_payload())


@router.get("/datasets", response_model=DatasetCatalogResponse)
def datasets(service: ModelTrainingService = Depends(get_training_service)) -> DatasetCatalogResponse:
    """List built-in benchmark datasets available to the model service."""
    return DatasetCatalogResponse(datasets=service.list_datasets())


@router.post("/train", response_model=TrainResponse)
def train(
    request: TrainRequest,
    service: ModelTrainingService = Depends(get_training_service),
) -> TrainResponse:
    """Execute synchronous anomaly-model training.

    Params:
    - `request`: training payload selecting dataset, model family, and tuning options.
    - `service`: injected training façade.

    What it does:
    - Delegates directly to the training service.

    Returns:
    - `TrainResponse` with artifact and metric metadata.
    """
    return service.train(request)


@router.post("/checks/classification", response_model=ClassificationCheckResponse)
def classification_check(
    request: MLCheckRequest,
    service: ModelTrainingService = Depends(get_training_service),
) -> ClassificationCheckResponse:
    """Run a one-shot classification evaluation against the supplied dataset."""
    return service.classification_check(request)


@router.post("/checks/regression", response_model=RegressionCheckResponse)
def regression_check(
    request: MLCheckRequest,
    service: ModelTrainingService = Depends(get_training_service),
) -> RegressionCheckResponse:
    """Run a one-shot regression evaluation against the supplied dataset."""
    return service.regression_check(request)


@router.post("/checks/forecasting", response_model=ForecastingCheckResponse)
def forecasting_check(
    request: MLCheckRequest,
    service: ModelTrainingService = Depends(get_training_service),
) -> ForecastingCheckResponse:
    """Run a one-shot forecasting evaluation, typically with GRU or LSTM."""
    return service.forecasting_check(request)


@router.post("/model-jobs", response_model=ModelJobResponse, status_code=202)
def submit_model_job(
    request: TrainRequest,
    service: ModelTrainingService = Depends(get_training_service),
) -> ModelJobResponse:
    """Submit a background training job and return the queued job record."""
    return service.submit_job(request)


@router.get("/model-jobs/{job_id}", response_model=ModelJobResponse)
def get_model_job(
    job_id: str,
    service: ModelTrainingService = Depends(get_training_service),
) -> ModelJobResponse:
    """Fetch one previously submitted model job by `job_id`."""
    job = service.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Unknown job: {job_id}")
    return job


@router.get("/model-jobs", response_model=ModelJobCatalogResponse)
def list_model_jobs(
    limit: int = 20,
    service: ModelTrainingService = Depends(get_training_service),
) -> ModelJobCatalogResponse:
    """List recent model jobs in reverse chronological order."""
    return ModelJobCatalogResponse(jobs=service.list_jobs(limit=limit))


@router.post("/sources")
def register_source(payload: dict[str, Any], service: ModelTrainingService = Depends(get_training_service)):
    """Validate and register a reusable source definition for model-service use."""
    source = TypeAdapter(SourceSpec).validate_python(payload)
    return service.register_source(source)


@router.get("/sources")
def list_sources(service: ModelTrainingService = Depends(get_training_service)):
    """List all source definitions registered with the model service."""
    return service.list_sources()


@router.get("/sources/{source_id}")
def get_source(source_id: str, service: ModelTrainingService = Depends(get_training_service)):
    """Fetch one stored source definition by id."""
    source = service.get_source(source_id)
    if source is None:
        raise HTTPException(status_code=404, detail=f"Unknown source: {source_id}")
    return source


@router.post("/sources/{source_id}:validate")
def validate_source(source_id: str, service: ModelTrainingService = Depends(get_training_service)):
    """Run shared source validation for one registered source id."""
    try:
        return service.validate_source(source_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
