(** Manifest compiler implementation.

    This module renders a typed [workload_spec] into Kubernetes YAML or a Docker
    Compose file. It is deterministic and has no side effects, so it can run
    before any deployment step. *)

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

(** Resolve the service account a component runs under, if any.

    A component that asks for a generated [ServiceAccount] is bound to one named
    after itself, so declaring the resource and wiring the pod to it are one
    decision rather than two that can disagree. *)
let service_account_name (component : workload_component) =
  match component.service_account_name with
  | Some name -> Some name
  | None -> if List.mem ServiceAccount component.generated_resources then Some component.name else None

(** Render the pod-spec line binding a workload to its service account. *)
let render_service_account_binding indent_prefix (component : workload_component) =
  match service_account_name component with
  | Some name -> [ indent_prefix ^ "serviceAccountName: " ^ name ]
  | None -> []

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
     ]
    @ render_service_account_binding "      " component
    @ [ "      containers:" ]
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
     ]
    @ render_service_account_binding "      " component
    @ [ "      containers:" ]
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
     ]
    @ render_service_account_binding "          " component
    @ [ "          containers:" ]
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
              "  type: " ^ component.service_type;
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

(** Render a ConfigMap carrying the component's own data.

    Component-supplied data wins. The metadata fallback exists so a component
    that asked for a ConfigMap without supplying any still gets a valid object
    rather than one with an empty [data] block. *)
let render_config_map namespace (component : workload_component) =
  let data =
    match component.config_map_data with
    | [] ->
        [
          ("component-kind", string_of_workload_kind component.kind);
          ("image", component.image);
        ]
    | entries -> entries
  in
  Some
    (String.concat "\n"
       ([
          "apiVersion: v1";
          "kind: ConfigMap";
          "metadata:";
          "  name: " ^ component.name ^ "-config";
          "  namespace: " ^ namespace;
          "data:";
        ]
       @ List.map (fun (key, value) -> "  " ^ key ^ ": \"" ^ value ^ "\"") data))

(** Render a ServiceAccount for a component that needs its own identity. *)
let render_service_account namespace (component : workload_component) =
  match service_account_name component with
  | None -> None
  | Some name ->
      Some
        (String.concat "\n"
           [
             "apiVersion: v1";
             "kind: ServiceAccount";
             "metadata:";
             "  name: " ^ name;
             "  namespace: " ^ namespace;
           ])

(** Render the primary Kubernetes object requested by [component.kind]. *)
let render_primary namespace (component : workload_component) =
  match component.kind with
  | Deployment -> render_deployment namespace component
  | Job -> render_job namespace component
  | CronJob -> render_cron_job namespace component
  | Service -> Option.value (render_service namespace component) ~default:""
  | ConfigMap -> Option.value (render_config_map namespace component) ~default:""
  | ServiceAccount -> Option.value (render_service_account namespace component) ~default:""

(** Whether a companion object should be attached to one component.

    A component's own [generated_resources] and the spec-wide flag are both
    honoured, because they answer different questions: the flag says what the
    bundle wants by default, the component says what it needs regardless. A
    companion is never attached when it is already the component's primary
    object, which would render it twice. *)
let wants_companion (component : workload_component) (kind : workload_kind) ~(spec_flag : bool) =
  component.kind <> kind && (spec_flag || List.mem kind component.generated_resources)

(** Compile all workload components into manifest records.

    The primary object is always rendered. Services, ConfigMaps, and
    ServiceAccounts are attached per {!wants_companion}. *)
let compile (spec : workload_spec) =
  List.map
    (fun component ->
      {
        component_name = component.name;
        kind = component.kind;
        deployment_yaml = render_primary spec.namespace component;
        service_yaml =
          if wants_companion component Service ~spec_flag:spec.include_services then
            render_service spec.namespace component
          else None;
        config_map_yaml =
          if wants_companion component ConfigMap ~spec_flag:spec.include_config_maps then
            render_config_map spec.namespace component
          else None;
        (* A ServiceAccount has no spec-wide flag: a bundle-wide "give every
           component its own identity" is not a thing anyone wants by default. *)
        service_account_yaml =
          if wants_companion component ServiceAccount ~spec_flag:false then
            render_service_account spec.namespace component
          else None;
      })
    spec.components

(** Combine all non-empty YAML fragments into one multi-document YAML string. *)
let combined_yaml manifests =
  manifests
  |> List.concat_map (fun manifest ->
         [
           Some manifest.deployment_yaml;
           manifest.service_yaml;
           manifest.config_map_yaml;
           manifest.service_account_yaml;
         ])
  |> List.filter_map Fun.id
  |> List.filter (fun section -> String.trim section <> "")
  |> String.concat "\n---\n"

(** Build a complete workload plan with both structured manifests and combined
    YAML. *)
let compile_plan spec =
  let manifests = compile spec in
  { plan_id = spec.plan_id; namespace = spec.namespace; manifests; combined_yaml = combined_yaml manifests }

(* ------------------------------------------------------------------------- *)
(* Docker Compose target                                                      *)
(* ------------------------------------------------------------------------- *)

(** Write a caller's value as a double-quoted YAML scalar for a Compose file.

    Backslash, double quote, newline, tab and carriage return get their YAML
    escapes, and every other control byte gets [\xHH]. A [$] becomes [$$]:
    Compose reads [$NAME] and [${NAME}] as variables of the shell that runs
    [docker compose], and [$$] passes one literal [$] to the container. Other
    bytes, UTF-8 included, are copied. The core-service fallback renderer
    copies this table, and its tests compare the two.

    Example test case:
    {[
      assert (quote_compose_scalar "a\"$b" = "\"a\\\"$$b\"")
    ]} *)
let quote_compose_scalar value =
  let buffer = Buffer.create (String.length value + 2) in
  Buffer.add_char buffer '"';
  String.iter
    (fun c ->
      match c with
      | '\\' -> Buffer.add_string buffer "\\\\"
      | '"' -> Buffer.add_string buffer "\\\""
      | '$' -> Buffer.add_string buffer "$$"
      | '\n' -> Buffer.add_string buffer "\\n"
      | '\t' -> Buffer.add_string buffer "\\t"
      | '\r' -> Buffer.add_string buffer "\\r"
      | c when Char.code c < 0x20 || Char.code c = 0x7f ->
          Buffer.add_string buffer (Printf.sprintf "\\x%02X" (Char.code c))
      | c -> Buffer.add_char buffer c)
    value;
  Buffer.add_char buffer '"';
  Buffer.contents buffer

(** Whether [c] is an ASCII lowercase letter or digit. *)
let is_lower_or_digit c = (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9')

(** Whether [c] is an ASCII digit. *)
let is_digit c = c >= '0' && c <= '9'

(** A Compose service or network name: [[a-z0-9][a-z0-9_.-]*].

    Only names that pass this check are written to the file, so a caller's
    name cannot add a key or end a line. They are still written double-quoted,
    because YAML reads a bare name such as [134], [null], [true] or
    [2024-01-01] as a number, null, a boolean or a date, and Compose refuses a
    key that is not a string. *)
let valid_compose_name name =
  name <> ""
  && is_lower_or_digit name.[0]
  && String.for_all (fun c -> is_lower_or_digit c || c = '-' || c = '_' || c = '.') name

(** An environment variable name: [[A-Za-z_][A-Za-z0-9_]*]. It is written
    double-quoted, like a service name, because YAML reads [NULL] or [True] as
    null or a boolean. *)
let valid_environment_name name =
  let letter c = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || c = '_' in
  name <> "" && letter name.[0] && String.for_all (fun c -> letter c || is_digit c) name

(** A dotted IPv4 address: four parts, each [0] or [[1-9][0-9]{0,2}], at most
    255. IPv6 addresses and host names are refused. *)
let valid_ipv4 address =
  let valid_part part =
    let length = String.length part in
    length >= 1 && length <= 3
    && String.for_all is_digit part
    && (length = 1 || part.[0] <> '0')
    && int_of_string part <= 255
  in
  match String.split_on_char '.' address with
  | [ a; b; c; d ] -> valid_part a && valid_part b && valid_part c && valid_part d
  | _ -> false

(** A TCP port number: 1 to 65535. *)
let valid_port port = port >= 1 && port <= 65535

(** The name of the network every service joins. *)
let compose_network_name = function ProjectNetwork name | ExternalNetwork name -> name

(** Whether [component] reaches itself through dependencies on services in
    [components].

    An edge goes from a component to the first component with each dependency's
    name. A dependency on itself is skipped, because it is reported as
    [SelfDependency]. A component that only leads into a cycle is not in it. *)
let in_dependency_cycle (components : workload_component list) (component : workload_component) =
  let find name = List.find_opt (fun (c : workload_component) -> c.name = name) components in
  let successors (c : workload_component) =
    List.filter_map
      (fun d -> if d.dependency_service = c.name then None else find d.dependency_service)
      c.service_dependencies
  in
  let rec visit visited = function
    | [] -> false
    | (c : workload_component) :: rest ->
        if c.name = component.name then true
        else if List.mem c.name visited then visit visited rest
        else visit (c.name :: visited) (successors c @ rest)
  in
  visit [] (successors component)

(** The issues with the spec as a whole: no services, an invalid network name,
    and each invalid name in [existing_services]. *)
let spec_issue_reasons (target : compose_target) (spec : workload_spec) =
  let network = compose_network_name target.compose_network in
  (if spec.components = [] then [ NoServices ] else [])
  @ (if valid_compose_name network then [] else [ InvalidNetworkName network ])
  @ List.filter_map
      (fun name -> if valid_compose_name name then None else Some (InvalidServiceName name))
      target.existing_services

(** The kinds Compose can run: a long-running service or a one-shot job. *)
let kind_issue_reasons (component : workload_component) =
  match component.kind with
  | Deployment | Job -> []
  | (CronJob | Service | ConfigMap | ServiceAccount) as kind -> [ NoComposeEquivalent kind ]

(** Each invalid environment variable name, then each name used twice. *)
let environment_issue_reasons (component : workload_component) =
  let names = List.map (fun (variable : env_var) -> variable.name) component.env in
  let rec repeated seen = function
    | [] -> []
    | name :: rest ->
        if List.mem name seen then DuplicateEnvironmentName name :: repeated seen rest
        else repeated (name :: seen) rest
  in
  List.filter_map (fun name -> if valid_environment_name name then None else Some (InvalidEnvironmentName name)) names
  @ repeated [] names

(** The issues with a component's ports, and the host ports in use after them.

    [used_host_ports] holds the valid host ports of earlier published ports in
    the plan. Two ports on one host port clash even on different host IPs. *)
let port_issue_reasons used_host_ports (component : workload_component) =
  List.fold_left
    (fun (reasons, used) (port : port) ->
      let container = if valid_port port.container_port then [] else [ InvalidPort port.container_port ] in
      match port.publication with
      | NotPublished -> (reasons @ container, used)
      | PublishedOn { host_ip; host_port } ->
          let address = if valid_ipv4 host_ip then [] else [ InvalidHostIp host_ip ] in
          let host, used =
            if not (valid_port host_port) then ([ InvalidPort host_port ], used)
            else if List.mem host_port used then ([ HostPortConflict host_port ], used)
            else ([], used @ [ host_port ])
          in
          (reasons @ container @ address @ host, used))
    ([], used_host_ports) component.ports

(** An empty health check command, then timing Compose would refuse. *)
let health_check_issue_reasons (component : workload_component) =
  match component.health_check with
  | None -> []
  | Some check ->
      (match check.health_test with
      | ExecTest [] | ShellTest "" -> [ EmptyHealthCheck ]
      | ExecTest _ | ShellTest _ -> [])
      @
      if check.interval_seconds < 1 || check.timeout_seconds < 1 || check.retries < 1 || check.start_period_seconds < 0
      then [ InvalidHealthCheckTiming ]
      else []

(** The issues with a component's dependencies, in its order.

    A dependency may name another component of the spec. It may name a
    service of the running project only when the file is merged into that
    project: a file that is its own project cannot reach it. *)
let dependency_issue_reasons (target : compose_target) (spec : workload_spec) (component : workload_component) =
  let in_spec name = List.exists (fun (c : workload_component) -> c.name = name) spec.components in
  let rec check seen = function
    | [] -> []
    | dependency :: rest ->
        let name = dependency.dependency_service in
        let found =
          if name = component.name then [ SelfDependency ]
          else if List.mem name seen then [ DuplicateDependency name ]
          else if in_spec name then []
          else if List.mem name target.existing_services then
            match target.compose_placement with
            | MergedProject -> []
            | OwnProject -> [ DependencyOutsideProject name ]
          else [ UnknownDependency name ]
        in
        found @ check (name :: seen) rest
  in
  check [] component.service_dependencies

(** Every issue that stops [spec] from becoming a Compose file, in the
    documented order: the spec's own issues, then each component's, in
    request order. *)
let compose_issues (target : compose_target) (spec : workload_spec) =
  let for_spec reason = { issue_service = None; issue_reason = reason } in
  let component_issues (earlier : workload_component list) used_host_ports (component : workload_component) =
    let ports, used_host_ports = port_issue_reasons used_host_ports component in
    let reasons =
      (if valid_compose_name component.name then [] else [ InvalidServiceName component.name ])
      @ (if List.exists (fun (c : workload_component) -> c.name = component.name) earlier then
           [ DuplicateServiceName ]
         else [])
      @ (if List.mem component.name target.existing_services then [ ServiceNameInUse ] else [])
      @ (if component.image = "" then [ EmptyImage ] else [])
      @ kind_issue_reasons component
      @ (if component.replicas > 1 then [ UnsupportedReplicas component.replicas ] else [])
      @ environment_issue_reasons component
      @ ports
      @ health_check_issue_reasons component
      @ dependency_issue_reasons target spec component
      @ if in_dependency_cycle spec.components component then [ DependencyCycle ] else []
    in
    (List.map (fun reason -> { issue_service = Some component.name; issue_reason = reason }) reasons, used_host_ports)
  in
  let _, _, issues =
    List.fold_left
      (fun (earlier, used_host_ports, issues) component ->
        let found, used_host_ports = component_issues earlier used_host_ports component in
        (earlier @ [ component ], used_host_ports, issues @ found))
      ([], [], []) spec.components
  in
  List.map for_spec (spec_issue_reasons target spec) @ issues

(** Write values as a YAML flow list: [[a, b]]. *)
let flow_list values = "[" ^ String.concat ", " values ^ "]"

(** Write a duration in seconds as Compose reads it. *)
let seconds value = string_of_int value ^ "s"

(** The restart policy for a component's kind: a job runs once. *)
let compose_restart_policy = function
  | Job -> "\"no\""
  | Deployment | CronJob | Service | ConfigMap | ServiceAccount -> "unless-stopped"

(** Render one component as the lines of its Compose service, without the
    indentation of the [services] block. Keys are written only when the
    component has a value for them, in a fixed order. *)
let render_compose_service network (component : workload_component) =
  let exposed = List.filter (fun (port : port) -> port.publication = NotPublished) component.ports in
  let published =
    List.filter_map
      (fun (port : port) ->
        match port.publication with
        | NotPublished -> None
        | PublishedOn { host_ip; host_port } -> Some (host_ip, host_port, port.container_port))
      component.ports
  in
  let section key lines = match lines with [] -> [] | _ -> ("  " ^ key ^ ":") :: lines in
  [ quote_compose_scalar component.name ^ ":"; "  image: " ^ quote_compose_scalar component.image ]
  @ (match component.args with
    | [] -> []
    | args -> [ "  command: " ^ flow_list (List.map quote_compose_scalar args) ])
  @ section "env_file" (List.map (fun path -> "    - " ^ quote_compose_scalar path) component.env_files)
  @ section "environment"
      (List.map
         (fun (variable : env_var) ->
           "    " ^ quote_compose_scalar variable.name ^ ": " ^ quote_compose_scalar variable.value)
         component.env)
  @ section "expose" (List.map (fun (port : port) -> "    - \"" ^ string_of_int port.container_port ^ "\"") exposed)
  @ section "ports"
      (List.map
         (fun (host_ip, host_port, container_port) ->
           "    - \"" ^ host_ip ^ ":" ^ string_of_int host_port ^ ":" ^ string_of_int container_port ^ "\"")
         published)
  @ (match component.health_check with
    | None -> []
    | Some check ->
        let test =
          match check.health_test with
          | ExecTest parts -> flow_list ("\"CMD\"" :: List.map quote_compose_scalar parts)
          | ShellTest line -> flow_list [ "\"CMD-SHELL\""; quote_compose_scalar line ]
        in
        [
          "  healthcheck:";
          "    test: " ^ test;
          "    interval: " ^ seconds check.interval_seconds;
          "    timeout: " ^ seconds check.timeout_seconds;
          "    retries: " ^ string_of_int check.retries;
          "    start_period: " ^ seconds check.start_period_seconds;
        ])
  @ section "depends_on"
      (List.concat_map
         (fun dependency ->
           [
             "    " ^ quote_compose_scalar dependency.dependency_service ^ ":";
             "      condition: " ^ string_of_dependency_condition dependency.dependency_condition;
           ])
         component.service_dependencies)
  @ [ "  networks:"; "    - " ^ quote_compose_scalar network; "  restart: " ^ compose_restart_policy component.kind ]

(** Render a spec that has no issues as one Compose file. *)
let render_compose_file (target : compose_target) (spec : workload_spec) =
  let network = compose_network_name target.compose_network in
  let networks =
    match target.compose_network with
    | ProjectNetwork name -> [ "networks:"; "  " ^ quote_compose_scalar name ^ ": {}" ]
    | ExternalNetwork name -> [ "networks:"; "  " ^ quote_compose_scalar name ^ ":"; "    external: true" ]
  in
  String.concat "\n"
    (("services:" :: List.concat_map (fun c -> indent "  " (render_compose_service network c)) spec.components)
    @ networks)

(** Render [spec] as a Compose file for [target], or report every issue that
    stops it. Never raises. *)
let compile_compose_plan (target : compose_target) (spec : workload_spec) =
  let issues = compose_issues target spec in
  {
    compose_plan_id = spec.plan_id;
    compose_service_names = List.map (fun (c : workload_component) -> c.name) spec.components;
    compose_issues = issues;
    compose_yaml = (match issues with [] -> Some (render_compose_file target spec) | _ :: _ -> None);
  }

(** Render [spec] for the target it names. *)
let compile_for_target (spec : workload_spec) =
  match spec.target with
  | Kubernetes -> KubernetesPlan (compile_plan spec)
  | Compose target -> ComposePlan (compile_compose_plan target spec)
