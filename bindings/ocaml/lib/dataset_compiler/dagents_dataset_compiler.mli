open Dagents_common_ir

val build_profile :
  scope_id:string ->
  scope_kind:scope_kind ->
  extraction_strategy:extraction_strategy ->
  ?feature_fields:string list ->
  ?label_field:string ->
  ?batch_size:int ->
  record list ->
  dataset_profile

