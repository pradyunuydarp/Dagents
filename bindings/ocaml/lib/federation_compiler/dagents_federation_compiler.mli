(** Federated round planning: who may take part, whether aggregation may
    proceed, and whether a candidate may be released.

    This module governs a federation without running one. It never opens a
    socket, never touches a model artifact, and never trains anything; the
    distributed protocol belongs to a specialist runtime such as NVIDIA FLARE,
    reached from the GMA through an adapter. What lives here is the part that
    has to be deterministic and reviewable: the round contract, the eligibility
    rules, the quorum arithmetic, and the release gates.

    The invariant the module exists to hold: aggregation produces a candidate,
    never a release. {!evaluate_release} can recommend a release, but a
    recommendation is an input to a human committee, not a deployment.

    Example test case:
    {[
      let plan = compile_round_plan manifest registrations in
      assert (plan.quorum_met = (List.length plan.selected_sites >= manifest.minimum_participants))
    ]} *)

open Dagents_common_ir

(** Derive a deterministic content digest for a round manifest.

    This is a non-cryptographic FNV-1a digest over the manifest's canonical
    field order. It detects an altered round contract and gives a round a
    stable identity across replays; it is explicitly not a signature and
    carries no authenticity guarantee. Signing belongs to the supply-chain
    controls outside this planner.

    Inputs:
    - [manifest]: the round contract.

    Output: a lowercase hexadecimal digest string. *)
val round_digest : round_manifest -> string

(** Select the sites that may take part in a round.

    A site is excluded, with a recorded reason, when it is not enrolled, was
    not invited, has not approved the study's condition, is on a different
    feature-contract version, lacks a required capability, or has a local
    cohort below the per-site floor. Exclusion reasons are part of the round
    evidence, so a site that was dropped can always be explained.

    Inputs:
    - [manifest]: the round contract, including invited sites and thresholds.
    - [registrations]: the consortium's standing site enrolments.

    Output:
    - [round_plan]: selected sites, excluded sites with reasons, whether quorum
      was met, and the stop condition when it was not.

    Example test case:
    {[
      let plan = compile_round_plan manifest [] in
      assert (plan.quorum_met = false);
      assert (plan.plan_stop_reason = Some QuorumNotMet)
    ]} *)
val compile_round_plan : round_manifest -> site_registration list -> round_plan

(** Decide whether returned contributions may be aggregated.

    A result from a site outside [selection] is rejected first, with the reason
    "site was not selected for this round"; it counts toward neither the quorum,
    the dropouts nor the weights. [Unchecked] skips that check.

    A contribution is rejected when the site did not complete, failed code
    verification, failed its privacy checks, returned a mismatched job digest,
    contributed fewer examples than the per-site floor, or exceeded the norm
    bound implied by a secure-aggregation profile. Aggregation is permitted
    only once the surviving contributions still meet the participant threshold,
    which is what stops a round from revealing a single site's update.

    Site weights are proportional to accepted example counts, the FedAvg
    convention; they are returned rather than applied, because applying them is
    the federated runtime's job.

    Inputs:
    - [selection]: the sites the round plan selected, or [Unchecked].
    - [manifest]: the round contract.
    - [results]: what each site returned.

    Output:
    - [aggregation_readiness]: accepted sites and weights, rejected
      contributions with reasons, and the stop condition when blocked.

    Example test case:
    {[
      let readiness =
        evaluate_aggregation_readiness ~selection:(Selected [ "site-a" ]) manifest [ result_from_site_b ]
      in
      assert (readiness.accepted_sites = [])
    ]} *)
val evaluate_aggregation_readiness :
  selection:selection -> round_manifest -> site_result list -> aggregation_readiness

(** Evaluate release gates against a candidate model's metrics.

    A gate whose metric is absent from the candidate's metrics is reported as
    [GateNotEvaluated] rather than silently passing, and an unevaluated
    blocking gate blocks the release. [ImprovesOnBaseline] compares against the
    baseline metrics, and reports not-evaluated when either side is missing.

    A failing blocking gate yields [RejectCandidate]. A failing non-blocking
    gate, or an unevaluated non-blocking gate, yields [RequireAnotherRound]:
    the candidate is not rejected but is not releasable as it stands.

    Inputs:
    - [gates]: the conditions the candidate must satisfy.
    - [candidate_metrics]: the candidate's measured metrics.
    - [baseline_metrics]: the current approved model's metrics.
    - [candidate_version]: the candidate's version identifier.
    - [rollback_version]: the last known-good version to restore on rejection.
    - [round_id]: the round that produced the candidate.

    Output:
    - [release_decision]: per-gate results, the recommended action, and the
      blocking failures behind it. *)
val evaluate_release :
  release_gate list ->
  (string * float) list ->
  (string * float) list ->
  string ->
  string option ->
  string ->
  release_decision

(** Decide the status changes that follow one event on one model version.

    Every status change of a model version comes from here, so a registry
    applies the answer and keeps no status rule of its own:

    - [Registered]: a new version is a candidate, or the release when it asks
      to be one and no version is released ([AlreadyReleased] otherwise).
    - [Aggregated]: the new version is a candidate. Its parent becomes
      superseded when the parent is a candidate, and keeps its status
      otherwise.
    - [ReleaseEvaluated]: the version stays a candidate when the gates decided
      release and the release Guard permitted it, and becomes rejected
      otherwise ([NotCandidate] when it is not a candidate).
    - [ReleaseEvaluatedWithStoredDecision]: the same event with the decision
      the version already holds. This check runs before the status is looked
      at. When the stored decision is for the same round and all six fields
      are equal, the event is allowed and changes nothing, whatever the
      status, so a retried evaluation succeeds. When it is for the same round
      and any field differs, the event is refused ([DecisionDiffers], naming
      the fields in field order). Gate results are compared item by item, in
      order, and their numbers with no tolerance ([Float.equal], so -0.0
      equals 0.0). A stored decision for another round leaves the event to
      the [ReleaseEvaluated] rule, with the same answer.
    - [ApprovalRequested]: the version becomes released and the released
      version becomes superseded. The checks run in this order, and the first
      that fails is the refusal: [NoDecision], [GatesDecided], [GuardDenied],
      [MetricsNotFromRounds], [NotCandidate], [BaselineChanged].

    A refusal changes nothing. Status changes are sorted by version. The
    reason is one sentence; the approval refusals use the wording the
    healthcare demo's approval has always used.

    Raises [Invalid_argument] for an event that names one version twice: an
    aggregation whose parent is the new version, or an approval of a
    candidate that is also given as the released version.

    Example test case:
    {[
      let outcome =
        decide_model_lifecycle
          {
            lifecycle_version = "model-2";
            lifecycle_event = ApprovalRequested { status = ModelCandidate; decision = None; released_version = None };
          }
      in
      assert (outcome.verdict = LifecycleRefused NoDecision)
    ]} *)
val decide_model_lifecycle : lifecycle_request -> lifecycle_outcome

(** Decide whether a round stored before a restart is kept or closed.

    The rows are matched in this order, and the first that applies answers:

    + [RoundAggregated] or [RoundClosed]: keep, whatever else is given.
    + [RoundPlanned], [RoundDispatched] or [RoundCollecting] whose record
      cannot be read: close, [UnreadableRecord].
    + [RoundPlanned] or [RoundDispatched]: close, [DispatchInterrupted],
      because a dispatch cannot resume after a restart.
    + [RoundCollecting] in the analytics or evaluation phase: keep, because
      the record holds the results.
    + [RoundCollecting] training round whose candidate is registered: keep,
      because a retried aggregate returns that candidate.
    + [RoundCollecting] training round whose updates were not kept: close,
      [UpdatesNotKept].
    + [RoundCollecting] training round whose oldest stored update is older
      than the age limit, when both are given: close, [UpdatesExpired]. An
      update exactly at the limit is not older.
    + Otherwise: keep.

    The clock stays with the caller: ages are inputs. The message is one
    sentence. Nothing here raises.

    Example test case:
    {[
      let answer =
        decide_round_recovery
          {
            recovery_status = RoundDispatched;
            recovery_phase = TrainingRound;
            record_readable = true;
            updates_kept = true;
            candidate_registered = false;
            oldest_update_age_seconds = None;
            update_max_age_seconds = Some 86400.0;
          }
      in
      assert (answer.recovery_verdict = CloseRound DispatchInterrupted)
    ]} *)
val decide_round_recovery : round_recovery_request -> round_recovery
