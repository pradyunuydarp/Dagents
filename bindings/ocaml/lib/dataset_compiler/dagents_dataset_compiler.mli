(** Dataset compiler public interface.

    This module contains pure functional planning utilities for source data:
    schema inference, source validation, extraction-plan compilation, dataset
    profiling, schema-contract validation, quality checks, and record
    transformation planning. It does not perform network I/O, database reads, or
    ML training; service adapters execute the plans produced here. *)

open Dagents_common_ir

(** Infer a schema from a list of records.

    Inputs:
    - [record list]: sample records represented as field/value pairs.

    Output:
    - [record_schema_field list]: one field entry per observed field name. The
      dtype is inferred from the first non-null value seen for that field.

    Example test case:
    {[
      let schema = infer_schema [ [ ("amount", VFloat 12.5); ("id", VInt 1) ] ] in
      assert (List.exists (fun f -> f.field_name = "amount" && f.dtype = "float") schema)
    ]} *)
val infer_schema : record list -> record_schema_field list

(** Derive metadata from a source specification without executing the source.

    Inputs:
    - [source_spec]: connector kind, selection payload, batching, and hints.

    Output:
    - [source_metadata]: source id, kind, schema where inferable, and estimated
      record count where available.

    Example test case:
    {[
      let metadata = metadata_of_source inline_source in
      assert (metadata.source_id = inline_source.source_id)
    ]} *)
val metadata_of_source : source_spec -> source_metadata

(** Validate that a source specification is structurally executable.

    Inputs:
    - [source_spec]: declarative source contract.

    Output:
    - [source_validation_result]: [valid = false] with blocking errors for
      invalid connector pairings, missing connection refs, or invalid batching.

    Example test case:
    {[
      let result = validate_source source in
      assert (result.valid || result.errors <> [])
    ]} *)
val validate_source : source_spec -> source_validation_result

(** Compile a connector-specific source spec into a normalized extraction plan.

    Inputs:
    - optional [partition_count]: fallback partition count when source options
      request hash partitioning but do not provide a count.
    - [source_spec]: validated source contract.

    Output:
    - [extraction_plan]: selected fields, predicates, ordering, partitioning,
      batching, and checkpoint metadata in connector-neutral form.

    Raises:
    - [Invalid_argument] when [validate_source] returns blocking errors.

    Example test case:
    {[
      let plan = compile_extraction_plan ~partition_count:4 source in
      assert (plan.extraction_source_id = source.source_id)
    ]} *)
val compile_extraction_plan : ?partition_count:int -> source_spec -> extraction_plan

(** Build a dataset profile for model routing and workload planning.

    Inputs:
    - [scope_id]: source, tenant, or aggregate id.
    - [scope_kind]: local source or assimilated multi-source scope.
    - [extraction_strategy]: tabular, time-series, text, or hybrid strategy.
    - optional [feature_fields]: explicit feature projection. Empty or missing
      values are inferred from records.
    - optional [label_field]: supervised target field excluded from features.
    - optional [batch_size]: partition-size hint used for [partition_count].
    - [record list]: sample or full record batch.

    Output:
    - [dataset_profile]: feature fields, numeric/categorical grouping, partition
      count, and suggested model families.

    Example test case:
    {[
      let profile =
        build_profile ~scope_id:"orders" ~scope_kind:Source
          ~extraction_strategy:Tabular [ [ ("amount", VFloat 10.0) ] ]
      in
      assert (profile.numeric_fields = [ "amount" ])
    ]} *)
val build_profile :
  scope_id:string ->
  scope_kind:scope_kind ->
  extraction_strategy:extraction_strategy ->
  ?feature_fields:string list ->
  ?label_field:string ->
  ?batch_size:int ->
  record list ->
  dataset_profile

(** Compare an inferred/actual schema against a declared schema contract.

    Inputs:
    - [schema_contract]: required/optional fields and extra-field policy.
    - [record_schema_field list]: actual schema.

    Output:
    - [schema_validation_report]: missing required fields, type mismatches,
      extra fields, warnings, and final validity.

    Example test case:
    {[
      let report = validate_schema_contract contract actual_schema in
      assert (report.schema_valid = (report.missing_fields = []))
    ]} *)
val validate_schema_contract : schema_contract -> record_schema_field list -> schema_validation_report

(** Evaluate one data-quality rule against records.

    Inputs:
    - [record list]: records to inspect.
    - [quality_rule]: field, operator, and severity.

    Output:
    - [quality_result]: pass/fail, violation count, and display message.

    Example test case:
    {[
      let result =
        evaluate_quality_rule [ [ ("id", VNull) ] ]
          { rule_id = "id-required"; field = "id"; operator = NonNull; severity = Error }
      in
      assert (not result.passed)
    ]} *)
val evaluate_quality_rule : record list -> quality_rule -> quality_result

(** Evaluate several quality rules independently over the same records. *)
val evaluate_quality_rules : record list -> quality_rule list -> quality_result list

(** Evaluate quality rules and summarize warning/error counts.

    Output: [blocking] is true when at least one failing [Error] rule exists. *)
val evaluate_quality_report : record list -> quality_rule list -> quality_report

(** Predict output schema after applying transform operations.

    Inputs:
    - [transform_operation list]: operations applied left-to-right.
    - [record_schema_field list]: input schema.

    Output:
    - [record_schema_field list]: projected/renamed/cast schema.

    Example test case:
    {[
      let output = transform_schema [ RenameFields [ ("a", "b") ] ] input_schema in
      assert (List.exists (fun f -> f.field_name = "b") output)
    ]} *)
val transform_schema : transform_operation list -> record_schema_field list -> record_schema_field list

(** Compile a transform plan from operations and sample records.

    Inputs:
    - [plan_id]: stable identifier for diagnostics.
    - [transform_operation list]: operations to apply at runtime.
    - [record list]: sample records used to infer input schema.

    Output:
    - [transform_plan]: operation list plus predicted output schema. *)
val compile_transform_plan : plan_id:string -> transform_operation list -> record list -> transform_plan

(** Apply a compiled transform plan to records.

    Inputs:
    - [transform_plan]: operations compiled by [compile_transform_plan].
    - [record list]: input records.

    Output:
    - [record list]: records transformed left-to-right.

    Example test case:
    {[
      let plan = { transform_plan_id = "p"; operations = [ SelectFields [ "id" ] ]; output_schema = [] } in
      let output = apply_transform_plan plan [ [ ("id", VInt 1); ("x", VInt 2) ] ] in
      assert (output = [ [ ("id", VInt 1) ] ])
    ]} *)
val apply_transform_plan : transform_plan -> record list -> record list
