"""Governance contracts for GRAILS-style ethical safeguards.

These types mirror the typed IR in ``bindings/ocaml/lib/common_ir`` so the same
restriction decision can be described on either side of the ``dagentsc``
subprocess boundary.

The split follows GRAILS (Kulkarni and Ramanathan, AIES 2025). The
Ethical-Restriction Rails decide what protection a request needs and live in
the OCaml planner, where an unhandled combination is a compile error. The
Ethical Guard applies that decision and records it, and lives here in Python,
because enforcement needs real data, a real requester, and an audit sink.

Nothing in this module is healthcare-specific. A consumer supplies its own
:class:`DataClassification` describing which of its fields are sensitive and
which regulations cover them; the framework supplies the decision procedure.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from agents.common.domain.base import DagentsModel

Sensitivity = Literal["low", "medium", "high"]
TrustLevel = Literal["low", "moderate", "high"]

#: How much is being asked for.
#:
#: ``cell``, ``row``, ``column``, and ``table`` are the granularities GRAILS
#: publishes, and every one of them is data that can be pointed at and read.
#: ``model_update`` is this project's extension: a federated round ships none of
#: those, it ships a model update, which is not a row but still carries patient
#: signal out of the boundary. Governing it needs a granularity of its own.
Granularity = Literal["cell", "row", "column", "table", "model_update"]

#: Where in the request path the guard is standing.
#:
#: A guard only at the API edge is a warning label. In a federated deployment it
#: has to stand at all four of these: before local data is read, before training
#: touches it, before anything leaves the boundary, and before a candidate model
#: is released.
GuardBoundary = Literal["before_read", "before_train", "before_send", "before_release"]

PlanDecision = Literal["permit", "narrow", "deny"]


class KyuAttribute(DagentsModel):
    """One signal feeding a Know-Your-User score.

    ``verified`` defaults to ``False`` on purpose: an attribute a caller forgot
    to mark must not be treated as checked.
    """

    attribute_id: str
    weight: float = Field(default=1.0, ge=0.0)
    verified: bool = False


class Requester(DagentsModel):
    """The party asking for data, an update, or a release.

    ``requester_kind`` carries the federated extension. A requester is not
    always a named human: it can be a peer site or the coordinator itself, and
    trust in a coordinator is a different question from trust in a person.
    """

    requester_id: str
    requester_kind: Literal["human", "site", "coordinator", "service"] = "service"
    affiliation: str | None = None
    stated_purpose: str | None = None
    attributes: list[KyuAttribute] = Field(default_factory=list)
    compliance_history: float = Field(default=0.0, ge=0.0, le=1.0)


class KyuAssessment(DagentsModel):
    """Computed trust for one requester, with the reasoning behind it."""

    requester_id: str
    kyu_score: float
    trust: TrustLevel
    rationale: list[str] = Field(default_factory=list)


class DataClassification(DagentsModel):
    """The data-side half of a restriction decision.

    This is configuration, not code: policy changes far more often than the
    framework does. ``default_sensitivity`` is ``"high"`` so an unclassified
    field is treated as the most protected until someone classifies it.
    """

    classification_id: str
    field_sensitivity: dict[str, Sensitivity] = Field(default_factory=dict)
    default_sensitivity: Sensitivity = "high"
    regulations: list[str] = Field(default_factory=list)
    minimum_cohort: int = Field(default=0, ge=0)


class RestrictionRequest(DagentsModel):
    """One request presented to the Ethical-Restriction Rails."""

    request_id: str
    boundary: GuardBoundary
    requester: Requester
    classification: DataClassification
    requested_fields: list[str] = Field(default_factory=list)
    granularity: Granularity
    cohort_size: int | None = Field(default=None, ge=0)
    declared_purpose: str | None = None
    approved_purposes: list[str] = Field(default_factory=list)


class FieldRestriction(DagentsModel):
    """The strategy selected for one requested field.

    ``strategy`` keeps its parameter in the string (``"clip_contribution:1"``,
    ``"aggregate_only:20"``) so an audit record shows the bound that was
    applied, not only the strategy's name.
    """

    field: str
    sensitivity: Sensitivity
    strategy: str
    reason: str


class RestrictionPlan(DagentsModel):
    """The Rails' decision for one request.

    ``filtering_score`` is GRAILS' measure of how much protection was actually
    applied, so the amount of filtering can be reported rather than asserted:
    0.0 means nothing was withheld, 1.0 means the request was fully refused.
    """

    request_id: str
    boundary: GuardBoundary
    assessment: KyuAssessment
    granularity: Granularity
    field_restrictions: list[FieldRestriction] = Field(default_factory=list)
    decision: PlanDecision
    filtering_score: float
    obligations: list[str] = Field(default_factory=list)
    violations: list[str] = Field(default_factory=list)


class GuardDecision(DagentsModel):
    """What the Ethical Guard did after the Rails decided.

    The plan says what should happen; this says what happened. ``permitted`` is
    the answer the caller acts on, ``payload`` is what survived enforcement, and
    ``withheld_fields`` names what did not, so a narrowed response is never
    mistaken for a complete one.
    """

    request_id: str
    boundary: GuardBoundary
    permitted: bool
    plan: RestrictionPlan
    payload: list[dict[str, Any]] = Field(default_factory=list)
    withheld_fields: list[str] = Field(default_factory=list)
    transformed_fields: list[str] = Field(default_factory=list)
    audit_id: str
    message: str = ""


class AuditRecord(DagentsModel):
    """One tamper-evident-by-convention entry in the guard's decision log.

    ``previous_digest`` and ``digest`` chain entries together, so removing or
    editing an entry breaks every digest after it. This is a deterministic
    content chain for local integrity checking, not a cryptographic signature;
    signing and immutable storage are separate production controls.
    """

    audit_id: str
    recorded_at: int
    boundary: GuardBoundary
    request_id: str
    requester_id: str
    decision: PlanDecision
    permitted: bool
    granularity: Granularity
    filtering_score: float
    withheld_fields: list[str] = Field(default_factory=list)
    obligations: list[str] = Field(default_factory=list)
    violations: list[str] = Field(default_factory=list)
    correlation_id: str | None = None
    previous_digest: str = ""
    digest: str = ""
