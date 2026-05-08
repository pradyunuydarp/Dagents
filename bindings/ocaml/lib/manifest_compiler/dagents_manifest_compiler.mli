(** Kubernetes manifest compiler public interface.

    This module lowers declarative Dagents workload specs into YAML fragments
    for Kubernetes objects. It is a compiler only: it renders manifests but does
    not apply them to a cluster. *)

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
