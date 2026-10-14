(** Pipeline compiler implementation.

    The compiler takes a declarative pipeline DAG and turns it into an ordered
    plan that the pipeline service can dispatch. It validates graph structure
    locally so runtime workers receive an already-normalized execution order. *)

open Dagents_common_ir

(** Map specialized to step ids. Used for O(log n) dependency lookup. *)
module StepMap = Map.Make (String)
(** Set specialized to step ids. Used for the steps the walk has finished. *)
module StepSet = Set.Make (String)

(** Raised inside the graph walk to stop at the first error. It never leaves
    this module: [ordered_steps_result] turns it into an [Error]. *)
exception Graph_error of pipeline_graph_error

(** Build an index from step id to step while rejecting duplicate ids.

    Inputs: user-authored [pipeline_step list].
    Output: immutable [StepMap.t] lookup table.
    Raises: [Graph_error (DuplicateStepId id)] if two steps share an id. *)
let build_index (steps : pipeline_step list) : pipeline_step StepMap.t =
  List.fold_left
    (fun index (step : pipeline_step) ->
      if StepMap.mem step.step_id index then raise (Graph_error (DuplicateStepId step.step_id));
      StepMap.add step.step_id step index)
    StepMap.empty steps

(** Topologically order pipeline steps, or report why the graph has no order.

    Inputs: [pipeline_definition] with dependency references.
    Output: [Ok steps], ordered so dependencies precede dependents, or
    [Error] with the first duplicate id, unknown dependency or cycle found. *)
let ordered_steps_result (definition : pipeline_definition) =
  let steps : pipeline_step list = definition.steps in
  (* [ordered] is accumulated in reverse DFS completion order, then reversed
     once at the end for execution order. *)
  let ordered : pipeline_step list ref = ref [] in
  (* [path] is the current recursion stack, newest step first. Seeing a step
     that is already on it means the graph has a cycle, and the part of the
     stack above that step is the cycle. *)
  let path : string list ref = ref [] in
  (* [visited] prevents re-processing shared dependencies. *)
  let visited = ref StepSet.empty in
  let cycle_from step_id =
    let rec take acc = function
      | [] -> acc
      | id :: rest -> if id = step_id then id :: acc else take (id :: acc) rest
    in
    take [] !path @ [ step_id ]
  in
  let rec visit index (step : pipeline_step) =
    if StepSet.mem step.step_id !visited then ()
    else if List.mem step.step_id !path then raise (Graph_error (CyclicDependency (cycle_from step.step_id)))
    else (
      path := step.step_id :: !path;
      List.iter
        (fun dependency ->
          match StepMap.find_opt dependency index with
          | Some dependency_step -> visit index dependency_step
          | None -> raise (Graph_error (UnknownDependency { step_id = step.step_id; dependency })))
        step.depends_on;
      (* Once all dependencies are complete, move the step from the recursion
         stack to the final visited set. *)
      path := List.tl !path;
      visited := StepSet.add step.step_id !visited;
      ordered := step :: !ordered)
  in
  match
    (let index = build_index steps in
     List.iter (visit index) steps)
  with
  | () -> Ok (List.rev !ordered)
  | exception Graph_error error -> Error error

(** Topologically order pipeline steps.

    Raises: [Invalid_argument] with [describe_pipeline_graph_error] for a
    duplicate id, an unknown dependency or a cycle. *)
let ordered_steps (definition : pipeline_definition) =
  match ordered_steps_result definition with
  | Ok ordered -> ordered
  | Error error -> invalid_arg (describe_pipeline_graph_error error)

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

(** Assign execution targets to steps that are already in dependency order. *)
let lower (definition : pipeline_definition) (ordered : pipeline_step list) =
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

(** Compile a pipeline definition, or report why its graph has no order. *)
let compile_result (definition : pipeline_definition) =
  Result.map (lower definition) (ordered_steps_result definition)

(** Compile a pipeline definition into an ordered execution plan. *)
let compile (definition : pipeline_definition) = lower definition (ordered_steps definition)
