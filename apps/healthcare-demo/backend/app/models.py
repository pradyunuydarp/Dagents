"""API contracts for the healthcare demo backend."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from agents.common.domain.governance import Granularity, GuardBoundary, RequesterKind


class TriageRequest(BaseModel):
    """One canonical stroke-triage record to score."""

    record: dict[str, Any] = Field(default_factory=dict)


class WorklistRequest(BaseModel):
    """A set of canonical records to rank by triage priority."""

    records: list[dict[str, Any]] = Field(default_factory=list)


class FhirIngestRequest(BaseModel):
    """A FHIR-shaped bundle to map into the stroke feature contract."""

    bundle: dict[str, Any] = Field(default_factory=dict)


class PilotRequest(BaseModel):
    """Run one governed federated pilot.

    ``round_prefix`` makes a run reproducible in the UI: the same prefix yields
    the same round ids, so a reviewer can point at a specific round.
    """

    round_prefix: str | None = None


class GuardProbeRequest(BaseModel):
    """Ask the Guard what it would do with a request, and show the result.

    This exists so the governance layer can be exercised directly from the UI
    rather than only as a side effect of a pilot run.
    """

    #: The three levers are declared with the domain's own types, not as plain
    #: strings. They are passed straight into `RestrictionRequest`, whose fields
    #: are `Literal`s, so a value outside the set used to be accepted here and
    #: rejected one frame later — surfacing as a 500 on a request that was
    #: simply wrong. Declaring them here makes it a 422 that names the field and
    #: lists what it accepts, which is what a client can act on.
    requester_id: str = "demo-operator"
    requester_kind: RequesterKind = "human"
    verified: bool = True
    compliance_history: float = 0.9
    boundary: GuardBoundary = "before_read"
    granularity: Granularity = "row"
    requested_fields: list[str] = Field(default_factory=lambda: ["nihss_total", "age_band", "arrival_mode"])
    cohort_size: int | None = None
    declared_purpose: str | None = "suspected_stroke"
    record_limit: int = 5
