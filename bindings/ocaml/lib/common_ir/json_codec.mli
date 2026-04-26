open Dagents_common_ir

val workload_spec_of_yojson : Yojson.Safe.t -> workload_spec
val yojson_of_workload_manifest : workload_manifest -> Yojson.Safe.t
val yojson_of_workload_plan : workload_plan -> Yojson.Safe.t

val source_spec_of_yojson : Yojson.Safe.t -> source_spec
val yojson_of_source_validation_result : source_validation_result -> Yojson.Safe.t
val yojson_of_source_metadata : source_metadata -> Yojson.Safe.t
val yojson_of_extraction_plan : extraction_plan -> Yojson.Safe.t
val schema_contract_of_yojson : Yojson.Safe.t -> schema_contract
val quality_rule_of_yojson : Yojson.Safe.t -> quality_rule
val transform_operations_of_yojson : Yojson.Safe.t -> transform_operation list
val yojson_of_schema_validation_report : schema_validation_report -> Yojson.Safe.t
val yojson_of_quality_result : quality_result -> Yojson.Safe.t
val yojson_of_transform_plan : transform_plan -> Yojson.Safe.t
val yojson_of_records : record list -> Yojson.Safe.t
val records_of_yojson : Yojson.Safe.t -> record list

val pipeline_definition_of_yojson : Yojson.Safe.t -> pipeline_definition
val yojson_of_pipeline : compiled_pipeline -> Yojson.Safe.t

val dataset_profile_to_yojson : dataset_profile -> Yojson.Safe.t
val route_plan_to_yojson : route_plan -> Yojson.Safe.t
