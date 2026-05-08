(** Kubernetes manifest compiler implementation.

    This module renders a typed [workload_spec] into YAML strings. It is
    intentionally deterministic and side-effect free, which makes it suitable
    for a functional planning layer that can run before deployment automation. *)

open Dagents_common_ir

(** Prefix each line with an indentation string.

    Inputs: [prefix] and [lines].
    Output: [lines] with the prefix prepended to every line.

    Example test case:
    {[
      assert (indent "  " [ "x" ] = [ "  x" ])
    ]} *)
let indent prefix lines = List.map (fun line -> prefix ^ line) lines

(** Render container command-line args when present. *)
let render_args indent_prefix args =
  match args with
  | [] -> []
  | values -> (indent_prefix ^ "args:") :: List.map (fun arg -> indent_prefix ^ "- \"" ^ arg ^ "\"") values

(** Render Kubernetes [env] entries when environment variables are present. *)
let render_env indent_prefix env_vars =
  match env_vars with
  | [] -> []
  | values ->
      (indent_prefix ^ "env:")
      :: List.concat_map
           (fun { name; value } ->
             [ indent_prefix ^ "- name: " ^ name; indent_prefix ^ "  value: \"" ^ value ^ "\"" ])
           values

(** Render Kubernetes container ports when ports are present. *)
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

(** Render resource requests and limits for one container. *)
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

(** Render the shared container block used by Deployment, Job, and CronJob.

    Inputs: indentation prefix and one [workload_component].
    Output: YAML lines for the container list entry. *)
let render_container indent_prefix (component : workload_component) =
  [
    indent_prefix ^ "- name: main";
    indent_prefix ^ "  image: " ^ component.image;
  ]
  @ render_args (indent_prefix ^ "  ") component.args
  @ render_ports (indent_prefix ^ "  ") component.ports
  @ render_env (indent_prefix ^ "  ") component.env
  @ render_resources (indent_prefix ^ "  ") component.resources

(** Render a Kubernetes Deployment for a workload component. *)
let render_deployment namespace (component : workload_component) =
  String.concat "\n"
    ([
       "apiVersion: apps/v1";
       "kind: Deployment";
       "metadata:";
       "  name: " ^ component.name;
       "  namespace: " ^ namespace;
       "spec:";
       (* A Deployment with zero replicas is rarely useful for framework demos,
          so we clamp to at least one replica in the rendered manifest. *)
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

(** Render a Kubernetes Job for one-shot workload execution. *)
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

(** Render a Kubernetes CronJob for scheduled workload execution. *)
let render_cron_job namespace (component : workload_component) =
  String.concat "\n"
    ([
       "apiVersion: batch/v1";
       "kind: CronJob";
       "metadata:";
       "  name: " ^ component.name;
       "  namespace: " ^ namespace;
       "spec:";
       (* Use an hourly schedule when the component did not provide one so the
          rendered CronJob stays syntactically complete. *)
       "  schedule: \"" ^ Option.value component.schedule ~default:"0 * * * *" ^ "\"";
       "  jobTemplate:";
       "    spec:";
       "      template:";
       "        spec:";
       "          restartPolicy: Never";
       "          containers:";
     ]
    @ render_container "          " component)

(** Render a Service only when the component exposes ports.

    Output: [None] for portless components, because Kubernetes Services without
    ports are not useful for these generated workloads. *)
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

(** Render a small ConfigMap that records component metadata. *)
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

(** Render the primary Kubernetes object requested by [component.kind]. *)
let render_primary namespace (component : workload_component) =
  match component.kind with
  | Deployment -> render_deployment namespace component
  | Job -> render_job namespace component
  | CronJob -> render_cron_job namespace component
  | Service -> Option.value (render_service namespace component) ~default:""
  | ConfigMap -> Option.value (render_config_map namespace component) ~default:""

(** Compile all workload components into manifest records.

    The primary object is always rendered. Services and ConfigMaps are attached
    only when requested by [workload_spec] flags and when they are not already
    the component's primary object. *)
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

(** Combine all non-empty YAML fragments into one multi-document YAML string. *)
let combined_yaml manifests =
  manifests
  |> List.concat_map (fun manifest ->
         [ Some manifest.deployment_yaml; manifest.service_yaml; manifest.config_map_yaml ])
  |> List.filter_map Fun.id
  |> List.filter (fun section -> String.trim section <> "")
  |> String.concat "\n---\n"

(** Build a complete workload plan with both structured manifests and combined
    YAML. *)
let compile_plan spec =
  let manifests = compile spec in
  { plan_id = spec.plan_id; namespace = spec.namespace; manifests; combined_yaml = combined_yaml manifests }
