open Dagents_common_ir

module StepMap = Map.Make (String)
module StepSet = Set.Make (String)

let build_index (steps : pipeline_step list) : pipeline_step StepMap.t =
  List.fold_left
    (fun index (step : pipeline_step) ->
      if StepMap.mem step.step_id index then invalid_arg ("Duplicate step id: " ^ step.step_id);
      StepMap.add step.step_id step index)
    StepMap.empty steps

let ordered_steps (definition : pipeline_definition) =
  let steps : pipeline_step list = definition.steps in
  let index = build_index steps in
  let ordered : pipeline_step list ref = ref [] in
  let visiting = ref StepSet.empty in
  let visited = ref StepSet.empty in
  let rec visit step_id =
    if StepSet.mem step_id !visited then ()
    else if StepSet.mem step_id !visiting then invalid_arg ("Cyclic dependency detected at " ^ step_id)
    else
      let step =
        match StepMap.find_opt step_id index with
        | Some step -> step
        | None -> invalid_arg ("Unknown dependency step id: " ^ step_id)
      in
      visiting := StepSet.add step_id !visiting;
      List.iter visit step.depends_on;
      visiting := StepSet.remove step_id !visiting;
      visited := StepSet.add step_id !visited;
      ordered := step :: !ordered
  in
  List.iter (fun (step : pipeline_step) -> visit step.step_id) steps;
  List.rev !ordered

let validate (definition : pipeline_definition) =
  ignore (ordered_steps definition)

let execution_target_for_step = function
  | RunModelJob -> PythonService
  | ProfileDataset -> LocalProcess
  | EnrichContext | FilterItems | SummarizeItems | ProjectFields -> LocalProcess

let compile (definition : pipeline_definition) =
  let ordered : pipeline_step list = ordered_steps definition in
  let steps : compiled_pipeline_step list =
    List.map
      (fun (step : pipeline_step) ->
        {
          step_id = step.step_id;
          kind = step.kind;
          depends_on = step.depends_on;
          execution_target = execution_target_for_step step.kind;
          config_json = step.config_json;
        })
      ordered
  in
  { pipeline_id = definition.pipeline_id; steps }
