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
    - [manifest]: the round contract.
    - [results]: what each site returned.

    Output:
    - [aggregation_readiness]: accepted sites and weights, rejected
      contributions with reasons, and the stop condition when blocked. *)
val evaluate_aggregation_readiness : round_manifest -> site_result list -> aggregation_readiness

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
