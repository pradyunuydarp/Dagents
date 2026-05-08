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

(** Return pipeline steps in dependency-safe execution order.

    Inputs:
    - [pipeline_definition]: possibly unordered step DAG.

    Output:
    - [pipeline_step list]: topologically ordered steps where every dependency
      appears before the dependent step.

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
