"""The Local Monitoring Agent's routes.

Every route reads the services of the app it runs in from
``request.app.state.lma`` (an :class:`agents.lma.di.LmaRuntime`), so one
process can serve several LMAs, one per site. Handlers keep the parameter
names ``service``, ``governance``, ``site`` and ``jobs``.

Each route has a short path and an ``/api/v1`` path; the versioned handler,
named ``<short handler>_v1``, calls the short one, so the two cannot answer
differently.

Tokens. With tokens configured, the routes need a bearer token of the scopes
below; without tokens the LMA runs open, as before tokens existed.

| Routes | Scopes |
|---|---|
| `/health` | none |
| `/site`, `/site/summary`, `/governance/audit:summary` | `site`, `coordinator` |
| `/site/profile`, `/site/source:refresh`, `/site/model`, `/site/model:evaluate`, `/governance/restrictions:probe`, `/federation/jobs` | `site` |
| `/federation/registration`, `/federation/jobs:offer`, `/federation/jobs:execute`, `/federation/jobs:run` | `coordinator` |
| every other route | `site` |
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel, Field, TypeAdapter

from agents.common.application.governance_service import GovernanceService
from agents.common.domain.federation import FederatedJob, JobAcceptance, JobRun, SiteRegistration, SiteResult
from agents.common.domain.governance import (
    DataClassification,
    GuardAuditLog,
    GuardDecision,
    RestrictionPlan,
    RestrictionRequest,
)
from agents.common.domain.models import SourceSpec, SourceValidationResult
from agents.common.domain.site import (
    AuditSummary,
    JobLogEntry,
    LocalEvaluation,
    ModelInUse,
    ProbeRequest,
    ProbeResponse,
    SiteInfo,
    SiteSummary,
    SourceState,
)
from agents.common.extensions import ExtensionRegistry
from agents.common.extensions.registry import ExtensionSummary
from agents.common.infrastructure.agent_auth import require_scope
from agents.lma.application.errors import SiteNotConfigured
from agents.lma.application.federation_jobs import FederationJobService
from agents.lma.application.monitoring_service import MonitoringService
from agents.lma.application.site_service import SiteService
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

router = APIRouter()


# --- Services, read from the app the request runs in ---------------------------
# Async, so reading them takes no worker thread.


async def get_service(request: Request) -> MonitoringService:
    return request.app.state.lma.monitoring


async def get_governance(request: Request) -> GovernanceService:
    return request.app.state.lma.governance


async def get_registry(request: Request) -> ExtensionRegistry:
    return request.app.state.lma.registry


_NO_SITE = "this LMA was started without a site; start it with LMA_SITE_ID and LMA_CONDITION_ID to serve one"


async def get_site(request: Request) -> SiteService:
    site = request.app.state.lma.site
    if site is None:
        raise SiteNotConfigured(_NO_SITE)
    return site


async def get_jobs(request: Request) -> FederationJobService:
    jobs = request.app.state.lma.jobs
    if jobs is None:
        raise SiteNotConfigured(_NO_SITE)
    return jobs


ServiceDep = Annotated[MonitoringService, Depends(get_service)]
GovernanceDep = Annotated[GovernanceService, Depends(get_governance)]
RegistryDep = Annotated[ExtensionRegistry, Depends(get_registry)]
SiteDep = Annotated[SiteService, Depends(get_site)]
JobsDep = Annotated[FederationJobService, Depends(get_jobs)]

_SITE_SCOPE = Depends(require_scope("site"))
_SITE_OR_COORDINATOR_SCOPE = Depends(require_scope("site", "coordinator"))
_COORDINATOR_SCOPE = Depends(require_scope("coordinator"))


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


class AgentErrorBody(BaseModel):
    """The body of an error the LMA answers with a code."""

    detail: str = Field(description="What went wrong, in words. Never a host, a token or a connection string.")
    code: str = Field(
        description=(
            "What kind of error it is: `unauthorized`, `forbidden`, `not_found`, `conflict`, `planner_unavailable`, "
            "`store_unavailable`, `store_not_set_up`, `agent_unavailable` or `bad_gateway`."
        )
    )


def _server_error(when: str) -> dict[str, Any]:
    """An unhandled exception: Starlette answers 500 with a plain-text body."""
    return {
        "description": when,
        "content": {"text/plain": {"schema": {"type": "string"}, "example": "Internal Server Error"}},
    }


def _error(description: str) -> dict[str, Any]:
    return {"model": AgentErrorBody, "description": description}


#: The answers every route with a token check can give.
_TOKEN_ERRORS: dict[int | str, dict[str, Any]] = {
    401: _error("Tokens are configured, and the request has no bearer token or one the LMA does not accept (`unauthorized`)."),
    403: _error("The token's scope may not call this route (`forbidden`)."),
}

#: The answers of a site route, besides the token errors.
_SITE_ERRORS: dict[int | str, dict[str, Any]] = {
    **_TOKEN_ERRORS,
    404: _error("The LMA was started without a site (`not_found`)."),
    503: _error("The site's store could not be read (`store_unavailable` or `store_not_set_up`)."),
}

_LIMIT_HELP = (
    "The most entries to return, newest first. There is no bound check: 0 returns every entry, and a "
    "negative number leaves out that many of the oldest entries."
)
_UNKNOWN_SOURCE = _server_error(
    "The records could not be read: `dataset.source_id` names no registered source, a file is missing, or the "
    "source's adapter cannot read it."
)
_SITE_SOURCE_REFUSED = _error(
    "The LMA serves a site, and the request embeds a source definition in `dataset`, or the token's scope may "
    "not call this route (`forbidden`). A site's records come from its configured source."
)
_SOURCE_SPEC_SCHEMA = TypeAdapter(SourceSpec).json_schema(ref_template="#/components/schemas/{model}")
_SOURCE_SPEC_SCHEMA.pop("$defs", None)
_SOURCE_ID = Path(description="The `source_id` the source was registered with.")


@router.get("/health", responses={200: {"model": LmaHealth}})
def health(service: ServiceDep) -> dict[str, object]:
    """Return the raw health payload used by the LMA's internal health checks."""
    return service.health_payload()


@router.get("/api/v1/health", responses={200: {"model": LmaHealth}})
def health_v1(service: ServiceDep) -> dict[str, object]:
    """Versioned alias for the health endpoint used by newer clients."""
    return health(service)


@router.get(
    "/bundles",
    dependencies=[_SITE_SCOPE],
    responses={200: {"model": list[BundleRecord], "description": "The stored bundles, newest deploy first."}, **_TOKEN_ERRORS},
)
def list_bundles(service: ServiceDep):
    """List monitoring bundles currently known to the local agent."""
    return service.list_bundles()


@router.post("/bundles/deploy", dependencies=[_SITE_SCOPE], responses={200: {"model": CommandStatus}, **_TOKEN_ERRORS})
def deploy_bundle(command: DeployBundleCommand, service: ServiceDep):
    """Store or update a bundle that future LMA runs may execute."""
    return service.deploy_bundle(command)


@router.get(
    "/runs",
    dependencies=[_SITE_SCOPE],
    responses={200: {"model": list[RunRecord], "description": "The stored runs, newest first."}, **_TOKEN_ERRORS},
)
def list_runs(service: ServiceDep, limit: Annotated[int, Query(description=_LIMIT_HELP)] = 20):
    """List recent local monitoring runs."""
    return service.list_runs(limit=limit)


_PROFILE_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {"model": DatasetProfile},
    403: _SITE_SOURCE_REFUSED,
    500: _UNKNOWN_SOURCE,
    401: _TOKEN_ERRORS[401],
}


@router.post("/datasets/profile", dependencies=[_SITE_SCOPE], responses=_PROFILE_RESPONSES)
def profile_source_dataset(request: DatasetProfileRequest, service: ServiceDep):
    """Profile source-scoped data before a model run is selected."""
    return service.profile_source_dataset(request)


@router.post("/api/v1/datasets:profile", dependencies=[_SITE_SCOPE], responses=_PROFILE_RESPONSES)
def profile_source_dataset_v1(request: DatasetProfileRequest, service: ServiceDep):
    """Versioned alias for source dataset profiling."""
    return profile_source_dataset(request, service)


_MODEL_RESPONSES: dict[int | str, dict[str, Any]] = {
    403: _SITE_SOURCE_REFUSED,
    500: _UNKNOWN_SOURCE,
    401: _TOKEN_ERRORS[401],
}


@router.post(
    "/models/run", dependencies=[_SITE_SCOPE], response_model=ModelExecutionResponse, responses=_MODEL_RESPONSES
)
def run_source_model(request: ModelExecutionRequest, service: ServiceDep) -> ModelExecutionResponse:
    """Execute a source-scoped model run synchronously."""
    return service.run_source_model(request)


@router.post(
    "/api/v1/model-jobs",
    dependencies=[_SITE_SCOPE],
    response_model=ModelExecutionResponse,
    status_code=202,
    responses=_MODEL_RESPONSES,
)
def run_source_model_v1(request: ModelExecutionRequest, service: ServiceDep) -> ModelExecutionResponse:
    """Versioned alias that presents source execution as a model-job API."""
    return run_source_model(request, service)


@router.get(
    "/models/runs",
    dependencies=[_SITE_SCOPE],
    responses={200: {"model": list[ModelRunRecord], "description": "The stored runs, newest first."}, **_TOKEN_ERRORS},
)
def list_model_runs(service: ServiceDep, limit: Annotated[int, Query(description=_LIMIT_HELP)] = 20):
    """List recent source-level model runs executed by the LMA."""
    return service.list_model_runs(limit=limit)


@router.get(
    "/api/v1/model-jobs",
    dependencies=[_SITE_SCOPE],
    responses={200: {"model": list[ModelRunRecord], "description": "The stored runs, newest first."}, **_TOKEN_ERRORS},
)
def list_model_runs_v1(service: ServiceDep, limit: Annotated[int, Query(description=_LIMIT_HELP)] = 20):
    """Versioned alias for listing source-level model jobs."""
    return list_model_runs(service, limit=limit)


@router.post(
    "/api/v1/sources",
    dependencies=[_SITE_SCOPE],
    responses={
        200: {
            "model": SourceSpec,
            "description": (
                "The stored source definition, with its defaults filled in. The value of every connection option "
                "except `role`, `sslmode` and `connect_timeout` is replaced with `[redacted]`."
            ),
        },
        403: _error(
            "The LMA serves a site, and the definition is not an inline source, or sets a connection option "
            "other than `rows` or `documents`; or the token's scope may not call this route (`forbidden`)."
        ),
        500: _server_error("The body is not a valid source definition. The handler validates it itself."),
        401: _TOKEN_ERRORS[401],
    },
    # The handler takes a plain dict and validates it with TypeAdapter, so
    # FastAPI cannot see the body's shape. This states it for the schema only.
    openapi_extra={"requestBody": {"content": {"application/json": {"schema": _SOURCE_SPEC_SCHEMA}}}},
)
def register_source(payload: dict[str, Any], service: ServiceDep):
    """Validate and register a reusable source definition at the agent edge."""
    source = TypeAdapter(SourceSpec).validate_python(payload)
    return service.register_source(source)


@router.get(
    "/api/v1/sources",
    dependencies=[_SITE_SCOPE],
    responses={
        200: {
            "model": list[SourceSpec],
            "description": "Every stored source, sorted by `source_id`, with connection options redacted.",
        },
        **_TOKEN_ERRORS,
    },
)
def list_sources(service: ServiceDep):
    """List all sources registered with this LMA instance."""
    return service.list_sources()


@router.get(
    "/api/v1/sources/{source_id}",
    dependencies=[_SITE_SCOPE],
    responses={
        200: {
            "model": SourceSpec | None,
            "description": "The source with its connection options redacted, or `null` when no source has this id.",
        },
        **_TOKEN_ERRORS,
    },
)
def get_source(source_id: Annotated[str, _SOURCE_ID], service: ServiceDep):
    """Fetch one source definition registered with this LMA instance."""
    return service.get_source(source_id)


@router.post(
    "/api/v1/sources/{source_id}:validate",
    dependencies=[_SITE_SCOPE],
    responses={
        200: {"model": SourceValidationResult},
        500: _server_error("No source is registered with this id."),
        **_TOKEN_ERRORS,
    },
)
def validate_source(source_id: Annotated[str, _SOURCE_ID], service: ServiceDep):
    """Run shared source validation for a registered source id."""
    return service.validate_source(source_id)


@router.get(
    "/telemetry",
    dependencies=[_SITE_SCOPE],
    responses={
        200: {"model": list[TelemetryEnvelope], "description": "The published telemetry, newest first."},
        **_TOKEN_ERRORS,
    },
)
def list_telemetry(service: ServiceDep, limit: Annotated[int, Query(description=_LIMIT_HELP)] = 20):
    """List recent telemetry envelopes emitted by local monitoring runs."""
    return service.recent_telemetry(limit=limit)


@router.post("/run", dependencies=[_SITE_SCOPE], response_model=RunExecutionResponse, responses=_TOKEN_ERRORS)
def run_monitoring(request: RunRequest, service: ServiceDep) -> RunExecutionResponse:
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
_PLANNER_FAILED = _error("The planner could not be run, or its answer could not be read (`planner_unavailable`).")
_STRICT_REFUSED = _error(
    "The LMA serves a site, and the request's classification is not the one registered under its id, or no "
    "classification has that id (`conflict`)."
)


@router.get(
    "/governance/classifications",
    dependencies=[_SITE_SCOPE],
    responses={200: {"model": list[DataClassification], "description": "Every classification, sorted by id."}, **_TOKEN_ERRORS},
)
def list_classifications(governance: GovernanceDep):
    """List the data classifications this agent can enforce against."""
    return governance.list_classifications()


@router.get(
    "/api/v1/governance/classifications",
    dependencies=[_SITE_SCOPE],
    responses={200: {"model": list[DataClassification], "description": "Every classification, sorted by id."}, **_TOKEN_ERRORS},
)
def list_classifications_v1(governance: GovernanceDep):
    """Versioned alias for listing data classifications."""
    return list_classifications(governance)


_REGISTER_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {"model": DataClassification, "description": "The stored classification."},
    400: {"model": ErrorDetail, "description": "The id in the path differs from the id in the body."},
    409: _error("An extension contributes a different classification with this id (`conflict`)."),
    **_TOKEN_ERRORS,
}


@router.put("/governance/classifications/{classification_id}", dependencies=[_SITE_SCOPE], responses=_REGISTER_RESPONSES)
def register_classification(
    classification_id: Annotated[str, _CLASSIFICATION_ID], classification: DataClassification, governance: GovernanceDep
):
    """Register or replace one runtime data classification.

    A classification an extension contributes cannot be replaced: a different
    one with its id answers 409, and the same one changes nothing.
    """
    if classification.classification_id != classification_id:
        raise HTTPException(
            status_code=400,
            detail=f"path id {classification_id} does not match body id {classification.classification_id}",
        )
    return governance.register_classification(classification)


@router.put(
    "/api/v1/governance/classifications/{classification_id}", dependencies=[_SITE_SCOPE], responses=_REGISTER_RESPONSES
)
def register_classification_v1(
    classification_id: Annotated[str, _CLASSIFICATION_ID], classification: DataClassification, governance: GovernanceDep
):
    """Versioned alias for registering a data classification."""
    return register_classification(classification_id, classification, governance)


_PLAN_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {"model": RestrictionPlan},
    409: _STRICT_REFUSED,
    503: _PLANNER_FAILED,
    **_TOKEN_ERRORS,
}


@router.post("/governance/restrictions:plan", dependencies=[_SITE_SCOPE], responses=_PLAN_RESPONSES)
def plan_restriction(request: RestrictionRequest, governance: GovernanceDep):
    """Ask what protection a request would need, without enforcing anything.

    Nothing is recorded, because nothing was enforced. Use the enforcement
    endpoint when the answer should be acted on and audited.
    """
    return governance.plan(request)


@router.post("/api/v1/governance/restrictions:plan", dependencies=[_SITE_SCOPE], responses=_PLAN_RESPONSES)
def plan_restriction_v1(request: RestrictionRequest, governance: GovernanceDep):
    """Versioned alias for restriction planning."""
    return plan_restriction(request, governance)


_ENFORCE_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {"model": GuardDecision, "description": "The Guard's decision. A denial is a 200 too."},
    409: _STRICT_REFUSED,
    **_TOKEN_ERRORS,
}


@router.post("/governance/restrictions:enforce", dependencies=[_SITE_SCOPE], responses=_ENFORCE_RESPONSES)
def enforce_restriction(payload: GuardEnforcementRequest, governance: GovernanceDep):
    """Plan, apply, and record one governed request against a payload.

    The Guard fails closed: when the planner cannot be reached, it denies the
    request and records the denial, so this endpoint still answers 200.
    """
    return governance.enforce(payload.request, payload.payload, correlation_id=payload.correlation_id)


@router.post("/api/v1/governance/restrictions:enforce", dependencies=[_SITE_SCOPE], responses=_ENFORCE_RESPONSES)
def enforce_restriction_v1(payload: GuardEnforcementRequest, governance: GovernanceDep):
    """Versioned alias for guard enforcement."""
    return enforce_restriction(payload, governance)


@router.get("/governance/audit", dependencies=[_SITE_SCOPE], responses={200: {"model": GuardAuditLog}, **_TOKEN_ERRORS})
def governance_audit(governance: GovernanceDep, limit: Annotated[int, Query(description=_LIMIT_HELP)] = 50):
    """List recent guard decisions and whether the digest chain still verifies."""
    return {
        "chain_intact": governance.audit_chain_intact(),
        "records": governance.recent_audit(limit=limit),
    }


@router.get(
    "/api/v1/governance/audit", dependencies=[_SITE_SCOPE], responses={200: {"model": GuardAuditLog}, **_TOKEN_ERRORS}
)
def governance_audit_v1(governance: GovernanceDep, limit: Annotated[int, Query(description=_LIMIT_HELP)] = 50):
    """Versioned alias for the guard audit log."""
    return governance_audit(governance, limit=limit)


@router.get("/extensions", dependencies=[_SITE_SCOPE], responses={200: {"model": ExtensionSummary}, **_TOKEN_ERRORS})
def list_extensions(registry: RegistryDep):
    """Describe what registered extensions contribute to this agent."""
    return registry.describe()


@router.get(
    "/api/v1/extensions", dependencies=[_SITE_SCOPE], responses={200: {"model": ExtensionSummary}, **_TOKEN_ERRORS}
)
def list_extensions_v1(registry: RegistryDep):
    """Versioned alias for the extension summary."""
    return list_extensions(registry)


# --- The site ------------------------------------------------------------------


@router.get("/site", dependencies=[_SITE_OR_COORDINATOR_SCOPE], response_model=SiteInfo, responses=_SITE_ERRORS)
def site_info(site: SiteDep) -> SiteInfo:
    """Describe the site this LMA serves: its condition, policy, source and record count.

    The source is named by its kind, location and role, never by a host or a
    connection string.
    """
    return site.info()


@router.get("/api/v1/site", dependencies=[_SITE_OR_COORDINATOR_SCOPE], response_model=SiteInfo, responses=_SITE_ERRORS)
def site_info_v1(site: SiteDep) -> SiteInfo:
    """Versioned alias for the site description."""
    return site_info(site)


@router.get(
    "/site/summary", dependencies=[_SITE_OR_COORDINATOR_SCOPE], response_model=SiteSummary, responses=_SITE_ERRORS
)
def site_summary(site: SiteDep) -> SiteSummary:
    """Report the site's counts, and its label rate only when the site's planner permits it.

    The planner decides on a `before_send` table request for the label at the
    labelled count. Nothing is recorded in the audit chain. When the planner
    cannot be run, the rate is withheld.
    """
    return site.summary()


@router.get(
    "/api/v1/site/summary", dependencies=[_SITE_OR_COORDINATOR_SCOPE], response_model=SiteSummary, responses=_SITE_ERRORS
)
def site_summary_v1(site: SiteDep) -> SiteSummary:
    """Versioned alias for the site summary."""
    return site_summary(site)


_PROFILE_DESCRIPTION = "The running profile, as `RunningProfile.snapshot()`: `record_count`, `tracked_fields` and per-field statistics."


@router.get(
    "/site/profile",
    dependencies=[_SITE_SCOPE],
    response_model=dict[str, Any],
    responses={200: {"description": _PROFILE_DESCRIPTION}, **_SITE_ERRORS},
)
def site_profile(site: SiteDep) -> dict[str, Any]:
    """Return the running profile of the site's records, for the site's own staff.

    The profile is rebuilt from the records when the source's version changes.
    It holds single-record minimums and maximums, so only the `site` scope may
    read it.
    """
    return site.profile()


@router.get(
    "/api/v1/site/profile",
    dependencies=[_SITE_SCOPE],
    response_model=dict[str, Any],
    responses={200: {"description": _PROFILE_DESCRIPTION}, **_SITE_ERRORS},
)
def site_profile_v1(site: SiteDep) -> dict[str, Any]:
    """Versioned alias for the site profile."""
    return site_profile(site)


@router.post("/site/source:refresh", dependencies=[_SITE_SCOPE], response_model=SourceState, responses=_SITE_ERRORS)
def refresh_site_source(site: SiteDep) -> SourceState:
    """Ask the site's source for its version now, and read the records again if it changed.

    Call it after writing to the site's store, so the next summary and profile
    include the change without waiting for the staleness window.
    """
    return site.refresh()


@router.post(
    "/api/v1/site/source:refresh", dependencies=[_SITE_SCOPE], response_model=SourceState, responses=_SITE_ERRORS
)
def refresh_site_source_v1(site: SiteDep) -> SourceState:
    """Versioned alias for refreshing the site's source."""
    return refresh_site_source(site)


_MODEL_ERRORS: dict[int | str, dict[str, Any]] = {
    **_SITE_ERRORS,
    404: _error("The LMA was started without a site, or the GMA has no released model (`not_found`)."),
    409: _error("The released model's parameters do not match its digest, or no model kind of this site runs it (`conflict`)."),
    502: _error("The GMA answered with an error (`bad_gateway`)."),
    503: _error("The GMA did not answer or is not configured (`agent_unavailable`), or the site's store could not be read."),
}


@router.get("/site/model", dependencies=[_SITE_SCOPE], response_model=ModelInUse, responses=_MODEL_ERRORS)
def site_model(site: SiteDep) -> ModelInUse:
    """Return the released model, as this LMA reads it from the GMA, after its model kind checked the digest."""
    return site.model_in_use()


@router.get("/api/v1/site/model", dependencies=[_SITE_SCOPE], response_model=ModelInUse, responses=_MODEL_ERRORS)
def site_model_v1(site: SiteDep) -> ModelInUse:
    """Versioned alias for the site's model in use."""
    return site_model(site)


@router.post("/site/model:evaluate", dependencies=[_SITE_SCOPE], response_model=LocalEvaluation, responses=_MODEL_ERRORS)
def evaluate_site_model(site: SiteDep) -> LocalEvaluation:
    """Measure the released model on the site's own labelled records. The records stay at the site."""
    return site.evaluate_model()


@router.post(
    "/api/v1/site/model:evaluate", dependencies=[_SITE_SCOPE], response_model=LocalEvaluation, responses=_MODEL_ERRORS
)
def evaluate_site_model_v1(site: SiteDep) -> LocalEvaluation:
    """Versioned alias for evaluating the site's model in use."""
    return evaluate_site_model(site)


@router.post(
    "/governance/restrictions:probe", dependencies=[_SITE_SCOPE], response_model=ProbeResponse, responses=_SITE_ERRORS
)
def probe_restriction(request: ProbeRequest, site: SiteDep) -> ProbeResponse:
    """Run one request through the site's Guard over the site's first records, and report what it would release.

    The LMA adds the site's classification and approved purposes. The decision
    is recorded in the site's audit chain. The answer holds the released count
    and field names, never the records.
    """
    return site.probe(request)


@router.post(
    "/api/v1/governance/restrictions:probe",
    dependencies=[_SITE_SCOPE],
    response_model=ProbeResponse,
    responses=_SITE_ERRORS,
)
def probe_restriction_v1(request: ProbeRequest, site: SiteDep) -> ProbeResponse:
    """Versioned alias for the Guard probe."""
    return probe_restriction(request, site)


@router.get(
    "/governance/audit:summary",
    dependencies=[_SITE_OR_COORDINATOR_SCOPE],
    response_model=AuditSummary,
    responses=_SITE_ERRORS,
)
def governance_audit_summary(site: SiteDep) -> AuditSummary:
    """Report whether the site's audit chain verifies and how many decisions it holds, without its records."""
    return site.audit_summary()


@router.get(
    "/api/v1/governance/audit:summary",
    dependencies=[_SITE_OR_COORDINATOR_SCOPE],
    response_model=AuditSummary,
    responses=_SITE_ERRORS,
)
def governance_audit_summary_v1(site: SiteDep) -> AuditSummary:
    """Versioned alias for the audit chain summary."""
    return governance_audit_summary(site)


# --- Federated jobs ------------------------------------------------------------


@router.get(
    "/federation/registration", dependencies=[_COORDINATOR_SCOPE], response_model=SiteRegistration, responses=_SITE_ERRORS
)
def federation_registration(site: SiteDep) -> SiteRegistration:
    """Return how the site enrols in a study, with its current record count as the cohort size."""
    return site.registration()


@router.get(
    "/api/v1/federation/registration",
    dependencies=[_COORDINATOR_SCOPE],
    response_model=SiteRegistration,
    responses=_SITE_ERRORS,
)
def federation_registration_v1(site: SiteDep) -> SiteRegistration:
    """Versioned alias for the site's registration."""
    return federation_registration(site)


_OFFER_ERRORS: dict[int | str, dict[str, Any]] = {
    **_SITE_ERRORS,
    409: _error("The site was already offered a job for this round (`conflict`)."),
}


@router.post("/federation/jobs:offer", dependencies=[_COORDINATOR_SCOPE], response_model=JobAcceptance, responses=_OFFER_ERRORS)
def offer_job(job: FederatedJob, jobs: JobsDep) -> JobAcceptance:
    """Let the site check a job before it reads any data, and accept or refuse it.

    The site checks the job's site, round and condition, its task, its model
    and the model's digests, the code digest, and the round digest it computes
    with its own planner; then the feature contract, the approved fields and
    the `before_train` Guard. A refusal answers 200 with `accepted` false and
    the reason, and is recorded in the job log.
    """
    return jobs.offer(job)


@router.post(
    "/api/v1/federation/jobs:offer", dependencies=[_COORDINATOR_SCOPE], response_model=JobAcceptance, responses=_OFFER_ERRORS
)
def offer_job_v1(job: FederatedJob, jobs: JobsDep) -> JobAcceptance:
    """Versioned alias for offering a job."""
    return offer_job(job, jobs)


_EXECUTE_ERRORS: dict[int | str, dict[str, Any]] = {
    **_SITE_ERRORS,
    409: _error("The site did not accept this job, ran it already, or was sent a different job (`conflict`)."),
}


@router.post(
    "/federation/jobs:execute", dependencies=[_COORDINATOR_SCOPE], response_model=SiteResult, responses=_EXECUTE_ERRORS
)
def execute_job(job: FederatedJob, jobs: JobsDep) -> SiteResult:
    """Run a job the site accepted, on the Guard's payload, and return what the Guard lets leave.

    A training job's clipped update goes to the GMA's update store; the result
    carries only its norm and digest. A refusal or a local failure is a result
    too, with `refusal_reason`. Each accepted job runs once.
    """
    return jobs.execute(job)


@router.post(
    "/api/v1/federation/jobs:execute",
    dependencies=[_COORDINATOR_SCOPE],
    response_model=SiteResult,
    responses=_EXECUTE_ERRORS,
)
def execute_job_v1(job: FederatedJob, jobs: JobsDep) -> SiteResult:
    """Versioned alias for executing a job."""
    return execute_job(job, jobs)


_RUN_ERRORS: dict[int | str, dict[str, Any]] = {
    **_SITE_ERRORS,
    409: _error("The site was already offered a job for this round (`conflict`)."),
}


@router.post("/federation/jobs:run", dependencies=[_COORDINATOR_SCOPE], response_model=JobRun, responses=_RUN_ERRORS)
def run_job(job: FederatedJob, jobs: JobsDep) -> JobRun:
    """Offer a job and, when the site accepts it, run it, in one call.

    The site takes the same two steps as `jobs:offer` then `jobs:execute`:
    it checks the job and asks its `before_train` Guard, and runs the job
    only when it accepted it. The answer holds the acceptance and, for an
    accepted job, the result; a refused job has no result. The GMA's HTTP
    engine sends each site one `jobs:run` call per round.
    """
    return jobs.run(job)


@router.post(
    "/api/v1/federation/jobs:run", dependencies=[_COORDINATOR_SCOPE], response_model=JobRun, responses=_RUN_ERRORS
)
def run_job_v1(job: FederatedJob, jobs: JobsDep) -> JobRun:
    """Versioned alias for running a job."""
    return run_job(job, jobs)


_JOB_LIMIT = Query(ge=1, le=200, description="The most entries to return, newest first. 1 to 200.")


@router.get("/federation/jobs", dependencies=[_SITE_SCOPE], response_model=list[JobLogEntry], responses=_SITE_ERRORS)
def list_jobs(jobs: JobsDep, limit: Annotated[int, _JOB_LIMIT] = 20) -> list[JobLogEntry]:
    """List the jobs the site refused or ran, newest first, as its job log recorded them."""
    return jobs.recent(limit)


@router.get(
    "/api/v1/federation/jobs", dependencies=[_SITE_SCOPE], response_model=list[JobLogEntry], responses=_SITE_ERRORS
)
def list_jobs_v1(jobs: JobsDep, limit: Annotated[int, _JOB_LIMIT] = 20) -> list[JobLogEntry]:
    """Versioned alias for the site's job log."""
    return list_jobs(jobs, limit=limit)
