open Dagents_common_ir

val infer_schema : record list -> record_schema_field list
val metadata_of_source : source_spec -> source_metadata
val validate_source : source_spec -> source_validation_result
val compile_extraction_plan : ?partition_count:int -> source_spec -> extraction_plan

val build_profile :
  scope_id:string ->
  scope_kind:scope_kind ->
  extraction_strategy:extraction_strategy ->
  ?feature_fields:string list ->
  ?label_field:string ->
  ?batch_size:int ->
  record list ->
  dataset_profile

val validate_schema_contract : schema_contract -> record_schema_field list -> schema_validation_report
val evaluate_quality_rule : record list -> quality_rule -> quality_result
val evaluate_quality_rules : record list -> quality_rule list -> quality_result list
val transform_schema : transform_operation list -> record_schema_field list -> record_schema_field list
val compile_transform_plan : plan_id:string -> transform_operation list -> record list -> transform_plan
val apply_transform_plan : transform_plan -> record list -> record list
