(** Dataset compiler implementation.

    The functions in this module are pure planning helpers for Dagents source
    workloads. They convert loosely structured records and declarative source
    specs into typed plans and reports that service adapters can execute.

    No function here performs database I/O, object-storage I/O, or model
    training. Keeping this layer side-effect free makes it suitable for the
    OCaml functional kernel: inputs are records/specs, outputs are explicit
    plans/reports, and failures are localized to validation errors. *)

open Dagents_common_ir

(** Set module used to collect field names deterministically while inferring
    schema. The functor application specializes OCaml's immutable Set to
    strings. *)
module StringSet = Set.Make (String)

(** Map one scalar IR value to the logical dtype string used in schema fields.

    Inputs: [value] constructor.
    Output: dtype string such as ["int"], ["float"], or ["null"].

    Example test case:
    {[
      assert (dtype_of_value (VBool true) = "bool")
    ]} *)
let dtype_of_value = function
  | VString _ -> "string"
  | VInt _ -> "int"
  | VFloat _ -> "float"
  | VBool _ -> "bool"
  | VNull -> "null"

(** Decide whether an actual field dtype satisfies an expected contract dtype.

    Inputs:
    - [expected]: dtype declared by a schema contract.
    - [actual]: dtype inferred from records.

    Output: [true] when the actual dtype is acceptable.

    Example test case:
    {[
      assert (compatible_dtype ~expected:"float" ~actual:"int")
    ]} *)
let compatible_dtype ~expected ~actual =
  (* Nulls are accepted at the schema layer because presence and nullability
     are enforced by quality rules, where severities can be applied. *)
  expected = actual || (expected = "float" && actual = "int") || actual = "null"

(** Find the first meaningful value for a field across records.

    Inputs:
    - [field]: field name to inspect.
    - [records]: candidate records.

    Output: first non-null value, or [VNull] when the field is missing/null in
    all records. *)
let first_non_null field records =
  let rec loop = function
    | [] -> VNull
    | record :: rest -> (
        match List.assoc_opt field record with
        | Some VNull | None -> loop rest
        | Some value -> value )
  in
  loop records

(** Infer feature fields from the first record while excluding an optional label.

    The first record establishes the user-facing order because records are
    association lists, not maps. *)
let infer_feature_fields records label_field =
  match records with
  | [] -> []
  | first :: _ ->
      first
      |> List.map fst
      |> List.filter (fun field -> Some field <> label_field)

(** Infer a stable schema from all field names observed in a record list.

    The field-name collection step uses [StringSet] so fields are unique and
    deterministic. The dtype step then scans the records for the first non-null
    sample value for each field. *)
let infer_schema records =
  let fields =
    List.fold_left
      (fun fields record ->
        List.fold_left (fun fields (field, _) -> StringSet.add field fields) fields record)
      StringSet.empty records
    |> StringSet.elements
  in
  List.map
    (fun field ->
      {
        field_name = field;
        dtype =
          (match first_non_null field records with
          | VNull -> "null"
          | value -> dtype_of_value value);
      })
    fields

(** Build non-executing metadata for a source specification.

    Inline sources can be inspected directly. External sources use declared
    projections or schema hints because this compiler layer must not connect to
    databases or object stores. *)
let metadata_of_source (source : source_spec) =
  (* External source schemas are intentionally descriptive rather than
     executable. Runtime adapters still own database inspection and I/O. *)
  let schema =
    match source.selection with
    | InlineSelection records -> infer_schema records
    | PostgresSelection selection ->
        List.map (fun field_name -> { field_name; dtype = "unknown" }) selection.columns
    | MongoSelection selection -> (
        match selection.projection_json with
        | Some (`Assoc fields) ->
            fields
            |> List.filter_map (function
                 | field_name, (`Int 1 | `Bool true) -> Some { field_name; dtype = "unknown" }
                 | _ -> None)
        | _ -> [] )
    | ObjectStorageSelection _ ->
        List.map
          (fun (field_name, dtype) -> { field_name; dtype })
          source.schema_hint
  in
  {
    source_id = source.source_id;
    source_kind = source.source_kind;
    schema;
    estimated_records =
      (match source.selection with
      | InlineSelection records -> Some (List.length records)
      | _ -> source.batching.max_records);
  }

(** Validate a source specification before plan compilation.

    Inputs: complete [source_spec].
    Output: [source_validation_result] with blocking [errors] and non-blocking
    [warnings].

    The function accumulates all structural problems instead of failing at the
    first one, which gives API callers useful diagnostics in a single response. *)
let validate_source (source : source_spec) =
  let errors = ref [] in
  let warnings = ref [] in
  (* Basic source identity and batching checks apply to every connector. *)
  if String.trim source.source_id = "" then errors := "source_id is required" :: !errors;
  if source.batching.batch_size <= 0 then errors := "batch_size must be positive" :: !errors;
  (* External connectors need connection metadata; inline records do not. *)
  (match (source.source_kind, source.connection_ref) with
  | Inline, _ -> ()
  | _, None -> errors := "connection_ref is required for external sources" :: !errors
  | _, Some _ -> ());
  (* Ensure the high-level connector kind and the detailed selection variant
     agree before later compilation pattern matches on the selection. *)
  (match (source.source_kind, source.selection) with
  | Inline, InlineSelection _ -> ()
  | Postgres, PostgresSelection selection ->
      if selection.sql = None && selection.table = None then
        errors := "postgres selection requires sql or table" :: !errors;
      if selection.sql <> None && selection.table <> None then
        warnings := "postgres sql takes precedence over table selection" :: !warnings
  | Mongodb, MongoSelection selection ->
      if String.trim selection.database = "" then errors := "mongo database is required" :: !errors;
      if String.trim selection.collection = "" then errors := "mongo collection is required" :: !errors
  | ObjectStorage, ObjectStorageSelection selection ->
      if selection.uri = None && selection.prefix = None then
        errors := "object storage selection requires uri or prefix" :: !errors
  | _ -> errors := "source_kind and selection kind do not match" :: !errors);
  (* Empty schemas are allowed but surfaced because downstream routing and
     manifest generation become less precise without field information. *)
  let metadata = metadata_of_source source in
  if metadata.schema = [] then warnings := "source schema is not declared or inferable" :: !warnings;
  { valid = !errors = []; errors = List.rev !errors; warnings = List.rev !warnings }

(** Compile a validated source into a connector-neutral extraction plan.

    Inputs:
    - optional [partition_count] fallback for hash partitioning.
    - [source] declarative connector spec.

    Output: [extraction_plan] consumed by LMA/GMA or pipeline runners.

    Example test case:
    {[
      let plan = compile_extraction_plan source in
      assert (plan.extraction_batch_size = source.batching.batch_size)
    ]} *)
let compile_extraction_plan ?(partition_count = 1) (source : source_spec) =
  let validation = validate_source source in
  if not validation.valid then invalid_arg (String.concat "; " validation.errors);
  (* Lower connector-specific selections into a normalized extraction plan
     that LMA/GMA and pipeline runners can dispatch without branching over
     every source kind again. *)
  let selected_fields, predicates, ordering =
    match source.selection with
    | InlineSelection records ->
        (List.map (fun field -> field.field_name) (infer_schema records), [], [])
    | PostgresSelection selection ->
        ( selection.columns,
          Option.to_list selection.where_clause,
          selection.order_by )
    | MongoSelection selection ->
        ( (match selection.projection_json with
          | Some (`Assoc fields) ->
              fields
              |> List.filter_map (function
                   | field, (`Int 1 | `Bool true) -> Some field
                   | _ -> None)
          | _ -> []),
          Option.to_list (Option.map Yojson.Safe.to_string selection.filter_json),
          List.map (fun (sort : selection_sort) -> sort.field ^ ":" ^ sort.direction) selection.sort )
    | ObjectStorageSelection selection ->
        ( List.map fst source.schema_hint,
          List.filter_map Fun.id [ selection.uri; selection.prefix; selection.glob ],
          [] )
  in
  (* Source options control the partition strategy in priority order:
     time-window partitioning first, hash partitioning second, and batching
     defaults last. *)
  let partition_strategy =
    match (List.assoc_opt "timeField" source.options, List.assoc_opt "timeWindow" source.options) with
    | Some field, Some window -> TimeWindow (field, window)
    | _ -> (
        let configured_partition_count =
          match List.assoc_opt "partitionCount" source.options with
          | Some value -> (try int_of_string value with Failure _ -> partition_count)
          | None -> partition_count
        in
        match List.assoc_opt "partitionField" source.options with
        | Some field when configured_partition_count > 1 -> HashPartition (field, configured_partition_count)
        | _ ->
            if source.batching.max_records = None then SinglePartition
            else FixedSize source.batching.batch_size )
  in
  {
    extraction_source_id = source.source_id;
    extraction_source_kind = source.source_kind;
    extraction_format = source.format;
    selected_fields;
    predicates;
    ordering;
    partition_strategy;
    extraction_batch_size = source.batching.batch_size;
    extraction_max_records = source.batching.max_records;
    extraction_checkpoint = source.checkpoint;
  }

(** Suggest model families from the extraction mode and field shape.

    Inputs: extraction strategy, optional label field, and numeric feature list.
    Output: ordered candidate model families for the router to consider. *)
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

(** Split feature fields into numeric and categorical buckets.

    The first non-null sample decides the bucket. Empty or non-numeric fields
    are treated as categorical because they usually need embedding/string
    handling rather than numeric model input. *)
let classify_fields feature_fields records =
  List.fold_left
    (fun (numeric_fields, categorical_fields) field ->
      match first_non_null field records with
      | VInt _ | VFloat _ -> (field :: numeric_fields, categorical_fields)
      | _ -> (numeric_fields, field :: categorical_fields))
    ([], []) feature_fields

(** Build a dataset profile from records and caller-supplied context.

    Inputs: scope metadata, extraction strategy, optional field overrides,
    optional label field, batch size, and records.
    Output: [dataset_profile] used by the model router and architecture demos. *)
let build_profile ~scope_id ~scope_kind ~extraction_strategy ?feature_fields ?label_field ?(batch_size = 1000) records =
  let feature_fields =
    match feature_fields with
    | Some fields when fields <> [] -> fields
    | _ -> infer_feature_fields records label_field
  in
  let numeric_fields, categorical_fields = classify_fields feature_fields records in
  let record_count = List.length records in
  (* Partition count is derived with ceiling division so partially filled final
     batches still reserve a partition. *)
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

(** Validate an actual schema against a declared contract.

    Inputs: [schema_contract] and actual schema fields.
    Output: [schema_validation_report] with missing fields, dtype mismatches,
    extra fields, and warnings.

    Example test case:
    {[
      let report = validate_schema_contract contract actual_schema in
      assert (report.schema_valid || report.missing_fields <> [])
    ]} *)
let validate_schema_contract contract actual_schema =
  (* Association lists are sufficient for small schema contracts and preserve
     dependency-light compiler code. *)
  let actual_by_name = List.map (fun field -> (field.field_name, field)) actual_schema in
  let missing_fields =
    List.filter
      (fun expected -> Option.is_none (List.assoc_opt expected.field_name actual_by_name))
      contract.required_fields
  in
  let type_mismatches =
    contract.required_fields @ contract.optional_fields
    |> List.filter_map (fun expected ->
           match List.assoc_opt expected.field_name actual_by_name with
           | None -> None
           | Some actual when compatible_dtype ~expected:expected.dtype ~actual:actual.dtype -> None
           | Some actual ->
               Some
                 {
                   issue_field = expected.field_name;
                   expected_dtype = expected.dtype;
                   actual_dtype = Some actual.dtype;
                 })
  in
  let allowed =
    List.fold_left
      (fun names field -> StringSet.add field.field_name names)
      StringSet.empty (contract.required_fields @ contract.optional_fields)
  in
  let extra_fields =
    if contract.allow_extra_fields then []
    else List.filter (fun field -> not (StringSet.mem field.field_name allowed)) actual_schema
  in
  let schema_warnings =
    if contract.allow_extra_fields || extra_fields = [] then []
    else [ "schema contains fields outside the declared contract" ]
  in
  {
    schema_valid = missing_fields = [] && type_mismatches = [] && extra_fields = [];
    missing_fields;
    type_mismatches;
    extra_fields;
    schema_warnings;
  }

(** Normalize IR values to strings for duplicate checks and allow-list checks. *)
let value_to_string = function
  | VString value -> value
  | VInt value -> string_of_int value
  | VFloat value -> string_of_float value
  | VBool value -> string_of_bool value
  | VNull -> ""

(** Convert numeric-compatible values to floats for range checks. *)
let value_to_float = function
  | VInt value -> Some (float_of_int value)
  | VFloat value -> Some value
  | VString value -> (try Some (float_of_string value) with Failure _ -> None)
  | VBool _ | VNull -> None

(** Find [needle] inside [value] starting from [from].

    Output: next search offset when found, [None] otherwise. This helper avoids
    introducing a regex dependency for simple wildcard matching. *)
let contains_substring ~from value needle =
  let value_len = String.length value in
  let needle_len = String.length needle in
  let rec loop index =
    if needle_len = 0 then Some index
    else if index + needle_len > value_len then None
    else if String.sub value index needle_len = needle then Some (index + needle_len)
    else loop (index + 1)
  in
  loop from

(** Match a value against a deterministic glob-style pattern.

    Inputs: [pattern] may contain [*] wildcards; [value] is the string being
    checked.
    Output: [true] when all pattern parts match in order.

    Example test case:
    {[
      assert (wildcard_match "prod-*" "prod-api")
    ]} *)
let wildcard_match pattern value =
  (* The functional kernel stays dependency-light, so regex_match currently
     supports deterministic glob-style '*' matching at planning time. *)
  match String.split_on_char '*' pattern with
  | [ exact ] -> exact = value
  | [ prefix; suffix ] ->
      String.length value >= String.length prefix + String.length suffix
      && String.sub value 0 (String.length prefix) = prefix
      && String.sub value (String.length value - String.length suffix) (String.length suffix) = suffix
  | parts ->
      let rec loop offset = function
        | [] -> true
        | "" :: rest -> loop offset rest
        | part :: rest -> (
            match contains_substring ~from:offset value part with
            | Some next_offset -> loop next_offset rest
            | None -> false )
      in
      loop 0 parts

(** Evaluate a single quality rule against records.

    Inputs: record list and [quality_rule].
    Output: [quality_result] containing pass/fail, violations, and message. *)
let evaluate_quality_rule records rule =
  (* Quality checks return counts rather than raising. Callers decide whether
     warning and error severities block a pipeline or only annotate a run. *)
  (* Missing fields are treated as [VNull] so NonNull and range checks can
     report field absence as ordinary quality violations. *)
  let values = List.map (fun record -> Option.value (List.assoc_opt rule.field record) ~default:VNull) records in
  let violations =
    match rule.operator with
    | NonNull -> List.length (List.filter (( = ) VNull) values)
    | Unique ->
        (* A small hash table gives linear duplicate counting while keeping the
           function result pure from the caller's perspective. *)
        let seen = Hashtbl.create (List.length values) in
        List.fold_left
          (fun duplicates value ->
            let key = value_to_string value in
            if Hashtbl.mem seen key then duplicates + 1
            else (
              Hashtbl.add seen key true;
              duplicates ))
          0 values
    | MinValue minimum ->
        List.length
          (List.filter
             (fun value ->
               match value_to_float value with
               | Some number -> number < minimum
               | None -> true)
             values)
    | MaxValue maximum ->
        List.length
          (List.filter
             (fun value ->
               match value_to_float value with
               | Some number -> number > maximum
               | None -> true)
             values)
    | RegexMatch pattern ->
        List.length
          (List.filter (fun value -> not (wildcard_match pattern (value_to_string value))) values)
    | AllowedValues allowed ->
        List.length
          (List.filter
             (fun value -> not (List.mem (value_to_string value) allowed))
             values)
  in
  {
    quality_rule_id = rule.rule_id;
    quality_severity = rule.severity;
    passed = violations = 0;
    violations;
    quality_message =
      if violations = 0 then "passed"
      else string_of_int violations ^ " record(s) violated " ^ string_of_quality_operator rule.operator;
  }

(** Evaluate many quality rules independently over the same records. *)
let evaluate_quality_rules records rules = List.map (evaluate_quality_rule records) rules

(** Summarize quality results into blocking/warning/error counters. *)
let evaluate_quality_report records rules =
  let quality_results = evaluate_quality_rules records rules in
  let warning_count =
    List.fold_left
      (fun count result -> if (not result.passed) && result.quality_severity = Warning then count + 1 else count)
      0 quality_results
  in
  let error_count =
    List.fold_left
      (fun count result -> if (not result.passed) && result.quality_severity = Error then count + 1 else count)
      0 quality_results
  in
  {
    quality_results;
    blocking = error_count > 0;
    warning_count;
    error_count;
    total_violations = List.fold_left (fun total result -> total + result.violations) 0 quality_results;
  }

(** Resolve a field rename from a mapping list, preserving the field when no
    mapping exists. *)
let rename_field mappings field =
  Option.value (List.assoc_opt field mappings) ~default:field

(** Compile the schema impact of transform operations.

    Inputs: operations and input schema.
    Output: predicted output schema without materializing transformed records. *)
let transform_schema operations input_schema =
  (* Schema compilation mirrors transform execution, giving services a cheap
     way to inspect the output contract before records are materialized. *)
  List.fold_left
    (fun schema operation ->
      match operation with
      | SelectFields fields -> List.filter (fun field -> List.mem field.field_name fields) schema
      | DropFields fields -> List.filter (fun field -> not (List.mem field.field_name fields)) schema
      | RenameFields mappings ->
          List.map
            (fun field -> { field with field_name = rename_field mappings field.field_name })
            schema
      | FilterNonNull _ -> schema
      | CastFields casts ->
          List.map
            (fun field ->
              match List.assoc_opt field.field_name casts with
              | Some dtype -> { field with dtype }
              | None -> field)
            schema)
    input_schema operations

(** Build a transform plan and infer its output schema from sample records. *)
let compile_transform_plan ~plan_id operations records =
  {
    transform_plan_id = plan_id;
    operations;
    output_schema = transform_schema operations (infer_schema records);
  }

(** Cast one IR value to a requested dtype where safe and deterministic.

    Failed string casts return [VNull] to make bad input visible to later
    quality checks instead of raising during transformation. *)
let cast_value dtype value =
  match (dtype, value) with
  | "string", value -> VString (value_to_string value)
  | "int", VFloat value -> VInt (int_of_float value)
  | "int", VString value -> (try VInt (int_of_string value) with Failure _ -> VNull)
  | "float", VInt value -> VFloat (float_of_int value)
  | "float", VString value -> (try VFloat (float_of_string value) with Failure _ -> VNull)
  | "bool", VString "true" -> VBool true
  | "bool", VString "false" -> VBool false
  | _ -> value

(** Execute transform operations over concrete records.

    Inputs: compiled [transform_plan] and source records.
    Output: transformed records after all operations are applied left-to-right. *)
let apply_transform_plan plan records =
  (* Transforms are applied left-to-right so the JSON API can be used as a
     small declarative data-engineering DSL. *)
  List.fold_left
    (fun records operation ->
      match operation with
      | SelectFields fields ->
          List.map (List.filter (fun (field, _) -> List.mem field fields)) records
      | DropFields fields ->
          List.map (List.filter (fun (field, _) -> not (List.mem field fields))) records
      | RenameFields mappings ->
          List.map
            (List.map (fun (field, value) -> (rename_field mappings field, value)))
            records
      | FilterNonNull fields ->
          List.filter
            (fun record ->
              List.for_all
                (fun field ->
                  match List.assoc_opt field record with
                  | Some VNull | None -> false
                  | Some _ -> true)
                fields)
            records
      | CastFields casts ->
          List.map
            (List.map (fun (field, value) ->
                 match List.assoc_opt field casts with
                 | Some dtype -> (field, cast_value dtype value)
                 | None -> (field, value)))
            records)
    records plan.operations
