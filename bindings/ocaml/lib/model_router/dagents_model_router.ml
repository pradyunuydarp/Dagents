open Dagents_common_ir

let route profile task_type =
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
  let selected_model =
    match candidates with
    | candidate :: _ -> candidate
    | [] -> Custom
  in
  let packaging_mode =
    match task_type with
    | Forecasting -> LongRunningDeployment
    | Classification | Embedding -> InlineServiceCall
    | Regression | AnomalyDetection ->
        if profile.record_count > 10_000 then KubernetesJobExecution else InlineServiceCall
  in
  { task_type; selected_model; candidates; packaging_mode }
