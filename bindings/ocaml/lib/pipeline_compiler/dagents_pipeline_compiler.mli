(** Pipeline compiler public interface.

    This module validates a user-authored pipeline DAG and lowers it into an
    execution plan. It is intentionally pure: it checks ordering and assigns
    execution targets, but it does not run pipeline steps. *)

open Dagents_common_ir

(** Validate a pipeline definition.

    Inputs:
    - [pipeline_definition]: pipeline id and unordered step list.

    Output:
    - [unit] when the graph is valid.

    Raises:
    - [Invalid_argument] for duplicate step ids, unknown dependencies, or
      cyclic dependencies.

    Example test case:
    {[
      validate { pipeline_id = "p"; steps = [ step_a; step_b ] }
    ]} *)
val validate : pipeline_definition -> unit

(** Return pipeline steps in dependency-safe execution order, or the reason
    the graph has none.

    Inputs:
    - [pipeline_definition]: possibly unordered step DAG.

    Output:
    - [Ok steps]: topologically ordered steps where every dependency appears
      before the dependent step. The order comes from a depth-first walk over
      the steps in the order they were declared, so it is deterministic.
    - [Error error]: the first duplicate id, unknown dependency or cycle,
      found by walking the steps in the order they were declared.

    Example test case:
    {[
      match ordered_steps_result definition with
      | Error (CyclicDependency cycle) -> assert (List.hd cycle = List.nth cycle (List.length cycle - 1))
      | _ -> ()
    ]} *)
val ordered_steps_result : pipeline_definition -> (pipeline_step list, pipeline_graph_error) result

(** Return pipeline steps in dependency-safe execution order.

    Inputs:
    - [pipeline_definition]: possibly unordered step DAG.

    Output:
    - [pipeline_step list]: topologically ordered steps where every dependency
      appears before the dependent step.

    Raises:
    - [Invalid_argument] with the [describe_pipeline_graph_error] message when
      [ordered_steps_result] returns an error.

    Example test case:
    {[
      let ordered = ordered_steps definition in
      assert ((List.hd ordered).depends_on = [])
    ]} *)
val ordered_steps : pipeline_definition -> pipeline_step list

(** Compile a pipeline into ordered executable steps.

    Inputs:
    - [pipeline_definition]: validated or unvalidated pipeline DAG.

    Output:
    - [compiled_pipeline]: ordered steps with execution targets assigned.

    Raises:
    - [Invalid_argument] with the same graph errors as [validate]. *)
val compile : pipeline_definition -> compiled_pipeline

(** Compile a pipeline, or report why its graph has no valid order.

    The CLI uses this function. It prints the error as a JSON report, so a
    service can tell its caller what is wrong with the pipeline. *)
val compile_result : pipeline_definition -> (compiled_pipeline, pipeline_graph_error) result
