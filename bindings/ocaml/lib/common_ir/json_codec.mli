open Dagents_common_ir

val workload_spec_of_yojson : Yojson.Safe.t -> workload_spec
val yojson_of_workload_manifest : workload_manifest -> Yojson.Safe.t
val yojson_of_workload_plan : workload_plan -> Yojson.Safe.t

val pipeline_definition_of_yojson : Yojson.Safe.t -> pipeline_definition
val yojson_of_pipeline : compiled_pipeline -> Yojson.Safe.t

val dataset_profile_to_yojson : dataset_profile -> Yojson.Safe.t
val route_plan_to_yojson : route_plan -> Yojson.Safe.t
