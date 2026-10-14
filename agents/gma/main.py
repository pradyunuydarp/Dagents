"""FastAPI entrypoint for the Global Monitoring Agent."""

from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, Field, TypeAdapter

from agents.common.domain.federation import (
    AggregationReadiness,
    CandidateModel,
    ReleaseDecision,
    ReleaseGate,
    RoundManifest,
    RoundPlan,
    RoundRecord,
    SiteRegistration,
    SiteResult,
)
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
from agents.gma.config import settings
from agents.gma.di import (
    build_aggregation_service,
    build_governance_service,
    build_round_controller,
)
from agents.gma.domain.models import (
    AgentIdentity,
    AgentSnapshot,
    CommandStatus,
    DatasetProfile,
    DatasetProfileRequest,
    DeploymentPlan,
    DeploymentSyncRequest,
    DeploymentSyncResponse,
    DesiredDeploymentRequest,
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


service = build_aggregation_service()
governance = build_governance_service()
rounds = build_round_controller()

app = FastAPI(title=settings.app_name, version="0.1.0")


# OpenAPI metadata. The `responses=` entries below describe what a route
# returns where the route has no response model; they change only the schema
# document, not the response.


class GmaHealth(BaseModel):
    """The GMA's health answer."""

    status: str = Field(description="`ok` whenever the process answers.")
    agent_type: str = Field(description="Always `GMA`.")


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
_PLANNER_FAILED = _server_error("The planner could not be run, or its answer could not be read.")
_ROUND_ID = Path(description="The `round_id` of a round dispatched with `POST /api/v1/federation/rounds`.")
_UNKNOWN_ROUND = {"model": ErrorDetail, "description": "No round has this id."}


@app.get("/health", responses={200: {"model": GmaHealth}})
def health() -> dict[str, str]:
    """Return the raw health payload used by the GMA's internal checks.

    It reads no stored state and does not call the planner.
    """
    return service.health_payload()


@app.get("/api/v1/health", responses={200: {"model": GmaHealth}})
def health_v1() -> dict[str, str]:
    """Versioned alias for the GMA health endpoint."""
    return health()


@app.post("/register", responses={200: {"model": RegisterResponse}})
def register(request: RegisterRequest):
    """Register one LMA with the aggregate control plane.

    The agent is stored in memory under `agent.agent_id`. Registering the same
    id again replaces its identity, scope, version and capabilities and keeps
    its status, heartbeat and bundle fields.
    """
    return service.register(request)


@app.put("/api/v1/agents/{agent_id}/registration", responses={200: {"model": RegisterResponse}})
def register_v1(agent_id: Annotated[str, _AGENT_ID_FROM_PATH], request: RegisterRequest):
    """Versioned registration endpoint keyed by path-level `agent_id`.

    The path id wins over any mismatched id in the request body so route-level
    identity stays authoritative.
    """
    payload = request if request.agent.agent_id == agent_id else request.model_copy(
        update={"agent": AgentIdentity(**{**request.agent.model_dump(), "agent_id": agent_id})}
    )
    return register(payload)


@app.post("/heartbeat", responses={200: {"model": HeartbeatResponse}})
def heartbeat(request: HeartbeatRequest):
    """Ingest one heartbeat update from a registered LMA.

    The heartbeat's status and timestamp are stored on the agent's record. An
    agent that never registered is added, with version `unknown`.
    """
    return service.heartbeat(request)


@app.post("/api/v1/agents/{agent_id}/heartbeats", responses={200: {"model": HeartbeatResponse}})
def heartbeat_v1(agent_id: Annotated[str, _AGENT_ID_FROM_PATH], request: HeartbeatRequest):
    """Versioned heartbeat endpoint keyed by path-level `agent_id`."""
    payload = request if request.agent.agent_id == agent_id else request.model_copy(
        update={"agent": AgentIdentity(**{**request.agent.model_dump(), "agent_id": agent_id})}
    )
    return heartbeat(payload)


@app.post("/telemetry", responses={200: {"model": TelemetryAck}})
def telemetry(request: TelemetryEnvelope):
    """Ingest one telemetry envelope into the aggregate repository.

    The envelope is appended to the GMA's telemetry list. When the sender is
    registered, its record keeps the new ingestion id.
    """
    return service.ingest_telemetry(request)


@app.post("/api/v1/agents/{agent_id}/telemetry", responses={200: {"model": TelemetryAck}})
def telemetry_v1(agent_id: Annotated[str, _AGENT_ID_FROM_PATH], request: TelemetryEnvelope):
    """Versioned telemetry endpoint keyed by path-level `agent_id`."""
    payload = request if request.agent.agent_id == agent_id else request.model_copy(
        update={"agent": AgentIdentity(**{**request.agent.model_dump(), "agent_id": agent_id})}
    )
    return telemetry(payload)


@app.post("/deployments/sync", responses={200: {"model": DeploymentSyncResponse}})
def sync_deployment(request: DeploymentSyncRequest):
    """Compare an LMA's current bundle with the desired deployment plan.

    The reported bundle is stored on the agent's record as its current bundle.
    An agent that never registered is added, with version `unknown`.
    """
    return service.sync_deployment(request)


@app.post("/api/v1/agents/{agent_id}/deployment-sync", responses={200: {"model": DeploymentSyncResponse}})
def sync_deployment_v1(agent_id: Annotated[str, _AGENT_ID_FROM_PATH], request: DeploymentSyncRequest):
    """Versioned deployment-sync endpoint keyed by path-level `agent_id`."""
    payload = request if request.agent.agent_id == agent_id else request.model_copy(
        update={"agent": AgentIdentity(**{**request.agent.model_dump(), "agent_id": agent_id})}
    )
    return sync_deployment(payload)


@app.post("/deployments/plan", responses={200: {"model": DeploymentPlan}})
def plan_deployment(request: DesiredDeploymentRequest):
    """Store the desired deployment state for one LMA.

    One desired deployment is kept per agent; a new one replaces the old. When
    the agent is registered, its record's desired bundle is updated too.
    """
    return service.plan_deployment(request)


@app.put("/api/v1/agents/{agent_id}/desired-deployment", responses={200: {"model": DeploymentPlan}})
def plan_deployment_v1(
    agent_id: Annotated[
        str, Path(description="The agent's id. When `agent_id` in the body differs, this value replaces it.")
    ],
    request: DesiredDeploymentRequest,
):
    """Versioned desired-deployment endpoint keyed by path-level `agent_id`."""
    payload = request if request.agent_id == agent_id else request.model_copy(update={"agent_id": agent_id})
    return plan_deployment(payload)


@app.get(
    "/deployments",
    responses={200: {"model": list[DeploymentPlan], "description": "Every desired deployment, sorted by `agent_id`."}},
)
def list_deployments():
    """List current desired deployment plans known to the GMA."""
    return service.list_deployments()


@app.post("/runs/dispatch", responses={200: {"model": CommandStatus}})
def dispatch_run(request: RunDispatchRequest):
    """Record one aggregate run dispatch for downstream execution systems.

    The dispatch is stored with status `QUEUED` when the agent is registered.
    Nothing in this version runs it or sends it to the agent.
    """
    return service.dispatch_run(request)


@app.get(
    "/runs/dispatch",
    responses={200: {"model": list[DispatchedRun], "description": "The stored dispatches, newest first."}},
)
def list_dispatched_runs(limit: Annotated[int, Query(description=_LIMIT_HELP)] = 20):
    """List recent run dispatches issued by the GMA."""
    return service.list_dispatched_runs(limit=limit)


@app.get(
    "/agents",
    responses={200: {"model": list[AgentSnapshot], "description": "Every agent the GMA knows, sorted by `agent_id`."}},
)
def list_agents():
    """List agents currently registered with the GMA."""
    return service.list_agents()


@app.get(
    "/api/v1/agents",
    responses={200: {"model": list[AgentSnapshot], "description": "Every agent the GMA knows, sorted by `agent_id`."}},
)
def list_agents_v1():
    """Versioned alias for agent listing."""
    return list_agents()


@app.get(
    "/api/v1/agents/{agent_id}",
    responses={200: {"model": AgentSnapshot | None, "description": "The agent, or `null` when no agent has this id."}},
)
def get_agent_v1(agent_id: Annotated[str, Path(description="The `agent_id` the agent registered or reported with.")]):
    """Fetch one registered agent snapshot by id."""
    return service.get_agent(agent_id)


@app.post("/datasets/profile", responses={200: {"model": DatasetProfile}, 500: _UNKNOWN_SOURCE})
def profile_assimilated_dataset(request: DatasetProfileRequest):
    """Profile an assimilated dataset before aggregate model selection.

    The profile is computed in Python and not stored. `scope_kind` is always
    `assimilated` in the answer.
    """
    return service.profile_assimilated_dataset(request)


@app.post("/api/v1/datasets:profile", responses={200: {"model": DatasetProfile}, 500: _UNKNOWN_SOURCE})
def profile_assimilated_dataset_v1(request: DatasetProfileRequest):
    """Versioned alias for assimilated dataset profiling."""
    return profile_assimilated_dataset(request)


@app.post("/models/run", responses={200: {"model": ModelExecutionResponse}, 500: _UNKNOWN_SOURCE})
def run_assimilated_model(request: ModelExecutionRequest):
    """Execute an aggregate model run synchronously.

    The run record is kept in the GMA's model-run history. No model is trained:
    the metrics are estimates.
    """
    return service.run_assimilated_model(request)


@app.post(
    "/api/v1/model-jobs",
    status_code=202,
    responses={202: {"model": ModelExecutionResponse}, 500: _UNKNOWN_SOURCE},
)
def run_assimilated_model_v1(request: ModelExecutionRequest):
    """Versioned alias that presents aggregate execution as a model-job API."""
    return run_assimilated_model(request)


@app.get(
    "/models/runs",
    responses={200: {"model": list[ModelRunRecord], "description": "The stored runs, newest first."}},
)
def list_model_runs(limit: Annotated[int, Query(description=_LIMIT_HELP)] = 20):
    """List recent aggregate model runs tracked by the GMA."""
    return service.list_model_runs(limit=limit)


@app.get(
    "/api/v1/model-jobs",
    responses={200: {"model": list[ModelRunRecord], "description": "The stored runs, newest first."}},
)
def list_model_runs_v1(limit: Annotated[int, Query(description=_LIMIT_HELP)] = 20):
    """Versioned alias for aggregate model-job listing."""
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
    """Register a reusable source definition for aggregate workflows."""
    source = TypeAdapter(SourceSpec).validate_python(payload)
    return service.register_source(source)


@app.get(
    "/api/v1/sources",
    responses={200: {"model": list[SourceSpec], "description": "Every stored source, sorted by `source_id`."}},
)
def list_sources():
    """List all sources registered with the GMA."""
    return service.list_sources()


@app.get(
    "/api/v1/sources/{source_id}",
    responses={200: {"model": SourceSpec | None, "description": "The source, or `null` when no source has this id."}},
)
def get_source(source_id: Annotated[str, _SOURCE_ID]):
    """Fetch one source registered with the GMA."""
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
    "/telemetry/recent",
    responses={200: {"model": list[TelemetryEnvelope], "description": "The received envelopes, newest first."}},
)
def recent_telemetry(limit: Annotated[int, Query(description=_LIMIT_HELP)] = 20):
    """List recent telemetry envelopes received from registered agents."""
    return service.recent_telemetry(limit=limit)


@app.get(
    "/telemetry/summary",
    responses={200: {"model": list[TelemetrySummary], "description": "One summary per sending agent, sorted by `agent_id`."}},
)
def telemetry_summary():
    """Return aggregate telemetry rollups by agent."""
    return service.telemetry_summary()


@app.get("/overview", responses={200: {"model": FleetOverview}})
def overview():
    """Return a fleet-level summary of agent and deployment state."""
    return service.overview()


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


class ReleaseEvaluationRequest(BaseModel):
    """A candidate's measured metrics, with the gates it must clear.

    ``baseline_metrics`` are the current approved model's, needed by any gate
    that asks for an improvement rather than an absolute threshold.
    """

    gates: list[ReleaseGate] = Field(
        default_factory=list,
        description=(
            "The gates to evaluate, in order, usually a condition pack's `release_gates`. With no gates the "
            "decision is `release`."
        ),
    )
    candidate_version: str = Field(
        description=(
            "The candidate's version, as `POST /api/v1/federation/rounds/{round_id}:aggregate` returned it. "
            "It is not checked against the round."
        )
    )
    candidate_metrics: dict[str, float] = Field(
        default_factory=dict,
        description="The candidate's measured metrics, by name. The gates read these values as sent.",
    )
    baseline_metrics: dict[str, float] = Field(
        default_factory=dict,
        description="The approved model's metrics, by name. Only `improves_on_baseline` gates read them.",
    )
    rollback_version: str | None = Field(
        default=None,
        description="The version to keep using if the candidate is not released. Default: the round's `model_version`.",
    )


_CLASSIFICATION_ID = Path(description="The classification's id. It must equal `classification_id` in the body.")


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


_SITE_ID = Path(description="The site's id. It must equal `site_id` in the body.")


@app.put(
    "/federation/sites/{site_id}",
    responses={
        200: {"model": SiteRegistration, "description": "The stored enrolment, with its defaults filled in."},
        400: {"model": ErrorDetail, "description": "The id in the path differs from the id in the body."},
    },
)
def register_site(site_id: Annotated[str, _SITE_ID], registration: SiteRegistration):
    """Enrol or update one site in the consortium.

    The enrolment is stored in memory under `site_id`; sending the same id
    again replaces it. Round planning reads every stored enrolment.
    """
    if registration.site_id != site_id:
        raise HTTPException(
            status_code=400, detail=f"path id {site_id} does not match body id {registration.site_id}"
        )
    return rounds.register_site(registration)


@app.put(
    "/api/v1/federation/sites/{site_id}",
    responses={
        200: {"model": SiteRegistration, "description": "The stored enrolment, with its defaults filled in."},
        400: {"model": ErrorDetail, "description": "The id in the path differs from the id in the body."},
    },
)
def register_site_v1(site_id: Annotated[str, _SITE_ID], registration: SiteRegistration):
    """Versioned alias for site enrolment."""
    return register_site(site_id, registration)


@app.get(
    "/federation/sites",
    responses={200: {"model": list[SiteRegistration], "description": "Every stored enrolment, sorted by `site_id`."}},
)
def list_sites():
    """List enrolled sites."""
    return rounds.list_sites()


@app.get(
    "/api/v1/federation/sites",
    responses={200: {"model": list[SiteRegistration], "description": "Every stored enrolment, sorted by `site_id`."}},
)
def list_sites_v1():
    """Versioned alias for listing enrolled sites."""
    return list_sites()


@app.post("/federation/rounds:plan", responses={200: {"model": RoundPlan}, 500: _PLANNER_FAILED})
def plan_round(manifest: RoundManifest):
    """Select the sites eligible for a round, without dispatching it.

    The planner (`dagentsc federation round plan`) checks every stored site
    enrolment against the manifest. Nothing is stored.
    """
    return rounds.plan_round(manifest)


@app.post("/api/v1/federation/rounds:plan", responses={200: {"model": RoundPlan}, 500: _PLANNER_FAILED})
def plan_round_v1(manifest: RoundManifest):
    """Versioned alias for round planning."""
    return plan_round(manifest)


@app.post("/federation/rounds", responses={200: {"model": RoundRecord}, 500: _PLANNER_FAILED})
def dispatch_round(manifest: RoundManifest):
    """Plan a round and, if quorum allows, offer it to the selected sites.

    The round is stored under `round_id`, replacing any round with that id.
    Without quorum it is stored as `closed` and offered to no site.
    """
    return rounds.dispatch_round(manifest)


@app.post(
    "/api/v1/federation/rounds",
    status_code=202,
    responses={202: {"model": RoundRecord}, 500: _PLANNER_FAILED},
)
def dispatch_round_v1(manifest: RoundManifest):
    """Versioned alias for round dispatch."""
    return dispatch_round(manifest)


@app.get(
    "/federation/rounds",
    responses={200: {"model": list[RoundRecord], "description": "The stored rounds, newest dispatch first."}},
)
def list_rounds(limit: Annotated[int, Query(description=_ROUND_LIMIT_HELP)] = 20):
    """List recent rounds, newest first."""
    return rounds.list_rounds(limit=limit)


@app.get(
    "/api/v1/federation/rounds",
    responses={200: {"model": list[RoundRecord], "description": "The stored rounds, newest dispatch first."}},
)
def list_rounds_v1(limit: Annotated[int, Query(description=_ROUND_LIMIT_HELP)] = 20):
    """Versioned alias for listing rounds."""
    return list_rounds(limit=limit)


@app.get("/federation/rounds/{round_id}", responses={200: {"model": RoundRecord}, 404: _UNKNOWN_ROUND})
def get_round(round_id: Annotated[str, _ROUND_ID]):
    """Fetch one round's full lineage."""
    record = rounds.get_round(round_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"unknown round: {round_id}")
    return record


@app.get("/api/v1/federation/rounds/{round_id}", responses={200: {"model": RoundRecord}, 404: _UNKNOWN_ROUND})
def get_round_v1(round_id: Annotated[str, _ROUND_ID]):
    """Versioned alias for fetching one round."""
    return get_round(round_id)


_RESULT_ERRORS = {
    400: {"model": ErrorDetail, "description": "`round_id` in the body differs from the round in the path."},
    404: _UNKNOWN_ROUND,
}


@app.post(
    "/federation/rounds/{round_id}/results",
    responses={200: {"model": RoundRecord, "description": "The round, with the result stored."}, **_RESULT_ERRORS},
)
def submit_result(round_id: Annotated[str, _ROUND_ID], result: SiteResult):
    """Record one site's returned result against its round.

    The result replaces any earlier result from the same site, and the round's
    status becomes `collecting`. The planner is not called.
    """
    if result.round_id != round_id:
        raise HTTPException(
            status_code=400, detail=f"path round {round_id} does not match body round {result.round_id}"
        )
    try:
        return rounds.submit_result(result)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post(
    "/api/v1/federation/rounds/{round_id}/results",
    status_code=202,
    responses={202: {"model": RoundRecord, "description": "The round, with the result stored."}, **_RESULT_ERRORS},
)
def submit_result_v1(round_id: Annotated[str, _ROUND_ID], result: SiteResult):
    """Versioned alias for submitting a site result."""
    return submit_result(round_id, result)


@app.get(
    "/federation/rounds/{round_id}/readiness",
    responses={200: {"model": AggregationReadiness}, 404: _UNKNOWN_ROUND, 500: _PLANNER_FAILED},
)
def round_readiness(round_id: Annotated[str, _ROUND_ID]):
    """Report whether the returned contributions may be aggregated.

    The planner (`dagentsc federation aggregate readiness`) checks every stored
    result. The answer is also saved in the round's `readiness` field.
    """
    try:
        return rounds.evaluate_readiness(round_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get(
    "/api/v1/federation/rounds/{round_id}/readiness",
    responses={200: {"model": AggregationReadiness}, 404: _UNKNOWN_ROUND, 500: _PLANNER_FAILED},
)
def round_readiness_v1(round_id: Annotated[str, _ROUND_ID]):
    """Versioned alias for aggregation readiness."""
    return round_readiness(round_id)


_AGGREGATE_RESPONSES = {
    200: {"model": CandidateModel, "description": "The candidate model. The GMA does not store it."},
    404: _UNKNOWN_ROUND,
    409: {"model": ErrorDetail, "description": "The planner does not permit aggregation for this round."},
    500: _PLANNER_FAILED,
}


@app.post("/federation/rounds/{round_id}:aggregate", responses=_AGGREGATE_RESPONSES)
def aggregate_round(round_id: Annotated[str, _ROUND_ID]):
    """Produce a candidate model, if and only if aggregation is permitted.

    A candidate is not a release. It becomes one only after the release gates
    pass and a human committee approves it.
    """
    try:
        return rounds.aggregate(round_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/v1/federation/rounds/{round_id}:aggregate", responses=_AGGREGATE_RESPONSES)
def aggregate_round_v1(round_id: Annotated[str, _ROUND_ID]):
    """Versioned alias for candidate aggregation."""
    return aggregate_round(round_id)


_RELEASE_RESPONSES = {
    200: {"model": ReleaseDecision, "description": "The decision. It is also saved in the round's `decision` field."},
    404: _UNKNOWN_ROUND,
    500: _PLANNER_FAILED,
}


@app.post("/federation/rounds/{round_id}:evaluate-release", responses=_RELEASE_RESPONSES)
def evaluate_release(round_id: Annotated[str, _ROUND_ID], payload: ReleaseEvaluationRequest):
    """Evaluate release gates against a candidate and record the verdict.

    The planner (`dagentsc federation release evaluate`) compares the metrics
    in the request with each gate. The decision is saved in the round, which
    becomes `closed`. This endpoint does not run the Guard: a caller that
    needs the `before_release` boundary sends its own request to
    `/governance/restrictions:enforce`.
    """
    record = rounds.get_round(round_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"unknown round: {round_id}")
    candidate = CandidateModel(
        round_id=round_id,
        candidate_version=payload.candidate_version,
        parent_version=record.manifest.model_version,
        aggregation_method=record.manifest.aggregation.kind,
        metrics=payload.candidate_metrics,
    )
    return rounds.evaluate_release(
        round_id,
        payload.gates,
        candidate,
        baseline_metrics=payload.baseline_metrics,
        rollback_version=payload.rollback_version,
    )


@app.post("/api/v1/federation/rounds/{round_id}:evaluate-release", responses=_RELEASE_RESPONSES)
def evaluate_release_v1(round_id: Annotated[str, _ROUND_ID], payload: ReleaseEvaluationRequest):
    """Versioned alias for release evaluation."""
    return evaluate_release(round_id, payload)


@app.get("/extensions", responses={200: {"model": ExtensionSummary}})
def list_extensions():
    """Describe what registered extensions contribute to this agent."""
    return default_registry.describe()


@app.get("/api/v1/extensions", responses={200: {"model": ExtensionSummary}})
def list_extensions_v1():
    """Versioned alias for the extension summary."""
    return list_extensions()
