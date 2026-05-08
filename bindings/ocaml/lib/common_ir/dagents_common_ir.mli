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

(** Describes the shape of data extraction that a source run should use.

    [Tabular] covers row/column data, [TimeSeries] covers ordered metric or
    event streams, [Text] covers unstructured strings, and [Hybrid] lets a
    profile describe mixed inputs. *)
type extraction_strategy = Tabular | TimeSeries | Text | Hybrid

(** Describes whether a dataset profile belongs to one local source or to an
    assimilated multi-source view. *)
type scope_kind = Source | Assimilated

(** Sum type for the ML task requested from the router or model service.

    Each constructor maps to a model family list in the model router. *)
type task_type = AnomalyDetection | Classification | Forecasting | Embedding | Regression

(** Pipeline step categories supported by the generic pipeline executor. *)
type step_kind = EnrichContext | FilterItems | SummarizeItems | ProjectFields | ProfileDataset | RunModelJob

(** Supported model families in framework-level planning.

    These constructors are planning identifiers, not concrete Python class
    names. Service implementations can map them to their own runtime classes
    while preserving a stable typed contract here. *)
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

(** Sort instruction used by Mongo and source-selection adapters. *)
type selection_sort = { field : string; direction : string }

(** JSON-compatible scalar value used by compiler inputs and record batches.

    The value type is deliberately small: it captures the scalar shapes needed
    for schema inference, quality checks, and transformation planning without
    coupling the compiler to Yojson everywhere. *)
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

(** Opaque connection reference supplied by a product backend.

    Compilers validate that a connection exists for external sources, but they
    do not open sockets or resolve credentials. *)
type connection_ref = {
  connection_id : string;
  connection_options : (string * string) list;
}

(** Declarative Postgres read selection.

    Inputs: either [sql] or [table] plus optional projection, predicate, and
    ordering fields.
    Output usage: lowered into [extraction_plan] metadata before execution. *)
type postgres_selection = {
  sql : string option;
  table : string option;
  columns : string list;
  where_clause : string option;
  order_by : string list;
}

(** Declarative MongoDB read selection.

    Inputs: database, collection, optional filter/projection JSON, and sort
    instructions. Projection JSON is also used as a lightweight schema hint. *)
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

(** Connector-specific selection payload.

    This variant keeps each source family explicit, making invalid pairings
    detectable during validation, e.g. [source_kind = Postgres] with an
    [InlineSelection] is rejected by the dataset compiler. *)
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

    Inputs: connector kind, selection, schema hints, batching, checkpoint, and
    additional string options.
    Output usage: validation report, source metadata, and extraction plan.

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

(** Dataset input wrapper used by service boundaries that may accept inline
    records, a full source spec, or a reference to a previously registered
    source. *)
type dataset_input = {
  inline_records : record list;
  source : source_spec option;
  source_id_ref : string option;
}

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

(** Validation outcome for [source_spec].

    [errors] are blocking. [warnings] call out ambiguous but allowed input,
    such as an empty schema hint. *)
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

(** Full schema validation result.

    Output: separates missing fields, type mismatches, extra fields, and
    warnings so callers can decide which issues block execution. *)
type schema_validation_report = {
  schema_valid : bool;
  missing_fields : record_schema_field list;
  type_mismatches : schema_validation_issue list;
  extra_fields : record_schema_field list;
  schema_warnings : string list;
}

(** Data-quality predicate applied to one field.

    [MinValue] and [MaxValue] operate on numeric-compatible values,
    [RegexMatch] currently performs a dependency-light wildcard match, and
    [AllowedValues] compares normalized string forms. *)
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

(** Partitioning strategy selected for source extraction.

    [SinglePartition] runs as one read, [FixedSize] chunks records by batch
    size, [HashPartition] splits on a stable field, and [TimeWindow] splits by
    a timestamp field/window pair. *)
type partition_strategy =
  | SinglePartition
  | FixedSize of int
  | HashPartition of string * int
  | TimeWindow of string * string

(** Lowered, execution-ready source extraction plan.

    Inputs come from [source_spec]; output fields are normalized so the runtime
    executor does not need to understand every connector-specific selection
    shape. *)
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

(** Pure record transformation operation.

    These operations are intentionally simple and composable so transform plans
    can be compiled, inspected, tested, and then executed left-to-right. *)
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

(** Summary of a dataset used by routing and planning.

    Output: field groups, record counts, partition estimate, and suggested
    model families derived from data shape and requested extraction strategy. *)
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

(** Model-router decision for one dataset profile and task. *)
type route_plan = {
  task_type : task_type;
  selected_model : model_family;
  candidates : model_family list;
  packaging_mode : packaging_mode;
}

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

(** Default resource requests and limits for generated workload components.

    Output: conservative CPU/memory values suitable for local demos and tests. *)
val default_resources : resources

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
