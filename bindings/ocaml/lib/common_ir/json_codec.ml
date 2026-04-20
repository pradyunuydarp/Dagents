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
