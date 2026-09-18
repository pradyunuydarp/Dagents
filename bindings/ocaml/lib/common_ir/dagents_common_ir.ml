(** Shared intermediate representation implementation.

    This module intentionally contains only pure type definitions, default
    values, and conversion helpers. It is the stable contract between Dagents
    functional kernels: dataset compilation, pipeline compilation, model
    routing, manifest generation, JSON encoding, and the command-line wrapper.

    The executable code here is deliberately small. Most logic lives in
    compiler-specific modules, while this file keeps the data model explicit
    and easy to pattern-match in tests. *)

(** Core enum types shared across all planner categories.

    This module is intentionally first because later modules refer to task,
    scope, source, pipeline, model, and workload variants. It lets maintainers
    find closed choice sets in one place while the [include] below preserves the
    original top-level API. *)
module Core_types = struct
  (** High-level dataset extraction mode used by profiles and routing inputs. *)
  type extraction_strategy = Tabular | TimeSeries | Text | Hybrid

  (** Dataset scope: one source boundary or an assimilated multi-source boundary. *)
  type scope_kind = Source | Assimilated

  (** ML task requested from model-routing logic. *)
  type task_type = AnomalyDetection | Classification | Forecasting | Embedding | Regression

  (** Pipeline step kind supplied by user-authored workflow definitions. *)
  type step_kind = EnrichContext | FilterItems | SummarizeItems | ProjectFields | ProfileDataset | RunModelJob

  (** Closed set of model families that the router can recommend.

      This is a sum type rather than strings so unsupported model families fail
      early during parsing instead of propagating through planning. *)
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

  (** Runtime packaging selected for a model workload. *)
  type packaging_mode = InlineServiceCall | KubernetesJobExecution | LongRunningDeployment

  (** Connector family for dataset source specifications. *)
  type source_kind = Inline | Postgres | Mongodb | ObjectStorage

  (** Shared job state used by model and pipeline run envelopes. *)
  type job_status = Queued | Running | Completed | Failed | Cancelled

  (** Kubernetes resource kind emitted by the manifest compiler. *)
  type workload_kind = Deployment | Job | CronJob | Service | ConfigMap

  (** Execution target assigned by the pipeline compiler. *)
  type pipeline_execution_target = LocalProcess | PythonService | KubernetesJobTarget
end

include Core_types

(** Record and schema primitives.

    This module holds the smallest data shapes used by source planning,
    profiling, quality checks, and transform execution. Keeping scalar and
    record concepts separate from connectors makes the IR easier to share with
    modules that never need to know about Postgres, MongoDB, or Kubernetes. *)
module Record_types = struct
  (** Minimal scalar value algebra used for schema inference and quality checks. *)
  type value =
    | VString of string
    | VInt of int
    | VFloat of float
    | VBool of bool
    | VNull

  (** Logical input row represented as an association list. *)
  type record = (string * value) list

  (** Field name and dtype pair. *)
  type record_schema_field = {
    field_name : string;
    dtype : string;
  }

  (** Metadata about a returned record batch. *)
  type record_batch_stats = {
    record_count : int;
    truncated : bool;
  }

  (** Extracted records with inferred schema and checkpoint metadata. *)
  type record_batch = {
    records : record list;
    schema : record_schema_field list;
    next_checkpoint : Yojson.Safe.t option;
    stats : record_batch_stats;
  }
end

include Record_types

(** Source and extraction contracts.

    Source-specific records live together so connector ownership is clear.
    Dataset, LMA, GMA, and pipeline code can depend on this group without
    pulling in workload or pipeline execution types. *)
module Source_types = struct
  (** Field-level sort instruction. *)
  type selection_sort = { field : string; direction : string }

  (** External connection identifier plus connector-specific string options. *)
  type connection_ref = {
    connection_id : string;
    connection_options : (string * string) list;
  }

  (** Postgres-specific declarative read selection. *)
  type postgres_selection = {
    sql : string option;
    table : string option;
    columns : string list;
    where_clause : string option;
    order_by : string list;
  }

  (** MongoDB-specific declarative read selection. *)
  type mongo_selection = {
    database : string;
    collection : string;
    filter_json : Yojson.Safe.t option;
    projection_json : Yojson.Safe.t option;
    sort : selection_sort list;
  }

  (** Object-storage-specific declarative file selection. *)
  type object_storage_selection = {
    uri : string option;
    prefix : string option;
    glob : string option;
    compression : string option;
  }

  (** Connector-specific source selection payload. *)
  type source_selection =
    | InlineSelection of record list
    | PostgresSelection of postgres_selection
    | MongoSelection of mongo_selection
    | ObjectStorageSelection of object_storage_selection

  (** Source extraction batching limits. *)
  type source_batching = {
    batch_size : int;
    max_records : int option;
  }

  (** Complete source contract consumed by the dataset compiler. *)
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

  (** API wrapper for inline data, a full source, or a registered source id. *)
  type dataset_input = {
    inline_records : record list;
    source : source_spec option;
    source_id_ref : string option;
  }

  (** Blocking errors and non-blocking warnings from source validation. *)
  type source_validation_result = {
    valid : bool;
    errors : string list;
    warnings : string list;
  }

  (** Metadata that can be derived from a source specification. *)
  type source_metadata = {
    source_id : string;
    source_kind : source_kind;
    schema : record_schema_field list;
    estimated_records : int option;
  }

  (** Extraction partitioning selected by source options and batching. *)
  type partition_strategy =
    | SinglePartition
    | FixedSize of int
    | HashPartition of string * int
    | TimeWindow of string * string

  (** Connector-neutral extraction plan lowered from [source_spec]. *)
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

include Source_types

(** Schema contract validation records.

    These types are kept apart from quality rules because schema validation is
    about field presence/type contracts, while quality rules are record-level
    predicates with severity. *)
module Schema_types = struct
  (** Declarative schema policy for required, optional, and extra fields. *)
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

  (** Full schema validation report. *)
  type schema_validation_report = {
    schema_valid : bool;
    missing_fields : record_schema_field list;
    type_mismatches : schema_validation_issue list;
    extra_fields : record_schema_field list;
    schema_warnings : string list;
  }
end

include Schema_types

(** Data-quality rule and result types.

    Quality checks can be warning-only or blocking, which is why severity lives
    directly on rules and results. *)
module Quality_types = struct
  (** Supported data-quality operators.

      Constructors with arguments carry the threshold, pattern, or allowed value
      set needed for evaluation. *)
  type quality_operator =
    | NonNull
    | Unique
    | MinValue of float
    | MaxValue of float
    | RegexMatch of string
    | AllowedValues of string list

  (** Severity for a quality rule result. *)
  type quality_severity = Info | Warning | Error

  (** One declarative quality rule over a field. *)
  type quality_rule = {
    rule_id : string;
    field : string;
    operator : quality_operator;
    severity : quality_severity;
  }

  (** Result produced by evaluating one quality rule. *)
  type quality_result = {
    quality_rule_id : string;
    quality_severity : quality_severity;
    passed : bool;
    violations : int;
    quality_message : string;
  }

  (** Aggregate report over several quality rule results. *)
  type quality_report = {
    quality_results : quality_result list;
    blocking : bool;
    warning_count : int;
    error_count : int;
    total_violations : int;
  }
end

include Quality_types

(** Transform planning types.

    Transforms are pure record operations that can be inspected before they are
    applied to concrete records. *)
module Transform_types = struct
  (** Pure record transformation operation used by compiled transform plans. *)
  type transform_operation =
    | SelectFields of string list
    | DropFields of string list
    | RenameFields of (string * string) list
    | FilterNonNull of string list
    | CastFields of (string * string) list

  (** Transform operations plus the predicted output schema. *)
  type transform_plan = {
    transform_plan_id : string;
    operations : transform_operation list;
    output_schema : record_schema_field list;
  }
end

include Transform_types

(** Dataset profile and model-routing input types. *)
module Profile_types = struct
  (** Dataset summary consumed by model routing and planning. *)
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

include Profile_types

(** Generic execution and API envelope types.

    These are shared response shapes rather than planner inputs. Keeping them
    grouped makes it easier to evolve service envelopes without touching source
    or workload contracts. *)
module Execution_types = struct
  (** Common run lifecycle metadata. *)
  type job_handle = {
    job_id : string;
    status : job_status;
    submitted_at : int;
    started_at : int option;
    completed_at : int option;
  }

  (** Model job envelope returned by service-facing APIs. *)
  type model_job = {
    job : job_handle;
    job_type : string;
    result : Yojson.Safe.t option;
    error : string option;
  }

  (** Pipeline run envelope returned by service-facing APIs. *)
  type pipeline_run = {
    job : job_handle;
    pipeline_id : string;
    result : Yojson.Safe.t option;
    error : string option;
  }

  (** API error shape shared across service boundaries. *)
  type error_envelope = {
    code : string;
    message : string;
    details : Yojson.Safe.t option;
    request_id : string option;
  }

  (** Generic paginated response. *)
  type 'a page_response = {
    items : 'a list;
    next_cursor : string option;
    total : int option;
  }
end

include Execution_types

(** Pipeline planning types. *)
module Pipeline_types = struct
  (** User-authored pipeline step before validation and target assignment. *)
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

  (** Pipeline step after topological ordering and target assignment. *)
  type compiled_pipeline_step = {
    step_id : string;
    kind : step_kind;
    depends_on : string list;
    execution_target : pipeline_execution_target;
    config_json : Yojson.Safe.t option;
  }

  (** Validated pipeline plan. *)
  type compiled_pipeline = {
    pipeline_id : string;
    steps : compiled_pipeline_step list;
  }
end

include Pipeline_types

(** Model-router output types. *)
module Routing_types = struct
  (** Model-router output. *)
  type route_plan = {
    task_type : task_type;
    selected_model : model_family;
    candidates : model_family list;
    packaging_mode : packaging_mode;
  }
end

include Routing_types

(** Workload and manifest-generation types.

    This module is the manifest compiler's contract surface. It is grouped away
    from pipeline/model contracts because it concerns deployment artifacts, not
    runtime data processing. *)
module Workload_types = struct
  (** Kubernetes environment variable declaration. *)
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

  (** One component that should become Kubernetes YAML. *)
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
  }

  (** Full manifest-compiler input. *)
  type workload_spec = {
    plan_id : string;
    namespace : string;
    components : workload_component list;
    include_services : bool;
    include_config_maps : bool;
  }

  (** Rendered YAML fragments for one component. *)
  type workload_manifest = {
    component_name : string;
    kind : workload_kind;
    deployment_yaml : string;
    service_yaml : string option;
    config_map_yaml : string option;
  }

  (** Complete manifest plan including combined YAML. *)
  type workload_plan = {
    plan_id : string;
    namespace : string;
    manifests : workload_manifest list;
    combined_yaml : string;
  }

  (** Conservative default resources used when callers do not tune a component. *)
  let default_resources =
    {
      cpu_request = "250m";
      cpu_limit = "1";
      memory_request = "256Mi";
      memory_limit = "1Gi";
    }
end

include Workload_types

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
module Governance_types = struct
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
    (* Coarsening level, not decimal places: higher is coarser. *)
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

include Governance_types

(** Federated round-control types.

    Dagents governs the federation; it does not run it. These types describe
    the round contract, who may join, whether aggregation may proceed, and
    whether a candidate may be released. The distributed training protocol
    itself belongs to a specialist runtime such as NVIDIA FLARE, reached
    through an adapter, so nothing here reimplements a federated optimizer.

    The rule these types exist to enforce: aggregation creates a candidate
    model, never an approved clinical release. *)
module Federation_types = struct
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

include Federation_types

(** Convert extraction strategy variants to stable API strings. *)
let string_of_extraction_strategy = function
  | Tabular -> "tabular"
  | TimeSeries -> "time_series"
  | Text -> "text"
  | Hybrid -> "hybrid"

(** Convert scope variants to stable API strings. *)
let string_of_scope_kind = function
  | Source -> "source"
  | Assimilated -> "assimilated"

(** Convert model-family variants to stable API strings. *)
let string_of_model_family = function
  | Autoencoder -> "autoencoder"
  | VariationalAutoencoder -> "variational_autoencoder"
  | Gru -> "gru"
  | Lstm -> "lstm"
  | NaiveBayes -> "naive_bayes"
  | Transformer -> "transformer"
  | RandomForest -> "random_forest"
  | Xgboost -> "xgboost"
  | Linear -> "linear"
  | Custom -> "custom"

(** Convert pipeline-step variants to stable API strings. *)
let string_of_step_kind = function
  | EnrichContext -> "enrich_context"
  | FilterItems -> "filter_items"
  | SummarizeItems -> "summarize_items"
  | ProjectFields -> "project_fields"
  | ProfileDataset -> "profile_dataset"
  | RunModelJob -> "run_model_job"

(** Convert workload-kind variants to Kubernetes-style strings. *)
let string_of_workload_kind = function
  | Deployment -> "Deployment"
  | Job -> "Job"
  | CronJob -> "CronJob"
  | Service -> "Service"
  | ConfigMap -> "ConfigMap"

(** Convert job-status variants to stable API strings. *)
let string_of_job_status = function
  | Queued -> "queued"
  | Running -> "running"
  | Completed -> "completed"
  | Failed -> "failed"
  | Cancelled -> "cancelled"

(** Convert packaging-mode variants to stable API strings. *)
let string_of_packaging_mode = function
  | InlineServiceCall -> "inline_service_call"
  | KubernetesJobExecution -> "kubernetes_job"
  | LongRunningDeployment -> "long_running_deployment"

(** Convert source-kind variants to stable API strings. *)
let string_of_source_kind = function
  | Inline -> "inline"
  | Postgres -> "postgres"
  | Mongodb -> "mongodb"
  | ObjectStorage -> "object_storage"

(** Convert quality-severity variants to stable API strings. *)
let string_of_quality_severity = function
  | Info -> "info"
  | Warning -> "warning"
  | Error -> "error"

(** Convert quality-operator variants to compact display/API strings. *)
let string_of_quality_operator = function
  | NonNull -> "non_null"
  | Unique -> "unique"
  | MinValue value -> "min_value:" ^ string_of_float value
  | MaxValue value -> "max_value:" ^ string_of_float value
  | RegexMatch pattern -> "regex_match:" ^ pattern
  | AllowedValues values -> "allowed_values:" ^ String.concat "," values

(** Convert partition strategies to compact display/API strings. *)
let string_of_partition_strategy = function
  | SinglePartition -> "single"
  | FixedSize size -> "fixed_size:" ^ string_of_int size
  | HashPartition (field, partitions) -> "hash:" ^ field ^ ":" ^ string_of_int partitions
  | TimeWindow (field, window) -> "time_window:" ^ field ^ ":" ^ window

(** Convert pipeline execution targets to stable API strings. *)
let string_of_execution_target = function
  | LocalProcess -> "local_process"
  | PythonService -> "python_service"
  | KubernetesJobTarget -> "kubernetes_job"

(** Parse stable API strings into extraction strategy variants. *)
let extraction_strategy_of_string = function
  | "tabular" -> Tabular
  | "time_series" -> TimeSeries
  | "text" -> Text
  | "hybrid" -> Hybrid
  | value -> invalid_arg ("Unknown extraction strategy: " ^ value)

(** Parse stable API strings into model-family variants. *)
let model_family_of_string = function
  | "autoencoder" -> Autoencoder
  | "variational_autoencoder" -> VariationalAutoencoder
  | "gru" -> Gru
  | "lstm" -> Lstm
  | "naive_bayes" -> NaiveBayes
  | "transformer" -> Transformer
  | "random_forest" -> RandomForest
  | "xgboost" -> Xgboost
  | "linear" -> Linear
  | "custom" -> Custom
  | value -> invalid_arg ("Unknown model family: " ^ value)

(** Parse stable API strings into pipeline-step variants. *)
let step_kind_of_string = function
  | "enrich_context" -> EnrichContext
  | "filter_items" -> FilterItems
  | "summarize_items" -> SummarizeItems
  | "project_fields" -> ProjectFields
  | "profile_dataset" -> ProfileDataset
  | "run_model_job" -> RunModelJob
  | value -> invalid_arg ("Unknown step kind: " ^ value)

(** Parse Kubernetes-style or lowercase strings into workload-kind variants. *)
let workload_kind_of_string = function
  | "Deployment" | "deployment" -> Deployment
  | "Job" | "job" -> Job
  | "CronJob" | "cronjob" | "cron_job" -> CronJob
  | "Service" | "service" -> Service
  | "ConfigMap" | "configmap" | "config_map" -> ConfigMap
  | value -> invalid_arg ("Unknown workload kind: " ^ value)

(** Parse stable API strings into source-kind variants. *)
let source_kind_of_string = function
  | "inline" -> Inline
  | "postgres" -> Postgres
  | "mongodb" -> Mongodb
  | "object_storage" -> ObjectStorage
  | value -> invalid_arg ("Unknown source kind: " ^ value)

(** Parse stable API strings into quality-severity variants. *)
let quality_severity_of_string = function
  | "info" -> Info
  | "warning" -> Warning
  | "error" -> Error
  | value -> invalid_arg ("Unknown quality severity: " ^ value)


(** Convert a sensitivity level to its stable JSON/API string. *)
let string_of_sensitivity = function
  | LowSensitivity -> "low"
  | MediumSensitivity -> "medium"
  | HighSensitivity -> "high"

(** Convert a trust level to its stable JSON/API string. *)
let string_of_trust_level = function
  | LowTrust -> "low"
  | ModerateTrust -> "moderate"
  | HighTrust -> "high"

(** Convert a request granularity to its stable JSON/API string. *)
let string_of_granularity = function
  | CellGrain -> "cell"
  | RowGrain -> "row"
  | ColumnGrain -> "column"
  | TableGrain -> "table"
  | ModelUpdateGrain -> "model_update"

(** Convert a guard boundary to its stable JSON/API string. *)
let string_of_guard_boundary = function
  | BeforeRead -> "before_read"
  | BeforeTrain -> "before_train"
  | BeforeSend -> "before_send"
  | BeforeRelease -> "before_release"

(** Convert a restriction strategy to its stable JSON/API string.

    Parameterized strategies keep their parameter in the string so an audit
    record shows the bound that was applied, not only the strategy name. *)
let string_of_restriction_strategy = function
  | AllowFull -> "allow_full"
  | Generalize precision -> Printf.sprintf "generalize:%d" precision
  | AddNoise scale -> Printf.sprintf "add_noise:%g" scale
  | ClipContribution bound -> Printf.sprintf "clip_contribution:%g" bound
  | AggregateOnly minimum -> Printf.sprintf "aggregate_only:%d" minimum
  | Redact -> "redact"
  | Refuse -> "refuse"

(** Convert a plan decision to its stable JSON/API string. *)
let string_of_plan_decision = function
  | PermitRequest -> "permit"
  | NarrowRequest -> "narrow"
  | DenyRequest -> "deny"

(** Convert a round phase to its stable JSON/API string. *)
let string_of_round_phase = function
  | AnalyticsRound -> "analytics"
  | EvaluationRound -> "evaluation"
  | TrainingRound -> "training"

(** Convert an aggregation method to its stable JSON/API string. *)
let string_of_aggregation_method = function
  | FedAvg -> "fedavg"
  | FedProx mu -> Printf.sprintf "fedprox:%g" mu
  | FedOpt optimizer -> "fedopt:" ^ optimizer
  | SecureAggregation threshold -> Printf.sprintf "secure_aggregation:%d" threshold

(** Convert a stop condition to its stable JSON/API string. *)
let string_of_stop_condition = function
  | SchemaFailure -> "schema_failure"
  | PrivacyBudgetExceeded -> "privacy_budget_exceeded"
  | UnsafeMetric -> "unsafe_metric"
  | QuorumNotMet -> "quorum_not_met"
  | ExcessiveDropout -> "excessive_dropout"

(** Convert a site participation state to its stable JSON/API string. *)
let string_of_site_participation = function
  | SiteAccepted -> "accepted"
  | SiteCompleted -> "completed"
  | SiteRejected -> "rejected"
  | SiteFailed -> "failed"
  | SiteDropped -> "dropped"

(** Convert a gate outcome to its stable JSON/API string. *)
let string_of_gate_outcome = function
  | GatePassed -> "passed"
  | GateFailed -> "failed"
  | GateNotEvaluated -> "not_evaluated"

(** Convert a release action to its stable JSON/API string. *)
let string_of_release_action = function
  | ReleaseCandidate -> "release"
  | RequireAnotherRound -> "another_round"
  | RejectCandidate -> "reject"

(** Convert a gate comparison to a display/API string. *)
let string_of_gate_comparison = function
  | AtLeast threshold -> Printf.sprintf "at_least:%g" threshold
  | AtMost threshold -> Printf.sprintf "at_most:%g" threshold
  | ImprovesOnBaseline margin -> Printf.sprintf "improves_on_baseline:%g" margin

(** Parse stable API strings into sensitivity variants. *)
let sensitivity_of_string = function
  | "low" -> LowSensitivity
  | "medium" -> MediumSensitivity
  | "high" -> HighSensitivity
  | value -> invalid_arg ("Unknown sensitivity: " ^ value)

(** Parse stable API strings into trust-level variants. *)
let trust_level_of_string = function
  | "low" -> LowTrust
  | "moderate" -> ModerateTrust
  | "high" -> HighTrust
  | value -> invalid_arg ("Unknown trust level: " ^ value)

(** Parse stable API strings into granularity variants. *)
let granularity_of_string = function
  | "cell" -> CellGrain
  | "row" -> RowGrain
  | "column" -> ColumnGrain
  | "table" -> TableGrain
  | "model_update" -> ModelUpdateGrain
  | value -> invalid_arg ("Unknown granularity: " ^ value)

(** Parse stable API strings into guard-boundary variants. *)
let guard_boundary_of_string = function
  | "before_read" -> BeforeRead
  | "before_train" -> BeforeTrain
  | "before_send" -> BeforeSend
  | "before_release" -> BeforeRelease
  | value -> invalid_arg ("Unknown guard boundary: " ^ value)

(** Parse stable API strings into round-phase variants. *)
let round_phase_of_string = function
  | "analytics" -> AnalyticsRound
  | "evaluation" -> EvaluationRound
  | "training" -> TrainingRound
  | value -> invalid_arg ("Unknown round phase: " ^ value)

(** Parse stable API strings into stop-condition variants. *)
let stop_condition_of_string = function
  | "schema_failure" -> SchemaFailure
  | "privacy_budget_exceeded" -> PrivacyBudgetExceeded
  | "unsafe_metric" -> UnsafeMetric
  | "quorum_not_met" -> QuorumNotMet
  | "excessive_dropout" -> ExcessiveDropout
  | value -> invalid_arg ("Unknown stop condition: " ^ value)

(** Parse stable API strings into site-participation variants. *)
let site_participation_of_string = function
  | "accepted" -> SiteAccepted
  | "completed" -> SiteCompleted
  | "rejected" -> SiteRejected
  | "failed" -> SiteFailed
  | "dropped" -> SiteDropped
  | value -> invalid_arg ("Unknown site participation: " ^ value)
