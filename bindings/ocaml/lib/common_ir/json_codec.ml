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

(** Read an optional integer field, returning [None] when absent or null. *)
let int_option_field name fields =
  match List.assoc_opt name fields with
  | Some (`Int value) -> Some value
  | Some (`Intlit value) -> Some (int_of_string value)
  | Some `Null | None -> None
  | _ -> fail ("Expected nullable int field: " ^ name)

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
let value_of_yojson (json : Yojson.Safe.t) =
  match json with
  | `String value -> VString value
  | `Int value -> VInt value
  | `Intlit value -> VInt (int_of_string value)
  | `Float value -> VFloat value
  | `Bool value -> VBool value
  | `Null -> VNull
  | _ -> fail "Expected scalar record value"

(** Serialize a Dagents scalar [value] back to JSON. *)
let yojson_of_value = function
  | VString value -> `String value
  | VInt value -> `Int value
  | VFloat value -> `Float value
  | VBool value -> `Bool value
  | VNull -> `Null

(** Parse one JSON object into a Dagents record. *)
let record_of_yojson (json : Yojson.Safe.t) =
  match json with
  | `Assoc fields -> List.map (fun (field, value) -> (field, value_of_yojson value)) fields
  | _ -> fail "Expected record object"

(** Serialize one Dagents record into a JSON object. *)
let yojson_of_record record =
  `Assoc (List.map (fun (field, value) -> (field, yojson_of_value value)) record)

(** Parse a JSON list of record objects. *)
let records_of_yojson (json : Yojson.Safe.t) =
  match json with
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
let source_selection_of_yojson kind (json : Yojson.Safe.t) =
  match json with
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

(** Parse one container port declaration.

    A port without [hostPort], or with [hostPort: null], is not published.
    [hostIp] is read only for a published port and defaults to the loopback
    address, so a published port is not reachable from other machines unless
    the caller asks for it. *)
let port_of_yojson = function
  | `Assoc fields ->
      {
        port_name =
          (match List.assoc_opt "name" fields with
          | Some (`String value) -> value
          | _ -> "http");
        container_port = int_field_with_default "containerPort" 0 fields;
        publication =
          (match int_option_field "hostPort" fields with
          | None -> NotPublished
          | Some host_port ->
              let host_ip = Option.value (string_option_field "hostIp" fields) ~default:"127.0.0.1" in
              PublishedOn { host_ip; host_port });
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

(** Parse a Compose health check.

    The object takes exactly one of [command] (run without a shell) and
    [shell] (run by the container's shell); a [null] value counts as absent.
    Absent timing keys take the values the repository's Dockerfiles use. *)
let health_check_of_yojson = function
  | `Assoc fields ->
      let command =
        match List.assoc_opt "command" fields with
        | Some `Null | None -> None
        | Some _ -> Some (string_list_field "command" fields)
      in
      let health_test =
        match (command, string_option_field "shell" fields) with
        | Some parts, None -> ExecTest parts
        | None, Some line -> ShellTest line
        | Some _, Some _ | None, None -> fail "Expected exactly one of healthCheck.command and healthCheck.shell"
      in
      {
        health_test;
        interval_seconds = int_field_with_default "intervalSeconds" 30 fields;
        timeout_seconds = int_field_with_default "timeoutSeconds" 5 fields;
        retries = int_field_with_default "retries" 3 fields;
        start_period_seconds = int_field_with_default "startPeriodSeconds" 10 fields;
      }
  | _ -> fail "Expected health check object"

(** Parse one Compose [depends_on] entry. An absent or [null] condition means
    [service_started]. *)
let service_dependency_of_yojson = function
  | `Assoc fields ->
      {
        dependency_service = string_field "service" fields;
        dependency_condition =
          (match string_option_field "condition" fields with
          | Some word -> dependency_condition_of_string word
          | None -> ServiceStarted);
      }
  | _ -> fail "Expected dependency object"

(** Parse a deployment target.

    Absent, [null], ["kubernetes"] and [{"kind": "kubernetes"}] mean
    Kubernetes. ["compose"] means {!default_compose_target}. The object form
    [{"kind": "compose", ...}] reads [placement] (["own"] or ["merge"]),
    [network], [externalNetwork] and [existingServices], each with the default
    of {!default_compose_target}. *)
let deployment_target_of_yojson (json : Yojson.Safe.t) =
  let compose fields =
    let placement =
      match string_option_field "placement" fields with
      | None | Some "own" -> OwnProject
      | Some "merge" -> MergedProject
      | Some value -> fail ("Unknown compose placement: " ^ value)
    in
    let network_name = Option.value (string_option_field "network" fields) ~default:"default" in
    Compose
      {
        compose_placement = placement;
        compose_network =
          (if bool_field_with_default "externalNetwork" false fields then ExternalNetwork network_name
           else ProjectNetwork network_name);
        existing_services = string_list_field "existingServices" fields;
      }
  in
  match json with
  | `Null | `String "kubernetes" -> Kubernetes
  | `String "compose" -> compose []
  | `String value -> fail ("Unknown deployment target: " ^ value)
  | `Assoc fields -> (
      match string_field "kind" fields with
      | "kubernetes" -> Kubernetes
      | "compose" -> compose fields
      | value -> fail ("Unknown deployment target: " ^ value))
  | _ -> fail "Expected deployment target"

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
        generated_resources =
          List.map workload_kind_of_string (string_list_field "generatedResources" fields);
        service_account_name = string_option_field "serviceAccountName" fields;
        service_type =
          (match string_option_field "serviceType" fields with
          | Some value -> value
          | None -> "ClusterIP");
        config_map_data = string_assoc_field "configMapData" fields;
        env_files = string_list_field "envFiles" fields;
        health_check =
          (match List.assoc_opt "healthCheck" fields with
          | Some `Null | None -> None
          | Some value -> Some (health_check_of_yojson value));
        service_dependencies =
          (match List.assoc_opt "dependsOn" fields with
          | Some (`List values) -> List.map service_dependency_of_yojson values
          | Some `Null | None -> []
          | Some _ -> fail "Expected list field: dependsOn");
      }
  | _ -> fail "Expected workload component object"

(** Raise unless every string in [json], object keys included, is valid
    UTF-8. [field] names the nearest enclosing key, for the message.

    JSON text is UTF-8, but Yojson copies bytes that are not, and it reads an
    escaped lone low surrogate such as [\udc80] as bytes that are not. A
    Compose file written from such a string is not UTF-8, and Docker Compose
    refuses it. *)
let rec require_utf_8 field (json : Yojson.Safe.t) =
  let refuse () = fail ("Expected UTF-8 text in field: " ^ field) in
  match json with
  | `String value -> if not (String.is_valid_utf_8 value) then refuse ()
  | `Assoc fields ->
      List.iter
        (fun (key, value) ->
          if not (String.is_valid_utf_8 key) then refuse ();
          require_utf_8 key value)
        fields
  | `List values -> List.iter (require_utf_8 field) values
  | _ -> ()

(** Parse a full workload spec for manifest compilation.

    Missing [planId] and [namespace] receive stable defaults so demo payloads
    can stay compact. A missing [target] means Kubernetes, so specs written
    before the Compose target keep their meaning. A Compose spec with a string
    that is not UTF-8 is refused. *)
let workload_spec_of_yojson json =
  match json with
  | `Assoc fields ->
      let spec =
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
          target =
            (match List.assoc_opt "target" fields with
            | Some value -> deployment_target_of_yojson value
            | None -> Kubernetes);
        }
      in
      (match spec.target with Compose _ -> require_utf_8 "spec" json | Kubernetes -> ());
      spec
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
      ( "serviceAccountYaml",
        match manifest.service_account_yaml with
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

(* ---------------------------------------------------------------------------
   Governance and federation codecs.

   These follow the same conventions as the codecs above: camelCase JSON field
   names on the wire, [Invalid_argument] with a field-specific message on bad
   input, and no silent defaulting for anything a governance decision depends
   on. Where a default is safe it is the conservative one, so a field omitted
   by a careless caller cannot widen what the planner permits.
   --------------------------------------------------------------------------- *)

(** Read a float field that also accepts JSON integers. *)
let float_field_with_default name default fields =
  match List.assoc_opt name fields with
  | Some (`Float value) -> value
  | Some (`Int value) -> float_of_int value
  | Some (`Intlit value) -> float_of_string value
  | Some `Null | None -> default
  | _ -> fail ("Expected float field: " ^ name)

(** Read an optional float field, accepting JSON integers. *)
let float_option_field name fields =
  match List.assoc_opt name fields with
  | Some (`Float value) -> Some value
  | Some (`Int value) -> Some (float_of_int value)
  | Some (`Intlit value) -> Some (float_of_string value)
  | Some `Null | None -> None
  | _ -> fail ("Expected nullable float field: " ^ name)

(** Read an object of numeric values, such as a metric bundle. *)
let float_assoc_field name fields =
  match List.assoc_opt name fields with
  | Some (`Assoc values) ->
      List.map
        (function
          | key, `Float value -> (key, value)
          | key, `Int value -> (key, float_of_int value)
          | key, `Intlit value -> (key, float_of_string value)
          | key, _ -> fail ("Expected numeric value for " ^ name ^ "." ^ key))
        values
  | Some `Null | None -> []
  | _ -> fail ("Expected numeric object field: " ^ name)

(** Serialize a string-keyed numeric map. *)
let yojson_of_float_assoc values = `Assoc (List.map (fun (key, value) -> (key, `Float value)) values)

(** Serialize a string list. *)
let yojson_of_string_list values = `List (List.map (fun value -> `String value) values)

(** Serialize an optional string as a nullable JSON value. *)
let yojson_of_string_option = function Some value -> `String value | None -> `Null

(** Parse one Know-Your-User attribute. *)
let kyu_attribute_of_yojson (json : Yojson.Safe.t) =
  match json with
  | `Assoc fields ->
      {
        attribute_id = string_field "attributeId" fields;
        attribute_weight = float_field_with_default "weight" 1.0 fields;
        (* Unverified is the safe default: a caller that forgets the flag must
           not have its requester treated as checked. *)
        attribute_verified = bool_field_with_default "verified" false fields;
      }
  | _ -> fail "Expected KYU attribute object"

(** Serialize one Know-Your-User attribute. *)
let yojson_of_kyu_attribute attribute =
  `Assoc
    [
      ("attributeId", `String attribute.attribute_id);
      ("weight", `Float attribute.attribute_weight);
      ("verified", `Bool attribute.attribute_verified);
    ]

(** Parse the party making a governance request. *)
let requester_of_yojson (json : Yojson.Safe.t) =
  match json with
  | `Assoc fields ->
      {
        requester_id = string_field "requesterId" fields;
        requester_kind = (match string_option_field "requesterKind" fields with Some v -> v | None -> "service");
        affiliation = string_option_field "affiliation" fields;
        stated_purpose = string_option_field "statedPurpose" fields;
        attributes =
          (match List.assoc_opt "attributes" fields with
          | Some (`List values) -> List.map kyu_attribute_of_yojson values
          | Some `Null | None -> []
          | _ -> fail "Expected attributes list");
        compliance_history = float_field_with_default "complianceHistory" 0.0 fields;
      }
  | _ -> fail "Expected requester object"

(** Serialize a computed trust assessment. *)
let yojson_of_kyu_assessment assessment =
  `Assoc
    [
      ("requesterId", `String assessment.assessed_requester_id);
      ("kyuScore", `Float assessment.kyu_score);
      ("trust", `String (string_of_trust_level assessment.trust));
      ("rationale", yojson_of_string_list assessment.trust_rationale);
    ]

(** Parse a data classification, the data-side half of a restriction decision. *)
let data_classification_of_yojson (json : Yojson.Safe.t) =
  match json with
  | `Assoc fields ->
      {
        classification_id = string_field "classificationId" fields;
        field_sensitivity =
          (match List.assoc_opt "fieldSensitivity" fields with
          | Some (`Assoc values) ->
              List.map
                (function
                  | key, `String value -> (key, sensitivity_of_string value)
                  | key, _ -> fail ("Expected sensitivity string for field " ^ key))
                values
          | Some `Null | None -> []
          | _ -> fail "Expected fieldSensitivity object");
        (* High is the safe default: an unclassified field is treated as the
           most protected until someone classifies it. *)
        default_sensitivity =
          (match string_option_field "defaultSensitivity" fields with
          | Some value -> sensitivity_of_string value
          | None -> HighSensitivity);
        regulations = string_list_field "regulations" fields;
        minimum_cohort = int_field_with_default "minimumCohort" 0 fields;
      }
  | _ -> fail "Expected data classification object"

(** Parse a restriction request for the Ethical-Restriction Rails. *)
let restriction_request_of_yojson (json : Yojson.Safe.t) =
  match json with
  | `Assoc fields ->
      {
        request_id = string_field "requestId" fields;
        boundary = guard_boundary_of_string (string_field "boundary" fields);
        requester = requester_of_yojson (field "requester" fields);
        classification = data_classification_of_yojson (field "classification" fields);
        requested_fields = string_list_field "requestedFields" fields;
        granularity = granularity_of_string (string_field "granularity" fields);
        cohort_size = int_option_field "cohortSize" fields;
        declared_purpose = string_option_field "declaredPurpose" fields;
        approved_purposes = string_list_field "approvedPurposes" fields;
      }
  | _ -> fail "Expected restriction request object"

(** Serialize one field-level restriction. *)
let yojson_of_field_restriction restriction =
  `Assoc
    [
      ("field", `String restriction.restricted_field);
      ("sensitivity", `String (string_of_sensitivity restriction.restricted_sensitivity));
      ("strategy", `String (string_of_restriction_strategy restriction.strategy));
      ("reason", `String restriction.restriction_reason);
    ]

(** Serialize a compiled restriction plan. *)
let yojson_of_restriction_plan plan =
  `Assoc
    [
      ("requestId", `String plan.plan_request_id);
      ("boundary", `String (string_of_guard_boundary plan.plan_boundary));
      ("assessment", yojson_of_kyu_assessment plan.assessment);
      ("granularity", `String (string_of_granularity plan.plan_granularity));
      ("fieldRestrictions", `List (List.map yojson_of_field_restriction plan.field_restrictions));
      ("decision", `String (string_of_plan_decision plan.decision));
      ("filteringScore", `Float plan.filtering_score);
      ("obligations", yojson_of_string_list plan.obligations);
      ("violations", yojson_of_string_list plan.plan_violations);
    ]

(** Parse an aggregation method, accepting either a bare name or a parameterized
    object such as [{ "kind": "secure_aggregation", "threshold": 3 }]. *)
let aggregation_method_of_yojson (json : Yojson.Safe.t) =
  match json with
  | `String "fedavg" -> FedAvg
  | `String "fedprox" -> FedProx 0.01
  | `String "fedopt" -> FedOpt "adam"
  | `String value -> fail ("Unknown aggregation method: " ^ value)
  | `Assoc fields -> (
      match string_field "kind" fields with
      | "fedavg" -> FedAvg
      | "fedprox" -> FedProx (float_field_with_default "mu" 0.01 fields)
      | "fedopt" -> FedOpt (match string_option_field "optimizer" fields with Some v -> v | None -> "adam")
      | "secure_aggregation" -> SecureAggregation (int_field_with_default "threshold" 3 fields)
      | value -> fail ("Unknown aggregation method: " ^ value))
  | _ -> fail "Expected aggregation method"

(** Serialize an aggregation method as a parameterized object. *)
let yojson_of_aggregation_method = function
  | FedAvg -> `Assoc [ ("kind", `String "fedavg") ]
  | FedProx mu -> `Assoc [ ("kind", `String "fedprox"); ("mu", `Float mu) ]
  | FedOpt optimizer -> `Assoc [ ("kind", `String "fedopt"); ("optimizer", `String optimizer) ]
  | SecureAggregation threshold ->
      `Assoc [ ("kind", `String "secure_aggregation"); ("threshold", `Int threshold) ]

(** Parse one site's standing enrolment in a study. *)
let site_registration_of_yojson (json : Yojson.Safe.t) =
  match json with
  | `Assoc fields ->
      {
        site_id = string_field "siteId" fields;
        site_capabilities = string_list_field "capabilities" fields;
        site_feature_contract_version = string_field "featureContractVersion" fields;
        approved_conditions = string_list_field "approvedConditions" fields;
        site_policy_version =
          (match string_option_field "policyVersion" fields with Some v -> v | None -> "unversioned");
        site_cohort_size = int_field_with_default "cohortSize" 0 fields;
        (* Not enrolled is the safe default: participation must be stated. *)
        site_enrolled = bool_field_with_default "enrolled" false fields;
      }
  | _ -> fail "Expected site registration object"

(** Serialize one site registration. *)
let yojson_of_site_registration registration =
  `Assoc
    [
      ("siteId", `String registration.site_id);
      ("capabilities", yojson_of_string_list registration.site_capabilities);
      ("featureContractVersion", `String registration.site_feature_contract_version);
      ("approvedConditions", yojson_of_string_list registration.approved_conditions);
      ("policyVersion", `String registration.site_policy_version);
      ("cohortSize", `Int registration.site_cohort_size);
      ("enrolled", `Bool registration.site_enrolled);
    ]

(** Parse a federated round manifest. *)
let round_manifest_of_yojson (json : Yojson.Safe.t) =
  match json with
  | `Assoc fields ->
      {
        round_id = string_field "roundId" fields;
        study_id = string_field "studyId" fields;
        condition_id = string_field "conditionId" fields;
        (* Analytics is the safe default phase: it moves the least. *)
        phase =
          (match string_option_field "phase" fields with
          | Some value -> round_phase_of_string value
          | None -> AnalyticsRound);
        model_version = string_field "modelVersion" fields;
        model_artifact_digest =
          (match string_option_field "modelArtifactDigest" fields with Some v -> v | None -> "");
        training_code_digest =
          (match string_option_field "trainingCodeDigest" fields with Some v -> v | None -> "");
        feature_contract_version = string_field "featureContractVersion" fields;
        privacy_profile =
          (match string_option_field "privacyProfile" fields with Some v -> v | None -> "default");
        aggregation =
          (match List.assoc_opt "aggregation" fields with
          | Some value -> aggregation_method_of_yojson value
          | None -> FedAvg);
        minimum_participants = int_field_with_default "minimumParticipants" 3 fields;
        minimum_cohort_per_site = int_field_with_default "minimumCohortPerSite" 0 fields;
        required_capabilities = string_list_field "requiredCapabilities" fields;
        stop_conditions =
          List.map stop_condition_of_string (string_list_field "stopConditions" fields);
        invited_sites = string_list_field "invitedSites" fields;
      }
  | _ -> fail "Expected round manifest object"

(** Serialize a round manifest back to its wire shape. *)
let yojson_of_round_manifest manifest =
  `Assoc
    [
      ("roundId", `String manifest.round_id);
      ("studyId", `String manifest.study_id);
      ("conditionId", `String manifest.condition_id);
      ("phase", `String (string_of_round_phase manifest.phase));
      ("modelVersion", `String manifest.model_version);
      ("modelArtifactDigest", `String manifest.model_artifact_digest);
      ("trainingCodeDigest", `String manifest.training_code_digest);
      ("featureContractVersion", `String manifest.feature_contract_version);
      ("privacyProfile", `String manifest.privacy_profile);
      ("aggregation", yojson_of_aggregation_method manifest.aggregation);
      ("minimumParticipants", `Int manifest.minimum_participants);
      ("minimumCohortPerSite", `Int manifest.minimum_cohort_per_site);
      ("requiredCapabilities", yojson_of_string_list manifest.required_capabilities);
      ( "stopConditions",
        yojson_of_string_list (List.map string_of_stop_condition manifest.stop_conditions) );
      ("invitedSites", yojson_of_string_list manifest.invited_sites);
    ]

(** Serialize a compiled round plan. *)
let yojson_of_round_plan plan =
  `Assoc
    [
      ("roundId", `String plan.plan_round_id);
      ("studyId", `String plan.plan_study_id);
      ("phase", `String (string_of_round_phase plan.plan_phase));
      ("selectedSites", yojson_of_string_list plan.selected_sites);
      ( "excludedSites",
        `List
          (List.map
             (fun exclusion ->
               `Assoc
                 [
                   ("siteId", `String exclusion.excluded_site_id);
                   ("reason", `String exclusion.exclusion_reason);
                 ])
             plan.excluded_sites) );
      ("quorumMet", `Bool plan.quorum_met);
      ("requiredParticipants", `Int plan.required_participants);
      ("aggregation", yojson_of_aggregation_method plan.plan_aggregation);
      ( "stopReason",
        match plan.plan_stop_reason with
        | Some reason -> `String (string_of_stop_condition reason)
        | None -> `Null );
      ("roundDigest", `String plan.round_digest);
    ]

(** Parse one site's returned round result. *)
let site_result_of_yojson (json : Yojson.Safe.t) =
  match json with
  | `Assoc fields ->
      {
        result_round_id = string_field "roundId" fields;
        result_site_id = string_field "siteId" fields;
        result_job_digest = (match string_option_field "jobDigest" fields with Some v -> v | None -> "");
        participation = site_participation_of_string (string_field "participation" fields);
        code_verified = bool_field_with_default "codeVerified" false fields;
        privacy_checks_passed = bool_field_with_default "privacyChecksPassed" false fields;
        contributed_examples = int_field_with_default "contributedExamples" 0 fields;
        update_norm = float_option_field "updateNorm" fields;
        result_metrics = float_assoc_field "metrics" fields;
        local_evidence_pointer = string_option_field "localEvidencePointer" fields;
      }
  | _ -> fail "Expected site result object"

(** Serialize one site result. *)
let yojson_of_site_result result =
  `Assoc
    [
      ("roundId", `String result.result_round_id);
      ("siteId", `String result.result_site_id);
      ("jobDigest", `String result.result_job_digest);
      ("participation", `String (string_of_site_participation result.participation));
      ("codeVerified", `Bool result.code_verified);
      ("privacyChecksPassed", `Bool result.privacy_checks_passed);
      ("contributedExamples", `Int result.contributed_examples);
      ( "updateNorm",
        match result.update_norm with Some value -> `Float value | None -> `Null );
      ("metrics", yojson_of_float_assoc result.result_metrics);
      ("localEvidencePointer", yojson_of_string_option result.local_evidence_pointer);
    ]

(** Parse the optional [selectedSites] of a readiness input.

    An absent key or [null] is [Unchecked], so inputs written before the key
    existed still parse. A list, even an empty one, is [Selected]: an empty
    selection accepts no result. *)
let selection_of_yojson = function
  | None | Some `Null -> Unchecked
  | Some (`List values) ->
      Selected
        (List.map
           (function `String site_id -> site_id | _ -> fail "Expected string list field: selectedSites")
           values)
  | Some _ -> fail "Expected list field: selectedSites"

(** Serialize an aggregation-readiness verdict. *)
let yojson_of_aggregation_readiness readiness =
  `Assoc
    [
      ("roundId", `String readiness.readiness_round_id);
      ("acceptedSites", yojson_of_string_list readiness.accepted_sites);
      ( "rejectedContributions",
        `List
          (List.map
             (fun rejection ->
               `Assoc
                 [
                   ("siteId", `String rejection.rejected_site_id);
                   ("reason", `String rejection.rejection_reason);
                 ])
             readiness.rejected_contributions) );
      ("acceptedExamples", `Int readiness.accepted_examples);
      ("aggregationPermitted", `Bool readiness.aggregation_permitted);
      ( "stopReason",
        match readiness.readiness_stop_reason with
        | Some reason -> `String (string_of_stop_condition reason)
        | None -> `Null );
      ("siteWeights", yojson_of_float_assoc readiness.site_weights);
    ]

(** Parse one release gate, accepting a bare threshold or a comparison object. *)
let release_gate_of_yojson (json : Yojson.Safe.t) =
  match json with
  | `Assoc fields ->
      let comparison =
        match List.assoc_opt "comparison" fields with
        | Some (`Assoc comparison_fields) -> (
            match string_field "kind" comparison_fields with
            | "at_least" -> AtLeast (float_field_with_default "value" 0.0 comparison_fields)
            | "at_most" -> AtMost (float_field_with_default "value" 0.0 comparison_fields)
            | "improves_on_baseline" ->
                ImprovesOnBaseline (float_field_with_default "margin" 0.0 comparison_fields)
            | value -> fail ("Unknown gate comparison: " ^ value))
        | Some _ -> fail "Expected gate comparison object"
        | None -> fail "Missing JSON field: comparison"
      in
      {
        gate_id = string_field "gateId" fields;
        gate_metric = string_field "metric" fields;
        comparison;
        (* Blocking is the safe default: a gate whose severity is unstated must
           not be treated as advisory. *)
        gate_blocking = bool_field_with_default "blocking" true fields;
      }
  | _ -> fail "Expected release gate object"

(** Serialize a release decision and its per-gate evidence. *)
let yojson_of_release_decision decision =
  `Assoc
    [
      ("roundId", `String decision.decision_round_id);
      ("candidateVersion", `String decision.candidate_version);
      ( "gateResults",
        `List
          (List.map
             (fun result ->
               `Assoc
                 [
                   ("gateId", `String result.result_gate_id);
                   ("outcome", `String (string_of_gate_outcome result.outcome));
                   ( "observed",
                     match result.observed with Some value -> `Float value | None -> `Null );
                   ( "threshold",
                     match result.threshold with Some value -> `Float value | None -> `Null );
                   ("detail", `String result.gate_detail);
                 ])
             decision.gate_results) );
      ("action", `String (string_of_release_action decision.action));
      ("blockingFailures", yojson_of_string_list decision.blocking_failures);
      ("rollbackVersion", yojson_of_string_option decision.rollback_version);
    ]

(** Read a required string field that may not be empty. *)
let non_empty_string_field name fields =
  match string_field name fields with "" -> fail ("Expected a non-empty string field: " ^ name) | value -> value

(** Parse the release decision stored with a candidate.

    Defaults are the ones that cannot lead to a release: an unstated Guard
    verdict is not permitted, and an unstated metrics source is
    [MetricsSourceUnrecorded]. *)
let recorded_release_decision_of_yojson (json : Yojson.Safe.t) =
  match json with
  | `Assoc fields ->
      {
        recorded_action = release_action_of_string (string_field "action" fields);
        recorded_guard_permitted = bool_field_with_default "guardPermitted" false fields;
        recorded_metrics_source =
          (match string_option_field "metricsSource" fields with
          | Some value -> metrics_source_of_string value
          | None -> MetricsSourceUnrecorded);
        recorded_baseline_version = string_option_field "baselineVersion" fields;
      }
  | _ -> fail "Expected release decision object"

(** Read a bool field that must be present. *)
let required_bool_field name fields =
  match field name fields with `Bool value -> value | _ -> fail ("Expected bool field: " ^ name)

(** Read a field that must be present and holds a string or [null]. *)
let required_string_option_field name fields =
  match field name fields with
  | `String value -> Some value
  | `Null -> None
  | _ -> fail ("Expected string or null field: " ^ name)

(** Read a field that must be present and holds a number or [null]. *)
let required_float_option_field name fields =
  match field name fields with
  | `Float value -> Some value
  | `Int value -> Some (float_of_int value)
  | `Intlit value -> Some (float_of_string value)
  | `Null -> None
  | _ -> fail ("Expected number or null field: " ^ name)

(** Read a metrics source that must be present. [null] is
    [MetricsSourceUnrecorded], as for a decision stored before the source was
    recorded. *)
let required_metrics_source_field name fields =
  match required_string_option_field name fields with
  | Some value -> metrics_source_of_string value
  | None -> MetricsSourceUnrecorded

(** Parse one gate result as a release decision stores it. Every key is
    required, because a repeated decision is compared on all of them. *)
let gate_result_of_yojson (json : Yojson.Safe.t) =
  match json with
  | `Assoc fields ->
      let result_gate_id = string_field "gateId" fields in
      let outcome = gate_outcome_of_string (string_field "outcome" fields) in
      let observed = required_float_option_field "observed" fields in
      let threshold = required_float_option_field "threshold" fields in
      let gate_detail = string_field "detail" fields in
      { result_gate_id; outcome; observed; threshold; gate_detail }
  | _ -> fail "Expected gate result object"

(** Read a required list of gate results, keeping their order. *)
let gate_results_field name fields =
  match field name fields with
  | `List values -> List.map gate_result_of_yojson values
  | _ -> fail ("Expected list field: " ^ name)

(** Read the four keys of a round's release decision besides its action and
    Guard verdict: [roundId], [baselineVersion], [metricsSource] and
    [gateResults]. All four are required; [baselineVersion] and
    [metricsSource] may hold [null]. *)
let decision_keys_of_fields fields =
  let round_id = non_empty_string_field "roundId" fields in
  let baseline_version = required_string_option_field "baselineVersion" fields in
  let metrics_source = required_metrics_source_field "metricsSource" fields in
  let gate_results = gate_results_field "gateResults" fields in
  (round_id, baseline_version, metrics_source, gate_results)

(** Read a round's release decision from [fields], given its action and Guard
    verdict. *)
let round_decision_of_fields ~action ~guard_permitted fields =
  let decided_round_id, decided_baseline_version, decided_metrics_source, decided_gate_results =
    decision_keys_of_fields fields
  in
  {
    decided_round_id;
    decided_action = action;
    decided_guard_permitted = guard_permitted;
    decided_baseline_version;
    decided_metrics_source;
    decided_gate_results;
  }

(** Parse the release decision a version already holds. Every key is
    required. *)
let stored_round_decision_of_yojson (json : Yojson.Safe.t) =
  match json with
  | `Assoc fields ->
      let action = release_action_of_string (string_field "action" fields) in
      let guard_permitted = required_bool_field "guardPermitted" fields in
      round_decision_of_fields ~action ~guard_permitted fields
  | _ -> fail "Expected stored decision object"

(** The keys of a [release_evaluated] event that describe its new decision
    beyond the action and the Guard verdict. *)
let new_decision_keys = [ "roundId"; "baselineVersion"; "metricsSource"; "gateResults" ]

(** Parse a [release_evaluated] event.

    Without [storedDecision] (absent or [null]) the event reads as it always
    has. The new decision's keys then come all together or not at all; when
    given they are checked, and the answer does not depend on them. With
    [storedDecision], the event's [guardPermitted] and every new-decision key
    are required. *)
let release_evaluated_of_fields fields =
  let status_field name = model_status_of_string (string_field name fields) in
  match List.assoc_opt "storedDecision" fields with
  | None | Some `Null ->
      (* The same record as before the stored decision existed, so an event
         without it reads, and fails, exactly as it did. *)
      let event =
        ReleaseEvaluated
          {
            status = status_field "status";
            action = release_action_of_string (string_field "action" fields);
            guard_permitted = bool_field_with_default "guardPermitted" false fields;
          }
      in
      if List.exists (fun key -> List.mem_assoc key fields) new_decision_keys then
        ignore (decision_keys_of_fields fields : string * string option * metrics_source * gate_result list);
      event
  | Some stored ->
      let status = status_field "status" in
      let action = release_action_of_string (string_field "action" fields) in
      let guard_permitted = required_bool_field "guardPermitted" fields in
      let new_decision = round_decision_of_fields ~action ~guard_permitted fields in
      let stored_decision = stored_round_decision_of_yojson stored in
      ReleaseEvaluatedWithStoredDecision { status; new_decision; stored_decision }

(** Parse one model lifecycle event from its ["kind"] discriminator. *)
let lifecycle_event_of_yojson (json : Yojson.Safe.t) =
  let status_field name fields = model_status_of_string (string_field name fields) in
  match json with
  | `Assoc fields -> (
      match string_field "kind" fields with
      | "registered" ->
          Registered
            {
              (* Candidate is the safe default: a registration that does not
                 ask for a release does not get one. *)
              requested_status =
                (match string_option_field "requestedStatus" fields with
                | Some value -> requested_status_of_string value
                | None -> RequestCandidate);
              released_version = string_option_field "releasedVersion" fields;
            }
      | "aggregated" ->
          Aggregated
            {
              parent_version = non_empty_string_field "parentVersion" fields;
              parent_status = status_field "parentStatus" fields;
            }
      | "release_evaluated" -> release_evaluated_of_fields fields
      | "approval_requested" ->
          ApprovalRequested
            {
              status = status_field "status" fields;
              decision =
                (match List.assoc_opt "decision" fields with
                | None | Some `Null -> None
                | Some value -> Some (recorded_release_decision_of_yojson value));
              released_version = string_option_field "releasedVersion" fields;
            }
      | value -> fail ("Unknown lifecycle event: " ^ value))
  | _ -> fail "Expected lifecycle event object"

(** Parse a model lifecycle request: a version and one event. *)
let lifecycle_request_of_yojson (json : Yojson.Safe.t) =
  match json with
  | `Assoc fields ->
      {
        lifecycle_version = non_empty_string_field "version" fields;
        lifecycle_event = lifecycle_event_of_yojson (field "event" fields);
      }
  | _ -> fail "Expected lifecycle request object"

(** Serialize one status change. [fromStatus] is [null] for a new version. *)
let yojson_of_status_change change =
  `Assoc
    [
      ("version", `String change.changed_version);
      ( "fromStatus",
        match change.from_status with Some status -> `String (string_of_model_status status) | None -> `Null );
      ("toStatus", `String (string_of_model_status change.to_status));
    ]

(** Serialize a lifecycle outcome as [allowed], [refusal], [reason] and
    [statusChanges]. A refused event has no status changes. *)
let yojson_of_lifecycle_outcome outcome =
  let allowed, refusal, changes =
    match outcome.verdict with
    | LifecycleAllowed changes -> (true, `Null, changes)
    | LifecycleRefused refusal -> (false, `String (string_of_lifecycle_refusal refusal), [])
  in
  `Assoc
    [
      ("allowed", `Bool allowed);
      ("refusal", refusal);
      ("reason", `String outcome.lifecycle_reason);
      ("statusChanges", `List (List.map yojson_of_status_change changes));
    ]

(** Read an age in seconds that must be present: [null], or a finite number
    that [valid] accepts. *)
let required_seconds_field name ~valid ~expected fields =
  match required_float_option_field name fields with
  | None -> None
  | Some value when Float.is_finite value && valid value -> Some value
  | Some _ -> fail ("Expected " ^ name ^ " to be null or " ^ expected)

(** Parse what recovery knows about one stored round. Every key is required,
    and the keys are read in order, so the first bad key is the one named. *)
let round_recovery_request_of_yojson (json : Yojson.Safe.t) =
  match json with
  | `Assoc fields ->
      let recovery_status = round_status_of_string (string_field "status" fields) in
      let recovery_phase = round_phase_of_string (string_field "phase" fields) in
      let record_readable = required_bool_field "readable" fields in
      let updates_kept = required_bool_field "updatesKept" fields in
      let candidate_registered = required_bool_field "candidateRegistered" fields in
      let oldest_update_age_seconds =
        required_seconds_field "oldestUpdateAgeSeconds"
          ~valid:(fun age -> age >= 0.0)
          ~expected:"a finite number of seconds, 0 or more" fields
      in
      let update_max_age_seconds =
        required_seconds_field "updateMaxAgeSeconds"
          ~valid:(fun limit -> limit > 0.0)
          ~expected:"a finite number of seconds, more than 0" fields
      in
      {
        recovery_status;
        recovery_phase;
        record_readable;
        updates_kept;
        candidate_registered;
        oldest_update_age_seconds;
        update_max_age_seconds;
      }
  | _ -> fail "Expected round recovery object"

(** Serialize a recovery verdict as [verdict], [reason] and [message]. A kept
    round has a [null] reason. *)
let yojson_of_round_recovery recovery =
  let reason =
    match recovery.recovery_verdict with
    | KeepRound -> `Null
    | CloseRound reason -> `String (string_of_recovery_close_reason reason)
  in
  `Assoc
    [
      ("verdict", `String (string_of_recovery_verdict recovery.recovery_verdict));
      ("reason", reason);
      ("message", `String recovery.recovery_message);
    ]

(** Serialize a deployment target in its object form, with every key, so that
    [deployment_target_of_yojson] reads it back unchanged. *)
let yojson_of_deployment_target = function
  | Kubernetes -> `Assoc [ ("kind", `String "kubernetes") ]
  | Compose target ->
      let network_name, is_external =
        match target.compose_network with
        | ProjectNetwork name -> (name, false)
        | ExternalNetwork name -> (name, true)
      in
      let placement = match target.compose_placement with OwnProject -> "own" | MergedProject -> "merge" in
      `Assoc
        [
          ("kind", `String "compose");
          ("placement", `String placement);
          ("network", `String network_name);
          ("externalNetwork", `Bool is_external);
          ("existingServices", yojson_of_string_list target.existing_services);
        ]

(** Serialize one Compose issue as its service, code and value. *)
let yojson_of_compose_issue (issue : compose_issue) =
  let code, value = compose_issue_code_and_value issue.issue_reason in
  `Assoc
    [
      ("service", yojson_of_string_option issue.issue_service);
      ("code", `String code);
      ("value", yojson_of_string_option value);
    ]

(** Serialize a Compose plan. The keys are always in this order:
    [planId], [services], [issues], [composeYaml]. *)
let yojson_of_compose_plan (plan : compose_plan) =
  `Assoc
    [
      ("planId", `String plan.compose_plan_id);
      ("services", yojson_of_string_list plan.compose_service_names);
      ("issues", `List (List.map yojson_of_compose_issue plan.compose_issues));
      ("composeYaml", yojson_of_string_option plan.compose_yaml);
    ]
