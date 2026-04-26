open Dagents_common_ir

let fail message = invalid_arg message

let field name fields =
  match List.assoc_opt name fields with
  | Some value -> value
  | None -> fail ("Missing JSON field: " ^ name)

let string_field name fields =
  match field name fields with
  | `String value -> value
  | _ -> fail ("Expected string field: " ^ name)

let string_option_field name fields =
  match List.assoc_opt name fields with
  | Some (`String value) -> Some value
  | Some `Null | None -> None
  | _ -> fail ("Expected nullable string field: " ^ name)

let bool_field_with_default name default fields =
  match List.assoc_opt name fields with
  | Some (`Bool value) -> value
  | None -> default
  | _ -> fail ("Expected bool field: " ^ name)

let int_field_with_default name default fields =
  match List.assoc_opt name fields with
  | Some (`Int value) -> value
  | Some (`Intlit value) -> int_of_string value
  | None -> default
  | _ -> fail ("Expected int field: " ^ name)

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

let assoc_or_empty = function
  | `Assoc fields -> fields
  | `Null -> []
  | _ -> fail "Expected object"

let json_option_field name fields = List.assoc_opt name fields

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

let yojson_of_string_assoc values =
  `Assoc (List.map (fun (key, value) -> (key, `String value)) values)

let value_of_yojson = function
  | `String value -> VString value
  | `Int value -> VInt value
  | `Intlit value -> VInt (int_of_string value)
  | `Float value -> VFloat value
  | `Bool value -> VBool value
  | `Null -> VNull
  | `Assoc _ | `List _ -> fail "Expected scalar record value"

let yojson_of_value = function
  | VString value -> `String value
  | VInt value -> `Int value
  | VFloat value -> `Float value
  | VBool value -> `Bool value
  | VNull -> `Null

let record_of_yojson = function
  | `Assoc fields -> List.map (fun (field, value) -> (field, value_of_yojson value)) fields
  | _ -> fail "Expected record object"

let yojson_of_record record =
  `Assoc (List.map (fun (field, value) -> (field, yojson_of_value value)) record)

let records_of_yojson = function
  | `List values -> List.map record_of_yojson values
  | _ -> fail "Expected records list"

let yojson_of_records records = `List (List.map yojson_of_record records)

let schema_field_of_yojson = function
  | `Assoc fields -> { field_name = string_field "name" fields; dtype = string_field "type" fields }
  | _ -> fail "Expected schema field object"

let yojson_of_schema_field field =
  `Assoc [ ("name", `String field.field_name); ("type", `String field.dtype) ]

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

let connection_ref_of_yojson = function
  | `Assoc fields ->
      {
        connection_id = string_field "connectionId" fields;
        connection_options = string_assoc_field "options" fields;
      }
  | _ -> fail "Expected connectionRef object"

let selection_sort_of_yojson = function
  | `Assoc fields -> { field = string_field "field" fields; direction = string_field "direction" fields }
  | _ -> fail "Expected sort object"

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

let env_var_of_yojson = function
  | `Assoc fields -> { name = string_field "name" fields; value = string_field "value" fields }
  | _ -> fail "Expected env var object"

let yojson_of_env_var (env : env_var) =
  `Assoc [ ("name", `String env.name); ("value", `String env.value) ]

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

let yojson_of_port (port : port) =
  `Assoc [ ("name", `String port.port_name); ("containerPort", `Int port.container_port) ]

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

let yojson_of_resources resources =
  `Assoc
    [
      ("cpuRequest", `String resources.cpu_request);
      ("cpuLimit", `String resources.cpu_limit);
      ("memoryRequest", `String resources.memory_request);
      ("memoryLimit", `String resources.memory_limit);
    ]

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

let yojson_of_workload_plan plan =
  `Assoc
    [
      ("planId", `String plan.plan_id);
      ("namespace", `String plan.namespace);
      ("manifests", `List (List.map yojson_of_workload_manifest plan.manifests));
      ("combinedYaml", `String plan.combined_yaml);
    ]

let yojson_of_source_validation_result result =
  `Assoc
    [
      ("valid", `Bool result.valid);
      ("errors", `List (List.map (fun value -> `String value) result.errors));
      ("warnings", `List (List.map (fun value -> `String value) result.warnings));
    ]

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

let transform_operations_of_yojson = function
  | `List values -> List.map transform_operation_of_yojson values
  | _ -> fail "Expected transform operations list"

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

let yojson_of_schema_validation_report report =
  `Assoc
    [
      ("valid", `Bool report.schema_valid);
      ("missingFields", `List (List.map yojson_of_schema_field report.missing_fields));
      ("typeMismatches", `List (List.map yojson_of_schema_issue report.type_mismatches));
      ("extraFields", `List (List.map yojson_of_schema_field report.extra_fields));
      ("warnings", `List (List.map (fun value -> `String value) report.schema_warnings));
    ]

let yojson_of_quality_result result =
  `Assoc
    [
      ("ruleId", `String result.quality_rule_id);
      ("passed", `Bool result.passed);
      ("violations", `Int result.violations);
      ("message", `String result.quality_message);
    ]

let yojson_of_transform_plan plan =
  `Assoc
    [
      ("planId", `String plan.transform_plan_id);
      ("outputSchema", `List (List.map yojson_of_schema_field plan.output_schema));
    ]

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

let yojson_of_pipeline_step step =
  `Assoc
    [
      ("stepId", `String step.step_id);
      ("kind", `String (string_of_step_kind step.kind));
      ("dependsOn", `List (List.map (fun value -> `String value) step.depends_on));
      ("executionTarget", `String (string_of_execution_target step.execution_target));
      ("config", Option.value step.config_json ~default:`Null);
    ]

let yojson_of_pipeline pipeline =
  `Assoc
    [
      ("pipelineId", `String pipeline.pipeline_id);
      ("steps", `List (List.map yojson_of_pipeline_step pipeline.steps));
    ]

let yojson_of_model_family model = `String (string_of_model_family model)

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

let route_plan_to_yojson plan =
  `Assoc
    [
      ("selectedModel", `String (string_of_model_family plan.selected_model));
      ("candidates", `List (List.map yojson_of_model_family plan.candidates));
      ("packagingMode", `String (string_of_packaging_mode plan.packaging_mode));
    ]
