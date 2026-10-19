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

from dagents.common.domain.base import DagentsModel

Sensitivity = Literal["low", "medium", "high"]
TrustLevel = Literal["low", "moderate", "high"]

#: How much is being asked for.
#:
#: ``cell``, ``row``, ``column``, and ``table`` are the granularities GRAILS
#: publishes, and every one of them is data that can be pointed at and read.
#: ``model_update`` is this project's extension: a federated round ships none of
#: those, it ships a model update, which is not a row but still carries signal
#: about the site's records out of the boundary. Governing it needs a
#: granularity of its own.
Granularity = Literal["cell", "row", "column", "table", "model_update"]

#: Where in the request path the guard is standing.
#:
#: A guard only at the API edge is a warning label. In a federated deployment it
#: has to stand at all four of these: before local data is read, before training
#: touches it, before anything leaves the boundary, and before a candidate model
#: is released.
GuardBoundary = Literal["before_read", "before_train", "before_send", "before_release"]

#: Who is asking. Named rather than inlined on `Requester`, so a consumer's API
#: can declare its own request bodies with the same closed set instead of taking
#: a `str` and discovering the mismatch when the domain model rejects it.
RequesterKind = Literal["human", "site", "coordinator", "service"]

PlanDecision = Literal["permit", "narrow", "deny"]


class KyuAttribute(DagentsModel):
    """One signal feeding a Know-Your-User score.

    ``verified`` defaults to ``False`` on purpose: an attribute a caller forgot
    to mark must not be treated as checked.
    """

    attribute_id: str = Field(description="What the signal is, for example `email_domain` or `signed_agreement`.")
    weight: float = Field(
        default=1.0, ge=0.0, description="How much the signal counts toward the trust score. At least 0."
    )
    verified: bool = Field(
        default=False,
        description=(
            "Whether the signal has been checked. An unverified signal counts for a quarter of its weight. "
            "Default false, so a signal nobody marked is not treated as checked."
        ),
    )


class Requester(DagentsModel):
    """The party asking for data, an update, or a release.

    ``requester_kind`` carries the federated extension. A requester is not
    always a named human: it can be a peer site or the coordinator itself, and
    trust in a coordinator is a different question from trust in a person.
    """

    requester_id: str = Field(description="Who is asking. Written into the plan's assessment and the audit record.")
    requester_kind: RequesterKind = Field(
        default="service",
        description="`human`, `site`, `coordinator` or `service`. Written into the trust rationale; it does not change the score.",
    )
    affiliation: str | None = Field(
        default=None,
        description="The organization the requester belongs to. Written into the rationale; it does not change the score.",
    )
    stated_purpose: str | None = Field(
        default=None,
        description=(
            "Why the requester says it is asking. Written into the rationale. The purpose check reads the "
            "request's `declared_purpose`, not this field."
        ),
    )
    attributes: list[KyuAttribute] = Field(
        default_factory=list,
        description="The signals the trust score is computed from. With none, the score is 0 and trust is `low`.",
    )
    compliance_history: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="The requester's record of past compliance, from 0 to 1. It is 30% of the trust score.",
    )


class KyuAssessment(DagentsModel):
    """Computed trust for one requester, with the reasoning behind it."""

    requester_id: str = Field(description="The requester that was assessed.")
    kyu_score: float = Field(
        description=(
            "The Know-Your-User score, from 0 to 1: 0.7 times the share of attribute weight that is earned "
            "(an unverified attribute earns a quarter of its weight), plus 0.3 times `compliance_history`. "
            "0 when the requester has no attributes."
        )
    )
    trust: TrustLevel = Field(description="`low` below 0.4, `moderate` from 0.4 up to 0.75, `high` from 0.75.")
    rationale: list[str] = Field(
        default_factory=list,
        description="The inputs behind the score, one per entry, such as `verified_attributes=2/2`.",
    )


class DataClassification(DagentsModel):
    """How sensitive each field is, and which regulations cover the data.

    This is the data-side half of a restriction decision. It is configuration, not code: policy changes far more often than the
    framework does. ``default_sensitivity`` is ``"high"`` so an unclassified
    field is treated as the most protected until someone classifies it.
    """

    classification_id: str = Field(description="The classification's id. Condition packs name a classification by it.")
    field_sensitivity: dict[str, Sensitivity] = Field(
        default_factory=dict, description="Field names mapped to their sensitivity: `low`, `medium` or `high`."
    )
    default_sensitivity: Sensitivity = Field(
        default="high",
        description="The sensitivity of every field not in `field_sensitivity`. Default `high`, the most protected.",
    )
    regulations: list[str] = Field(
        default_factory=list,
        description="Regulations that cover the data, such as `HIPAA`. Each adds an obligation to every plan.",
    )
    minimum_cohort: int = Field(
        default=0,
        ge=0,
        description=(
            "The fewest subjects a `column`, `table` or `model_update` result may describe. 0 turns the check "
            "off. At least 0."
        ),
    )


class RestrictionRequest(DagentsModel):
    """One request for data, an update or a release, as the governance planner reads it.

    The planner's governance rules are the Ethical-Restriction Rails.
    """

    request_id: str = Field(
        description=(
            "An id for the request, chosen by the caller. It is copied into the plan, the decision and the "
            "audit record, and it seeds the random noise `add_noise` draws, so a decision can be reproduced."
        )
    )
    boundary: GuardBoundary = Field(
        description=(
            "Where the Guard stands: `before_read`, `before_train`, `before_send` or `before_release`. It is "
            "recorded in the plan and the audit record; it does not change the strategy chosen."
        )
    )
    requester: Requester = Field(description="Who is asking. The planner computes the requester's trust from it.")
    classification: DataClassification = Field(
        description="The classification to plan against, sent in full. It is not looked up by its id."
    )
    requested_fields: list[str] = Field(
        default_factory=list,
        description=(
            "The fields asked for. The plan has one restriction per field, and the Guard drops every other "
            "field from the payload."
        ),
    )
    granularity: Granularity = Field(
        description="How much is asked for: `cell`, `row`, `column`, `table` or `model_update` (a federated model update)."
    )
    cohort_size: int | None = Field(
        default=None,
        ge=0,
        description=(
            "How many subjects the result describes. Required for `column`, `table` and `model_update` "
            "requests when the classification's `minimum_cohort` is above 0. Below that minimum, the request "
            "is denied. At least 0."
        ),
    )
    declared_purpose: str | None = Field(
        default=None,
        description="Why the data is wanted. When `approved_purposes` is not empty, it is required and must be one of them.",
    )
    approved_purposes: list[str] = Field(
        default_factory=list, description="The purposes allowed for this data. Empty turns the purpose check off."
    )


class FieldRestriction(DagentsModel):
    """The strategy selected for one requested field.

    ``strategy`` keeps its parameter in the string (``"clip_contribution:1"``,
    ``"aggregate_only:20"``) so an audit record shows the bound that was
    applied, not only the strategy's name.
    """

    field: str = Field(description="The requested field.")
    sensitivity: Sensitivity = Field(description="The field's sensitivity, from the classification.")
    strategy: str = Field(
        description=(
            "What the Guard does with the field: `allow_full`, `generalize:<level>`, "
            "`clip_contribution:<bound>`, `add_noise:<scale>`, `aggregate_only:<minimum cohort>`, `redact` or "
            "`refuse`."
        )
    )
    reason: str = Field(
        description="Why the strategy was chosen, such as `sensitivity=high trust=high granularity=row -> generalize:1`."
    )


class RestrictionPlan(DagentsModel):
    """The planner's decision for one request: a strategy for each field, and a verdict.

    ``filtering_score`` is GRAILS' measure of how much protection was actually
    applied, so the amount of filtering can be reported rather than asserted:
    0.0 means nothing was withheld, 1.0 means the request was fully refused.
    """

    request_id: str = Field(description="Copied from the request.")
    boundary: GuardBoundary = Field(description="Copied from the request.")
    assessment: KyuAssessment = Field(description="How far the planner trusts the requester, and why.")
    granularity: Granularity = Field(description="Copied from the request.")
    field_restrictions: list[FieldRestriction] = Field(
        default_factory=list, description="One entry per requested field, in the order of the request."
    )
    decision: PlanDecision = Field(
        description=(
            "`deny` when a request-level check fails or any field is `refuse`, `permit` when every field is "
            "`allow_full`, and `narrow` otherwise."
        )
    )
    filtering_score: float = Field(
        description=(
            "How much protection the plan applies, from 0 (nothing withheld) to 1 (refused): the mean weight "
            "of the fields' strategies. The weights are allow_full 0, generalize 0.35, clip_contribution 0.45, "
            "add_noise 0.55, aggregate_only 0.7, redact 0.9 and refuse 1."
        )
    )
    obligations: list[str] = Field(
        default_factory=list,
        description=(
            "Duties the Guard must carry out, such as recording the decision, applying each regulation's "
            "controls and suppressing groups below the minimum cohort."
        ),
    )
    violations: list[str] = Field(
        default_factory=list,
        description=(
            "Request-level checks that failed: a missing or unapproved purpose, or a missing or too small "
            "cohort. Any violation makes the decision `deny`."
        ),
    )


class GuardDecision(DagentsModel):
    """What the Ethical Guard did with a request after the planner decided.

    The plan says what should happen; this says what happened. ``permitted`` is
    the answer the caller acts on, ``payload`` is what survived enforcement, and
    ``withheld_fields`` names what did not, so a narrowed response is never
    mistaken for a complete one.
    """

    request_id: str = Field(description="Copied from the request.")
    boundary: GuardBoundary = Field(description="Copied from the request.")
    permitted: bool = Field(
        description=(
            "Whether the request may go ahead. False for a `deny` plan, when the planner cannot be run or its "
            "answer cannot be read, when its plan names a strategy the Guard cannot apply, and when applying the "
            "plan to the records fails."
        )
    )
    plan: RestrictionPlan = Field(
        description=(
            "The plan the Guard applied. When the planner cannot be run, its answer cannot be read, its plan "
            "names a strategy the Guard cannot apply, or applying the plan to the records fails, a deny plan the "
            "Guard builds itself."
        )
    )
    payload: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "The records after enforcement: only the requested fields, each changed by its strategy, or one "
            "summary row when a field is `aggregate_only`. Empty when the request is denied, when every "
            "requested field is withheld, and when an aggregate group is smaller than its minimum."
        ),
    )
    withheld_fields: list[str] = Field(
        default_factory=list,
        description=(
            "Requested fields that were removed: redacted, refused, or holding a value the strategy could not "
            "be applied to. Every requested field when the request is denied."
        ),
    )
    transformed_fields: list[str] = Field(
        default_factory=list,
        description="Requested fields returned with changed values: generalized, clipped, noised or aggregated.",
    )
    audit_id: str = Field(description="The id of the audit record written for this decision.")
    message: str = Field(default="", description="The result in words, such as `permitted with restrictions`.")


class AuditRecord(DagentsModel):
    """One entry in the Guard's audit log, linked to the entry before it by a digest.

    ``previous_digest`` and ``digest`` chain entries together, so removing or
    editing an entry breaks every digest after it. This is a deterministic
    content chain for local integrity checking, not a cryptographic signature;
    signing and immutable storage are separate production controls.

    The ``contribution_*`` fields are the privacy accounting the planner asks
    for when it bounds a contribution with ``clip_contribution`` or
    ``add_noise``. ``contribution_norm_before`` and ``contribution_norm_after``
    are L2 norms (the absolute value for a single number). When a payload holds
    several contributions, they record the largest norm before and the largest
    norm after. ``contribution_strategies`` lists the strategies applied, with
    their parameters. All three stay empty for a decision that bounded nothing.

    ``planner_version`` and ``planner_sha256`` name the planner that decided,
    or that the Guard tried to run when the decision is a denial because the
    planner failed. Records written before these fields existed have neither,
    and the digest leaves out each one that is null, so those records still
    hash to the digests they were stored with.
    """

    audit_id: str = Field(description="A random id for the record: a UUID4 in hex, without hyphens.")
    recorded_at: int = Field(description="When the decision was recorded, in Unix seconds.")
    boundary: GuardBoundary = Field(description="The boundary from the request.")
    request_id: str = Field(description="The id from the request.")
    requester_id: str = Field(description="The requester's id from the request.")
    decision: PlanDecision = Field(description="The plan's decision: `permit`, `narrow` or `deny`.")
    permitted: bool = Field(description="Whether the Guard let the request go ahead.")
    granularity: Granularity = Field(description="The granularity from the request.")
    filtering_score: float = Field(description="The plan's filtering score, from 0 to 1.")
    withheld_fields: list[str] = Field(default_factory=list, description="The fields the Guard removed.")
    obligations: list[str] = Field(default_factory=list, description="The plan's obligations.")
    violations: list[str] = Field(default_factory=list, description="The plan's violations.")
    correlation_id: str | None = Field(
        default=None, description="The correlation id sent with the enforcement request, such as a round or run id."
    )
    contribution_norm_before: float | None = Field(
        default=None,
        description="The largest L2 norm of a clipped or noised contribution before the Guard changed it. Empty when nothing was bounded.",
    )
    contribution_norm_after: float | None = Field(
        default=None,
        description="The largest L2 norm of a clipped or noised contribution after the Guard changed it. Empty when nothing was bounded.",
    )
    contribution_strategies: list[str] = Field(
        default_factory=list, description="The bounding strategies applied, with their parameters, such as `clip_contribution:1`."
    )
    planner_version: str | None = Field(
        default=None,
        description=(
            "The version of the `dagents` package whose runner started the planner for this decision. The planner "
            "a platform wheel bundles, and the one an image or a checkout builds, come from the same source as the "
            "package. A planner that `DAGENTSC_BIN` names can come from elsewhere; `planner_sha256` names it "
            "exactly. Empty in records written before audit records named their planner, and when the Guard's "
            "planner is not `dagentsc`."
        ),
    )
    planner_sha256: str | None = Field(
        default=None,
        description=(
            "The SHA-256, in hex, of the planner binary the runner started, or tried to start, for this decision. "
            "It names the binary exactly; each GitHub Release lists its planners' hashes in `SHA256SUMS`. Empty "
            "when there was no binary to read, in records written before audit records named their planner, and "
            "when the Guard's planner is not `dagentsc`."
        ),
    )
    previous_digest: str = Field(
        default="", description="The `digest` of the record before this one. Empty for the first record."
    )
    digest: str = Field(
        default="",
        description=(
            "SHA-256, in hex, of every other field of this record as JSON with sorted keys, leaving out "
            "`planner_version` and `planner_sha256` when they are null. Editing or removing a record breaks the "
            "chain from that record on."
        ),
    )


class GuardAuditLog(DagentsModel):
    """The audit log as an agent's audit endpoint returns it.

    The endpoints build this shape as a plain dict. The model documents it in
    the OpenAPI schema; it is not used to build the response.
    """

    chain_intact: bool = Field(
        description=(
            "Whether the log's digest chain verifies. Records an earlier check already verified are not "
            "hashed again; only newer ones are."
        )
    )
    records: list[AuditRecord] = Field(
        default_factory=list, description="The newest records first, at most `limit` of them."
    )
