"""Tests for the consumer extension registry.

The registry is what keeps domain logic out of the framework while still letting
a consumer reach the framework's generic machinery. These tests pin the
properties that make that safe: conflicts are errors, dangling references are
errors, and a failed registration leaves nothing behind.
"""

from __future__ import annotations

import math
import unittest

from dagents.common.domain.errors import ModelVerificationError
from dagents.common.domain.federation import GateComparison, ReleaseGate
from dagents.common.domain.governance import DataClassification
from dagents.common.extensions import (
    ConditionPack,
    ExtensionError,
    ExtensionRegistry,
    ExtensionSummary,
    FeatureContract,
    FeatureField,
    TrainingOutcome,
    load_extensions,
)
from dagents.tests.fixtures import toy_extension as toy


class StubExtension:
    """A minimal consumer extension, built field by field for each test."""

    def __init__(
        self,
        extension_id: str = "demo",
        contracts: list[FeatureContract] | None = None,
        classifications: list[DataClassification] | None = None,
        conditions: list[ConditionPack] | None = None,
        steps: dict | None = None,
        adapters: dict | None = None,
    ) -> None:
        self.extension_id = extension_id
        self.version = "1.0.0"
        self._contracts = contracts or []
        self._classifications = classifications or []
        self._conditions = conditions or []
        self._steps = steps or {}
        self._adapters = adapters or {}

    def feature_contracts(self) -> list[FeatureContract]:
        return self._contracts

    def classifications(self) -> list[DataClassification]:
        return self._classifications

    def condition_packs(self) -> list[ConditionPack]:
        return self._conditions

    def pipeline_steps(self) -> dict:
        return self._steps

    def model_adapters(self) -> dict:
        return self._adapters


CONTRACT = FeatureContract(
    contract_id="stroke-triage-features",
    version="v2",
    fields=[
        FeatureField(name="nihss_total", dtype="float", unit="points"),
        FeatureField(name="age_band", dtype="string"),
        FeatureField(name="last_known_well_minutes", dtype="float", required=False, unit="minutes"),
    ],
)

CLASSIFICATION = DataClassification(
    classification_id="stroke-triage-v1", default_sensitivity="high", minimum_cohort=20
)

CONDITION = ConditionPack(
    condition_id="suspected_stroke",
    display_name="Suspected stroke",
    intended_use="Prioritize suspected-stroke scans for specialist review. Decision support only.",
    feature_contract_id="stroke-triage-features",
    classification_id="stroke-triage-v1",
    release_gates=[
        ReleaseGate(gate_id="auc", metric="auc", comparison=GateComparison(kind="at_least", value=0.85))
    ],
)


class ExtensionRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = ExtensionRegistry()

    def test_registration_indexes_every_contribution(self) -> None:
        self.registry.register(
            StubExtension(
                contracts=[CONTRACT],
                classifications=[CLASSIFICATION],
                conditions=[CONDITION],
                steps={"score_stroke": lambda: None},
                adapters={"stroke_rule": lambda: None},
            )
        )
        self.assertEqual(self.registry.feature_contract("stroke-triage-features").version, "v2")
        self.assertEqual(self.registry.classification("stroke-triage-v1").minimum_cohort, 20)
        self.assertEqual(self.registry.condition_pack("suspected_stroke").display_name, "Suspected stroke")
        self.assertIsNotNone(self.registry.pipeline_step("score_stroke"))
        self.assertIsNotNone(self.registry.model_adapter("stroke_rule"))

    def test_unknown_lookups_raise_rather_than_returning_none(self) -> None:
        """A missing classification must not read as an absent policy.

        Returning ``None`` here would let a guard fall through to whatever the
        caller passed instead, which is the silent-default behaviour this
        registry exists to avoid.
        """
        with self.assertRaises(ExtensionError):
            self.registry.classification("does-not-exist")
        with self.assertRaises(ExtensionError):
            self.registry.feature_contract("does-not-exist")
        with self.assertRaises(ExtensionError):
            self.registry.condition_pack("does-not-exist")

    def test_conflicting_contributions_are_rejected(self) -> None:
        """Two extensions claiming one id would make behaviour import-order dependent."""
        self.registry.register(StubExtension(extension_id="a", contracts=[CONTRACT]))
        with self.assertRaises(ExtensionError) as caught:
            self.registry.register(StubExtension(extension_id="b", contracts=[CONTRACT]))
        self.assertIn("stroke-triage-features", str(caught.exception))

    def test_a_rejected_registration_leaves_nothing_behind(self) -> None:
        """A conflict must not half-register the extension that caused it."""
        self.registry.register(StubExtension(extension_id="a", contracts=[CONTRACT]))
        rejected = StubExtension(
            extension_id="b",
            contracts=[CONTRACT],
            classifications=[CLASSIFICATION],
            steps={"only_in_b": lambda: None},
        )
        with self.assertRaises(ExtensionError):
            self.registry.register(rejected)
        self.assertIsNone(self.registry.pipeline_step("only_in_b"))
        with self.assertRaises(ExtensionError):
            self.registry.classification("stroke-triage-v1")
        self.assertEqual([e.extension_id for e in self.registry.extensions()], ["a"])

    def test_a_condition_pack_cannot_reference_an_unknown_contract(self) -> None:
        """A pack pointing at a contract nobody defines is a dangling policy."""
        with self.assertRaises(ExtensionError) as caught:
            self.registry.register(StubExtension(conditions=[CONDITION]))
        self.assertIn("stroke-triage-features", str(caught.exception))

    def test_a_condition_pack_cannot_reference_an_unknown_classification(self) -> None:
        """A pack pointing at a classification nobody defines is dangling policy.

        The classification is what the governance Rails read. Left unchecked it
        surfaces much later, as a guard-time failure on a request already in
        flight, rather than at registration where it can be fixed.
        """
        with self.assertRaises(ExtensionError) as caught:
            self.registry.register(StubExtension(contracts=[CONTRACT], conditions=[CONDITION]))
        self.assertIn("stroke-triage-v1", str(caught.exception))

    def test_lookups_hand_out_copies_not_registered_instances(self) -> None:
        """Otherwise anything holding a request can rewrite registered policy."""
        self.registry.register(
            StubExtension(contracts=[CONTRACT], classifications=[CLASSIFICATION], conditions=[CONDITION])
        )
        borrowed = self.registry.classification("stroke-triage-v1")
        borrowed.field_sensitivity["injected"] = "low"
        borrowed.minimum_cohort = 0
        fresh = self.registry.classification("stroke-triage-v1")
        self.assertNotIn("injected", fresh.field_sensitivity)
        self.assertEqual(fresh.minimum_cohort, 20)

    def test_an_extension_without_an_id_is_rejected(self) -> None:
        self.registry.register(StubExtension(extension_id="a"))
        with self.assertRaises(ExtensionError):
            self.registry.register(StubExtension(extension_id=""))

    def test_feature_contract_renders_a_schema_contract(self) -> None:
        """A contract must translate into what the OCaml schema validator expects."""
        rendered = CONTRACT.as_schema_contract()
        self.assertEqual(
            [field["name"] for field in rendered["required_fields"]], ["nihss_total", "age_band"]
        )
        self.assertEqual(
            [field["name"] for field in rendered["optional_fields"]], ["last_known_well_minutes"]
        )
        self.assertEqual(CONTRACT.required_fields(), ["nihss_total", "age_band"])

    def test_describe_summarizes_the_registry(self) -> None:
        self.registry.register(
            StubExtension(contracts=[CONTRACT], classifications=[CLASSIFICATION], conditions=[CONDITION])
        )
        described = self.registry.describe()
        self.assertEqual(described["extensions"], [{"extension_id": "demo", "version": "1.0.0"}])
        self.assertEqual(described["classifications"], ["stroke-triage-v1"])
        self.assertEqual(described["condition_packs"][0]["condition_id"], "suspected_stroke")


def toy_pack(**overrides) -> ConditionPack:
    """The toy condition pack, with fields changed for one test."""
    return toy.TOY_CONDITION.model_copy(update=overrides)


class ConditionPackLabelFieldTests(unittest.TestCase):
    """A pack names the field that holds its label, and the purposes it is approved for."""

    def setUp(self) -> None:
        self.registry = ExtensionRegistry()

    def test_the_new_fields_default_to_none_and_empty(self) -> None:
        self.assertIsNone(CONDITION.label_field)
        self.assertEqual(CONDITION.approved_purposes, [])

    def test_a_label_field_from_the_contract_registers(self) -> None:
        self.registry.register(toy.ToyExtension())
        pack = self.registry.condition_pack("toy_condition")
        self.assertEqual(pack.label_field, "label")
        self.assertEqual(pack.approved_purposes, ["toy_condition", "toy_research"])

    def test_a_label_field_outside_the_contract_is_rejected(self) -> None:
        """A label the contract does not define would be read from a field no site has agreed to."""
        extension = StubExtension(
            contracts=[toy.TOY_CONTRACT],
            classifications=[toy.TOY_CLASSIFICATION],
            conditions=[toy_pack(label_field="not_in_contract")],
            steps={"left_out": lambda: None},
        )
        with self.assertRaises(ExtensionError) as caught:
            self.registry.register(extension)
        self.assertIn("not_in_contract", str(caught.exception))
        self.assertIn("toy_condition", str(caught.exception))
        self.assertEqual(self.registry.extensions(), [])
        self.assertIsNone(self.registry.pipeline_step("left_out"))

    def test_the_label_check_reads_a_contract_another_extension_registered(self) -> None:
        self.registry.register(
            StubExtension(extension_id="contracts", contracts=[toy.TOY_CONTRACT], classifications=[toy.TOY_CLASSIFICATION])
        )
        with self.assertRaises(ExtensionError):
            self.registry.register(StubExtension(extension_id="bad", conditions=[toy_pack(label_field="x9")]))
        self.registry.register(StubExtension(extension_id="good", conditions=[toy_pack(label_field="x1")]))
        self.assertEqual(self.registry.condition_pack("toy_condition").label_field, "x1")


class ModelKindRegistrationTests(unittest.TestCase):
    """Model kinds are typed contributions, keyed by `kind`, with the usual conflict rule."""

    def setUp(self) -> None:
        self.registry = ExtensionRegistry()

    def test_registration_indexes_model_kinds_and_site_sources(self) -> None:
        self.registry.register(toy.ToyExtension())
        self.assertEqual(self.registry.list_model_kinds(), ["toy_linear", "toy_threshold"])
        self.assertEqual(self.registry.model_kind("toy_linear").version_prefix, "toy-linear")
        self.assertFalse(self.registry.model_kind("toy_threshold").trainable)
        self.assertEqual(self.registry.list_site_sources(), ["toy_static"])
        source = self.registry.site_source("toy_static")(None, "site_role", "site-1")
        self.assertEqual(source.count(), 30)

    def test_describe_lists_model_kinds_and_site_sources(self) -> None:
        self.registry.register(toy.ToyExtension())
        described = self.registry.describe()
        self.assertEqual(described["model_kinds"], ["toy_linear", "toy_threshold"])
        self.assertEqual(described["site_sources"], ["toy_static"])
        self.assertEqual(ExtensionSummary.model_validate(described).model_dump(), described)

    def test_unknown_kinds_and_sources_raise(self) -> None:
        with self.assertRaises(ExtensionError):
            self.registry.model_kind("missing")
        with self.assertRaises(ExtensionError):
            self.registry.site_source("missing")

    def test_two_extensions_cannot_contribute_one_kind(self) -> None:
        self.registry.register(toy.ToyExtension())
        rival = StubExtension(extension_id="rival", steps={"only_in_rival": lambda: None})
        rival.model_kinds = lambda: [toy.ToyLinearKind()]  # type: ignore[attr-defined]
        with self.assertRaises(ExtensionError) as caught:
            self.registry.register(rival)
        self.assertIn("toy_linear", str(caught.exception))
        self.assertIsNone(self.registry.pipeline_step("only_in_rival"))
        self.assertEqual([e.extension_id for e in self.registry.extensions()], ["dagents-toy-extension"])

    def test_two_extensions_cannot_contribute_one_site_source(self) -> None:
        self.registry.register(toy.ToyExtension())
        rival = StubExtension(extension_id="rival")
        rival.site_sources = lambda: {"toy_static": toy.toy_site_source}  # type: ignore[attr-defined]
        with self.assertRaises(ExtensionError) as caught:
            self.registry.register(rival)
        self.assertIn("toy_static", str(caught.exception))

    def test_one_extension_cannot_contribute_a_kind_twice(self) -> None:
        twice = StubExtension(extension_id="twice")
        twice.model_kinds = lambda: [toy.ToyLinearKind(), toy.ToyLinearKind()]  # type: ignore[attr-defined]
        with self.assertRaises(ExtensionError) as caught:
            self.registry.register(twice)
        self.assertIn("toy_linear", str(caught.exception))
        self.assertEqual(self.registry.extensions(), [])

    def test_a_malformed_kind_is_rejected(self) -> None:
        """A kind the agents would fail to call is refused at registration, not mid-round."""

        class NoCombine(toy.ToyLinearKind):
            kind = "no_combine"
            combine = None  # type: ignore[assignment]

        class NoName(toy.ToyThresholdKind):
            kind = ""

        class NotBool(toy.ToyThresholdKind):
            kind = "not_bool"
            trainable = "yes"  # type: ignore[assignment]

        class NoPrefix(toy.ToyLinearKind):
            kind = "no_prefix"
            version_prefix = ""

        class NoEvaluate:
            kind = "no_evaluate"
            trainable = False
            version_prefix = "no-evaluate"

            def parameter_digest(self, params):  # noqa: ANN001, ANN201 - test double
                return "sha256:x"

            def code_digest(self):  # noqa: ANN201 - test double
                return "sha256:y"

        for kind in (NoCombine(), NoName(), NotBool(), NoPrefix(), NoEvaluate()):
            with self.subTest(kind=type(kind).__name__):
                extension = StubExtension(extension_id=f"bad-{type(kind).__name__}")
                extension.model_kinds = lambda kind=kind: [kind]  # type: ignore[attr-defined]
                with self.assertRaises(ExtensionError):
                    self.registry.register(extension)
        self.assertEqual(self.registry.list_model_kinds(), [])

    def test_a_site_source_must_be_callable(self) -> None:
        extension = StubExtension(extension_id="bad-source")
        extension.site_sources = lambda: {"broken": "not a factory"}  # type: ignore[attr-defined]
        with self.assertRaises(ExtensionError):
            self.registry.register(extension)

    def test_an_extension_without_the_new_methods_still_registers(self) -> None:
        self.registry.register(
            StubExtension(contracts=[CONTRACT], classifications=[CLASSIFICATION], conditions=[CONDITION])
        )
        described = self.registry.describe()
        self.assertEqual(described["model_kinds"], [])
        self.assertEqual(described["site_sources"], [])
        self.assertEqual(self.registry.list_model_kinds(), [])

    def test_a_rejected_registration_leaves_no_kind_behind(self) -> None:
        bad = toy.ToyExtension()
        bad.condition_packs = lambda: [toy_pack(label_field="nope")]  # type: ignore[method-assign]
        with self.assertRaises(ExtensionError):
            self.registry.register(bad)
        self.assertEqual(self.registry.list_model_kinds(), [])
        self.assertEqual(self.registry.list_site_sources(), [])


class LoadExtensionsTests(unittest.TestCase):
    """`load_extensions` registers extensions named as `module:attribute`."""

    TOY = "dagents.tests.fixtures.toy_extension:ToyExtension"
    SPARE = "dagents.tests.fixtures.toy_extension:SPARE_EXTENSION"

    def setUp(self) -> None:
        self.registry = ExtensionRegistry()

    def test_a_class_is_instantiated_and_registered(self) -> None:
        loaded = load_extensions(self.TOY, self.registry)
        self.assertEqual([e.extension_id for e in loaded], ["dagents-toy-extension"])
        self.assertEqual(self.registry.list_model_kinds(), ["toy_linear", "toy_threshold"])

    def test_several_specs_with_spaces_register_in_order(self) -> None:
        loaded = load_extensions(f" {self.TOY} ,{self.SPARE} ", self.registry)
        self.assertEqual(
            [e.extension_id for e in loaded], ["dagents-toy-extension", "dagents-spare-extension"]
        )
        self.assertEqual(
            [e.extension_id for e in self.registry.extensions()],
            ["dagents-toy-extension", "dagents-spare-extension"],
        )

    def test_an_empty_setting_registers_nothing(self) -> None:
        self.assertEqual(load_extensions("", self.registry), [])
        self.assertEqual(load_extensions(" , ", self.registry), [])
        self.assertEqual(self.registry.extensions(), [])

    def test_malformed_and_missing_specs_raise_extension_errors(self) -> None:
        for spec in (
            "dagents.tests.fixtures.toy_extension",
            "dagents.tests.fixtures.toy_extension:",
            ":ToyExtension",
            "dagents.tests.fixtures.no_such_module:ToyExtension",
            "dagents.tests.fixtures.toy_extension:NoSuchExtension",
        ):
            with self.subTest(spec=spec):
                with self.assertRaises(ExtensionError) as caught:
                    load_extensions(spec, self.registry)
                self.assertIn(spec.strip(), str(caught.exception))
        self.assertEqual(self.registry.extensions(), [])

    def test_a_spec_that_fails_to_load_registers_none_of_the_others(self) -> None:
        with self.assertRaises(ExtensionError):
            load_extensions(f"{self.TOY},dagents.tests.fixtures.toy_extension:Missing", self.registry)
        self.assertEqual(self.registry.extensions(), [])

    def test_loading_the_same_extension_twice_is_a_conflict(self) -> None:
        load_extensions(self.TOY, self.registry)
        with self.assertRaises(ExtensionError):
            load_extensions(self.TOY, self.registry)


class ToyModelKindTests(unittest.TestCase):
    """The fixture's model kinds behave as the `ModelKind` protocol says.

    The LMA and GMA tests run rounds with these kinds, so their behaviour is
    pinned here.
    """

    def setUp(self) -> None:
        self.kind = toy.ToyLinearKind()
        self.params = {"bias": 0.1, "w.x1": -0.2, "w.x2": 0.3}
        self.records = toy.toy_records("site-1", 40)

    def test_the_parameter_digest_is_stable_and_checks_the_names(self) -> None:
        self.assertEqual(self.kind.parameter_digest(self.params), self.kind.parameter_digest(dict(self.params)))
        self.assertNotEqual(self.kind.parameter_digest(self.params), self.kind.parameter_digest(self.kind.initial_params()))
        for bad in (
            {**self.params, "extra": 1.0},
            {"bias": 0.1, "w.x1": 0.2},
            {**self.params, "bias": math.nan},
            {**self.params, "bias": True},
            ["bias"],
        ):
            with self.subTest(bad=bad):
                with self.assertRaises(ModelVerificationError):
                    self.kind.parameter_digest(bad)  # type: ignore[arg-type]

    def test_training_returns_an_update_and_lowers_the_loss(self) -> None:
        outcome = self.kind.train(self.kind.initial_params(), self.records, "label")
        self.assertIsInstance(outcome, TrainingOutcome)
        self.assertEqual(sorted(outcome.update), sorted(self.kind.NAMES))
        self.assertEqual(outcome.examples, 40)
        self.assertLess(outcome.metrics["train_loss"], outcome.metrics["start_loss"])

    def test_training_without_labels_sends_no_update(self) -> None:
        outcome = self.kind.train(self.params, toy.toy_records("site-1", 10, labelled_share=0.0), "label")
        self.assertIsNone(outcome.update)
        self.assertEqual(outcome.examples, 0)

    def test_check_update_refuses_unknown_names_and_values_that_are_not_finite(self) -> None:
        self.assertEqual(self.kind.check_update({"bias": 1, "w.x1": 0.5, "w.x2": -0.5})["bias"], 1.0)
        for bad in ({"bias": 0.0, "w.x1": 0.0, "w.x2": 0.0, "w.x3": 0.0}, {"bias": math.inf, "w.x1": 0.0, "w.x2": 0.0}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.kind.check_update(bad)

    def test_combine_adds_each_weighted_update(self) -> None:
        first = {"bias": 1.0, "w.x1": 0.0, "w.x2": 2.0}
        second = {"bias": -1.0, "w.x1": 4.0, "w.x2": 0.0}
        combined = self.kind.combine(self.params, [(0.25, first), (0.75, second)])
        self.assertAlmostEqual(combined["bias"], 0.1 + 0.25 - 0.75)
        self.assertAlmostEqual(combined["w.x1"], -0.2 + 3.0)
        self.assertAlmostEqual(combined["w.x2"], 0.3 + 0.5)
        self.assertAlmostEqual(self.kind.parameter_change(combined, self.params), math.sqrt(0.5**2 + 3.0**2 + 0.5**2))

    def test_evaluate_measures_accuracy_on_labelled_records(self) -> None:
        trained = self.kind.combine(
            self.kind.initial_params(), [(1.0, self.kind.train(self.kind.initial_params(), self.records, "label").update)]
        )
        metrics = self.kind.evaluate(trained, self.records, "label")
        self.assertGreater(metrics["accuracy"], 0.7)
        self.assertEqual(metrics["labelled_count"], 40.0)

    def test_the_rule_kind_cannot_train(self) -> None:
        rule = toy.ToyThresholdKind()
        self.assertFalse(rule.trainable)
        self.assertFalse(hasattr(rule, "train"))
        self.assertIn("accuracy", rule.evaluate({"threshold": 0.0}, self.records, "label"))
        with self.assertRaises(ModelVerificationError):
            rule.parameter_digest({"threshold": "high"})

    def test_the_site_source_reports_a_new_version_after_a_write(self) -> None:
        source = toy.toy_site_source(None, "site_role", "site-2")
        before = source.version()
        source.append({"x1": 0.1, "x2": 0.2, "group": "a"})
        self.assertNotEqual(source.version(), before)
        self.assertEqual(source.count(), 31)
        self.assertNotIn("postgresql", repr(source.location()))


if __name__ == "__main__":
    unittest.main()
