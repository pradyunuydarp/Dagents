(** Ethical-Restriction Rails: the deciding half of GRAILS.

    This module answers one question and never performs the answer: given who
    is asking, what the data is, and how much of it is being requested, what
    protection does this request need? It reads no records, opens no
    connections, and writes no audit entries. Enforcement is the Ethical
    Guard's job, and the Guard lives in the LMA and GMA request paths.

    Keeping the decision here is the point. {!select_strategy} matches
    exhaustively on the full sensitivity/trust/granularity triple, so adding a
    sensitivity level, a trust level, or a granularity fails to compile until
    every combination has been considered. A policy engine keyed on strings
    would silently fall through to a default instead.

    Example test case:
    {[
      let strategy = select_strategy HighSensitivity LowTrust TableGrain 5 in
      assert (strategy = Refuse)
    ]} *)

open Dagents_common_ir

(** Score a requester's Know-Your-User attributes into a trust level.

    Verified attributes contribute their full weight; unverified attributes
    contribute a quarter of it, because a claimed affiliation is not a checked
    one. The attribute component is blended with compliance history so a
    well-credentialed requester with a poor record does not score as highly as
    the credentials alone suggest.

    Inputs:
    - [requester]: identity, affiliation, purpose, attributes, and history.

    Output:
    - [kyu_assessment]: score in [0.0, 1.0], the derived trust level, and the
      rationale lines that produced it.

    Example test case:
    {[
      let assessment =
        assess_requester
          {
            requester_id = "unknown";
            requester_kind = "human";
            affiliation = None;
            stated_purpose = None;
            attributes = [];
            compliance_history = 0.0;
          }
      in
      assert (assessment.trust = LowTrust)
    ]} *)
val assess_requester : requester -> kyu_assessment

(** Look up one field's sensitivity, falling back to the classification default.

    Inputs:
    - [classification]: the data-side knowledge base entry.
    - [field]: the requested field name.

    Output: the field's declared sensitivity, or the default when undeclared. *)
val sensitivity_of_field : data_classification -> string -> sensitivity

(** Select the protection strategy for one sensitivity/trust/granularity triple.

    This is the lookup GRAILS describes, expressed as an exhaustive match
    rather than a conditional chain. One invariant is enforced across every
    row: [ModelUpdateGrain] never resolves to [AllowFull]. A model update is
    not a row, but it still carries signal derived from patient records, so it
    is always clipped, noised, or refused.

    Inputs:
    - [sensitivity]: how protected the data is.
    - [trust]: how far the requester is trusted.
    - [granularity]: how much is being asked for.
    - [minimum_cohort]: the smallest group size an aggregate may describe.

    Output: the [restriction_strategy] to apply. *)
val select_strategy : sensitivity -> trust_level -> granularity -> int -> restriction_strategy

(** Report how much protection a strategy applies, in [0.0, 1.0].

    [AllowFull] is 0.0 (nothing withheld) and [Refuse] is 1.0 (everything
    withheld). These weights are what make GRAILS' Filtering Score measurable
    instead of asserted. *)
val strategy_weight : restriction_strategy -> float

(** Compile a full restriction plan for one request.

    Beyond the per-field strategy lookup, this applies the request-level gates
    that a per-field decision cannot see: an undeclared or unapproved purpose,
    and a cohort too small for the requested granularity. Either forces the
    whole plan to [DenyRequest], because a single permitted field would
    otherwise leak past a failed request-level check.

    Inputs:
    - [request]: requester, classification, fields, granularity, and cohort.

    Output:
    - [restriction_plan]: per-field strategies, the overall decision, the
      filtering score, the obligations the Guard must discharge, and any
      request-level violations found.

    Example test case:
    {[
      let plan =
        plan_restrictions
          {
            request_id = "r-1";
            boundary = BeforeSend;
            requester =
              {
                requester_id = "coordinator";
                requester_kind = "coordinator";
                affiliation = Some "consortium";
                stated_purpose = Some "stroke_triage_research";
                attributes =
                  [ { attribute_id = "verified_identity"; attribute_weight = 1.0; attribute_verified = true } ];
                compliance_history = 1.0;
              };
            classification =
              {
                classification_id = "stroke-v1";
                field_sensitivity = [ ("update", HighSensitivity) ];
                default_sensitivity = HighSensitivity;
                regulations = [ "HIPAA" ];
                minimum_cohort = 20;
              };
            requested_fields = [ "update" ];
            granularity = ModelUpdateGrain;
            cohort_size = Some 120;
            declared_purpose = Some "stroke_triage_research";
            approved_purposes = [ "stroke_triage_research" ];
          }
      in
      assert (plan.decision <> DenyRequest);
      assert (List.for_all (fun r -> r.strategy <> AllowFull) plan.field_restrictions)
    ]} *)
val plan_restrictions : restriction_request -> restriction_plan
