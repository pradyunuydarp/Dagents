open Dagents_common_ir

val validate : pipeline_definition -> unit
val ordered_steps : pipeline_definition -> pipeline_step list
val compile : pipeline_definition -> compiled_pipeline
