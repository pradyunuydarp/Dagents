"""API routes for the core service."""

from fastapi import APIRouter, Depends, HTTPException

from app.core.config import Settings, settings
from app.models import (
    HealthResponse,
    ServiceCatalogResponse,
    ServiceDescriptor,
    TopologyResponse,
    WorkloadCompileRequest,
    WorkloadManifestRequest,
    WorkloadManifestResponse,
    WorkloadPlanResponse,
)
from app.services.manifest_service import ManifestService, manifest_service

router = APIRouter(prefix="/api/v1", tags=["core-service"])


def get_settings() -> Settings:
    """Dependency hook that exposes the shared core-service settings."""
    return settings


def get_manifest_service() -> ManifestService:
    """Dependency hook that exposes the shared manifest compiler service."""
    return manifest_service


def build_catalog(runtime_settings: Settings) -> list[ServiceDescriptor]:
    """Build the service catalog returned by `/services` and `/topology`.

    Params:
    - `runtime_settings`: resolved service URLs from the active environment.

    What it does:
    - Maps configured service endpoints into API-facing catalog entries.

    Returns:
    - `list[ServiceDescriptor]`.
    """
    return [
        ServiceDescriptor(name="lma", kind="agent", base_url=runtime_settings.lma_url),
        ServiceDescriptor(name="gma", kind="agent", base_url=runtime_settings.gma_url),
        ServiceDescriptor(name="model-service", kind="service", base_url=runtime_settings.model_service_url),
        ServiceDescriptor(name="pipeline-service", kind="service", base_url=runtime_settings.pipeline_service_url),
    ]


@router.get("/health", response_model=HealthResponse)
def health(runtime_settings: Settings = Depends(get_settings)) -> HealthResponse:
    """Return service health and basic runtime metadata."""
    return HealthResponse(**runtime_settings.as_health_payload())


@router.get("/services", response_model=ServiceCatalogResponse)
def services(runtime_settings: Settings = Depends(get_settings)) -> ServiceCatalogResponse:
    """Expose the configured Dagents service catalog."""
    return ServiceCatalogResponse(services=build_catalog(runtime_settings))


@router.get("/topology", response_model=TopologyResponse)
def topology(runtime_settings: Settings = Depends(get_settings)) -> TopologyResponse:
    """Expose a framework topology view suitable for operators and tests."""
    return TopologyResponse(
        framework="dagents",
        services=build_catalog(runtime_settings),
    )


@router.post("/manifests/pods", response_model=WorkloadManifestResponse)
def generate_workload_manifests(
    request: WorkloadManifestRequest,
    service: ManifestService = Depends(get_manifest_service),
) -> WorkloadManifestResponse:
    """Render workload manifests directly without persisting a retrievable plan."""
    return service.generate(request)


@router.post("/workloads:compile", response_model=WorkloadPlanResponse)
def compile_workloads(
    request: WorkloadCompileRequest,
    service: ManifestService = Depends(get_manifest_service),
) -> WorkloadPlanResponse:
    """Compile a workload request into a stored workload plan."""
    return service.compile(request)


@router.get("/workload-plans/{plan_id}", response_model=WorkloadPlanResponse)
def get_workload_plan(
    plan_id: str,
    service: ManifestService = Depends(get_manifest_service),
) -> WorkloadPlanResponse:
    """Fetch a previously compiled workload plan by `plan_id`."""
    plan = service.get_plan(plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail=f"Unknown workload plan: {plan_id}")
    return plan
