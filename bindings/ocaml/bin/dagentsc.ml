open Dagents_common_ir

let read_all channel =
  let buffer = Buffer.create 1024 in
  (try
     while true do
       Buffer.add_channel buffer channel 1024
     done
   with End_of_file -> ());
  Buffer.contents buffer

let usage () =
  prerr_endline "Usage:";
  prerr_endline "  dagentsc manifest compile [--input <file|->] [--output yaml|json]";
  prerr_endline "  dagentsc pipeline compile [--input <file|->] [--output text|json]";
  prerr_endline "  dagentsc model route [--task <task>] [--output text|json]";
  prerr_endline "  dagentsc dataset profile [--scope-id <id>] [--output text|json]";
  exit 1

let arg_value flag args default =
  let rec loop = function
    | [] -> default
    | key :: value :: rest when key = flag -> value
    | _ :: rest -> loop rest
  in
  loop args

let split_dependencies raw =
  if raw = "" then [] else String.split_on_char ',' raw

let read_json_input args =
  let input = arg_value "--input" args "-" in
  if input = "-" then Yojson.Safe.from_string (read_all stdin) else Yojson.Safe.from_file input

let parse_step raw =
  match String.split_on_char ':' raw with
  | [ step_id; kind; deps ] ->
      { step_id; kind = step_kind_of_string kind; depends_on = split_dependencies deps; config_json = None }
  | [ step_id; kind ] ->
      { step_id; kind = step_kind_of_string kind; depends_on = []; config_json = None }
  | _ -> invalid_arg ("Invalid --step format: " ^ raw)

let output_json json =
  Yojson.Safe.pretty_to_channel stdout json;
  print_newline ()

let manifest_compile args =
  let output = arg_value "--output" args "yaml" in
  let spec =
    if List.mem "--input" args then Json_codec.workload_spec_of_yojson (read_json_input args)
    else
      let namespace = arg_value "--namespace" args "dagents" in
      let plan_id = arg_value "--plan-id" args "dagents-plan" in
      let name = arg_value "--name" args "component" in
      let image = arg_value "--image" args "ghcr.io/example/component:latest" in
      let kind = workload_kind_of_string (arg_value "--kind" args "Deployment") in
      {
        plan_id;
        namespace;
        include_services = true;
        include_config_maps = true;
        components =
          [
            {
              name;
              image;
              kind;
              replicas = 1;
              schedule = None;
              env = [];
              ports = [ { port_name = "http"; container_port = 8080 } ];
              args = [];
              resources = default_resources;
            };
          ];
      }
  in
  let plan = Dagents_manifest_compiler.compile_plan spec in
  if output = "json" then output_json (Json_codec.yojson_of_workload_plan plan)
  else print_endline plan.combined_yaml

let pipeline_compile args =
  let definition =
    if List.mem "--input" args then Json_codec.pipeline_definition_of_yojson (read_json_input args)
    else
      let pipeline_id = arg_value "--pipeline-id" args "pipeline" in
      let rec collect_steps acc = function
        | [] -> List.rev acc
        | "--step" :: raw :: rest -> collect_steps (parse_step raw :: acc) rest
        | _ :: rest -> collect_steps acc rest
      in
      { pipeline_id; steps = collect_steps [] args }
  in
  let pipeline = Dagents_pipeline_compiler.compile definition in
  if arg_value "--output" args "text" = "json" then
    output_json (Json_codec.yojson_of_pipeline pipeline)
  else List.iter (fun step -> print_endline (step.step_id ^ ":" ^ string_of_step_kind step.kind)) pipeline.steps

let model_route args =
  let task =
    match arg_value "--task" args "anomaly_detection" with
    | "forecasting" -> Forecasting
    | "classification" -> Classification
    | "embedding" -> Embedding
    | "regression" -> Regression
    | _ -> AnomalyDetection
  in
  let profile =
    Dagents_dataset_compiler.build_profile
      ~scope_id:(arg_value "--scope-id" args "scope")
      ~scope_kind:Source
      ~extraction_strategy:Tabular
      [ [ ("value", VFloat 1.0); ("score", VFloat 0.2) ] ]
  in
  let plan = Dagents_model_router.route profile task in
  if arg_value "--output" args "text" = "json" then
    output_json (Json_codec.route_plan_to_yojson plan)
  else print_endline (string_of_model_family plan.selected_model)

let dataset_profile args =
  let scope_id = arg_value "--scope-id" args "scope" in
  let profile =
    Dagents_dataset_compiler.build_profile
      ~scope_id
      ~scope_kind:Source
      ~extraction_strategy:Tabular
      [
        [ ("value", VFloat 1.0); ("score", VFloat 0.2) ];
        [ ("value", VFloat 2.0); ("score", VFloat 0.4) ];
      ]
  in
  if arg_value "--output" args "text" = "json" then
    output_json (Json_codec.dataset_profile_to_yojson profile)
  else (
    print_endline ("records=" ^ string_of_int profile.record_count);
    print_endline ("partitions=" ^ string_of_int profile.partition_count) )

let () =
  let args = Array.to_list Sys.argv |> List.tl in
  match args with
  | "manifest" :: "compile" :: rest -> manifest_compile rest
  | "pipeline" :: "compile" :: rest -> pipeline_compile rest
  | "model" :: "route" :: rest -> model_route rest
  | "dataset" :: "profile" :: rest -> dataset_profile rest
  | _ -> usage ()
