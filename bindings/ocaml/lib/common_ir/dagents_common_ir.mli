type extraction_strategy = Tabular | TimeSeries | Text | Hybrid
type scope_kind = Source | Assimilated
type task_type = AnomalyDetection | Classification | Forecasting | Embedding | Regression
type step_kind = EnrichContext | FilterItems | SummarizeItems | ProjectFields | ProfileDataset | RunModelJob
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

type packaging_mode = InlineServiceCall | KubernetesJobExecution | LongRunningDeployment
type source_kind = Inline | Postgres | Mongodb | ObjectStorage
type job_status = Queued | Running | Completed | Failed | Cancelled
type workload_kind = Deployment | Job | CronJob | Service | ConfigMap
type pipeline_execution_target = LocalProcess | PythonService | KubernetesJobTarget
type selection_sort = { field : string; direction : string }

type value =
  | VString of string
  | VInt of int
  | VFloat of float
  | VBool of bool
  | VNull

type record = (string * value) list

type connection_ref = {
  connection_id : string;
  connection_options : (string * string) list;
}

type postgres_selection = {
  sql : string option;
  table : string option;
  columns : string list;
  where_clause : string option;
  order_by : string list;
}

type mongo_selection = {
  database : string;
  collection : string;
  filter_json : Yojson.Safe.t option;
  projection_json : Yojson.Safe.t option;
  sort : selection_sort list;
}

type object_storage_selection = {
  uri : string option;
  prefix : string option;
  glob : string option;
  compression : string option;
}

type source_selection =
  | InlineSelection of record list
  | PostgresSelection of postgres_selection
  | MongoSelection of mongo_selection
  | ObjectStorageSelection of object_storage_selection

type source_batching = {
  batch_size : int;
  max_records : int option;
}

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

type dataset_input = {
  inline_records : record list;
  source : source_spec option;
  source_id_ref : string option;
}

type record_schema_field = {
  field_name : string;
  dtype : string;
}

type record_batch_stats = {
  record_count : int;
  truncated : bool;
}

type record_batch = {
  records : record list;
  schema : record_schema_field list;
  next_checkpoint : Yojson.Safe.t option;
  stats : record_batch_stats;
}

type source_validation_result = {
  valid : bool;
  errors : string list;
  warnings : string list;
}

type source_metadata = {
  source_id : string;
  source_kind : source_kind;
  schema : record_schema_field list;
  estimated_records : int option;
}

type schema_contract = {
  required_fields : record_schema_field list;
  optional_fields : record_schema_field list;
  allow_extra_fields : bool;
}

type schema_validation_issue = {
  issue_field : string;
  expected_dtype : string;
  actual_dtype : string option;
}

type schema_validation_report = {
  schema_valid : bool;
  missing_fields : record_schema_field list;
  type_mismatches : schema_validation_issue list;
  extra_fields : record_schema_field list;
  schema_warnings : string list;
}

type quality_operator =
  | NonNull
  | Unique
  | MinValue of float
  | MaxValue of float
  | RegexMatch of string
  | AllowedValues of string list

type quality_severity = Info | Warning | Error

type quality_rule = {
  rule_id : string;
  field : string;
  operator : quality_operator;
  severity : quality_severity;
}

type quality_result = {
  quality_rule_id : string;
  passed : bool;
  violations : int;
  quality_message : string;
}

type partition_strategy =
  | SinglePartition
  | FixedSize of int
  | HashPartition of string * int
  | TimeWindow of string * string

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

type transform_operation =
  | SelectFields of string list
  | DropFields of string list
  | RenameFields of (string * string) list
  | FilterNonNull of string list
  | CastFields of (string * string) list

type transform_plan = {
  transform_plan_id : string;
  operations : transform_operation list;
  output_schema : record_schema_field list;
}

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

type job_handle = {
  job_id : string;
  status : job_status;
  submitted_at : int;
  started_at : int option;
  completed_at : int option;
}

type model_job = {
  job : job_handle;
  job_type : string;
  result : Yojson.Safe.t option;
  error : string option;
}

type pipeline_run = {
  job : job_handle;
  pipeline_id : string;
  result : Yojson.Safe.t option;
  error : string option;
}

type error_envelope = {
  code : string;
  message : string;
  details : Yojson.Safe.t option;
  request_id : string option;
}

type 'a page_response = {
  items : 'a list;
  next_cursor : string option;
  total : int option;
}

type pipeline_step = {
  step_id : string;
  kind : step_kind;
  depends_on : string list;
  config_json : Yojson.Safe.t option;
}

type pipeline_definition = {
  pipeline_id : string;
  steps : pipeline_step list;
}

type compiled_pipeline_step = {
  step_id : string;
  kind : step_kind;
  depends_on : string list;
  execution_target : pipeline_execution_target;
  config_json : Yojson.Safe.t option;
}

type compiled_pipeline = {
  pipeline_id : string;
  steps : compiled_pipeline_step list;
}

type route_plan = {
  task_type : task_type;
  selected_model : model_family;
  candidates : model_family list;
  packaging_mode : packaging_mode;
}

type env_var = {
  name : string;
  value : string;
}

type port = {
  port_name : string;
  container_port : int;
}

type resources = {
  cpu_request : string;
  cpu_limit : string;
  memory_request : string;
  memory_limit : string;
}

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

type workload_spec = {
  plan_id : string;
  namespace : string;
  components : workload_component list;
  include_services : bool;
  include_config_maps : bool;
}

type workload_manifest = {
  component_name : string;
  kind : workload_kind;
  deployment_yaml : string;
  service_yaml : string option;
  config_map_yaml : string option;
}

type workload_plan = {
  plan_id : string;
  namespace : string;
  manifests : workload_manifest list;
  combined_yaml : string;
}

val default_resources : resources
val string_of_extraction_strategy : extraction_strategy -> string
val string_of_scope_kind : scope_kind -> string
val string_of_model_family : model_family -> string
val string_of_step_kind : step_kind -> string
val string_of_workload_kind : workload_kind -> string
val string_of_job_status : job_status -> string
val string_of_packaging_mode : packaging_mode -> string
val string_of_source_kind : source_kind -> string
val string_of_execution_target : pipeline_execution_target -> string
val string_of_quality_severity : quality_severity -> string
val string_of_quality_operator : quality_operator -> string
val string_of_partition_strategy : partition_strategy -> string
val extraction_strategy_of_string : string -> extraction_strategy
val model_family_of_string : string -> model_family
val step_kind_of_string : string -> step_kind
val workload_kind_of_string : string -> workload_kind
val source_kind_of_string : string -> source_kind
val quality_severity_of_string : string -> quality_severity
