(** Shared intermediate representation for Dagents functional kernels.

    This interface defines the typed contract used by the dataset compiler,
    pipeline compiler, model router, manifest compiler, and the CLI wrapper.
    The types are intentionally plain OCaml records and variant types so that
    planner code can stay deterministic, easy to test, and independent from
    Python or service-framework runtime concerns.

    Example test case:
    {[
      let profile =
        {
          scope_id = "tenant-a";
          scope_kind = Source;
          extraction_strategy = Tabular;
          record_count = 100;
          feature_fields = [ "amount" ];
          label_field = None;
          numeric_fields = [ "amount" ];
          categorical_fields = [];
          partition_count = 1;
          suggested_models = [ Autoencoder ];
        }
      in
      assert (string_of_model_family (List.hd profile.suggested_models) = "autoencoder")
    ]} *)

(** Core enum types shared across all planner categories.

    This module holds the closed choice sets used by later IR groups. The
    [include] keeps existing callers source-compatible, so both
    [Dagents_common_ir.model_family] and
    [Dagents_common_ir.Core_types.model_family] remain available. *)
module Core_types : sig
  (** Describes the shape of data extraction that a source run should use. *)
  type extraction_strategy = Tabular | TimeSeries | Text | Hybrid

  (** Describes whether a profile belongs to one source or assimilated data. *)
  type scope_kind = Source | Assimilated

  (** ML task requested from the router or model service. *)
  type task_type = AnomalyDetection | Classification | Forecasting | Embedding | Regression

  (** Pipeline step categories supported by the generic pipeline executor. *)
  type step_kind = EnrichContext | FilterItems | SummarizeItems | ProjectFields | ProfileDataset | RunModelJob

  (** Supported model families in framework-level planning. *)
  type model_family =
    | Autoencoder
    | VariationalAutoencoder
    | Gru
    | Lstm
    | NaiveBayes
    | Transformer
    | RandomForest
    | Xgboost
    | Linear
    | Custom

  (** Deployment/runtime shape selected for a routed model workload. *)
  type packaging_mode = InlineServiceCall | KubernetesJobExecution | LongRunningDeployment

  (** Source connector family used by source specs and extraction plans. *)
  type source_kind = Inline | Postgres | Mongodb | ObjectStorage

  (** Generic lifecycle state for model jobs and pipeline runs. *)
  type job_status = Queued | Running | Completed | Failed | Cancelled

  (** Kubernetes object family emitted by the manifest compiler. *)
  type workload_kind = Deployment | Job | CronJob | Service | ConfigMap | ServiceAccount

  (** Runtime target selected for an individual compiled pipeline step. *)
  type pipeline_execution_target = LocalProcess | PythonService | KubernetesJobTarget
end

include module type of Core_types

(** Record and schema primitives.

    These values are the common data substrate for profiling, quality checks,
    source extraction, and transformation. They deliberately avoid connector
    concepts so they can be reused by any functional kernel. *)
module Record_types : sig
  (** JSON-compatible scalar value used by compiler inputs and record batches. *)
  type value =
    | VString of string
    | VInt of int
    | VFloat of float
    | VBool of bool
    | VNull

  (** One logical row or event represented as field/value pairs.

      Example:
      {[
        [ ("amount", VFloat 42.0); ("country", VString "IN") ]
      ]} *)
  type record = (string * value) list

  (** Field name and logical dtype inferred or declared for a record batch. *)
  type record_schema_field = {
    field_name : string;
    dtype : string;
  }

  (** Execution metadata for a fetched record batch. *)
  type record_batch_stats = {
    record_count : int;
    truncated : bool;
  }

  (** Records plus schema and checkpoint information returned from extraction. *)
  type record_batch = {
    records : record list;
    schema : record_schema_field list;
    next_checkpoint : Yojson.Safe.t option;
    stats : record_batch_stats;
  }
end

include module type of Record_types

(** Source and extraction contracts.

    This module groups connector-specific inputs, validation outputs, and the
    normalized extraction plan that runtime executors consume. *)
module Source_types : sig
  (** Sort instruction used by Mongo and source-selection adapters. *)
  type selection_sort = { field : string; direction : string }

  (** Opaque connection reference supplied by a product backend. *)
  type connection_ref = {
    connection_id : string;
    connection_options : (string * string) list;
  }

  (** Declarative Postgres read selection. *)
  type postgres_selection = {
    sql : string option;
    table : string option;
    columns : string list;
    where_clause : string option;
    order_by : string list;
  }

  (** Declarative MongoDB read selection. *)
  type mongo_selection = {
    database : string;
    collection : string;
    filter_json : Yojson.Safe.t option;
    projection_json : Yojson.Safe.t option;
    sort : selection_sort list;
  }

  (** Declarative object-storage selection for files or prefixes. *)
  type object_storage_selection = {
    uri : string option;
    prefix : string option;
    glob : string option;
    compression : string option;
  }

  (** Connector-specific selection payload. *)
  type source_selection =
    | InlineSelection of record list
    | PostgresSelection of postgres_selection
    | MongoSelection of mongo_selection
    | ObjectStorageSelection of object_storage_selection

  (** Runtime batching constraints for source extraction. *)
  type source_batching = {
    batch_size : int;
    max_records : int option;
  }

  (** Full source description consumed by the dataset compiler.

      Example test case:
      {[
        let source =
          {
            source_id = "orders-inline";
            source_kind = Inline;
            connection_ref = None;
            selection = InlineSelection [ [ ("amount", VFloat 10.0) ] ];
            format = "json";
            schema_hint = [];
            batching = { batch_size = 100; max_records = None };
            checkpoint = None;
            options = [];
          }
        in
        assert ((validate_source source).valid)
      ]} *)
  type source_spec = {
    source_id : string;
    source_kind : source_kind;
    connection_ref : connection_ref option;
    selection : source_selection;
    format : string;
    schema_hint : (string * string) list;
    batching : source_batching;
    checkpoint : Yojson.Safe.t option;
    options : (string * string) list;
  }

  (** Dataset input wrapper for inline data, a full source, or a source id. *)
  type dataset_input = {
    inline_records : record list;
    source : source_spec option;
    source_id_ref : string option;
  }

  (** Validation outcome for [source_spec]. *)
  type source_validation_result = {
    valid : bool;
    errors : string list;
    warnings : string list;
  }

  (** Static metadata that can be derived from a source without running models. *)
  type source_metadata = {
    source_id : string;
    source_kind : source_kind;
    schema : record_schema_field list;
    estimated_records : int option;
  }

  (** Partitioning strategy selected for source extraction. *)
  type partition_strategy =
    | SinglePartition
    | FixedSize of int
    | HashPartition of string * int
    | TimeWindow of string * string

  (** Lowered, execution-ready source extraction plan. *)
  type extraction_plan = {
    extraction_source_id : string;
    extraction_source_kind : source_kind;
    extraction_format : string;
    selected_fields : string list;
    predicates : string list;
    ordering : string list;
    partition_strategy : partition_strategy;
    extraction_batch_size : int;
    extraction_max_records : int option;
    extraction_checkpoint : Yojson.Safe.t option;
  }
end

include module type of Source_types

(** Schema contract validation records.

    Schema validation is separate from data-quality rules: it answers whether
    the inferred fields satisfy declared type and presence constraints. *)
module Schema_types : sig
  (** Required and optional schema policy for validating inferred fields. *)
  type schema_contract = {
    required_fields : record_schema_field list;
    optional_fields : record_schema_field list;
    allow_extra_fields : bool;
  }

  (** One field-level schema mismatch. *)
  type schema_validation_issue = {
    issue_field : string;
    expected_dtype : string;
    actual_dtype : string option;
  }

  (** Full schema validation result. *)
  type schema_validation_report = {
    schema_valid : bool;
    missing_fields : record_schema_field list;
    type_mismatches : schema_validation_issue list;
    extra_fields : record_schema_field list;
    schema_warnings : string list;
  }
end

include module type of Schema_types

(** Data-quality rule and result records.

    Quality rules can be warning-only or blocking, so the severity travels with
    both the rule definition and the evaluated result. *)
module Quality_types : sig
  (** Data-quality predicate applied to one field. *)
  type quality_operator =
    | NonNull
    | Unique
    | MinValue of float
    | MaxValue of float
    | RegexMatch of string
    | AllowedValues of string list

  (** Severity used to decide whether quality violations should block a run. *)
  type quality_severity = Info | Warning | Error

  (** Declarative quality rule evaluated by the dataset compiler. *)
  type quality_rule = {
    rule_id : string;
    field : string;
    operator : quality_operator;
    severity : quality_severity;
  }

  (** Result for one evaluated quality rule. *)
  type quality_result = {
    quality_rule_id : string;
    quality_severity : quality_severity;
    passed : bool;
    violations : int;
    quality_message : string;
  }

  (** Aggregate quality result across all evaluated rules. *)
  type quality_report = {
    quality_results : quality_result list;
    blocking : bool;
    warning_count : int;
    error_count : int;
    total_violations : int;
  }
end

include module type of Quality_types

(** Transform planning types.

    Transform plans are pure record operations with a predicted output schema,
    which makes them easy to inspect and test before execution. *)
module Transform_types : sig
  (** Pure record transformation operation. *)
  type transform_operation =
    | SelectFields of string list
    | DropFields of string list
    | RenameFields of (string * string) list
    | FilterNonNull of string list
    | CastFields of (string * string) list

  (** Compiled transform plan with predicted output schema. *)
  type transform_plan = {
    transform_plan_id : string;
    operations : transform_operation list;
    output_schema : record_schema_field list;
  }
end

include module type of Transform_types

(** Dataset profile and model-routing input types. *)
module Profile_types : sig
  (** Summary of a dataset used by routing and planning. *)
  type dataset_profile = {
    scope_id : string;
    scope_kind : scope_kind;
    extraction_strategy : extraction_strategy;
    record_count : int;
    feature_fields : string list;
    label_field : string option;
    numeric_fields : string list;
    categorical_fields : string list;
    partition_count : int;
    suggested_models : model_family list;
  }
end

include module type of Profile_types

(** Generic execution and API envelope types.

    These are shared response shapes, not planner inputs. Grouping them keeps
    service envelopes separate from compiler contracts. *)
module Execution_types : sig
  (** Common job/run lifecycle metadata. *)
  type job_handle = {
    job_id : string;
    status : job_status;
    submitted_at : int;
    started_at : int option;
    completed_at : int option;
  }

  (** Model job response envelope. *)
  type model_job = {
    job : job_handle;
    job_type : string;
    result : Yojson.Safe.t option;
    error : string option;
  }

  (** Pipeline run response envelope. *)
  type pipeline_run = {
    job : job_handle;
    pipeline_id : string;
    result : Yojson.Safe.t option;
    error : string option;
  }

  (** Service-safe error envelope for API boundaries. *)
  type error_envelope = {
    code : string;
    message : string;
    details : Yojson.Safe.t option;
    request_id : string option;
  }

  (** Generic paginated response shape. *)
  type 'a page_response = {
    items : 'a list;
    next_cursor : string option;
    total : int option;
  }
end

include module type of Execution_types

(** Pipeline planning types.

    This module separates user-authored pipeline definitions from compiled
    pipeline plans that already have execution targets assigned. *)
module Pipeline_types : sig
  (** User-authored pipeline step before compilation. *)
  type pipeline_step = {
    step_id : string;
    kind : step_kind;
    depends_on : string list;
    config_json : Yojson.Safe.t option;
  }

  (** User-authored pipeline DAG. *)
  type pipeline_definition = {
    pipeline_id : string;
    steps : pipeline_step list;
  }

  (** Pipeline step after validation and execution-target selection. *)
  type compiled_pipeline_step = {
    step_id : string;
    kind : step_kind;
    depends_on : string list;
    execution_target : pipeline_execution_target;
    config_json : Yojson.Safe.t option;
  }

  (** Validated pipeline with topologically ordered compiled steps. *)
  type compiled_pipeline = {
    pipeline_id : string;
    steps : compiled_pipeline_step list;
  }
end

include module type of Pipeline_types

(** Model-router output types. *)
module Routing_types : sig
  (** Model-router decision for one dataset profile and task. *)
  type route_plan = {
    task_type : task_type;
    selected_model : model_family;
    candidates : model_family list;
    packaging_mode : packaging_mode;
  }
end

include module type of Routing_types

(** Workload and manifest-generation types.

    This module is the manifest compiler's contract surface. It is kept apart
    from pipeline and routing types because it describes deployment artifacts. *)
module Workload_types : sig
  (** Kubernetes environment variable. *)
  type env_var = {
    name : string;
    value : string;
  }

  (** Kubernetes container port declaration. *)
  type port = {
    port_name : string;
    container_port : int;
  }

  (** Kubernetes resource requests and limits. *)
  type resources = {
    cpu_request : string;
    cpu_limit : string;
    memory_request : string;
    memory_limit : string;
  }

  (** One deployable component in a generated workload bundle. *)
  type workload_component = {
    name : string;
    image : string;
    kind : workload_kind;
    replicas : int;
    schedule : string option;
    env : env_var list;
    ports : port list;
    args : string list;
    resources : resources;
    generated_resources : workload_kind list;
    service_account_name : string option;
    service_type : string;
    config_map_data : (string * string) list;
  }

  (** Declarative manifest-generation input.

      Example test case:
      {[
        let spec =
          {
            plan_id = "demo";
            namespace = "default";
            components = [];
            include_services = true;
            include_config_maps = true;
          }
        in
        assert (spec.namespace = "default")
      ]} *)
  type workload_spec = {
    plan_id : string;
    namespace : string;
    components : workload_component list;
    include_services : bool;
    include_config_maps : bool;
  }

  (** Rendered Kubernetes YAML for one workload component. *)
  type workload_manifest = {
    component_name : string;
    kind : workload_kind;
    deployment_yaml : string;
    service_yaml : string option;
    config_map_yaml : string option;
    service_account_yaml : string option;
  }

  (** Complete rendered manifest plan, including per-component and combined YAML. *)
  type workload_plan = {
    plan_id : string;
    namespace : string;
    manifests : workload_manifest list;
    combined_yaml : string;
  }

  (** Default resource requests and limits for generated workload components. *)
  val default_resources : resources
end

include module type of Workload_types

(** GRAILS-style governance types.

    These types express the "Ethical-Restriction Rails" half of GRAILS
    (Kulkarni and Ramanathan, AIES 2025): the part that decides what protection
    a request needs without ever touching data. Keeping the decision here, in
    the pure planner layer, means a new sensitivity level or a new filtering
    strategy fails to compile until every combination is handled, which a
    string-keyed policy table cannot guarantee.

    The enforcement half (the "Ethical Guard") deliberately lives outside
    OCaml, in the LMA and GMA request paths, because it needs real data, a real
    requester, and somewhere to write an audit record. *)
module Governance_types : sig
  (** How much protection the data itself demands. *)
  type sensitivity = LowSensitivity | MediumSensitivity | HighSensitivity

  (** How far the requester is trusted, derived from a Know-Your-User score. *)
  type trust_level = LowTrust | ModerateTrust | HighTrust

  (** How much is being asked for.

      [ModelUpdateGrain] is this project's extension to the published GRAILS
      granularity set. GRAILS covers cell, row, column, and table, all of which
      are data that can be pointed at and read. A federated round ships none of
      them; it ships a model update, which is not a row but still carries
      patient signal out of the hospital. Treating it as a fifth granularity is
      what lets the same planner govern federated egress. *)
  type granularity = CellGrain | RowGrain | ColumnGrain | TableGrain | ModelUpdateGrain

  (** The concrete protection applied to one field or one outbound update.

      Ordered loosely from least to most protective. [ClipContribution] and
      [AddNoise] are the federated-egress strategies; the remainder are the
      row-oriented strategies GRAILS describes. *)
  type restriction_strategy =
    | AllowFull
    | Generalize of int
    | ClipContribution of float
    | AddNoise of float
    | AggregateOnly of int
    | Redact
    | Refuse

  (** Where in the request path the guard is standing.

      A guard only at the API edge is a warning label; in a federated pilot it
      has to stand at all four of these. *)
  type guard_boundary = BeforeRead | BeforeTrain | BeforeSend | BeforeRelease

  (** Overall verdict for one restriction request. *)
  type plan_decision = PermitRequest | NarrowRequest | DenyRequest

  (** One signal feeding the Know-Your-User score. *)
  type kyu_attribute = {
    attribute_id : string;
    attribute_weight : float;
    attribute_verified : bool;
  }

  (** The party asking for data, an update, or a release.

      [requester_kind] carries the federated extension: a requester is not
      always a named human. It can be a peer site or the coordinator itself. *)
  type requester = {
    requester_id : string;
    requester_kind : string;
    affiliation : string option;
    stated_purpose : string option;
    attributes : kyu_attribute list;
    compliance_history : float;
  }

  (** Computed trust for one requester, with the reasoning that produced it. *)
  type kyu_assessment = {
    assessed_requester_id : string;
    kyu_score : float;
    trust : trust_level;
    trust_rationale : string list;
  }

  (** Data-side knowledge: per-field sensitivity plus the rules that cover it.

      This is configuration, not code. Policy changes far more often than the
      framework does. *)
  type data_classification = {
    classification_id : string;
    field_sensitivity : (string * sensitivity) list;
    default_sensitivity : sensitivity;
    regulations : string list;
    minimum_cohort : int;
  }

  (** One request presented to the Restriction Planner. *)
  type restriction_request = {
    request_id : string;
    boundary : guard_boundary;
    requester : requester;
    classification : data_classification;
    requested_fields : string list;
    granularity : granularity;
    cohort_size : int option;
    declared_purpose : string option;
    approved_purposes : string list;
  }

  (** The strategy selected for one requested field. *)
  type field_restriction = {
    restricted_field : string;
    restricted_sensitivity : sensitivity;
    strategy : restriction_strategy;
    restriction_reason : string;
  }

  (** The Restriction Planner's output.

      [filtering_score] is GRAILS' measure of how much protection was actually
      applied, so the amount of filtering can be reported rather than asserted:
      0.0 means nothing was withheld, 1.0 means the request was fully refused. *)
  type restriction_plan = {
    plan_request_id : string;
    plan_boundary : guard_boundary;
    assessment : kyu_assessment;
    plan_granularity : granularity;
    field_restrictions : field_restriction list;
    decision : plan_decision;
    filtering_score : float;
    obligations : string list;
    plan_violations : string list;
  }
end

include module type of Governance_types

(** Federated round-control types.

    Dagents governs the federation; it does not run it. These types describe
    the round contract, who may join, whether aggregation may proceed, and
    whether a candidate may be released. The distributed training protocol
    itself belongs to a specialist runtime such as NVIDIA FLARE, reached
    through an adapter, so nothing here reimplements a federated optimizer.

    The rule these types exist to enforce: aggregation creates a candidate
    model, never an approved clinical release. *)
module Federation_types : sig
  (** What a round is for.

      The recommended pilot order is analytics first, then evaluation, then
      training; the phase is explicit so a study cannot skip ahead silently. *)
  type round_phase = AnalyticsRound | EvaluationRound | TrainingRound

  (** How permitted contributions are combined. *)
  type aggregation_method =
    | FedAvg
    | FedProx of float
    | FedOpt of string
    | SecureAggregation of int

  (** Reasons a round may be halted before it produces a candidate. *)
  type stop_condition =
    | SchemaFailure
    | PrivacyBudgetExceeded
    | UnsafeMetric
    | QuorumNotMet
    | ExcessiveDropout

  (** How one site finished, or failed to finish, a round. *)
  type site_participation = SiteAccepted | SiteCompleted | SiteRejected | SiteFailed | SiteDropped

  (** How a release gate compares an observed metric to its requirement. *)
  type gate_comparison = AtLeast of float | AtMost of float | ImprovesOnBaseline of float

  (** Outcome of one evaluated release gate. *)
  type gate_outcome = GatePassed | GateFailed | GateNotEvaluated

  (** What governance should do with a candidate model. *)
  type release_action = ReleaseCandidate | RequireAnotherRound | RejectCandidate

  (** One site's standing enrolment in a study. *)
  type site_registration = {
    site_id : string;
    site_capabilities : string list;
    site_feature_contract_version : string;
    approved_conditions : string list;
    site_policy_version : string;
    site_cohort_size : int;
    site_enrolled : bool;
  }

  (** The signed round contract distributed to every approved site. *)
  type round_manifest = {
    round_id : string;
    study_id : string;
    condition_id : string;
    phase : round_phase;
    model_version : string;
    model_artifact_digest : string;
    training_code_digest : string;
    feature_contract_version : string;
    privacy_profile : string;
    aggregation : aggregation_method;
    minimum_participants : int;
    minimum_cohort_per_site : int;
    required_capabilities : string list;
    stop_conditions : stop_condition list;
    invited_sites : string list;
  }

  (** One invited site that will not take part, and why. *)
  type site_exclusion = { excluded_site_id : string; exclusion_reason : string }

  (** The compiled, reviewable plan for one round. *)
  type round_plan = {
    plan_round_id : string;
    plan_study_id : string;
    plan_phase : round_phase;
    selected_sites : string list;
    excluded_sites : site_exclusion list;
    quorum_met : bool;
    required_participants : int;
    plan_aggregation : aggregation_method;
    plan_stop_reason : stop_condition option;
    round_digest : string;
  }

  (** What one hospital returns from a round.

      Deliberately minimal: no patient identifier, row, image, note, or
      patient-level prediction has a place in this contract. *)
  type site_result = {
    result_round_id : string;
    result_site_id : string;
    result_job_digest : string;
    participation : site_participation;
    code_verified : bool;
    privacy_checks_passed : bool;
    contributed_examples : int;
    update_norm : float option;
    result_metrics : (string * float) list;
    local_evidence_pointer : string option;
  }

  (** One contribution that may not enter the aggregate, and why. *)
  type contribution_rejection = { rejected_site_id : string; rejection_reason : string }

  (** Whether aggregation may proceed, and on whose contributions. *)
  type aggregation_readiness = {
    readiness_round_id : string;
    accepted_sites : string list;
    rejected_contributions : contribution_rejection list;
    accepted_examples : int;
    aggregation_permitted : bool;
    readiness_stop_reason : stop_condition option;
    site_weights : (string * float) list;
  }

  (** One condition a candidate must satisfy before it may be released. *)
  type release_gate = {
    gate_id : string;
    gate_metric : string;
    comparison : gate_comparison;
    gate_blocking : bool;
  }

  (** Evaluation of one release gate against the candidate's metrics. *)
  type gate_result = {
    result_gate_id : string;
    outcome : gate_outcome;
    observed : float option;
    gate_detail : string;
  }

  (** The governed verdict on a candidate model.

      A passing decision is a recommendation to a human committee, never an
      automatic deployment. *)
  type release_decision = {
    decision_round_id : string;
    candidate_version : string;
    gate_results : gate_result list;
    action : release_action;
    blocking_failures : string list;
    rollback_version : string option;
  }
end

include module type of Federation_types

(** Convert an extraction strategy to its stable JSON/API string. *)
val string_of_extraction_strategy : extraction_strategy -> string
(** Convert a scope kind to its stable JSON/API string. *)
val string_of_scope_kind : scope_kind -> string
(** Convert a model family to its stable JSON/API string. *)
val string_of_model_family : model_family -> string
(** Convert a pipeline step kind to its stable JSON/API string. *)
val string_of_step_kind : step_kind -> string
(** Convert a workload kind to its stable JSON/API string. *)
val string_of_workload_kind : workload_kind -> string
(** Convert a job status to its stable JSON/API string. *)
val string_of_job_status : job_status -> string
(** Convert a packaging mode to its stable JSON/API string. *)
val string_of_packaging_mode : packaging_mode -> string
(** Convert a source kind to its stable JSON/API string. *)
val string_of_source_kind : source_kind -> string
(** Convert a pipeline execution target to its stable JSON/API string. *)
val string_of_execution_target : pipeline_execution_target -> string
(** Convert quality severity to its stable JSON/API string. *)
val string_of_quality_severity : quality_severity -> string
(** Convert a quality operator to a display/API string. *)
val string_of_quality_operator : quality_operator -> string
(** Convert a partition strategy to a compact display/API string. *)
val string_of_partition_strategy : partition_strategy -> string
(** Parse an extraction strategy string; raises [Invalid_argument] on unknown input. *)
val extraction_strategy_of_string : string -> extraction_strategy
(** Parse a model family string; raises [Invalid_argument] on unknown input. *)
val model_family_of_string : string -> model_family
(** Parse a pipeline step kind string; raises [Invalid_argument] on unknown input. *)
val step_kind_of_string : string -> step_kind
(** Parse a workload kind string; raises [Invalid_argument] on unknown input. *)
val workload_kind_of_string : string -> workload_kind
(** Parse a source kind string; raises [Invalid_argument] on unknown input. *)
val source_kind_of_string : string -> source_kind
(** Parse quality severity; raises [Invalid_argument] on unknown input. *)
val quality_severity_of_string : string -> quality_severity

(** Convert a sensitivity level to its stable JSON/API string. *)
val string_of_sensitivity : sensitivity -> string
(** Convert a trust level to its stable JSON/API string. *)
val string_of_trust_level : trust_level -> string
(** Convert a request granularity to its stable JSON/API string. *)
val string_of_granularity : granularity -> string
(** Convert a guard boundary to its stable JSON/API string. *)
val string_of_guard_boundary : guard_boundary -> string
(** Convert a restriction strategy, including its parameter, to a stable string. *)
val string_of_restriction_strategy : restriction_strategy -> string
(** Convert a plan decision to its stable JSON/API string. *)
val string_of_plan_decision : plan_decision -> string
(** Convert a round phase to its stable JSON/API string. *)
val string_of_round_phase : round_phase -> string
(** Convert an aggregation method to its stable JSON/API string. *)
val string_of_aggregation_method : aggregation_method -> string
(** Convert a stop condition to its stable JSON/API string. *)
val string_of_stop_condition : stop_condition -> string
(** Convert a site participation state to its stable JSON/API string. *)
val string_of_site_participation : site_participation -> string
(** Convert a gate outcome to its stable JSON/API string. *)
val string_of_gate_outcome : gate_outcome -> string
(** Convert a release action to its stable JSON/API string. *)
val string_of_release_action : release_action -> string
(** Convert a gate comparison to a display/API string. *)
val string_of_gate_comparison : gate_comparison -> string
(** Parse a sensitivity string; raises [Invalid_argument] on unknown input. *)
val sensitivity_of_string : string -> sensitivity
(** Parse a trust-level string; raises [Invalid_argument] on unknown input. *)
val trust_level_of_string : string -> trust_level
(** Parse a granularity string; raises [Invalid_argument] on unknown input. *)
val granularity_of_string : string -> granularity
(** Parse a guard-boundary string; raises [Invalid_argument] on unknown input. *)
val guard_boundary_of_string : string -> guard_boundary
(** Parse a round-phase string; raises [Invalid_argument] on unknown input. *)
val round_phase_of_string : string -> round_phase
(** Parse a stop-condition string; raises [Invalid_argument] on unknown input. *)
val stop_condition_of_string : string -> stop_condition
(** Parse a site-participation string; raises [Invalid_argument] on unknown input. *)
val site_participation_of_string : string -> site_participation
