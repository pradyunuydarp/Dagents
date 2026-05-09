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
  type workload_kind = Deployment | Job | CronJob | Service | ConfigMap

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
