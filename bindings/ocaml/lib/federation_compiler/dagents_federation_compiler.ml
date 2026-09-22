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

(** Decide whether one returned contribution may enter the aggregate. *)
let contribution_failure manifest result =
  let expected_digest = round_digest manifest in
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

let evaluate_aggregation_readiness manifest results =
  let sorted = List.sort (fun left right -> compare left.result_site_id right.result_site_id) results in
  let accepted, rejected =
    List.fold_left
      (fun (accepted, rejected) result ->
        match contribution_failure manifest result with
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
  let dropouts =
    List.length (List.filter (fun result -> result.participation = SiteDropped) results)
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

(** Evaluate one release gate against candidate and baseline metrics. *)
let evaluate_gate gate candidate_metrics baseline_metrics =
  let observed = List.assoc_opt gate.gate_metric candidate_metrics in
  match (gate.comparison, observed) with
  | _, None ->
      {
        result_gate_id = gate.gate_id;
        outcome = GateNotEvaluated;
        observed = None;
        gate_detail = Printf.sprintf "candidate reported no metric named %s" gate.gate_metric;
      }
  | AtLeast threshold, Some value ->
      {
        result_gate_id = gate.gate_id;
        outcome = (if value >= threshold then GatePassed else GateFailed);
        observed = Some value;
        gate_detail = Printf.sprintf "%s=%g required >= %g" gate.gate_metric value threshold;
      }
  | AtMost threshold, Some value ->
      {
        result_gate_id = gate.gate_id;
        outcome = (if value <= threshold then GatePassed else GateFailed);
        observed = Some value;
        gate_detail = Printf.sprintf "%s=%g required <= %g" gate.gate_metric value threshold;
      }
  | ImprovesOnBaseline margin, Some value -> (
      match List.assoc_opt gate.gate_metric baseline_metrics with
      | None ->
          {
            result_gate_id = gate.gate_id;
            outcome = GateNotEvaluated;
            observed = Some value;
            gate_detail = Printf.sprintf "baseline reported no metric named %s" gate.gate_metric;
          }
      | Some baseline ->
          {
            result_gate_id = gate.gate_id;
            outcome = (if value >= baseline +. margin then GatePassed else GateFailed);
            observed = Some value;
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
