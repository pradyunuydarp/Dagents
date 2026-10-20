(** Federated round planning implementation.

    Pure and total throughout. Round planning has to be replayable months later
    during an audit, so nothing here reads a clock, a random source, or the
    network; every output is a function of the manifest and the results it is
    given. *)

open Dagents_common_ir

(** FNV-1a over a canonical string, rendered as 16 hex digits.

    Chosen because it is short, dependency-free, and deterministic across
    platforms. It detects accidental or careless manifest drift. It is not a
    cryptographic digest and must not be relied on to detect a deliberate
    forgery. *)
let fnv1a text =
  let offset_basis = 0xcbf29ce484222325L in
  let prime = 0x100000001b3L in
  let hash = ref offset_basis in
  String.iter
    (fun character ->
      hash := Int64.logxor !hash (Int64.of_int (Char.code character));
      hash := Int64.mul !hash prime)
    text;
  Printf.sprintf "%016Lx" !hash

(** Render a manifest into the canonical string the digest is taken over. *)
let canonical_manifest manifest =
  String.concat "|"
    [
      manifest.round_id;
      manifest.study_id;
      manifest.condition_id;
      string_of_round_phase manifest.phase;
      manifest.model_version;
      manifest.model_artifact_digest;
      manifest.training_code_digest;
      manifest.feature_contract_version;
      manifest.privacy_profile;
      string_of_aggregation_method manifest.aggregation;
      string_of_int manifest.minimum_participants;
      string_of_int manifest.minimum_cohort_per_site;
      String.concat "," (List.sort compare manifest.required_capabilities);
      String.concat "," (List.sort compare (List.map string_of_stop_condition manifest.stop_conditions));
      String.concat "," (List.sort compare manifest.invited_sites);
    ]

let round_digest manifest = "fnv1a:" ^ fnv1a (canonical_manifest manifest)

(** The participation floor implied by the aggregation method.

    A secure-aggregation profile carries its own threshold, and that threshold
    is the whole reason the coordinator cannot read an individual update. Where
    it is stricter than the manifest's stated minimum, it wins. *)
let effective_minimum manifest =
  match manifest.aggregation with
  | SecureAggregation threshold -> max manifest.minimum_participants threshold
  | FedAvg | FedProx _ | FedOpt _ -> manifest.minimum_participants

(** Decide whether one registered site may join, returning the reason if not. *)
let eligibility_failure manifest registration =
  if not registration.site_enrolled then Some "site is not enrolled in the study"
  else if not (List.mem registration.site_id manifest.invited_sites) then
    Some "site was not invited to this round"
  else if not (List.mem manifest.condition_id registration.approved_conditions) then
    Some (Printf.sprintf "site has not approved condition %s" manifest.condition_id)
  else if registration.site_feature_contract_version <> manifest.feature_contract_version then
    Some
      (Printf.sprintf "site feature contract %s does not match round contract %s"
         registration.site_feature_contract_version manifest.feature_contract_version)
  else
    match
      List.find_opt
        (fun capability -> not (List.mem capability registration.site_capabilities))
        manifest.required_capabilities
    with
    | Some missing -> Some ("site lacks required capability " ^ missing)
    | None ->
        if registration.site_cohort_size < manifest.minimum_cohort_per_site then
          Some
            (Printf.sprintf "site cohort %d is below the per-site minimum %d" registration.site_cohort_size
               manifest.minimum_cohort_per_site)
        else None

let compile_round_plan manifest registrations =
  let sorted = List.sort (fun left right -> compare left.site_id right.site_id) registrations in
  let selected, excluded =
    List.fold_left
      (fun (selected, excluded) registration ->
        match eligibility_failure manifest registration with
        | None -> (registration.site_id :: selected, excluded)
        | Some reason ->
            (selected, { excluded_site_id = registration.site_id; exclusion_reason = reason } :: excluded))
      ([], []) sorted
  in
  let selected = List.rev selected in
  let excluded = List.rev excluded in
  (* An invited site that never registered is an exclusion too; leaving it
     silent would make a short round look fully subscribed. *)
  let unregistered =
    List.filter
      (fun site_id -> not (List.exists (fun registration -> registration.site_id = site_id) registrations))
      (List.sort compare manifest.invited_sites)
    |> List.map (fun site_id ->
           { excluded_site_id = site_id; exclusion_reason = "invited site has no registration" })
  in
  let excluded = excluded @ unregistered in
  let required = effective_minimum manifest in
  let quorum_met = List.length selected >= required in
  {
    plan_round_id = manifest.round_id;
    plan_study_id = manifest.study_id;
    plan_phase = manifest.phase;
    selected_sites = selected;
    excluded_sites = excluded;
    quorum_met;
    required_participants = required;
    plan_aggregation = manifest.aggregation;
    plan_stop_reason = (if quorum_met then None else Some QuorumNotMet);
    round_digest = round_digest manifest;
  }

(** The norm bound a contribution may not exceed, when the profile implies one. *)
let contribution_bound manifest =
  match manifest.aggregation with SecureAggregation _ -> Some 10.0 | FedAvg | FedProx _ | FedOpt _ -> None

(** Whether the round plan selected this site. [Unchecked] selects every site. *)
let site_selected selection site_id =
  match selection with Unchecked -> true | Selected sites -> List.mem site_id sites

(** Decide whether one returned contribution may enter the aggregate.

    The selection is checked first, so a site that was never part of the round
    is reported as such whatever else its result says. *)
let contribution_failure selection manifest result =
  let expected_digest = round_digest manifest in
  if not (site_selected selection result.result_site_id) then Some "site was not selected for this round"
  else
    match result.participation with
    | SiteRejected -> Some "site rejected the job"
    | SiteFailed -> Some "site reported a failed run"
    | SiteDropped -> Some "site dropped out of the round"
    | SiteAccepted -> Some "site accepted the job but has not completed it"
    | SiteCompleted ->
        if result.result_round_id <> manifest.round_id then Some "result belongs to a different round"
        else if result.result_job_digest <> expected_digest then
          Some "returned job digest does not match the round manifest"
        else if not result.code_verified then Some "site did not verify the training code digest"
        else if not result.privacy_checks_passed then Some "site privacy checks did not pass"
        else if result.contributed_examples < manifest.minimum_cohort_per_site then
          Some
            (Printf.sprintf "contributed %d examples, below the per-site minimum %d" result.contributed_examples
               manifest.minimum_cohort_per_site)
        else (
          match (contribution_bound manifest, result.update_norm) with
          | Some bound, Some norm when norm > bound ->
              Some (Printf.sprintf "update norm %g exceeds the permitted bound %g" norm bound)
          | Some _, None -> Some "update norm is required under a secure-aggregation profile"
          | _ -> None)

let evaluate_aggregation_readiness ~selection manifest results =
  let sorted = List.sort (fun left right -> compare left.result_site_id right.result_site_id) results in
  let accepted, rejected =
    List.fold_left
      (fun (accepted, rejected) result ->
        match contribution_failure selection manifest result with
        | None -> (result :: accepted, rejected)
        | Some reason ->
            ( accepted,
              { rejected_site_id = result.result_site_id; rejection_reason = reason } :: rejected ))
      ([], []) sorted
  in
  let accepted = List.rev accepted in
  let rejected = List.rev rejected in
  let accepted_examples = List.fold_left (fun total result -> total + result.contributed_examples) 0 accepted in
  let required = effective_minimum manifest in
  let permitted = List.length accepted >= required in
  let weights =
    if accepted_examples <= 0 then
      (* Equal weights when no site reported an example count, so aggregation
         is still well defined rather than dividing by zero. *)
      let count = List.length accepted in
      if count = 0 then []
      else List.map (fun result -> (result.result_site_id, 1.0 /. float_of_int count)) accepted
    else
      List.map
        (fun result ->
          (result.result_site_id, float_of_int result.contributed_examples /. float_of_int accepted_examples))
        accepted
  in
  (* A site the plan did not select cannot drop out of the round. *)
  let dropouts =
    List.length
      (List.filter
         (fun result -> result.participation = SiteDropped && site_selected selection result.result_site_id)
         results)
  in
  let stop_reason =
    if permitted then None
    else if dropouts > 0 && List.length accepted + dropouts >= required then Some ExcessiveDropout
    else Some QuorumNotMet
  in
  {
    readiness_round_id = manifest.round_id;
    accepted_sites = List.map (fun result -> result.result_site_id) accepted;
    rejected_contributions = rejected;
    accepted_examples;
    aggregation_permitted = permitted;
    readiness_stop_reason = stop_reason;
    site_weights = weights;
  }

(** The value a gate compares the observed metric with, if one exists.

    An improvement gate has a threshold only when the baseline reported its
    metric. *)
let gate_threshold gate baseline_metrics =
  match gate.comparison with
  | AtLeast threshold | AtMost threshold -> Some threshold
  | ImprovesOnBaseline margin ->
      Option.map (fun baseline -> baseline +. margin) (List.assoc_opt gate.gate_metric baseline_metrics)

(** Evaluate one release gate against candidate and baseline metrics. *)
let evaluate_gate gate candidate_metrics baseline_metrics =
  let observed = List.assoc_opt gate.gate_metric candidate_metrics in
  let threshold = gate_threshold gate baseline_metrics in
  match (gate.comparison, observed) with
  | _, None ->
      {
        result_gate_id = gate.gate_id;
        outcome = GateNotEvaluated;
        observed = None;
        threshold;
        gate_detail = Printf.sprintf "candidate reported no metric named %s" gate.gate_metric;
      }
  | AtLeast limit, Some value ->
      {
        result_gate_id = gate.gate_id;
        outcome = (if value >= limit then GatePassed else GateFailed);
        observed = Some value;
        threshold;
        gate_detail = Printf.sprintf "%s=%g required >= %g" gate.gate_metric value limit;
      }
  | AtMost limit, Some value ->
      {
        result_gate_id = gate.gate_id;
        outcome = (if value <= limit then GatePassed else GateFailed);
        observed = Some value;
        threshold;
        gate_detail = Printf.sprintf "%s=%g required <= %g" gate.gate_metric value limit;
      }
  | ImprovesOnBaseline margin, Some value -> (
      match List.assoc_opt gate.gate_metric baseline_metrics with
      | None ->
          {
            result_gate_id = gate.gate_id;
            outcome = GateNotEvaluated;
            observed = Some value;
            threshold;
            gate_detail = Printf.sprintf "baseline reported no metric named %s" gate.gate_metric;
          }
      | Some baseline ->
          {
            result_gate_id = gate.gate_id;
            outcome = (if value >= baseline +. margin then GatePassed else GateFailed);
            observed = Some value;
            threshold;
            gate_detail =
              Printf.sprintf "%s=%g required >= baseline %g + margin %g" gate.gate_metric value baseline margin;
          })

let evaluate_release gates candidate_metrics baseline_metrics candidate_version rollback_version round_id =
  let results = List.map (fun gate -> evaluate_gate gate candidate_metrics baseline_metrics) gates in
  let paired = List.combine gates results in
  let blocking_failures =
    List.filter_map
      (fun (gate, result) ->
        if (not gate.gate_blocking) || result.outcome = GatePassed then None
        else Some (Printf.sprintf "%s: %s" gate.gate_id result.gate_detail))
      paired
  in
  let unresolved =
    List.exists (fun (_, result) -> result.outcome <> GatePassed) paired
  in
  let action =
    if blocking_failures <> [] then RejectCandidate
    else if unresolved then RequireAnotherRound
    else ReleaseCandidate
  in
  {
    decision_round_id = round_id;
    candidate_version;
    gate_results = results;
    action;
    blocking_failures;
    rollback_version;
  }

(** Join words as a sentence lists them: ["a"], ["a and b"], ["a, b and c"]. *)
let join_words words =
  match List.rev words with
  | [] -> ""
  | [ only ] -> only
  | last :: earlier -> String.concat ", " (List.rev earlier) ^ " and " ^ last

(** The sentence for a lifecycle refusal.

    The approval refusals keep the wording the healthcare demo's approval
    used before this rule moved into the planner, so what a person reads does
    not change. *)
let refusal_reason version = function
  | AlreadyReleased released ->
      Printf.sprintf "%s cannot be registered as released while %s is released" version released
  | NotCandidate status -> Printf.sprintf "%s is %s, not a candidate" version (string_of_model_status status)
  | NoDecision -> Printf.sprintf "%s has no release decision; only a pilot's candidate can be approved" version
  | GatesDecided action ->
      Printf.sprintf "the release gates decided %s for %s" (string_of_release_action action) version
  | GuardDenied -> Printf.sprintf "the release Guard did not permit %s" version
  | MetricsNotFromRounds ->
      Printf.sprintf "the release decision for %s did not take its metrics from the rounds" version
  | BaselineChanged { checked_against; in_use } ->
      let checked =
        match checked_against with
        | Some baseline -> "was checked against " ^ baseline
        | None -> "has no record of the model it was checked against"
      in
      let now = match in_use with Some released -> released ^ " is now in use" | None -> "no model is now in use" in
      Printf.sprintf "%s %s, but %s. Run the pilot again." version checked now
  | DecisionDiffers { differing_round_id; differing_fields } ->
      Printf.sprintf "%s already has a release decision for round %s, and this one differs in %s" version
        differing_round_id
        (join_words (List.map string_of_decision_field differing_fields))

(** A refused event: no status changes, and the refusal's sentence. *)
let refuse version refusal =
  { verdict = LifecycleRefused refusal; lifecycle_reason = refusal_reason version refusal }

(** An allowed event, with its status changes sorted by version. *)
let allow reason changes =
  let sorted = List.sort (fun left right -> compare left.changed_version right.changed_version) changes in
  { verdict = LifecycleAllowed sorted; lifecycle_reason = reason }

(** Decide whether a person may release a candidate, in the order the checks
    have always run: decision, gates, Guard, metrics source, status, baseline. *)
let decide_approval version status decision released_version =
  match decision with
  | None -> refuse version NoDecision
  | Some decision -> (
      match decision.recorded_action with
      | (RequireAnotherRound | RejectCandidate) as action -> refuse version (GatesDecided action)
      | ReleaseCandidate -> (
          if not decision.recorded_guard_permitted then refuse version GuardDenied
          else
            match decision.recorded_metrics_source with
            | MetricsFromRequest | MetricsSourceUnrecorded -> refuse version MetricsNotFromRounds
            | MetricsFromRounds -> (
                match status with
                | ModelReleased | ModelRejected | ModelSuperseded -> refuse version (NotCandidate status)
                | ModelCandidate -> (
                    if decision.recorded_baseline_version <> released_version then
                      refuse version
                        (BaselineChanged
                           { checked_against = decision.recorded_baseline_version; in_use = released_version })
                    else
                      let released =
                        { changed_version = version; from_status = Some ModelCandidate; to_status = ModelReleased }
                      in
                      match released_version with
                      | None -> allow (Printf.sprintf "%s is released" version) [ released ]
                      | Some previous when previous = version ->
                          invalid_arg (version ^ " is given both as a candidate and as the released version")
                      | Some previous ->
                          allow
                            (Printf.sprintf "%s is released; %s is superseded" version previous)
                            [
                              released;
                              {
                                changed_version = previous;
                                from_status = Some ModelReleased;
                                to_status = ModelSuperseded;
                              };
                            ]))))

(** Decide a [release_evaluated] event from the version's status, the gates'
    action and the Guard verdict: the rule as it was before the stored
    decision existed. *)
let decide_release_evaluated version status action guard_permitted =
  match status with
  | ModelReleased | ModelRejected | ModelSuperseded -> refuse version (NotCandidate status)
  | ModelCandidate -> (
      let rejected because =
        allow
          (Printf.sprintf "%s is rejected, because %s" version because)
          [ { changed_version = version; from_status = Some ModelCandidate; to_status = ModelRejected } ]
      in
      (* The gates are named before the Guard, as in the approval order. *)
      match (action, guard_permitted) with
      | ReleaseCandidate, true ->
          allow
            (Printf.sprintf
               "%s stays a candidate: the release gates decided release and the release Guard permitted it"
               version)
            []
      | ReleaseCandidate, false -> rejected "the release Guard did not permit it"
      | (RequireAnotherRound | RejectCandidate), _ ->
          rejected ("the release gates decided " ^ string_of_release_action action))

(** Whether two optional numbers are equal with no tolerance. [Float.equal]
    counts -0.0 and 0.0 as equal, and NaN as equal to itself. *)
let same_number left right =
  match (left, right) with
  | None, None -> true
  | Some left, Some right -> Float.equal left right
  | None, Some _ | Some _, None -> false

(** Whether two gate results are equal in every field. *)
let same_gate_result left right =
  left.result_gate_id = right.result_gate_id
  && left.outcome = right.outcome
  && same_number left.observed right.observed
  && same_number left.threshold right.threshold
  && left.gate_detail = right.gate_detail

(** Whether two lists of gate results are equal, item by item, in order. *)
let same_gate_results left right =
  List.length left = List.length right && List.for_all2 same_gate_result left right

(** The fields in which a new decision differs from the stored one, in field
    order. The round ids are compared by the caller. *)
let differing_decision_fields new_decision stored =
  List.filter_map
    (fun (field, same) -> if same then None else Some field)
    [
      (DecisionAction, new_decision.decided_action = stored.decided_action);
      (DecisionGuardPermitted, new_decision.decided_guard_permitted = stored.decided_guard_permitted);
      (DecisionBaselineVersion, new_decision.decided_baseline_version = stored.decided_baseline_version);
      (DecisionMetricsSource, new_decision.decided_metrics_source = stored.decided_metrics_source);
      (DecisionGateResults, same_gate_results new_decision.decided_gate_results stored.decided_gate_results);
    ]

(** Decide a [release_evaluated] event that carries the version's stored
    decision. A stored decision for another round leaves the event to
    {!decide_release_evaluated}. For the same round, the status does not
    matter: the same decision changes nothing, and any other is refused. *)
let decide_release_evaluated_with_stored version status new_decision stored =
  if new_decision.decided_round_id <> stored.decided_round_id then
    decide_release_evaluated version status new_decision.decided_action new_decision.decided_guard_permitted
  else
    match differing_decision_fields new_decision stored with
    | [] ->
        allow
          (Printf.sprintf "%s already has the same release decision for round %s, so nothing changes" version
             stored.decided_round_id)
          []
    | fields ->
        refuse version (DecisionDiffers { differing_round_id = stored.decided_round_id; differing_fields = fields })

let decide_model_lifecycle request =
  let version = request.lifecycle_version in
  let added status = { changed_version = version; from_status = None; to_status = status } in
  match request.lifecycle_event with
  | Registered { requested_status = RequestCandidate; released_version = _ } ->
      allow (Printf.sprintf "%s is registered as a candidate" version) [ added ModelCandidate ]
  | Registered { requested_status = RequestReleased; released_version = Some released } ->
      refuse version (AlreadyReleased released)
  | Registered { requested_status = RequestReleased; released_version = None } ->
      allow
        (Printf.sprintf "%s is registered as released, because no version is released" version)
        [ added ModelReleased ]
  | Aggregated { parent_version; parent_status } -> (
      if parent_version = version then invalid_arg (version ^ " cannot be its own parent");
      match parent_status with
      | ModelCandidate ->
          allow
            (Printf.sprintf "%s is a candidate; %s, the candidate it was trained from, is superseded" version
               parent_version)
            [
              added ModelCandidate;
              { changed_version = parent_version; from_status = Some ModelCandidate; to_status = ModelSuperseded };
            ]
      | ModelReleased | ModelRejected | ModelSuperseded ->
          allow
            (Printf.sprintf "%s is a candidate; %s stays %s" version parent_version
               (string_of_model_status parent_status))
            [ added ModelCandidate ])
  | ReleaseEvaluated { status; action; guard_permitted } ->
      decide_release_evaluated version status action guard_permitted
  | ReleaseEvaluatedWithStoredDecision { status; new_decision; stored_decision } ->
      decide_release_evaluated_with_stored version status new_decision stored_decision
  | ApprovalRequested { status; decision; released_version } ->
      decide_approval version status decision released_version

(** Format a number of seconds for a message: whole numbers without a
    fraction, others with the fewest digits that read back as the same
    number. *)
let seconds_text value =
  if Float.is_integer value && Float.abs value < 1e15 then Printf.sprintf "%.0f" value
  else
    let exact precision =
      let text = Printf.sprintf "%.*g" precision value in
      if float_of_string text = value then Some text else None
    in
    match exact 12 with
    | Some text -> text
    | None -> ( match exact 15 with Some text -> text | None -> Printf.sprintf "%.17g" value)

(** A kept round and the sentence that explains it. *)
let keep message = { recovery_verdict = KeepRound; recovery_message = message }

(** A closed round, its reason and the sentence that explains it. *)
let close reason message = { recovery_verdict = CloseRound reason; recovery_message = message }

(** The end of each sentence that closes a round the operator can run
    again. *)
let run_again = "so it is closed; run it again under a new round id."

(** Decide a readable collecting training round: rows 5 to 8 of the table. *)
let decide_collecting_training request =
  match (request.candidate_registered, request.updates_kept) with
  | true, _ ->
      keep
        ("The round's candidate is already registered, "
        ^ "so it stays open and a retried aggregate returns that candidate.")
  | false, false -> close UpdatesNotKept ("The round's training updates were not kept across the restart, " ^ run_again)
  | false, true -> (
      let kept = keep "The round's training updates are kept, so it stays open." in
      match (request.oldest_update_age_seconds, request.update_max_age_seconds) with
      | Some age, Some limit ->
          if age > limit then
            close UpdatesExpired
              (Printf.sprintf
                 "The round's oldest stored update is %s seconds old, more than the limit of %s seconds, %s"
                 (seconds_text age) (seconds_text limit) run_again)
          else kept
      | None, _ | _, None -> kept)

let decide_round_recovery request =
  let status = string_of_round_status request.recovery_status in
  match (request.recovery_status, request.record_readable) with
  | (RoundAggregated | RoundClosed), _ -> keep (Printf.sprintf "The round is already %s, so it stays as it is." status)
  | (RoundPlanned | RoundDispatched | RoundCollecting), false ->
      close UnreadableRecord
        (Printf.sprintf "The round is %s, but its stored record cannot be read, so it is closed." status)
  | (RoundPlanned | RoundDispatched), true ->
      close DispatchInterrupted
        (Printf.sprintf "The round is %s, and a dispatch cannot resume after a restart, %s" status run_again)
  | RoundCollecting, true -> (
      match request.recovery_phase with
      | AnalyticsRound | EvaluationRound ->
          keep
            (Printf.sprintf "The %s round already holds its results, so it stays open and can be aggregated."
               (string_of_round_phase request.recovery_phase))
      | TrainingRound -> decide_collecting_training request)
