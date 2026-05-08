(** JSON codec for Dagents shared IR.

    This module is the boundary between service/CLI JSON payloads and the typed
    OCaml intermediate representation. Parser functions raise
    [Invalid_argument] with a field-specific message when required JSON does
    not match the expected shape. Serializer functions produce stable API field
    names used by tests and demo scripts. *)

open Dagents_common_ir

(** Parse a JSON workload spec into typed manifest-compiler input.

    Example test case:
    {[
      let spec = workload_spec_of_yojson (`Assoc [ ("components", `List []) ]) in
      assert (spec.namespace = "dagents")
    ]} *)
val workload_spec_of_yojson : Yojson.Safe.t -> workload_spec
(** Serialize one rendered workload manifest to JSON. *)
val yojson_of_workload_manifest : workload_manifest -> Yojson.Safe.t
(** Serialize a full workload plan, including combined YAML, to JSON. *)
val yojson_of_workload_plan : workload_plan -> Yojson.Safe.t

(** Parse a JSON source spec into typed dataset-compiler input. *)
val source_spec_of_yojson : Yojson.Safe.t -> source_spec
(** Serialize source validation output for API/CLI callers. *)
val yojson_of_source_validation_result : source_validation_result -> Yojson.Safe.t
(** Serialize derived source metadata. *)
val yojson_of_source_metadata : source_metadata -> Yojson.Safe.t
(** Serialize a compiled extraction plan. *)
val yojson_of_extraction_plan : extraction_plan -> Yojson.Safe.t
(** Parse a JSON schema contract for validation. *)
val schema_contract_of_yojson : Yojson.Safe.t -> schema_contract
(** Parse a JSON quality rule.

    Example test case:
    {[
      let rule =
        quality_rule_of_yojson
          (`Assoc
             [
               ("ruleId", `String "id-required");
               ("field", `String "id");
               ("operator", `String "non_null");
             ])
      in
      assert (rule.operator = NonNull)
    ]} *)
val quality_rule_of_yojson : Yojson.Safe.t -> quality_rule
(** Parse a JSON list of transform operations. *)
val transform_operations_of_yojson : Yojson.Safe.t -> transform_operation list
(** Serialize a schema validation report. *)
val yojson_of_schema_validation_report : schema_validation_report -> Yojson.Safe.t
(** Serialize one quality result. *)
val yojson_of_quality_result : quality_result -> Yojson.Safe.t
(** Serialize an aggregate quality report. *)
val yojson_of_quality_report : quality_report -> Yojson.Safe.t
(** Serialize a transform plan and its output schema. *)
val yojson_of_transform_plan : transform_plan -> Yojson.Safe.t
(** Serialize records to JSON arrays of objects. *)
val yojson_of_records : record list -> Yojson.Safe.t
(** Parse JSON arrays of objects into typed records. *)
val records_of_yojson : Yojson.Safe.t -> record list

(** Parse a JSON pipeline definition. *)
val pipeline_definition_of_yojson : Yojson.Safe.t -> pipeline_definition
(** Serialize a compiled pipeline with execution targets. *)
val yojson_of_pipeline : compiled_pipeline -> Yojson.Safe.t

(** Serialize a dataset profile. *)
val dataset_profile_to_yojson : dataset_profile -> Yojson.Safe.t
(** Serialize a model-route plan. *)
val route_plan_to_yojson : route_plan -> Yojson.Safe.t
