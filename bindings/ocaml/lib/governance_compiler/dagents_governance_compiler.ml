(** Ethical-Restriction Rails implementation.

    Every function here is total and side-effect free. The module deliberately
    contains no I/O, no clock, and no randomness, so a restriction plan is
    reproducible from its request alone and can be replayed during an audit. *)

open Dagents_common_ir

(** Clamp a float into the unit interval. *)
let clamp_unit value = if value < 0.0 then 0.0 else if value > 1.0 then 1.0 else value

(** Weight one Know-Your-User attribute.

    Unverified attributes are not discarded, because a stated affiliation is
    still weak evidence, but they are discounted heavily against verified ones. *)
let attribute_contribution attribute =
  if attribute.attribute_verified then attribute.attribute_weight else attribute.attribute_weight *. 0.25

let assess_requester requester =
  let total_weight =
    List.fold_left (fun total attribute -> total +. attribute.attribute_weight) 0.0 requester.attributes
  in
  let earned_weight =
    List.fold_left (fun total attribute -> total +. attribute_contribution attribute) 0.0 requester.attributes
  in
  let attribute_component = if total_weight <= 0.0 then 0.0 else earned_weight /. total_weight in
  let history = clamp_unit requester.compliance_history in
  (* An identity with no attributes at all scores zero regardless of history:
     a clean record is not evidence of identity. *)
  let score = if total_weight <= 0.0 then 0.0 else clamp_unit ((attribute_component *. 0.7) +. (history *. 0.3)) in
  let trust = if score < 0.4 then LowTrust else if score < 0.75 then ModerateTrust else HighTrust in
  let verified_count = List.length (List.filter (fun a -> a.attribute_verified) requester.attributes) in
  let rationale =
    [
      Printf.sprintf "requester_kind=%s" requester.requester_kind;
      Printf.sprintf "verified_attributes=%d/%d" verified_count (List.length requester.attributes);
      Printf.sprintf "compliance_history=%.2f" history;
      Printf.sprintf "kyu_score=%.2f" score;
    ]
    @ (match requester.affiliation with
      | Some affiliation -> [ "affiliation=" ^ affiliation ]
      | None -> [ "affiliation=unstated" ])
    @ (match requester.stated_purpose with
      | Some purpose -> [ "stated_purpose=" ^ purpose ]
      | None -> [ "stated_purpose=unstated" ])
  in
  { assessed_requester_id = requester.requester_id; kyu_score = score; trust; trust_rationale = rationale }

let sensitivity_of_field classification field =
  match List.assoc_opt field classification.field_sensitivity with
  | Some sensitivity -> sensitivity
  | None -> classification.default_sensitivity

(** Default rounding precision applied by [Generalize]. *)
let generalize_precision = 1

(** Noise scale applied when an update is permitted but not from a trusted peer. *)
let noise_scale = 0.5

(** Norm bound applied to a permitted federated contribution. *)
let clip_bound = 1.0

let select_strategy sensitivity trust granularity minimum_cohort =
  match (sensitivity, trust, granularity) with
  (* High sensitivity with a low-trust requester is refused outright, at every
     granularity. There is no amount of rounding that makes this acceptable. *)
  | HighSensitivity, LowTrust, (CellGrain | RowGrain | ColumnGrain | TableGrain | ModelUpdateGrain) -> Refuse
  | HighSensitivity, ModerateTrust, (CellGrain | RowGrain) -> Redact
  | HighSensitivity, ModerateTrust, (ColumnGrain | TableGrain) -> AggregateOnly minimum_cohort
  | HighSensitivity, ModerateTrust, ModelUpdateGrain -> AddNoise noise_scale
  | HighSensitivity, HighTrust, (CellGrain | RowGrain) -> Generalize generalize_precision
  | HighSensitivity, HighTrust, (ColumnGrain | TableGrain) -> AggregateOnly minimum_cohort
  | HighSensitivity, HighTrust, ModelUpdateGrain -> ClipContribution clip_bound
  | MediumSensitivity, LowTrust, (CellGrain | RowGrain) -> Redact
  | MediumSensitivity, LowTrust, ColumnGrain -> AggregateOnly minimum_cohort
  | MediumSensitivity, LowTrust, TableGrain -> Refuse
  | MediumSensitivity, LowTrust, ModelUpdateGrain -> Refuse
  | MediumSensitivity, ModerateTrust, (CellGrain | RowGrain) -> Generalize generalize_precision
  | MediumSensitivity, ModerateTrust, (ColumnGrain | TableGrain) -> AggregateOnly minimum_cohort
  | MediumSensitivity, ModerateTrust, ModelUpdateGrain -> ClipContribution clip_bound
  | MediumSensitivity, HighTrust, (CellGrain | RowGrain) -> AllowFull
  | MediumSensitivity, HighTrust, ColumnGrain -> Generalize generalize_precision
  | MediumSensitivity, HighTrust, TableGrain -> AggregateOnly minimum_cohort
  | MediumSensitivity, HighTrust, ModelUpdateGrain -> ClipContribution clip_bound
  | LowSensitivity, LowTrust, (CellGrain | RowGrain) -> AllowFull
  | LowSensitivity, LowTrust, ColumnGrain -> Generalize generalize_precision
  | LowSensitivity, LowTrust, TableGrain -> AggregateOnly minimum_cohort
  | LowSensitivity, LowTrust, ModelUpdateGrain -> AddNoise noise_scale
  | LowSensitivity, ModerateTrust, (CellGrain | RowGrain | ColumnGrain) -> AllowFull
  | LowSensitivity, ModerateTrust, TableGrain -> Generalize generalize_precision
  | LowSensitivity, ModerateTrust, ModelUpdateGrain -> ClipContribution clip_bound
  | LowSensitivity, HighTrust, (CellGrain | RowGrain | ColumnGrain | TableGrain) -> AllowFull
  (* The federated extension: an update is never released unprotected, however
     trusted the receiver and however low the field sensitivity, because the
     update is derived from records rather than being one. *)
  | LowSensitivity, HighTrust, ModelUpdateGrain -> ClipContribution clip_bound

let strategy_weight = function
  | AllowFull -> 0.0
  | Generalize _ -> 0.35
  (* Clipping bounds what an update can carry. Noise bounds it and obscures it,
     so noise is the stronger protection and must weigh more; ordering these the
     other way makes the score claim that trusting a requester more can protect
     the data more, which is never true. *)
  | ClipContribution _ -> 0.45
  | AddNoise _ -> 0.55
  | AggregateOnly _ -> 0.7
  | Redact -> 0.9
  | Refuse -> 1.0

(** Explain in one line why a strategy was chosen, for the audit record. *)
let restriction_reason sensitivity trust granularity strategy =
  Printf.sprintf "sensitivity=%s trust=%s granularity=%s -> %s"
    (string_of_sensitivity sensitivity) (string_of_trust_level trust)
    (string_of_granularity granularity)
    (string_of_restriction_strategy strategy)

(** Granularities whose results describe a group rather than one subject.

    These are the requests a minimum-cohort rule applies to; a single cell or
    row is already about one subject, so a cohort floor does not protect it. *)
let cohort_sensitive_granularity = function
  | ColumnGrain | TableGrain | ModelUpdateGrain -> true
  | CellGrain | RowGrain -> false

(** Check the request-level gates that no per-field lookup can see. *)
let request_violations request =
  let purpose_violations =
    match (request.approved_purposes, request.declared_purpose) with
    | [], _ -> []
    | _ :: _, None -> [ "declared_purpose is required when the classification lists approved purposes" ]
    | approved, Some declared ->
        if List.mem declared approved then []
        else [ Printf.sprintf "declared_purpose %s is not an approved purpose" declared ]
  in
  let cohort_violations =
    let minimum = request.classification.minimum_cohort in
    if (not (cohort_sensitive_granularity request.granularity)) || minimum <= 0 then []
    else
      match request.cohort_size with
      | None ->
          [
            Printf.sprintf "cohort_size is required at %s granularity when minimum_cohort is %d"
              (string_of_granularity request.granularity) minimum;
          ]
      | Some size when size < minimum ->
          [ Printf.sprintf "cohort_size %d is below minimum_cohort %d" size minimum ]
      | Some _ -> []
  in
  purpose_violations @ cohort_violations

(** Collect the duties the Ethical Guard must discharge when it enforces a plan. *)
let plan_obligations request restrictions =
  let base =
    [
      Printf.sprintf "record the decision, reason, and request id %s in the audit log" request.request_id;
      Printf.sprintf "enforce at boundary %s" (string_of_guard_boundary request.boundary);
    ]
  in
  let regulations =
    List.map (fun regulation -> "apply controls required by " ^ regulation) request.classification.regulations
  in
  let cohort =
    if cohort_sensitive_granularity request.granularity && request.classification.minimum_cohort > 0 then
      [ Printf.sprintf "suppress results describing fewer than %d subjects" request.classification.minimum_cohort ]
    else []
  in
  let federated =
    if
      List.exists
        (fun restriction ->
          match restriction.strategy with ClipContribution _ | AddNoise _ -> true | _ -> false)
        restrictions
    then [ "record privacy accounting for the bounded contribution before it leaves the boundary" ]
    else []
  in
  base @ regulations @ cohort @ federated

let plan_restrictions request =
  let assessment = assess_requester request.requester in
  let violations = request_violations request in
  let denied = violations <> [] in
  let minimum_cohort = request.classification.minimum_cohort in
  let restrictions =
    List.map
      (fun field ->
        let sensitivity = sensitivity_of_field request.classification field in
        let strategy =
          if denied then Refuse else select_strategy sensitivity assessment.trust request.granularity minimum_cohort
        in
        let reason =
          if denied then "request-level gate failed; field withheld"
          else restriction_reason sensitivity assessment.trust request.granularity strategy
        in
        {
          restricted_field = field;
          restricted_sensitivity = sensitivity;
          strategy;
          restriction_reason = reason;
        })
      request.requested_fields
  in
  let decision =
    if denied || List.exists (fun restriction -> restriction.strategy = Refuse) restrictions then DenyRequest
    else if List.for_all (fun restriction -> restriction.strategy = AllowFull) restrictions then PermitRequest
    else NarrowRequest
  in
  let filtering_score =
    match restrictions with
    | [] -> if denied then 1.0 else 0.0
    | _ ->
        let total =
          List.fold_left (fun total restriction -> total +. strategy_weight restriction.strategy) 0.0 restrictions
        in
        clamp_unit (total /. float_of_int (List.length restrictions))
  in
  {
    plan_request_id = request.request_id;
    plan_boundary = request.boundary;
    assessment;
    plan_granularity = request.granularity;
    field_restrictions = restrictions;
    decision;
    filtering_score;
    obligations = plan_obligations request restrictions;
    plan_violations = violations;
  }
