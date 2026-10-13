"""The parameter-averaging runtime and the model registry.

The runtime applies the planner's weights to the sites' clipped updates and
refuses an update that does not match the digest its site reported. These
tests build manifests, readiness and results by hand, so they need no planner.
The registry tests run against both coordinator stores; the Postgres half needs
``HEALTHCARE_DEMO_TEST_DATABASE_URL`` (see ``test_site_isolation.py``).
"""

from __future__ import annotations

from pathlib import Path
import sys
import threading
from typing import Any
import unittest
from unittest import mock

APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT / "backend"))
sys.path.insert(0, str(APP_ROOT.parents[1]))

from agents.common.domain.federation import (  # noqa: E402
    AggregationMethod,
    AggregationReadiness,
    FederatedJob,
    RoundManifest,
    SiteResult,
)

from app.domain import stroke_model, stroke_rule  # noqa: E402
from app.domain.dataset import SITES  # noqa: E402
from app.services.runtime import (  # noqa: E402
    CANDIDATE_PREFIX,
    INITIAL_VERSION,
    SEED_VERSION,
    AggregationRefused,
    ModelRegistry,
    ModelVerificationError,
    ParameterAveragingRuntime,
    UpdateDigestMismatch,
    code_digest,
    model_digest,
    update_digest,
)
from app.services.site_store import MemoryCoordinatorStore, PostgresCoordinatorStore  # noqa: E402
from tests.test_site_isolation import SupabaseLikeDatabase, admin_dsn_or_skip  # noqa: E402


def _update(scale: float) -> dict[str, float]:
    """A small update with a different value on every coordinate."""
    return {name: scale * (index + 1) / 100.0 for index, name in enumerate(stroke_model.FEATURES)}


class RuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = MemoryCoordinatorStore()
        self.registry = ModelRegistry(self.store)
        self.runtime = ParameterAveragingRuntime(self.registry)
        self.base = self.registry.ensure_initial()
        self.manifest = RoundManifest(
            round_id="r-train",
            study_id="study",
            condition_id="suspected_stroke",
            phase="training",
            model_version=self.base.version,
            model_artifact_digest=self.base.digest,
            training_code_digest=code_digest(self.base.kind),
            feature_contract_version="v3",
            aggregation=AggregationMethod(kind="secure_aggregation", threshold=2),
        )

    def job(self, site_id: str, task: str = "train", round_id: str = "r-train") -> FederatedJob:
        return FederatedJob(
            round_id=round_id, site_id=site_id, manifest=self.manifest, manifest_digest="digest", task=task
        )

    def result(self, site_id: str, digest: str | None, examples: int = 100) -> SiteResult:
        return SiteResult(
            round_id="r-train",
            site_id=site_id,
            participation="completed",
            code_verified=True,
            privacy_checks_passed=True,
            contributed_examples=examples,
            update_norm=1.0,
            update_digest=digest,
            metrics={"train_loss": 0.5 if site_id == "a" else 0.3},
        )

    def readiness(self, weights: dict[str, float]) -> AggregationReadiness:
        return AggregationReadiness(
            round_id="r-train",
            accepted_sites=sorted(weights),
            accepted_examples=200,
            aggregation_permitted=True,
            site_weights=weights,
        )

    def test_submit_returns_the_digest_of_the_stored_update(self) -> None:
        update = _update(1.0)
        digest = self.runtime.submit(self.job("a"), update)
        self.assertEqual(update_digest(update), digest)
        self.assertEqual(stroke_model.parameter_digest(update), digest)
        self.assertEqual(["r-train"], self.runtime.pending_rounds())

    def test_submit_refuses_what_is_not_one_training_update(self) -> None:
        with self.assertRaises(ValueError):
            self.runtime.submit(self.job("a", task="evaluate"), _update(1.0))
        with self.assertRaises(ValueError):
            self.runtime.submit(self.job("a"), {"not_a_parameter": 1.0})
        with self.assertRaises(ValueError):
            self.runtime.submit(self.job("a"), {"intercept": float("nan")})
        with self.assertRaises(ValueError):
            self.runtime.submit(self.job("a"), {"intercept": True})
        self.runtime.submit(self.job("a"), _update(1.0))
        with self.assertRaises(ValueError):
            self.runtime.submit(self.job("a"), _update(2.0))

    def test_aggregate_applies_the_planner_weights_as_given(self) -> None:
        """The weights are not recomputed from the examples (100 and 100 here)."""
        first, second = _update(1.0), _update(-3.0)
        results = [
            self.result("a", self.runtime.submit(self.job("a"), first)),
            self.result("b", self.runtime.submit(self.job("b"), second)),
        ]
        weights = {"a": 0.25, "b": 0.75}
        candidate = self.runtime.aggregate(self.manifest, self.readiness(weights), results)

        expected = stroke_model.apply_update(self.base.params, [(0.25, first), (0.75, second)])
        stored = self.registry.get(candidate.candidate_version)
        self.assertEqual(expected, stored.params)
        for name in stroke_model.FEATURES:
            self.assertAlmostEqual(0.25 * first[name] + 0.75 * second[name], stored.params[name], places=9)
        self.assertEqual(f"{CANDIDATE_PREFIX}+r-train", candidate.candidate_version)
        self.assertEqual("candidate", stored.status)
        self.assertEqual(INITIAL_VERSION, stored.parent_version)
        self.assertEqual(INITIAL_VERSION, candidate.parent_version)
        self.assertEqual(stroke_model.parameter_digest(expected), candidate.artifact_digest)
        self.assertEqual(stored.digest, candidate.artifact_digest)
        self.assertEqual(f"registry://models/{candidate.candidate_version}", candidate.artifact_uri)
        self.assertAlmostEqual(0.25 * 0.5 + 0.75 * 0.3, candidate.metrics["train_loss"], places=12)
        self.assertEqual([], self.runtime.pending_rounds(), "the updates were kept after aggregation")

    def test_a_site_left_out_by_the_planner_is_not_applied(self) -> None:
        first = _update(1.0)
        results = [
            self.result("a", self.runtime.submit(self.job("a"), first)),
            self.result("b", self.runtime.submit(self.job("b"), _update(5.0))),
        ]
        candidate = self.runtime.aggregate(self.manifest, self.readiness({"a": 1.0}), results)
        self.assertEqual(
            stroke_model.apply_update(self.base.params, [(1.0, first)]),
            self.registry.get(candidate.candidate_version).params,
        )

    def test_aggregate_refuses_an_update_that_does_not_match_its_reported_digest(self) -> None:
        self.runtime.submit(self.job("a"), _update(1.0))
        results = [self.result("a", update_digest(_update(1.5)))]
        with self.assertRaises(UpdateDigestMismatch):
            self.runtime.aggregate(self.manifest, self.readiness({"a": 1.0}), results)
        self.assertIsNone(self.registry.get(f"{CANDIDATE_PREFIX}+r-train"))
        self.assertEqual([], self.runtime.pending_rounds())

    def test_aggregate_refuses_an_update_changed_after_it_was_sent(self) -> None:
        digest = self.runtime.submit(self.job("a"), _update(1.0))
        self.runtime._updates[("r-train", "a")].vector["intercept"] += 0.1
        with self.assertRaises(UpdateDigestMismatch):
            self.runtime.aggregate(self.manifest, self.readiness({"a": 1.0}), [self.result("a", digest)])

    def test_aggregate_refuses_a_missing_update_weight_or_starting_point(self) -> None:
        with self.assertRaises(AggregationRefused):
            self.runtime.aggregate(self.manifest, self.readiness({"a": 1.0}), [self.result("a", "sha256:x")])

        digest = self.runtime.submit(self.job("a"), _update(1.0))
        readiness = self.readiness({"a": 1.0}).model_copy(update={"site_weights": {}})
        with self.assertRaises(AggregationRefused):
            self.runtime.aggregate(self.manifest, readiness, [self.result("a", digest)])

        digest = self.runtime.submit(self.job("a"), _update(1.0))
        wrong = self.manifest.model_copy(update={"model_artifact_digest": "sha256:" + "0" * 64})
        with self.assertRaises(AggregationRefused):
            self.runtime.aggregate(wrong, self.readiness({"a": 1.0}), [self.result("a", digest)])

    def test_training_cannot_start_from_the_rule(self) -> None:
        seed = self.registry.ensure_seed()
        manifest = self.manifest.model_copy(
            update={"model_version": seed.version, "model_artifact_digest": seed.digest}
        )
        digest = self.runtime.submit(self.job("a"), _update(1.0))
        with self.assertRaises(AggregationRefused):
            self.runtime.aggregate(manifest, self.readiness({"a": 1.0}), [self.result("a", digest)])

    def test_other_rounds_average_metrics_and_register_nothing(self) -> None:
        manifest = self.manifest.model_copy(update={"phase": "evaluation"})
        results = [self.result("a", None), self.result("b", None)]
        candidate = self.runtime.aggregate(manifest, self.readiness({"a": 0.5, "b": 0.5}), results)
        self.assertAlmostEqual(0.4, candidate.metrics["train_loss"])
        self.assertEqual("", candidate.artifact_digest)
        self.assertEqual([INITIAL_VERSION], [model["version"] for model in self.registry.list()])


class RegistryContract:
    """The registry's behaviour, run against each coordinator store."""

    def make_store(self) -> Any:
        raise NotImplementedError

    def setUp(self) -> None:
        self.registry = ModelRegistry(self.make_store())

    def test_the_seed_rule_is_registered_as_the_release_on_first_use(self) -> None:
        seed = self.registry.ensure_seed()
        self.assertEqual(SEED_VERSION, seed.version)
        self.assertEqual(stroke_rule.RULE_KIND, seed.kind)
        self.assertEqual(stroke_rule.rule_digest(), seed.digest)
        self.assertEqual(stroke_rule.rule_parameters(), seed.params)
        self.assertEqual("released", seed.status)
        self.assertEqual(seed, self.registry.model_in_use())
        self.assertEqual(1, len(self.registry.list()))

    def test_no_seed_is_written_while_a_model_is_released(self) -> None:
        initial = self.registry.ensure_initial()
        candidate = self.registry.register_candidate("v-test", stroke_model.initial_params(), initial.version)
        self.registry.set_status(candidate.version, "released")
        self.assertEqual("v-test", self.registry.model_in_use().version)
        self.assertNotIn(SEED_VERSION, [model["version"] for model in self.registry.list()])

    def test_load_checks_the_digest_a_round_names(self) -> None:
        seed = self.registry.ensure_seed()
        self.assertEqual(seed, self.registry.load(seed.version, stroke_rule.rule_digest()))
        initial = self.registry.ensure_initial()
        self.assertEqual(stroke_model.parameter_digest(stroke_model.initial_params()), initial.digest)
        self.assertEqual(initial, self.registry.load(INITIAL_VERSION, initial.digest))
        with self.assertRaises(ModelVerificationError):
            self.registry.load(INITIAL_VERSION, seed.digest)
        with self.assertRaises(ModelVerificationError):
            self.registry.load("no-such-version", seed.digest)

    def test_the_initial_version_is_stored_once(self) -> None:
        first = self.registry.ensure_initial()
        second = self.registry.ensure_initial()
        self.assertEqual(first.digest, second.digest)
        self.assertEqual(1, len(self.registry.list()))

    def count_reads(self) -> list[str]:
        """Record every version the registry reads from its store."""
        reads: list[str] = []
        store = self.registry.store
        real = store.get_model

        def counting(version: str) -> Any:
            reads.append(version)
            return real(version)

        patcher = mock.patch.object(store, "get_model", side_effect=counting)
        patcher.start()
        self.addCleanup(patcher.stop)
        return reads

    def test_inside_pinned_each_version_is_read_from_the_store_once(self) -> None:
        initial = self.registry.ensure_initial()
        reads = self.count_reads()
        with self.registry.pinned():
            for _ in range(5):
                self.assertEqual(initial.digest, self.registry.load(INITIAL_VERSION, initial.digest).digest)
            # A version written through the registry is known without a read,
            # and a status changed through it is the one returned.
            candidate = self.registry.register_candidate("v-pin", stroke_model.initial_params(), INITIAL_VERSION)
            self.registry.load("v-pin", candidate.digest)
            self.registry.set_status("v-pin", "rejected")
            self.assertEqual("rejected", self.registry.get("v-pin").status)
            with self.assertRaises(ModelVerificationError):
                self.registry.load(INITIAL_VERSION, stroke_rule.rule_digest())
        self.assertEqual([INITIAL_VERSION], reads)
        # Outside the block every read goes to the store again.
        self.registry.load(INITIAL_VERSION, initial.digest)
        self.assertEqual("rejected", self.registry.get("v-pin").status)
        self.assertEqual([INITIAL_VERSION, INITIAL_VERSION, "v-pin"], reads)

    def test_pinned_reads_belong_to_the_thread_that_pinned(self) -> None:
        initial = self.registry.ensure_initial()
        reads = self.count_reads()
        with self.registry.pinned():
            self.registry.load(INITIAL_VERSION, initial.digest)
            other = threading.Thread(target=self.registry.load, args=(INITIAL_VERSION, initial.digest))
            other.start()
            other.join()
            self.registry.load(INITIAL_VERSION, initial.digest)
        self.assertEqual([INITIAL_VERSION, INITIAL_VERSION], reads)

    def test_a_release_decision_is_stored_with_the_version(self) -> None:
        initial = self.registry.ensure_initial()
        self.assertIsNone(initial.release_decision)
        self.assertIn("release_decision", initial.summary())
        candidate = self.registry.register_candidate("v-decided", stroke_model.initial_params(), INITIAL_VERSION)
        decided = self.registry.record_release_decision(
            candidate.version,
            action="reject",
            guard_permitted=True,
            gate_results=[{"gate_id": "discrimination", "outcome": "fail"}],
            round_id="r-1",
            baseline_version=INITIAL_VERSION,
        )
        self.assertEqual("reject", decided.release_decision["action"])
        self.assertEqual(INITIAL_VERSION, decided.release_decision["baseline_version"])
        self.assertEqual(decided.release_decision, decided.summary()["release_decision"])
        # Another registry on the same store, as after a restart, reads it back.
        again = ModelRegistry(self.registry.store).get(candidate.version)
        self.assertEqual(decided.release_decision, again.release_decision)
        self.assertEqual("candidate", again.status)


class MemoryRegistryTests(RegistryContract, unittest.TestCase):
    def make_store(self) -> MemoryCoordinatorStore:
        return MemoryCoordinatorStore()

    def test_load_refuses_parameters_changed_in_the_store(self) -> None:
        store = MemoryCoordinatorStore()
        registry = ModelRegistry(store)
        initial = registry.ensure_initial()
        store._models[INITIAL_VERSION]["params"]["intercept"] = 1.0
        with self.assertRaises(ModelVerificationError):
            registry.load(INITIAL_VERSION, initial.digest)

    def test_a_change_made_during_a_pin_is_refused_once_it_ends(self) -> None:
        """Inside the pin the verified copy is used; the next read from the store sees the change."""
        store = MemoryCoordinatorStore()
        registry = ModelRegistry(store)
        initial = registry.ensure_initial()
        with registry.pinned():
            loaded = registry.load(INITIAL_VERSION, initial.digest)
            store._models[INITIAL_VERSION]["params"]["intercept"] = 1.0
            self.assertEqual(loaded, registry.load(INITIAL_VERSION, initial.digest))
        with self.assertRaises(ModelVerificationError):
            registry.load(INITIAL_VERSION, initial.digest)


class PostgresRegistryTests(RegistryContract, unittest.TestCase):
    db: SupabaseLikeDatabase

    @classmethod
    def setUpClass(cls) -> None:
        cls.db = SupabaseLikeDatabase(admin_dsn_or_skip())
        try:
            cls.db.start()
        except BaseException:
            cls.db.stop()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.stop()

    def make_store(self) -> PostgresCoordinatorStore:
        with self.db.admin() as conn:
            conn.execute("DELETE FROM coordinator.site_contributions")
            # Children first: parent_version references another row.
            conn.execute("UPDATE coordinator.models SET parent_version = NULL")
            conn.execute("DELETE FROM coordinator.models")
        return PostgresCoordinatorStore(self.db.dsn, SITES)

    def test_load_refuses_parameters_changed_in_the_database(self) -> None:
        initial = self.registry.ensure_initial()
        with self.db.admin() as conn:
            conn.execute(
                "UPDATE coordinator.models SET params = jsonb_set(params, '{intercept}', '0.7') WHERE version = %s",
                (INITIAL_VERSION,),
            )
        with self.assertRaises(ModelVerificationError):
            self.registry.load(INITIAL_VERSION, initial.digest)


class DigestTests(unittest.TestCase):
    def test_a_rule_that_differs_from_the_code_does_not_verify(self) -> None:
        params = stroke_rule.rule_parameters()
        self.assertEqual(stroke_rule.rule_digest(), model_digest(stroke_rule.RULE_KIND, params))
        params["atrial_fibrillation_points"] = 3
        with self.assertRaises(ModelVerificationError):
            model_digest(stroke_rule.RULE_KIND, params)

    def test_unknown_kinds_and_parameters_do_not_verify(self) -> None:
        with self.assertRaises(ModelVerificationError):
            model_digest("neural_network", {})
        with self.assertRaises(ModelVerificationError):
            model_digest(stroke_model.MODEL_KIND, {"unknown": 1.0})
        with self.assertRaises(ModelVerificationError):
            code_digest("neural_network")

    def test_each_kind_names_the_code_it_runs(self) -> None:
        self.assertEqual(stroke_rule.rule_digest(), code_digest(stroke_rule.RULE_KIND))
        self.assertEqual(stroke_model.training_code_digest(), code_digest(stroke_model.MODEL_KIND))


if __name__ == "__main__":
    unittest.main()
