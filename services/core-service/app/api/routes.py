"""API routes for the core service."""

from fastapi import APIRouter, Depends

from app.core.config import Settings, settings
from app.models import HealthResponse, ServiceCatalogResponse, ServiceDescriptor, TopologyResponse

router = APIRouter(prefix="/api/v1", tags=["core-service"])


def get_settings() -> Settings:
    return settings


def build_catalog(runtime_settings: Settings) -> list[ServiceDescriptor]:
    return [
        ServiceDescriptor(name="lma", kind="agent", base_url=runtime_settings.lma_url),
        ServiceDescriptor(name="gma", kind="agent", base_url=runtime_settings.gma_url),
        ServiceDescriptor(name="model-service", kind="service", base_url=runtime_settings.model_service_url),
        ServiceDescriptor(name="pipeline-service", kind="service", base_url=runtime_settings.pipeline_service_url),
    ]


@router.get("/health", response_model=HealthResponse)
def health(runtime_settings: Settings = Depends(get_settings)) -> HealthResponse:
    return HealthResponse(**runtime_settings.as_health_payload())


@router.get("/services", response_model=ServiceCatalogResponse)
def services(runtime_settings: Settings = Depends(get_settings)) -> ServiceCatalogResponse:
    return ServiceCatalogResponse(services=build_catalog(runtime_settings))


@router.get("/topology", response_model=TopologyResponse)
def topology(runtime_settings: Settings = Depends(get_settings)) -> TopologyResponse:
    return TopologyResponse(
        framework="dagents",
        services=build_catalog(runtime_settings),
    )
