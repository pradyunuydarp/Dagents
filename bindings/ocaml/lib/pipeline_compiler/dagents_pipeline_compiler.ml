(** Pipeline compiler implementation.

    The compiler takes a declarative pipeline DAG and turns it into an ordered
    plan that the pipeline service can dispatch. It validates graph structure
    locally so runtime workers receive an already-normalized execution order. *)

open Dagents_common_ir

(** Map specialized to step ids. Used for O(log n) dependency lookup. *)
module StepMap = Map.Make (String)
(** Set specialized to step ids. Used to track DFS state during cycle checks. *)
module StepSet = Set.Make (String)

(** Build an index from step id to step while rejecting duplicate ids.

    Inputs: user-authored [pipeline_step list].
    Output: immutable [StepMap.t] lookup table.
    Raises: [Invalid_argument] if two steps share the same [step_id].

    Example test case:
    {[
      ignore (build_index [ step_a; step_b ])
    ]} *)
let build_index (steps : pipeline_step list) : pipeline_step StepMap.t =
  List.fold_left
    (fun index (step : pipeline_step) ->
      if StepMap.mem step.step_id index then invalid_arg ("Duplicate step id: " ^ step.step_id);
      StepMap.add step.step_id step index)
    StepMap.empty steps

(** Topologically order pipeline steps.

    Inputs: [pipeline_definition] with dependency references.
    Output: list ordered so dependencies precede dependents.
    Raises: [Invalid_argument] for cycles or unknown dependency ids. *)
let ordered_steps (definition : pipeline_definition) =
  let steps : pipeline_step list = definition.steps in
  let index = build_index steps in
  (* [ordered] is accumulated in reverse DFS completion order, then reversed
     once at the end for execution order. *)
  let ordered : pipeline_step list ref = ref [] in
  (* [visiting] tracks the current recursion stack. Seeing the same id twice
     in this set means the graph has a cycle. *)
  let visiting = ref StepSet.empty in
  (* [visited] prevents re-processing shared dependencies. *)
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
      (* Mark the step as being explored before visiting its dependencies. *)
      visiting := StepSet.add step_id !visiting;
      List.iter visit step.depends_on;
      (* Once all dependencies are complete, move the step from the recursion
         stack to the final visited set. *)
      visiting := StepSet.remove step_id !visiting;
      visited := StepSet.add step_id !visited;
      ordered := step :: !ordered
  in
  List.iter (fun (step : pipeline_step) -> visit step.step_id) steps;
  List.rev !ordered

(** Validate graph shape by attempting to order it and discarding the result. *)
let validate (definition : pipeline_definition) =
  ignore (ordered_steps definition)

(** Assign a runtime target for a step kind.

    Model execution is routed to Python because model training/inference lives
    outside this functional kernel. Current lightweight pipeline operations run
    locally. *)
let execution_target_for_step = function
  | RunModelJob -> PythonService
  | ProfileDataset -> LocalProcess
  | EnrichContext | FilterItems | SummarizeItems | ProjectFields -> LocalProcess

(** Compile a pipeline definition into an ordered execution plan. *)
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
