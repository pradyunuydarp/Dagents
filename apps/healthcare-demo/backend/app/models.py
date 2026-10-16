"""Request bodies of the healthcare demo API, the data-entry form, and shared response parts.

The data-entry body and the form the browser renders are built from the same
place: ``STROKE_FIELD_DOMAINS`` and ``STROKE_ENTRY_FIELDS`` in
``app.domain.conditions``. A choice is a ``Literal`` of the allowed values, a
number has the domain's range, and a yes/no field takes ``true``, ``false`` or
``null``. No field takes free text, and any other key is refused, so an entry
cannot carry a name, an identifier or a note.

Every successful answer whose body is a JSON object also has
``framework_calls``: the calls to the Dagents LMAs and GMA behind it
(:class:`FrameworkCall`).
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, create_model, field_validator
from pydantic_core import PydanticCustomError

from dagents.common.domain.governance import Granularity, GuardBoundary, RequesterKind, RestrictionPlan

from app.domain.conditions import (
    GUARD_PROBE_DEFAULT_FIELDS,
    STROKE_ENTRY_FIELDS,
    STROKE_FEATURE_CONTRACT,
    STROKE_FIELD_DOMAINS,
    STROKE_LABEL_FIELD,
)

#: Fields an entry must have. Every IST record has them, and without them the
#: record cannot be scored or banded. The four examination findings are in the
#: contract too, but may be left empty when they could not be assessed, as the
#: IST form allowed.
ENTRY_REQUIRED_FIELDS: tuple[str, ...] = (
    "age",
    "sex",
    "conscious_level",
    "hours_since_onset",
    "systolic_bp",
)

#: An idempotency key: letters, digits and ``. _ : -``, at most 100 characters.
#: A UUID fits.
IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9._:-]{1,100}$"
IDEMPOTENCY_KEY_MAX_LENGTH = 100

#: The round prefixes a request may choose. The pilot id starts with the
#: prefix and is stored in every model version the pilot registers, so a
#: visitor picks from a fixed list instead of writing text: ``ui`` for the
#: page, ``api`` for scripts and tests.
ROUND_PREFIXES: tuple[str, ...] = ("ui", "api")

#: Fields a Guard probe may ask for: the contract's fields except the training
#: label. The exact age, the row keys and the label stay at the hospital.
PROBE_FIELDS: tuple[str, ...] = tuple(
    name for name in STROKE_FEATURE_CONTRACT.field_names() if name != STROKE_LABEL_FIELD
)

_CONTRACT_DTYPES = {field.name: field.dtype for field in STROKE_FEATURE_CONTRACT.fields}


def field_type(name: str) -> str:
    """The form type of an entry field: ``integer``, ``number``, ``choice`` or ``boolean``."""
    domain = STROKE_FIELD_DOMAINS[name]
    if domain.options:
        return "choice"
    dtype = domain.dtype or _CONTRACT_DTYPES.get(name)
    if dtype == "int":
        return "integer"
    if dtype == "float":
        return "number"
    if dtype == "bool":
        return "boolean"
    raise TypeError(f"entry field {name} would take free text ({dtype}); give it options or a range")


def _annotation(name: str) -> Any:
    domain = STROKE_FIELD_DOMAINS[name]
    kind = field_type(name)
    if kind == "choice":
        base: Any = Literal[tuple(domain.options)]  # type: ignore[valid-type]
    elif kind == "boolean":
        base = StrictBool
    elif kind == "integer":
        base = Annotated[StrictInt, Field(ge=domain.minimum, le=domain.maximum)]
    else:
        base = Annotated[float, Field(strict=True, allow_inf_nan=False, ge=domain.minimum, le=domain.maximum)]
    return base


def _entry_fields() -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for name in STROKE_ENTRY_FIELDS:
        domain = STROKE_FIELD_DOMAINS[name]
        description = domain.label + (f" ({domain.unit})" if domain.unit else "") + (
            f". {domain.help}" if domain.help else ""
        )
        if name in ENTRY_REQUIRED_FIELDS:
            fields[name] = (_annotation(name), Field(description=description))
        else:
            fields[name] = (Optional[_annotation(name)], Field(default=None, description=description))
    return fields


#: The body of ``POST /api/v1/hospitals/{site_id}/encounters``.
EncounterEntry = create_model(  # type: ignore[call-overload]
    "EncounterEntry",
    __config__=ConfigDict(extra="forbid"),
    __doc__="A new encounter, as the data-entry form sends it. Missing values are null or absent.",
    **_entry_fields(),
)


def entry_form_fields() -> list[dict[str, Any]]:
    """The entry form's fields, in order, for the browser to render.

    Each field has ``name, label, type, required, options, min, max, unit,
    help``. ``options`` is set for a choice; ``min`` and ``max`` for a number.
    A yes/no field that is not required may be left empty.
    """
    form = []
    for name in STROKE_ENTRY_FIELDS:
        domain = STROKE_FIELD_DOMAINS[name]
        kind = field_type(name)
        form.append(
            {
                "name": name,
                "label": domain.label,
                "type": kind,
                "required": name in ENTRY_REQUIRED_FIELDS,
                "options": list(domain.options) if domain.options else None,
                "min": domain.minimum,
                "max": domain.maximum,
                "unit": domain.unit,
                "help": domain.help or None,
            }
        )
    return form


class OutcomeRequest(BaseModel):
    """The six-month outcome of an entered record."""

    model_config = ConfigDict(extra="forbid")

    poor_outcome_6m: StrictBool = Field(description="Dead or dependent at six months.")


class TriageRequest(BaseModel):
    """One record to score with the seed rule."""

    record: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "A record in the stroke feature contract, such as `age_band`, `conscious_level` and the five "
            "deficits. The record is not checked against the contract: a missing or unknown value adds no "
            "points and is listed in `data_quality_issues`, and other keys are ignored."
        ),
    )


class WorklistRequest(BaseModel):
    """Records to rank by the seed rule's score."""

    records: list[dict[str, Any]] = Field(
        default_factory=list,
        max_length=500,
        description=(
            "Records in the stroke feature contract, at most 500. Each one is returned with its assessment "
            "added under `assessment`; every other key is returned unchanged."
        ),
    )


class FhirIngestRequest(BaseModel):
    """A FHIR-shaped bundle to map into the stroke feature contract."""

    bundle: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "A FHIR-shaped `Bundle`. Each `Encounter` in `entry` becomes one record, with the `Observation` "
            "entries whose `encounter.reference` is `Encounter/<its id>`. Other resource types are skipped."
        ),
    )


class PilotRequest(BaseModel):
    """Run one federated pilot.

    ``round_prefix`` starts every round id: ``ui`` or ``api``. A timestamp and
    a random suffix follow it, so two pilots never share a round id.
    """

    model_config = ConfigDict(extra="forbid")

    round_prefix: Literal[ROUND_PREFIXES] | None = Field(  # type: ignore[valid-type]
        default=None,
        description=(
            "The start of the pilot id and every round id: `ui` for the demo page, `api` for scripts. "
            "Without it the prefix is `pilot`. A UTC timestamp and six random hex characters follow."
        ),
    )


class GuardProbeRequest(BaseModel):
    """Ask one hospital's Guard what it would do with a request.

    ``site_id`` picks the hospital; the default is the first one. The Guard
    runs over that hospital's first ``record_limit`` records and writes the
    decision to that hospital's audit chain.

    The audit chain is shown to every visitor and cannot be edited without
    breaking it, so the body takes no free text. The requester and the purpose
    are fixed by the server, and ``requested_fields`` names fields from
    :data:`PROBE_FIELDS` only. Any other key is refused.
    """

    model_config = ConfigDict(extra="forbid")

    #: The levers use the domain's own types, so a value outside the set is a
    #: 422 that names the field, not a 500 from deeper in the Guard.
    site_id: str | None = Field(
        default=None,
        max_length=64,
        description="The hospital whose Guard answers, such as `uk-134`. Without it, the first hospital answers.",
    )
    requester_kind: RequesterKind = Field(
        default="human",
        description="Who the requester is. It is written into the trust rationale and does not change the score.",
    )
    verified: bool = Field(
        default=True,
        description=(
            "Whether the requester's two attributes, `verified_identity` and `signed_dua`, are verified. "
            "With `false`, each counts for a quarter of its weight, so trust falls."
        ),
    )
    compliance_history: float = Field(
        default=0.9,
        ge=0.0,
        le=1.0,
        description="The requester's record of past compliance, from 0 to 1. It is 30% of the trust score.",
    )
    boundary: GuardBoundary = Field(
        default="before_read",
        description="Where the Guard stands. It is recorded in the plan and the audit record.",
    )
    granularity: Granularity = Field(
        default="row",
        description=(
            "How much is asked for. `column`, `table` and `model_update` requests are checked against the "
            "minimum cohort of 20."
        ),
    )
    requested_fields: list[str] = Field(
        default_factory=lambda: list(GUARD_PROBE_DEFAULT_FIELDS),
        max_length=len(PROBE_FIELDS),
        description=(
            "The contract fields asked for. Any field of the stroke feature contract except the outcome "
            "`poor_outcome_6m`. Every other field is dropped from the released records."
        ),
    )
    cohort_size: int | None = Field(
        default=None,
        ge=0,
        le=1_000_000,
        description="How many patients the result describes. Without it, the number of records read (`record_limit`).",
    )
    record_limit: int = Field(
        default=5,
        ge=0,
        le=20,
        description="How many of the hospital's first records the Guard reads, 0 to 20.",
    )

    @field_validator("requested_fields")
    @classmethod
    def _contract_fields_only(cls, fields: list[str]) -> list[str]:
        refused = [field for field in fields if field not in PROBE_FIELDS]
        if refused:
            shown = ", ".join(field[:64] for field in refused[:3])
            raise PydanticCustomError(
                "probe_field",
                "{refused} cannot be requested. Choose from: {allowed}",
                {"refused": shown, "allowed": ", ".join(PROBE_FIELDS)},
            )
        return fields


class FrameworkCall(BaseModel):
    """One call to a Dagents agent behind a response, as ``framework_calls`` lists it.

    The app adds the list to every successful (2xx) answer whose body is a
    JSON object. Errors, a JSON list and an SVG have none. It names the route,
    never a host, a token or a body.
    """

    service: Literal["lma", "gma"] = Field(description="The agent called: a hospital's `lma` or the study's `gma`.")
    site_id: str | None = Field(description="The hospital whose LMA was called. Null for the GMA.")
    method: str = Field(description="The HTTP method, such as `GET` or `POST`.")
    path: str = Field(
        description="The route as the agent declares it, such as `/api/v1/federation/rounds/{round_id}`. Never a host."
    )
    status: int | None = Field(description="The status the agent answered with. Null when no answer came.")
    ms: float = Field(description="How long the call took, in milliseconds.")
    via: Literal["app", "gma"] = Field(
        description=(
            "`app` for a call the app made. `gma` for a call the GMA made to an LMA while it dispatched a round; "
            "it follows the GMA call that made it."
        )
    )


class AgentReach(BaseModel):
    """One hospital's LMA, as the GMA reaches it."""

    site_id: str = Field(description="The hospital.")
    reachable: bool = Field(description="Whether the LMA answered the GMA.")
    source_kind: str | None = Field(
        description="The kind of store the LMA reports it reads: `postgres` or `in-memory`. Null when it did not answer."
    )


class AgentsStatus(BaseModel):
    """How the Dagents agents run behind the demo, as ``GET /api/v1/framework/status`` reports it."""

    mode: Literal["in-process", "network"] = Field(
        description=(
            "`in-process` when the three LMAs and the GMA run inside the demo server and are called through their "
            "routes without a socket; `network` when they run as their own processes."
        )
    )
    engine: str | None = Field(description="The GMA's round engine, such as `http`. Null when the GMA did not answer.")
    lmas: list[AgentReach] | None = Field(description="Each hospital's LMA, from the GMA. Null when the GMA did not answer.")


class GuardProbeResponse(BaseModel):
    """What a hospital's Guard would release for a probe, without the records.

    The coordinator's page calls the probe, and no patient row reaches the
    coordinator. So the answer counts the released records and names their
    fields; the values stay at the hospital. The hospital's LMA runs the Guard
    (``POST /api/v1/governance/restrictions:probe``). A key the LMA's answer
    adds that is not listed here is dropped from the response.
    """

    site_id: str = Field(description="The hospital whose Guard answered.")
    request_id: str = Field(
        description="`probe:<site_id>:<random hex>`, given by the hospital's LMA. It is the request id in the audit record."
    )
    boundary: GuardBoundary = Field(description="Copied from the request.")
    permitted: bool = Field(description="False when the plan's decision is `deny`.")
    plan: RestrictionPlan = Field(description="The planner's restriction plan: a strategy for each requested field.")
    released_count: int = Field(ge=0, description="How many records or summary rows the Guard would release.")
    released_fields: list[str] = Field(
        description="The field names in the released records, in the order they first appear."
    )
    withheld_fields: list[str] = Field(
        description=(
            "Requested fields the Guard removed: a field whose strategy is `redact` or `refuse`, a field it "
            "could not protect, and every requested field when the request is denied."
        )
    )
    transformed_fields: list[str] = Field(
        description="Requested fields the Guard changed before release: generalized, clipped, noised or aggregated."
    )
    audit_id: str = Field(description="The id of the record the decision added to the hospital's audit chain.")
    message: str = Field(description="The Guard's decision in words, such as `permitted with restrictions`.")
    framework_calls: list[FrameworkCall] = Field(
        default_factory=list,
        description="The calls to the hospital's LMA behind this answer: `POST /api/v1/governance/restrictions:probe`.",
    )


# --- Error bodies, for the OpenAPI document ---------------------------------------
# The app's exception handlers in ``app.main`` write these shapes. The models
# only describe them; nothing validates a response against them.


class ErrorResponse(BaseModel):
    """The body of every error the app sends except a 422: one plain message."""

    detail: str = Field(
        description=(
            "What went wrong, in plain words, and what to do where there is something to do. It never names a "
            "database host, a user or the API's address."
        )
    )


class ValidationErrorResponse(BaseModel):
    """The body of a 422: a message, and one plain reason per field."""

    detail: str = Field(
        description=(
            "The message. It is `Some fields are not valid.` when the request does not match the schema, and "
            "`The record was not saved. Correct the fields listed.` when the hospital's store refuses the record."
        )
    )
    errors: dict[str, str] = Field(
        description=(
            "The field to the reason it was refused, such as `\"age\": \"must be between 16 and 110\"`. A "
            "query parameter or body field is named by its path (`limit`, `records`), a header by its name "
            "(`Idempotency-Key`), and a body that is not JSON as `body`."
        )
    )
