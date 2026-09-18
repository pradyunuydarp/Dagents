"""Federated round contracts shared by the GMA, the LMA, and consumer apps.

Dagents governs a federation; it does not run one. These contracts describe the
round agreement, who may take part, what a site returns, and what has to be true
before a candidate model may be released. The distributed training protocol
belongs to a specialist runtime such as NVIDIA FLARE, reached through
:class:`~agents.common.application.federation.FederationEngine`, so nothing here
reimplements a federated optimizer.

The rule these contracts exist to hold: aggregation creates a candidate model,
never an approved release.

Nothing here is healthcare-specific. ``condition_id`` is whatever a consumer
calls the thing being studied, and ``feature_contract_version`` is whichever
schema agreement its sites have signed.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from agents.common.domain.base import DagentsModel

#: What a round is for.
#:
#: The recommended pilot order is analytics, then evaluation, then training, so
#: a consortium proves it can produce the same measures before any model update
#: moves. The phase is explicit so a study cannot skip ahead silently.
RoundPhase = Literal["analytics", "evaluation", "training"]

StopCondition = Literal[
    "schema_failure",
    "privacy_budget_exceeded",
    "unsafe_metric",
    "quorum_not_met",
    "excessive_dropout",
]

SiteParticipation = Literal["accepted", "completed", "rejected", "failed", "dropped"]
GateOutcome = Literal["passed", "failed", "not_evaluated"]
ReleaseAction = Literal["release", "another_round", "reject"]


class AggregationMethod(DagentsModel):
    """How permitted contributions are combined.

    ``threshold`` applies to ``secure_aggregation`` and is the number of sites
    below which the coordinator must not be able to resolve any single site's
    update. The planner treats it as a floor on participation, not a hint.
    """

    kind: Literal["fedavg", "fedprox", "fedopt", "secure_aggregation"] = "fedavg"
    mu: float | None = None
    optimizer: str | None = None
    threshold: int | None = Field(default=None, ge=1)


class SiteRegistration(DagentsModel):
    """One site's standing enrolment in a study.

    ``enrolled`` defaults to ``False``: taking part has to be stated, never
    inferred from the presence of a record.
    """

    site_id: str
    capabilities: list[str] = Field(default_factory=list)
    feature_contract_version: str
    approved_conditions: list[str] = Field(default_factory=list)
    policy_version: str = "unversioned"
    cohort_size: int = Field(default=0, ge=0)
    enrolled: bool = False


class RoundManifest(DagentsModel):
    """The round contract distributed to every approved site.

    A site verifies this before any code touches local data: the digests say
    which model and which training code were approved, and
    ``feature_contract_version`` says which fields the round may read.
    """

    round_id: str
    study_id: str
    condition_id: str
    phase: RoundPhase = "analytics"
    model_version: str
    model_artifact_digest: str = ""
    training_code_digest: str = ""
    feature_contract_version: str
    privacy_profile: str = "default"
    aggregation: AggregationMethod = Field(default_factory=AggregationMethod)
    minimum_participants: int = Field(default=3, ge=1)
    minimum_cohort_per_site: int = Field(default=0, ge=0)
    required_capabilities: list[str] = Field(default_factory=list)
    stop_conditions: list[StopCondition] = Field(default_factory=list)
    invited_sites: list[str] = Field(default_factory=list)


class SiteExclusion(DagentsModel):
    """One invited site that will not take part, and why."""

    site_id: str
    reason: str


class RoundPlan(DagentsModel):
    """The compiled, reviewable plan for one round.

    ``required_participants`` is the effective floor, which may exceed the
    manifest's stated minimum when a secure-aggregation threshold is stricter.
    """

    round_id: str
    study_id: str
    phase: RoundPhase
    selected_sites: list[str] = Field(default_factory=list)
    excluded_sites: list[SiteExclusion] = Field(default_factory=list)
    quorum_met: bool
    required_participants: int
    aggregation: AggregationMethod
    stop_reason: StopCondition | None = None
    round_digest: str


class SiteResult(DagentsModel):
    """What one site returns from a round.

    Deliberately minimal. No patient identifier, row, image, note, or
    patient-level prediction has a place in this contract; detailed local
    evidence stays at the site behind ``local_evidence_pointer``.
    """

    round_id: str
    site_id: str
    job_digest: str = ""
    participation: SiteParticipation
    code_verified: bool = False
    privacy_checks_passed: bool = False
    contributed_examples: int = Field(default=0, ge=0)
    update_norm: float | None = None
    metrics: dict[str, float] = Field(default_factory=dict)
    local_evidence_pointer: str | None = None


class ContributionRejection(DagentsModel):
    """One contribution that may not enter the aggregate, and why."""

    site_id: str
    reason: str


class AggregationReadiness(DagentsModel):
    """Whether aggregation may proceed, and on whose contributions.

    ``site_weights`` follow accepted example counts, the FedAvg convention. They
    are reported rather than applied: applying them is the federated runtime's
    job, not the control plane's.
    """

    round_id: str
    accepted_sites: list[str] = Field(default_factory=list)
    rejected_contributions: list[ContributionRejection] = Field(default_factory=list)
    accepted_examples: int = 0
    aggregation_permitted: bool
    stop_reason: StopCondition | None = None
    site_weights: dict[str, float] = Field(default_factory=dict)


class GateComparison(DagentsModel):
    """How a release gate compares an observed metric to its requirement."""

    kind: Literal["at_least", "at_most", "improves_on_baseline"]
    value: float | None = None
    margin: float | None = None


class ReleaseGate(DagentsModel):
    """One condition a candidate must satisfy before it may be released.

    ``blocking`` defaults to ``True``: a gate whose severity nobody stated must
    not be treated as advisory.
    """

    gate_id: str
    metric: str
    comparison: GateComparison
    blocking: bool = True


class GateResult(DagentsModel):
    """Evaluation of one release gate against a candidate's metrics."""

    gate_id: str
    outcome: GateOutcome
    observed: float | None = None
    detail: str = ""


class ReleaseDecision(DagentsModel):
    """The governed verdict on a candidate model.

    A ``release`` action is a recommendation to a human committee, never an
    automatic deployment.
    """

    round_id: str
    candidate_version: str
    gate_results: list[GateResult] = Field(default_factory=list)
    action: ReleaseAction
    blocking_failures: list[str] = Field(default_factory=list)
    rollback_version: str | None = None


class FederatedJob(DagentsModel):
    """The work one site is asked to perform in a round.

    ``manifest_digest`` is what the site checks before it accepts: a job whose
    digest does not match the manifest it was given is not the approved job.
    """

    round_id: str
    site_id: str
    manifest: RoundManifest
    manifest_digest: str
    task: Literal["profile", "evaluate", "train"] = "evaluate"
    feature_fields: list[str] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)


class JobAcceptance(DagentsModel):
    """A site's answer to an offered job.

    A site may always refuse, and the reason is recorded. Refusal before code
    runs is the point of distributing a signed job at all.
    """

    round_id: str
    site_id: str
    accepted: bool
    reason: str = ""
    verified_digest: bool = False
    feature_contract_version: str = ""


class RoundRecord(DagentsModel):
    """The GMA's stored lineage for one round.

    Kept so the path from seed model to round to candidate to release can be
    reconstructed later without reading any site's local evidence.
    """

    manifest: RoundManifest
    plan: RoundPlan
    results: list[SiteResult] = Field(default_factory=list)
    readiness: AggregationReadiness | None = None
    decision: ReleaseDecision | None = None
    created_at: int
    updated_at: int
    status: Literal["planned", "dispatched", "collecting", "aggregated", "closed"] = "planned"


class CandidateModel(DagentsModel):
    """The model an aggregation produced, before any release decision.

    Named "candidate" throughout on purpose. Aggregation produces this; it does
    not produce a release. The path from here to a deployed model runs through
    cross-site evaluation and a human committee.
    """

    round_id: str
    candidate_version: str
    parent_version: str
    aggregation_method: str
    contributing_sites: list[str] = Field(default_factory=list)
    site_weights: dict[str, float] = Field(default_factory=dict)
    contributed_examples: int = 0
    metrics: dict[str, float] = Field(default_factory=dict)
    artifact_uri: str = ""
    produced_at: int = 0


class RoundSubmission(DagentsModel):
    """The federated engine's answer to a dispatched round.

    ``engine`` names which runtime handled it, so a study's evidence records the
    runtime it actually ran on rather than assuming one.
    """

    round_id: str
    engine: str
    dispatched_sites: list[str] = Field(default_factory=list)
    acceptances: list[JobAcceptance] = Field(default_factory=list)
    message: str = ""
