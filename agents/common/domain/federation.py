"""Federated round contracts shared by the GMA, the LMA, and consumer apps.

These contracts describe the round agreement, who may take part, the job each
site is offered, what a site returns, the update store, the model registry,
and what has to be true before a candidate model may be released.

The runtime behind
:class:`~agents.common.application.federation.FederationEngine` moves the jobs
and the clipped updates. It can be the GMA's own HTTP engine or a specialist
runtime such as NVIDIA FLARE. How updates are combined comes from a model kind
the application contributes, so nothing here is a federated optimizer.

The rule these contracts exist to hold: aggregation creates a candidate model,
never an approved release.

Nothing here is healthcare-specific. ``condition_id`` is whatever a consumer
calls the thing being studied, and ``feature_contract_version`` is whichever
schema agreement its sites have signed.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Mapping

from pydantic import ConfigDict, Field, SerializerFunctionWrapHandler, model_serializer

from agents.common.domain.base import DagentsModel
from agents.common.domain.governance import Requester, RestrictionPlan

#: What a round is for.
#:
#: The recommended pilot order is analytics, then evaluation, then training, so
#: a consortium proves it can produce the same measures before any model update
#: moves. The phase is explicit so a study cannot skip ahead silently.
RoundPhase = Literal["analytics", "evaluation", "training"]

#: What a site does in each phase: the ``task`` of every job in the round.
JobTask = Literal["profile", "evaluate", "train"]

#: The one map from a round's phase to its jobs' task. The GMA's HTTP engine,
#: the in-process simulator and the LMA all read it, so a new phase cannot be
#: added to one of them and missed in another.
PHASE_TASKS: Mapping[RoundPhase, JobTask] = {"analytics": "profile", "evaluation": "evaluate", "training": "train"}

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
            "The round's id, chosen by the caller. The GMA stores the round under it and refuses a manifest "
            "whose id is already stored, with 409."
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
    refusal_reason: str | None = Field(
        default=None,
        description=(
            "Why the site refused or could not finish the job, in words, such as `the guard withheld the "
            "outbound model update`. Present only when there is a reason: a result without one leaves the key "
            "out. Stored; no planner rule reads it."
        ),
    )

    @model_serializer(mode="wrap")
    def _leave_out_an_empty_refusal_reason(self, handler: SerializerFunctionWrapHandler):  # noqa: ANN202
        """Leave ``refusal_reason`` out of a result that has none.

        A result without a reason then has exactly the keys it had before the
        field existed, so stored results, recorded examples and consumers that
        check a result's keys against a fixed list see no change.

        The method has no return annotation on purpose: with one, pydantic
        would describe the serialized model by that type instead of by its
        fields, and the OpenAPI schema would lose the fields.
        """
        data = handler(self)
        if isinstance(data, dict) and data.get("refusal_reason") is None:
            data.pop("refusal_reason", None)
        return data


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

    round_id: str = Field(description="The round the job belongs to.")
    site_id: str = Field(description="The site asked to do the job. A site refuses a job addressed to another site.")
    manifest: RoundManifest = Field(description="The round's manifest, as the GMA dispatched it.")
    manifest_digest: str = Field(
        description=(
            "The plan's `round_digest` for the manifest. The site computes the digest itself and refuses the job "
            "when the two differ."
        )
    )
    task: JobTask = Field(
        default="evaluate",
        description="`profile` for an analytics round, `evaluate` for an evaluation round, `train` for a training round.",
    )
    feature_fields: list[str] = Field(
        default_factory=list,
        description=(
            "The fields the job asks to read. The site narrows them to the fields it approved. Empty asks for "
            "every approved field."
        ),
    )
    parameters: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Settings for the site's runner. The GMA's HTTP engine puts the round's model here under `model`, "
            "as a `JobModel`."
        ),
    )


class JobAcceptance(DagentsModel):
    """A site's answer to an offered job.

    A site may always refuse, and the reason is recorded. Refusal before code
    runs is the point of distributing a signed job at all.
    """

    round_id: str = Field(description="The round the job belongs to.")
    site_id: str = Field(description="The site that answered.")
    accepted: bool = Field(description="Whether the site accepted the job. A site that refuses reads no data.")
    reason: str = Field(default="", description="Why the site accepted or refused the job, in words.")
    verified_digest: bool = Field(
        default=False, description="Whether the job's digest matched the digest the site computed for the manifest."
    )
    feature_contract_version: str = Field(
        default="", description="The feature contract version the site produces."
    )


class JobModel(DagentsModel):
    """The model a round's jobs start from, sent to each site with the job.

    The GMA's HTTP engine reads it from its model registry and puts it in
    ``FederatedJob.parameters["model"]``. Before it accepts the job, a site
    checks that ``digest`` equals the manifest's ``model_artifact_digest`` and
    that its own model kind computes the same digest from ``params``.
    """

    version: str = Field(description="The model version, equal to the manifest's `model_version`.")
    kind: str = Field(description="The model kind that runs it, such as a kind an extension contributes.")
    params: dict[str, Any] = Field(description="The model's parameters, in the form its model kind reads.")
    digest: str = Field(description="The digest of `params`, computed by the model kind.")

    @classmethod
    def from_parameters(cls, parameters: Mapping[str, Any]) -> JobModel | None:
        """Read the model from a job's ``parameters``.

        Returns ``None`` when the job carries no model. Raises
        ``pydantic.ValidationError`` (a ``ValueError``) when ``model`` is not
        a valid ``JobModel``.
        """
        raw = parameters.get("model")
        if raw is None:
            return None
        return cls.model_validate(raw)


#: The calls the GMA's HTTP engine makes to an LMA for one site in a round.
DispatchStep = Literal["offer", "execute"]

#: What one dispatch call came to. ``accepted`` and ``refused`` answer an
#: offer. ``completed``, ``rejected``, ``failed``, ``dropped`` and
#: ``accepted`` are the participation of the result an execute call returned.
#: ``failed`` is also any call that did not get an answer it could read.
DispatchOutcome = Literal["accepted", "refused", "completed", "rejected", "failed", "dropped"]


class DispatchCall(DagentsModel):
    """One call the GMA made to an LMA while it dispatched a round."""

    site_id: str = Field(description="The site whose LMA was called.")
    step: DispatchStep = Field(description="`offer` (the site checks the job) or `execute` (the site runs it).")
    status_code: int | None = Field(
        default=None,
        description="The HTTP status the LMA answered with. Null when no answer came: the LMA was unreachable or the call timed out.",
    )
    ms: float = Field(ge=0, description="How long the call took, in milliseconds. At least 0.")
    outcome: DispatchOutcome = Field(
        description=(
            "For an offer: `accepted`, `refused`, or `failed` when no answer could be read. For an execute "
            "call: the participation of the returned result (`completed`, `rejected`, `failed`, `dropped`), or "
            "`failed` when no answer could be read."
        )
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
        description=(
            "The candidate's version. The in-process engine writes `<model_version>+<round_id>`. The HTTP "
            "engine writes `<version_prefix>+<round_id>`, with the model kind's prefix."
        )
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
            "The candidate's metrics: the weighted mean of each metric that every contributing site reported. "
            "A metric only some sites reported is left out."
        ),
    )
    artifact_uri: str = Field(
        default="",
        description=(
            "Where the candidate's parameters are. The in-process engine writes `memory://candidates/<round_id>`, "
            "which names no file."
        ),
    )
    artifact_digest: str = Field(
        default="",
        description=(
            "The digest of the candidate's parameters. Empty from the in-process engine, which makes no "
            "parameters."
        ),
    )
    produced_at: int = Field(default=0, description="When the engine made the candidate, in Unix seconds.")


class RoundRecord(DagentsModel):
    """The GMA's stored lineage for one round.

    Kept so the path from seed model to round to candidate to release can be
    reconstructed later without reading any site's local evidence.
    """

    manifest: RoundManifest = Field(description="The manifest the round was dispatched with.")
    plan: RoundPlan = Field(description="The planner's plan for the round.")
    results: list[SiteResult] = Field(
        default_factory=list,
        description=(
            "One result per site, sorted by site id. A later result from a site replaces its earlier one. With the "
            "HTTP engine every result comes from the site's LMA; none can be posted."
        ),
    )
    readiness: AggregationReadiness | None = Field(
        default=None, description="The readiness check saved by the aggregate endpoint. Null before it."
    )
    decision: ReleaseDecision | None = Field(
        default=None, description="The release decision, saved by the evaluate-release endpoint. Null before it."
    )
    created_at: int = Field(description="When the round was dispatched, in Unix seconds.")
    updated_at: int = Field(description="When the record last changed, in Unix seconds.")
    status: Literal["planned", "dispatched", "collecting", "aggregated", "closed"] = Field(
        default="planned",
        description=(
            "`dispatched` while the engine offers and runs the jobs, `collecting` after a dispatch that met "
            "quorum or a new result, `aggregated` after a candidate was made, `closed` after a dispatch without quorum, a "
            "release decision or a discard. A round is aggregated once and takes one release decision. The GMA never "
            "stores `planned`."
        ),
    )
    acceptances: list[JobAcceptance] = Field(
        default_factory=list,
        description="Each selected site's answer to the job offer, sorted by site id. Empty before dispatch and for a round without quorum.",
    )
    candidate: CandidateModel | None = Field(
        default=None, description="The candidate the aggregate endpoint made from this round. Null before it."
    )
    dispatch: list[DispatchCall] = Field(
        default_factory=list,
        description=(
            "Every call the GMA's HTTP engine made to an LMA for this round, in the order the calls finished. "
            "Empty for the in-process engine."
        ),
    )
    abandoned_reason: str | None = Field(
        default=None,
        description=(
            "Why the round was discarded: the reason the first discard request gave, or `the engine failed while it "
            "dispatched the round`. Null for a round that was not discarded."
        ),
    )


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


#: One coordinate of an uploaded update: a finite JSON number. Strict, so a
#: string such as ``"0.5"`` or a boolean is refused rather than converted.
UpdateValue = Annotated[float, Field(strict=True, allow_inf_nan=False)]


class UpdateUpload(DagentsModel):
    """One site's clipped model update, as its LMA sends it to the GMA's update store.

    The store keeps it until the round is aggregated or discarded. No route
    reads an update back.
    """

    model_config = ConfigDict(extra="forbid")

    job_digest: str = Field(
        description="The round digest from the site's job. It must equal the plan's `round_digest`, or the store answers 409."
    )
    update: dict[str, UpdateValue] = Field(
        description=(
            "The update the site's `before_send` Guard returned, by parameter name. Every value must be a finite "
            "number. The model kind's `check_update` checks the names."
        )
    )


class UpdateReceipt(DagentsModel):
    """What the update store answers when it has stored an update."""

    digest: str = Field(
        description="The digest of the stored update, computed with the model kind's `update_digest`. The site checks it against its own."
    )


#: Where a model version stands in the GMA's model registry. At most one
#: version is ``released`` at a time.
ModelStatus = Literal["candidate", "released", "rejected", "superseded"]

#: Where the metrics behind a release decision came from: the stored
#: aggregates of the study's own rounds, or numbers the caller sent.
MetricsSource = Literal["rounds", "request"]

#: Why the model lifecycle rule refused an event. The planner's
#: ``federation model lifecycle`` command returns one of these.
LifecycleRefusal = Literal[
    "already_released",
    "not_candidate",
    "no_decision",
    "gates_decided",
    "guard_denied",
    "metrics_not_from_rounds",
    "baseline_changed",
]


class ModelReleaseDecision(DagentsModel):
    """The latest release decision for a model version, stored with the version.

    An approval reads it. Decisions stored before ``baseline_version`` or
    ``metrics_source`` existed lack them; the lifecycle rule refuses to approve
    such a decision.
    """

    action: ReleaseAction = Field(description="What the release gates decided: `release`, `another_round` or `reject`.")
    guard_permitted: bool = Field(description="Whether the `before_release` Guard permitted the release.")
    gate_results: list[GateResult] = Field(default_factory=list, description="One result per gate, in gate order.")
    round_id: str = Field(description="The training round the candidate came from.")
    baseline_version: str | None = Field(
        default=None,
        description=(
            "The version the gates compared the candidate with: the version the baseline round measured, or the "
            "version released at the time. An approval is refused once another version is released."
        ),
    )
    metrics_source: MetricsSource | None = Field(
        default=None,
        description=(
            "`rounds` when the gates read the stored aggregates of rounds whose results the GMA's HTTP engine "
            "collected from the sites, `request` when they read metrics a caller sent or posted. Only `rounds` can "
            "lead to an approval. Null for an older decision."
        ),
    )
    decided_at: str | None = Field(default=None, description="When the decision was stored, in ISO 8601. Set by the store.")


class ModelVersion(DagentsModel):
    """One model version in the GMA's model registry."""

    version: str = Field(description="The version's name. A candidate is named `<version_prefix>+<round_id>`.")
    kind: str = Field(description="The model kind that runs it.")
    digest: str = Field(description="The digest of the parameters, computed by the model kind. Checked on every load.")
    status: ModelStatus = Field(
        description="`candidate`, `released` (the version sites use), `rejected` or `superseded`. Only the lifecycle rule changes it."
    )
    parent_version: str | None = Field(
        default=None, description="The version a training round started from. Null for a registered version."
    )
    created_at: str | None = Field(default=None, description="When the version was registered, in ISO 8601.")
    params: dict[str, Any] | None = Field(
        default=None,
        description="The parameters. Only `GET /api/v1/model-versions:released` returns them; other routes leave them null.",
    )
    release_decision: ModelReleaseDecision | None = Field(
        default=None, description="The latest release decision for the version. Null before one."
    )


class ModelVersionRegistration(DagentsModel):
    """A request to register a model version under a name the caller chooses."""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(min_length=1, max_length=64, description="The model kind. An extension must contribute it.")
    params: dict[str, Any] = Field(description="The parameters. The model kind computes their digest and refuses invalid ones.")
    requested_status: Literal["candidate", "released"] = Field(
        default="candidate",
        description="`candidate`, or `released`, which the lifecycle rule allows only when no version is released.",
    )


class ModelVersionList(DagentsModel):
    """The model versions the GMA's registry holds, without their parameters."""

    model_in_use: ModelVersion | None = Field(
        default=None, description="The released version, the one sites use. Null when nothing is released."
    )
    versions: list[ModelVersion] = Field(
        default_factory=list, description="The newest versions first, at most `limit` of them."
    )


class ApprovalResult(DagentsModel):
    """What an approval changed."""

    released: ModelVersion = Field(description="The version now released, without its parameters.")
    previous: str | None = Field(
        default=None, description="The version that was released before. It is now `superseded`. Null when nothing was released."
    )


class ModelStatusChange(DagentsModel):
    """One status change the lifecycle rule asks the registry to apply."""

    version: str = Field(description="The version whose status changes.")
    from_status: ModelStatus | None = Field(description="Its status before the change. Null for a version the event adds.")
    to_status: ModelStatus = Field(description="Its status after the change.")


class ModelLifecycleDecision(DagentsModel):
    """The planner's answer to one model lifecycle event (`dagentsc federation model lifecycle`)."""

    allowed: bool = Field(description="Whether the event is allowed. A refused event changes no status.")
    refusal: LifecycleRefusal | None = Field(
        default=None, description="Why the event was refused. Null when it is allowed."
    )
    reason: str = Field(description="The decision in words.")
    status_changes: list[ModelStatusChange] = Field(
        default_factory=list, description="The status changes to apply, sorted by version. Empty for a refused event."
    )


class ReleaseEvaluationRequest(DagentsModel):
    """A request to decide on a round's candidate.

    Two flows use it:

    - **Round flow**: ``condition_id`` names a condition pack. The GMA reads
      the gates from the pack, the candidate's metrics from
      ``metrics_from_round`` and the baseline from ``baseline_from_round``,
      and runs the ``before_release`` Guard for ``release_requester`` before
      the gates. Gates in the body are refused.
    - **Bare flow**: no ``condition_id``. The GMA evaluates ``gates`` against
      ``candidate_metrics`` and ``baseline_metrics`` as sent, and runs no Guard.
    """

    gates: list[ReleaseGate] = Field(
        default_factory=list,
        description=(
            "Bare flow only: the gates to evaluate, in order, usually a condition pack's `release_gates`. With "
            "no gates the decision is `release`."
        ),
    )
    candidate_version: str = Field(
        description=(
            "The candidate's version, as `POST /api/v1/federation/rounds/{round_id}:aggregate` returned it. "
            "When the round holds a candidate, the two must be equal, or the GMA answers 409."
        )
    )
    candidate_metrics: dict[str, float] = Field(
        default_factory=dict,
        description="Bare flow only: the candidate's measured metrics, by name. The gates read these values as sent.",
    )
    baseline_metrics: dict[str, float] = Field(
        default_factory=dict,
        description="Bare flow only: the released model's metrics, by name. Only `improves_on_baseline` gates read them.",
    )
    rollback_version: str | None = Field(
        default=None,
        description="The version to keep using if the candidate is not released. Default: the round's `model_version`.",
    )
    condition_id: str | None = Field(
        default=None,
        description="Round flow: the condition pack whose gates, classification and purposes apply. Null for the bare flow.",
    )
    metrics_from_round: str | None = Field(
        default=None,
        description=(
            "Round flow: the round whose stored aggregate holds the candidate's metrics, usually a validation "
            "round. A round that was not aggregated gives no metrics, so every gate is `not_evaluated`."
        ),
    )
    baseline_from_round: str | None = Field(
        default=None,
        description="Round flow: the round whose stored aggregate holds the released model's metrics, usually an evaluation round.",
    )
    change_from_version: str | None = Field(
        default=None,
        description=(
            "Round flow: the version the release Guard measures the candidate's change from. The Guard's "
            "payload is the model kind's `parameter_change` between the two."
        ),
    )
    release_requester: Requester | None = Field(
        default=None, description="Round flow: who asks for the release, as the `before_release` Guard assesses them."
    )
    declared_purpose: str | None = Field(
        default=None,
        description="Round flow: why the release is wanted. It must be one of the condition pack's approved purposes.",
    )


class ReleaseGuardCheck(DagentsModel):
    """What the `before_release` Guard decided for a release evaluation."""

    permitted: bool = Field(description="Whether the Guard permitted the release.")
    plan: RestrictionPlan = Field(description="The plan the Guard applied.")
    audit_id: str = Field(description="The id of the record in the GMA's audit chain.")
    message: str = Field(default="", description="The decision in words.")


class ReleaseEvaluation(DagentsModel):
    """The GMA's answer to a release evaluation."""

    decision: ReleaseDecision = Field(description="The release gates' decision. It is also saved in the round's `decision` field.")
    guard: ReleaseGuardCheck | None = Field(
        default=None, description="The `before_release` Guard's decision. Null in the bare flow, which runs no Guard."
    )
    candidate_status: ModelStatus | None = Field(
        default=None,
        description="The candidate's status in the model registry after the decision. Null when the registry does not hold the candidate.",
    )
    metrics_source: MetricsSource = Field(
        description=(
            "`rounds` when the gates read the stored aggregates of rounds whose results the GMA's HTTP engine "
            "collected from the sites. `request` when they read metrics from the body (the bare flow), or "
            "aggregates of rounds whose results callers posted (a GMA on the simulator). Only `rounds` can lead "
            "to an approval."
        )
    )
    baseline_version: str | None = Field(
        default=None,
        description=(
            "The version the baseline round measured. Without a baseline round, the version released when the "
            "decision was made. Null when neither is known."
        ),
    )
