"""FastAPI entrypoint for the Local Monitoring Agent."""

from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, Field, TypeAdapter

from agents.common.domain.governance import (
    DataClassification,
    GuardAuditLog,
    GuardDecision,
    RestrictionPlan,
    RestrictionRequest,
)
from agents.common.domain.models import SourceSpec, SourceValidationResult
from agents.common.extensions import default_registry
from agents.common.extensions.registry import ExtensionSummary
from agents.lma.config import settings
from agents.lma.di import build_governance_service, build_monitoring_service
from agents.lma.domain.models import (
    BundleRecord,
    CommandStatus,
    DatasetProfile,
    DatasetProfileRequest,
    DeployBundleCommand,
    ModelExecutionRequest,
    ModelExecutionResponse,
    ModelRunRecord,
    RunExecutionResponse,
    RunRecord,
    RunRequest,
    TelemetryEnvelope,
)


service = build_monitoring_service()
governance = build_governance_service()

app = FastAPI(title=settings.app_name, version="0.1.0")


# OpenAPI metadata. The `responses=` entries below describe what a route
# returns where the route has no response model; they change only the schema
# document, not the response.


class LmaHealth(BaseModel):
    """The LMA's health answer: its status and how much it holds in memory."""

    status: str = Field(description="`ok` whenever the process answers.")
    agent_type: str = Field(description="Always `LMA`.")
    deployed_bundles: int = Field(description="How many bundles are stored. Each id and version pair counts once.")
    runs: int = Field(description="How many monitoring runs are stored, counted up to 10,000.")
    model_runs: int = Field(description="How many model runs are stored, counted up to 10,000.")
    active_bundle: str | None = Field(
        description="`<bundle_id>:<bundle_version>` of the most recently deployed bundle. Null when none is deployed."
    )


class ErrorDetail(BaseModel):
    """The body of an error the handler raises itself."""

    detail: str = Field(description="What went wrong, in words.")


def _server_error(when: str) -> dict[str, Any]:
    """An unhandled exception: Starlette answers 500 with a plain-text body."""
    return {
        "description": when,
        "content": {"text/plain": {"schema": {"type": "string"}, "example": "Internal Server Error"}},
    }


_LIMIT_HELP = (
    "The most entries to return, newest first. There is no bound check: 0 returns every entry, and a "
    "negative number leaves out that many of the oldest entries."
)
_UNKNOWN_SOURCE = _server_error(
    "The records could not be read: `dataset.source_id` names no registered source, a file is missing, or the "
    "source's adapter cannot read it."
)
_SOURCE_SPEC_SCHEMA = TypeAdapter(SourceSpec).json_schema(ref_template="#/components/schemas/{model}")
_SOURCE_SPEC_SCHEMA.pop("$defs", None)
_SOURCE_ID = Path(description="The `source_id` the source was registered with.")


@app.get("/health", responses={200: {"model": LmaHealth}})
def health() -> dict[str, object]:
    """Return the raw health payload used by the LMA's internal health checks."""
    return service.health_payload()


@app.get("/api/v1/health", responses={200: {"model": LmaHealth}})
def health_v1() -> dict[str, object]:
    """Versioned alias for the health endpoint used by newer clients."""
    return health()


@app.get("/bundles", responses={200: {"model": list[BundleRecord], "description": "The stored bundles, newest deploy first."}})
def list_bundles():
    """List monitoring bundles currently known to the local agent."""
    return service.list_bundles()


@app.post("/bundles/deploy", responses={200: {"model": CommandStatus}})
def deploy_bundle(command: DeployBundleCommand):
    """Store or update a bundle that future LMA runs may execute."""
    return service.deploy_bundle(command)


@app.get("/runs", responses={200: {"model": list[RunRecord], "description": "The stored runs, newest first."}})
def list_runs(limit: Annotated[int, Query(description=_LIMIT_HELP)] = 20):
    """List recent local monitoring runs."""
    return service.list_runs(limit=limit)


@app.post("/datasets/profile", responses={200: {"model": DatasetProfile}, 500: _UNKNOWN_SOURCE})
def profile_source_dataset(request: DatasetProfileRequest):
    """Profile source-scoped data before a model run is selected."""
    return service.profile_source_dataset(request)


@app.post("/api/v1/datasets:profile", responses={200: {"model": DatasetProfile}, 500: _UNKNOWN_SOURCE})
def profile_source_dataset_v1(request: DatasetProfileRequest):
    """Versioned alias for source dataset profiling."""
    return profile_source_dataset(request)


@app.post("/models/run", response_model=ModelExecutionResponse, responses={500: _UNKNOWN_SOURCE})
def run_source_model(request: ModelExecutionRequest) -> ModelExecutionResponse:
    """Execute a source-scoped model run synchronously."""
    return service.run_source_model(request)


@app.post(
    "/api/v1/model-jobs", response_model=ModelExecutionResponse, status_code=202, responses={500: _UNKNOWN_SOURCE}
)
def run_source_model_v1(request: ModelExecutionRequest) -> ModelExecutionResponse:
    """Versioned alias that presents source execution as a model-job API."""
    return run_source_model(request)


@app.get("/models/runs", responses={200: {"model": list[ModelRunRecord], "description": "The stored runs, newest first."}})
def list_model_runs(limit: Annotated[int, Query(description=_LIMIT_HELP)] = 20):
    """List recent source-level model runs executed by the LMA."""
    return service.list_model_runs(limit=limit)


@app.get(
    "/api/v1/model-jobs",
    responses={200: {"model": list[ModelRunRecord], "description": "The stored runs, newest first."}},
)
def list_model_runs_v1(limit: Annotated[int, Query(description=_LIMIT_HELP)] = 20):
    """Versioned alias for listing source-level model jobs."""
    return list_model_runs(limit=limit)


@app.post(
    "/api/v1/sources",
    responses={
        200: {"model": SourceSpec, "description": "The stored source definition, with its defaults filled in."},
        500: _server_error("The body is not a valid source definition. The handler validates it itself."),
    },
    # The handler takes a plain dict and validates it with TypeAdapter, so
    # FastAPI cannot see the body's shape. This states it for the schema only.
    openapi_extra={"requestBody": {"content": {"application/json": {"schema": _SOURCE_SPEC_SCHEMA}}}},
)
def register_source(payload: dict[str, Any]):
    """Validate and register a reusable source definition at the agent edge."""
    source = TypeAdapter(SourceSpec).validate_python(payload)
    return service.register_source(source)


@app.get(
    "/api/v1/sources",
    responses={200: {"model": list[SourceSpec], "description": "Every stored source, sorted by `source_id`."}},
)
def list_sources():
    """List all sources registered with this LMA instance."""
    return service.list_sources()


@app.get(
    "/api/v1/sources/{source_id}",
    responses={200: {"model": SourceSpec | None, "description": "The source, or `null` when no source has this id."}},
)
def get_source(source_id: Annotated[str, _SOURCE_ID]):
    """Fetch one source definition registered with this LMA instance."""
    return service.get_source(source_id)


@app.post(
    "/api/v1/sources/{source_id}:validate",
    responses={
        200: {"model": SourceValidationResult},
        500: _server_error("No source is registered with this id."),
    },
)
def validate_source(source_id: Annotated[str, _SOURCE_ID]):
    """Run shared source validation for a registered source id."""
    return service.validate_source(source_id)


@app.get(
    "/telemetry",
    responses={200: {"model": list[TelemetryEnvelope], "description": "The published telemetry, newest first."}},
)
def list_telemetry(limit: Annotated[int, Query(description=_LIMIT_HELP)] = 20):
    """List recent telemetry envelopes emitted by local monitoring runs."""
    return service.recent_telemetry(limit=limit)


@app.post("/run", response_model=RunExecutionResponse)
def run_monitoring(request: RunRequest) -> RunExecutionResponse:
    """Execute a legacy monitoring run over a deployed bundle."""
    return service.run(request)


class GuardEnforcementRequest(BaseModel):
    """A restriction request plus the payload the Guard should enforce against."""

    request: RestrictionRequest = Field(description="The request to plan and enforce.")
    payload: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "The records the request would return. The Guard applies the plan to them and returns what is "
            "left. Empty asks for a decision only."
        ),
    )
    correlation_id: str | None = Field(
        default=None,
        description="An id that ties the decision to other work, such as a round or run id. Written into the audit record.",
    )


_CLASSIFICATION_ID = Path(description="The classification's id. It must equal `classification_id` in the body.")
_PLANNER_FAILED = _server_error("The planner could not be run, or its answer could not be read.")


@app.get(
    "/governance/classifications",
    responses={200: {"model": list[DataClassification], "description": "Every classification, sorted by id."}},
)
def list_classifications():
    """List the data classifications this agent can enforce against."""
    return governance.list_classifications()


@app.get(
    "/api/v1/governance/classifications",
    responses={200: {"model": list[DataClassification], "description": "Every classification, sorted by id."}},
)
def list_classifications_v1():
    """Versioned alias for listing data classifications."""
    return list_classifications()


@app.put(
    "/governance/classifications/{classification_id}",
    responses={
        200: {"model": DataClassification, "description": "The stored classification."},
        400: {"model": ErrorDetail, "description": "The id in the path differs from the id in the body."},
    },
)
def register_classification(classification_id: Annotated[str, _CLASSIFICATION_ID], classification: DataClassification):
    """Register or replace one runtime data classification."""
    if classification.classification_id != classification_id:
        raise HTTPException(
            status_code=400,
            detail=f"path id {classification_id} does not match body id {classification.classification_id}",
        )
    return governance.register_classification(classification)


@app.put(
    "/api/v1/governance/classifications/{classification_id}",
    responses={
        200: {"model": DataClassification, "description": "The stored classification."},
        400: {"model": ErrorDetail, "description": "The id in the path differs from the id in the body."},
    },
)
def register_classification_v1(classification_id: Annotated[str, _CLASSIFICATION_ID], classification: DataClassification):
    """Versioned alias for registering a data classification."""
    return register_classification(classification_id, classification)


@app.post("/governance/restrictions:plan", responses={200: {"model": RestrictionPlan}, 500: _PLANNER_FAILED})
def plan_restriction(request: RestrictionRequest):
    """Ask what protection a request would need, without enforcing anything.

    Nothing is recorded, because nothing was enforced. Use the enforcement
    endpoint when the answer should be acted on and audited.
    """
    return governance.plan(request)


@app.post("/api/v1/governance/restrictions:plan", responses={200: {"model": RestrictionPlan}, 500: _PLANNER_FAILED})
def plan_restriction_v1(request: RestrictionRequest):
    """Versioned alias for restriction planning."""
    return plan_restriction(request)


@app.post(
    "/governance/restrictions:enforce",
    responses={200: {"model": GuardDecision, "description": "The Guard's decision. A denial is a 200 too."}},
)
def enforce_restriction(payload: GuardEnforcementRequest):
    """Plan, apply, and record one governed request against a payload.

    The Guard fails closed: when the planner cannot be reached, it denies the
    request and records the denial, so this endpoint still answers 200.
    """
    return governance.enforce(payload.request, payload.payload, correlation_id=payload.correlation_id)


@app.post(
    "/api/v1/governance/restrictions:enforce",
    responses={200: {"model": GuardDecision, "description": "The Guard's decision. A denial is a 200 too."}},
)
def enforce_restriction_v1(payload: GuardEnforcementRequest):
    """Versioned alias for guard enforcement."""
    return enforce_restriction(payload)


@app.get("/governance/audit", responses={200: {"model": GuardAuditLog}})
def governance_audit(limit: Annotated[int, Query(description=_LIMIT_HELP)] = 50):
    """List recent guard decisions and whether the digest chain still verifies."""
    return {
        "chain_intact": governance.audit_chain_intact(),
        "records": governance.recent_audit(limit=limit),
    }


@app.get("/api/v1/governance/audit", responses={200: {"model": GuardAuditLog}})
def governance_audit_v1(limit: Annotated[int, Query(description=_LIMIT_HELP)] = 50):
    """Versioned alias for the guard audit log."""
    return governance_audit(limit=limit)


@app.get("/extensions", responses={200: {"model": ExtensionSummary}})
def list_extensions():
    """Describe what registered extensions contribute to this agent."""
    return default_registry.describe()


@app.get("/api/v1/extensions", responses={200: {"model": ExtensionSummary}})
def list_extensions_v1():
    """Versioned alias for the extension summary."""
    return list_extensions()
