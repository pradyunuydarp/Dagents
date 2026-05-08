(** Model router implementation.

    The router is a pure planner that converts a dataset profile and requested
    task into a route plan. It keeps model selection deterministic and makes the
    packaging decision explicit for service orchestration. *)

open Dagents_common_ir

(** Select model candidates and runtime packaging.

    Inputs:
    - [profile]: dataset characteristics and any dataset-compiler suggestions.
    - [task_type]: workload requested by the caller.

    Output:
    - [route_plan]: ordered candidates, selected first candidate, and packaging
      mode.

    Example test case:
    {[
      let plan = route profile Classification in
      assert (List.mem plan.selected_model plan.candidates)
    ]} *)
let route profile task_type =
  (* Task-specific candidate lists provide a default routing policy. Anomaly
     detection prefers dataset-profile suggestions because the dataset compiler
     has already inspected strategy and field shape. *)
  let candidates =
    match task_type with
    | Forecasting -> [ Gru; Lstm; Transformer ]
    | Classification -> [ RandomForest; Xgboost; NaiveBayes ]
    | Regression -> [ Linear; RandomForest; Xgboost ]
    | Embedding -> [ Transformer; Custom ]
    | AnomalyDetection -> (
        match profile.suggested_models with
        | [] -> [ Autoencoder; VariationalAutoencoder; RandomForest ]
        | models -> models )
  in
  (* The first candidate is the deterministic default. The empty-list fallback
     keeps the route total even if future candidate logic becomes configurable. *)
  let selected_model =
    match candidates with
    | candidate :: _ -> candidate
    | [] -> Custom
  in
  (* Packaging balances latency and workload size: lightweight tasks can be
     called inline, large anomaly/regression jobs become Kubernetes jobs, and
     forecasting is treated as a long-running deployment. *)
  let packaging_mode =
    match task_type with
    | Forecasting -> LongRunningDeployment
    | Classification | Embedding -> InlineServiceCall
    | Regression | AnomalyDetection ->
        if profile.record_count > 10_000 then KubernetesJobExecution else InlineServiceCall
  in
  { task_type; selected_model; candidates; packaging_mode }
