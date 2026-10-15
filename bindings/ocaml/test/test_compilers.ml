(** Regression tests for the Dagents OCaml functional kernels.

    These tests exercise the pure compiler modules end-to-end:
    - dataset profiling, source validation, extraction planning, quality checks,
      schema validation, and transforms;
    - pipeline DAG validation and compilation;
    - model routing;
    - manifest rendering for Kubernetes and Docker Compose;
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

(** Assert that two texts are equal, printing both when they differ. *)
let assert_equal_text message expected actual =
  if expected <> actual then begin
    Printf.eprintf "%s\n--- expected ---\n%s\n--- actual ---\n%s\n--- end ---\n%!" message expected actual;
    failwith message
  end

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

(** A Deployment, a CronJob and a Service, with both spec-wide companion flags
    on. Several manifest tests render it. *)
let three_component_spec =
  {
    plan_id = "plan-1";
    namespace = "dagents";
    include_services = true;
    include_config_maps = true;
    target = Kubernetes;
    components =
      [
        {
          name = "core";
          image = "ghcr.io/example/core:latest";
          kind = Deployment;
          replicas = 2;
          schedule = None;
          env = [ { name = "APP_ENV"; value = "cloud" } ];
          ports = [ { port_name = "http"; container_port = 8060; publication = NotPublished } ];
          args = [ "--server.port=8060" ];
          resources = default_resources;
          generated_resources = [];
          service_account_name = None;
          service_type = "ClusterIP";
          config_map_data = [];
          env_files = [];
          health_check = None;
          service_dependencies = [];
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
          generated_resources = [];
          service_account_name = None;
          service_type = "ClusterIP";
          config_map_data = [];
          env_files = [];
          health_check = None;
          service_dependencies = [];
        };
        {
          name = "edge-service";
          image = "ghcr.io/example/edge:latest";
          kind = Service;
          replicas = 1;
          schedule = None;
          env = [];
          ports = [ { port_name = "grpc"; container_port = 9090; publication = NotPublished } ];
          args = [];
          resources = default_resources;
          generated_resources = [];
          service_account_name = None;
          service_type = "ClusterIP";
          config_map_data = [];
          env_files = [];
          health_check = None;
          service_dependencies = [];
        };
      ];
  }

(** Verifies manifest rendering for Deployment, CronJob, Service, ConfigMap,
    environment variables, command args, and plan metadata. *)
let test_manifest_compiler_plan () =
  let plan = Dagents_manifest_compiler.compile_plan three_component_spec in
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

(** Verifies that each gate result carries the threshold it was checked
    against, so a service can show it without recomputing baseline + margin. *)
let test_release_gate_results_carry_their_threshold () =
  let decision =
    Dagents_federation_compiler.evaluate_release release_gates
      [ ("auc", 0.91); ("alerts_per_1000", 28.0) ]
      [ ("auc", 0.86) ] "candidate-v4" (Some "release-v3") "round-0042"
  in
  let threshold_of gate_id =
    (List.find (fun result -> result.result_gate_id = gate_id) decision.gate_results).threshold
  in
  let close expected = function Some value -> Float.abs (value -. expected) < 1e-9 | None -> false in
  assert_true "an at_least gate reports its value" (close 0.85 (threshold_of "discrimination"));
  assert_true "an at_most gate reports its value" (close 40.0 (threshold_of "alert_burden"));
  assert_true "an improvement gate reports baseline + margin" (close 0.88 (threshold_of "improves_on_current"));
  let unreported =
    Dagents_federation_compiler.evaluate_release release_gates [] [ ("auc", 0.86) ] "candidate-v4" None
      "round-0042"
  in
  assert_true "a gate keeps its threshold when the candidate reported no metric"
    (List.for_all (fun result -> result.threshold <> None) unreported.gate_results);
  let no_baseline =
    Dagents_federation_compiler.evaluate_release release_gates [ ("auc", 0.91); ("alerts_per_1000", 28.0) ] []
      "candidate-v4" None "round-0042"
  in
  let improvement =
    List.find (fun result -> result.result_gate_id = "improves_on_current") no_baseline.gate_results
  in
  assert_true "an improvement gate without a baseline has no threshold" (improvement.threshold = None);
  let encoded = Json_codec.yojson_of_release_decision decision in
  let gate_json gate_id =
    match encoded with
    | `Assoc fields -> (
        match List.assoc "gateResults" fields with
        | `List results ->
            List.find
              (fun result ->
                match result with
                | `Assoc result_fields -> List.assoc "gateId" result_fields = `String gate_id
                | _ -> false)
              results
        | _ -> failwith "gateResults is not a list")
    | _ -> failwith "a release decision is not an object"
  in
  let threshold_json gate_id =
    match gate_json gate_id with `Assoc fields -> List.assoc "threshold" fields | _ -> `Null
  in
  assert_true "the threshold is written to JSON" (threshold_json "improves_on_current" <> `Null);
  let encoded_no_baseline = Json_codec.yojson_of_release_decision no_baseline in
  let missing =
    match encoded_no_baseline with
    | `Assoc fields -> (
        match List.assoc "gateResults" fields with
        | `List results ->
            List.exists
              (fun result ->
                match result with
                | `Assoc result_fields ->
                    List.assoc "gateId" result_fields = `String "improves_on_current"
                    && List.assoc "threshold" result_fields = `Null
                | _ -> false)
              results
        | _ -> false)
    | _ -> false
  in
  assert_true "an unknown threshold is written as null" missing

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

(** Verifies the companion objects a component can ask for on its own.

    The spec-wide flags can only say "all components" or "none". A real bundle
    has one component that needs a ConfigMap and several that do not, so the
    per-component list is what makes the compiler usable — and its absence is
    what made the OCaml path render strictly less than the Python fallback it
    was supposed to replace. *)
let test_manifest_per_component_resources () =
  let component =
    {
      name = "router";
      image = "ghcr.io/example/router:latest";
      kind = Deployment;
      replicas = 1;
      schedule = None;
      env = [];
      ports = [ { port_name = "http"; container_port = 8080; publication = NotPublished } ];
      args = [];
      resources = default_resources;
      generated_resources = [ Service; ConfigMap; ServiceAccount ];
      service_account_name = None;
      service_type = "NodePort";
      config_map_data = [ ("router_mode", "edge") ];
      env_files = [];
      health_check = None;
      service_dependencies = [];
    }
  in
  let plan =
    Dagents_manifest_compiler.compile_plan
      {
        plan_id = "per-component";
        namespace = "dagents-generated";
        components = [ component ];
        (* Both spec-wide flags off: everything below comes from the component. *)
        include_services = false;
        include_config_maps = false;
        target = Kubernetes;
      }
  in
  let manifest = List.hd plan.manifests in
  assert_true "a component asking for a Service should get one" (manifest.service_yaml <> None);
  assert_true "a component asking for a ConfigMap should get one" (manifest.config_map_yaml <> None);
  assert_true "a component asking for a ServiceAccount should get one"
    (manifest.service_account_yaml <> None);
  assert_true "the service exposure mode should be the one requested"
    (contains (Option.get manifest.service_yaml) "type: NodePort");
  assert_true "config map data keys must survive verbatim"
    (contains (Option.get manifest.config_map_yaml) "router_mode: \"edge\"");
  assert_true "the pod must be bound to the generated service account"
    (contains manifest.deployment_yaml "serviceAccountName: router");
  assert_true "every rendered object should reach the combined YAML"
    (contains plan.combined_yaml "kind: Service"
    && contains plan.combined_yaml "kind: ConfigMap"
    && contains plan.combined_yaml "kind: ServiceAccount")

(** Verifies that a companion is not rendered twice when it is also the primary.

    A component whose kind is [Service] already renders a Service; attaching one
    again would emit the same object into the bundle twice. *)
let test_manifest_does_not_duplicate_a_primary_object () =
  let component =
    {
      name = "edge";
      image = "ghcr.io/example/edge:latest";
      kind = Service;
      replicas = 1;
      schedule = None;
      env = [];
      ports = [ { port_name = "http"; container_port = 8080; publication = NotPublished } ];
      args = [];
      resources = default_resources;
      generated_resources = [ Service ];
      service_account_name = None;
      service_type = "ClusterIP";
      config_map_data = [];
      env_files = [];
      health_check = None;
      service_dependencies = [];
    }
  in
  let plan =
    Dagents_manifest_compiler.compile_plan
      {
        plan_id = "no-duplicate";
        namespace = "dagents";
        components = [ component ];
        include_services = true;
        include_config_maps = false;
        target = Kubernetes;
      }
  in
  let manifest = List.hd plan.manifests in
  assert_true "a Service component must not also carry a companion Service"
    (manifest.service_yaml = None);
  assert_true "the primary Service is still rendered" (contains manifest.deployment_yaml "kind: Service")

(** One Job with arguments, environment, a port and a generated service
    account, and no spec-wide companions. *)
let job_spec =
  {
    plan_id = "job-1";
    namespace = "batch";
    include_services = false;
    include_config_maps = false;
    target = Kubernetes;
    components =
      [
        {
          name = "seed";
          image = "ghcr.io/example/seed:1.0";
          kind = Job;
          replicas = 1;
          schedule = None;
          env = [ { name = "SEED_ROWS"; value = "400" } ];
          ports = [ { port_name = "metrics"; container_port = 9100; publication = NotPublished } ];
          args = [ "--once" ];
          resources = default_resources;
          generated_resources = [ ServiceAccount ];
          service_account_name = None;
          service_type = "ClusterIP";
          config_map_data = [];
          env_files = [];
          health_check = None;
          service_dependencies = [];
        };
      ];
  }

(** Verifies the exact Kubernetes YAML and JSON for two specs.

    The text was captured before the Docker Compose target was added. Adding a
    target must not change one byte of the Kubernetes output, and these
    literals fail on any change. *)
let test_manifest_kubernetes_output_is_pinned () =
  let check name spec expected_yaml expected_json =
    let plan = Dagents_manifest_compiler.compile_plan spec in
    assert_equal_text (name ^ ": Kubernetes YAML changed") expected_yaml plan.combined_yaml;
    (* Compared as JSON values, so the literal can be laid out for reading and
       the printer's spacing does not matter; key order still does. *)
    assert_equal_text (name ^ ": Kubernetes JSON changed")
      (Yojson.Safe.to_string (Yojson.Safe.from_string expected_json))
      (Yojson.Safe.to_string (Json_codec.yojson_of_workload_plan plan))
  in
  check "three components" three_component_spec
    {|apiVersion: apps/v1
kind: Deployment
metadata:
  name: core
  namespace: dagents
spec:
  replicas: 2
  selector:
    matchLabels:
      app: core
  template:
    metadata:
      labels:
        app: core
    spec:
      containers:
      - name: main
        image: ghcr.io/example/core:latest
        args:
        - "--server.port=8060"
        ports:
        - name: http
          containerPort: 8060
        env:
        - name: APP_ENV
          value: "cloud"
        resources:
          requests:
            cpu: 250m
            memory: 256Mi
          limits:
            cpu: 1
            memory: 1Gi
---
apiVersion: v1
kind: Service
metadata:
  name: core
  namespace: dagents
spec:
  type: ClusterIP
  selector:
    app: core
  ports:
  - name: http
    port: 8060
    targetPort: 8060
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: core-config
  namespace: dagents
data:
  component-kind: "Deployment"
  image: "ghcr.io/example/core:latest"
---
apiVersion: batch/v1
kind: CronJob
metadata:
  name: reconciler
  namespace: dagents
spec:
  schedule: "*/15 * * * *"
  jobTemplate:
    spec:
      template:
        spec:
          restartPolicy: Never
          containers:
          - name: main
            image: ghcr.io/example/reconciler:latest
            args:
            - "--sync"
            env:
            - name: MODE
              value: "reconcile"
            resources:
              requests:
                cpu: 250m
                memory: 256Mi
              limits:
                cpu: 1
                memory: 1Gi
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: reconciler-config
  namespace: dagents
data:
  component-kind: "CronJob"
  image: "ghcr.io/example/reconciler:latest"
---
apiVersion: v1
kind: Service
metadata:
  name: edge-service
  namespace: dagents
spec:
  type: ClusterIP
  selector:
    app: edge-service
  ports:
  - name: grpc
    port: 9090
    targetPort: 9090
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: edge-service-config
  namespace: dagents
data:
  component-kind: "Service"
  image: "ghcr.io/example/edge:latest"|}
    {|{
  "planId": "plan-1",
  "namespace": "dagents",
  "manifests": [
    {
      "componentName": "core",
      "kind": "Deployment",
      "deploymentYaml": "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: core\n  namespace: dagents\nspec:\n  replicas: 2\n  selector:\n    matchLabels:\n      app: core\n  template:\n    metadata:\n      labels:\n        app: core\n    spec:\n      containers:\n      - name: main\n        image: ghcr.io/example/core:latest\n        args:\n        - \"--server.port=8060\"\n        ports:\n        - name: http\n          containerPort: 8060\n        env:\n        - name: APP_ENV\n          value: \"cloud\"\n        resources:\n          requests:\n            cpu: 250m\n            memory: 256Mi\n          limits:\n            cpu: 1\n            memory: 1Gi",
      "serviceYaml": "apiVersion: v1\nkind: Service\nmetadata:\n  name: core\n  namespace: dagents\nspec:\n  type: ClusterIP\n  selector:\n    app: core\n  ports:\n  - name: http\n    port: 8060\n    targetPort: 8060",
      "configMapYaml": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: core-config\n  namespace: dagents\ndata:\n  component-kind: \"Deployment\"\n  image: \"ghcr.io/example/core:latest\"",
      "serviceAccountYaml": null
    },
    {
      "componentName": "reconciler",
      "kind": "CronJob",
      "deploymentYaml": "apiVersion: batch/v1\nkind: CronJob\nmetadata:\n  name: reconciler\n  namespace: dagents\nspec:\n  schedule: \"*/15 * * * *\"\n  jobTemplate:\n    spec:\n      template:\n        spec:\n          restartPolicy: Never\n          containers:\n          - name: main\n            image: ghcr.io/example/reconciler:latest\n            args:\n            - \"--sync\"\n            env:\n            - name: MODE\n              value: \"reconcile\"\n            resources:\n              requests:\n                cpu: 250m\n                memory: 256Mi\n              limits:\n                cpu: 1\n                memory: 1Gi",
      "serviceYaml": null,
      "configMapYaml": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: reconciler-config\n  namespace: dagents\ndata:\n  component-kind: \"CronJob\"\n  image: \"ghcr.io/example/reconciler:latest\"",
      "serviceAccountYaml": null
    },
    {
      "componentName": "edge-service",
      "kind": "Service",
      "deploymentYaml": "apiVersion: v1\nkind: Service\nmetadata:\n  name: edge-service\n  namespace: dagents\nspec:\n  type: ClusterIP\n  selector:\n    app: edge-service\n  ports:\n  - name: grpc\n    port: 9090\n    targetPort: 9090",
      "serviceYaml": null,
      "configMapYaml": "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: edge-service-config\n  namespace: dagents\ndata:\n  component-kind: \"Service\"\n  image: \"ghcr.io/example/edge:latest\"",
      "serviceAccountYaml": null
    }
  ],
  "combinedYaml": "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: core\n  namespace: dagents\nspec:\n  replicas: 2\n  selector:\n    matchLabels:\n      app: core\n  template:\n    metadata:\n      labels:\n        app: core\n    spec:\n      containers:\n      - name: main\n        image: ghcr.io/example/core:latest\n        args:\n        - \"--server.port=8060\"\n        ports:\n        - name: http\n          containerPort: 8060\n        env:\n        - name: APP_ENV\n          value: \"cloud\"\n        resources:\n          requests:\n            cpu: 250m\n            memory: 256Mi\n          limits:\n            cpu: 1\n            memory: 1Gi\n---\napiVersion: v1\nkind: Service\nmetadata:\n  name: core\n  namespace: dagents\nspec:\n  type: ClusterIP\n  selector:\n    app: core\n  ports:\n  - name: http\n    port: 8060\n    targetPort: 8060\n---\napiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: core-config\n  namespace: dagents\ndata:\n  component-kind: \"Deployment\"\n  image: \"ghcr.io/example/core:latest\"\n---\napiVersion: batch/v1\nkind: CronJob\nmetadata:\n  name: reconciler\n  namespace: dagents\nspec:\n  schedule: \"*/15 * * * *\"\n  jobTemplate:\n    spec:\n      template:\n        spec:\n          restartPolicy: Never\n          containers:\n          - name: main\n            image: ghcr.io/example/reconciler:latest\n            args:\n            - \"--sync\"\n            env:\n            - name: MODE\n              value: \"reconcile\"\n            resources:\n              requests:\n                cpu: 250m\n                memory: 256Mi\n              limits:\n                cpu: 1\n                memory: 1Gi\n---\napiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: reconciler-config\n  namespace: dagents\ndata:\n  component-kind: \"CronJob\"\n  image: \"ghcr.io/example/reconciler:latest\"\n---\napiVersion: v1\nkind: Service\nmetadata:\n  name: edge-service\n  namespace: dagents\nspec:\n  type: ClusterIP\n  selector:\n    app: edge-service\n  ports:\n  - name: grpc\n    port: 9090\n    targetPort: 9090\n---\napiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: edge-service-config\n  namespace: dagents\ndata:\n  component-kind: \"Service\"\n  image: \"ghcr.io/example/edge:latest\""
}|};
  check "one job" job_spec
    {|apiVersion: batch/v1
kind: Job
metadata:
  name: seed
  namespace: batch
spec:
  template:
    spec:
      restartPolicy: Never
      serviceAccountName: seed
      containers:
      - name: main
        image: ghcr.io/example/seed:1.0
        args:
        - "--once"
        ports:
        - name: metrics
          containerPort: 9100
        env:
        - name: SEED_ROWS
          value: "400"
        resources:
          requests:
            cpu: 250m
            memory: 256Mi
          limits:
            cpu: 1
            memory: 1Gi
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: seed
  namespace: batch|}
    {|{
  "planId": "job-1",
  "namespace": "batch",
  "manifests": [
    {
      "componentName": "seed",
      "kind": "Job",
      "deploymentYaml": "apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: seed\n  namespace: batch\nspec:\n  template:\n    spec:\n      restartPolicy: Never\n      serviceAccountName: seed\n      containers:\n      - name: main\n        image: ghcr.io/example/seed:1.0\n        args:\n        - \"--once\"\n        ports:\n        - name: metrics\n          containerPort: 9100\n        env:\n        - name: SEED_ROWS\n          value: \"400\"\n        resources:\n          requests:\n            cpu: 250m\n            memory: 256Mi\n          limits:\n            cpu: 1\n            memory: 1Gi",
      "serviceYaml": null,
      "configMapYaml": null,
      "serviceAccountYaml": "apiVersion: v1\nkind: ServiceAccount\nmetadata:\n  name: seed\n  namespace: batch"
    }
  ],
  "combinedYaml": "apiVersion: batch/v1\nkind: Job\nmetadata:\n  name: seed\n  namespace: batch\nspec:\n  template:\n    spec:\n      restartPolicy: Never\n      serviceAccountName: seed\n      containers:\n      - name: main\n        image: ghcr.io/example/seed:1.0\n        args:\n        - \"--once\"\n        ports:\n        - name: metrics\n          containerPort: 9100\n        env:\n        - name: SEED_ROWS\n          value: \"400\"\n        resources:\n          requests:\n            cpu: 250m\n            memory: 256Mi\n          limits:\n            cpu: 1\n            memory: 1Gi\n---\napiVersion: v1\nkind: ServiceAccount\nmetadata:\n  name: seed\n  namespace: batch"
}|}

(** Parse a workload spec from JSON text, as [dagentsc manifest compile --input] does. *)
let spec_of_json text = Json_codec.workload_spec_of_yojson (Yojson.Safe.from_string text)

(** The first component of a spec parsed from JSON text. *)
let component_of_json text = List.hd (spec_of_json text).components

(** Verifies every form of the [target] key, the new component and port keys,
    and the shape errors that stop a request before planning. *)
let test_deployment_target_codec () =
  let target_of text = (spec_of_json text).target in
  let compose placement network existing =
    Compose { compose_placement = placement; compose_network = network; existing_services = existing }
  in
  (* Every Kubernetes form, including a spec written before the target existed. *)
  List.iter
    (fun text -> assert_true ("should mean Kubernetes: " ^ text) (target_of text = Kubernetes))
    [
      {|{"components": []}|};
      {|{"target": null}|};
      {|{"target": "kubernetes"}|};
      {|{"target": {"kind": "kubernetes"}}|};
    ];
  (* The Compose forms and their defaults. *)
  List.iter
    (fun text -> assert_true ("should mean the default Compose target: " ^ text) (target_of text = Compose default_compose_target))
    [
      {|{"target": "compose"}|};
      {|{"target": {"kind": "compose"}}|};
      {|{"target": {"kind": "compose", "placement": null, "network": null}}|};
      {|{"target": {"kind": "compose", "placement": "own", "network": "default", "externalNetwork": false, "existingServices": []}}|};
    ];
  assert_true "default_compose_target is its own project on the default network"
    (default_compose_target
    = { compose_placement = OwnProject; compose_network = ProjectNetwork "default"; existing_services = [] });
  assert_true "every Compose key should be read"
    (target_of
       {|{"target": {"kind": "compose", "placement": "merge", "network": "healthcare-demo_default", "externalNetwork": true, "existingServices": ["gma", "lma-uk-134"]}}|}
    = compose MergedProject (ExternalNetwork "healthcare-demo_default") [ "gma"; "lma-uk-134" ]);
  assert_true "a network that is not external should be declared by the file"
    (target_of {|{"target": {"kind": "compose", "network": "dagents"}}|}
    = compose OwnProject (ProjectNetwork "dagents") []);
  (* The object form round-trips, and always carries all five keys in order. *)
  List.iter
    (fun target ->
      let json = Json_codec.yojson_of_deployment_target target in
      assert_true "a deployment target should read back unchanged"
        (Json_codec.deployment_target_of_yojson json = target))
    [
      Kubernetes;
      Compose default_compose_target;
      compose MergedProject (ExternalNetwork "shared") [ "gma"; "core" ];
      compose OwnProject (ProjectNetwork "dagents") [ "gma" ];
    ];
  (match Json_codec.yojson_of_deployment_target (Compose default_compose_target) with
  | `Assoc fields ->
      assert_true "the Compose object form should list every key in a fixed order"
        (List.map fst fields = [ "kind"; "placement"; "network"; "externalNetwork"; "existingServices" ])
  | _ -> failwith "expected a deployment target object");
  assert_true "Kubernetes in object form is only its kind"
    (Json_codec.yojson_of_deployment_target Kubernetes = `Assoc [ ("kind", `String "kubernetes") ]);
  (* Ports: hostPort decides publication; hostIp is read only with it. *)
  let ports_of text = (component_of_json text).ports |> List.map (fun p -> p.publication) in
  assert_true "port publication keys should parse"
    (ports_of
       {|{"components": [{"name": "a", "image": "x", "ports": [
           {"containerPort": 8080},
           {"containerPort": 8081, "hostPort": null},
           {"containerPort": 8082, "hostIp": "0.0.0.0"},
           {"containerPort": 8083, "hostPort": 9083},
           {"containerPort": 8084, "hostPort": 9084, "hostIp": "0.0.0.0"},
           {"containerPort": 8085, "hostPort": 9085, "hostIp": null}]}]}|}
    = [
        NotPublished;
        NotPublished;
        NotPublished;
        PublishedOn { host_ip = "127.0.0.1"; host_port = 9083 };
        PublishedOn { host_ip = "0.0.0.0"; host_port = 9084 };
        PublishedOn { host_ip = "127.0.0.1"; host_port = 9085 };
      ]);
  (* Components: env files, health check and dependencies, with their defaults. *)
  let plain = component_of_json {|{"components": [{"name": "a", "image": "x"}]}|} in
  assert_true "a component without the Compose keys should get empty defaults"
    (plain.env_files = [] && plain.health_check = None && plain.service_dependencies = []);
  let nulls =
    component_of_json {|{"components": [{"name": "a", "image": "x", "healthCheck": null, "dependsOn": null}]}|}
  in
  assert_true "null healthCheck and dependsOn should mean none"
    (nulls.health_check = None && nulls.service_dependencies = []);
  let full =
    component_of_json
      {|{"components": [{"name": "a", "image": "x",
          "envFiles": ["../../env/.env.shared", "env/site.env"],
          "healthCheck": {"shell": "true", "command": null},
          "dependsOn": [
            {"service": "b"},
            {"service": "c", "condition": null},
            {"service": "d", "condition": "service_started"},
            {"service": "e", "condition": "service_healthy"},
            {"service": "f", "condition": "service_completed_successfully"}]}]}|}
  in
  assert_true "envFiles should keep their order" (full.env_files = [ "../../env/.env.shared"; "env/site.env" ]);
  assert_true "a null command should count as absent"
    (match full.health_check with Some { health_test = ShellTest "true"; _ } -> true | _ -> false);
  assert_true "dependency conditions should parse, defaulting to service_started"
    (List.map (fun d -> (d.dependency_service, d.dependency_condition)) full.service_dependencies
    = [
        ("b", ServiceStarted);
        ("c", ServiceStarted);
        ("d", ServiceStarted);
        ("e", ServiceHealthy);
        ("f", ServiceCompletedSuccessfully);
      ]);
  List.iter
    (fun condition ->
      assert_true "a dependency condition should read back from its word"
        (dependency_condition_of_string (string_of_dependency_condition condition) = condition))
    [ ServiceStarted; ServiceHealthy; ServiceCompletedSuccessfully ];
  (* Shape errors: no plan can be made, so the codec raises. *)
  List.iter
    (fun text -> expect_invalid_argument ("should be refused: " ^ text) (fun () -> spec_of_json text))
    [
      {|{"target": "helm"}|};
      {|{"target": {"kind": "helm"}}|};
      {|{"target": {"placement": "own"}}|};
      {|{"target": {"kind": "compose", "placement": "stack"}}|};
      {|{"target": 3}|};
      {|{"target": ["compose"]}|};
      {|{"components": [{"name": "a", "image": "x", "dependsOn": [{"service": "b", "condition": "started"}]}]}|};
      {|{"components": [{"name": "a", "image": "x", "dependsOn": [{"condition": "service_started"}]}]}|};
      {|{"components": [{"name": "a", "image": "x", "dependsOn": {"service": "b"}}]}|};
      {|{"components": [{"name": "a", "image": "x", "healthCheck": {"command": ["true"], "shell": "true"}}]}|};
      {|{"components": [{"name": "a", "image": "x", "healthCheck": {"intervalSeconds": 5}}]}|};
      {|{"components": [{"name": "a", "image": "x", "healthCheck": {"command": null, "shell": null}}]}|};
      {|{"components": [{"name": "a", "image": "x", "healthCheck": "true"}]}|};
      {|{"components": [{"name": "a", "image": "x", "ports": [{"containerPort": 80, "hostPort": "80"}]}]}|};
    ]

(** Verifies that a Compose spec with a string that is not UTF-8 is refused
    before planning, wherever the string is, and that a Kubernetes spec is
    read as before. Yojson reads an escaped lone low surrogate as bytes that
    are not UTF-8 and refuses an escaped lone high surrogate itself. *)
let test_compose_codec_refuses_text_that_is_not_utf_8 () =
  let compose_spec component = Printf.sprintf {|{"target": "compose", "components": [%s]}|} component in
  List.iter
    (fun text ->
      expect_invalid_argument ("should be refused: " ^ String.escaped text) (fun () -> spec_of_json text))
    [
      compose_spec {|{"name": "a", "image": "\udc80"}|};
      compose_spec "{\"name\": \"a\", \"image\": \"x\255y\"}";
      compose_spec {|{"name": "a", "image": "x", "env": [{"name": "V", "value": "a\udfffb"}]}|};
      compose_spec {|{"name": "a", "image": "x", "args": ["ok", "\udc80"]}|};
      compose_spec {|{"name": "a", "image": "x", "dependsOn": [{"service": "\udc80"}]}|};
      compose_spec "{\"name\": \"a\", \"image\": \"x\", \"x\192\": 1}";
      {|{"target": {"kind": "compose", "existingServices": ["\udc80"]}, "components": [{"name": "a", "image": "x"}]}|};
      {|{"planId": "\udc80", "target": "compose", "components": [{"name": "a", "image": "x"}]}|};
    ];
  (try
     ignore (spec_of_json (compose_spec {|{"name": "a", "image": "\ud800"}|}));
     failwith "an escaped lone high surrogate should be refused"
   with Yojson.Json_error _ -> ());
  assert_true "valid UTF-8 and escaped surrogate pairs should be read"
    ((component_of_json (compose_spec {|{"name": "a", "image": "z\u00fcrich \ud83d\ude00 \u2028"}|})).image
    = "z\195\188rich \240\159\152\128 \226\128\168");
  assert_true "the UTF-8 check should apply to Compose specs only"
    ((component_of_json {|{"components": [{"name": "a", "image": "\udc80"}]}|}).image = "\237\178\128")

(** Verifies how caller values are quoted in a Compose file: YAML escapes,
    [$] doubled so Compose does not read a variable, and UTF-8 copied. *)
let test_compose_quoting () =
  List.iter
    (fun (input, expected) ->
      assert_equal_text ("quote_compose_scalar " ^ String.escaped input) expected
        (Dagents_manifest_compiler.quote_compose_scalar input))
    [
      ("", {|""|});
      ("plain", {|"plain"|});
      ({|C:\path|}, {|"C:\\path"|});
      ({|say "hi"|}, {|"say \"hi\""|});
      ("$HOME and ${VAR} and $$", {|"$$HOME and $${VAR} and $$$$"|});
      ("line1\nline2", {|"line1\nline2"|});
      ("tab\there", {|"tab\there"|});
      ("cr\rx", {|"cr\rx"|});
      ("nul\000x", {|"nul\x00x"|});
      ("bell\001", {|"bell\x01"|});
      ("esc\027", {|"esc\x1B"|});
      ("unit\031", {|"unit\x1F"|});
      ("del\127", {|"del\x7F"|});
      ("z\195\188rich", "\"z\195\188rich\"");
      ("space and: colon # hash", {|"space and: colon # hash"|});
    ]

(** Compile a spec through {!Dagents_manifest_compiler.compile_for_target}
    and return its Compose plan. *)
let compose_plan_of_spec spec =
  match Dagents_manifest_compiler.compile_for_target spec with
  | ComposePlan plan -> plan
  | KubernetesPlan _ -> failwith "expected a Compose plan"

(** The Compose plan for a spec given as JSON text. *)
let compose_plan_of_json text = compose_plan_of_spec (spec_of_json text)

(** A Compose plan's issues as (service, code, value), as JSON shows them. *)
let issue_triples (plan : compose_plan) =
  List.map
    (fun issue ->
      let code, value = compose_issue_code_and_value issue.issue_reason in
      (issue.issue_service, code, value))
    plan.compose_issues

(** The issue codes of one service, in order. *)
let issue_codes_of service (plan : compose_plan) =
  List.filter_map (fun (s, code, _) -> if s = Some service then Some code else None) (issue_triples plan)

(** The Compose file for a spec given as JSON text; fails with the issues if
    there is no file. *)
let compose_yaml_of_json text =
  let plan = compose_plan_of_json text in
  match plan.compose_yaml with
  | Some yaml -> yaml
  | None ->
      failwith
        ("expected a Compose file, got issues: "
        ^ String.concat ", " (List.map (fun (_, code, _) -> code) (issue_triples plan)))

(** One LMA merged into the running healthcare project: the reference
    example of the Compose target, in camelCase as dagentsc reads it. *)
let lma_merge_json =
  {|{
  "planId": "add-hospital-d",
  "target": {
    "kind": "compose",
    "placement": "merge",
    "network": "default",
    "existingServices": ["gma", "lma-uk-134", "lma-it-002", "lma-ch-245", "healthcare-demo-backend", "healthcare-demo-frontend"]
  },
  "components": [
    {
      "name": "lma-hospital-d",
      "image": "dagents/healthcare-demo-backend:local",
      "args": ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8085"],
      "envFiles": ["../../env/.env.shared", "env/.env.healthcare-demo"],
      "env": [
        {"name": "HEALTHCARE_DEMO_ROLE", "value": "lma"},
        {"name": "HEALTHCARE_DEMO_SITE_ID", "value": "hospital-d"},
        {"name": "HEALTHCARE_DEMO_API_PORT", "value": "8085"}
      ],
      "ports": [{"name": "http", "containerPort": 8085, "hostPort": 8085}],
      "healthCheck": {"command": ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8085/api/v1/health')"]},
      "dependsOn": [{"service": "gma", "condition": "service_healthy"}]
    }
  ]
}|}

(** The Compose file [lma_merge_json] renders, byte for byte. *)
let lma_merge_yaml =
  {|services:
  "lma-hospital-d":
    image: "dagents/healthcare-demo-backend:local"
    command: ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8085"]
    env_file:
      - "../../env/.env.shared"
      - "env/.env.healthcare-demo"
    environment:
      "HEALTHCARE_DEMO_ROLE": "lma"
      "HEALTHCARE_DEMO_SITE_ID": "hospital-d"
      "HEALTHCARE_DEMO_API_PORT": "8085"
    ports:
      - "127.0.0.1:8085:8085"
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8085/api/v1/health')"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s
    depends_on:
      "gma":
        condition: service_healthy
    networks:
      - "default"
    restart: unless-stopped
networks:
  "default": {}|}

(** Verifies the reference example: an LMA merged into a running project
    renders byte for byte, [compile_for_target] dispatches on the target, and
    the JSON form has a fixed key order with a null file when there are
    issues. *)
let test_compose_renders_an_lma_merged_into_a_project () =
  let spec = spec_of_json lma_merge_json in
  let plan = compose_plan_of_spec spec in
  assert_true "the merged LMA should have no issues" (plan.compose_issues = []);
  assert_true "the plan should keep its id" (plan.compose_plan_id = "add-hospital-d");
  assert_true "the plan should name its one service" (plan.compose_service_names = [ "lma-hospital-d" ]);
  assert_equal_text "the merged LMA's Compose file changed" lma_merge_yaml (Option.get plan.compose_yaml);
  (match spec.target with
  | Compose target ->
      assert_true "compile_for_target should give compile_compose_plan's plan"
        (Dagents_manifest_compiler.compile_compose_plan target spec = plan)
  | Kubernetes -> failwith "expected a Compose target");
  assert_true "the same spec for Kubernetes should give compile_plan's plan"
    (Dagents_manifest_compiler.compile_for_target { spec with target = Kubernetes }
    = KubernetesPlan (Dagents_manifest_compiler.compile_plan spec));
  (match Json_codec.yojson_of_compose_plan plan with
  | `Assoc fields ->
      assert_true "the Compose plan JSON should have a fixed key order"
        (List.map fst fields = [ "planId"; "services"; "issues"; "composeYaml" ]);
      assert_true "the Compose plan JSON should carry the file"
        (List.assoc "composeYaml" fields = `String lma_merge_yaml && List.assoc "issues" fields = `List [])
  | _ -> failwith "expected a Compose plan object");
  (* The same spec as its own project cannot reach the running gma. *)
  let own =
    compose_plan_of_spec
      {
        spec with
        target =
          Compose
            {
              compose_placement = OwnProject;
              compose_network = ProjectNetwork "default";
              existing_services = [ "gma"; "lma-uk-134" ];
            };
      }
  in
  assert_true "an own project should report the dependency on the running gma"
    (issue_triples own = [ (Some "lma-hospital-d", "dependency_outside_project", Some "gma") ]);
  assert_true "a plan with issues should have no file" (own.compose_yaml = None);
  assert_true "a plan with issues should still name its services" (own.compose_service_names = [ "lma-hospital-d" ]);
  assert_true "the JSON of a plan with issues should hold the issue and a null file"
    (Json_codec.yojson_of_compose_plan own
    = `Assoc
        [
          ("planId", `String "add-hospital-d");
          ("services", `List [ `String "lma-hospital-d" ]);
          ( "issues",
            `List
              [
                `Assoc
                  [
                    ("service", `String "lma-hospital-d");
                    ("code", `String "dependency_outside_project");
                    ("value", `String "gma");
                  ];
              ] );
          ("composeYaml", `Null);
        ])

(** Verifies the kind rule: a Deployment is a service that restarts, a Job
    runs once, and every other kind has no Compose equivalent. *)
let test_compose_maps_every_workload_kind () =
  let spec_for kind =
    Printf.sprintf {|{"target": "compose", "components": [{"name": "a", "image": "x", "kind": "%s"}]}|} kind
  in
  assert_true "a Deployment should restart unless stopped"
    (contains (compose_yaml_of_json (spec_for "Deployment")) "\n    restart: unless-stopped");
  assert_true "a Job should not restart" (contains (compose_yaml_of_json (spec_for "Job")) "\n    restart: \"no\"");
  List.iter
    (fun kind ->
      let plan = compose_plan_of_json (spec_for (string_of_workload_kind kind)) in
      assert_true
        ("a " ^ string_of_workload_kind kind ^ " should have no Compose equivalent")
        (List.map (fun issue -> issue.issue_reason) plan.compose_issues = [ NoComposeEquivalent kind ]
        && plan.compose_yaml = None))
    [ CronJob; Service; ConfigMap; ServiceAccount ]

(** Verifies health checks: none keeps the image's own, both command forms,
    the codec's timing defaults, and the two health check issues. A duration
    may not exceed 9223372036 seconds, the most a Go [time.Duration] holds. *)
let test_compose_health_checks () =
  let with_check check =
    Printf.sprintf {|{"target": "compose", "components": [{"name": "a", "image": "x"%s}]}|} check
  in
  assert_true "a component without a health check should keep the image's own"
    (not (contains (compose_yaml_of_json (with_check "")) "healthcheck"));
  assert_true "a command should run without a shell, with the default timing"
    (contains
       (compose_yaml_of_json (with_check {|, "healthCheck": {"command": ["curl", "-f", "http://127.0.0.1/"]}|}))
       {|    healthcheck:
      test: ["CMD", "curl", "-f", "http://127.0.0.1/"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s
    networks:|});
  assert_true "a shell line should run by the shell, with its own timing"
    (contains
       (compose_yaml_of_json
          (with_check
             {|, "healthCheck": {"shell": "pg_isready -U $USER", "intervalSeconds": 2, "timeoutSeconds": 1, "retries": 30, "startPeriodSeconds": 0}|}))
       {|    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U $$USER"]
      interval: 2s
      timeout: 1s
      retries: 30
      start_period: 0s|});
  let codes check = issue_codes_of "a" (compose_plan_of_json (with_check check)) in
  assert_true "an empty command should be reported"
    (codes {|, "healthCheck": {"command": []}|} = [ "empty_health_check" ]);
  assert_true "an empty shell line should be reported"
    (codes {|, "healthCheck": {"shell": ""}|} = [ "empty_health_check" ]);
  List.iter
    (fun timing ->
      assert_true ("timing should be refused: " ^ timing)
        (codes (Printf.sprintf {|, "healthCheck": {"command": ["true"], %s}|} timing)
        = [ "invalid_health_check_timing" ]))
    [
      {|"intervalSeconds": 0|};
      {|"timeoutSeconds": 0|};
      {|"retries": 0|};
      {|"startPeriodSeconds": -1|};
      {|"intervalSeconds": 9223372037|};
      {|"timeoutSeconds": 9223372037|};
      {|"startPeriodSeconds": 9223372037|};
    ];
  assert_true "the longest duration Compose reads should be accepted"
    (contains
       (compose_yaml_of_json
          (with_check
             {|, "healthCheck": {"command": ["true"], "intervalSeconds": 9223372036, "timeoutSeconds": 9223372036, "retries": 4611686018427387903, "startPeriodSeconds": 9223372036}|}))
       {|      interval: 9223372036s
      timeout: 9223372036s
      retries: 4611686018427387903
      start_period: 9223372036s|});
  assert_true "both health check issues should be reported, emptiness first"
    (codes {|, "healthCheck": {"shell": "", "retries": 0}|} = [ "empty_health_check"; "invalid_health_check_timing" ])

(** Verifies ports: unpublished ports are exposed on the network, published
    ports bind to loopback unless asked, and the port and address issues. *)
let test_compose_ports () =
  let yaml =
    compose_yaml_of_json
      {|{"target": "compose", "components": [{"name": "a", "image": "x", "ports": [
          {"containerPort": 8010},
          {"containerPort": 8085, "hostPort": 18085},
          {"containerPort": 8011},
          {"containerPort": 9100, "hostPort": 9100, "hostIp": "0.0.0.0"}]}]}|}
  in
  assert_true "unpublished ports should be exposed in order, published ports bound in order"
    (contains yaml
       {|    expose:
      - "8010"
      - "8011"
    ports:
      - "127.0.0.1:18085:8085"
      - "0.0.0.0:9100:9100"
    networks:|});
  assert_true "a spec without published ports should have no ports key"
    (not
       (contains
          (compose_yaml_of_json {|{"target": "compose", "components": [{"name": "a", "image": "x", "ports": [{"containerPort": 80}]}]}|})
          "ports:"));
  List.iter
    (fun address ->
      assert_true ("a valid host address should be accepted: " ^ address)
        ((compose_plan_of_json
            (Printf.sprintf
               {|{"target": "compose", "components": [{"name": "a", "image": "x", "ports": [{"containerPort": 80, "hostPort": 8080, "hostIp": "%s"}]}]}|}
               address))
           .compose_issues = []))
    [ "0.0.0.0"; "127.0.0.1"; "10.20.30.40"; "255.255.255.255" ];
  List.iter
    (fun address ->
      assert_true ("an invalid host address should be reported: " ^ address)
        (issue_triples
           (compose_plan_of_json
              (Printf.sprintf
                 {|{"target": "compose", "components": [{"name": "a", "image": "x", "ports": [{"containerPort": 80, "hostPort": 8080, "hostIp": "%s"}]}]}|}
                 address))
        = [ (Some "a", "invalid_host_ip", Some address) ]))
    [ "localhost"; "127.0.0.01"; "256.0.0.1"; "1.2.3"; "1.2.3.4.5"; ""; "::1"; "1.2.3.-4"; "1..3.4" ];
  assert_true "container and host ports outside 1 to 65535 should be reported"
    (issue_triples
       (compose_plan_of_json
          {|{"target": "compose", "components": [{"name": "a", "image": "x", "ports": [
              {"containerPort": 0}, {"containerPort": 65536}, {"containerPort": 65535, "hostPort": 0}, {"containerPort": 1, "hostPort": 65536}]}]}|})
    = [
        (Some "a", "invalid_port", Some "0");
        (Some "a", "invalid_port", Some "65536");
        (Some "a", "invalid_port", Some "0");
        (Some "a", "invalid_port", Some "65536");
      ]);
  assert_true "one host port used twice should clash, across services and host addresses"
    (issue_triples
       (compose_plan_of_json
          {|{"target": "compose", "components": [
              {"name": "a", "image": "x", "ports": [{"containerPort": 80, "hostPort": 8080}, {"containerPort": 81, "hostPort": 8080, "hostIp": "0.0.0.0"}]},
              {"name": "b", "image": "x", "ports": [{"containerPort": 80, "hostPort": 8080, "hostIp": "10.0.0.1"}, {"containerPort": 81, "hostPort": 8081}]}]}|})
    = [ (Some "a", "host_port_conflict", Some "8080"); (Some "b", "host_port_conflict", Some "8080") ]);
  assert_true "an invalid host port should not count as used"
    ((compose_plan_of_json
        {|{"target": "compose", "components": [
            {"name": "a", "image": "x", "ports": [{"containerPort": 80, "hostPort": 70000}]},
            {"name": "b", "image": "x", "ports": [{"containerPort": 80, "hostPort": 8080}]}]}|})
       .compose_issues
    |> List.map (fun issue -> issue.issue_reason)
    = [ InvalidPort 70000 ])

(** The job fixture: a long-running LMA, a seed job and an init job on a
    network that exists already. *)
let job_on_external_network_json =
  {|{"planId": "job-on-external-network",
     "target": {"kind": "compose", "placement": "own", "network": "healthcare-demo_default", "externalNetwork": true, "existingServices": ["gma"]},
     "components": [
       {"name": "lma-hospital-e", "image": "dagents/lma:1",
        "ports": [{"containerPort": 8010}, {"containerPort": 9100, "hostPort": 9100, "hostIp": "0.0.0.0"}],
        "healthCheck": {"command": ["curl", "-fsS", "http://127.0.0.1:8010/health"]}},
       {"name": "seed-hospital-e", "image": "dagents/lma:1", "kind": "Job", "args": ["seed"],
        "dependsOn": [{"service": "lma-hospital-e", "condition": "service_healthy"},
                      {"service": "lma-hospital-e-init", "condition": "service_completed_successfully"}]},
       {"name": "lma-hospital-e-init", "image": "dagents/lma:1", "kind": "job"}]}|}

(** Verifies the network and placement: a network the file declares or one
    that exists already, the network in every service, the network name rule,
    and which dependencies and names each placement allows. *)
let test_compose_network_and_placement () =
  assert_equal_text "the external-network job file changed"
    {|services:
  "lma-hospital-e":
    image: "dagents/lma:1"
    expose:
      - "8010"
    ports:
      - "0.0.0.0:9100:9100"
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://127.0.0.1:8010/health"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 10s
    networks:
      - "healthcare-demo_default"
    restart: unless-stopped
  "seed-hospital-e":
    image: "dagents/lma:1"
    command: ["seed"]
    depends_on:
      "lma-hospital-e":
        condition: service_healthy
      "lma-hospital-e-init":
        condition: service_completed_successfully
    networks:
      - "healthcare-demo_default"
    restart: "no"
  "lma-hospital-e-init":
    image: "dagents/lma:1"
    networks:
      - "healthcare-demo_default"
    restart: "no"
networks:
  "healthcare-demo_default":
    external: true|}
    (compose_yaml_of_json job_on_external_network_json);
  let two_services network =
    Printf.sprintf
      {|{"target": {"kind": "compose", "network": "%s"}, "components": [{"name": "a", "image": "x"}, {"name": "b", "image": "x"}]}|}
      network
  in
  let yaml = compose_yaml_of_json (two_services "dagents.net-1") in
  assert_true "a network the file declares should be an empty mapping"
    (contains yaml "\nnetworks:\n  \"dagents.net-1\": {}" && not (contains yaml "external"));
  assert_true "every service should join the network"
    (contains yaml "  \"a\":\n    image: \"x\"\n    networks:\n      - \"dagents.net-1\"\n"
    && contains yaml "  \"b\":\n    image: \"x\"\n    networks:\n      - \"dagents.net-1\"\n");
  (* Each pair is the name as JSON text and as the parsed value. *)
  List.iter
    (fun (json_text, network) ->
      assert_true ("an invalid network name should be reported: " ^ json_text)
        (issue_triples (compose_plan_of_json (two_services json_text)) = [ (None, "invalid_network_name", Some network) ]))
    [
      ("Bad Net", "Bad Net");
      ("UPPER", "UPPER");
      ("-dash-first", "-dash-first");
      ("_under", "_under");
      ("", "");
      ("a:b", "a:b");
      ({|a\nb|}, "a\nb");
    ];
  let placement_plan placement depends =
    compose_plan_of_json
      (Printf.sprintf
         {|{"target": {"kind": "compose", "placement": "%s", "existingServices": ["gma"]},
            "components": [{"name": "lma-x", "image": "x", "dependsOn": [{"service": "%s"}]}]}|}
         placement depends)
  in
  assert_true "a merged file may depend on a running service" ((placement_plan "merge" "gma").compose_issues = []);
  assert_true "an own project may not depend on a running service"
    (issue_triples (placement_plan "own" "gma") = [ (Some "lma-x", "dependency_outside_project", Some "gma") ]);
  List.iter
    (fun placement ->
      assert_true ("a dependency on nothing known should be reported: " ^ placement)
        (issue_triples (placement_plan placement "nowhere") = [ (Some "lma-x", "unknown_dependency", Some "nowhere") ]))
    [ "own"; "merge" ];
  List.iter
    (fun placement ->
      assert_true ("a running service's name may not be reused: " ^ placement)
        (issue_triples
           (compose_plan_of_json
              (Printf.sprintf
                 {|{"target": {"kind": "compose", "placement": "%s", "existingServices": ["gma"]}, "components": [{"name": "gma", "image": "x"}]}|}
                 placement))
        = [ (Some "gma", "service_name_in_use", None) ]))
    [ "own"; "merge" ];
  assert_true "an invalid running service name should be reported for the spec"
    (issue_triples
       (compose_plan_of_json
          {|{"target": {"kind": "compose", "placement": "merge", "existingServices": ["gma", "Bad Name", "ok", "x y"]}, "components": [{"name": "a", "image": "x"}]}|})
    = [ (None, "invalid_service_name", Some "Bad Name"); (None, "invalid_service_name", Some "x y") ])

(** The quoting fixture: every escape in one service. *)
let quoting_json =
  {|{"planId": "quoting", "target": {"kind": "compose"}, "components": [{"name": "quoting",
     "image": "registry.example/z\u00fcrich:1",
     "args": ["say \"hi\"", "C:\\path", "$HOME", "line1\nline2", "tab\there", "bell\u0001", "del\u007f", "cr\rx"],
     "envFiles": ["env/$SITE.env"],
     "env": [{"name": "PRICE", "value": "$5 and ${VAR}"}, {"name": "_under_score9", "value": ""}],
     "healthCheck": {"shell": "test \"$(cat /tmp/ready)\" = ok", "intervalSeconds": 5, "timeoutSeconds": 2, "retries": 10, "startPeriodSeconds": 0}}]}|}

(** Verifies that every caller value in a rendered file is quoted, with the
    escapes of the quoting table. *)
let test_compose_quoted_file () =
  assert_equal_text "the quoting fixture's Compose file changed"
    {|services:
  "quoting":
    image: "registry.example/zürich:1"
    command: ["say \"hi\"", "C:\\path", "$$HOME", "line1\nline2", "tab\there", "bell\x01", "del\x7F", "cr\rx"]
    env_file:
      - "env/$$SITE.env"
    environment:
      "PRICE": "$$5 and $${VAR}"
      "_under_score9": ""
    healthcheck:
      test: ["CMD-SHELL", "test \"$$(cat /tmp/ready)\" = ok"]
      interval: 5s
      timeout: 2s
      retries: 10
      start_period: 0s
    networks:
      - "default"
    restart: unless-stopped
networks:
  "default": {}|}
    (compose_yaml_of_json quoting_json)

(** The names fixture: valid names that YAML reads as a number, null, a
    boolean or a date when they are not quoted. *)
let yaml_typed_names_json =
  {|{"planId": "yaml-typed-names",
     "target": {"kind": "compose", "placement": "merge", "network": "123", "existingServices": ["null"]},
     "components": [
       {"name": "134", "image": "x",
        "env": [{"name": "NULL", "value": "a"}, {"name": "True", "value": "b"}, {"name": "false", "value": "c"}, {"name": "Y", "value": "d"}],
        "dependsOn": [{"service": "null"}, {"service": "true"}]},
       {"name": "true", "image": "x"},
       {"name": "1e3", "image": "x"},
       {"name": "0x1f", "image": "x"},
       {"name": "1.0", "image": "x"},
       {"name": "1_000", "image": "x"},
       {"name": "2024-01-01", "image": "x"}]}|}

(** Verifies that service, dependency, network and environment names are
    double-quoted. Unquoted, YAML reads [134], [true], [1e3], [0x1f], [1.0],
    [1_000], [2024-01-01], [NULL] and [True] as numbers, booleans, null or a
    date, and Docker Compose refuses a key that is not a string. *)
let test_compose_quotes_names () =
  let plan = compose_plan_of_json yaml_typed_names_json in
  assert_true "names YAML would read as other types are valid names" (plan.compose_issues = []);
  assert_equal_text "the names fixture's Compose file changed"
    {|services:
  "134":
    image: "x"
    environment:
      "NULL": "a"
      "True": "b"
      "false": "c"
      "Y": "d"
    depends_on:
      "null":
        condition: service_started
      "true":
        condition: service_started
    networks:
      - "123"
    restart: unless-stopped
  "true":
    image: "x"
    networks:
      - "123"
    restart: unless-stopped
  "1e3":
    image: "x"
    networks:
      - "123"
    restart: unless-stopped
  "0x1f":
    image: "x"
    networks:
      - "123"
    restart: unless-stopped
  "1.0":
    image: "x"
    networks:
      - "123"
    restart: unless-stopped
  "1_000":
    image: "x"
    networks:
      - "123"
    restart: unless-stopped
  "2024-01-01":
    image: "x"
    networks:
      - "123"
    restart: unless-stopped
networks:
  "123": {}|}
    (Option.get plan.compose_yaml);
  assert_true "an external network's name should be quoted too"
    (contains
       (compose_yaml_of_json
          {|{"target": {"kind": "compose", "network": "123", "externalNetwork": true}, "components": [{"name": "a", "image": "x"}]}|})
       "\nnetworks:\n  \"123\":\n    external: true")

(** A spec with 19 of the 20 issue codes; only [no_services] is missing. *)
let every_issue_json =
  {|{"planId": "every-issue",
     "target": {"kind": "compose", "placement": "own", "network": "Bad Net", "existingServices": ["gma", "Bad Name", "core"]},
     "components": [
      {"name": "Lma_1", "image": "", "kind": "CronJob", "replicas": 3,
       "env": [{"name": "9X", "value": "a"}, {"name": "A", "value": "b"}, {"name": "A", "value": "c"}],
       "ports": [{"containerPort": 0}, {"containerPort": 8080, "hostPort": 70000, "hostIp": "localhost"}, {"containerPort": 8081, "hostPort": 8085}],
       "healthCheck": {"command": [], "intervalSeconds": 0}},
      {"name": "gma", "image": "x", "ports": [{"containerPort": 8086, "hostPort": 8085, "hostIp": "127.0.0.01"}],
       "dependsOn": [{"service": "gma"}, {"service": "b"}, {"service": "b"}, {"service": "nowhere"}]},
      {"name": "b", "image": "x", "dependsOn": [{"service": "c"}]},
      {"name": "c", "image": "x", "dependsOn": [{"service": "b"}, {"service": "gma"}, {"service": "core"}]},
      {"name": "b", "image": "x", "healthCheck": {"shell": "", "retries": 0}}]}|}

(** Verifies that every issue is reported, in the documented order, and that
    a plan with issues has no file. *)
let test_compose_reports_every_issue_in_order () =
  let plan = compose_plan_of_json every_issue_json in
  assert_true "a plan with issues should have no file" (plan.compose_yaml = None);
  assert_true "a plan with issues should name every component in order"
    (plan.compose_service_names = [ "Lma_1"; "gma"; "b"; "c"; "b" ]);
  let expected =
    [
      (None, "invalid_network_name", Some "Bad Net");
      (None, "invalid_service_name", Some "Bad Name");
      (Some "Lma_1", "invalid_service_name", Some "Lma_1");
      (Some "Lma_1", "empty_image", None);
      (Some "Lma_1", "no_compose_equivalent", Some "CronJob");
      (Some "Lma_1", "unsupported_replicas", Some "3");
      (Some "Lma_1", "invalid_environment_name", Some "9X");
      (Some "Lma_1", "duplicate_environment_name", Some "A");
      (Some "Lma_1", "invalid_port", Some "0");
      (Some "Lma_1", "invalid_host_ip", Some "localhost");
      (Some "Lma_1", "invalid_port", Some "70000");
      (Some "Lma_1", "empty_health_check", None);
      (Some "Lma_1", "invalid_health_check_timing", None);
      (Some "gma", "service_name_in_use", None);
      (Some "gma", "invalid_host_ip", Some "127.0.0.01");
      (Some "gma", "host_port_conflict", Some "8085");
      (Some "gma", "self_dependency", None);
      (Some "gma", "duplicate_dependency", Some "b");
      (Some "gma", "unknown_dependency", Some "nowhere");
      (Some "gma", "dependency_cycle", None);
      (Some "b", "dependency_cycle", None);
      (Some "c", "dependency_outside_project", Some "core");
      (Some "c", "dependency_cycle", None);
      (Some "b", "duplicate_service_name", None);
      (Some "b", "empty_health_check", None);
      (Some "b", "invalid_health_check_timing", None);
    ]
  in
  let actual = issue_triples plan in
  if actual <> expected then begin
    let show (service, code, value) =
      Printf.sprintf "(%s, %s, %s)" (Option.value service ~default:"null") code (Option.value value ~default:"null")
    in
    assert_equal_text "every-issue list changed"
      (String.concat "\n" (List.map show expected))
      (String.concat "\n" (List.map show actual))
  end;
  let codes = List.sort_uniq compare (List.map (fun (_, code, _) -> code) actual) in
  assert_true "the spec should cover 19 of the 20 issue codes" (List.length codes = 19 && not (List.mem "no_services" codes));
  let empty = compose_plan_of_json {|{"planId": "no-services", "target": "compose", "components": []}|} in
  assert_true "a spec with no components should report no_services and nothing else"
    (issue_triples empty = [ (None, "no_services", None) ]
    && empty.compose_service_names = [] && empty.compose_yaml = None && empty.compose_plan_id = "no-services")

(** Verifies cycle detection: every member of a cycle is reported, a service
    that only leads into a cycle is not, and a self-dependency is reported
    once, as itself. *)
let test_compose_dependency_cycles () =
  let plan =
    compose_plan_of_json
      {|{"target": "compose", "components": [
          {"name": "a", "image": "x", "dependsOn": [{"service": "b"}]},
          {"name": "b", "image": "x", "dependsOn": [{"service": "a"}]},
          {"name": "c", "image": "x", "dependsOn": [{"service": "a"}]},
          {"name": "d", "image": "x", "dependsOn": [{"service": "e"}]},
          {"name": "e", "image": "x", "dependsOn": [{"service": "f"}]},
          {"name": "f", "image": "x", "dependsOn": [{"service": "d"}, {"service": "c"}]},
          {"name": "g", "image": "x", "dependsOn": [{"service": "g"}, {"service": "c"}]}]}|}
  in
  List.iter
    (fun service ->
      assert_true (service ^ " is in a cycle") (issue_codes_of service plan = [ "dependency_cycle" ]))
    [ "a"; "b"; "d"; "e"; "f" ];
  assert_true "a service that only leads into a cycle is not in it" (issue_codes_of "c" plan = []);
  assert_true "a self-dependency is reported once, not as a cycle" (issue_codes_of "g" plan = [ "self_dependency" ]);
  let chain =
    compose_plan_of_json
      {|{"target": "compose", "components": [
          {"name": "a", "image": "x", "dependsOn": [{"service": "b", "condition": "service_healthy"}]},
          {"name": "b", "image": "x", "dependsOn": [{"service": "c", "condition": "service_completed_successfully"}]},
          {"name": "c", "image": "x", "kind": "Job"}]}|}
  in
  assert_true "a chain without a cycle should render" (chain.compose_issues = [] && chain.compose_yaml <> None)

(** Verifies determinism: the same spec gives the same bytes, and services,
    environment variables, ports and dependencies keep the request's order. *)
let test_compose_output_is_deterministic () =
  let spec = spec_of_json job_on_external_network_json in
  let first = compose_plan_of_spec spec in
  let second = compose_plan_of_spec (spec_of_json job_on_external_network_json) in
  assert_true "two compiles should give the same plan" (first = second);
  let reversed = compose_plan_of_spec { spec with components = List.rev spec.components } in
  assert_true "reversed components should give reversed services"
    (reversed.compose_service_names = List.rev first.compose_service_names);
  let yaml = Option.get reversed.compose_yaml in
  let position needle =
    let rec find index =
      if index + String.length needle > String.length yaml then failwith ("missing " ^ needle)
      else if String.sub yaml index (String.length needle) = needle then index
      else find (index + 1)
    in
    find 0
  in
  assert_true "services should appear in request order, not sorted"
    (position "\n  \"lma-hospital-e-init\":\n" < position "\n  \"seed-hospital-e\":\n"
    && position "\n  \"seed-hospital-e\":\n" < position "\n  \"lma-hospital-e\":\n");
  let ordered =
    compose_yaml_of_json
      {|{"target": {"kind": "compose", "placement": "merge", "existingServices": ["zeta", "alpha"]}, "components": [{"name": "a", "image": "x",
          "env": [{"name": "Z", "value": "1"}, {"name": "A", "value": "2"}, {"name": "M", "value": "3"}],
          "ports": [{"containerPort": 9000}, {"containerPort": 80}, {"containerPort": 81, "hostPort": 9081}, {"containerPort": 82, "hostPort": 8082}],
          "dependsOn": [{"service": "zeta"}, {"service": "alpha"}]}]}|}
  in
  assert_true "environment variables, ports and dependencies should keep their order"
    (contains ordered
       {|    environment:
      "Z": "1"
      "A": "2"
      "M": "3"
    expose:
      - "9000"
      - "80"
    ports:
      - "127.0.0.1:9081:81"
      - "127.0.0.1:8082:82"
    depends_on:
      "zeta":
        condition: service_started
      "alpha":
        condition: service_started
|})

(** Verifies that the Kubernetes renderers ignore the Compose fields and the
    target: one spec renders the same YAML and JSON with and without them. *)
let test_kubernetes_ignores_the_compose_fields () =
  let with_compose_fields =
    {
      three_component_spec with
      target = Compose { default_compose_target with compose_placement = MergedProject; existing_services = [ "gma" ] };
      components =
        List.map
          (fun (c : workload_component) ->
            {
              c with
              ports =
                List.map
                  (fun (p : port) -> { p with publication = PublishedOn { host_ip = "0.0.0.0"; host_port = 18000 } })
                  c.ports;
              env_files = [ "env/.env.shared" ];
              health_check =
                Some
                  {
                    health_test = ShellTest "true";
                    interval_seconds = 1;
                    timeout_seconds = 1;
                    retries = 1;
                    start_period_seconds = 0;
                  };
              service_dependencies = [ { dependency_service = "gma"; dependency_condition = ServiceHealthy } ];
            })
          three_component_spec.components;
    }
  in
  let plain = Dagents_manifest_compiler.compile_plan three_component_spec in
  let extended = Dagents_manifest_compiler.compile_plan with_compose_fields in
  assert_equal_text "Kubernetes YAML should not read the Compose fields" plain.combined_yaml extended.combined_yaml;
  assert_true "Kubernetes plans should be equal with and without the Compose fields" (plain = extended);
  assert_true "Kubernetes JSON should not read the Compose fields"
    (Json_codec.yojson_of_workload_plan plain = Json_codec.yojson_of_workload_plan extended)

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
      ("manifest per-component resources", test_manifest_per_component_resources);
      ("manifest does not duplicate a primary object", test_manifest_does_not_duplicate_a_primary_object);
      ("manifest Kubernetes output is pinned", test_manifest_kubernetes_output_is_pinned);
      ("JSON codec round trip", test_json_codec_roundtrip);
      ("deployment target codec", test_deployment_target_codec);
      ("compose codec refuses text that is not UTF-8", test_compose_codec_refuses_text_that_is_not_utf_8);
      ("compose quoting", test_compose_quoting);
      ("compose renders an LMA merged into a project", test_compose_renders_an_lma_merged_into_a_project);
      ("compose maps every workload kind", test_compose_maps_every_workload_kind);
      ("compose health checks", test_compose_health_checks);
      ("compose ports", test_compose_ports);
      ("compose network and placement", test_compose_network_and_placement);
      ("compose quoted file", test_compose_quoted_file);
      ("compose quotes names", test_compose_quotes_names);
      ("compose reports every issue in order", test_compose_reports_every_issue_in_order);
      ("compose dependency cycles", test_compose_dependency_cycles);
      ("compose output is deterministic", test_compose_output_is_deterministic);
      ("kubernetes ignores the compose fields", test_kubernetes_ignores_the_compose_fields);
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
      ("release gate results carry their threshold", test_release_gate_results_carry_their_threshold);
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
