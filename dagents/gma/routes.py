"""The GMA's routes.

Every route has a short path and a versioned ``/api/v1`` path; the versioned
handler is named ``<short>_v1`` and calls the short handler, so the two cannot
answer differently. The services come from ``app.state.gma``
(:class:`dagents.gma.app_factory.GmaState`) through dependencies, so one process
can serve several GMAs.

Tokens (``dagents/common/infrastructure/agent_auth.py``). With no token
configured every caller may call every route, as before tokens existed. With
tokens:

| Routes | Scopes |
|---|---|
| `GET /health` | none |
| The LMA reports: registration, heartbeats, telemetry, deployment sync | `admin`, `site` (a site token only for an agent registered with its own site in `scope.site`) |
| `PUT /federation/rounds/{round_id}/updates/{site_id}` | `site`, bound to `site_id` |
| `GET /model-versions:released` | `admin`, `site` |
| Every other route | `admin` |

With ``GMA_ALLOW_SOURCE_READS=0`` the source, dataset profile and model job
routes answer 403.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, TypeAdapter

from dagents.common.application.federation import FederatedRoundController
from dagents.common.application.governance_service import GovernanceService
from dagents.common.domain.errors import NotSelected, RoundConflict
from dagents.common.domain.federation import (
    AggregationReadiness,
    ApprovalResult,
    CandidateModel,
    ModelVersion,
    ModelVersionList,
    ModelVersionRegistration,
    ReleaseEvaluation,
    ReleaseEvaluationRequest,
    RoundManifest,
    RoundPlan,
    RoundRecord,
    SiteAddressList,
    SiteAddressRequest,
    SiteAddressState,
    SiteRegistration,
    SiteResult,
    UpdateReceipt,
    UpdateUpload,
)
from dagents.common.domain.governance import (
    DataClassification,
    GuardAuditLog,
    GuardDecision,
    RestrictionPlan,
    RestrictionRequest,
)
from dagents.common.domain.models import SourceSpec, SourceValidationResult
from dagents.common.domain.site import AuditSummary, SiteDirectoryEntry
from dagents.common.extensions import ExtensionRegistry
from dagents.common.extensions.registry import ExtensionSummary
from dagents.common.infrastructure.agent_auth import AccessDenied, Caller, require_scope
from dagents.common.infrastructure.sources import redact_source
from dagents.gma.app_factory import HandlerThreads
from dagents.gma.application.aggregation_service import AggregationService
from dagents.gma.application.model_registry import ModelRegistry, UnknownModelKind
from dagents.gma.application.release_service import ReleaseService
from dagents.gma.application.site_addresses import SiteAddressBook
from dagents.gma.application.site_directory import SiteDirectory
from dagents.gma.domain.errors import SourceReadsDisabled
from dagents.gma.domain.models import (
    AgentIdentity,
    AgentSnapshot,
    CommandStatus,
    DatasetProfile,
    DatasetProfileRequest,
    DeploymentPlan,
    DeploymentSyncRequest,
    DeploymentSyncResponse,
    DesiredDeploymentRequest,
    DiscardRequest,
    DispatchedRun,
    FleetOverview,
    HeartbeatRequest,
    HeartbeatResponse,
    ModelExecutionRequest,
    ModelExecutionResponse,
    ModelRunRecord,
    RegisterRequest,
    RegisterResponse,
    RunDispatchRequest,
    TelemetryAck,
    TelemetryEnvelope,
    TelemetrySummary,
)
from dagents.gma.infrastructure.update_store import UpdateStore

router = APIRouter()


# --------------------------------------------------------------------------- #
# Services, read from app.state.gma. Async, so no thread is used to read them.
# --------------------------------------------------------------------------- #


async def _service(request: Request) -> AggregationService:
    return request.app.state.gma.service


async def _governance(request: Request) -> GovernanceService:
    return request.app.state.gma.governance


async def _rounds(request: Request) -> FederatedRoundController:
    return request.app.state.gma.rounds


async def _models(request: Request) -> ModelRegistry:
    return request.app.state.gma.models


async def _updates(request: Request) -> UpdateStore:
    return request.app.state.gma.updates


async def _directory(request: Request) -> SiteDirectory:
    return request.app.state.gma.directory


async def _releases(request: Request) -> ReleaseService:
    return request.app.state.gma.releases


async def _addresses(request: Request) -> SiteAddressBook:
    return request.app.state.gma.addresses


async def _extensions(request: Request) -> ExtensionRegistry:
    return request.app.state.gma.extensions


async def _upload_threads(request: Request) -> HandlerThreads:
    return request.app.state.gma.upload_threads


async def _source_reads(request: Request) -> None:
    if not request.app.state.gma.settings.allow_source_reads:
        raise SourceReadsDisabled("this GMA reads no source: it was started with GMA_ALLOW_SOURCE_READS=0")


ServiceDep = Annotated[AggregationService, Depends(_service)]
GovernanceDep = Annotated[GovernanceService, Depends(_governance)]
RoundsDep = Annotated[FederatedRoundController, Depends(_rounds)]
ModelsDep = Annotated[ModelRegistry, Depends(_models)]
UpdatesDep = Annotated[UpdateStore, Depends(_updates)]
DirectoryDep = Annotated[SiteDirectory, Depends(_directory)]
ReleasesDep = Annotated[ReleaseService, Depends(_releases)]
AddressesDep = Annotated[SiteAddressBook, Depends(_addresses)]
ExtensionsDep = Annotated[ExtensionRegistry, Depends(_extensions)]
ThreadsDep = Annotated[HandlerThreads, Depends(_upload_threads)]

ADMIN = [Depends(require_scope("admin"))]
_REPORTER = require_scope("admin", "site")
ReporterDep = Annotated[Caller, Depends(_REPORTER)]
#: The admin and every site's token: the released model, which each site's LMA reads.
ADMIN_OR_SITE = [Depends(_REPORTER)]
SOURCE_READS = [Depends(require_scope("admin")), Depends(_source_reads)]


# --------------------------------------------------------------------------- #
# OpenAPI metadata. The `responses=` entries describe what a route returns
# where the route has no response model; they change only the schema document.
# --------------------------------------------------------------------------- #


class GmaHealth(BaseModel):
    """The GMA's health answer."""

    status: str = Field(description="`ok` whenever the process answers.")
    agent_type: str = Field(description="Always `GMA`.")
    engine: str = Field(
        description="The engine that runs the rounds: `in-process-simulator`, or `http` when jobs go to the LMAs over HTTP."
    )


class ErrorDetail(BaseModel):
    """The body of an error the handler raises itself."""

    detail: str = Field(description="What went wrong, in words.")


class AgentErrorDetail(BaseModel):
    """The body of an error the GMA answers with a code."""

    detail: str = Field(description="What went wrong, in words. Never a connection string, a host or a token.")
    code: str = Field(
        description=(
            "What kind of error: `conflict`, `forbidden`, `not_found`, `unauthorized`, `invalid_request`, "
            "`aggregation_refused`, `planner_unavailable`, `store_unavailable`, `store_not_set_up` and so on."
        )
    )


class LifecycleRefusalDetail(AgentErrorDetail):
    """The body of a 409 that the planner's model lifecycle rule caused."""

    refusal: str | None = Field(
        default=None,
        description=(
            "The planner's refusal: `already_released`, `not_candidate`, `no_decision`, `gates_decided`, "
            "`guard_denied`, `metrics_not_from_rounds`, `baseline_changed` or `decision_differs`. Absent for other "
            "conflicts."
        ),
    )


def _server_error(when: str) -> dict[str, Any]:
    """An unhandled exception: Starlette answers 500 with a plain-text body."""
    return {
        "description": when,
        "content": {"text/plain": {"schema": {"type": "string"}, "example": "Internal Server Error"}},
    }


def _coded(description: str, model: type[BaseModel] = AgentErrorDetail) -> dict[str, Any]:
    return {"model": model, "description": description}


def _coded_or_invalid_body(description: str) -> dict[str, Any]:
    """A 422 that is either FastAPI's validation error (a malformed body) or a coded `invalid_request`."""
    return {
        "description": description,
        "content": {
            "application/json": {
                "schema": {
                    "anyOf": [
                        {"$ref": "#/components/schemas/HTTPValidationError"},
                        {"$ref": "#/components/schemas/AgentErrorDetail"},
                    ]
                }
            }
        },
    }


_LIMIT_HELP = (
    "The most entries to return, newest first. There is no bound check: 0 returns every entry, and a "
    "negative number leaves out that many of the oldest entries."
)
_ROUND_LIMIT_HELP = (
    "The most rounds to return, newest first. There is no bound check: 0 returns none, and a negative "
    "number leaves out that many of the oldest rounds."
)
_AGENT_ID_FROM_PATH = Path(
    description="The agent's id. When `agent.agent_id` in the body differs, this value replaces it."
)
_UNKNOWN_SOURCE = _server_error(
    "The records could not be read: `dataset.source_id` names no registered source, a file is missing, or the "
    "source's adapter cannot read it."
)
_SOURCE_SPEC_SCHEMA = TypeAdapter(SourceSpec).json_schema(ref_template="#/components/schemas/{model}")
_SOURCE_SPEC_SCHEMA.pop("$defs", None)
_SOURCE_ID = Path(description="The `source_id` the source was registered with.")
_SOURCE_READS_OFF = _coded("The GMA was started with `GMA_ALLOW_SOURCE_READS=0`, so it reads no source.")
_PLANNER_FAILED = _coded("`planner_unavailable`: the planner could not be run, or its answer could not be used.")
_ROUND_ID = Path(description="The `round_id` of a round dispatched with `POST /api/v1/federation/rounds`.")
_UNKNOWN_ROUND = {"model": ErrorDetail, "description": "No round has this id."}
_VERSION = Path(description="The model version's name: 1 to 200 characters.")


def _unknown_round(round_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"unknown round: {round_id}")


def _check_reporter(
    caller: Caller, service: AggregationService, agent_id: str, scope: dict[str, str] | None = None
) -> None:
    """Let a site token report only for an agent whose ``scope.site`` is the token's site.

    The admin token, and every caller of a GMA without tokens, may report for
    any agent. A site token may register an agent only with its own site in
    ``scope.site``, and only when no agent of another site holds that id; it
    may send heartbeats, telemetry and deployment syncs only for an agent
    registered that way. Raises :class:`AccessDenied` (403) otherwise.
    """
    if caller.site_id is None:
        return
    stored = service.get_agent(agent_id)
    stored_site = stored.scope.get("site") if stored is not None else None
    if scope is not None:
        allowed = scope.get("site") == caller.site_id and (stored is None or stored_site == caller.site_id)
    else:
        allowed = stored is not None and stored_site == caller.site_id
    if not allowed:
        raise AccessDenied(
            "a site token may report only for an agent registered with its own site in scope.site"
        )


# --------------------------------------------------------------------------- #
# Health and the LMA control plane
# --------------------------------------------------------------------------- #


@router.get("/health", responses={200: {"model": GmaHealth}})
def health(service: ServiceDep, rounds: RoundsDep) -> dict[str, str]:
    """Return the raw health payload used by the GMA's internal checks.

    It names the engine that runs the rounds. It reads no stored state and does
    not call the planner.
    """
    return {**service.health_payload(), "engine": rounds.engine_name}


@router.get("/api/v1/health", responses={200: {"model": GmaHealth}})
def health_v1(service: ServiceDep, rounds: RoundsDep) -> dict[str, str]:
    """Versioned alias for the GMA health endpoint."""
    return health(service, rounds)


_REPORT_FORBIDDEN = {
    403: _coded(
        "`forbidden`: the token may not call this route, or a site token reports for an agent that is not "
        "registered with its own site in `scope.site`."
    )
}


@router.post("/register", responses={200: {"model": RegisterResponse}, **_REPORT_FORBIDDEN})
def register(request: RegisterRequest, service: ServiceDep, caller: ReporterDep):
    """Register one LMA with the aggregate control plane.

    The agent is stored in memory under `agent.agent_id`. Registering the same
    id again replaces its identity, scope, version and capabilities and keeps
    its status, heartbeat and bundle fields. A site token may register an agent
    only with its own site in `scope.site`, and never over an agent of another
    site.
    """
    _check_reporter(caller, service, request.agent.agent_id, request.scope)
    return service.register(request)


@router.put("/api/v1/agents/{agent_id}/registration", responses={200: {"model": RegisterResponse}, **_REPORT_FORBIDDEN})
def register_v1(
    agent_id: Annotated[str, _AGENT_ID_FROM_PATH], request: RegisterRequest, service: ServiceDep, caller: ReporterDep
):
    """Versioned registration endpoint keyed by path-level `agent_id`.

    The path id wins over any mismatched id in the request body so route-level
    identity stays authoritative.
    """
    payload = request if request.agent.agent_id == agent_id else request.model_copy(
        update={"agent": AgentIdentity(**{**request.agent.model_dump(), "agent_id": agent_id})}
    )
    return register(payload, service, caller)


@router.post("/heartbeat", responses={200: {"model": HeartbeatResponse}, **_REPORT_FORBIDDEN})
def heartbeat(request: HeartbeatRequest, service: ServiceDep, caller: ReporterDep):
    """Ingest one heartbeat update from a registered LMA.

    The heartbeat's status and timestamp are stored on the agent's record. An
    agent that never registered is added, with version `unknown`. A site token
    may send heartbeats only for an agent registered with its own site in
    `scope.site`.
    """
    _check_reporter(caller, service, request.agent.agent_id)
    return service.heartbeat(request)


@router.post("/api/v1/agents/{agent_id}/heartbeats", responses={200: {"model": HeartbeatResponse}, **_REPORT_FORBIDDEN})
def heartbeat_v1(
    agent_id: Annotated[str, _AGENT_ID_FROM_PATH], request: HeartbeatRequest, service: ServiceDep, caller: ReporterDep
):
    """Versioned heartbeat endpoint keyed by path-level `agent_id`."""
    payload = request if request.agent.agent_id == agent_id else request.model_copy(
        update={"agent": AgentIdentity(**{**request.agent.model_dump(), "agent_id": agent_id})}
    )
    return heartbeat(payload, service, caller)


@router.post("/telemetry", responses={200: {"model": TelemetryAck}, **_REPORT_FORBIDDEN})
def telemetry(request: TelemetryEnvelope, service: ServiceDep, caller: ReporterDep):
    """Ingest one telemetry envelope into the aggregate repository.

    The envelope is appended to the GMA's telemetry list. When the sender is
    registered, its record keeps the new ingestion id. A site token may send
    telemetry only for an agent registered with its own site in `scope.site`.
    """
    _check_reporter(caller, service, request.agent.agent_id)
    return service.ingest_telemetry(request)


@router.post("/api/v1/agents/{agent_id}/telemetry", responses={200: {"model": TelemetryAck}, **_REPORT_FORBIDDEN})
def telemetry_v1(
    agent_id: Annotated[str, _AGENT_ID_FROM_PATH], request: TelemetryEnvelope, service: ServiceDep, caller: ReporterDep
):
    """Versioned telemetry endpoint keyed by path-level `agent_id`."""
    payload = request if request.agent.agent_id == agent_id else request.model_copy(
        update={"agent": AgentIdentity(**{**request.agent.model_dump(), "agent_id": agent_id})}
    )
    return telemetry(payload, service, caller)


@router.post("/deployments/sync", responses={200: {"model": DeploymentSyncResponse}, **_REPORT_FORBIDDEN})
def sync_deployment(request: DeploymentSyncRequest, service: ServiceDep, caller: ReporterDep):
    """Compare an LMA's current bundle with the desired deployment plan.

    The reported bundle is stored on the agent's record as its current bundle.
    An agent that never registered is added, with version `unknown`. A site
    token may sync only an agent registered with its own site in `scope.site`.
    """
    _check_reporter(caller, service, request.agent.agent_id)
    return service.sync_deployment(request)


@router.post(
    "/api/v1/agents/{agent_id}/deployment-sync",
    responses={200: {"model": DeploymentSyncResponse}, **_REPORT_FORBIDDEN},
)
def sync_deployment_v1(
    agent_id: Annotated[str, _AGENT_ID_FROM_PATH], request: DeploymentSyncRequest, service: ServiceDep, caller: ReporterDep
):
    """Versioned deployment-sync endpoint keyed by path-level `agent_id`."""
    payload = request if request.agent.agent_id == agent_id else request.model_copy(
        update={"agent": AgentIdentity(**{**request.agent.model_dump(), "agent_id": agent_id})}
    )
    return sync_deployment(payload, service, caller)


@router.post("/deployments/plan", responses={200: {"model": DeploymentPlan}}, dependencies=ADMIN)
def plan_deployment(request: DesiredDeploymentRequest, service: ServiceDep):
    """Store the desired deployment state for one LMA.

    One desired deployment is kept per agent; a new one replaces the old. When
    the agent is registered, its record's desired bundle is updated too.
    """
    return service.plan_deployment(request)


@router.put("/api/v1/agents/{agent_id}/desired-deployment", responses={200: {"model": DeploymentPlan}}, dependencies=ADMIN)
def plan_deployment_v1(
    agent_id: Annotated[
        str, Path(description="The agent's id. When `agent_id` in the body differs, this value replaces it.")
    ],
    request: DesiredDeploymentRequest,
    service: ServiceDep,
):
    """Versioned desired-deployment endpoint keyed by path-level `agent_id`."""
    payload = request if request.agent_id == agent_id else request.model_copy(update={"agent_id": agent_id})
    return plan_deployment(payload, service)


@router.get(
    "/deployments",
    responses={200: {"model": list[DeploymentPlan], "description": "Every desired deployment, sorted by `agent_id`."}},
    dependencies=ADMIN,
)
def list_deployments(service: ServiceDep):
    """List current desired deployment plans known to the GMA."""
    return service.list_deployments()


@router.post("/runs/dispatch", responses={200: {"model": CommandStatus}}, dependencies=ADMIN)
def dispatch_run(request: RunDispatchRequest, service: ServiceDep):
    """Record one aggregate run dispatch for downstream execution systems.

    The dispatch is stored with status `QUEUED` when the agent is registered.
    Nothing in this version runs it or sends it to the agent.
    """
    return service.dispatch_run(request)


@router.get(
    "/runs/dispatch",
    responses={200: {"model": list[DispatchedRun], "description": "The stored dispatches, newest first."}},
    dependencies=ADMIN,
)
def list_dispatched_runs(service: ServiceDep, limit: Annotated[int, Query(description=_LIMIT_HELP)] = 20):
    """List recent run dispatches issued by the GMA."""
    return service.list_dispatched_runs(limit=limit)


@router.get(
    "/agents",
    responses={200: {"model": list[AgentSnapshot], "description": "Every agent the GMA knows, sorted by `agent_id`."}},
    dependencies=ADMIN,
)
def list_agents(service: ServiceDep):
    """List agents currently registered with the GMA."""
    return service.list_agents()


@router.get(
    "/api/v1/agents",
    responses={200: {"model": list[AgentSnapshot], "description": "Every agent the GMA knows, sorted by `agent_id`."}},
    dependencies=ADMIN,
)
def list_agents_v1(service: ServiceDep):
    """Versioned alias for agent listing."""
    return list_agents(service)


@router.get(
    "/api/v1/agents/{agent_id}",
    responses={200: {"model": AgentSnapshot | None, "description": "The agent, or `null` when no agent has this id."}},
    dependencies=ADMIN,
)
def get_agent_v1(
    agent_id: Annotated[str, Path(description="The `agent_id` the agent registered or reported with.")],
    service: ServiceDep,
):
    """Fetch one registered agent snapshot by id."""
    return service.get_agent(agent_id)


# --------------------------------------------------------------------------- #
# Sources, dataset profiles and aggregate model jobs (GMA_ALLOW_SOURCE_READS)
# --------------------------------------------------------------------------- #


@router.post(
    "/datasets/profile",
    responses={200: {"model": DatasetProfile}, 403: _SOURCE_READS_OFF, 500: _UNKNOWN_SOURCE},
    dependencies=SOURCE_READS,
)
def profile_assimilated_dataset(request: DatasetProfileRequest, service: ServiceDep):
    """Profile an assimilated dataset before aggregate model selection.

    The profile is computed in Python and not stored. `scope_kind` is always
    `assimilated` in the answer.
    """
    return service.profile_assimilated_dataset(request)


@router.post(
    "/api/v1/datasets:profile",
    responses={200: {"model": DatasetProfile}, 403: _SOURCE_READS_OFF, 500: _UNKNOWN_SOURCE},
    dependencies=SOURCE_READS,
)
def profile_assimilated_dataset_v1(request: DatasetProfileRequest, service: ServiceDep):
    """Versioned alias for assimilated dataset profiling."""
    return profile_assimilated_dataset(request, service)


@router.post(
    "/models/run",
    responses={200: {"model": ModelExecutionResponse}, 403: _SOURCE_READS_OFF, 500: _UNKNOWN_SOURCE},
    dependencies=SOURCE_READS,
)
def run_assimilated_model(request: ModelExecutionRequest, service: ServiceDep):
    """Execute an aggregate model run synchronously.

    The run record is kept in the GMA's model-run history. No model is trained:
    the metrics are estimates.
    """
    return service.run_assimilated_model(request)


@router.post(
    "/api/v1/model-jobs",
    status_code=202,
    responses={202: {"model": ModelExecutionResponse}, 403: _SOURCE_READS_OFF, 500: _UNKNOWN_SOURCE},
    dependencies=SOURCE_READS,
)
def run_assimilated_model_v1(request: ModelExecutionRequest, service: ServiceDep):
    """Versioned alias that presents aggregate execution as a model-job API."""
    return run_assimilated_model(request, service)


@router.get(
    "/models/runs",
    responses={200: {"model": list[ModelRunRecord], "description": "The stored runs, newest first."}, 403: _SOURCE_READS_OFF},
    dependencies=SOURCE_READS,
)
def list_model_runs(service: ServiceDep, limit: Annotated[int, Query(description=_LIMIT_HELP)] = 20):
    """List recent aggregate model runs tracked by the GMA."""
    return service.list_model_runs(limit=limit)


@router.get(
    "/api/v1/model-jobs",
    responses={200: {"model": list[ModelRunRecord], "description": "The stored runs, newest first."}, 403: _SOURCE_READS_OFF},
    dependencies=SOURCE_READS,
)
def list_model_runs_v1(service: ServiceDep, limit: Annotated[int, Query(description=_LIMIT_HELP)] = 20):
    """Versioned alias for aggregate model-job listing."""
    return list_model_runs(service, limit=limit)


@router.post(
    "/api/v1/sources",
    responses={
        200: {
            "model": SourceSpec,
            "description": (
                "The stored source definition, with its defaults filled in. Each `connection_ref.options` value is "
                "replaced with `[redacted]`, except `role`, `sslmode` and `connect_timeout`."
            ),
        },
        403: _SOURCE_READS_OFF,
        500: _server_error("The body is not a valid source definition. The handler validates it itself."),
    },
    # The handler takes a plain dict and validates it with TypeAdapter, so
    # FastAPI cannot see the body's shape. This states it for the schema only.
    openapi_extra={"requestBody": {"content": {"application/json": {"schema": _SOURCE_SPEC_SCHEMA}}}},
    dependencies=SOURCE_READS,
)
def register_source(payload: dict[str, Any], service: ServiceDep):
    """Register a reusable source definition for aggregate workflows."""
    source = TypeAdapter(SourceSpec).validate_python(payload)
    return redact_source(service.register_source(source))


@router.get(
    "/api/v1/sources",
    responses={
        200: {
            "model": list[SourceSpec],
            "description": "Every stored source, sorted by `source_id`, with its connection options redacted.",
        },
        403: _SOURCE_READS_OFF,
    },
    dependencies=SOURCE_READS,
)
def list_sources(service: ServiceDep):
    """List all sources registered with the GMA."""
    return [redact_source(source) for source in service.list_sources()]


@router.get(
    "/api/v1/sources/{source_id}",
    responses={
        200: {
            "model": SourceSpec | None,
            "description": "The source, with its connection options redacted, or `null` when no source has this id.",
        },
        403: _SOURCE_READS_OFF,
    },
    dependencies=SOURCE_READS,
)
def get_source(source_id: Annotated[str, _SOURCE_ID], service: ServiceDep):
    """Fetch one source registered with the GMA."""
    source = service.get_source(source_id)
    return redact_source(source) if source is not None else None


@router.post(
    "/api/v1/sources/{source_id}:validate",
    responses={
        200: {"model": SourceValidationResult},
        403: _SOURCE_READS_OFF,
        500: _server_error("No source is registered with this id."),
    },
    dependencies=SOURCE_READS,
)
def validate_source(source_id: Annotated[str, _SOURCE_ID], service: ServiceDep):
    """Run shared source validation for a registered source id."""
    return service.validate_source(source_id)


@router.get(
    "/telemetry/recent",
    responses={200: {"model": list[TelemetryEnvelope], "description": "The received envelopes, newest first."}},
    dependencies=ADMIN,
)
def recent_telemetry(service: ServiceDep, limit: Annotated[int, Query(description=_LIMIT_HELP)] = 20):
    """List recent telemetry envelopes received from registered agents."""
    return service.recent_telemetry(limit=limit)


@router.get(
    "/telemetry/summary",
    responses={200: {"model": list[TelemetrySummary], "description": "One summary per sending agent, sorted by `agent_id`."}},
    dependencies=ADMIN,
)
def telemetry_summary(service: ServiceDep):
    """Return aggregate telemetry rollups by agent."""
    return service.telemetry_summary()


@router.get("/overview", responses={200: {"model": FleetOverview}}, dependencies=ADMIN)
def overview(service: ServiceDep):
    """Return a fleet-level summary of agent and deployment state."""
    return service.overview()


# --------------------------------------------------------------------------- #
# Governance
# --------------------------------------------------------------------------- #


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
_STRICT_CONFLICT = _coded(
    "`conflict`: the GMA has condition packs, and the request's classification is not the one registered under its id."
)


@router.get(
    "/governance/classifications",
    responses={200: {"model": list[DataClassification], "description": "Every classification, sorted by id."}},
    dependencies=ADMIN,
)
def list_classifications(governance: GovernanceDep):
    """List the data classifications this agent can enforce against."""
    return governance.list_classifications()


@router.get(
    "/api/v1/governance/classifications",
    responses={200: {"model": list[DataClassification], "description": "Every classification, sorted by id."}},
    dependencies=ADMIN,
)
def list_classifications_v1(governance: GovernanceDep):
    """Versioned alias for listing data classifications."""
    return list_classifications(governance)


_CLASSIFICATION_RESPONSES = {
    200: {"model": DataClassification, "description": "The stored classification."},
    400: {"model": ErrorDetail, "description": "The id in the path differs from the id in the body."},
    409: _coded("`conflict`: an extension contributes a different classification with this id."),
}


@router.put("/governance/classifications/{classification_id}", responses=_CLASSIFICATION_RESPONSES, dependencies=ADMIN)
def register_classification(
    classification_id: Annotated[str, _CLASSIFICATION_ID], classification: DataClassification, governance: GovernanceDep
):
    """Register or replace one runtime data classification.

    A classification an extension contributes cannot be replaced: a different
    one with its id is refused with 409, and the same one changes nothing.
    """
    if classification.classification_id != classification_id:
        raise HTTPException(
            status_code=400,
            detail=f"path id {classification_id} does not match body id {classification.classification_id}",
        )
    return governance.register_classification(classification)


@router.put("/api/v1/governance/classifications/{classification_id}", responses=_CLASSIFICATION_RESPONSES, dependencies=ADMIN)
def register_classification_v1(
    classification_id: Annotated[str, _CLASSIFICATION_ID], classification: DataClassification, governance: GovernanceDep
):
    """Versioned alias for registering a data classification."""
    return register_classification(classification_id, classification, governance)


_PLAN_RESPONSES = {200: {"model": RestrictionPlan}, 409: _STRICT_CONFLICT, 503: _PLANNER_FAILED}


@router.post("/governance/restrictions:plan", responses=_PLAN_RESPONSES, dependencies=ADMIN)
def plan_restriction(request: RestrictionRequest, governance: GovernanceDep):
    """Ask what protection a request would need, without enforcing anything.

    Nothing is recorded, because nothing was enforced. Use the enforcement
    endpoint when the answer should be acted on and audited.
    """
    return governance.plan(request)


@router.post("/api/v1/governance/restrictions:plan", responses=_PLAN_RESPONSES, dependencies=ADMIN)
def plan_restriction_v1(request: RestrictionRequest, governance: GovernanceDep):
    """Versioned alias for restriction planning."""
    return plan_restriction(request, governance)


_ENFORCE_RESPONSES = {
    200: {"model": GuardDecision, "description": "The Guard's decision. A denial is a 200 too."},
    409: _STRICT_CONFLICT,
}


@router.post("/governance/restrictions:enforce", responses=_ENFORCE_RESPONSES, dependencies=ADMIN)
def enforce_restriction(payload: GuardEnforcementRequest, governance: GovernanceDep):
    """Plan, apply, and record one governed request against a payload.

    The Guard fails closed: when the planner cannot be reached, it denies the
    request and records the denial, so this endpoint still answers 200.
    """
    return governance.enforce(payload.request, payload.payload, correlation_id=payload.correlation_id)


@router.post("/api/v1/governance/restrictions:enforce", responses=_ENFORCE_RESPONSES, dependencies=ADMIN)
def enforce_restriction_v1(payload: GuardEnforcementRequest, governance: GovernanceDep):
    """Versioned alias for guard enforcement."""
    return enforce_restriction(payload, governance)


@router.get("/governance/audit", responses={200: {"model": GuardAuditLog}}, dependencies=ADMIN)
def governance_audit(governance: GovernanceDep, limit: Annotated[int, Query(description=_LIMIT_HELP)] = 50):
    """List recent guard decisions and whether the digest chain still verifies."""
    return {
        "chain_intact": governance.audit_chain_intact(),
        "records": governance.recent_audit(limit=limit),
    }


@router.get("/api/v1/governance/audit", responses={200: {"model": GuardAuditLog}}, dependencies=ADMIN)
def governance_audit_v1(governance: GovernanceDep, limit: Annotated[int, Query(description=_LIMIT_HELP)] = 50):
    """Versioned alias for the guard audit log."""
    return governance_audit(governance, limit=limit)


@router.get("/governance/audit:summary", response_model=AuditSummary, dependencies=ADMIN)
def governance_audit_summary(governance: GovernanceDep) -> AuditSummary:
    """Report whether the GMA's audit chain verifies and how many decisions it holds, without its records.

    The release Guard's decisions are in this chain.
    """
    return AuditSummary(chain_intact=governance.audit_chain_intact(), record_count=governance.audit_record_count())


@router.get("/api/v1/governance/audit:summary", response_model=AuditSummary, dependencies=ADMIN)
def governance_audit_summary_v1(governance: GovernanceDep) -> AuditSummary:
    """Versioned alias for the audit chain summary."""
    return governance_audit_summary(governance)


# --------------------------------------------------------------------------- #
# Federation: sites
# --------------------------------------------------------------------------- #


_SITE_ID = Path(description="The site's id. It must equal `site_id` in the body.")


@router.put(
    "/federation/sites/{site_id}",
    responses={
        200: {"model": SiteRegistration, "description": "The stored enrolment, with its defaults filled in."},
        400: {"model": ErrorDetail, "description": "The id in the path differs from the id in the body."},
    },
    dependencies=ADMIN,
)
def register_site(site_id: Annotated[str, _SITE_ID], registration: SiteRegistration, rounds: RoundsDep):
    """Enrol or update one site in the consortium.

    The enrolment is stored under `site_id`; sending the same id again
    replaces it. The GMA keeps enrolments in memory, unless the application
    that built it passed its own enrolment store. Round planning reads every
    stored enrolment.
    """
    if registration.site_id != site_id:
        raise HTTPException(
            status_code=400, detail=f"path id {site_id} does not match body id {registration.site_id}"
        )
    return rounds.register_site(registration)


@router.put(
    "/api/v1/federation/sites/{site_id}",
    responses={
        200: {"model": SiteRegistration, "description": "The stored enrolment, with its defaults filled in."},
        400: {"model": ErrorDetail, "description": "The id in the path differs from the id in the body."},
    },
    dependencies=ADMIN,
)
def register_site_v1(site_id: Annotated[str, _SITE_ID], registration: SiteRegistration, rounds: RoundsDep):
    """Versioned alias for site enrolment."""
    return register_site(site_id, registration, rounds)


@router.get(
    "/federation/sites",
    responses={200: {"model": list[SiteRegistration], "description": "Every stored enrolment, sorted by `site_id`."}},
    dependencies=ADMIN,
)
def list_sites(rounds: RoundsDep):
    """List enrolled sites."""
    return rounds.list_sites()


@router.get(
    "/api/v1/federation/sites",
    responses={200: {"model": list[SiteRegistration], "description": "Every stored enrolment, sorted by `site_id`."}},
    dependencies=ADMIN,
)
def list_sites_v1(rounds: RoundsDep):
    """Versioned alias for listing enrolled sites."""
    return list_sites(rounds)


_REFRESHED = {
    200: {
        "model": list[SiteRegistration],
        "description": "The enrolments the LMAs answered with, sorted by `site_id`. An LMA that did not answer is left out.",
    }
}


@router.post("/federation/sites:refresh", responses=_REFRESHED, dependencies=ADMIN)
def refresh_sites(directory: DirectoryDep):
    """Enrol each configured LMA from its own registration.

    The GMA calls `GET /api/v1/federation/registration` on every LMA in its
    agent network, at the same time, and stores each answer as that site's
    enrolment, with its current cohort size. The LMA addresses come from the
    GMA's configuration only. An LMA that does not answer, or answers for
    another site, keeps its earlier enrolment. A GMA with no LMA configured
    answers an empty list.
    """
    return directory.refresh()


@router.post("/api/v1/federation/sites:refresh", responses=_REFRESHED, dependencies=ADMIN)
def refresh_sites_v1(directory: DirectoryDep):
    """Versioned alias for refreshing the enrolments from the LMAs."""
    return refresh_sites(directory)


_SUMMARIES = {
    200: {
        "model": list[SiteDirectoryEntry],
        "description": "One entry per configured LMA, sorted by `site_id`. An LMA that did not answer has `reachable: false`.",
    }
}
_INCLUDE = Query(description="`audit` adds each site's audit chain state and length. Leave it out for summaries only.")


@router.get("/federation/sites/summaries", responses=_SUMMARIES, dependencies=ADMIN)
def site_summaries(directory: DirectoryDep, include: Annotated[Literal["audit"] | None, _INCLUDE] = None):
    """Report what each configured site's LMA says about its records.

    Each entry holds the site's summary (counts and the label rate its planner
    permits) and, with `include=audit`, its audit chain state and length. The
    GMA never reads a site's rows. The answer is kept for
    `GMA_SUMMARY_TTL_SECONDS` (default 2).
    """
    return directory.summaries(include_audit=include == "audit")


@router.get("/api/v1/federation/sites/summaries", responses=_SUMMARIES, dependencies=ADMIN)
def site_summaries_v1(directory: DirectoryDep, include: Annotated[Literal["audit"] | None, _INCLUDE] = None):
    """Versioned alias for the site summaries."""
    return site_summaries(directory, include=include)


# --------------------------------------------------------------------------- #
# Federation: site addresses
# --------------------------------------------------------------------------- #


_ADDRESS_SITE_ID = Path(description="The site's id, as in `GMA_SITE_ENDPOINTS` or `GMA_JOINABLE_SITE_ENDPOINTS`.")
_ADDRESS_RESPONSES = {
    200: {"model": SiteAddressState, "description": "The address is held. `source` says where it came from."},
    403: _coded(
        "`not_joinable`: the site is not in `GMA_JOINABLE_SITE_ENDPOINTS` (a GMA with no HTTP network has no joinable "
        "site). `address_differs`: the address differs from the one listed for the site. `forbidden`: the token is "
        "not the admin token."
    ),
    409: _coded("`configured_elsewhere`: the site is in `GMA_SITE_ENDPOINTS` with another address."),
    422: _coded_or_invalid_body(
        "The body is not `{url}`, or `url` is not an http or https base address (`invalid_request`)."
    ),
}


@router.put("/federation/sites/{site_id}/address", responses=_ADDRESS_RESPONSES, dependencies=ADMIN)
def register_site_address(
    site_id: Annotated[str, _ADDRESS_SITE_ID], body: SiteAddressRequest, addresses: AddressesDep
) -> SiteAddressState:
    """Give the GMA the address of a joinable site's LMA, while it runs.

    The GMA accepts only the address `GMA_JOINABLE_SITE_ENDPOINTS` lists for
    the site, and adds it to its agent network, in memory. From then on the
    site refresh, the site summaries and round dispatch call that LMA, with
    the coordinator token in `GMA_LMA_TOKENS`. A configured site at its own
    address is accepted as `configured`; sending a joined site's address again
    changes nothing. The route does not enrol the site: call
    `POST /api/v1/federation/sites:refresh` next. No answer or log line repeats
    the address.
    """
    return addresses.attach(site_id, body.url)


@router.put("/api/v1/federation/sites/{site_id}/address", responses=_ADDRESS_RESPONSES, dependencies=ADMIN)
def register_site_address_v1(
    site_id: Annotated[str, _ADDRESS_SITE_ID], body: SiteAddressRequest, addresses: AddressesDep
) -> SiteAddressState:
    """Versioned alias for giving the GMA a joinable site's address."""
    return register_site_address(site_id, body, addresses)


_REMOVE_ADDRESS_RESPONSES = {
    204: {"description": "The joined address is removed, or the joinable site had not joined."},
    403: _coded("`not_joinable`: the site is not in `GMA_JOINABLE_SITE_ENDPOINTS`. `forbidden`: not the admin token."),
    409: _coded("`configured`: the site's address comes from `GMA_SITE_ENDPOINTS`, so it stays."),
}


@router.delete(
    "/federation/sites/{site_id}/address", status_code=204, responses=_REMOVE_ADDRESS_RESPONSES, dependencies=ADMIN
)
def remove_site_address(site_id: Annotated[str, _ADDRESS_SITE_ID], addresses: AddressesDep) -> Response:
    """Remove a joined site's address from the GMA's agent network.

    The GMA closes its client for that LMA and calls it no more: the site
    refresh and the summaries leave it out, and a round that still selects it
    records a failed result without calling it. Withdraw the site's enrolment
    first (`PUT /api/v1/federation/sites/{site_id}` with `enrolled: false`), so
    the next plan does not select it.
    """
    addresses.detach(site_id)
    return Response(status_code=204)


@router.delete(
    "/api/v1/federation/sites/{site_id}/address",
    status_code=204,
    responses=_REMOVE_ADDRESS_RESPONSES,
    dependencies=ADMIN,
)
def remove_site_address_v1(site_id: Annotated[str, _ADDRESS_SITE_ID], addresses: AddressesDep) -> Response:
    """Versioned alias for removing a joined site's address."""
    return remove_site_address(site_id, addresses)


_ADDRESS_LIST_RESPONSES = {200: {"model": SiteAddressList, "description": "The joinable sites and every held address's source."}}


@router.get("/federation/site-addresses", responses=_ADDRESS_LIST_RESPONSES, dependencies=ADMIN)
def list_site_addresses(addresses: AddressesDep) -> SiteAddressList:
    """List the sites that may join while the GMA runs, and where each address the GMA holds came from.

    `joinable` is `GMA_JOINABLE_SITE_ENDPOINTS`'s sites, joined or not;
    `sites` is every configured or joined site. No address is listed. A GMA
    with no HTTP network answers two empty lists.
    """
    return addresses.listing()


@router.get("/api/v1/federation/site-addresses", responses=_ADDRESS_LIST_RESPONSES, dependencies=ADMIN)
def list_site_addresses_v1(addresses: AddressesDep) -> SiteAddressList:
    """Versioned alias for listing the site addresses."""
    return list_site_addresses(addresses)


# --------------------------------------------------------------------------- #
# Federation: rounds
# --------------------------------------------------------------------------- #


@router.post("/federation/rounds:plan", responses={200: {"model": RoundPlan}, 503: _PLANNER_FAILED}, dependencies=ADMIN)
def plan_round(manifest: RoundManifest, rounds: RoundsDep):
    """Select the sites eligible for a round, without dispatching it.

    The planner (`dagentsc federation round plan`) checks every stored site
    enrolment against the manifest. Nothing is stored.
    """
    return rounds.plan_round(manifest)


@router.post("/api/v1/federation/rounds:plan", responses={200: {"model": RoundPlan}, 503: _PLANNER_FAILED}, dependencies=ADMIN)
def plan_round_v1(manifest: RoundManifest, rounds: RoundsDep):
    """Versioned alias for round planning."""
    return plan_round(manifest, rounds)


_DISPATCH_ERRORS = {
    409: _coded(
        "`conflict`: a round with this id is already stored, or (HTTP engine) the round's model is not "
        "registered or does not match `model_artifact_digest`."
    ),
    503: _PLANNER_FAILED,
}


@router.post("/federation/rounds", responses={200: {"model": RoundRecord}, **_DISPATCH_ERRORS}, dependencies=ADMIN)
def dispatch_round(manifest: RoundManifest, rounds: RoundsDep):
    """Plan a round and, if quorum allows, dispatch its jobs to the selected sites.

    A round id can be used once: a stored id is refused with 409. Without
    quorum the round is stored as `closed` and offered to no site. With the
    HTTP engine (`GMA_ENGINE=http`) each selected site's LMA is offered the
    job, then runs it, up to `GMA_DISPATCH_CONCURRENCY` sites at a time; the
    round holds the sites' answers (`acceptances`), their results, every call
    made (`dispatch`) and the planner's `readiness`, since every result is in.
    """
    return rounds.dispatch_round(manifest)


@router.post(
    "/api/v1/federation/rounds",
    status_code=202,
    responses={202: {"model": RoundRecord}, **_DISPATCH_ERRORS},
    dependencies=ADMIN,
)
def dispatch_round_v1(manifest: RoundManifest, rounds: RoundsDep):
    """Versioned alias for round dispatch."""
    return dispatch_round(manifest, rounds)


@router.get(
    "/federation/rounds",
    responses={200: {"model": list[RoundRecord], "description": "The stored rounds, newest dispatch first."}},
    dependencies=ADMIN,
)
def list_rounds(rounds: RoundsDep, limit: Annotated[int, Query(description=_ROUND_LIMIT_HELP)] = 20):
    """List recent rounds, newest first."""
    return rounds.list_rounds(limit=limit)


@router.get(
    "/api/v1/federation/rounds",
    responses={200: {"model": list[RoundRecord], "description": "The stored rounds, newest dispatch first."}},
    dependencies=ADMIN,
)
def list_rounds_v1(rounds: RoundsDep, limit: Annotated[int, Query(description=_ROUND_LIMIT_HELP)] = 20):
    """Versioned alias for listing rounds."""
    return list_rounds(rounds, limit=limit)


@router.get("/federation/rounds/{round_id}", responses={200: {"model": RoundRecord}, 404: _UNKNOWN_ROUND}, dependencies=ADMIN)
def get_round(round_id: Annotated[str, _ROUND_ID], rounds: RoundsDep):
    """Fetch one round's full lineage."""
    record = rounds.get_round(round_id)
    if record is None:
        raise _unknown_round(round_id)
    return record


@router.get(
    "/api/v1/federation/rounds/{round_id}", responses={200: {"model": RoundRecord}, 404: _UNKNOWN_ROUND}, dependencies=ADMIN
)
def get_round_v1(round_id: Annotated[str, _ROUND_ID], rounds: RoundsDep):
    """Versioned alias for fetching one round."""
    return get_round(round_id, rounds)


_RESULT_ERRORS = {
    400: {"model": ErrorDetail, "description": "`round_id` in the body differs from the round in the path."},
    403: _coded("`forbidden`: the round's plan did not select this site."),
    404: _UNKNOWN_ROUND,
    409: _coded("`conflict`: the round is `aggregated` or `closed`, so it takes no more results."),
}


@router.post(
    "/federation/rounds/{round_id}/results",
    responses={200: {"model": RoundRecord, "description": "The round, with the result stored."}, **_RESULT_ERRORS},
    dependencies=ADMIN,
)
def submit_result(round_id: Annotated[str, _ROUND_ID], result: SiteResult, rounds: RoundsDep):
    """Record one site's returned result against its round.

    The result replaces any earlier result from the same site, and the round's
    status becomes `collecting`. A site the plan did not select is refused with
    403, and a round that was aggregated or closed with 409. The planner is not
    called.
    """
    if result.round_id != round_id:
        raise HTTPException(
            status_code=400, detail=f"path round {round_id} does not match body round {result.round_id}"
        )
    try:
        return rounds.submit_result(result)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/api/v1/federation/rounds/{round_id}/results",
    status_code=202,
    responses={202: {"model": RoundRecord, "description": "The round, with the result stored."}, **_RESULT_ERRORS},
    dependencies=ADMIN,
)
def submit_result_v1(round_id: Annotated[str, _ROUND_ID], result: SiteResult, rounds: RoundsDep):
    """Versioned alias for submitting a site result."""
    return submit_result(round_id, result, rounds)


_READINESS_RESPONSES = {200: {"model": AggregationReadiness}, 404: _UNKNOWN_ROUND, 503: _PLANNER_FAILED}


@router.get("/federation/rounds/{round_id}/readiness", responses=_READINESS_RESPONSES, dependencies=ADMIN)
def round_readiness(round_id: Annotated[str, _ROUND_ID], rounds: RoundsDep):
    """Report whether the returned contributions may be aggregated.

    The planner (`dagentsc federation aggregate readiness`) checks every stored
    result against the plan's selected sites. A result from any other site is
    rejected. Nothing is saved: the aggregate endpoint saves the readiness it
    acts on.
    """
    try:
        return rounds.evaluate_readiness(round_id, save=False)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/v1/federation/rounds/{round_id}/readiness", responses=_READINESS_RESPONSES, dependencies=ADMIN)
def round_readiness_v1(round_id: Annotated[str, _ROUND_ID], rounds: RoundsDep):
    """Versioned alias for aggregation readiness."""
    return round_readiness(round_id, rounds)


_AGGREGATE_RESPONSES = {
    200: {"model": CandidateModel, "description": "The candidate model. It is stored on the round."},
    404: _UNKNOWN_ROUND,
    409: _coded(
        "`aggregation_refused`: the planner does not permit aggregation. The round's updates are then kept until "
        "the round is discarded or closed. With the HTTP engine, the code also means that a stored update is "
        "missing or does not match its digest, or that the candidate an earlier attempt registered does not match "
        "its digest. The engine then deletes the round's updates. `conflict`: the round was discarded, or (HTTP "
        "engine) the candidate's name holds another model, as after a restart that freed a round id the model "
        "store still uses. The engine then deletes the round's updates."
    ),
    503: _PLANNER_FAILED,
}


@router.post("/federation/rounds/{round_id}:aggregate", responses=_AGGREGATE_RESPONSES, dependencies=ADMIN)
def aggregate_round(round_id: Annotated[str, _ROUND_ID], rounds: RoundsDep):
    """Produce a candidate model, if and only if aggregation is permitted.

    A candidate is not a release. It becomes one only after the release gates
    pass and a person approves it. With the HTTP engine, a training round's
    stored updates are combined by the model kind with the planner's weights,
    the candidate is registered as `<version_prefix>+<round_id>`, and the
    round's updates are deleted once the round is stored as `aggregated`. If
    an attempt fails after it registered the candidate, for example on a
    store error, the updates are kept: a retry combines them again and gets
    the same candidate. When the updates are gone after a restart, a retry
    returns the registered candidate, but only if the GMA's round store keeps
    round ids across restarts. The default store in memory does not.
    """
    try:
        return rounds.aggregate(round_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/v1/federation/rounds/{round_id}:aggregate", responses=_AGGREGATE_RESPONSES, dependencies=ADMIN)
def aggregate_round_v1(round_id: Annotated[str, _ROUND_ID], rounds: RoundsDep):
    """Versioned alias for candidate aggregation."""
    return aggregate_round(round_id, rounds)


_UPLOAD_SITE_ID = Path(description="The site whose update this is. A site token may upload only for its own site.")
_UPLOAD_RESPONSES = {
    200: {"model": UpdateReceipt},
    403: _coded("`forbidden`: the plan did not select this site, or the token belongs to another site."),
    404: _UNKNOWN_ROUND,
    409: _coded(
        "`conflict`: the round is not a `dispatched` or `collecting` training round, `job_digest` is not the "
        "plan's `round_digest`, an update from this site is already stored, or the model kind refused the update."
    ),
}


@router.put(
    "/federation/rounds/{round_id}/updates/{site_id}",
    responses=_UPLOAD_RESPONSES,
    dependencies=[Depends(require_scope("site", bind_site="site_id"))],
)
async def upload_update(
    round_id: Annotated[str, _ROUND_ID],
    site_id: Annotated[str, _UPLOAD_SITE_ID],
    upload: UpdateUpload,
    rounds: RoundsDep,
    models: ModelsDep,
    updates: UpdatesDep,
    threads: ThreadsDep,
) -> UpdateReceipt:
    """Store one site's clipped model update for a training round.

    The site's LMA calls it while it runs its job. The update store checks the
    upload against the round as the GMA stored it, and the model kind of the
    round's starting model checks the update's names and values and computes
    its digest, which the answer returns. The site compares it with its own.
    No route reads an update back. Aggregation reads the updates. They are
    deleted once the round is stored as aggregated, when the HTTP engine
    refuses the aggregation (for example because an update is missing or
    changed), or when the round is discarded or closed. A planner refusal of
    the aggregation keeps them. The work runs on the GMA's own upload threads,
    never on the server's, so an upload does not wait for the dispatch that
    caused it.
    """

    def store() -> UpdateReceipt:
        record = rounds.get_round(round_id)
        if record is None:
            raise _unknown_round(round_id)
        if site_id not in record.plan.selected_sites:
            raise NotSelected(f"site {site_id} was not selected for round {round_id}")
        kind_name = models.kind_of(record.manifest.model_version)
        if kind_name is None:
            raise RoundConflict(f"round {round_id} starts from {record.manifest.model_version}, which is not registered")
        try:
            kind = models.kind(kind_name)
        except UnknownModelKind as error:
            raise RoundConflict(f"round {round_id} starts from a model of a kind no extension contributes") from error
        return updates.put(record, site_id, upload, kind)

    return await threads.run(store)


@router.put(
    "/api/v1/federation/rounds/{round_id}/updates/{site_id}",
    responses=_UPLOAD_RESPONSES,
    dependencies=[Depends(require_scope("site", bind_site="site_id"))],
)
async def upload_update_v1(
    round_id: Annotated[str, _ROUND_ID],
    site_id: Annotated[str, _UPLOAD_SITE_ID],
    upload: UpdateUpload,
    rounds: RoundsDep,
    models: ModelsDep,
    updates: UpdatesDep,
    threads: ThreadsDep,
) -> UpdateReceipt:
    """Versioned alias for storing a site's update."""
    return await upload_update(round_id, site_id, upload, rounds, models, updates, threads)


_DISCARD_RESPONSES = {204: {"description": "The round's updates are deleted and the reason is stored."}, 404: _UNKNOWN_ROUND}


@router.post(
    "/federation/rounds/{round_id}:discard",
    status_code=204,
    response_class=Response,
    responses=_DISCARD_RESPONSES,
    dependencies=ADMIN,
)
def discard_round(round_id: Annotated[str, _ROUND_ID], request: DiscardRequest, rounds: RoundsDep) -> Response:
    """Give up a round: delete its stored updates and record why.

    A `dispatched` or `collecting` round becomes `closed` and takes no more
    results or updates. An aggregated round keeps its candidate. The first
    reason is kept in `abandoned_reason`. Call it at the end of every training
    round, so no site's update stays in the GMA's update store.
    """
    try:
        rounds.discard(round_id, request.reason)
    except KeyError as exc:
        raise _unknown_round(round_id) from exc
    return Response(status_code=204)


@router.post(
    "/api/v1/federation/rounds/{round_id}:discard",
    status_code=204,
    response_class=Response,
    responses=_DISCARD_RESPONSES,
    dependencies=ADMIN,
)
def discard_round_v1(round_id: Annotated[str, _ROUND_ID], request: DiscardRequest, rounds: RoundsDep) -> Response:
    """Versioned alias for discarding a round."""
    return discard_round(round_id, request, rounds)


_RELEASE_RESPONSES = {
    200: {
        "model": ReleaseEvaluation,
        "description": "The decision, the release Guard's decision and the candidate's status. The decision is also saved in the round's `decision` field.",
    },
    404: _UNKNOWN_ROUND,
    409: _coded(
        "`conflict`: `candidate_version` is not the round's candidate, the metrics round measured another "
        "version, the round already holds a decision or is closed, or the lifecycle rule refused the decision. "
        "`refusal: not_candidate`: the version is no longer a candidate. `refusal: decision_differs`: an earlier "
        "request stored a decision for this round with the version but not on the round, and the decision "
        "computed now differs from it, for example because another version was released since. The round stays "
        "undecided.",
        LifecycleRefusalDetail,
    ),
    422: _coded_or_invalid_body(
        "The body does not match the request model (FastAPI's validation error, `detail` is a list), or "
        "`invalid_request`: the round flow got gates or metrics in the body, or no `release_requester`."
    ),
    503: _PLANNER_FAILED,
}


@router.post("/federation/rounds/{round_id}:evaluate-release", responses=_RELEASE_RESPONSES, dependencies=ADMIN)
def evaluate_release(round_id: Annotated[str, _ROUND_ID], payload: ReleaseEvaluationRequest, releases: ReleasesDep):
    """Evaluate a round's candidate against the release gates and record the verdict.

    With a `condition_id` (the round flow), the GMA reads the gates from the
    condition pack, the candidate's metrics from `metrics_from_round` and the
    baseline from `baseline_from_round`, runs the `before_release` Guard (its
    decision joins the GMA's audit chain), runs the planner's gates
    (`dagentsc federation release evaluate`) and its lifecycle rule
    (`dagentsc federation model lifecycle`), and stores the decision with the
    model version. Without a `condition_id` (the bare flow), it evaluates the
    gates and metrics in the body, runs no Guard and changes no model version.
    Either way the decision is saved in the round, which becomes `closed`.

    In the round flow the lifecycle rule also reads the decision the version
    already holds. When an earlier request stored it for this round and then
    failed to save it on the round, a retry that computes the same decision
    changes nothing on the version and saves the decision on the round; a
    retry that computes a different one answers 409 with
    `refusal: decision_differs`.
    """
    try:
        return releases.evaluate(round_id, payload)
    except KeyError as exc:
        raise _unknown_round(round_id) from exc


@router.post("/api/v1/federation/rounds/{round_id}:evaluate-release", responses=_RELEASE_RESPONSES, dependencies=ADMIN)
def evaluate_release_v1(round_id: Annotated[str, _ROUND_ID], payload: ReleaseEvaluationRequest, releases: ReleasesDep):
    """Versioned alias for release evaluation."""
    return evaluate_release(round_id, payload, releases)


# --------------------------------------------------------------------------- #
# Model versions
# --------------------------------------------------------------------------- #


_VERSION_LIMIT = Query(ge=1, le=200, description="The most versions to return, newest first. 1 to 200.")


@router.get("/model-versions", response_model=ModelVersionList, dependencies=ADMIN)
def list_model_versions(models: ModelsDep, limit: Annotated[int, _VERSION_LIMIT] = 20) -> ModelVersionList:
    """List the model in use and the newest model versions, without their parameters."""
    return models.list(limit)


@router.get("/api/v1/model-versions", response_model=ModelVersionList, dependencies=ADMIN)
def list_model_versions_v1(models: ModelsDep, limit: Annotated[int, _VERSION_LIMIT] = 20) -> ModelVersionList:
    """Versioned alias for listing model versions."""
    return list_model_versions(models, limit=limit)


_RELEASED_RESPONSES = {
    200: {
        "model": ModelVersion,
        "description": "The released version with its parameters. The `ETag` header is its digest, in quotes.",
    },
    304: {"description": "`If-None-Match` names the released version's digest: it has not changed."},
    404: _coded("`not_found`: no version is released."),
    409: _coded("`conflict`: the stored parameters do not match the stored digest."),
}
_IF_NONE_MATCH = Header(description="The digest of the released version the caller holds, in quotes, as the `ETag` gave it.")


def _matches(if_none_match: str | None, etag: str) -> bool:
    if not if_none_match:
        return False
    tags = [tag.strip() for tag in if_none_match.split(",")]
    return "*" in tags or etag in tags or f"W/{etag}" in tags


@router.get("/model-versions:released", responses=_RELEASED_RESPONSES, dependencies=ADMIN_OR_SITE)
def released_model_version(
    models: ModelsDep, if_none_match: Annotated[str | None, _IF_NONE_MATCH] = None
) -> Response:
    """Return the released model version, the one sites use, with its parameters.

    The digest is recomputed by the model kind before the answer. Send the
    `ETag` back in `If-None-Match` to get 304 while the released version is
    unchanged. A site's token may call it, so an LMA can score with the model
    in use.
    """
    model = models.released(with_params=True)
    if model is None:
        return JSONResponse({"detail": "no model version is released", "code": "not_found"}, status_code=404)
    etag = f'"{model.digest}"'
    if _matches(if_none_match, etag):
        return Response(status_code=304, headers={"ETag": etag})
    return JSONResponse(model.model_dump(mode="json"), headers={"ETag": etag})


@router.get("/api/v1/model-versions:released", responses=_RELEASED_RESPONSES, dependencies=ADMIN_OR_SITE)
def released_model_version_v1(
    models: ModelsDep, if_none_match: Annotated[str | None, _IF_NONE_MATCH] = None
) -> Response:
    """Versioned alias for the released model version."""
    return released_model_version(models, if_none_match=if_none_match)


_VERSION_RESPONSES = {200: {"model": ModelVersion}, 404: _coded("`not_found`: no version has this name.")}


@router.get("/model-versions/{version}", responses=_VERSION_RESPONSES, response_model=ModelVersion, dependencies=ADMIN)
def get_model_version(version: Annotated[str, _VERSION], models: ModelsDep) -> ModelVersion:
    """Fetch one model version, with its status and latest release decision, without its parameters."""
    return models.require(version)


@router.get("/api/v1/model-versions/{version}", responses=_VERSION_RESPONSES, response_model=ModelVersion, dependencies=ADMIN)
def get_model_version_v1(version: Annotated[str, _VERSION], models: ModelsDep) -> ModelVersion:
    """Versioned alias for fetching one model version."""
    return get_model_version(version, models)


_REGISTER_RESPONSES = {
    201: {"model": ModelVersion, "description": "The version was registered."},
    200: {"model": ModelVersion, "description": "The same content was registered before; nothing changed."},
    404: _coded("`not_found`: no extension contributes the model kind."),
    409: _coded(
        "`conflict`: the name holds different contents, the model kind refused the parameters, or the "
        "lifecycle rule refused a release (`refusal: already_released`).",
        LifecycleRefusalDetail,
    ),
    422: _coded_or_invalid_body(
        "The body does not match the request model (FastAPI's validation error, `detail` is a list), or "
        "`invalid_request`: the name is empty or too long, or the parameters are not plain JSON."
    ),
}


@router.put(
    "/model-versions/{version}",
    status_code=201,
    response_model=ModelVersion,
    responses=_REGISTER_RESPONSES,
    dependencies=ADMIN,
)
def register_model_version(
    version: Annotated[str, _VERSION], registration: ModelVersionRegistration, response: Response, models: ModelsDep
) -> ModelVersion:
    """Register a model version under a name the caller chooses.

    The model kind computes the parameters' digest and refuses parameters it
    cannot run. The planner's lifecycle rule decides the status: `released`
    only when asked for and when no version is released, otherwise
    `candidate`. Registering the same content again changes nothing and
    answers 200, so an application can register its versions at every start.
    The answer leaves out the parameters.
    """
    result = models.register(version, registration)
    response.status_code = 201 if result.created else 200
    return result.model


@router.put(
    "/api/v1/model-versions/{version}",
    status_code=201,
    response_model=ModelVersion,
    responses=_REGISTER_RESPONSES,
    dependencies=ADMIN,
)
def register_model_version_v1(
    version: Annotated[str, _VERSION], registration: ModelVersionRegistration, response: Response, models: ModelsDep
) -> ModelVersion:
    """Versioned alias for registering a model version."""
    return register_model_version(version, registration, response, models)


_APPROVE_RESPONSES = {
    200: {"model": ApprovalResult},
    404: _coded("`not_found`: no version has this name."),
    409: _coded(
        "`conflict`: the planner's lifecycle rule refused the approval; `refusal` says why "
        "(`no_decision`, `gates_decided`, `guard_denied`, `metrics_not_from_rounds`, `not_candidate`, "
        "`baseline_changed`). Also when another approval changed the release meanwhile.",
        LifecycleRefusalDetail,
    ),
    503: _PLANNER_FAILED,
}


@router.post("/model-versions/{version}:approve", response_model=ApprovalResult, responses=_APPROVE_RESPONSES, dependencies=ADMIN)
def approve_model_version(version: Annotated[str, _VERSION], models: ModelsDep) -> ApprovalResult:
    """Release a candidate, as the person who approves releases.

    The planner's lifecycle rule checks the version's stored release decision
    in this order: there is one, the gates decided `release`, the release
    Guard permitted it, its metrics came from the study's rounds, the version
    is a candidate, and the version released now is the one the gates compared
    it with. The released version becomes `superseded` in the same step.
    """
    return models.approve(version)


@router.post(
    "/api/v1/model-versions/{version}:approve", response_model=ApprovalResult, responses=_APPROVE_RESPONSES, dependencies=ADMIN
)
def approve_model_version_v1(version: Annotated[str, _VERSION], models: ModelsDep) -> ApprovalResult:
    """Versioned alias for approving a model version."""
    return approve_model_version(version, models)


# --------------------------------------------------------------------------- #
# Extensions
# --------------------------------------------------------------------------- #


@router.get("/extensions", responses={200: {"model": ExtensionSummary}}, dependencies=ADMIN)
def list_extensions(extensions: ExtensionsDep):
    """Describe what registered extensions contribute to this agent."""
    return extensions.describe()


@router.get("/api/v1/extensions", responses={200: {"model": ExtensionSummary}}, dependencies=ADMIN)
def list_extensions_v1(extensions: ExtensionsDep):
    """Versioned alias for the extension summary."""
    return list_extensions(extensions)


__all__ = ["GuardEnforcementRequest", "router"]
