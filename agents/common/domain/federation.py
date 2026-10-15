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

    kind: Literal["fedavg", "fedprox", "fedopt", "secure_aggregation"] = Field(
        default="fedavg",
        description=(
            "How contributions are combined: `fedavg`, `fedprox`, `fedopt` or `secure_aggregation`. Only "
            "`secure_aggregation` changes a planner decision: it raises the participant floor to `threshold` "
            "and requires each completed result to report an `update_norm` of at most 10."
        ),
    )
    mu: float | None = Field(
        default=None,
        description="The FedProx proximal term. Part of the round digest. The planner uses 0.01 when it is missing.",
    )
    optimizer: str | None = Field(
        default=None,
        description="The FedOpt server optimizer, such as `adam`. Part of the round digest. The planner uses `adam` when it is missing.",
    )
    threshold: int | None = Field(
        default=None,
        ge=1,
        description=(
            "For `secure_aggregation`: the fewest sites whose updates may be combined. It replaces the "
            "manifest's `minimum_participants` when it is higher. Set it: without it the planner fails. At least 1."
        ),
    )


class SiteRegistration(DagentsModel):
    """One site's standing enrolment in a study.

    ``enrolled`` defaults to ``False``: taking part has to be stated, never
    inferred from the presence of a record.
    """

    site_id: str = Field(
        description="The site's id. Enrolling the same id again replaces the stored enrolment."
    )
    capabilities: list[str] = Field(
        default_factory=list,
        description="What the site can do, such as `evaluate` or `train`. A round's `required_capabilities` must all be here.",
    )
    feature_contract_version: str = Field(
        description=(
            "The version of the feature contract (the agreed list of fields) the site has signed. It must equal "
            "the round's `feature_contract_version`."
        )
    )
    approved_conditions: list[str] = Field(
        default_factory=list,
        description="The conditions (study topics) the site has approved. The round's `condition_id` must be one of them.",
    )
    policy_version: str = Field(
        default="unversioned", description="The version of the site's local policy. Stored; no rule reads it."
    )
    cohort_size: int = Field(
        default=0,
        ge=0,
        description="How many subjects the site holds. It must be at least the round's `minimum_cohort_per_site`. At least 0.",
    )
    enrolled: bool = Field(
        default=False,
        description="Whether the site has agreed to take part. Default false: a site that does not say so is never selected.",
    )


class RoundManifest(DagentsModel):
    """The round contract distributed to every approved site.

    A site verifies this before any code touches local data: the digests say
    which model and which training code were approved, and
    ``feature_contract_version`` says which fields the round may read.
    """

    round_id: str = Field(
        description=(
            "The round's id, chosen by the caller. The GMA stores the round under it; dispatching a manifest "
            "whose id is already stored replaces that round."
        )
    )
    study_id: str = Field(description="The study the round belongs to. Copied into the plan.")
    condition_id: str = Field(
        description=(
            "What the study is about, named in the application's own words. A site is selected only when its "
            "`approved_conditions` include it."
        )
    )
    phase: RoundPhase = Field(
        default="analytics",
        description=(
            "`analytics`, `evaluation` or `training`. Copied into the plan. The in-process engine turns it into "
            "each site's task: `profile`, `evaluate` or `train`."
        ),
    )
    model_version: str = Field(
        description=(
            "The model version the round starts from. A candidate from the round names it as `parent_version`, "
            "and a release decision falls back to it as the rollback version."
        )
    )
    model_artifact_digest: str = Field(
        default="", description="The digest of the approved model parameters. Part of the round digest; the GMA does not check it."
    )
    training_code_digest: str = Field(
        default="",
        description=(
            "The digest of the approved training code. Part of the round digest. A site that checked it sets "
            "`code_verified` in its result."
        ),
    )
    feature_contract_version: str = Field(
        description="The feature contract the round reads. A site is selected only when its `feature_contract_version` is the same."
    )
    privacy_profile: str = Field(
        default="default", description="A name for the privacy settings the sites apply. Part of the round digest; no rule reads it."
    )
    aggregation: AggregationMethod = Field(
        default_factory=AggregationMethod, description="How the accepted contributions are combined."
    )
    minimum_participants: int = Field(
        default=3,
        ge=1,
        description=(
            "The fewest selected sites for quorum, and the fewest accepted results for aggregation. A higher "
            "`secure_aggregation` threshold replaces it. At least 1."
        ),
    )
    minimum_cohort_per_site: int = Field(
        default=0,
        ge=0,
        description=(
            "The fewest subjects a site must hold to be selected (`cohort_size`) and must use for its result to "
            "count (`contributed_examples`). At least 0."
        ),
    )
    required_capabilities: list[str] = Field(
        default_factory=list, description="Capabilities every selected site must declare, such as `evaluate`."
    )
    stop_conditions: list[StopCondition] = Field(
        default_factory=list,
        description=(
            "The conditions the study says stop a round. Part of the round digest only: the planner sets "
            "`quorum_not_met` or `excessive_dropout` whether or not they are listed, and never sets the others."
        ),
    )
    invited_sites: list[str] = Field(
        default_factory=list,
        description="The sites asked to take part. Only these can be selected. An invited site with no enrolment is listed as excluded.",
    )


class SiteExclusion(DagentsModel):
    """One invited site that will not take part, and why."""

    site_id: str = Field(description="The site that will not take part.")
    reason: str = Field(
        description="The first eligibility check it failed, such as `site lacks required capability evaluate`."
    )


class RoundPlan(DagentsModel):
    """The compiled, reviewable plan for one round.

    ``required_participants`` is the effective floor, which may exceed the
    manifest's stated minimum when a secure-aggregation threshold is stricter.
    """

    round_id: str = Field(description="Copied from the manifest.")
    study_id: str = Field(description="Copied from the manifest.")
    phase: RoundPhase = Field(description="Copied from the manifest.")
    selected_sites: list[str] = Field(
        default_factory=list,
        description=(
            "Stored sites that passed every check, in this order: `enrolled` is true, invited, condition "
            "approved, same feature contract, every required capability, large enough cohort. Sorted by id."
        ),
    )
    excluded_sites: list[SiteExclusion] = Field(
        default_factory=list,
        description="Every other stored site, sorted by id, then each invited site that has no stored enrolment.",
    )
    quorum_met: bool = Field(description="True when `selected_sites` holds at least `required_participants` sites.")
    required_participants: int = Field(
        description="`minimum_participants`, or the `secure_aggregation` threshold when that is higher."
    )
    aggregation: AggregationMethod = Field(
        description="The manifest's aggregation method, with the planner's defaults filled in."
    )
    stop_reason: StopCondition | None = Field(
        default=None, description="`quorum_not_met` when quorum is not met, otherwise null."
    )
    round_digest: str = Field(
        description=(
            "`fnv1a:` and 16 hex digits: an FNV-1a hash of the manifest's fields. A site's result must return it "
            "as `job_digest`. It detects a changed manifest; it is not a cryptographic signature."
        )
    )


class SiteResult(DagentsModel):
    """What one site returns from a round.

    Deliberately minimal. No subject identifier, row, image, note, or
    per-subject prediction has a place in this contract; detailed local
    evidence stays at the site behind ``local_evidence_pointer``.

    A model update is not carried here either. Results are stored with the
    round and shown to the coordinator, so a site that trains sends its clipped
    update through an update channel and reports only its norm
    (``update_norm``) and the digest the channel returned (``update_digest``).
    The runtime that aggregates checks each update against that digest.
    """

    round_id: str = Field(description="The round the result belongs to.")
    site_id: str = Field(description="The site that sends it. A later result from the same site replaces this one.")
    job_digest: str = Field(
        default="",
        description="The round digest the site received with its job. A completed result counts only when it equals the plan's `round_digest`.",
    )
    participation: SiteParticipation = Field(
        description=(
            "`accepted` (not finished yet), `completed`, `rejected` (the site refused the job), `failed` or "
            "`dropped`. Only a `completed` result can count."
        )
    )
    code_verified: bool = Field(
        default=False,
        description="Whether the site checked the training code digest. A completed result counts only when it is true.",
    )
    privacy_checks_passed: bool = Field(
        default=False,
        description="Whether the site's privacy checks passed. A completed result counts only when it is true.",
    )
    contributed_examples: int = Field(
        default=0,
        ge=0,
        description=(
            "How many examples the site used. A completed result counts only when it is at least the manifest's "
            "`minimum_cohort_per_site`. It sets the site's weight. At least 0."
        ),
    )
    update_norm: float | None = Field(
        default=None,
        description=(
            "The L2 norm (length) of the site's clipped model update, when it sent one. Under "
            "`secure_aggregation` it is required and must be at most 10."
        ),
    )
    update_digest: str | None = Field(
        default=None,
        description="The digest the update channel returned for the site's update. Stored; no planner rule reads it.",
    )
    metrics: dict[str, float] = Field(
        default_factory=dict, description="Named measurements the site reports, such as `accuracy`."
    )
    local_evidence_pointer: str | None = Field(
        default=None, description="Where the site keeps its detailed evidence. Stored; the GMA never follows it."
    )


class ContributionRejection(DagentsModel):
    """One contribution that may not enter the aggregate, and why."""

    site_id: str = Field(description="The site whose result does not count.")
    reason: str = Field(
        description="The first check it failed, such as `site reported a failed run` or `returned job digest does not match the round manifest`."
    )


class AggregationReadiness(DagentsModel):
    """Whether aggregation may proceed, and on whose contributions.

    ``site_weights`` follow accepted example counts, the FedAvg convention. They
    are reported rather than applied: applying them is the federated runtime's
    job, not the control plane's.
    """

    round_id: str = Field(description="The round.")
    accepted_sites: list[str] = Field(
        default_factory=list, description="The sites whose results may be combined, sorted by id."
    )
    rejected_contributions: list[ContributionRejection] = Field(
        default_factory=list, description="Every other stored result, sorted by site id, with the reason."
    )
    accepted_examples: int = Field(default=0, description="The sum of `contributed_examples` over the accepted sites.")
    aggregation_permitted: bool = Field(
        description="True when at least as many results are accepted as the plan's `required_participants`."
    )
    stop_reason: StopCondition | None = Field(
        default=None,
        description=(
            "Null when aggregation is permitted. `excessive_dropout` when some sites dropped out and the accepted "
            "and dropped sites together would have been enough; `quorum_not_met` otherwise."
        ),
    )
    site_weights: dict[str, float] = Field(
        default_factory=dict,
        description=(
            "Each accepted site's share of `accepted_examples`, adding up to 1. Equal shares when no accepted "
            "site reported an example count. The engine uses them when it combines contributions."
        ),
    )


class GateComparison(DagentsModel):
    """How a release gate compares an observed metric to its requirement."""

    kind: Literal["at_least", "at_most", "improves_on_baseline"] = Field(
        description=(
            "`at_least`: the metric must be at least `value`. `at_most`: at most `value`. "
            "`improves_on_baseline`: at least the baseline's value plus `margin`."
        )
    )
    value: float | None = Field(
        default=None, description="The limit for `at_least` and `at_most`. The planner uses 0 when it is missing."
    )
    margin: float | None = Field(
        default=None,
        description="How much `improves_on_baseline` asks the candidate to gain over the baseline. The planner uses 0 when it is missing.",
    )


class ReleaseGate(DagentsModel):
    """One condition a candidate must satisfy before it may be released.

    ``blocking`` defaults to ``True``: a gate whose severity nobody stated must
    not be treated as advisory.
    """

    gate_id: str = Field(description="The gate's id. It names the gate in `gate_results` and `blocking_failures`.")
    metric: str = Field(description="The name of the candidate metric the gate reads, such as `accuracy`.")
    comparison: GateComparison = Field(description="What the metric is compared with.")
    blocking: bool = Field(
        default=True,
        description=(
            "Whether a gate that does not pass rejects the candidate. Default true. A non-blocking gate that "
            "does not pass asks for another round instead."
        ),
    )


class GateResult(DagentsModel):
    """Evaluation of one release gate against a candidate's metrics.

    ``threshold`` is the value the planner compared ``observed`` with: the
    gate's value for ``at_least`` and ``at_most``, and baseline + margin for
    ``improves_on_baseline``. It is ``None`` when an improvement gate has no
    baseline metric. Read it from here; do not recompute it.
    """

    gate_id: str = Field(description="The gate's id.")
    outcome: GateOutcome = Field(
        description=(
            "`passed`, `failed`, or `not_evaluated` when the candidate has no such metric (or, for "
            "`improves_on_baseline`, the baseline has none)."
        )
    )
    observed: float | None = Field(default=None, description="The candidate's value for the metric. Null when it has none.")
    threshold: float | None = Field(
        default=None,
        description=(
            "The value `observed` was compared with: the gate's `value`, or the baseline plus the margin. Null "
            "when an improvement gate has no baseline value."
        ),
    )
    detail: str = Field(
        default="", description="The comparison in words, such as `accuracy=0.84 required >= 0.8`."
    )


class ReleaseDecision(DagentsModel):
    """The governed verdict on a candidate model.

    A ``release`` action is a recommendation to a human committee, never an
    automatic deployment.
    """

    round_id: str = Field(description="The round the candidate came from.")
    candidate_version: str = Field(description="The candidate's version, copied from the request.")
    gate_results: list[GateResult] = Field(
        default_factory=list, description="One result per gate, in the order of the request."
    )
    action: ReleaseAction = Field(
        description=(
            "`reject` when a blocking gate did not pass; otherwise `another_round` when any gate did not pass; "
            "otherwise `release`. A `release` is a recommendation to the people who approve releases; nothing "
            "is deployed."
        )
    )
    blocking_failures: list[str] = Field(
        default_factory=list, description="`<gate_id>: <detail>` for each blocking gate that did not pass."
    )
    rollback_version: str | None = Field(
        default=None,
        description="The version to keep using if the candidate is not released: the request's `rollback_version`, or the round's `model_version`.",
    )


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

    manifest: RoundManifest = Field(description="The manifest the round was dispatched with.")
    plan: RoundPlan = Field(description="The planner's plan for the round.")
    results: list[SiteResult] = Field(
        default_factory=list, description="One result per site, sorted by site id. A later result from a site replaces its earlier one."
    )
    readiness: AggregationReadiness | None = Field(
        default=None, description="The last readiness check, saved by the readiness and aggregate endpoints. Null before either."
    )
    decision: ReleaseDecision | None = Field(
        default=None, description="The release decision, saved by the evaluate-release endpoint. Null before it."
    )
    created_at: int = Field(description="When the round was dispatched, in Unix seconds.")
    updated_at: int = Field(description="When the record last changed, in Unix seconds.")
    status: Literal["planned", "dispatched", "collecting", "aggregated", "closed"] = Field(
        default="planned",
        description=(
            "`collecting` after a dispatch that met quorum or a new result, `aggregated` after a candidate was "
            "made, `closed` after a dispatch without quorum or a release decision. The GMA never stores "
            "`planned` or `dispatched`."
        ),
    )


class CandidateModel(DagentsModel):
    """The model an aggregation produced, before any release decision.

    Named "candidate" throughout on purpose. Aggregation produces this; it does
    not produce a release. The path from here to a deployed model runs through
    cross-site evaluation and a human committee.

    ``artifact_digest`` identifies the parameters at ``artifact_uri``. A
    runtime that produces real parameters sets it, so the next round's manifest
    can name them in ``model_artifact_digest``. The in-process simulator
    produces no parameters and leaves it empty.
    """

    round_id: str = Field(description="The round that produced the candidate.")
    candidate_version: str = Field(
        description="The candidate's version. The in-process engine writes `<model_version>+<round_id>`."
    )
    parent_version: str = Field(description="The round manifest's `model_version`.")
    aggregation_method: str = Field(description="The manifest's aggregation `kind`, such as `fedavg`.")
    contributing_sites: list[str] = Field(
        default_factory=list, description="The accepted sites from the readiness check."
    )
    site_weights: dict[str, float] = Field(
        default_factory=dict, description="Each contributing site's weight, from the readiness check."
    )
    contributed_examples: int = Field(
        default=0, description="The examples the contributing sites used, from the readiness check."
    )
    metrics: dict[str, float] = Field(
        default_factory=dict,
        description=(
            "The candidate's metrics. The in-process engine gives the weighted mean of each metric that every "
            "contributing site reported, and leaves out a metric only some sites reported."
        ),
    )
    artifact_uri: str = Field(
        default="",
        description="Where the candidate's parameters are. The in-process engine writes `memory://candidates/<round_id>`, which names no file.",
    )
    artifact_digest: str = Field(
        default="",
        description="The digest of the parameters at `artifact_uri`. Empty from the in-process engine, which makes no parameters.",
    )
    produced_at: int = Field(default=0, description="When the engine made the candidate, in Unix seconds.")


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


#: Where the GMA learned a site's address: from ``GMA_FEDERATION_SITES`` at
#: start-up (``configured``), or from a call at run time (``joined``).
SiteAddressSource = Literal["configured", "joined"]

#: Why the GMA refused an address offered at run time.
SiteAddressRefusalReason = Literal["not_joinable", "address_differs", "configured_elsewhere"]


class SiteAddress(DagentsModel):
    """The address the GMA uses to reach one site's LMA.

    An address alone never enrols a site. Enrolment stays a separate call, so
    a site with an address and no enrolment is never selected for a round.
    """

    site_id: str = Field(description="The site's id.")
    url: str = Field(
        description="The base address the GMA sends the site's jobs to, without a trailing `/`."
    )
    source: SiteAddressSource = Field(
        description=(
            "`configured` when the address comes from `GMA_FEDERATION_SITES` at start-up, `joined` when it was "
            "accepted at run time from `GMA_FEDERATION_JOINABLE_SITES`."
        )
    )


class SiteAddressList(DagentsModel):
    """The addresses the GMA knows, and the sites it would accept at run time."""

    joinable: list[str] = Field(
        default_factory=list,
        description=(
            "The ids listed in `GMA_FEDERATION_JOINABLE_SITES`, sorted, whether or not they have joined. Empty when "
            "the GMA accepts no address at run time."
        ),
    )
    addresses: list[SiteAddress] = Field(
        default_factory=list,
        description="Every address the GMA uses: the configured ones first, then the joined ones, each group sorted by `site_id`.",
    )


class SiteAddressRequest(DagentsModel):
    """An address offered for one site at run time."""

    url: str = Field(
        min_length=1,
        max_length=2048,
        description=(
            "The site's base address. It must equal the site's entry in `GMA_FEDERATION_JOINABLE_SITES`; a trailing "
            "`/` is ignored. 1 to 2048 characters."
        ),
    )


class SiteAddressRefused(ValueError):
    """The GMA does not accept this address for this site.

    ``reason`` says which rule refused it: the site is not joinable, the
    address differs from the joinable entry, or the site is configured at
    start-up with another address.
    """

    def __init__(self, site_id: str, reason: SiteAddressRefusalReason) -> None:
        super().__init__(f"site {site_id}: {reason}")
        self.site_id = site_id
        self.reason = reason
