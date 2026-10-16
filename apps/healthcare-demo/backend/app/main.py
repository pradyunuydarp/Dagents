"""FastAPI entrypoint for the Dagents healthcare stroke-triage demo.

This app uses the Dagents framework; it is not part of it. Each hospital is
served by a Dagents LMA (local monitoring agent), and the study is coordinated
by a Dagents GMA (global monitoring agent). The app calls them only through
their HTTP APIs (``app.services.agents``): the hospital views call each
hospital's LMA, and the study operator calls the GMA. By default the four
agents run inside this process and are called without a socket; they can also
run as their own processes.

The app keeps the stroke domain: the feature contract, the classification and
the condition pack (contributed to the agents as an extension), the seed rule
and the logistic regression (contributed as model kinds), the hospitals'
stores and data entry, the hospitals' own views, the order of the pilot's
rounds, the charts and this public API with its limits.

The records are real, de-identified records from the International Stroke
Trial (1991-1996), grouped by hospital. Each hospital's records are in its own
store: its own schema and role in Postgres, or its own in-memory store when no
database is configured. Nothing this app returns is clinical advice.

Every successful answer whose body is a JSON object has a ``framework_calls``
field: the LMA and GMA calls behind it, by route, with no host and no token.

Building the app reads no records, opens no database connection and starts no
agent. The stores and the agents are built on first use and stopped when the
app shuts down.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import Annotated, Any, AsyncIterator, Awaitable, Callable, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from agents.common.domain.governance import KyuAttribute, Requester
from agents.common.domain.site import ProbeRequest
from agents.common.infrastructure.charts import ChartsUnavailableError

from app.api.app_header import APP_HEADER, AppHeader
from app.api.errors import UNEXPECTED_ERROR_MESSAGE, UnhandledErrors
from app.api.limits import (
    BodySizeLimit,
    HourlyCap,
    InFlightCap,
    RateLimiter,
    SingleFlight,
    forwarded_for_entries,
    retry_after,
)
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
from app.domain.dataset import DATASET, UnknownSiteError
from app.domain.fhir import map_bundle
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
from app.services.agents import AgentError, FrameworkCalls, HealthcareDemo, loggable
from app.services.charts_service import ChartNotAvailable, ChartService, UnknownChartError
from app.services.hospital_views import HospitalView
from app.services.overview import ApprovalRefused, SiteReportMissing, SiteUnreachable, UnknownModelVersion
from app.services.site_store import (
    EntryLimitReached,
    IdempotencyKeyReused,
    OutcomeAlreadyRecorded,
    RecordRejected,
    StoreNotSetUpError,
    StoreUnavailableError,
    UnknownEncounterError,
    not_set_up_errors,
)

try:
    import psycopg
except ModuleNotFoundError:  # pragma: no cover - psycopg is in requirements.txt
    psycopg = None  # type: ignore[assignment]

logger = logging.getLogger("healthcare_demo")

#: The version of this app. The extension reports the same version.
APP_VERSION = "1.1.0"

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

#: The message a visitor sees when the planner cannot be run during a pilot.
PLANNER_UNAVAILABLE_MESSAGE = "The planner is not available right now, so the pilot cannot run. Try again in a minute."

#: The message for any other page that needs the planner, such as one that
#: shows the model in use: the GMA checks each model version with it.
PLANNER_NEEDED_MESSAGE = "The planner is not available right now. Try again in a minute."

#: The path of the pilot, which gets the pilot's own planner message.
PILOT_PATH = "/api/v1/pilot:run"

#: The message a visitor sees when a Dagents agent did not answer in time.
AGENT_UNAVAILABLE_MESSAGE = "Part of the demo server did not answer in time. Try again in a minute."

#: The message a visitor sees when a hospital's LMA could not get what it
#: needed from the GMA, for example because the GMA's store failed.
AGENT_FAILED_MESSAGE = "Part of the demo server could not complete this request. Try again in a minute."

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

# Settings that cannot work stop the app here, before it serves a request:
# an unknown agent mode, or too few agent threads for the in-flight cap.
settings.check_agent_capacity()

#: The app's services: the hospitals' stores, the agents, the hospital views,
#: the study and the overview. Built on first use. Tests replace it with their own.
demo = HealthcareDemo()
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
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Stop the agents and close every store's database connection when the app shuts down."""
    try:
        yield
    finally:
        demo.close()


# --- framework_calls ------------------------------------------------------------------------


class FrameworkCallsField:
    """ASGI middleware that adds ``framework_calls`` to every successful JSON object the app answers with.

    It records the agent calls made while the request runs
    (:class:`~app.services.agents.FrameworkCalls`). Sync route handlers run in
    a thread with a copy of the request's context, so their calls are
    recorded too. For a 2xx response whose body is a JSON object, it sets
    ``framework_calls`` to the list. Other responses pass unchanged: errors, a
    JSON list, an SVG chart and a 304.
    """

    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Callable[[], Awaitable[dict]], send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        with FrameworkCalls() as calls:
            start: dict | None = None
            chunks: list[bytes] = []

            async def add_calls(message: dict) -> None:
                nonlocal start
                if message["type"] == "http.response.start":
                    headers = dict(message.get("headers") or [])
                    json_body = headers.get(b"content-type", b"").startswith(b"application/json")
                    if 200 <= message["status"] < 300 and json_body:
                        start = message
                        return
                    await send(message)
                    return
                if message["type"] == "http.response.body" and start is not None:
                    chunks.append(message.get("body", b""))
                    if message.get("more_body", False):
                        return
                    body = _with_calls(b"".join(chunks), calls.entries())
                    headers = [(name, value) for name, value in start.get("headers") or [] if name.lower() != b"content-length"]
                    headers.append((b"content-length", str(len(body)).encode()))
                    await send({**start, "headers": headers})
                    await send({"type": "http.response.body", "body": body})
                    return
                await send(message)

            await self.app(scope, receive, add_calls)


def _with_calls(body: bytes, entries: list[dict[str, Any]]) -> bytes:
    """``body`` with ``framework_calls`` set, when it is a JSON object; otherwise ``body`` unchanged."""
    try:
        content = json.loads(body)
    except ValueError:
        return body
    if not isinstance(content, dict):
        return body
    content["framework_calls"] = entries
    return json.dumps(content, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


app = FastAPI(
    title=settings.app_name,
    version=APP_VERSION,
    description=(
        "A Dagents demo app for federated stroke triage, on de-identified records from the International "
        "Stroke Trial. Each hospital is served by a Dagents LMA and the study by a Dagents GMA, called through "
        "their HTTP APIs. Nothing here is a validated clinical model or clinical advice."
    ),
    lifespan=lifespan,
)
# Innermost, so it sees each route's own answer and every agent call the route makes.
app.add_middleware(FrameworkCallsField)
# Inside the body limit, so a request takes a slot only after its whole body has
# arrived: a client that sends its body slowly holds no slot. Inside the CORS
# middleware and the marker too, so its 503 "busy" carries both.
app.add_middleware(InFlightCap, max_in_flight=settings.max_in_flight, wait_seconds=settings.in_flight_wait_seconds)
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


def _not_set_up(request: Request, exc: BaseException) -> JSONResponse:
    logger.error(
        "the database is not set up for this version of the app on %s %s (apply the migrations, then seed): %s",
        request.method,
        request.url.path,
        loggable(exc),
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
    logger.warning("store unavailable on %s %s: %s", request.method, request.url.path, loggable(exc))
    return JSONResponse(status_code=503, content={"detail": STORE_UNAVAILABLE_MESSAGE})


if psycopg is not None:

    @app.exception_handler(psycopg.Error)
    def database_error(request: Request, exc: Exception) -> JSONResponse:
        """Any other database error is a 503 with a plain message, never a 500 without CORS headers."""
        if isinstance(exc, not_set_up_errors()):
            return _not_set_up(request, exc)
        logger.error(
            "database error on %s %s: %s: %s", request.method, request.url.path, type(exc).__name__, loggable(exc)
        )
        return JSONResponse(status_code=503, content={"detail": STORE_UNAVAILABLE_MESSAGE})


#: What a visitor sees for each error code an agent can answer with. Any other
#: code is the app's own fault and gets the plain 500. The agent's own message
#: is logged, never returned: a store's message can name a host.
AGENT_ERROR_MESSAGES: dict[str, tuple[int, str]] = {
    "planner_unavailable": (503, PLANNER_UNAVAILABLE_MESSAGE),
    "store_unavailable": (503, STORE_UNAVAILABLE_MESSAGE),
    "store_not_set_up": (503, DATABASE_NOT_SET_UP_MESSAGE),
    "agent_unavailable": (503, AGENT_UNAVAILABLE_MESSAGE),
    # An LMA's 502: the GMA behind it failed, for example because its store did.
    "bad_gateway": (503, AGENT_FAILED_MESSAGE),
}


@app.exception_handler(AgentError)
def agent_error(request: Request, exc: AgentError) -> JSONResponse:
    """An LMA or GMA error the route did not expect, as a plain answer with the status its code calls for.

    The agent's message goes to the log only, with any address removed. A
    visitor gets the app's own words.
    """
    if exc.code == "store_not_set_up":
        return _not_set_up(request, exc)
    status, message = AGENT_ERROR_MESSAGES.get(exc.code, (500, UNEXPECTED_ERROR_MESSAGE))
    if exc.code == "store_unavailable":
        logger.warning("store unavailable on %s %s: %s", request.method, request.url.path, loggable(exc))
    elif exc.code == "planner_unavailable":
        logger.error("planner unavailable on %s %s: %s", request.method, request.url.path, loggable(exc))
        if request.url.path != PILOT_PATH:
            message = PLANNER_NEEDED_MESSAGE
    else:
        (logger.warning if status == 503 else logger.error)(
            "an agent call failed on %s %s, calling %s %s: %s",
            request.method,
            request.url.path,
            exc.service,
            exc.path,
            loggable(exc),
        )
    return JSONResponse(status_code=status, content={"detail": message})


@app.exception_handler(SiteUnreachable)
def site_unreachable(request: Request, exc: SiteUnreachable) -> JSONResponse:
    logger.warning("%s on %s %s", exc, request.method, request.url.path)
    return JSONResponse(status_code=503, content={"detail": AGENT_UNAVAILABLE_MESSAGE})


@app.exception_handler(SiteReportMissing)
def site_report_missing(request: Request, exc: SiteReportMissing) -> JSONResponse:
    logger.warning("%s on %s %s", exc, request.method, request.url.path)
    return JSONResponse(status_code=503, content={"detail": STORE_UNAVAILABLE_MESSAGE})


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
    # The GMA's reason can end with its own full stop.
    reason = str(exc).rstrip(".")
    return JSONResponse(status_code=409, content={"detail": f"The model cannot be approved: {reason}."})


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


@app.exception_handler(UnknownModelVersion)
def unknown_model(_: Request, exc: UnknownModelVersion) -> JSONResponse:
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


def _hospital(site_id: str) -> HospitalView:
    """The hospital's view, with the services ready. Raises ``UnknownSiteError`` first for an unknown id."""
    return demo.hospital(site_id)


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
    "The demo server is busy, a Dagents agent did not answer, or, with a database, a hospital's store cannot be "
    "reached or the database is not set up for this version of the app.",
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
def health() -> dict[str, Any]:
    """Liveness. Reads no store and calls no agent."""
    return settings.as_health_payload()


@app.get("/api/v1/health")
def health_v1() -> dict[str, Any]:
    """Versioned alias for the health endpoint."""
    return health()


@app.get("/api/v1/overview", responses=_errors(STORE_DOWN))
def overview() -> dict[str, Any]:
    """The consortium, the numbers each hospital reports through the GMA, and the release gates."""
    return demo.overview.overview()


@app.get("/api/v1/conditions")
def conditions() -> dict[str, Any]:
    """The condition this app implements, the conditions it plans, its contract and classification."""
    return {
        "implemented": [STROKE_CONDITION.model_dump(mode="json")],
        "planned": PLANNED_CONDITIONS,
        "feature_contract": STROKE_FEATURE_CONTRACT.model_dump(mode="json"),
        "classification": STROKE_CLASSIFICATION.model_dump(mode="json"),
    }


@app.get("/api/v1/extension", responses=_errors(STORE_DOWN))
def extension() -> dict[str, Any]:
    """What this app contributes to the agents, as the GMA's extension registry describes it, and what each side owns."""
    return {
        "registry": demo.overview.extension(),
        "boundary": {
            "framework_owns": [
                "the LMA at each hospital: its Guard and audit chain, running profile, outcome-rate decision and job log",
                "the GMA: enrolment, job dispatch to the LMAs, the update store and the model registry",
                "restriction planning (Ethical-Restriction Rails) and Guard enforcement",
                "federated round planning, quorum and aggregation readiness",
                "the release Guard, release gate evaluation and approval",
                "the chart renderer",
            ],
            "app_owns": [
                "the stroke feature contract and its units",
                "which fields are sensitive and under which regulations",
                "the seed outcome rule and the logistic regression, contributed as model kinds",
                "how a hospital's update is checked and combined",
                "the hospitals' stores, data entry and the hospitals' own views",
                "the order of the pilot's rounds and its report",
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
    """What each hospital reports about its records through the GMA: counts and an outcome rate."""
    return demo.overview.hospital_rows()


@app.get("/api/v1/hospitals/{site_id}", responses=_errors(NOT_A_HOSPITAL, NOT_VALID, STORE_DOWN))
def hospital_dashboard(site_id: SiteId) -> dict[str, Any]:
    """One hospital's own view: its store, what its LMA reports, local metrics and what it sent."""
    return _hospital(site_id).dashboard()


@app.get("/api/v1/hospitals/{site_id}/worklist", responses=_errors(NOT_A_HOSPITAL, NOT_VALID, STORE_DOWN))
def hospital_worklist(
    site_id: SiteId,
    limit: int = Query(default=12, ge=1, le=200, description="How many records to return, highest risk first."),
) -> dict[str, Any]:
    """One hospital's records ranked by the model its LMA reports in use. The list stays at the hospital."""
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
    """One hospital's Guard decisions, newest first, and whether its audit chain verifies, from its LMA."""
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
    """Store a new record at one hospital, as that hospital, and have its LMA read the store again.

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
    """The consortium charts, from what the hospitals report through the GMA and the last pilot. Never a 4xx."""
    return {"charts": chart_service.consortium_list(demo.overview)}


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
    overview = demo.overview
    return _draw(request, lambda: chart_service.consortium_svg(overview, chart_id, theme), theme)


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
    """Run one request through a hospital's Guard, at its LMA, and report what it would release.

    The decision is a lookup in the typed planner, so changing the requester's
    verification, the boundary or the granularity changes it. The hospital's
    LMA runs the Guard (``POST /api/v1/governance/restrictions:probe``) over
    the hospital's first records, with the hospital's classification and
    approved purposes, and writes the decision to the hospital's audit chain.
    The requester and the purpose are fixed here, so no visitor's text reaches
    the chain.

    The coordinator's page calls this, so the answer holds no record: it
    counts the released records and names their fields
    (:class:`GuardProbeResponse`).
    """
    site_id = request.site_id or demo.sites[0].site_id
    hospital = _hospital(site_id)
    # Counted after the hospital is known, so an unknown id is a 404 and adds no key.
    wait = hourly_caps.take("probes", hospital.site_id)
    if wait > 0:
        raise _over_hourly_cap(wait, f"Guard probes at {hospital.display_name}")
    probe = ProbeRequest(
        boundary=request.boundary,
        granularity=request.granularity,
        requested_fields=request.requested_fields,
        cohort_size=request.cohort_size,
        record_limit=request.record_limit,
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
        declared_purpose=PROBE_PURPOSE,
    )
    return hospital.probe(probe)


@app.post(
    PILOT_PATH,
    dependencies=[Depends(rate_limits.dependency("pilots"))],
    responses=_errors(
        (409, "A pilot is already running. One pilot runs at a time."),
        NOT_VALID,
        (
            429,
            "Too many pilots from this client, or the hourly cap on pilots is reached. Wait for the seconds in "
            "the Retry-After header.",
        ),
        (503, "The planner cannot be run, an agent did not answer, or, with a database, a store cannot be reached."),
    ),
)
def run_pilot(request: PilotRequest) -> Any:
    """Run the federated pilot across every hospital, through the GMA, and return its evidence.

    Analytics, then evaluation of the model in use, then training rounds, then
    validation of the candidate, then the release Guard and gates. One pilot
    runs at a time; a request while one runs gets 409. Beyond the hourly cap
    on pilots, a request gets 429.
    """
    if not pilot_flight.try_enter():
        raise HTTPException(status_code=409, detail=PILOT_BUSY_MESSAGE)
    try:
        # Counted only for a pilot that starts, not for one refused as busy.
        wait = hourly_caps.take("pilots")
        if wait > 0:
            raise _over_hourly_cap(wait, "pilots")
        return demo.pilot.run_pilot(request.round_prefix)
    finally:
        pilot_flight.leave()


@app.get("/api/v1/models", responses=_errors(STORE_DOWN))
def models() -> dict[str, Any]:
    """The model the hospitals use, and the 20 newest registered versions, oldest first, from the GMA."""
    return demo.overview.models(MODELS_LISTED)


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
    """Release a candidate whose last release decision was ``release``, through the GMA.

    Returns ``{released, previous}``. 409 when the gates or the release Guard
    did not allow it, or the version is no longer a candidate.
    """
    return demo.overview.approve(version)


# --- The framework ----------------------------------------------------------------------------


@app.get("/api/v1/framework/status")
def framework_status(request: Request) -> dict[str, Any]:
    """Where the records are stored, how the Dagents agents run, which answer, and the allowed origins.

    The page shows the storage the hospitals' LMAs report, so a reader can tell
    records in Postgres from records in memory. ``agents`` says whether the
    LMAs and the GMA run inside the demo server or as their own processes.

    ``rate_limit_client`` says how the rate limits identify a client. A
    request sent without ``X-Forwarded-For`` arrives with one entry per proxy
    that appended to it, so ``forwarded_for_entries`` should equal
    ``forwarded_for_hops`` for such a request. Fewer means every visitor is
    counted as the proxy; more means the limits count a proxy's address.

    It never fails because a store or an agent does not answer.
    """
    hops = rate_limits.forwarded_for_hops
    try:
        status = demo.status()
    except Exception as error:  # noqa: BLE001 - the status report must answer while the agents cannot be built
        logger.warning("the agents could not be built for the status report: %s", type(error).__name__)
        status = _status_without_agents()
    return {
        **status,
        # A browser can discover this list by asking, so it is not a secret,
        # and a wrong value blocks every request from the published page.
        "allowed_origins": settings.allowed_origins(),
        "rate_limit_client": {
            "source": "x-forwarded-for" if hops else "peer",
            "forwarded_for_hops": hops,
            "forwarded_for_entries": len(forwarded_for_entries(request)),
        },
    }


def _status_without_agents() -> dict[str, Any]:
    """The status report when the agents cannot be built: nothing is ready.

    ``cohort_source`` is then the kind of store the app is configured for.
    """
    kind = "postgres" if demo.database_url else "in-memory"
    return {
        "cohort_source": kind,
        "store_layout": {"postgres": "schema-per-site", "in-memory": "in-memory-per-site"}[kind],
        "dataset": dict(DATASET),
        "ready": False,
        "hospitals": None,
        "agents": {"mode": settings.agents_mode, "engine": None, "lmas": None},
        "services": [],
    }
