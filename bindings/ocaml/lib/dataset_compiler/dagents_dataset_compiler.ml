open Dagents_common_ir

let first_non_null field records =
  let rec loop = function
    | [] -> VNull
    | record :: rest -> (
        match List.assoc_opt field record with
        | Some VNull | None -> loop rest
        | Some value -> value )
  in
  loop records

let infer_feature_fields records label_field =
  match records with
  | [] -> []
  | first :: _ ->
      first
      |> List.map fst
      |> List.filter (fun field -> Some field <> label_field)

let suggest_models extraction_strategy label_field numeric_fields =
  match extraction_strategy with
  | TimeSeries -> [ Gru; Lstm; Autoencoder ]
  | Text -> [ NaiveBayes; Gru; Transformer ]
  | Hybrid -> [ Transformer; RandomForest; Custom ]
  | Tabular -> (
      match (label_field, numeric_fields) with
      | Some _, _ -> [ RandomForest; Xgboost; NaiveBayes ]
      | None, _ :: _ -> [ Autoencoder; VariationalAutoencoder; RandomForest ]
      | None, [] -> [ Transformer; Custom ] )

let classify_fields feature_fields records =
  List.fold_left
    (fun (numeric_fields, categorical_fields) field ->
      match first_non_null field records with
      | VInt _ | VFloat _ -> (field :: numeric_fields, categorical_fields)
      | _ -> (numeric_fields, field :: categorical_fields))
    ([], []) feature_fields

let build_profile ~scope_id ~scope_kind ~extraction_strategy ?feature_fields ?label_field ?(batch_size = 1000) records =
  let feature_fields =
    match feature_fields with
    | Some fields when fields <> [] -> fields
    | _ -> infer_feature_fields records label_field
  in
  let numeric_fields, categorical_fields = classify_fields feature_fields records in
  let record_count = List.length records in
  let partition_count =
    if record_count = 0 then 0 else max 1 ((record_count + batch_size - 1) / batch_size)
  in
  {
    scope_id;
    scope_kind;
    extraction_strategy;
    record_count;
    feature_fields;
    label_field;
    numeric_fields = List.rev numeric_fields;
    categorical_fields = List.rev categorical_fields;
    partition_count;
    suggested_models = suggest_models extraction_strategy label_field numeric_fields;
  }

