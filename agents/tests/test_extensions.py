"""Tests for the consumer extension registry.

The registry is what keeps domain logic out of the framework while still letting
a consumer reach the framework's generic machinery. These tests pin the
properties that make that safe: conflicts are errors, dangling references are
errors, and a failed registration leaves nothing behind.
"""

from __future__ import annotations

import unittest

from agents.common.domain.federation import GateComparison, ReleaseGate
from agents.common.domain.governance import DataClassification
from agents.common.extensions import (
    ConditionPack,
    ExtensionError,
    ExtensionRegistry,
    FeatureContract,
    FeatureField,
)


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


if __name__ == "__main__":
    unittest.main()
