(** Dagents compiler CLI.

    This binary exposes the OCaml functional kernels from the command line so
    demo scripts and service prototypes can compile manifests, compile
    pipelines, route models, profile datasets, validate sources, evaluate
    quality rules, and apply transforms.

    The CLI is intentionally thin: it parses arguments/JSON, delegates planning
    to library modules, and prints text or JSON output. It does not perform
    cluster deployment, database I/O, or model execution. *)

open Dagents_common_ir

(** Read all bytes from an input channel.

    Inputs:
    - [channel]: usually [stdin] or an opened file channel.

    Output:
    - [string]: full channel contents.

    Example test case:
    {[
      (* With stdin containing "{}", [read_all stdin] returns "{}". *)
    ]} *)
let read_all channel =
  let buffer = Buffer.create 1024 in
  (try
     while true do
       Buffer.add_channel buffer channel 1024
     done
   with End_of_file -> ());
  Buffer.contents buffer

(** Print supported command shapes and terminate with a non-zero exit.

    Output: does not return; exits the process with code 1. *)
let usage () =
  prerr_endline "Usage:";
  prerr_endline "  dagentsc manifest compile [--input <file|->] [--output yaml|json]";
  prerr_endline "  dagentsc pipeline compile [--input <file|->] [--output text|json]";
  prerr_endline "  dagentsc model route [--task <task>] [--output text|json]";
  prerr_endline "  dagentsc dataset profile [--scope-id <id>] [--output text|json]";
  prerr_endline "  dagentsc dataset source validate|metadata|extract [--input <file|->]";
  prerr_endline "  dagentsc dataset schema validate [--records <file|->] [--contract <file>]";
  prerr_endline "  dagentsc dataset quality evaluate [--records <file|->] [--rules <file>]";
  prerr_endline "  dagentsc dataset transform compile|apply [--records <file|->] [--operations <file>]";
  exit 1

(** Read a flag value from the command-line argument list.

    Inputs:
    - [flag]: flag name such as ["--output"].
    - [args]: command arguments after the top-level command words.
    - [default]: value returned when the flag is absent.

    Output: flag value or [default]. *)
let arg_value flag args default =
  let rec loop = function
    | [] -> default
    | key :: value :: rest when key = flag -> value
    | _ :: rest -> loop rest
  in
  loop args

(** Parse a comma-separated dependency list from [--step] shorthand. *)
let split_dependencies raw =
  if raw = "" then [] else String.split_on_char ',' raw

(** Read the generic [--input] JSON payload from stdin or a file. *)
let read_json_input args =
  let input = arg_value "--input" args "-" in
  if input = "-" then Yojson.Safe.from_string (read_all stdin) else Yojson.Safe.from_file input

(** Parse a compact pipeline step declaration.

    Inputs:
    - [raw]: ["step_id:kind"] or ["step_id:kind:dep1,dep2"].

    Output:
    - [pipeline_step] with no config JSON.

    Raises:
    - [Invalid_argument] when the compact format is malformed. *)
let parse_step raw =
  match String.split_on_char ':' raw with
  | [ step_id; kind; deps ] ->
      { step_id; kind = step_kind_of_string kind; depends_on = split_dependencies deps; config_json = None }
  | [ step_id; kind ] ->
      { step_id; kind = step_kind_of_string kind; depends_on = []; config_json = None }
  | _ -> invalid_arg ("Invalid --step format: " ^ raw)

(** Pretty-print JSON output with a trailing newline for shell use. *)
let output_json json =
  Yojson.Safe.pretty_to_channel stdout json;
  print_newline ()

(** Handle [dagentsc manifest compile].

    Inputs: CLI args, optionally including [--input] JSON. Without [--input],
    the handler builds a small demo workload from flags.
    Output: YAML by default, JSON when [--output json]. *)
let manifest_compile args =
  let output = arg_value "--output" args "yaml" in
  (* Accept a full JSON workload spec for service-like callers, while keeping a
     flag-only path for live demos. *)
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

(** Handle [dagentsc pipeline compile].

    Inputs: pipeline JSON via [--input] or repeated compact [--step] flags.
    Output: text step listing by default, JSON when requested. *)
let pipeline_compile args =
  let definition =
    if List.mem "--input" args then Json_codec.pipeline_definition_of_yojson (read_json_input args)
    else
      let pipeline_id = arg_value "--pipeline-id" args "pipeline" in
      (* Preserve command-line step order before the compiler topologically
         orders dependencies. *)
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

(** Handle [dagentsc model route].

    This command uses a small built-in profile so a demo can show routing
    behavior without needing an external dataset file. *)
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

(** Handle [dagentsc dataset profile] using a small built-in tabular dataset. *)
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

(** Read JSON from a named file/stdin flag, such as [--records] or [--rules]. *)
let read_json_from_flag flag args =
  let input = arg_value flag args "-" in
  if input = "-" then Yojson.Safe.from_string (read_all stdin) else Yojson.Safe.from_file input

(** Handle source validation, metadata, and extraction-plan compilation. *)
let dataset_source action args =
  let source = Json_codec.source_spec_of_yojson (read_json_input args) in
  match action with
  | "validate" ->
      output_json
        (Json_codec.yojson_of_source_validation_result
           (Dagents_dataset_compiler.validate_source source))
  | "metadata" ->
      output_json
        (Json_codec.yojson_of_source_metadata
           (Dagents_dataset_compiler.metadata_of_source source))
  | "extract" ->
      output_json
        (Json_codec.yojson_of_extraction_plan
           (Dagents_dataset_compiler.compile_extraction_plan source))
  | _ -> usage ()

(** Handle schema-contract validation over record JSON. *)
let dataset_schema action args =
  match action with
  | "validate" ->
      let records = Json_codec.records_of_yojson (read_json_from_flag "--records" args) in
      let contract = Json_codec.schema_contract_of_yojson (read_json_from_flag "--contract" args) in
      let report =
        Dagents_dataset_compiler.validate_schema_contract contract
          (Dagents_dataset_compiler.infer_schema records)
      in
      output_json (Json_codec.yojson_of_schema_validation_report report)
  | _ -> usage ()

(** Handle data-quality evaluation over record JSON. *)
let dataset_quality action args =
  match action with
  | "evaluate" ->
      let records = Json_codec.records_of_yojson (read_json_from_flag "--records" args) in
      let rules =
        match read_json_from_flag "--rules" args with
        | `List values -> List.map Json_codec.quality_rule_of_yojson values
        | value -> [ Json_codec.quality_rule_of_yojson value ]
      in
      let report = Dagents_dataset_compiler.evaluate_quality_report records rules in
      output_json (Json_codec.yojson_of_quality_report report)
  | _ -> usage ()

(** Handle transform-plan compilation and transform execution. *)
let dataset_transform action args =
  let records = Json_codec.records_of_yojson (read_json_from_flag "--records" args) in
  let operations = Json_codec.transform_operations_of_yojson (read_json_from_flag "--operations" args) in
  let plan =
    Dagents_dataset_compiler.compile_transform_plan
      ~plan_id:(arg_value "--plan-id" args "transform-plan")
      operations records
  in
  match action with
  | "compile" -> output_json (Json_codec.yojson_of_transform_plan plan)
  | "apply" ->
      output_json
        (Json_codec.yojson_of_records
           (Dagents_dataset_compiler.apply_transform_plan plan records))
  | _ -> usage ()

(** Dispatch the top-level command.

    The nested pattern match keeps command names explicit and makes unsupported
    command shapes fall back to [usage]. *)
let () =
  let args = Array.to_list Sys.argv |> List.tl in
  match args with
  | "manifest" :: "compile" :: rest -> manifest_compile rest
  | "pipeline" :: "compile" :: rest -> pipeline_compile rest
  | "model" :: "route" :: rest -> model_route rest
  | "dataset" :: "profile" :: rest -> dataset_profile rest
  | "dataset" :: "source" :: action :: rest -> dataset_source action rest
  | "dataset" :: "schema" :: action :: rest -> dataset_schema action rest
  | "dataset" :: "quality" :: action :: rest -> dataset_quality action rest
  | "dataset" :: "transform" :: action :: rest -> dataset_transform action rest
  | _ -> usage ()
