(** Manifest compiler public interface.

    This module lowers declarative Dagents workload specs into YAML fragments
    for Kubernetes objects, or into one Docker Compose file. It is a compiler
    only: it renders files but does not apply them to a cluster or start
    containers. The Kubernetes functions ignore [spec.target];
    {!compile_for_target} reads it. *)

open Dagents_common_ir

(** Compile a workload spec into per-component manifests.

    Inputs:
    - [workload_spec]: namespace, components, and flags for service/config-map
      generation.

    Output:
    - [workload_manifest list]: rendered primary object YAML plus optional
      Service and ConfigMap YAML for each component.

    Example test case:
    {[
      let manifests = compile spec in
      assert (List.length manifests = List.length spec.components)
    ]} *)
val compile : workload_spec -> workload_manifest list

(** Combine rendered manifest fragments into one YAML document stream.

    Inputs:
    - [workload_manifest list]: manifests produced by [compile].

    Output:
    - [string]: non-empty YAML sections joined by [---].

    Example test case:
    {[
      let yaml = combined_yaml manifests in
      assert (String.length yaml > 0)
    ]} *)
val combined_yaml : workload_manifest list -> string

(** Compile a full workload plan.

    Inputs:
    - [workload_spec]: declarative manifest-generation request.

    Output:
    - [workload_plan]: plan id, namespace, per-component manifests, and combined
      YAML suitable for display or downstream apply steps. *)
val compile_plan : workload_spec -> workload_plan

(** Write a caller's value as a double-quoted YAML scalar for a Compose file.

    Escapes backslash, double quote, newline, tab and carriage return, writes
    other control bytes as [\xHH], and doubles [$] so Compose passes it to the
    container instead of reading a variable.

    Example test case:
    {[
      assert (quote_compose_scalar "$HOME" = "\"$$HOME\"")
    ]} *)
val quote_compose_scalar : string -> string

(** Render a workload spec as one Docker Compose file.

    Inputs:
    - [compose_target]: placement, network and the services of the running
      project.
    - [workload_spec]: the components; [namespace], [include_services],
      [include_config_maps] and [target] are not read.

    Output:
    - [compose_plan]: the plan id, every component name in request order, the
      issues in the documented order, and the file. The file is [None] exactly
      when there are issues. A spec that cannot become a Compose file is
      reported, not raised.

    Example test case:
    {[
      let plan = compile_compose_plan default_compose_target { spec with components = [] } in
      assert (plan.compose_yaml = None && List.length plan.compose_issues = 1)
    ]} *)
val compile_compose_plan : compose_target -> workload_spec -> compose_plan

(** Render [spec] for the target it names: [KubernetesPlan (compile_plan spec)]
    or [ComposePlan (compile_compose_plan target spec)]. *)
val compile_for_target : workload_spec -> target_plan
