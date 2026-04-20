open Dagents_common_ir

let indent prefix lines = List.map (fun line -> prefix ^ line) lines

let render_args indent_prefix args =
  match args with
  | [] -> []
  | values -> (indent_prefix ^ "args:") :: List.map (fun arg -> indent_prefix ^ "- \"" ^ arg ^ "\"") values

let render_env indent_prefix env_vars =
  match env_vars with
  | [] -> []
  | values ->
      (indent_prefix ^ "env:")
      :: List.concat_map
           (fun { name; value } ->
             [ indent_prefix ^ "- name: " ^ name; indent_prefix ^ "  value: \"" ^ value ^ "\"" ])
           values

let render_ports indent_prefix ports =
  match ports with
  | [] -> []
  | values ->
      (indent_prefix ^ "ports:")
      :: List.concat_map
           (fun { port_name; container_port } ->
             [
               indent_prefix ^ "- name: " ^ port_name;
               indent_prefix ^ "  containerPort: " ^ string_of_int container_port;
             ])
           values

let render_resources indent_prefix resources =
  [
    indent_prefix ^ "resources:";
    indent_prefix ^ "  requests:";
    indent_prefix ^ "    cpu: " ^ resources.cpu_request;
    indent_prefix ^ "    memory: " ^ resources.memory_request;
    indent_prefix ^ "  limits:";
    indent_prefix ^ "    cpu: " ^ resources.cpu_limit;
    indent_prefix ^ "    memory: " ^ resources.memory_limit;
  ]

let render_container indent_prefix (component : workload_component) =
  [
    indent_prefix ^ "- name: main";
    indent_prefix ^ "  image: " ^ component.image;
  ]
  @ render_args (indent_prefix ^ "  ") component.args
  @ render_ports (indent_prefix ^ "  ") component.ports
  @ render_env (indent_prefix ^ "  ") component.env
  @ render_resources (indent_prefix ^ "  ") component.resources

let render_deployment namespace (component : workload_component) =
  String.concat "\n"
    ([
       "apiVersion: apps/v1";
       "kind: Deployment";
       "metadata:";
       "  name: " ^ component.name;
       "  namespace: " ^ namespace;
       "spec:";
       "  replicas: " ^ string_of_int (max 1 component.replicas);
       "  selector:";
       "    matchLabels:";
       "      app: " ^ component.name;
       "  template:";
       "    metadata:";
       "      labels:";
       "        app: " ^ component.name;
       "    spec:";
       "      containers:";
     ]
    @ render_container "      " component)

let render_job namespace (component : workload_component) =
  String.concat "\n"
    ([
       "apiVersion: batch/v1";
       "kind: Job";
       "metadata:";
       "  name: " ^ component.name;
       "  namespace: " ^ namespace;
       "spec:";
       "  template:";
       "    spec:";
       "      restartPolicy: Never";
       "      containers:";
     ]
    @ render_container "      " component)

let render_cron_job namespace (component : workload_component) =
  String.concat "\n"
    ([
       "apiVersion: batch/v1";
       "kind: CronJob";
       "metadata:";
       "  name: " ^ component.name;
       "  namespace: " ^ namespace;
       "spec:";
       "  schedule: \"" ^ Option.value component.schedule ~default:"0 * * * *" ^ "\"";
       "  jobTemplate:";
       "    spec:";
       "      template:";
       "        spec:";
       "          restartPolicy: Never";
       "          containers:";
     ]
    @ render_container "          " component)

let render_service namespace (component : workload_component) =
  match component.ports with
  | [] -> None
  | ports ->
      Some
        (String.concat "\n"
           ([
              "apiVersion: v1";
              "kind: Service";
              "metadata:";
              "  name: " ^ component.name;
              "  namespace: " ^ namespace;
              "spec:";
              "  selector:";
              "    app: " ^ component.name;
              "  ports:";
            ]
           @ List.concat_map
               (fun { port_name; container_port } ->
                 [
                   "  - name: " ^ port_name;
                   "    port: " ^ string_of_int container_port;
                   "    targetPort: " ^ string_of_int container_port;
                 ])
               ports))

let render_config_map namespace (component : workload_component) =
  Some
    (String.concat "\n"
       [
         "apiVersion: v1";
         "kind: ConfigMap";
         "metadata:";
         "  name: " ^ component.name ^ "-config";
         "  namespace: " ^ namespace;
         "data:";
         "  component-kind: \"" ^ string_of_workload_kind component.kind ^ "\"";
         "  image: \"" ^ component.image ^ "\"";
       ])

let render_primary namespace (component : workload_component) =
  match component.kind with
  | Deployment -> render_deployment namespace component
  | Job -> render_job namespace component
  | CronJob -> render_cron_job namespace component
  | Service -> Option.value (render_service namespace component) ~default:""
  | ConfigMap -> Option.value (render_config_map namespace component) ~default:""

let compile (spec : workload_spec) =
  List.map
    (fun component ->
      {
        component_name = component.name;
        kind = component.kind;
        deployment_yaml = render_primary spec.namespace component;
        service_yaml =
          if spec.include_services && component.kind <> Service then render_service spec.namespace component else None;
        config_map_yaml =
          if spec.include_config_maps && component.kind <> ConfigMap then render_config_map spec.namespace component
          else None;
      })
    spec.components

let combined_yaml manifests =
  manifests
  |> List.concat_map (fun manifest ->
         [ Some manifest.deployment_yaml; manifest.service_yaml; manifest.config_map_yaml ])
  |> List.filter_map Fun.id
  |> List.filter (fun section -> String.trim section <> "")
  |> String.concat "\n---\n"

let compile_plan spec =
  let manifests = compile spec in
  { plan_id = spec.plan_id; namespace = spec.namespace; manifests; combined_yaml = combined_yaml manifests }
