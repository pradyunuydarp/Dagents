(** Regression tests for the Dagents OCaml functional kernels.

    These tests exercise the pure compiler modules end-to-end:
    - dataset profiling, source validation, extraction planning, quality checks,
      schema validation, and transforms;
    - pipeline DAG validation and compilation;
    - model routing;
    - manifest rendering;
    - JSON codec round-trips.

    The tests intentionally avoid service or network dependencies so failures
    point to deterministic compiler behavior. *)

open Dagents_common_ir

(** Assert helper that keeps failure messages domain-specific. *)
let assert_true message condition =
  if not condition then failwith message

(** Dependency-free substring helper used to inspect rendered YAML. *)
let contains haystack needle =
  let haystack_len = String.length haystack in
  let needle_len = String.length needle in
  let rec loop index =
    if index + needle_len > haystack_len then false
    else if String.sub haystack index needle_len = needle then true
    else loop (index + 1)
  in
  if needle_len = 0 then true else loop 0

(** Assert that a thunk fails with [Invalid_argument]. *)
let expect_invalid_argument message thunk =
  try
    let _ = thunk () in
    failwith message
  with
  | Invalid_argument _ -> ()

(** Verifies that profiling counts records, classifies numeric fields, and
    excludes the supervised label from inferred features. *)
let test_dataset_profile () =
  let profile =
    Dagents_dataset_compiler.build_profile
      ~scope_id:"source-a"
      ~scope_kind:Source
      ~extraction_strategy:Tabular
      ~label_field:"label"
      [ [ ("value", VFloat 1.2); ("score", VFloat 0.1); ("label", VInt 0) ];
        [ ("value", VFloat 2.4); ("score", VFloat 0.2); ("label", VInt 1) ] ]
  in
  assert_true "dataset profile should count records" (profile.record_count = 2);
  assert_true "dataset profile should infer numeric fields"
    (profile.numeric_fields = [ "value"; "score" ]);
  assert_true "dataset profile should exclude label from feature fields"
    (profile.feature_fields = [ "value"; "score" ])

(** Verifies that a valid Postgres source compiles into selected fields and a
    hash partition plan. *)
let test_dataset_source_and_extraction_plan () =
  let source =
    {
      source_id = "orders";
      source_kind = Postgres;
      connection_ref = Some { connection_id = "warehouse"; connection_options = [] };
      selection =
        PostgresSelection
          {
            sql = None;
            table = Some "public.orders";
            columns = [ "tenant_id"; "amount"; "status" ];
            where_clause = Some "amount > 0";
            order_by = [ "tenant_id" ];
          };
      format = "rows";
      schema_hint = [];
      batching = { batch_size = 500; max_records = Some 2500 };
      checkpoint = None;
      options = [ ("partitionField", "tenant_id") ];
    }
  in
  let validation = Dagents_dataset_compiler.validate_source source in
  assert_true "postgres source should validate" validation.valid;
  let plan = Dagents_dataset_compiler.compile_extraction_plan ~partition_count:4 source in
  assert_true "extraction plan should keep selected columns"
    (plan.selected_fields = [ "tenant_id"; "amount"; "status" ]);
  assert_true "extraction plan should compile hash partition"
    (plan.partition_strategy = HashPartition ("tenant_id", 4))

(** Verifies source validation errors for missing connections and incomplete
    Postgres selections. *)
let test_dataset_source_negative_cases () =
  let missing_connection =
    {
      source_id = "orders";
      source_kind = Postgres;
      connection_ref = None;
      selection =
        PostgresSelection { sql = None; table = Some "orders"; columns = [ "id" ]; where_clause = None; order_by = [] };
      format = "rows";
      schema_hint = [];
      batching = { batch_size = 100; max_records = Some 1000 };
      checkpoint = None;
      options = [];
    }
  in
  let validation = Dagents_dataset_compiler.validate_source missing_connection in
  assert_true "external source should require connection ref" (not validation.valid);
  assert_true "external source should report connection error"
    (List.mem "connection_ref is required for external sources" validation.errors);
  expect_invalid_argument "invalid source should not compile extraction plan" (fun () ->
      Dagents_dataset_compiler.compile_extraction_plan missing_connection);
  let bad_postgres =
    { missing_connection with connection_ref = Some { connection_id = "warehouse"; connection_options = [] };
      selection = PostgresSelection { sql = None; table = None; columns = []; where_clause = None; order_by = [] } }
  in
  let validation = Dagents_dataset_compiler.validate_source bad_postgres in
  assert_true "postgres source should require sql or table"
    (List.mem "postgres selection requires sql or table" validation.errors)

(** Verifies partition-strategy precedence: time-window options override hash
    partition options, while explicit partition counts drive hash planning. *)
let test_time_window_partition_planning () =
  let source =
    {
      source_id = "events";
      source_kind = ObjectStorage;
      connection_ref = Some { connection_id = "lake"; connection_options = [] };
      selection = ObjectStorageSelection { uri = Some "s3://demo/events"; prefix = None; glob = Some "*.json"; compression = None };
      format = "json";
      schema_hint = [ ("event_time", "string"); ("tenant_id", "string") ];
      batching = { batch_size = 1000; max_records = Some 10000 };
      checkpoint = None;
      options = [ ("timeField", "event_time"); ("timeWindow", "1h"); ("partitionField", "tenant_id"); ("partitionCount", "8") ];
    }
  in
  let plan = Dagents_dataset_compiler.compile_extraction_plan source in
  assert_true "time window partition should win over hash partition"
    (plan.partition_strategy = TimeWindow ("event_time", "1h"));
  let hash_source = { source with options = [ ("partitionField", "tenant_id"); ("partitionCount", "8") ] } in
  let plan = Dagents_dataset_compiler.compile_extraction_plan hash_source in
  assert_true "partitionCount option should drive hash partition count"
    (plan.partition_strategy = HashPartition ("tenant_id", 8))

(** Verifies schema contracts, quality checks, quality-report aggregation, and
    transform compilation/application over a representative record batch. *)
let test_schema_quality_and_transform_apis () =
  let records =
    [
      [ ("id", VString "a"); ("amount", VString "1.5"); ("status", VString "paid") ];
      [ ("id", VString "b"); ("amount", VString "3.0"); ("status", VString "paid") ];
      [ ("id", VString "b"); ("amount", VString "-1.0"); ("status", VNull) ];
    ]
  in
  let contract =
    {
      required_fields = [ { field_name = "id"; dtype = "string" }; { field_name = "amount"; dtype = "string" } ];
      optional_fields = [ { field_name = "status"; dtype = "string" } ];
      allow_extra_fields = false;
    }
  in
  let report =
    Dagents_dataset_compiler.validate_schema_contract contract
      (Dagents_dataset_compiler.infer_schema records)
  in
  assert_true "schema contract should pass declared fields" report.schema_valid;
  let quality_results =
    Dagents_dataset_compiler.evaluate_quality_rules records
      [
        { rule_id = "id_unique"; field = "id"; operator = Unique; severity = Error };
        { rule_id = "status_present"; field = "status"; operator = NonNull; severity = Error };
        { rule_id = "amount_non_negative"; field = "amount"; operator = MinValue 0.0; severity = Error };
      ]
  in
  assert_true "quality rule should detect duplicate id"
    ((List.nth quality_results 0).violations = 1);
  assert_true "quality rule should detect null status"
    ((List.nth quality_results 1).violations = 1);
  assert_true "quality rule should detect negative amount"
    ((List.nth quality_results 2).violations = 1);
  let quality_report =
    Dagents_dataset_compiler.evaluate_quality_report records
      [
        { rule_id = "status_present"; field = "status"; operator = NonNull; severity = Warning };
        { rule_id = "amount_non_negative"; field = "amount"; operator = MinValue 0.0; severity = Error };
      ]
  in
  assert_true "quality report should block failed error rules" quality_report.blocking;
  assert_true "quality report should count warning failures" (quality_report.warning_count = 1);
  assert_true "quality report should count error failures" (quality_report.error_count = 1);
  assert_true "quality report should sum violations" (quality_report.total_violations = 2);
  let plan =
    Dagents_dataset_compiler.compile_transform_plan ~plan_id:"normalize-orders"
      [ CastFields [ ("amount", "float") ]; DropFields [ "status" ]; RenameFields [ ("amount", "amount_usd") ] ]
      records
  in
  assert_true "transform plan should expose normalized schema"
    (plan.output_schema
    = [ { field_name = "amount_usd"; dtype = "float" }; { field_name = "id"; dtype = "string" } ]);
  let transformed = Dagents_dataset_compiler.apply_transform_plan plan records in
  let first = List.hd transformed in
  assert_true "transform plan should cast and rename values"
    (List.assoc "amount_usd" first = VFloat 1.5);
  assert_true "transform plan should drop fields"
    (Option.is_none (List.assoc_opt "status" first))

(** Verifies schema validation diagnostics for missing, mismatched, and extra
    fields. *)
let test_schema_validation_negative_cases () =
  let records = [ [ ("id", VString "a"); ("amount", VString "1.5"); ("extra", VBool true) ] ] in
  let contract =
    {
      required_fields = [ { field_name = "id"; dtype = "string" }; { field_name = "amount"; dtype = "float" }; { field_name = "status"; dtype = "string" } ];
      optional_fields = [];
      allow_extra_fields = false;
    }
  in
  let report =
    Dagents_dataset_compiler.validate_schema_contract contract
      (Dagents_dataset_compiler.infer_schema records)
  in
  assert_true "schema report should fail invalid contract" (not report.schema_valid);
  assert_true "schema report should list missing required field"
    (List.exists (fun field -> field.field_name = "status") report.missing_fields);
  assert_true "schema report should list type mismatch"
    (List.exists (fun issue -> issue.issue_field = "amount") report.type_mismatches);
  assert_true "schema report should list extra field"
    (List.exists (fun field -> field.field_name = "extra") report.extra_fields)

(** Verifies that the pipeline compiler topologically sorts steps and assigns
    runtime targets based on step kind. *)
let test_pipeline_compiler_orders_and_lowers () =
  let compiled =
    Dagents_pipeline_compiler.compile
      {
        pipeline_id = "pipeline";
        steps =
          [
            {
              step_id = "summarize";
              kind = SummarizeItems;
              depends_on = [ "filter" ];
              config_json = None;
            };
            {
              step_id = "filter";
              kind = FilterItems;
              depends_on = [ "profile" ];
              config_json = None;
            };
            {
              step_id = "profile";
              kind = ProfileDataset;
              depends_on = [ "model" ];
              config_json = None;
            };
            {
              step_id = "model";
              kind = RunModelJob;
              depends_on = [];
              config_json = None;
            };
          ];
      }
  in
  let step_ids = List.map (fun step -> step.step_id) compiled.steps in
  assert_true "pipeline compiler should topologically sort steps"
    (step_ids = [ "model"; "profile"; "filter"; "summarize" ]);
  let model_step = List.find (fun step -> step.step_id = "model") compiled.steps in
  let profile_step = List.find (fun step -> step.step_id = "profile") compiled.steps in
  assert_true "run_model_job should target python service"
    (model_step.execution_target = PythonService);
  assert_true "profile_dataset should target local process"
    (profile_step.execution_target = LocalProcess)

(** Verifies cycle detection in pipeline dependency graphs. *)
let test_pipeline_compiler_rejects_cycles () =
  expect_invalid_argument "pipeline compiler should reject cycles" (fun () ->
      Dagents_pipeline_compiler.validate
        {
          pipeline_id = "cyclic";
          steps =
            [
              { step_id = "a"; kind = EnrichContext; depends_on = [ "b" ]; config_json = None };
              { step_id = "b"; kind = FilterItems; depends_on = [ "a" ]; config_json = None };
            ];
        } )

(** Verifies duplicate step-id rejection and unknown dependency rejection. *)
let test_pipeline_compiler_rejects_duplicates_and_unknown_dependencies () =
  expect_invalid_argument "pipeline compiler should reject duplicate step ids" (fun () ->
      Dagents_pipeline_compiler.validate
        {
          pipeline_id = "duplicate";
          steps =
            [
              { step_id = "a"; kind = EnrichContext; depends_on = []; config_json = None };
              { step_id = "a"; kind = FilterItems; depends_on = []; config_json = None };
            ];
        } );
  expect_invalid_argument "pipeline compiler should reject unknown dependency ids" (fun () ->
      Dagents_pipeline_compiler.validate
        {
          pipeline_id = "unknown-dep";
          steps =
            [ { step_id = "a"; kind = FilterItems; depends_on = [ "missing" ]; config_json = None } ];
        } )

(** Verifies task-aware model routing and packaging selection. *)
let test_model_router () =
  let profile =
    Dagents_dataset_compiler.build_profile
      ~scope_id:"scope"
      ~scope_kind:Source
      ~extraction_strategy:TimeSeries
      [ [ ("timestamp", VString "2026-01-01T00:00:00Z"); ("errors", VInt 2) ] ]
  in
  let plan = Dagents_model_router.route profile Forecasting in
  assert_true "model router should prefer gru for forecasting" (plan.selected_model = Gru);
  assert_true "forecasting should use long running deployment"
    (plan.packaging_mode = LongRunningDeployment)

(** Verifies manifest rendering for Deployment, CronJob, Service, ConfigMap,
    environment variables, command args, and plan metadata. *)
let test_manifest_compiler_plan () =
  let plan =
    Dagents_manifest_compiler.compile_plan
      {
        plan_id = "plan-1";
        namespace = "dagents";
        include_services = true;
        include_config_maps = true;
        components =
          [
            {
              name = "core";
              image = "ghcr.io/example/core:latest";
              kind = Deployment;
              replicas = 2;
              schedule = None;
              env = [ { name = "APP_ENV"; value = "cloud" } ];
              ports = [ { port_name = "http"; container_port = 8060 } ];
              args = [ "--server.port=8060" ];
              resources = default_resources;
            };
            {
              name = "reconciler";
              image = "ghcr.io/example/reconciler:latest";
              kind = CronJob;
              replicas = 1;
              schedule = Some "*/15 * * * *";
              env = [ { name = "MODE"; value = "reconcile" } ];
              ports = [];
              args = [ "--sync" ];
              resources = default_resources;
            };
            {
              name = "edge-service";
              image = "ghcr.io/example/edge:latest";
              kind = Service;
              replicas = 1;
              schedule = None;
              env = [];
              ports = [ { port_name = "grpc"; container_port = 9090 } ];
              args = [];
              resources = default_resources;
            };
          ];
      }
  in
  assert_true "manifest compiler should preserve plan id" (plan.plan_id = "plan-1");
  assert_true "manifest compiler should render deployment" (contains plan.combined_yaml "kind: Deployment");
  assert_true "manifest compiler should render cronjob" (contains plan.combined_yaml "kind: CronJob");
  assert_true "manifest compiler should render service" (contains plan.combined_yaml "kind: Service");
  assert_true "manifest compiler should render config map" (contains plan.combined_yaml "kind: ConfigMap");
  assert_true "cronjob should include schedule" (contains plan.combined_yaml "*/15 * * * *");
  assert_true "rendered workload should include env vars" (contains plan.combined_yaml "APP_ENV");
  assert_true "rendered workload should include args" (contains plan.combined_yaml "--sync")

(** Verifies JSON decoding into a workload spec and JSON encoding of the
    compiled workload plan. *)
let test_json_codec_roundtrip () =
  let spec_json =
    `Assoc
      [
        ("planId", `String "json-plan");
        ("namespace", `String "dagents");
        ("includeServices", `Bool true);
        ("includeConfigMaps", `Bool true);
        ( "components",
          `List
            [
              `Assoc
                [
                  ("name", `String "compiler");
                  ("image", `String "ghcr.io/example/compiler:latest");
                  ("kind", `String "CronJob");
                  ("schedule", `String "0 * * * *");
                  ("replicas", `Int 1);
                  ("args", `List [ `String "--compile" ]);
                  ("env", `List [ `Assoc [ ("name", `String "APP_ENV"); ("value", `String "test") ] ]);
                  ( "ports",
                    `List [ `Assoc [ ("name", `String "http"); ("containerPort", `Int 8080) ] ] );
                  ( "resources",
                    `Assoc
                      [
                        ("cpuRequest", `String "100m");
                        ("cpuLimit", `String "500m");
                        ("memoryRequest", `String "128Mi");
                        ("memoryLimit", `String "512Mi");
                      ] );
                ];
            ] );
      ]
  in
  let spec = Json_codec.workload_spec_of_yojson spec_json in
  let plan = Dagents_manifest_compiler.compile_plan spec in
  let encoded = Json_codec.yojson_of_workload_plan plan in
  match encoded with
  | `Assoc fields ->
      assert_true "json codec should include plan id"
        (List.assoc "planId" fields = `String "json-plan");
      assert_true "json codec should include manifests"
        (match List.assoc "manifests" fields with `List manifests -> List.length manifests = 1 | _ -> false)
  | _ -> failwith "expected workload plan object"

(** Render a compact textual progress bar for Dune test output.

    Dune already has build-level progress with [--display=progress]. This
    helper adds test-case-level progress, which is more useful for a live demo
    because each functional module check is named as it runs. *)
let progress_bar ~completed ~total =
  let width = 24 in
  let filled = if total = 0 then 0 else (completed * width) / total in
  "[" ^ String.make filled '#'
  ^ String.make (width - filled) '-'
  ^ "]"

(** Build a requester whose attributes are all verified, for trust-level tests. *)
let trusted_requester requester_id =
  {
    requester_id;
    requester_kind = "coordinator";
    affiliation = Some "stroke-consortium";
    stated_purpose = Some "stroke_triage_research";
    attributes =
      [
        { attribute_id = "verified_identity"; attribute_weight = 1.0; attribute_verified = true };
        { attribute_id = "signed_dua"; attribute_weight = 1.0; attribute_verified = true };
      ];
    compliance_history = 0.9;
  }

(** A classification treating every field as highly sensitive under HIPAA. *)
let strict_classification =
  {
    classification_id = "stroke-triage-v1";
    field_sensitivity = [ ("age_band", MediumSensitivity); ("nihss_total", HighSensitivity) ];
    default_sensitivity = HighSensitivity;
    regulations = [ "HIPAA" ];
    minimum_cohort = 20;
  }

(** Verifies that Know-Your-User scoring separates a verified, well-behaved
    requester from an anonymous one, and that unverified attributes are
    discounted rather than counted in full. *)
let test_kyu_assessment () =
  let trusted = Dagents_governance_compiler.assess_requester (trusted_requester "gma") in
  assert_true "verified requester should reach high trust" (trusted.trust = HighTrust);
  let anonymous =
    Dagents_governance_compiler.assess_requester
      {
        requester_id = "anonymous";
        requester_kind = "human";
        affiliation = None;
        stated_purpose = None;
        attributes = [];
        compliance_history = 1.0;
      }
  in
  assert_true "requester with no attributes should score zero" (anonymous.kyu_score = 0.0);
  assert_true "a clean history alone should not earn trust" (anonymous.trust = LowTrust);
  let unverified =
    Dagents_governance_compiler.assess_requester
      {
        requester_id = "claimed";
        requester_kind = "human";
        affiliation = Some "unchecked";
        stated_purpose = Some "research";
        attributes =
          [ { attribute_id = "claimed_identity"; attribute_weight = 1.0; attribute_verified = false } ];
        compliance_history = 0.5;
      }
  in
  assert_true "unverified attributes should be discounted"
    (unverified.kyu_score < trusted.kyu_score);
  assert_true "trust rationale should be reported for audit"
    (List.length trusted.trust_rationale > 0)

(** Verifies the core GRAILS invariant this project extends: a model update is
    never released unprotected, at any sensitivity and any trust level, because
    an update is derived from patient records even though it is not one. *)
let test_model_update_is_never_allowed_raw () =
  let sensitivities = [ LowSensitivity; MediumSensitivity; HighSensitivity ] in
  let trusts = [ LowTrust; ModerateTrust; HighTrust ] in
  List.iter
    (fun sensitivity ->
      List.iter
        (fun trust ->
          let strategy = Dagents_governance_compiler.select_strategy sensitivity trust ModelUpdateGrain 20 in
          assert_true
            (Printf.sprintf "model update must never be allowed raw (sensitivity=%s trust=%s)"
               (string_of_sensitivity sensitivity) (string_of_trust_level trust))
            (strategy <> AllowFull))
        trusts)
    sensitivities

(** Verifies that the strategy lookup is monotone in trust: raising trust never
    makes the applied protection stricter for the same data and granularity. *)
let test_strategy_is_monotone_in_trust () =
  let granularities = [ CellGrain; RowGrain; ColumnGrain; TableGrain; ModelUpdateGrain ] in
  List.iter
    (fun sensitivity ->
      List.iter
        (fun granularity ->
          let weight trust =
            Dagents_governance_compiler.strategy_weight
              (Dagents_governance_compiler.select_strategy sensitivity trust granularity 20)
          in
          assert_true
            (Printf.sprintf "more trust must not mean more filtering (sensitivity=%s granularity=%s)"
               (string_of_sensitivity sensitivity) (string_of_granularity granularity))
            (weight LowTrust >= weight ModerateTrust && weight ModerateTrust >= weight HighTrust))
        granularities)
    [ LowSensitivity; MediumSensitivity; HighSensitivity ]

(** Verifies that a high-sensitivity request from an untrusted requester is
    refused outright, whatever granularity it asks for. *)
let test_high_sensitivity_low_trust_is_refused () =
  List.iter
    (fun granularity ->
      assert_true
        ("high sensitivity with low trust must be refused at " ^ string_of_granularity granularity)
        (Dagents_governance_compiler.select_strategy HighSensitivity LowTrust granularity 20 = Refuse))
    [ CellGrain; RowGrain; ColumnGrain; TableGrain; ModelUpdateGrain ]

(** Verifies that a well-formed federated egress request is narrowed rather than
    denied, is clipped rather than released raw, and carries the obligations the
    Ethical Guard has to discharge. *)
let test_restriction_plan_permits_bounded_egress () =
  let plan =
    Dagents_governance_compiler.plan_restrictions
      {
        request_id = "req-1";
        boundary = BeforeSend;
        requester = trusted_requester "gma";
        classification = strict_classification;
        requested_fields = [ "nihss_total" ];
        granularity = ModelUpdateGrain;
        cohort_size = Some 140;
        declared_purpose = Some "stroke_triage_research";
        approved_purposes = [ "stroke_triage_research" ];
      }
  in
  assert_true "bounded egress should narrow, not deny" (plan.decision = NarrowRequest);
  assert_true "no violations expected for a compliant request" (plan.plan_violations = []);
  assert_true "filtering score should be strictly between permit and refuse"
    (plan.filtering_score > 0.0 && plan.filtering_score < 1.0);
  assert_true "an audit obligation must always be attached"
    (List.exists (fun obligation -> contains obligation "audit log") plan.obligations);
  assert_true "privacy accounting is owed for a bounded contribution"
    (List.exists (fun obligation -> contains obligation "privacy accounting") plan.obligations)

(** Verifies the request-level gates that no per-field lookup can see: a cohort
    below the floor and an unapproved purpose each deny the whole request, and
    denial withholds every field rather than only the offending one. *)
let test_restriction_plan_request_level_gates () =
  let base_request =
    {
      request_id = "req-2";
      boundary = BeforeSend;
      requester = trusted_requester "gma";
      classification = strict_classification;
      requested_fields = [ "age_band"; "nihss_total" ];
      granularity = TableGrain;
      cohort_size = Some 140;
      declared_purpose = Some "stroke_triage_research";
      approved_purposes = [ "stroke_triage_research" ];
    }
  in
  let small_cohort =
    Dagents_governance_compiler.plan_restrictions { base_request with cohort_size = Some 4 }
  in
  assert_true "a cohort below the floor must deny the request" (small_cohort.decision = DenyRequest);
  assert_true "denial must withhold every field, not only the sensitive one"
    (List.for_all (fun restriction -> restriction.strategy = Refuse) small_cohort.field_restrictions);
  assert_true "a denied plan scores as fully filtered" (small_cohort.filtering_score = 1.0);
  let wrong_purpose =
    Dagents_governance_compiler.plan_restrictions
      { base_request with declared_purpose = Some "marketing_analytics" }
  in
  assert_true "an unapproved purpose must deny the request" (wrong_purpose.decision = DenyRequest);
  assert_true "the violation must name the purpose"
    (List.exists (fun violation -> contains violation "marketing_analytics") wrong_purpose.plan_violations);
  let missing_purpose =
    Dagents_governance_compiler.plan_restrictions { base_request with declared_purpose = None }
  in
  assert_true "an undeclared purpose must deny when purposes are restricted"
    (missing_purpose.decision = DenyRequest);
  (* A cell-level request is about one subject already, so the cohort floor
     does not apply to it and must not deny it. *)
  let cell_request =
    Dagents_governance_compiler.plan_restrictions
      { base_request with granularity = CellGrain; cohort_size = None }
  in
  assert_true "a cohort floor must not apply to single-cell requests"
    (cell_request.plan_violations = [])

(** A round manifest used across the federation tests. *)
let stroke_manifest =
  {
    round_id = "round-0042";
    study_id = "stroke-triage";
    condition_id = "suspected_stroke";
    phase = TrainingRound;
    model_version = "seed-v3";
    model_artifact_digest = "sha256:seed";
    training_code_digest = "sha256:code";
    feature_contract_version = "stroke-triage-features-v2";
    privacy_profile = "consortium-profile-v1";
    aggregation = FedAvg;
    minimum_participants = 2;
    minimum_cohort_per_site = 50;
    required_capabilities = [ "local_training" ];
    stop_conditions = [ SchemaFailure; QuorumNotMet ];
    invited_sites = [ "hospital-a"; "hospital-b"; "hospital-c" ];
  }

(** Build an eligible site registration, so tests can vary one field at a time. *)
let eligible_site site_id =
  {
    site_id;
    site_capabilities = [ "local_training" ];
    site_feature_contract_version = "stroke-triage-features-v2";
    approved_conditions = [ "suspected_stroke" ];
    site_policy_version = "hospital-policy-v7";
    site_cohort_size = 300;
    site_enrolled = true;
  }

(** Verifies that round planning selects only eligible sites, records a reason
    for every exclusion, and reports quorum against the effective threshold. *)
let test_round_plan_selects_eligible_sites () =
  let registrations =
    [
      eligible_site "hospital-a";
      eligible_site "hospital-b";
      { (eligible_site "hospital-c") with site_feature_contract_version = "stroke-triage-features-v1" };
    ]
  in
  let plan = Dagents_federation_compiler.compile_round_plan stroke_manifest registrations in
  assert_true "eligible sites should be selected" (plan.selected_sites = [ "hospital-a"; "hospital-b" ]);
  assert_true "quorum should be met with two of two required" plan.quorum_met;
  assert_true "no stop reason when quorum is met" (plan.plan_stop_reason = None);
  assert_true "a contract mismatch should be excluded with a reason"
    (List.exists
       (fun exclusion ->
         exclusion.excluded_site_id = "hospital-c" && contains exclusion.exclusion_reason "feature contract")
       plan.excluded_sites);
  assert_true "selected sites should be deterministically ordered"
    (plan.selected_sites = List.sort compare plan.selected_sites)

(** Verifies each individual eligibility rule, and that an invited site which
    never registered is still reported rather than silently ignored. *)
let test_round_plan_exclusion_reasons () =
  (* Sites that were invited but never registered are excluded too, so this
     looks up the reason for one named site rather than expecting a single
     exclusion overall. *)
  let reason_for registration =
    let plan = Dagents_federation_compiler.compile_round_plan stroke_manifest [ registration ] in
    match
      List.find_opt
        (fun exclusion -> exclusion.excluded_site_id = registration.site_id)
        plan.excluded_sites
    with
    | Some exclusion -> exclusion.exclusion_reason
    | None -> failwith ("expected an exclusion for " ^ registration.site_id)
  in
  assert_true "an unenrolled site should be excluded"
    (contains (reason_for { (eligible_site "hospital-a") with site_enrolled = false }) "not enrolled");
  assert_true "an uninvited site should be excluded"
    (contains (reason_for (eligible_site "hospital-z")) "not invited");
  assert_true "a site that has not approved the condition should be excluded"
    (contains (reason_for { (eligible_site "hospital-a") with approved_conditions = [] }) "approved condition");
  assert_true "a site missing a capability should be excluded"
    (contains (reason_for { (eligible_site "hospital-a") with site_capabilities = [] }) "capability");
  assert_true "a site below the cohort floor should be excluded"
    (contains (reason_for { (eligible_site "hospital-a") with site_cohort_size = 10 }) "cohort");
  let plan = Dagents_federation_compiler.compile_round_plan stroke_manifest [ eligible_site "hospital-a" ] in
  assert_true "invited sites that never registered should still be reported"
    (List.length (List.filter (fun e -> contains e.exclusion_reason "no registration") plan.excluded_sites) = 2);
  assert_true "one participant should not meet a quorum of two" (not plan.quorum_met);
  assert_true "a short round should report why it stopped"
    (plan.plan_stop_reason = Some QuorumNotMet)

(** Verifies that a secure-aggregation threshold raises the participation floor
    above the manifest's stated minimum, since that threshold is the reason the
    coordinator cannot read one site's update. *)
let test_secure_aggregation_raises_quorum () =
  let manifest = { stroke_manifest with aggregation = SecureAggregation 3 } in
  let plan =
    Dagents_federation_compiler.compile_round_plan manifest
      [ eligible_site "hospital-a"; eligible_site "hospital-b" ]
  in
  assert_true "the secure-aggregation threshold should raise the requirement"
    (plan.required_participants = 3);
  assert_true "two sites should not satisfy a threshold of three" (not plan.quorum_met)

(** Verifies that the round digest is deterministic, independent of list order,
    and changes when any governed field of the manifest changes. *)
let test_round_digest_is_deterministic () =
  let digest = Dagents_federation_compiler.round_digest stroke_manifest in
  assert_true "the digest should be stable across calls"
    (digest = Dagents_federation_compiler.round_digest stroke_manifest);
  assert_true "the digest should not depend on invited-site order"
    (digest
    = Dagents_federation_compiler.round_digest
        { stroke_manifest with invited_sites = [ "hospital-c"; "hospital-a"; "hospital-b" ] });
  assert_true "changing the training code must change the digest"
    (digest
    <> Dagents_federation_compiler.round_digest
         { stroke_manifest with training_code_digest = "sha256:tampered" });
  assert_true "changing the privacy profile must change the digest"
    (digest
    <> Dagents_federation_compiler.round_digest { stroke_manifest with privacy_profile = "weaker" })

(** Build a completed site result whose job digest matches the manifest. *)
let completed_result site_id examples =
  {
    result_round_id = stroke_manifest.round_id;
    result_site_id = site_id;
    result_job_digest = Dagents_federation_compiler.round_digest stroke_manifest;
    participation = SiteCompleted;
    code_verified = true;
    privacy_checks_passed = true;
    contributed_examples = examples;
    update_norm = Some 0.8;
    result_metrics = [ ("auc", 0.9) ];
    local_evidence_pointer = Some "hospital-local://evidence";
  }

(** Verifies that only verified, privacy-checked contributions are aggregated,
    that weights follow accepted example counts, and that the weights sum to one. *)
let test_aggregation_readiness_accepts_verified_contributions () =
  let readiness =
    Dagents_federation_compiler.evaluate_aggregation_readiness stroke_manifest
      [
        completed_result "hospital-a" 300;
        completed_result "hospital-b" 100;
        { (completed_result "hospital-c" 200) with code_verified = false };
      ]
  in
  assert_true "verified contributions should be accepted"
    (readiness.accepted_sites = [ "hospital-a"; "hospital-b" ]);
  assert_true "an unverified contribution should be rejected with a reason"
    (List.exists
       (fun rejection ->
         rejection.rejected_site_id = "hospital-c" && contains rejection.rejection_reason "training code")
       readiness.rejected_contributions);
  assert_true "aggregation should be permitted once quorum survives"
    readiness.aggregation_permitted;
  assert_true "accepted examples should sum only the accepted sites"
    (readiness.accepted_examples = 400);
  let total = List.fold_left (fun total (_, weight) -> total +. weight) 0.0 readiness.site_weights in
  assert_true "site weights should sum to one" (Float.abs (total -. 1.0) < 1e-9);
  assert_true "weights should follow accepted example counts"
    (List.assoc "hospital-a" readiness.site_weights > List.assoc "hospital-b" readiness.site_weights)

(** Verifies every reason a contribution must not enter the aggregate, and that
    losing enough contributions blocks aggregation instead of revealing a
    single site's update. *)
let test_aggregation_readiness_rejects_bad_contributions () =
  let reason_for result =
    let readiness = Dagents_federation_compiler.evaluate_aggregation_readiness stroke_manifest [ result ] in
    match
      List.find_opt
        (fun rejection -> rejection.rejected_site_id = result.result_site_id)
        readiness.rejected_contributions
    with
    | Some rejection -> rejection.rejection_reason
    | None -> failwith ("expected a rejection for " ^ result.result_site_id)
  in
  assert_true "a failed privacy check should be rejected"
    (contains (reason_for { (completed_result "hospital-a" 300) with privacy_checks_passed = false }) "privacy");
  assert_true "a mismatched job digest should be rejected"
    (contains (reason_for { (completed_result "hospital-a" 300) with result_job_digest = "fnv1a:forged" }) "digest");
  assert_true "a result from another round should be rejected"
    (contains (reason_for { (completed_result "hospital-a" 300) with result_round_id = "round-0001" }) "different round");
  assert_true "a site that dropped out should be rejected"
    (contains (reason_for { (completed_result "hospital-a" 300) with participation = SiteDropped }) "dropped");
  assert_true "a site that rejected the job should be rejected"
    (contains (reason_for { (completed_result "hospital-a" 300) with participation = SiteRejected }) "rejected the job");
  assert_true "a contribution below the cohort floor should be rejected"
    (contains (reason_for (completed_result "hospital-a" 10)) "below the per-site minimum");
  let blocked =
    Dagents_federation_compiler.evaluate_aggregation_readiness stroke_manifest
      [ completed_result "hospital-a" 300; { (completed_result "hospital-b" 200) with participation = SiteFailed } ]
  in
  assert_true "aggregation must not proceed on a single surviving contribution"
    (not blocked.aggregation_permitted);
  assert_true "a blocked aggregation should report why" (blocked.readiness_stop_reason <> None)

(** Verifies that a secure-aggregation profile requires a reported update norm
    and rejects a contribution that exceeds the permitted bound. *)
let test_secure_aggregation_bounds_contributions () =
  let manifest = { stroke_manifest with aggregation = SecureAggregation 2 } in
  let readiness =
    Dagents_federation_compiler.evaluate_aggregation_readiness manifest
      [
        {
          (completed_result "hospital-a" 300) with
          result_job_digest = Dagents_federation_compiler.round_digest manifest;
          update_norm = None;
        };
        {
          (completed_result "hospital-b" 300) with
          result_job_digest = Dagents_federation_compiler.round_digest manifest;
          update_norm = Some 99.0;
        };
      ]
  in
  assert_true "a missing update norm should be rejected under secure aggregation"
    (List.exists
       (fun rejection -> rejection.rejected_site_id = "hospital-a" && contains rejection.rejection_reason "norm is required")
       readiness.rejected_contributions);
  assert_true "an oversized update should be rejected"
    (List.exists
       (fun rejection -> rejection.rejected_site_id = "hospital-b" && contains rejection.rejection_reason "exceeds")
       readiness.rejected_contributions);
  assert_true "no contribution should survive" (readiness.accepted_sites = [])

(** Release gates used across the release tests. *)
let release_gates =
  [
    { gate_id = "discrimination"; gate_metric = "auc"; comparison = AtLeast 0.85; gate_blocking = true };
    {
      gate_id = "improves_on_current";
      gate_metric = "auc";
      comparison = ImprovesOnBaseline 0.02;
      gate_blocking = true;
    };
    {
      gate_id = "alert_burden";
      gate_metric = "alerts_per_1000";
      comparison = AtMost 40.0;
      gate_blocking = false;
    };
  ]

(** Verifies that a candidate clearing every gate is recommended for release,
    and that the recommendation still carries a rollback point. *)
let test_release_allows_a_passing_candidate () =
  let decision =
    Dagents_federation_compiler.evaluate_release release_gates
      [ ("auc", 0.91); ("alerts_per_1000", 28.0) ]
      [ ("auc", 0.86) ] "candidate-v4" (Some "release-v3") "round-0042"
  in
  assert_true "a passing candidate should be recommended for release"
    (decision.action = ReleaseCandidate);
  assert_true "no blocking failures should be reported" (decision.blocking_failures = []);
  assert_true "every gate should be evaluated"
    (List.for_all (fun result -> result.outcome = GatePassed) decision.gate_results);
  assert_true "a rollback point must survive into the decision"
    (decision.rollback_version = Some "release-v3")

(** Verifies that a failing blocking gate rejects the candidate, a failing
    advisory gate only demands another round, and that a metric the candidate
    never reported blocks rather than silently passing. *)
let test_release_gates_block_and_defer () =
  let rejected =
    Dagents_federation_compiler.evaluate_release release_gates
      [ ("auc", 0.70); ("alerts_per_1000", 28.0) ]
      [ ("auc", 0.86) ] "candidate-v4" (Some "release-v3") "round-0042"
  in
  assert_true "a failing blocking gate should reject the candidate" (rejected.action = RejectCandidate);
  assert_true "the failure should name the gate"
    (List.exists (fun failure -> contains failure "discrimination") rejected.blocking_failures);
  let deferred =
    Dagents_federation_compiler.evaluate_release release_gates
      [ ("auc", 0.91); ("alerts_per_1000", 90.0) ]
      [ ("auc", 0.86) ] "candidate-v4" (Some "release-v3") "round-0042"
  in
  assert_true "a failing advisory gate should demand another round"
    (deferred.action = RequireAnotherRound);
  assert_true "an advisory failure is not a blocking failure" (deferred.blocking_failures = []);
  (* A metric the candidate never reported is the dangerous case: treating an
     absent subgroup measure as a pass would release an unevaluated model. *)
  let unreported =
    Dagents_federation_compiler.evaluate_release release_gates [ ("alerts_per_1000", 28.0) ] [ ("auc", 0.86) ]
      "candidate-v4" None "round-0042"
  in
  assert_true "an unreported blocking metric must not pass" (unreported.action = RejectCandidate);
  assert_true "an unreported metric should be marked not-evaluated"
    (List.exists (fun result -> result.outcome = GateNotEvaluated) unreported.gate_results);
  let no_baseline =
    Dagents_federation_compiler.evaluate_release release_gates [ ("auc", 0.91); ("alerts_per_1000", 28.0) ] []
      "candidate-v4" None "round-0042"
  in
  assert_true "a missing baseline must not pass an improvement gate"
    (no_baseline.action = RejectCandidate)

(** Verifies that governance payloads survive the JSON boundary services use,
    since the planners are reached over a subprocess rather than in-process. *)
let test_governance_and_federation_codec_roundtrip () =
  let request_json =
    `Assoc
      [
        ("requestId", `String "req-9");
        ("boundary", `String "before_release");
        ( "requester",
          `Assoc
            [
              ("requesterId", `String "committee");
              ("requesterKind", `String "human");
              ("complianceHistory", `Float 0.8);
              ( "attributes",
                `List [ `Assoc [ ("attributeId", `String "id"); ("weight", `Float 1.0); ("verified", `Bool true) ] ] );
            ] );
        ( "classification",
          `Assoc
            [
              ("classificationId", `String "stroke-v1");
              ("fieldSensitivity", `Assoc [ ("nihss_total", `String "high") ]);
              ("defaultSensitivity", `String "medium");
              ("regulations", `List [ `String "HIPAA" ]);
              ("minimumCohort", `Int 20);
            ] );
        ("requestedFields", `List [ `String "nihss_total" ]);
        ("granularity", `String "column");
        ("cohortSize", `Int 100);
      ]
  in
  let request = Json_codec.restriction_request_of_yojson request_json in
  assert_true "boundary should round-trip" (request.boundary = BeforeRelease);
  assert_true "granularity should round-trip" (request.granularity = ColumnGrain);
  let plan_json =
    Json_codec.yojson_of_restriction_plan (Dagents_governance_compiler.plan_restrictions request)
  in
  assert_true "a serialized plan should carry its filtering score"
    (contains (Yojson.Safe.to_string plan_json) "filteringScore");
  (* An omitted defaultSensitivity must fall back to the most protected value,
     so a careless payload cannot widen what the planner permits. *)
  let permissive =
    Json_codec.restriction_request_of_yojson
      (`Assoc
        [
          ("requestId", `String "req-10");
          ("boundary", `String "before_read");
          ("requester", `Assoc [ ("requesterId", `String "svc") ]);
          ("classification", `Assoc [ ("classificationId", `String "c") ]);
          ("requestedFields", `List [ `String "any" ]);
          ("granularity", `String "row");
        ])
  in
  assert_true "an unclassified field should default to high sensitivity"
    (permissive.classification.default_sensitivity = HighSensitivity);
  assert_true "an unstated attribute verification should default to unverified"
    ((Json_codec.kyu_attribute_of_yojson (`Assoc [ ("attributeId", `String "a") ])).attribute_verified = false);
  let manifest_json = Json_codec.yojson_of_round_manifest stroke_manifest in
  let reparsed = Json_codec.round_manifest_of_yojson manifest_json in
  assert_true "a round manifest should survive a JSON round trip"
    (Dagents_federation_compiler.round_digest reparsed
    = Dagents_federation_compiler.round_digest stroke_manifest);
  assert_true "an unstated site enrolment should default to not enrolled"
    ((Json_codec.site_registration_of_yojson
        (`Assoc [ ("siteId", `String "s"); ("featureContractVersion", `String "v") ]))
       .site_enrolled
    = false);
  assert_true "an unstated gate severity should default to blocking"
    ((Json_codec.release_gate_of_yojson
        (`Assoc
          [
            ("gateId", `String "g");
            ("metric", `String "auc");
            ("comparison", `Assoc [ ("kind", `String "at_least"); ("value", `Float 0.8) ]);
          ]))
       .gate_blocking
    = true);
  expect_invalid_argument "an unknown granularity should be rejected" (fun () ->
      Json_codec.restriction_request_of_yojson
        (`Assoc
          [
            ("requestId", `String "req-11");
            ("boundary", `String "before_read");
            ("requester", `Assoc [ ("requesterId", `String "svc") ]);
            ("classification", `Assoc [ ("classificationId", `String "c") ]);
            ("requestedFields", `List []);
            ("granularity", `String "galaxy");
          ]))

(** Run one named test and print a progress line before and after it. *)
let run_test ~index ~total (name, test) =
  Printf.printf "%s %02d/%02d RUN  %s\n%!" (progress_bar ~completed:(index - 1) ~total) index total name;
  test ();
  Printf.printf "%s %02d/%02d PASS %s\n%!" (progress_bar ~completed:index ~total) index total name

(** Run all regression tests with a visible report. Dune treats uncaught
    exceptions as test failures, so a failing test stops after printing the
    last RUN line. *)
let () =
  let tests =
    [
      ("dataset profile", test_dataset_profile);
      ("source validation and extraction plan", test_dataset_source_and_extraction_plan);
      ("source validation negative cases", test_dataset_source_negative_cases);
      ("time-window partition planning", test_time_window_partition_planning);
      ("schema, quality, and transform APIs", test_schema_quality_and_transform_apis);
      ("schema validation negative cases", test_schema_validation_negative_cases);
      ("pipeline ordering and lowering", test_pipeline_compiler_orders_and_lowers);
      ("pipeline cycle rejection", test_pipeline_compiler_rejects_cycles);
      ("pipeline duplicate and unknown dependency rejection", test_pipeline_compiler_rejects_duplicates_and_unknown_dependencies);
      ("model routing", test_model_router);
      ("manifest compiler plan", test_manifest_compiler_plan);
      ("JSON codec round trip", test_json_codec_roundtrip);
      ("KYU trust assessment", test_kyu_assessment);
      ("model updates are never released raw", test_model_update_is_never_allowed_raw);
      ("restriction strategy is monotone in trust", test_strategy_is_monotone_in_trust);
      ("high sensitivity with low trust is refused", test_high_sensitivity_low_trust_is_refused);
      ("restriction plan permits bounded egress", test_restriction_plan_permits_bounded_egress);
      ("restriction plan request-level gates", test_restriction_plan_request_level_gates);
      ("round plan selects eligible sites", test_round_plan_selects_eligible_sites);
      ("round plan exclusion reasons", test_round_plan_exclusion_reasons);
      ("secure aggregation raises quorum", test_secure_aggregation_raises_quorum);
      ("round digest is deterministic", test_round_digest_is_deterministic);
      ("aggregation accepts verified contributions", test_aggregation_readiness_accepts_verified_contributions);
      ("aggregation rejects bad contributions", test_aggregation_readiness_rejects_bad_contributions);
      ("secure aggregation bounds contributions", test_secure_aggregation_bounds_contributions);
      ("release allows a passing candidate", test_release_allows_a_passing_candidate);
      ("release gates block and defer", test_release_gates_block_and_defer);
      ("governance and federation codec round trip", test_governance_and_federation_codec_roundtrip);
    ]
  in
  let total = List.length tests in
  Printf.printf "Dagents OCaml functional test suite\n";
  Printf.printf "Running %d test groups under Dune\n%!" total;
  List.iteri (fun offset test -> run_test ~index:(offset + 1) ~total test) tests;
  Printf.printf "%s %02d/%02d OK   all functional planner tests passed\n%!"
    (progress_bar ~completed:total ~total)
    total total
