"""API routes for the model service."""

from fastapi import APIRouter, Depends

from app.core.config import Settings, settings
from app.models import DatasetCatalogResponse, HealthResponse, TrainRequest, TrainResponse
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
