(** Model router public interface.

    This module chooses candidate model families and a packaging mode from a
    dataset profile plus requested ML task. It plans what should run; it does
    not train or invoke the selected model. *)

open Dagents_common_ir

(** Route a profiled dataset to a model plan.

    Inputs:
    - [dataset_profile]: record count, suggested models, and feature shape.
    - [task_type]: requested ML task.

    Output:
    - [route_plan]: ordered model candidates, selected first-choice model, and
      runtime packaging mode.

    Example test case:
    {[
      let plan = route profile AnomalyDetection in
      assert (plan.selected_model = List.hd plan.candidates)
    ]} *)
val route : dataset_profile -> task_type -> route_plan
