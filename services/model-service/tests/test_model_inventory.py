"""Tests for the provider-agnostic model inventory.

The OCaml router decides *which* model family a dataset and task should get.
Nothing on the Python side used to say which of those families this runtime can
actually execute, so a routable-but-unimplemented family (``xgboost``,
``transformer``) surfaced as a ``ValueError`` from deep inside a check function.
The inventory is the answer to "what can this service run, and with what".

Two properties matter most and are asserted here:

- **The vocabulary cannot drift from the planner.** Family names cross a JSON
  subprocess boundary as strings, so a typo on either side is a runtime failure
  with no compiler to catch it. The inventory's families are checked against the
  OCaml ``string_of_model_family`` mapping, read from the source, which needs no
  ``dagentsc`` build.
- **Nothing here touches the network or imports a heavy dependency at import
  time.** Resolving a capability is data; loading a model is an explicit,
  separate step that refuses to download unless told it may.
"""

from __future__ import annotations

from pathlib import Path
import re
import subprocess
import sys
import unittest

from app.ml.inventory import (
    ANOMALY_DETECTION,
    CLASSIFICATION,
    EMBEDDING,
    FORECASTING,
    REGRESSION,
    TASK_TYPES,
    CapabilityStatus,
    ModelCapability,
    ModelInventory,
    ModelProvider,
    UnsupportedModelError,
    build_default_inventory,
)


REPO_ROOT = Path(__file__).resolve().parents[3]
COMMON_IR = REPO_ROOT / "bindings" / "ocaml" / "lib" / "common_ir" / "dagents_common_ir.ml"
COMMON_IR_MLI = REPO_ROOT / "bindings" / "ocaml" / "lib" / "common_ir" / "dagents_common_ir.mli"


def ocaml_model_families() -> frozenset[str]:
    """Read the planner's model-family vocabulary out of the OCaml source.

    Params:
    - None.

    What it does:
    - Parses the `| Constructor -> "string"` arms of `string_of_model_family`,
      which is the only place the wire names are defined.
    - Fails loudly if it finds implausibly few, so a parser that stops
      understanding the source cannot quietly report an empty set and let this
      whole check pass by accident.

    Returns:
    - The set of family strings the planner can emit.
    """
    source = COMMON_IR.read_text(encoding="utf-8")
    block = re.search(
        r"let string_of_model_family = function(.*?)\n\n", source, re.DOTALL
    )
    if block is None:
        raise AssertionError(
            f"string_of_model_family not found in {COMMON_IR}; the cross-language "
            "vocabulary check in this test needs updating"
        )
    families = frozenset(re.findall(r'->\s*"([^"]+)"', block.group(1)))
    if len(families) < 5:
        raise AssertionError(
            f"Only parsed {sorted(families)} from string_of_model_family; the parser is broken"
        )
    return families


def ocaml_task_types() -> frozenset[str]:
    """Read the planner's task vocabulary out of the OCaml interface.

    The constructors are declared on one line and converted to snake_case on the
    wire (``AnomalyDetection`` -> ``anomaly_detection``), matching what
    ``dagentsc model route --task`` accepts.
    """
    source = COMMON_IR_MLI.read_text(encoding="utf-8")
    declaration = re.search(r"type task_type =([^\n]+)", source)
    if declaration is None:
        raise AssertionError(f"type task_type not found in {COMMON_IR_MLI}")
    constructors = re.findall(r"[A-Z]\w+", declaration.group(1))
    if len(constructors) < 4:
        raise AssertionError(f"Only parsed {constructors} from type task_type; the parser is broken")
    return frozenset(
        re.sub(r"(?<!^)(?=[A-Z])", "_", constructor).lower() for constructor in constructors
    )


class VocabularyTests(unittest.TestCase):
    """The Python inventory and the OCaml planner must agree on names."""

    def test_task_vocabulary_matches_the_planner(self) -> None:
        self.assertEqual(ocaml_task_types(), TASK_TYPES)

    def test_every_inventory_family_is_a_planner_family(self) -> None:
        """An invented family name would fail only at runtime, in production."""
        inventory = build_default_inventory()
        unknown = set(inventory.families()) - ocaml_model_families()
        self.assertEqual(
            set(),
            unknown,
            f"Inventory names families the planner cannot emit: {sorted(unknown)}",
        )

    def test_every_planner_family_is_accounted_for(self) -> None:
        """Adding a family to the router forces a decision about running it.

        A family the router can select but the inventory never mentions is the
        gap this module exists to close: it would reach the service as a string
        nothing recognises. Recording it as `planned` is a valid answer; silence
        is not.
        """
        inventory = build_default_inventory()
        missing = ocaml_model_families() - set(inventory.families())
        self.assertEqual(
            set(),
            missing,
            f"The planner can route to {sorted(missing)} but the inventory does not list them. "
            "Register a capability — `planned` is fine — so the gap is visible.",
        )

    def test_every_capability_uses_a_known_task(self) -> None:
        for capability in build_default_inventory().capabilities():
            with self.subTest(family=capability.family, task=capability.task):
                self.assertIn(capability.task, TASK_TYPES)


class InventoryBehaviourTests(unittest.TestCase):
    """Lookup, resolution, and the rules that keep the registry unambiguous."""

    def setUp(self) -> None:
        self.inventory = build_default_inventory()

    def test_resolves_an_implemented_capability(self) -> None:
        capability = self.inventory.resolve("autoencoder", ANOMALY_DETECTION)
        self.assertEqual(ModelProvider.PYTORCH, capability.provider)
        self.assertEqual(CapabilityStatus.IMPLEMENTED, capability.status)
        self.assertTrue(capability.entrypoint)

    def test_resolving_a_planned_capability_says_what_would_implement_it(self) -> None:
        """The error has to be actionable, not just a rejection."""
        with self.assertRaises(UnsupportedModelError) as caught:
            self.inventory.resolve("xgboost", CLASSIFICATION)
        message = str(caught.exception)
        self.assertIn("xgboost", message)
        self.assertIn("classification", message)
        self.assertIn("planned", message)

    def test_resolving_an_unknown_family_lists_what_is_available(self) -> None:
        with self.assertRaises(UnsupportedModelError) as caught:
            self.inventory.resolve("not_a_model", CLASSIFICATION)
        self.assertIn("random_forest", str(caught.exception))

    def test_resolving_a_known_family_for_the_wrong_task_is_rejected(self) -> None:
        """`gru` forecasts; it does not classify."""
        with self.assertRaises(UnsupportedModelError):
            self.inventory.resolve("gru", CLASSIFICATION)

    def test_families_for_task_lists_only_implemented_by_default(self) -> None:
        classification = self.inventory.families_for_task(CLASSIFICATION)
        self.assertIn("random_forest", classification)
        self.assertIn("naive_bayes", classification)
        self.assertIn("linear", classification)
        self.assertNotIn("xgboost", classification)
        self.assertIn("xgboost", self.inventory.families_for_task(CLASSIFICATION, implemented_only=False))

    def test_families_for_task_is_sorted_and_deterministic(self) -> None:
        for task in sorted(TASK_TYPES):
            with self.subTest(task=task):
                families = self.inventory.families_for_task(task, implemented_only=False)
                self.assertEqual(tuple(sorted(families)), families)

    def test_the_existing_check_families_are_all_present(self) -> None:
        """The inventory has to cover what the service already runs, or it is fiction."""
        self.assertEqual(
            {"linear", "naive_bayes", "random_forest"},
            set(self.inventory.families_for_task(CLASSIFICATION)),
        )
        self.assertEqual(
            {"linear", "random_forest"},
            set(self.inventory.families_for_task(REGRESSION)),
        )
        self.assertEqual({"gru", "lstm"}, set(self.inventory.families_for_task(FORECASTING)))
        self.assertEqual(
            {"autoencoder", "variational_autoencoder"},
            set(self.inventory.families_for_task(ANOMALY_DETECTION)),
        )

    def test_hugging_face_backs_the_embedding_task(self) -> None:
        """Embedding is the family the planner routes and PyTorch modules do not cover."""
        capability = self.inventory.resolve("transformer", EMBEDDING)
        self.assertEqual(ModelProvider.HUGGINGFACE, capability.provider)
        self.assertTrue(capability.default_checkpoint)
        self.assertTrue(capability.requires_download)
        self.assertIn("transformers", " ".join(capability.extra_requirements))

    def test_pytorch_capabilities_need_no_download(self) -> None:
        for capability in self.inventory.capabilities():
            if capability.provider is ModelProvider.PYTORCH:
                with self.subTest(family=capability.family):
                    self.assertFalse(capability.requires_download)

    def test_gaps_are_exactly_the_unimplemented_capabilities(self) -> None:
        gaps = self.inventory.gaps()
        self.assertTrue(gaps)
        for capability in gaps:
            with self.subTest(family=capability.family, task=capability.task):
                self.assertEqual(CapabilityStatus.PLANNED, capability.status)
        self.assertEqual(
            {(gap.family, gap.task) for gap in gaps},
            {
                (capability.family, capability.task)
                for capability in self.inventory.capabilities()
                if capability.status is CapabilityStatus.PLANNED
            },
        )

    def test_registering_a_duplicate_capability_is_an_error(self) -> None:
        """Two entries for one (family, task) would make resolution import-order dependent."""
        inventory = ModelInventory()
        capability = ModelCapability(
            family="linear",
            task=REGRESSION,
            provider=ModelProvider.SCIKIT_LEARN,
            status=CapabilityStatus.IMPLEMENTED,
            entrypoint="app.ml.checks:_build_regressor",
            notes="",
        )
        inventory.register(capability)
        with self.assertRaises(ValueError):
            inventory.register(capability)

    def test_registering_an_unknown_task_is_an_error(self) -> None:
        with self.assertRaises(ValueError):
            ModelInventory().register(
                ModelCapability(
                    family="linear",
                    task="teleportation",
                    provider=ModelProvider.SCIKIT_LEARN,
                    status=CapabilityStatus.IMPLEMENTED,
                    entrypoint="app.ml.checks:_build_regressor",
                    notes="",
                )
            )

    def test_an_implemented_capability_must_name_an_entrypoint(self) -> None:
        """"Implemented" with nothing to call is the failure this rule prevents."""
        with self.assertRaises(ValueError):
            ModelInventory().register(
                ModelCapability(
                    family="linear",
                    task=REGRESSION,
                    provider=ModelProvider.SCIKIT_LEARN,
                    status=CapabilityStatus.IMPLEMENTED,
                    entrypoint=None,
                    notes="",
                )
            )

    def test_describe_is_json_serializable_and_complete(self) -> None:
        described = self.inventory.describe()
        self.assertEqual(len(self.inventory.capabilities()), len(described["capabilities"]))
        self.assertEqual(sorted(TASK_TYPES), sorted(described["tasks"]))
        import json

        json.dumps(described)  # would raise if a value were not serializable


class ImportCostTests(unittest.TestCase):
    """The inventory is data. Reading it must not drag in a framework."""

    def test_importing_the_inventory_pulls_in_neither_torch_nor_transformers(self) -> None:
        """A service listing its model families should not pay for a torch import.

        This runs in a subprocess because torch is already imported in this one by
        the other model-service tests.
        """
        script = (
            "import sys; import app.ml.inventory as inventory; "
            "inventory.build_default_inventory().describe(); "
            "print(','.join(m for m in ('torch', 'transformers', 'sklearn') if m in sys.modules))"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            env={
                "PATH": "/usr/bin:/bin",
                "PYTHONPATH": f"{REPO_ROOT}:{REPO_ROOT / 'services' / 'model-service'}",
            },
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(
            "",
            completed.stdout.strip(),
            f"app.ml.inventory imported heavy modules: {completed.stdout.strip()}",
        )


if __name__ == "__main__":
    unittest.main()
