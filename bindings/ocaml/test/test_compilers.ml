open Dagents_common_ir

let assert_true message condition =
  if not condition then failwith message

let contains haystack needle =
  let haystack_len = String.length haystack in
  let needle_len = String.length needle in
  let rec loop index =
    if index + needle_len > haystack_len then false
    else if String.sub haystack index needle_len = needle then true
    else loop (index + 1)
  in
  if needle_len = 0 then true else loop 0

let expect_invalid_argument message thunk =
  try
    let _ = thunk () in
    failwith message
  with
  | Invalid_argument _ -> ()

let test_dataset_profile () =
  let profile =
    Dagents_dataset_compiler.build_profile
      ~scope_id:"source-a"
      ~scope_kind:Source
      ~extraction_strategy:Tabular
      ~label_field:"label"
      [ [ ("value", VFloat 1.2); ("score", VFloat 0.1); ("label", VInt 0) ];
        [ ("value", VFloat 2.4); ("score", VFloat 0.2); ("label", VInt 1) ] ]
  in
  assert_true "dataset profile should count records" (profile.record_count = 2);
  assert_true "dataset profile should infer numeric fields"
    (profile.numeric_fields = [ "value"; "score" ]);
  assert_true "dataset profile should exclude label from feature fields"
    (profile.feature_fields = [ "value"; "score" ])

let test_pipeline_compiler_orders_and_lowers () =
  let compiled =
    Dagents_pipeline_compiler.compile
      {
        pipeline_id = "pipeline";
        steps =
          [
            {
              step_id = "summarize";
              kind = SummarizeItems;
              depends_on = [ "filter" ];
              config_json = None;
            };
            {
              step_id = "filter";
              kind = FilterItems;
              depends_on = [ "profile" ];
              config_json = None;
            };
            {
              step_id = "profile";
              kind = ProfileDataset;
              depends_on = [ "model" ];
              config_json = None;
            };
            {
              step_id = "model";
              kind = RunModelJob;
              depends_on = [];
              config_json = None;
            };
          ];
      }
  in
  let step_ids = List.map (fun step -> step.step_id) compiled.steps in
  assert_true "pipeline compiler should topologically sort steps"
    (step_ids = [ "model"; "profile"; "filter"; "summarize" ]);
  let model_step = List.find (fun step -> step.step_id = "model") compiled.steps in
  let profile_step = List.find (fun step -> step.step_id = "profile") compiled.steps in
  assert_true "run_model_job should target python service"
    (model_step.execution_target = PythonService);
  assert_true "profile_dataset should target local process"
    (profile_step.execution_target = LocalProcess)

let test_pipeline_compiler_rejects_cycles () =
  expect_invalid_argument "pipeline compiler should reject cycles" (fun () ->
      Dagents_pipeline_compiler.validate
        {
          pipeline_id = "cyclic";
          steps =
            [
              { step_id = "a"; kind = EnrichContext; depends_on = [ "b" ]; config_json = None };
              { step_id = "b"; kind = FilterItems; depends_on = [ "a" ]; config_json = None };
            ];
        } )

let test_model_router () =
  let profile =
    Dagents_dataset_compiler.build_profile
      ~scope_id:"scope"
      ~scope_kind:Source
      ~extraction_strategy:TimeSeries
      [ [ ("timestamp", VString "2026-01-01T00:00:00Z"); ("errors", VInt 2) ] ]
  in
  let plan = Dagents_model_router.route profile Forecasting in
  assert_true "model router should prefer gru for forecasting" (plan.selected_model = Gru);
  assert_true "forecasting should use long running deployment"
    (plan.packaging_mode = LongRunningDeployment)

let test_manifest_compiler_plan () =
  let plan =
    Dagents_manifest_compiler.compile_plan
      {
        plan_id = "plan-1";
        namespace = "dagents";
        include_services = true;
        include_config_maps = true;
        components =
          [
            {
              name = "core";
              image = "ghcr.io/example/core:latest";
              kind = Deployment;
              replicas = 2;
              schedule = None;
              env = [ { name = "APP_ENV"; value = "cloud" } ];
              ports = [ { port_name = "http"; container_port = 8060 } ];
              args = [ "--server.port=8060" ];
              resources = default_resources;
            };
            {
              name = "reconciler";
              image = "ghcr.io/example/reconciler:latest";
              kind = CronJob;
              replicas = 1;
              schedule = Some "*/15 * * * *";
              env = [ { name = "MODE"; value = "reconcile" } ];
              ports = [];
              args = [ "--sync" ];
              resources = default_resources;
            };
            {
              name = "edge-service";
              image = "ghcr.io/example/edge:latest";
              kind = Service;
              replicas = 1;
              schedule = None;
              env = [];
              ports = [ { port_name = "grpc"; container_port = 9090 } ];
              args = [];
              resources = default_resources;
            };
          ];
      }
  in
  assert_true "manifest compiler should preserve plan id" (plan.plan_id = "plan-1");
  assert_true "manifest compiler should render deployment" (contains plan.combined_yaml "kind: Deployment");
  assert_true "manifest compiler should render cronjob" (contains plan.combined_yaml "kind: CronJob");
  assert_true "manifest compiler should render service" (contains plan.combined_yaml "kind: Service");
  assert_true "manifest compiler should render config map" (contains plan.combined_yaml "kind: ConfigMap");
  assert_true "cronjob should include schedule" (contains plan.combined_yaml "*/15 * * * *");
  assert_true "rendered workload should include env vars" (contains plan.combined_yaml "APP_ENV");
  assert_true "rendered workload should include args" (contains plan.combined_yaml "--sync")

let test_json_codec_roundtrip () =
  let spec_json =
    `Assoc
      [
        ("planId", `String "json-plan");
        ("namespace", `String "dagents");
        ("includeServices", `Bool true);
        ("includeConfigMaps", `Bool true);
        ( "components",
          `List
            [
              `Assoc
                [
                  ("name", `String "compiler");
                  ("image", `String "ghcr.io/example/compiler:latest");
                  ("kind", `String "CronJob");
                  ("schedule", `String "0 * * * *");
                  ("replicas", `Int 1);
                  ("args", `List [ `String "--compile" ]);
                  ("env", `List [ `Assoc [ ("name", `String "APP_ENV"); ("value", `String "test") ] ]);
                  ( "ports",
                    `List [ `Assoc [ ("name", `String "http"); ("containerPort", `Int 8080) ] ] );
                  ( "resources",
                    `Assoc
                      [
                        ("cpuRequest", `String "100m");
                        ("cpuLimit", `String "500m");
                        ("memoryRequest", `String "128Mi");
                        ("memoryLimit", `String "512Mi");
                      ] );
                ];
            ] );
      ]
  in
  let spec = Json_codec.workload_spec_of_yojson spec_json in
  let plan = Dagents_manifest_compiler.compile_plan spec in
  let encoded = Json_codec.yojson_of_workload_plan plan in
  match encoded with
  | `Assoc fields ->
      assert_true "json codec should include plan id"
        (List.assoc "planId" fields = `String "json-plan");
      assert_true "json codec should include manifests"
        (match List.assoc "manifests" fields with `List manifests -> List.length manifests = 1 | _ -> false)
  | _ -> failwith "expected workload plan object"

let () =
  test_dataset_profile ();
  test_pipeline_compiler_orders_and_lowers ();
  test_pipeline_compiler_rejects_cycles ();
  test_model_router ();
  test_manifest_compiler_plan ();
  test_json_codec_roundtrip ()
