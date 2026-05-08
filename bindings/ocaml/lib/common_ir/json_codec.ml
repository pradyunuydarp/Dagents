(** JSON codec implementation for the Dagents shared IR.

    The codec sits at the boundary between untyped Yojson payloads and the
    strongly typed OCaml planner modules. Parsing functions fail early with
    explicit [Invalid_argument] messages so CLI commands and service adapters
    can report actionable input errors. Serialization functions keep the JSON
    contract stable for tests, demos, and backend integrations. *)

open Dagents_common_ir

(** Raise a uniform parser error.

    Input: human-readable message.
    Output: never returns; raises [Invalid_argument]. *)
let fail message = invalid_arg message

(** Read a required JSON object field from an association list. *)
let field name fields =
  match List.assoc_opt name fields with
  | Some value -> value
  | None -> fail ("Missing JSON field: " ^ name)

(** Read a required string field.

    Example test case:
    {[
      assert (string_field "id" [ ("id", `String "x") ] = "x")
    ]} *)
let string_field name fields =
  match field name fields with
  | `String value -> value
  | _ -> fail ("Expected string field: " ^ name)

(** Read an optional nullable string field. *)
let string_option_field name fields =
  match List.assoc_opt name fields with
  | Some (`String value) -> Some value
  | Some `Null | None -> None
  | _ -> fail ("Expected nullable string field: " ^ name)

(** Read an optional bool field, returning [default] when absent. *)
let bool_field_with_default name default fields =
  match List.assoc_opt name fields with
  | Some (`Bool value) -> value
  | None -> default
  | _ -> fail ("Expected bool field: " ^ name)

(** Read an optional integer field, supporting Yojson integer literals. *)
let int_field_with_default name default fields =
  match List.assoc_opt name fields with
  | Some (`Int value) -> value
  | Some (`Intlit value) -> int_of_string value
  | None -> default
  | _ -> fail ("Expected int field: " ^ name)

(** Read an optional string-list field, defaulting to an empty list. *)
let string_list_field name fields =
  match List.assoc_opt name fields with
  | Some (`List values) ->
      List.map
        (function
          | `String value -> value
          | _ -> fail ("Expected string list field: " ^ name))
        values
  | None -> []
  | _ -> fail ("Expected list field: " ^ name)

(** Convert a JSON object to fields, treating [null] as an empty object. *)
let assoc_or_empty = function
  | `Assoc fields -> fields
  | `Null -> []
  | _ -> fail "Expected object"

(** Read any optional JSON field without validating its inner shape. *)
let json_option_field name fields = List.assoc_opt name fields

(** Read an object whose values are normalized to strings.

    Non-string values are preserved by converting them to compact JSON strings,
    which keeps connector options flexible without expanding the IR. *)
let string_assoc_field name fields =
  match List.assoc_opt name fields with
  | Some (`Assoc values) ->
      List.map
        (function
          | key, `String value -> (key, value)
          | key, value -> (key, Yojson.Safe.to_string value))
        values
  | None -> []
  | _ -> fail ("Expected string object field: " ^ name)

(** Serialize a string association list as a JSON object. *)
let yojson_of_string_assoc values =
  `Assoc (List.map (fun (key, value) -> (key, `String value)) values)

(** Parse a JSON scalar into the Dagents [value] variant.

    Objects and arrays are rejected because record fields are scalar at this
    layer; nested data should be passed through explicit JSON config fields. *)
let value_of_yojson = function
  | `String value -> VString value
  | `Int value -> VInt value
  | `Intlit value -> VInt (int_of_string value)
  | `Float value -> VFloat value
  | `Bool value -> VBool value
  | `Null -> VNull
  | `Assoc _ | `List _ -> fail "Expected scalar record value"

(** Serialize a Dagents scalar [value] back to JSON. *)
let yojson_of_value = function
  | VString value -> `String value
  | VInt value -> `Int value
  | VFloat value -> `Float value
  | VBool value -> `Bool value
  | VNull -> `Null

(** Parse one JSON object into a Dagents record. *)
let record_of_yojson = function
  | `Assoc fields -> List.map (fun (field, value) -> (field, value_of_yojson value)) fields
  | _ -> fail "Expected record object"

(** Serialize one Dagents record into a JSON object. *)
let yojson_of_record record =
  `Assoc (List.map (fun (field, value) -> (field, yojson_of_value value)) record)

(** Parse a JSON list of record objects. *)
let records_of_yojson = function
  | `List values -> List.map record_of_yojson values
  | _ -> fail "Expected records list"

(** Serialize a record list. *)
let yojson_of_records records = `List (List.map yojson_of_record records)

(** Parse a schema field using external JSON names ["name"] and ["type"]. *)
let schema_field_of_yojson = function
  | `Assoc fields -> { field_name = string_field "name" fields; dtype = string_field "type" fields }
  | _ -> fail "Expected schema field object"

(** Serialize a schema field using external JSON names ["name"] and ["type"]. *)
let yojson_of_schema_field field =
  `Assoc [ ("name", `String field.field_name); ("type", `String field.dtype) ]

(** Parse source batching options with conservative defaults.

    Missing batching means [batch_size = 1000] and no max-record limit. *)
let source_batching_of_yojson = function
  | `Assoc fields ->
      {
        batch_size = int_field_with_default "batchSize" 1000 fields;
        max_records =
          (match List.assoc_opt "maxRecords" fields with
          | Some (`Int value) -> Some value
          | Some (`Intlit value) -> Some (int_of_string value)
          | Some `Null | None -> None
          | _ -> fail "Expected maxRecords integer");
      }
  | `Null -> { batch_size = 1000; max_records = None }
  | _ -> fail "Expected batching object"

(** Parse a connection reference for external source adapters. *)
let connection_ref_of_yojson = function
  | `Assoc fields ->
      {
        connection_id = string_field "connectionId" fields;
        connection_options = string_assoc_field "options" fields;
      }
  | _ -> fail "Expected connectionRef object"

(** Parse one source sort directive. *)
let selection_sort_of_yojson = function
  | `Assoc fields -> { field = string_field "field" fields; direction = string_field "direction" fields }
  | _ -> fail "Expected sort object"

(** Parse connector-specific source selection based on the declared source kind.

    Inputs:
    - [kind]: already-parsed source connector kind.
    - JSON value containing connector-specific selection fields.

    Output: the matching [source_selection] variant. *)
let source_selection_of_yojson kind = function
  | `Assoc fields -> (
      match kind with
      | Inline ->
          InlineSelection
            (match List.assoc_opt "records" fields with
            | Some records -> records_of_yojson records
            | None -> [])
      | Postgres ->
          PostgresSelection
            {
              sql = string_option_field "sql" fields;
              table = string_option_field "table" fields;
              columns = string_list_field "columns" fields;
              where_clause = string_option_field "where" fields;
              order_by = string_list_field "orderBy" fields;
            }
      | Mongodb ->
          MongoSelection
            {
              database = string_field "database" fields;
              collection = string_field "collection" fields;
              filter_json = List.assoc_opt "filter" fields;
              projection_json = List.assoc_opt "projection" fields;
              sort =
                (match List.assoc_opt "sort" fields with
                | Some (`List values) -> List.map selection_sort_of_yojson values
                | _ -> []);
            }
      | ObjectStorage ->
          ObjectStorageSelection
            {
              uri = string_option_field "uri" fields;
              prefix = string_option_field "prefix" fields;
              glob = string_option_field "glob" fields;
              compression = string_option_field "compression" fields;
            } )
  | value when kind = Inline -> InlineSelection (records_of_yojson value)
  | _ -> fail "Expected source selection object"

(** Parse a full source specification.

    Defaults are intentionally demo-friendly: missing [kind] becomes [Inline],
    missing [format] becomes ["json"], and missing batching uses 1000-record
    batches. Validation of semantic correctness happens in the dataset
    compiler. *)
let source_spec_of_yojson = function
  | `Assoc fields ->
      let source_kind =
        match List.assoc_opt "kind" fields with
        | Some (`String value) -> source_kind_of_string value
        | _ -> Inline
      in
      {
        source_id = string_field "sourceId" fields;
        source_kind;
        connection_ref =
          (match List.assoc_opt "connectionRef" fields with
          | Some `Null | None -> None
          | Some value -> Some (connection_ref_of_yojson value));
        selection =
          (match List.assoc_opt "selection" fields with
          | Some value -> source_selection_of_yojson source_kind value
          | None -> source_selection_of_yojson source_kind (`Assoc []));
        format =
          (match List.assoc_opt "format" fields with
          | Some (`String value) -> value
          | _ -> "json");
        schema_hint =
          (match List.assoc_opt "schemaHint" fields with
          | Some (`List values) ->
              List.map
                (fun value ->
                  let field = schema_field_of_yojson value in
                  (field.field_name, field.dtype))
                values
          | _ -> []);
        batching =
          (match List.assoc_opt "batching" fields with
          | Some value -> source_batching_of_yojson value
          | None -> { batch_size = 1000; max_records = None });
        checkpoint = List.assoc_opt "checkpoint" fields;
        options = string_assoc_field "options" fields;
      }
  | _ -> fail "Expected source spec object"

(** Parse one Kubernetes environment variable. *)
let env_var_of_yojson = function
  | `Assoc fields -> { name = string_field "name" fields; value = string_field "value" fields }
  | _ -> fail "Expected env var object"

(** Serialize one Kubernetes environment variable. *)
let yojson_of_env_var (env : env_var) =
  `Assoc [ ("name", `String env.name); ("value", `String env.value) ]

(** Parse one Kubernetes port declaration. *)
let port_of_yojson = function
  | `Assoc fields ->
      {
        port_name =
          (match List.assoc_opt "name" fields with
          | Some (`String value) -> value
          | _ -> "http");
        container_port = int_field_with_default "containerPort" 0 fields;
      }
  | _ -> fail "Expected port object"

(** Serialize one Kubernetes port declaration. *)
let yojson_of_port (port : port) =
  `Assoc [ ("name", `String port.port_name); ("containerPort", `Int port.container_port) ]

(** Parse Kubernetes resources, filling absent fields from [default_resources]. *)
let resources_of_yojson = function
  | `Assoc fields ->
      {
        cpu_request =
          (match List.assoc_opt "cpuRequest" fields with
          | Some (`String value) -> value
          | _ -> default_resources.cpu_request);
        cpu_limit =
          (match List.assoc_opt "cpuLimit" fields with
          | Some (`String value) -> value
          | _ -> default_resources.cpu_limit);
        memory_request =
          (match List.assoc_opt "memoryRequest" fields with
          | Some (`String value) -> value
          | _ -> default_resources.memory_request);
        memory_limit =
          (match List.assoc_opt "memoryLimit" fields with
          | Some (`String value) -> value
          | _ -> default_resources.memory_limit);
      }
  | `Null -> default_resources
  | _ -> fail "Expected resources object"

(** Serialize Kubernetes resource requests and limits. *)
let yojson_of_resources resources =
  `Assoc
    [
      ("cpuRequest", `String resources.cpu_request);
      ("cpuLimit", `String resources.cpu_limit);
      ("memoryRequest", `String resources.memory_request);
      ("memoryLimit", `String resources.memory_limit);
    ]

(** Parse one deployable workload component. *)
let workload_component_of_yojson = function
  | `Assoc fields ->
      {
        name = string_field "name" fields;
        image = string_field "image" fields;
        kind =
          (match List.assoc_opt "kind" fields with
          | Some (`String value) -> workload_kind_of_string value
          | _ -> Deployment);
        replicas = int_field_with_default "replicas" 1 fields;
        schedule = string_option_field "schedule" fields;
        env =
          (match List.assoc_opt "env" fields with
          | Some (`List values) -> List.map env_var_of_yojson values
          | _ -> []);
        ports =
          (match List.assoc_opt "ports" fields with
          | Some (`List values) -> List.map port_of_yojson values
          | _ -> []);
        args = string_list_field "args" fields;
        resources =
          (match List.assoc_opt "resources" fields with
          | Some value -> resources_of_yojson value
          | None -> default_resources);
      }
  | _ -> fail "Expected workload component object"

(** Parse a full workload spec for manifest compilation.

    Missing [planId] and [namespace] receive stable defaults so demo payloads
    can stay compact. *)
let workload_spec_of_yojson = function
  | `Assoc fields ->
      {
        plan_id =
          (match List.assoc_opt "planId" fields with
          | Some (`String value) -> value
          | _ -> "dagents-plan");
        namespace =
          (match List.assoc_opt "namespace" fields with
          | Some (`String value) -> value
          | _ -> "dagents");
        components =
          (match List.assoc_opt "components" fields with
          | Some (`List values) -> List.map workload_component_of_yojson values
          | _ -> []);
        include_services = bool_field_with_default "includeServices" true fields;
        include_config_maps = bool_field_with_default "includeConfigMaps" false fields;
      }
  | _ -> fail "Expected workload spec object"

(** Serialize one rendered workload manifest. *)
let yojson_of_workload_manifest manifest =
  `Assoc
    [
      ("componentName", `String manifest.component_name);
      ("kind", `String (string_of_workload_kind manifest.kind));
      ("deploymentYaml", `String manifest.deployment_yaml);
      ( "serviceYaml",
        match manifest.service_yaml with
        | Some value -> `String value
        | None -> `Null );
      ( "configMapYaml",
        match manifest.config_map_yaml with
        | Some value -> `String value
        | None -> `Null );
    ]

(** Serialize a full workload plan with combined YAML. *)
let yojson_of_workload_plan plan =
  `Assoc
    [
      ("planId", `String plan.plan_id);
      ("namespace", `String plan.namespace);
      ("manifests", `List (List.map yojson_of_workload_manifest plan.manifests));
      ("combinedYaml", `String plan.combined_yaml);
    ]

(** Serialize source validation diagnostics. *)
let yojson_of_source_validation_result result =
  `Assoc
    [
      ("valid", `Bool result.valid);
      ("errors", `List (List.map (fun value -> `String value) result.errors));
      ("warnings", `List (List.map (fun value -> `String value) result.warnings));
    ]

(** Serialize source metadata derived by the dataset compiler. *)
let yojson_of_source_metadata metadata =
  `Assoc
    [
      ("sourceId", `String metadata.source_id);
      ("kind", `String (string_of_source_kind metadata.source_kind));
      ("schema", `List (List.map yojson_of_schema_field metadata.schema));
      ( "estimatedRecords",
        match metadata.estimated_records with
        | Some value -> `Int value
        | None -> `Null );
    ]

(** Serialize a connector-neutral extraction plan. *)
let yojson_of_extraction_plan plan =
  `Assoc
    [
      ("sourceId", `String plan.extraction_source_id);
      ("kind", `String (string_of_source_kind plan.extraction_source_kind));
      ("format", `String plan.extraction_format);
      ("selectedFields", `List (List.map (fun value -> `String value) plan.selected_fields));
      ("predicates", `List (List.map (fun value -> `String value) plan.predicates));
      ("ordering", `List (List.map (fun value -> `String value) plan.ordering));
      ("partitionStrategy", `String (string_of_partition_strategy plan.partition_strategy));
      ("batchSize", `Int plan.extraction_batch_size);
      ( "maxRecords",
        match plan.extraction_max_records with
        | Some value -> `Int value
        | None -> `Null );
      ("checkpoint", Option.value plan.extraction_checkpoint ~default:`Null);
    ]

(** Parse a schema contract used by schema validation. *)
let schema_contract_of_yojson = function
  | `Assoc fields ->
      {
        required_fields =
          (match List.assoc_opt "requiredFields" fields with
          | Some (`List values) -> List.map schema_field_of_yojson values
          | _ -> []);
        optional_fields =
          (match List.assoc_opt "optionalFields" fields with
          | Some (`List values) -> List.map schema_field_of_yojson values
          | _ -> []);
        allow_extra_fields = bool_field_with_default "allowExtraFields" true fields;
      }
  | _ -> fail "Expected schema contract object"

(** Parse a quality operator from compact string or object forms.

    Object form is required for operators that carry parameters, such as
    [min_value], [regex_match], or [allowed_values]. *)
let quality_operator_of_yojson = function
  | `Assoc fields -> (
      match string_field "kind" fields with
      | "non_null" -> NonNull
      | "unique" -> Unique
      | "min_value" -> (
          match field "value" fields with
          | `Float value -> MinValue value
          | `Int value -> MinValue (float_of_int value)
          | _ -> fail "Expected numeric min_value")
      | "max_value" -> (
          match field "value" fields with
          | `Float value -> MaxValue value
          | `Int value -> MaxValue (float_of_int value)
          | _ -> fail "Expected numeric max_value")
      | "regex_match" -> RegexMatch (string_field "pattern" fields)
      | "allowed_values" -> AllowedValues (string_list_field "values" fields)
      | value -> fail ("Unknown quality operator: " ^ value) )
  | `String "non_null" -> NonNull
  | `String "unique" -> Unique
  | _ -> fail "Expected quality operator object"

(** Parse one quality rule. Missing severity defaults to [Error] so violations
    are blocking unless the caller explicitly relaxes them. *)
let quality_rule_of_yojson = function
  | `Assoc fields ->
      {
        rule_id = string_field "ruleId" fields;
        field = string_field "field" fields;
        operator = quality_operator_of_yojson (field "operator" fields);
        severity =
          (match List.assoc_opt "severity" fields with
          | Some (`String value) -> quality_severity_of_string value
          | _ -> Error);
      }
  | _ -> fail "Expected quality rule object"

(** Parse one transform operation from its ["kind"] discriminator. *)
let transform_operation_of_yojson = function
  | `Assoc fields -> (
      match string_field "kind" fields with
      | "select_fields" -> SelectFields (string_list_field "fields" fields)
      | "drop_fields" -> DropFields (string_list_field "fields" fields)
      | "rename_fields" -> RenameFields (string_assoc_field "mappings" fields)
      | "filter_non_null" -> FilterNonNull (string_list_field "fields" fields)
      | "cast_fields" -> CastFields (string_assoc_field "casts" fields)
      | value -> fail ("Unknown transform operation: " ^ value) )
  | _ -> fail "Expected transform operation object"

(** Parse a list of transform operations. *)
let transform_operations_of_yojson = function
  | `List values -> List.map transform_operation_of_yojson values
  | _ -> fail "Expected transform operations list"

(** Serialize one schema validation issue. *)
let yojson_of_schema_issue issue =
  `Assoc
    [
      ("field", `String issue.issue_field);
      ("expectedType", `String issue.expected_dtype);
      ( "actualType",
        match issue.actual_dtype with
        | Some value -> `String value
        | None -> `Null );
    ]

(** Serialize a schema validation report. *)
let yojson_of_schema_validation_report report =
  `Assoc
    [
      ("valid", `Bool report.schema_valid);
      ("missingFields", `List (List.map yojson_of_schema_field report.missing_fields));
      ("typeMismatches", `List (List.map yojson_of_schema_issue report.type_mismatches));
      ("extraFields", `List (List.map yojson_of_schema_field report.extra_fields));
      ("warnings", `List (List.map (fun value -> `String value) report.schema_warnings));
    ]

(** Serialize one quality rule result. *)
let yojson_of_quality_result result =
  `Assoc
    [
      ("ruleId", `String result.quality_rule_id);
      ("severity", `String (string_of_quality_severity result.quality_severity));
      ("passed", `Bool result.passed);
      ("violations", `Int result.violations);
      ("message", `String result.quality_message);
    ]

(** Serialize an aggregate quality report. *)
let yojson_of_quality_report report =
  `Assoc
    [
      ("results", `List (List.map yojson_of_quality_result report.quality_results));
      ("blocking", `Bool report.blocking);
      ("warningCount", `Int report.warning_count);
      ("errorCount", `Int report.error_count);
      ("totalViolations", `Int report.total_violations);
    ]

(** Serialize transform-plan metadata.

    The operation list is intentionally not echoed here; callers usually need
    the generated output schema for inspection. *)
let yojson_of_transform_plan plan =
  `Assoc
    [
      ("planId", `String plan.transform_plan_id);
      ("outputSchema", `List (List.map yojson_of_schema_field plan.output_schema));
    ]

(** Parse one pipeline step from JSON. *)
let pipeline_step_of_yojson = function
  | `Assoc fields ->
      ({
        step_id = string_field "stepId" fields;
        kind = step_kind_of_string (string_field "kind" fields);
        depends_on = string_list_field "dependsOn" fields;
        config_json = json_option_field "config" fields;
      }
        : pipeline_step)
  | _ -> fail "Expected pipeline step object"

(** Parse a pipeline definition from JSON. *)
let pipeline_definition_of_yojson json =
  match json with
  | `Assoc fields ->
      ({
        pipeline_id = string_field "pipelineId" fields;
        steps =
          (match List.assoc_opt "steps" fields with
          | Some (`List values) -> List.map pipeline_step_of_yojson values
          | _ -> []);
      }
        : pipeline_definition)
  | _ -> fail "Expected pipeline definition object"

(** Serialize one compiled pipeline step with execution target. *)
let yojson_of_pipeline_step step =
  `Assoc
    [
      ("stepId", `String step.step_id);
      ("kind", `String (string_of_step_kind step.kind));
      ("dependsOn", `List (List.map (fun value -> `String value) step.depends_on));
      ("executionTarget", `String (string_of_execution_target step.execution_target));
      ("config", Option.value step.config_json ~default:`Null);
    ]

(** Serialize a compiled pipeline. *)
let yojson_of_pipeline pipeline =
  `Assoc
    [
      ("pipelineId", `String pipeline.pipeline_id);
      ("steps", `List (List.map yojson_of_pipeline_step pipeline.steps));
    ]

(** Serialize a model-family variant as a stable string. *)
let yojson_of_model_family model = `String (string_of_model_family model)

(** Serialize a dataset profile produced by the dataset compiler. *)
let dataset_profile_to_yojson profile =
  `Assoc
    [
      ("scopeId", `String profile.scope_id);
      ("scopeKind", `String (string_of_scope_kind profile.scope_kind));
      ("extractionStrategy", `String (string_of_extraction_strategy profile.extraction_strategy));
      ("recordCount", `Int profile.record_count);
      ("featureFields", `List (List.map (fun value -> `String value) profile.feature_fields));
      ( "labelField",
        match profile.label_field with
        | Some value -> `String value
        | None -> `Null );
      ("numericFields", `List (List.map (fun value -> `String value) profile.numeric_fields));
      ( "categoricalFields",
        `List (List.map (fun value -> `String value) profile.categorical_fields) );
      ("partitionCount", `Int profile.partition_count);
      ("suggestedModels", `List (List.map yojson_of_model_family profile.suggested_models));
    ]

(** Serialize a route plan produced by the model router. *)
let route_plan_to_yojson plan =
  `Assoc
    [
      ("selectedModel", `String (string_of_model_family plan.selected_model));
      ("candidates", `List (List.map yojson_of_model_family plan.candidates));
      ("packagingMode", `String (string_of_packaging_mode plan.packaging_mode));
    ]
