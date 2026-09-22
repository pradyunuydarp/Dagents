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

(** Parse a restriction request for the Ethical-Restriction Rails.

    Defaults are deliberately conservative: an unclassified field is treated as
    [HighSensitivity] and an attribute without an explicit [verified] flag is
    treated as unverified, so a careless payload cannot widen what the planner
    permits.

    Example test case:
    {[
      let request =
        restriction_request_of_yojson
          (`Assoc
             [
               ("requestId", `String "r-1");
               ("boundary", `String "before_send");
               ("requester", `Assoc [ ("requesterId", `String "svc") ]);
               ("classification", `Assoc [ ("classificationId", `String "c-1") ]);
               ("requestedFields", `List [ `String "age" ]);
               ("granularity", `String "row");
             ])
      in
      assert (request.classification.default_sensitivity = HighSensitivity)
    ]} *)
val restriction_request_of_yojson : Yojson.Safe.t -> restriction_request
(** Serialize a compiled restriction plan, including its filtering score. *)
val yojson_of_restriction_plan : restriction_plan -> Yojson.Safe.t
(** Parse one Know-Your-User attribute. *)
val kyu_attribute_of_yojson : Yojson.Safe.t -> kyu_attribute
(** Serialize one Know-Your-User attribute. *)
val yojson_of_kyu_attribute : kyu_attribute -> Yojson.Safe.t
(** Parse the party making a governance request. *)
val requester_of_yojson : Yojson.Safe.t -> requester
(** Serialize a computed trust assessment. *)
val yojson_of_kyu_assessment : kyu_assessment -> Yojson.Safe.t
(** Parse a data classification from the policy knowledge base. *)
val data_classification_of_yojson : Yojson.Safe.t -> data_classification

(** Parse a federated round manifest. *)
val round_manifest_of_yojson : Yojson.Safe.t -> round_manifest
(** Serialize a round manifest back to its wire shape. *)
val yojson_of_round_manifest : round_manifest -> Yojson.Safe.t
(** Parse one site's standing enrolment in a study. *)
val site_registration_of_yojson : Yojson.Safe.t -> site_registration
(** Serialize one site registration. *)
val yojson_of_site_registration : site_registration -> Yojson.Safe.t
(** Serialize a compiled round plan, including exclusions and their reasons. *)
val yojson_of_round_plan : round_plan -> Yojson.Safe.t
(** Parse one site's returned round result. *)
val site_result_of_yojson : Yojson.Safe.t -> site_result
(** Serialize one site result. *)
val yojson_of_site_result : site_result -> Yojson.Safe.t
(** Serialize an aggregation-readiness verdict. *)
val yojson_of_aggregation_readiness : aggregation_readiness -> Yojson.Safe.t
(** Parse one release gate and its comparison. *)
val release_gate_of_yojson : Yojson.Safe.t -> release_gate
(** Serialize a release decision and its per-gate evidence. *)
val yojson_of_release_decision : release_decision -> Yojson.Safe.t
(** Parse an aggregation method, bare name or parameterized object. *)
val aggregation_method_of_yojson : Yojson.Safe.t -> aggregation_method
(** Serialize an aggregation method as a parameterized object. *)
val yojson_of_aggregation_method : aggregation_method -> Yojson.Safe.t
