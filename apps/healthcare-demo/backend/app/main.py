"""FastAPI entrypoint for the Dagents healthcare stroke-triage demo.

This app uses the Dagents framework; it is not part of it. It contributes a
feature contract, a data classification, a condition pack, a seed rule and a
model encoding. The framework supplies validation, restriction planning, Guard
enforcement, round planning, quorum, release gates and workload compilation.

The records are real, de-identified records from the International Stroke
Trial (1991-1996), grouped by hospital. Each hospital's records are in its own
store: its own schema and role in Postgres, or its own in-memory store when no
database is configured. Nothing this app returns is clinical advice.

Building the app reads no records and opens no database connection. The
stores are opened on first use and closed when the app shuts down.

``HEALTHCARE_DEMO_ROLE`` chooses what this process runs (see
``app.services.roles``). ``combined``, the default, runs every hospital and
the coordinator here. In a split run, ``lma`` runs one hospital and ``app``
runs the study through the Dagents GMA service and sends each hospital's pages
to that hospital's LMA. The public API is the same in every role.
"""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Annotated, Any, AsyncIterator, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from agents.common.domain.governance import KyuAttribute, Requester, RestrictionRequest
from agents.common.extensions import default_registry
from agents.common.infrastructure.charts import ChartsUnavailableError
from agents.common.infrastructure.dagents_runner import PlannerUnavailableError

from app.api.app_header import APP_HEADER, AppHeader
from app.api.errors import UNEXPECTED_ERROR_MESSAGE, UnhandledErrors
from app.api.coordinator_routes import build_coordinator_app, internal_listener
from app.api.deploy_routes import build_deploy_router
from app.api.limits import BodySizeLimit, HourlyCap, RateLimiter, SingleFlight, forwarded_for_entries, retry_after
from app.api.role_paths import OnlyPaths, is_hospital_path, lma_serves
from app.api.site_routes import build_site_router
from app.core.config import settings
from app.domain import stroke_rule
from app.domain.conditions import (
    PLANNED_CONDITIONS,
    STROKE_APPROVED_PURPOSES,
    STROKE_CLASSIFICATION,
    STROKE_CONDITION,
    STROKE_FEATURE_CONTRACT,
    STROKE_FIELD_DOMAINS,
)
from app.domain.dataset import UnknownSiteError
from app.domain.fhir import map_bundle
from app.extension import install
from app.models import (
    IDEMPOTENCY_KEY_MAX_LENGTH,
    IDEMPOTENCY_KEY_PATTERN,
    EncounterEntry,
    ErrorResponse,
    FhirIngestRequest,
    GuardProbeRequest,
    GuardProbeResponse,
    OutcomeRequest,
    PilotRequest,
    TriageRequest,
    ValidationErrorResponse,
    WorklistRequest,
    entry_form_fields,
)
from app.services.charts_service import ChartNotAvailable, ChartService, UnknownChartError
from app.services.consortium import ApprovalRefused, Consortium
from app.services.framework_client import CoreServiceClient, FrameworkClient
from app.services.hospital import EntryLimitReached, Hospital
from app.services.remote import RemoteServiceUnavailable
from app.services.deployment import build_hospital_deployer, check_deployer
from app.services.roles import build_app_consortium, build_site_node, check_role
from app.services.site_store import (
    IdempotencyKeyReused,
    OutcomeAlreadyRecorded,
    RecordRejected,
    StoreNotSetUpError,
    StoreUnavailableError,
    UnknownEncounterError,
    UnknownModelError,
    not_set_up_errors,
)

try:
    import psycopg
except ModuleNotFoundError:  # pragma: no cover - psycopg is in requirements.txt
    psycopg = None  # type: ignore[assignment]

logger = logging.getLogger("healthcare_demo")

#: The message a visitor sees when a store cannot be reached or a database
#: operation fails. The database error itself names the host and user, so it
#: is logged, never returned.
STORE_UNAVAILABLE_MESSAGE = "The hospital records cannot be reached right now. Try again in a minute."

#: The message a visitor sees when the database lacks a schema, table, role or
#: grant this version of the app needs: the migrations have not run yet.
DATABASE_NOT_SET_UP_MESSAGE = (
    "The demo database is not set up for this version of the app yet, so the hospital records cannot be "
    "shown. Try again later."
)

#: The message a visitor sees when the planner cannot be run.
PLANNER_UNAVAILABLE_MESSAGE = "The planner is not available right now, so the pilot cannot run. Try again in a minute."

OUTCOME_ALREADY_RECORDED_MESSAGE = "This record already has an outcome. It cannot be changed."

#: The requester and purpose of every Guard probe. A probe body takes no free
#: text, because the hospital's audit chain records both and every visitor can
#: read it.
PROBE_REQUESTER_ID = "demo-operator"
PROBE_PURPOSE = STROKE_APPROVED_PURPOSES[0]

#: The model versions ``GET /api/v1/models`` lists: the newest, besides the
#: model in use. Every pilot registers several.
MODELS_LISTED = 20

#: What the data-entry form asks of everyone who uses it.
ENTRY_NOTICE = (
    "Do not enter real patient information. This is a public demo: anything entered here is stored "
    "in the hospital's demo store and is visible to every visitor."
)

PILOT_BUSY_MESSAGE = "A pilot is already running. Try again when it finishes."

#: What a pilot request is told when the flight is held, by who holds it. A
#: pilot and the addition of a hospital share ``pilot_flight``.
PILOT_BUSY_MESSAGES = {
    "pilot": PILOT_BUSY_MESSAGE,
    "deployment": "A hospital is being added. Run the pilot when it has joined.",
}

if settings.register_extension:
    install()

#: ``combined``, ``lma`` or ``app`` (see ``app.services.roles``).
ROLE = check_role(settings)
# Adding a hospital starts a process or a container. The app refuses to start
# with a setting that would let a visitor of a public demo do that.
check_deployer(settings)

consortium: Any
if ROLE == "lma":
    # One hospital. Its pages and its round jobs run here.
    consortium = build_site_node(settings)
elif ROLE == "app":
    # The study, run through the Dagents GMA. Each hospital is its own LMA.
    consortium = build_app_consortium(settings)
else:
    consortium = Consortium(
        database_url=settings.database_url,
        secure_aggregation_threshold=settings.secure_aggregation_threshold,
        max_entered_per_site=settings.max_entered_per_site,
    )
framework = FrameworkClient(settings)
chart_service = ChartService()

#: Per-client limits on the POST endpoints. Tests replace them with
#: ``rate_limits.configure``.
rate_limits = RateLimiter(
    {
        "entries": settings.rate_limit_entries_per_minute,
        "probes": settings.rate_limit_probes_per_minute,
        "pilots": settings.rate_limit_pilots_per_minute,
        "approvals": settings.rate_limit_approvals_per_minute,
    },
    forwarded_for_hops=settings.client_proxy_hops(),
)
pilot_flight = SingleFlight()

#: Caps on Guard probes per hospital and on pilots in any 60 minutes, for
#: every visitor together. Tests replace them with ``hourly_caps.configure``.
hourly_caps = HourlyCap({"probes": settings.probes_per_site_per_hour, "pilots": settings.pilots_per_hour})


def _over_hourly_cap(wait: float, action: str) -> HTTPException:
    """The 429 for an action beyond its hourly cap."""
    header, minutes = retry_after(wait)
    return HTTPException(
        status_code=429,
        detail=f"The demo's hourly limit for {action} is reached. Try again in {minutes}.",
        headers={"Retry-After": header},
    )


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    """Start the internal listener in the ``app`` role, and close everything when the app shuts down.

    On shutdown the internal listener stops, then a hospital process this app
    started while it ran, and then every store's database connection and the
    forwarding client are closed.
    """
    try:
        async with AsyncExitStack() as stack:
            if internal_app is not None:
                await stack.enter_async_context(
                    internal_listener(internal_app, settings.internal_host, settings.internal_port)
                )
            yield
    finally:
        try:
            # A hospital process this app started stops with it. A container keeps running.
            deployer.close()
        except Exception:  # noqa: BLE001 - the stores must still close
            logger.exception("a hospital this app started could not be stopped")
        consortium.close()
        forwarder = getattr(application.state, "forwarder", None)
        if forwarder is not None:
            await forwarder.aclose()


app = FastAPI(
    title=settings.app_name,
    version="0.3.0",
    description=(
        "A Dagents demo app for federated stroke triage, on de-identified records from the International "
        "Stroke Trial. Nothing here is a validated clinical model or clinical advice."
    ),
    lifespan=lifespan,
)


def _install_forwarding() -> None:
    """In the ``app`` role, send each hospital-specific request to that hospital's LMA.

    Installed first, so the forwarding sits inside the body limit, the CORS
    middleware and the app header, and its answers carry their headers. If the
    forwarding module is missing, hospital-specific paths answer 404 here
    instead of reaching routes that need a hospital in this process.
    """
    try:
        from app.api.forward import install_forwarding
    except ModuleNotFoundError as error:
        if error.name != "app.api.forward":
            raise
        logger.warning("app.api.forward is missing, so hospital pages answer 404 in the app role")
        app.add_middleware(OnlyPaths, serves=lambda path: not is_hospital_path(path))
        return
    install_forwarding(app, settings, rate_limits)


if ROLE == "lma":
    app.add_middleware(OnlyPaths, serves=lma_serves)
elif ROLE == "app":
    _install_forwarding()

#: Adds a hospital while the demo runs. It has no applier, and says why, unless
#: ``HEALTHCARE_DEMO_DEPLOYER`` turns it on in the ``app`` role. Tests replace it.
deployer = build_hospital_deployer(
    settings,
    consortium=consortium,
    forwarder=getattr(app.state, "forwarder", None),
    gma=consortium.controller if ROLE == "app" else None,
    core=CoreServiceClient(settings.core_service_url, timeout=settings.upstream_timeout_seconds),
)

app.add_middleware(BodySizeLimit, max_bytes=settings.max_body_bytes)
# Inside the CORS middleware and the marker, so the 500 it sends for an
# unhandled error carries both. Starlette's own 500 comes from outside them.
app.add_middleware(UnhandledErrors)
# Outside the body limit, so every response carries the CORS headers, including
# a 413, a 429 or a 500. The published frontend is on a different origin, and a
# response without them looks to the browser like a network error.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins(),
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Retry-After", "ETag", APP_HEADER],
)
# Added last, so it is the outermost layer and every response the app sends
# carries X-Dagents-App: a 413 from the body limit, a CORS preflight and the
# app's own 503. The frontend retries a 502, 503 or 504 without it, which comes
# from the platform while the container starts, and shows one with it at once.
app.add_middleware(AppHeader)


# --- Errors -----------------------------------------------------------------------------


def _short(value: object, limit: int = 100) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _not_set_up(request: Request, exc: Exception) -> JSONResponse:
    logger.error(
        "the database is not set up for this version of the app on %s %s (apply the migrations, then seed): %s",
        request.method,
        request.url.path,
        exc,
    )
    return JSONResponse(status_code=503, content={"detail": DATABASE_NOT_SET_UP_MESSAGE})


@app.exception_handler(StoreUnavailableError)
def store_unavailable(request: Request, exc: StoreUnavailableError) -> JSONResponse:
    """A store that cannot be reached is a 503 with a plain message.

    The app never falls back to other data. ``/api/v1/health`` reads no store,
    so the platform's health check keeps the service up and this message
    reachable. A database without this version's tables gets its own message.
    """
    if isinstance(exc, StoreNotSetUpError):
        return _not_set_up(request, exc)
    logger.warning("store unavailable on %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(status_code=503, content={"detail": STORE_UNAVAILABLE_MESSAGE})


if psycopg is not None:

    @app.exception_handler(psycopg.Error)
    def database_error(request: Request, exc: Exception) -> JSONResponse:
        """Any other database error is a 503 with a plain message, never a 500 without CORS headers."""
        if isinstance(exc, not_set_up_errors()):
            return _not_set_up(request, exc)
        logger.error("database error on %s %s: %s: %s", request.method, request.url.path, type(exc).__name__, exc)
        return JSONResponse(status_code=503, content={"detail": STORE_UNAVAILABLE_MESSAGE})


@app.exception_handler(RemoteServiceUnavailable)
def remote_unavailable(request: Request, exc: RemoteServiceUnavailable) -> JSONResponse:
    """Another process of a split run cannot be reached: a 503 that names it, never its address."""
    logger.warning("remote service unavailable on %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(status_code=503, content={"detail": exc.public_message})


@app.exception_handler(PlannerUnavailableError)
def planner_unavailable(request: Request, exc: PlannerUnavailableError) -> JSONResponse:
    logger.error("planner unavailable on %s %s: %s", request.method, request.url.path, exc)
    return JSONResponse(status_code=503, content={"detail": PLANNER_UNAVAILABLE_MESSAGE})


@app.exception_handler(OutcomeAlreadyRecorded)
def outcome_recorded(_: Request, exc: OutcomeAlreadyRecorded) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": OUTCOME_ALREADY_RECORDED_MESSAGE})


@app.exception_handler(RecordRejected)
def record_rejected(_: Request, exc: RecordRejected) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"detail": "The record was not saved. Correct the fields listed.", "errors": exc.errors},
    )


@app.exception_handler(IdempotencyKeyReused)
def key_reused(_: Request, exc: IdempotencyKeyReused) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={"detail": "This Idempotency-Key was already used for a different record. Send a new key."},
    )


@app.exception_handler(EntryLimitReached)
def entry_limit(_: Request, exc: EntryLimitReached) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": f"{exc}. No more records can be entered here."})


@app.exception_handler(ApprovalRefused)
def approval_refused(_: Request, exc: ApprovalRefused) -> JSONResponse:
    return JSONResponse(status_code=409, content={"detail": f"The model cannot be approved: {exc}."})


@app.exception_handler(UnknownSiteError)
def unknown_site(_: Request, exc: UnknownSiteError) -> JSONResponse:
    site = exc.args[0] if exc.args else ""
    return JSONResponse(status_code=404, content={"detail": f"There is no hospital with the id {_short(site)!r}."})


@app.exception_handler(UnknownEncounterError)
def unknown_encounter(_: Request, exc: UnknownEncounterError) -> JSONResponse:
    encounter = exc.args[0] if exc.args else ""
    return JSONResponse(
        status_code=404, content={"detail": f"This hospital has no record with the id {_short(encounter)!r}."}
    )


@app.exception_handler(UnknownModelError)
def unknown_model(_: Request, exc: UnknownModelError) -> JSONResponse:
    version = exc.args[0] if exc.args else ""
    return JSONResponse(status_code=404, content={"detail": f"There is no model version {_short(version)!r}."})


#: What a value that does not match its pattern must look like.
PATTERN_MESSAGES = {
    "idempotency-key": "must be 1-100 letters, digits or the characters . _ : -",
}


#: Plain messages for pydantic's error types, so a form can show them next to
#: the field. Ranges and choices come from the field's own constraints.
def _validation_message(error: dict[str, Any], *, entry: bool = False) -> str:
    kind = error.get("type", "")
    context = error.get("ctx") or {}
    field = str(error["loc"][-1]) if error.get("loc") else ""
    domain = STROKE_FIELD_DOMAINS.get(field)
    if kind == "missing":
        return "is required"
    if kind == "extra_forbidden":
        # A data entry names the record's fields; any other body is a request.
        return "not a field of the record" if entry else "not a field of this request"
    if kind == "literal_error":
        return f"must be one of {context.get('expected', '').replace(' or ', ', ')}".replace("'", "")
    if kind in ("bool_type", "bool_parsing"):
        return "must be true or false"
    if kind in ("int_type", "int_parsing", "int_from_float"):
        return "must be a whole number"
    if kind in ("float_type", "float_parsing", "finite_number"):
        return "must be a number"
    if kind in ("greater_than_equal", "less_than_equal", "greater_than", "less_than"):
        if domain is not None and domain.minimum is not None and domain.maximum is not None:
            return f"must be between {domain.minimum:g} and {domain.maximum:g}"
    if kind == "string_pattern_mismatch":
        return PATTERN_MESSAGES.get(field.lower(), "has characters that are not allowed")
    if kind == "string_too_long":
        return f"must be at most {context.get('max_length')} characters"
    if kind == "json_invalid":
        return "is not valid JSON"
    return str(error.get("msg", "is not valid"))


def _field_name(location: tuple[Any, ...]) -> str:
    if not location:
        return "body"
    if location[0] == "header" and len(location) > 1:
        return "-".join(part.capitalize() for part in str(location[1]).split("-"))
    if location[0] in ("body", "query", "path") and len(location) > 1:
        return ".".join(str(part) for part in location[1:])
    return str(location[0])


@app.exception_handler(RequestValidationError)
def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
    """A 422 that names each field: ``{"detail": ..., "errors": {field: reason}}``."""
    errors: dict[str, str] = {}
    entry = request.method == "POST" and request.url.path.endswith("/encounters")
    for error in exc.errors():
        location = tuple(error.get("loc") or ())
        name = "body" if error.get("type") == "json_invalid" else _field_name(location)
        errors.setdefault(name, _validation_message(error, entry=entry))
    return JSONResponse(status_code=422, content={"detail": "Some fields are not valid.", "errors": errors})


def _hospital(site_id: str) -> Hospital:
    return consortium.hospital(site_id)


def _idempotency_key(
    key: str | None = Header(
        default=None,
        alias="Idempotency-Key",
        max_length=IDEMPOTENCY_KEY_MAX_LENGTH,
        pattern=IDEMPOTENCY_KEY_PATTERN,
        description="Optional. Repeating a key with the same record returns the first entry and adds nothing.",
    ),
) -> str | None:
    return key


# --- Parameters and error responses, for the OpenAPI document ------------------------------
# These add descriptions only. Validation and the handlers' answers do not change.

SiteId = Annotated[
    str, Path(description="The hospital's id, as `GET /api/v1/hospitals` lists it, such as `uk-134`.")
]
ChartTheme = Annotated[
    Literal["light", "dark"],
    Query(description="The colours to draw the chart in: `light` or `dark`. The data is the same."),
]


def _errors(*statuses: tuple[int, str]) -> dict[int | str, dict[str, Any]]:
    """Error responses for the OpenAPI document, as ``(status, meaning)`` pairs.

    The exception handlers above write these bodies: a 422 names each field,
    every other error is one message. Declaring 422 also replaces FastAPI's
    default 422 schema, which is not the shape this app sends.
    """
    return {
        status: {"model": ValidationErrorResponse if status == 422 else ErrorResponse, "description": meaning}
        for status, meaning in statuses
    }


NOT_A_HOSPITAL = (404, "No hospital has this id.")
NOT_VALID = (422, "A parameter or a body field is not valid. The errors field names each one.")
TOO_MANY = (429, "Too many requests from this client. Wait for the seconds in the Retry-After header.")
STORE_DOWN = (
    503,
    "Only with a database: a hospital's store cannot be reached, or the database is not set up for this "
    "version of the app.",
)
SVG_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {"description": "The chart, as an SVG document.", "content": {"image/svg+xml": {}}},
    304: {"description": "The chart has not changed since the ETag sent in If-None-Match. No body."},
}
IF_NONE_MATCH = {
    "parameters": [
        {
            "name": "If-None-Match",
            "in": "header",
            "required": False,
            "description": "The `ETag` of a copy you hold. If the chart has not changed, the answer is 304 with no body.",
            "schema": {"type": "string"},
        }
    ]
}


# --- Health and descriptions ----------------------------------------------------------------


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness. Reads no store."""
    return settings.as_health_payload()


@app.get("/api/v1/health")
def health_v1() -> dict[str, str]:
    """Versioned alias for the health endpoint."""
    return health()


@app.get("/api/v1/overview", responses=_errors(STORE_DOWN))
def overview() -> dict[str, Any]:
    """The consortium, the numbers each hospital reports, and the release gates."""
    return consortium.overview()


@app.get("/api/v1/conditions")
def conditions() -> dict[str, Any]:
    """The condition this app implements, the conditions it plans, its contract and classification."""
    return {
        "implemented": [STROKE_CONDITION.model_dump(mode="json")],
        "planned": PLANNED_CONDITIONS,
        "feature_contract": STROKE_FEATURE_CONTRACT.model_dump(mode="json"),
        "classification": STROKE_CLASSIFICATION.model_dump(mode="json"),
    }


@app.get("/api/v1/extension")
def extension() -> dict[str, Any]:
    """What this app contributes to the framework, and what each side owns."""
    return {
        "registry": default_registry.describe(),
        "boundary": {
            "framework_owns": [
                "source and schema validation",
                "data-quality rule evaluation",
                "restriction planning (Ethical-Restriction Rails)",
                "Guard enforcement and audit",
                "federated round planning, quorum and aggregation readiness",
                "release gate evaluation",
                "pipeline DAG planning and Kubernetes workload compilation",
            ],
            "app_owns": [
                "the stroke feature contract and its units",
                "which fields are sensitive and under which regulations",
                "the seed outcome rule and the federated model's encoding",
                "FHIR mapping into the contract",
                "the intended-use statement and its limitations",
                "the release gates this condition requires",
            ],
        },
    }


# --- Scoring without a store ---------------------------------------------------------------


@app.post("/api/v1/triage:assess", responses=_errors(NOT_VALID))
def assess(request: TriageRequest) -> dict[str, Any]:
    """Score one record with the seed rule, with the basis for the score."""
    return stroke_rule.assess(request.record).as_dict()


@app.post("/api/v1/triage:worklist", responses=_errors(NOT_VALID))
def worklist(request: WorklistRequest) -> dict[str, Any]:
    """Rank records by the seed rule's score. Nothing is removed from the list."""
    return {"worklist": stroke_rule.rank_worklist(request.records)}


@app.post("/api/v1/ingest:fhir", responses=_errors(NOT_VALID))
def ingest_fhir(request: FhirIngestRequest) -> dict[str, Any]:
    """Map a FHIR-shaped bundle into the stroke feature contract, reporting codes it does not know."""
    results = map_bundle(request.bundle)
    return {
        "mapped": [result.as_dict() for result in results],
        "unmapped_codes": sorted({code for result in results for code in result.unmapped}),
        "record_count": len(results),
    }


# --- Hospitals ----------------------------------------------------------------------------


@app.get("/api/v1/hospitals", responses=_errors(STORE_DOWN))
def hospitals() -> list[dict[str, Any]]:
    """What each hospital reports about its records: counts and an outcome rate."""
    return [consortium.hospitals[site.site_id].summary() for site in consortium.sites]


@app.get("/api/v1/hospitals/{site_id}", responses=_errors(NOT_A_HOSPITAL, NOT_VALID, STORE_DOWN))
def hospital_dashboard(site_id: SiteId) -> dict[str, Any]:
    """One hospital's own view: its store, statistics, local metrics and what it sent."""
    return _hospital(site_id).dashboard()


@app.get("/api/v1/hospitals/{site_id}/worklist", responses=_errors(NOT_A_HOSPITAL, NOT_VALID, STORE_DOWN))
def hospital_worklist(
    site_id: SiteId,
    limit: int = Query(default=12, ge=1, le=200, description="How many records to return, highest risk first."),
) -> dict[str, Any]:
    """One hospital's records ranked by the model in use. The list stays at the hospital."""
    return {"site_id": site_id, "worklist": _hospital(site_id).worklist(limit)}


@app.get("/api/v1/hospitals/{site_id}/thresholds", responses=_errors(NOT_A_HOSPITAL, NOT_VALID, STORE_DOWN))
def hospital_thresholds(site_id: SiteId) -> dict[str, Any]:
    """Sensitivity, specificity and alerts at a range of alert thresholds, at one hospital."""
    return {"site_id": site_id, "sweep": _hospital(site_id).threshold_sweep()}


@app.get("/api/v1/hospitals/{site_id}/audit", responses=_errors(NOT_A_HOSPITAL, NOT_VALID, STORE_DOWN))
def hospital_audit(
    site_id: SiteId,
    limit: int = Query(default=50, ge=1, le=500, description="How many audit records to return, newest first."),
) -> dict[str, Any]:
    """One hospital's Guard decisions, newest first, and whether its audit chain verifies."""
    return _hospital(site_id).audit_trail(limit)


@app.get("/api/v1/hospitals/{site_id}/encounters", responses=_errors(NOT_A_HOSPITAL, NOT_VALID, STORE_DOWN))
def hospital_encounters(
    site_id: SiteId,
    origin: Literal["entered"] = Query(
        default="entered",
        description="Which records to list. Only `entered`, the records entered through the data-entry form.",
    ),
    limit: int = Query(default=20, ge=1, le=300, description="How many records to return, newest first."),
) -> dict[str, Any]:
    """Records entered at one hospital, newest first, each with its assessment."""
    hospital = _hospital(site_id)
    return {
        "site_id": site_id,
        "origin": origin,
        "encounters": hospital.entered(limit),
        "counts": hospital.counts(),
        "max_entered": hospital.max_entered,
    }


@app.get("/api/v1/hospitals/{site_id}/entry-form", responses=_errors(NOT_A_HOSPITAL, NOT_VALID, STORE_DOWN))
def hospital_entry_form(site_id: SiteId) -> dict[str, Any]:
    """The data-entry form, built from the field domains. The browser only renders it."""
    hospital = _hospital(site_id)
    counts = hospital.counts()
    return {
        "site_id": site_id,
        "display_name": hospital.display_name,
        "fields": entry_form_fields(),
        "max_entered": hospital.max_entered,
        "entered_count": counts["entered"],
        "remaining": max(hospital.max_entered - counts["entered"], 0),
        "idempotency": {
            "header": "Idempotency-Key",
            "max_length": IDEMPOTENCY_KEY_MAX_LENGTH,
            "pattern": IDEMPOTENCY_KEY_PATTERN,
        },
        "notice": ENTRY_NOTICE,
    }


@app.post(
    "/api/v1/hospitals/{site_id}/encounters",
    status_code=201,
    dependencies=[Depends(rate_limits.dependency("entries"))],
    responses=_errors(
        NOT_A_HOSPITAL,
        (
            409,
            "The Idempotency-Key was already used for a different record, or the hospital already holds the "
            "most entered records the demo accepts.",
        ),
        NOT_VALID,
        TOO_MANY,
        STORE_DOWN,
    ),
)
def hospital_enter(
    site_id: SiteId,
    entry: EncounterEntry,  # type: ignore[valid-type]
    idempotency_key: str | None = Depends(_idempotency_key),
) -> dict[str, Any]:
    """Store a new record at one hospital, as that hospital, and update its running statistics.

    Returns ``{record, assessment, counts, profile}``. The next round, worklist,
    chart and probe at this hospital include the record.
    """
    return _hospital(site_id).enter(entry.model_dump(), client_request_id=idempotency_key)


@app.post(
    "/api/v1/hospitals/{site_id}/encounters/{encounter_id}/outcome",
    dependencies=[Depends(rate_limits.dependency("entries"))],
    responses=_errors(
        (404, "No hospital has this id, or the hospital has no record with this id."),
        (409, "The record already has a different outcome. An outcome is recorded once."),
        (422, "The body is not valid, or the record came from the dataset: only entered records take an outcome."),
        TOO_MANY,
        STORE_DOWN,
    ),
)
def hospital_outcome(
    site_id: SiteId,
    encounter_id: Annotated[
        str,
        Path(description="The id of a record entered at this hospital, such as `ch-245-e1`, from the entry's answer."),
    ],
    request: OutcomeRequest,
) -> dict[str, Any]:
    """Record the six-month outcome of a record entered at this hospital.

    Returns ``{record, counts, profile}``. The next pilot trains and evaluates
    on the labelled record.
    """
    return _hospital(site_id).set_outcome(encounter_id, request.poor_outcome_6m)


# --- Charts ---------------------------------------------------------------------------------


def _svg_response(request: Request, svg: str, etag: str) -> Response:
    headers = {
        "ETag": etag,
        # Revalidate every time: the data behind a chart changes when records
        # are entered or a pilot runs, and the ETag makes an unchanged chart a
        # 304.
        "Cache-Control": "no-cache",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
    }
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(content=svg, media_type="image/svg+xml", headers=headers)


def _draw(request: Request, draw: Any, theme: str) -> Response:
    try:
        rendered = draw()
    except UnknownChartError as error:
        raise HTTPException(status_code=404, detail=f"There is no chart called {_short(error.args[0])!r}.") from None
    except ChartNotAvailable as error:
        raise HTTPException(status_code=404, detail=error.reason) from None
    except ChartsUnavailableError:
        logger.error("charts cannot be drawn: matplotlib is not installed")
        raise HTTPException(status_code=503, detail="Charts cannot be drawn on this server right now.") from None
    return _svg_response(request, rendered.svg, rendered.etag(theme))


@app.get("/api/v1/charts", responses=_errors(STORE_DOWN))
def consortium_charts() -> dict[str, Any]:
    """The consortium charts, from site summaries and the last pilot. Never a 4xx."""
    return {"charts": chart_service.consortium_list(consortium)}


@app.get(
    "/api/v1/charts/{chart_id}.svg",
    response_class=Response,
    responses={
        **SVG_RESPONSES,
        **_errors(
            (404, "No chart has this id, or the chart needs a pilot that has not run yet."),
            NOT_VALID,
            (503, "Charts cannot be drawn on this server (matplotlib is missing), or a store cannot be reached."),
        ),
    },
    openapi_extra=IF_NONE_MATCH,
)
def consortium_chart_svg(
    request: Request,
    chart_id: Annotated[
        str, Path(description="The chart's id, as `GET /api/v1/charts` lists it, such as `hospital-records`.")
    ],
    theme: ChartTheme = "light",
) -> Response:
    """One consortium chart as SVG. 404 for a chart that needs a pilot that has not run."""
    return _draw(request, lambda: chart_service.consortium_svg(consortium, chart_id, theme), theme)


@app.get("/api/v1/hospitals/{site_id}/charts", responses=_errors(NOT_A_HOSPITAL, NOT_VALID, STORE_DOWN))
def hospital_charts(site_id: SiteId) -> dict[str, Any]:
    """One hospital's charts, from its own records. Shown only in that hospital's view."""
    return {"site_id": site_id, "charts": chart_service.site_list(_hospital(site_id))}


@app.get(
    "/api/v1/hospitals/{site_id}/charts/{chart_id}.svg",
    response_class=Response,
    responses={
        **SVG_RESPONSES,
        **_errors(
            (404, "No hospital has this id, no chart has this id, or the hospital's store holds no records."),
            NOT_VALID,
            (503, "Charts cannot be drawn on this server (matplotlib is missing), or the store cannot be reached."),
        ),
    },
    openapi_extra=IF_NONE_MATCH,
)
def hospital_chart_svg(
    request: Request,
    site_id: SiteId,
    chart_id: Annotated[
        str,
        Path(description="The chart's id, as `GET /api/v1/hospitals/{site_id}/charts` lists it, such as `systolic-bp`."),
    ],
    theme: ChartTheme = "light",
) -> Response:
    """One hospital chart as SVG."""
    hospital = _hospital(site_id)
    return _draw(request, lambda: chart_service.site_svg(hospital, chart_id, theme), theme)


# --- Governance, the pilot and models --------------------------------------------------------


@app.post(
    "/api/v1/governance:probe",
    response_model=GuardProbeResponse,
    dependencies=[Depends(rate_limits.dependency("probes"))],
    responses=_errors(
        NOT_A_HOSPITAL,
        NOT_VALID,
        (
            429,
            "Too many probes from this client, or the hourly cap on probes at this hospital is reached. Wait for "
            "the seconds in the Retry-After header.",
        ),
        STORE_DOWN,
    ),
)
def governance_probe(request: GuardProbeRequest) -> dict[str, Any]:
    """Run one request through a hospital's Guard and report what it would release.

    The decision is a lookup in the typed planner, so changing the requester's
    verification, the boundary or the granularity changes it. The Guard runs at
    the chosen hospital (the first by default), over that hospital's first
    records, and writes the decision to that hospital's audit chain. The
    requester and the purpose are fixed here, so no visitor's text reaches the
    chain.

    The coordinator's page calls this, so the answer holds no record: it
    counts the released records and names their fields
    (:class:`GuardProbeResponse`).
    """
    hospital = _hospital(request.site_id) if request.site_id else consortium.hospitals[consortium.sites[0].site_id]
    # Counted after the hospital is known, so an unknown id is a 404 and adds no key.
    wait = hourly_caps.take("probes", hospital.site_id)
    if wait > 0:
        raise _over_hourly_cap(wait, f"Guard probes at {hospital.display_name}")
    restriction = RestrictionRequest(
        request_id=f"probe:{hospital.site_id}:{request.boundary}:{request.granularity}",
        boundary=request.boundary,  # type: ignore[arg-type]
        requester=Requester(
            requester_id=PROBE_REQUESTER_ID,
            requester_kind=request.requester_kind,  # type: ignore[arg-type]
            affiliation="stroke-triage-consortium",
            stated_purpose=PROBE_PURPOSE,
            compliance_history=request.compliance_history,
            attributes=[
                KyuAttribute(attribute_id="verified_identity", verified=request.verified),
                KyuAttribute(attribute_id="signed_dua", verified=request.verified),
            ],
        ),
        classification=STROKE_CLASSIFICATION,
        requested_fields=request.requested_fields,
        granularity=request.granularity,  # type: ignore[arg-type]
        cohort_size=request.cohort_size,
        declared_purpose=PROBE_PURPOSE,
        approved_purposes=STROKE_APPROVED_PURPOSES,
    )
    decision = hospital.guard_probe(restriction, record_limit=request.record_limit)
    return {"site_id": hospital.site_id, **decision}


@app.post(
    "/api/v1/pilot:run",
    dependencies=[Depends(rate_limits.dependency("pilots"))],
    responses=_errors(
        (409, "A pilot is already running, or a hospital is being added. One of them runs at a time."),
        NOT_VALID,
        (
            429,
            "Too many pilots from this client, or the hourly cap on pilots is reached. Wait for the seconds in "
            "the Retry-After header.",
        ),
        (503, "The planner cannot be run, or, with a database, a store cannot be reached."),
    ),
)
def run_pilot(request: PilotRequest) -> Any:
    """Run the federated pilot across every hospital and return its evidence.

    Analytics, then evaluation of the model in use, then training rounds, then
    validation of the candidate, then the release Guard and gates. One pilot
    runs at a time, and never while a hospital is being added; a request
    meanwhile gets 409. Beyond the hourly cap
    on pilots, a request gets 429.
    """
    if not pilot_flight.try_enter("pilot"):
        # The holder may have left since; then the message is a pilot's.
        holder = pilot_flight.holder or "pilot"
        raise HTTPException(status_code=409, detail=PILOT_BUSY_MESSAGES.get(holder, PILOT_BUSY_MESSAGE))
    try:
        # Counted only for a pilot that starts, not for one refused as busy.
        wait = hourly_caps.take("pilots")
        if wait > 0:
            raise _over_hourly_cap(wait, "pilots")
        return consortium.run_pilot(request.round_prefix)
    finally:
        pilot_flight.leave()


@app.get("/api/v1/models", responses=_errors(STORE_DOWN))
def models() -> dict[str, Any]:
    """The model the hospitals use, and the 20 newest registered versions, oldest first."""
    return {"model_in_use": consortium.model_in_use(), "models": consortium.models(limit=MODELS_LISTED)}


@app.post(
    "/api/v1/models/{version}:approve",
    dependencies=[Depends(rate_limits.dependency("approvals"))],
    responses=_errors(
        (404, "There is no model version with this name."),
        (
            409,
            "The version cannot be approved: it has no release decision, the gates or the release Guard did not "
            "allow it, it is no longer a candidate, or another model is now in use.",
        ),
        NOT_VALID,
        TOO_MANY,
        STORE_DOWN,
    ),
)
def approve_model(
    version: Annotated[
        str, Path(description="The candidate's version, as the pilot's `candidate.candidate_version` gives it.")
    ],
) -> dict[str, Any]:
    """Release a candidate whose last release decision was ``release``.

    Returns ``{released, previous}``. 409 when the gates or the release Guard
    did not allow it, or the version is no longer a candidate.
    """
    return consortium.approve(version)


# --- The framework ----------------------------------------------------------------------------


@app.get("/api/v1/framework/status")
def framework_status(request: Request) -> dict[str, Any]:
    """Where the records are stored, which framework services answer, and the allowed origins.

    The page shows the storage the backend reports, so a reader can tell
    records in Postgres from records in memory.

    ``rate_limit_client`` says how the rate limits identify a client. A
    request sent without ``X-Forwarded-For`` arrives with one entry per proxy
    that appended to it, so ``forwarded_for_entries`` should equal
    ``forwarded_for_hops`` for such a request. Fewer means every visitor is
    counted as the proxy; more means the limits count a proxy's address.
    """
    hops = rate_limits.forwarded_for_hops
    return {
        **consortium.data_source_status(),
        "services": framework.service_status(),
        # A browser can discover this list by asking, so it is not a secret,
        # and a wrong value blocks every request from the published page.
        "allowed_origins": settings.allowed_origins(),
        "rate_limit_client": {
            "source": "x-forwarded-for" if hops else "peer",
            "forwarded_for_hops": hops,
            "forwarded_for_entries": len(forwarded_for_entries(request)),
        },
    }


@app.get("/api/v1/framework/trace", responses=_errors(NOT_A_HOSPITAL, NOT_VALID, STORE_DOWN))
def framework_trace(
    site_id: str | None = Query(
        default=None,
        max_length=64,
        description="The hospital whose records are traced, such as `it-002`. Without it, the first hospital.",
    ),
    limit: int = Query(default=50, ge=1, le=1000, description="How many of the hospital's first records to trace."),
) -> dict[str, Any]:
    """Run one hospital's records through the framework's planners, at that hospital.

    None of the steps knows what a stroke is. They work from the contract and
    classification the extension declared.
    """
    hospital = _hospital(site_id) if site_id else consortium.hospitals[consortium.sites[0].site_id]
    records = hospital.local_records(limit)
    return {"site_id": hospital.site_id, "record_count": len(records), "trace": framework.planner_trace(records)}


# --- Adding a hospital while the demo runs ------------------------------------------------
# Declared in every role. An LMA answers 404 for both (``lma_serves``), and a
# server whose deployer is off says why on GET and refuses the POST.

app.include_router(build_deploy_router(lambda: deployer, pilot_flight))


# --- Internal routes of a split run -----------------------------------------------------------
# Only in their role, and left out of the OpenAPI document. The combined
# process has neither.
#
# An LMA's internal routes are on its one port, which only the app and the GMA
# reach. The app's are on a second listener (``internal_app``, started by the
# lifespan), because its public port is the one visitors reach.

#: The app role's internal listener, or ``None`` in the other roles.
internal_app: FastAPI | None = None

if ROLE == "lma":
    app.include_router(build_site_router(lambda: consortium))
elif ROLE == "app":
    internal_app = build_coordinator_app(
        lambda: consortium, exception_handlers=app.exception_handlers, max_body_bytes=settings.max_body_bytes
    )
