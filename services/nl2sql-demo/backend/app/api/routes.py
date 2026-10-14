"""FastAPI routes for the Dagents NL2SQL demo app."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.core.config import Settings, settings
from app.models import (
    DagentsBackendStatus,
    HealthResponse,
    ModelDescriptor,
    NL2SQLRequest,
    NL2SQLResponse,
    SamplePrompt,
)
from app.services.dagents_orchestrator import DagentsOrchestrator
from app.services.model_adapters import NL2SQLModelRegistry
from app.services.samples import SAMPLES
from app.services.schema import ddl_from_tables

router = APIRouter(prefix="/api/v1", tags=["nl2sql-demo"])
model_registry = NL2SQLModelRegistry(settings)
dagents_orchestrator = DagentsOrchestrator(settings)


def get_settings() -> Settings:
    return settings


def get_registry() -> NL2SQLModelRegistry:
    return model_registry


def get_orchestrator() -> DagentsOrchestrator:
    return dagents_orchestrator


@router.get("/health", response_model=HealthResponse)
def health(runtime_settings: Settings = Depends(get_settings)) -> HealthResponse:
    """Liveness. Reads nothing and calls no other service."""
    return HealthResponse(**runtime_settings.as_health_payload())


@router.get("/samples", response_model=list[SamplePrompt])
def samples() -> list[SamplePrompt]:
    """The sample questions the demo page offers, each with its tables."""
    return SAMPLES


@router.get("/models", response_model=list[ModelDescriptor])
def models(registry: NL2SQLModelRegistry = Depends(get_registry)) -> list[ModelDescriptor]:
    """The SQL models this backend can use: one per model zip in `NL2SQL_MODELS_DIR`, then the heuristic."""
    return registry.descriptors()


@router.get("/dagents/status", response_model=DagentsBackendStatus)
def dagents_status(orchestrator: DagentsOrchestrator = Depends(get_orchestrator)) -> DagentsBackendStatus:
    """Call each framework service's health check and report which ones answer."""
    return orchestrator.backend_status()


@router.post("/generate", response_model=NL2SQLResponse)
def generate_sql(
    request: NL2SQLRequest,
    registry: NL2SQLModelRegistry = Depends(get_registry),
    orchestrator: DagentsOrchestrator = Depends(get_orchestrator),
) -> NL2SQLResponse:
    """Turn a question into SQL, after running the framework steps on its schema.

    The SQL is generated, not run: no database is queried.
    """
    descriptor = registry.descriptor(request.model_id)
    schema_ddl = ddl_from_tables(request.tables)
    trace = orchestrator.functional_trace(request.tables, request.question) if request.use_dagents_services else []
    sql, prompt, used_fallback, fallback_detail = registry.generate(
        descriptor,
        request.question,
        request.tables,
        request.max_new_tokens,
    )
    return NL2SQLResponse(
        sql=sql,
        model_id=descriptor.model_id,
        model_label=descriptor.label,
        prompt=prompt,
        schema_ddl=schema_ddl,
        adapter_kind=descriptor.adapter_kind,
        used_fallback=used_fallback,
        fallback_detail=fallback_detail,
        dagents_trace=trace,
    )
