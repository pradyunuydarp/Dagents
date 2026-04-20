open Dagents_common_ir

val compile : workload_spec -> workload_manifest list
val combined_yaml : workload_manifest list -> string
val compile_plan : workload_spec -> workload_plan
