"""The two model kinds the app contributes, and the extension that contributes them.

A model kind is how the Dagents LMA and GMA run this app's models without
importing them. The LMA evaluates and trains a model through its kind, and the
GMA checks each site's update and combines the updates through it. The app
keeps the maths; the agents keep the transport, the storage and the checks.

These tests check that:

- each kind computes the digests the app computed before it used the agents,
  so the model versions already stored in the coordinator schema verify
  unchanged (the digests are written out below);
- evaluation, training, one training round and the hospital views give what
  the app's own code computed at 6fb62ac from the same records
  (``fixtures/model_kinds_6fb62ac.json``);
- ``combine`` is :func:`stroke_model.apply_update`, with the planner's weights
  as given;
- the seed rule cannot be trained;
- the extension registers in a fresh registry, and the GMA accepts the
  versions the app registers when it starts (with the real planner).
"""

from __future__ import annotations

import json
import math
from pathlib import Path
import sys
import unittest
from unittest import mock

from agents.common.domain.errors import ModelVerificationError
from agents.common.domain.federation import ModelVersionRegistration
from agents.common.extensions import ExtensionError, ExtensionRegistry, TrainingOutcome, load_extensions

from app.domain import model_kinds, stroke_model, stroke_rule
from app.domain.conditions import (
    STROKE_APPROVED_PURPOSES,
    STROKE_CONDITION,
    STROKE_FEATURE_CONTRACT,
    STROKE_LABEL_FIELD,
    STROKE_RELEASE_GATES,
)
from app.domain.dataset import load_site_records
from app.domain.model_kinds import (
    INITIAL_VERSION,
    LOGISTIC_REGRESSION,
    POINTS_RULE,
    SEED_VERSION,
    LogisticRegressionKind,
    PointsRuleKind,
)
from app.extension import HealthcareStrokeExtension, healthcare_site_store, install
from tests.support import dagentsc_available

LABEL = STROKE_LABEL_FIELD

#: The digests the app computed before it used the agents. The coordinator
#: schema stores the seed rule and the starting model with them, so a kind that
#: computed another digest would make the GMA refuse the app's registrations.
SEED_DIGEST = "sha256:04177e54a0c7c829a484a962f37eb0945a39f1ff7e5dc3319b266842fc3bd416"
INITIAL_DIGEST = "sha256:aa3d97c86ae04f1598eba39e3224607034579ba621fd11c00eba4b11d4aaf974"
#: The digest of :func:`_update` with scale 1.0.
SAMPLE_UPDATE_DIGEST = "sha256:5084d5a099bdd208732957bd76c78fdc79a27ef092319ecc9889a6b56b24a2fd"

PARITY_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "pilot_parity_6fb62ac.json"
#: What the app's own code computed at 6fb62ac, before the kinds replaced it.
RECORDED_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "model_kinds_6fb62ac.json"

RECORD = {
    "age_band": "70-79",
    "sex": "female",
    "conscious_level": "drowsy",
    "face_weakness": True,
    "arm_weakness": False,
    "leg_weakness": True,
    "speech_difficulty": True,
    "visual_field_loss": False,
    "atrial_fibrillation": True,
    "onset_on_waking": False,
    "stroke_subtype": "TACS",
    "hours_since_onset": 12.0,
    "systolic_bp": 190.0,
}

#: Parameters with a different value on every coordinate, for scoring.
SCORING_PARAMS = {
    name: round(((-1) ** index) * (index + 1) / 10.0, 10) for index, name in enumerate(stroke_model.FEATURES)
}


def _update(scale: float) -> dict[str, float]:
    """A small update with a different value on every coordinate."""
    return {name: scale * (index + 1) / 100.0 for index, name in enumerate(stroke_model.FEATURES)}


def _labelled(records: list[dict]) -> list[dict]:
    return [record for record in records if isinstance(record.get(LABEL), bool)]


def _rule_log_loss(records: list[dict]) -> float:
    """Mean log loss of the rule over the labelled records it scores, as a hospital computed it."""
    total = 0.0
    count = 0
    for record in _labelled(records):
        score = stroke_rule.rule_score(record)
        if score is None:
            continue
        p = min(max(score, 1e-12), 1.0 - 1e-12)
        total -= math.log(p) if record[LABEL] else math.log(1.0 - p)
        count += 1
    return total / count


class DigestTests(unittest.TestCase):
    def test_the_seed_rule_has_the_digest_the_coordinator_stores(self) -> None:
        params = stroke_rule.rule_parameters()
        self.assertEqual(SEED_DIGEST, POINTS_RULE.parameter_digest(params))
        self.assertEqual(stroke_rule.rule_digest(), POINTS_RULE.parameter_digest(params))
        # The GMA stores parameters as JSON, so they come back through a round trip.
        self.assertEqual(SEED_DIGEST, POINTS_RULE.parameter_digest(json.loads(json.dumps(params))))
        # The pilot the parity test recorded used the same seed release.
        fixture = json.loads(PARITY_FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(SEED_DIGEST, fixture["view"]["model_in_use"]["digest"])

    def test_a_rule_that_differs_from_the_code_does_not_verify(self) -> None:
        params = stroke_rule.rule_parameters()
        params["atrial_fibrillation_points"] = 3
        with self.assertRaises(ModelVerificationError):
            POINTS_RULE.parameter_digest(params)
        for invalid in ({}, None, [1, 2], {**stroke_rule.rule_parameters(), "extra": 1}):
            with self.subTest(params=invalid), self.assertRaises(ModelVerificationError):
                POINTS_RULE.parameter_digest(invalid)  # type: ignore[arg-type]

    def test_the_starting_model_has_the_digest_the_coordinator_stores(self) -> None:
        params = stroke_model.initial_params()
        self.assertEqual(INITIAL_DIGEST, LOGISTIC_REGRESSION.parameter_digest(params))
        self.assertEqual(INITIAL_DIGEST, LOGISTIC_REGRESSION.parameter_digest(json.loads(json.dumps(params))))

    def test_parameters_and_updates_share_one_digest_rule(self) -> None:
        update = _update(1.0)
        self.assertEqual(SAMPLE_UPDATE_DIGEST, LOGISTIC_REGRESSION.parameter_digest(update))
        self.assertEqual(SAMPLE_UPDATE_DIGEST, LOGISTIC_REGRESSION.update_digest(update))
        self.assertEqual(stroke_model.parameter_digest(update), LOGISTIC_REGRESSION.update_digest(update))

    def test_invalid_parameters_do_not_verify(self) -> None:
        cases = {
            "unknown name": {"not_a_parameter": 1.0},
            "not a number": {"intercept": "0.5"},
            "yes or no": {"intercept": True},
            "not finite": {"intercept": float("nan")},
            "infinite": {"intercept": float("inf")},
            "not a mapping": [0.0],
        }
        for name, params in cases.items():
            with self.subTest(case=name), self.assertRaises(ModelVerificationError):
                LOGISTIC_REGRESSION.parameter_digest(params)  # type: ignore[arg-type]
        # The framework's error is a ValueError, which the LMA and the GMA catch.
        self.assertTrue(issubclass(ModelVerificationError, ValueError))

    def test_each_kind_names_the_code_it_runs(self) -> None:
        self.assertEqual(stroke_rule.rule_digest(), POINTS_RULE.code_digest())
        self.assertEqual(stroke_model.training_code_digest(), LOGISTIC_REGRESSION.code_digest())


class RecordedOutputTests(unittest.TestCase):
    """The kinds against what the app's own code computed at 6fb62ac.

    ``fixtures/model_kinds_6fb62ac.json`` holds the values ``services/runtime.py``
    and ``services/hospital.py`` computed from each hospital's committed
    records: digests, evaluation, local training, one size-weighted training
    round, and the scores and assessments the hospital views showed. That code
    is gone, so the file cannot be captured again. Every value must match
    exactly.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.recorded = json.loads(RECORDED_FIXTURE.read_text(encoding="utf-8"))
        cls.candidate = cls.recorded["round_1"]["candidate_params"]

    def test_the_digests_match(self) -> None:
        seed, initial = self.recorded["seed"], self.recorded["initial"]
        self.assertEqual((SEED_VERSION, POINTS_RULE.kind), (seed["version"], seed["kind"]))
        self.assertEqual(seed["digest"], POINTS_RULE.parameter_digest(stroke_rule.rule_parameters()))
        self.assertEqual(seed["code_digest"], POINTS_RULE.code_digest())
        self.assertEqual((INITIAL_VERSION, LOGISTIC_REGRESSION.kind), (initial["version"], initial["kind"]))
        self.assertEqual(initial["digest"], LOGISTIC_REGRESSION.parameter_digest(stroke_model.initial_params()))
        self.assertEqual(initial["code_digest"], LOGISTIC_REGRESSION.code_digest())
        self.assertEqual(self.recorded["candidate_prefix"], LOGISTIC_REGRESSION.version_prefix)

    def test_each_hospital_evaluates_and_trains_as_recorded(self) -> None:
        models = {
            "evaluate_rule": (POINTS_RULE, stroke_rule.rule_parameters()),
            "evaluate_initial": (LOGISTIC_REGRESSION, stroke_model.initial_params()),
            "evaluate_candidate": (LOGISTIC_REGRESSION, self.candidate),
        }
        starts = {"train_initial": stroke_model.initial_params(), "train_candidate": self.candidate}
        for site_id, recorded in self.recorded["sites"].items():
            records = load_site_records(site_id)
            labelled = _labelled(records)
            self.assertEqual(recorded["record_count"], len(records))
            for key, (kind, params) in models.items():
                with self.subTest(site=site_id, run=key):
                    self.assertEqual(recorded[key]["metrics"], kind.evaluate(params, labelled, LABEL))
                    self.assertEqual(recorded[key]["examples"], len(labelled))
            for key, start in starts.items():
                with self.subTest(site=site_id, run=key):
                    outcome = LOGISTIC_REGRESSION.train(start, records, LABEL)
                    self.assertEqual(recorded[key]["metrics"], outcome.metrics)
                    self.assertEqual(recorded[key]["examples"], outcome.examples)
                    self.assertEqual(recorded[key]["update_digest"], LOGISTIC_REGRESSION.update_digest(outcome.update))
                    self.assertEqual(recorded[key]["update_norm"], stroke_model.l2_norm(outcome.update))
                    if "update" in recorded[key]:
                        self.assertEqual(recorded[key]["update"], outcome.update)

    def test_one_training_round_combines_as_recorded(self) -> None:
        recorded = self.recorded["round_1"]
        start = stroke_model.initial_params()
        updates = {
            site_id: LOGISTIC_REGRESSION.train(start, load_site_records(site_id), LABEL).update
            for site_id in recorded["weights"]
        }
        # The GMA passes the updates in site order, with the planner's weights.
        pairs = [(recorded["weights"][site_id], updates[site_id]) for site_id in sorted(updates)]
        combined = LOGISTIC_REGRESSION.combine(start, pairs)
        self.assertEqual(recorded["candidate_params"], combined)
        self.assertEqual(recorded["candidate_digest"], LOGISTIC_REGRESSION.parameter_digest(combined))
        self.assertEqual(recorded["parameter_change_from_initial"], LOGISTIC_REGRESSION.parameter_change(combined, start))
        self.assertEqual(recorded["candidate_version"], f"{LOGISTIC_REGRESSION.version_prefix}+golden-train-1")

    def test_the_hospital_views_score_and_explain_as_recorded(self) -> None:
        views = self.recorded["views"]
        records = load_site_records(views["site_id"])
        shown = records[: views["record_count"]]
        rule = stroke_rule.rule_parameters()
        version = self.recorded["round_1"]["candidate_version"]
        self.assertEqual(views["rule_scores"], [model_kinds.score("points_rule", rule, record) for record in shown])
        self.assertEqual(
            views["candidate_scores"], [model_kinds.score("logistic_regression", self.candidate, record) for record in shown]
        )
        self.assertEqual(
            views["rule_assessments"], [model_kinds.assess(SEED_VERSION, "points_rule", rule, record) for record in shown]
        )
        self.assertEqual(
            views["candidate_assessments"],
            [model_kinds.assess(version, "logistic_regression", self.candidate, record) for record in shown],
        )
        no_examination = {"age_band": "70-79", "sex": "female"}
        self.assertEqual(
            views["suppressed_rule_assessment"], model_kinds.assess(SEED_VERSION, "points_rule", rule, no_examination)
        )
        self.assertEqual(
            views["suppressed_candidate_assessment"],
            model_kinds.assess(version, "logistic_regression", self.candidate, no_examination),
        )
        sweep = stroke_rule.sweep_alert_thresholds(
            records, score_fn=lambda record: model_kinds.score("logistic_regression", self.candidate, record)
        )
        self.assertEqual(views["candidate_sweep"], sweep)
        local = stroke_rule.evaluate_against_labels(
            _labelled(records), score_fn=lambda record: model_kinds.score("points_rule", rule, record)
        )
        self.assertEqual(views["rule_local_metrics"], local)

    def test_what_was_refused_is_still_refused(self) -> None:
        self.assertEqual({"ModelVerificationError"}, set(self.recorded["refusals"].values()))
        changed = {**stroke_rule.rule_parameters(), "atrial_fibrillation_points": 3}
        with self.assertRaises(ModelVerificationError):
            POINTS_RULE.parameter_digest(changed)
        with self.assertRaises(ModelVerificationError):
            LOGISTIC_REGRESSION.parameter_digest({"unknown": 1.0})
        with self.assertRaises(ModelVerificationError):
            model_kinds.kind_for("neural_network")


class IdentityTests(unittest.TestCase):
    def test_each_kind_keeps_the_name_the_coordinator_stores(self) -> None:
        self.assertEqual((stroke_rule.RULE_KIND, False), (POINTS_RULE.kind, POINTS_RULE.trainable))
        self.assertEqual((stroke_model.MODEL_KIND, True), (LOGISTIC_REGRESSION.kind, LOGISTIC_REGRESSION.trainable))
        # A training round's candidate is named <prefix>+<round id>, as before.
        self.assertEqual("stroke-outcome-logreg", LOGISTIC_REGRESSION.version_prefix)
        self.assertTrue(POINTS_RULE.version_prefix)

    def test_the_rule_cannot_be_trained(self) -> None:
        self.assertFalse(POINTS_RULE.trainable)
        for member in ("train", "check_update", "update_digest", "combine", "parameter_change"):
            with self.subTest(member=member):
                self.assertFalse(hasattr(POINTS_RULE, member))

    def test_a_kind_is_found_by_its_name(self) -> None:
        self.assertIs(POINTS_RULE, model_kinds.kind_for(stroke_rule.RULE_KIND))
        self.assertIs(LOGISTIC_REGRESSION, model_kinds.kind_for(stroke_model.MODEL_KIND))
        with self.assertRaises(ModelVerificationError):
            model_kinds.kind_for("neural_network")


class EvaluationTests(unittest.TestCase):
    """``evaluate`` is what a hospital computed for an evaluation round: the rule's metrics and the log loss."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.records = load_site_records("ch-245")
        cls.labelled = _labelled(cls.records)

    def test_the_rule_is_measured_as_before(self) -> None:
        metrics = POINTS_RULE.evaluate(stroke_rule.rule_parameters(), self.labelled, LABEL)
        expected = stroke_rule.evaluate_against_labels(self.labelled, score_fn=stroke_rule.rule_score)
        expected["log_loss"] = _rule_log_loss(self.labelled)
        self.assertEqual(expected, metrics)
        self.assertEqual({"auc", "alerts_per_1000", "sensitivity", "specificity", "log_loss"} - set(metrics), set())

    def test_the_logistic_regression_is_measured_as_before(self) -> None:
        params = stroke_model.train_local(stroke_model.initial_params(), self.labelled, LABEL)
        metrics = LOGISTIC_REGRESSION.evaluate(params, self.labelled, LABEL)
        expected = stroke_rule.evaluate_against_labels(
            self.labelled, score_fn=lambda record: stroke_model.score(params, record)
        )
        loss = metrics.pop("log_loss")
        self.assertEqual(expected, metrics)
        self.assertAlmostEqual(stroke_model.log_loss(params, self.labelled, LABEL), loss, places=12)

    def test_records_without_a_label_do_not_count(self) -> None:
        unlabelled = [{**record, LABEL: None} for record in self.records[:20]]
        mixed = self.labelled + unlabelled
        rule = stroke_rule.rule_parameters()
        self.assertEqual(POINTS_RULE.evaluate(rule, self.labelled, LABEL), POINTS_RULE.evaluate(rule, mixed, LABEL))
        self.assertEqual({}, POINTS_RULE.evaluate(rule, unlabelled, LABEL))
        self.assertEqual({}, LOGISTIC_REGRESSION.evaluate(stroke_model.initial_params(), unlabelled, LABEL))

    def test_parameters_that_do_not_verify_are_refused(self) -> None:
        rule = stroke_rule.rule_parameters()
        rule["risk_midpoint_points"] = 4.0
        with self.assertRaises(ModelVerificationError):
            POINTS_RULE.evaluate(rule, self.labelled, LABEL)
        with self.assertRaises(ModelVerificationError):
            LOGISTIC_REGRESSION.evaluate({"not_a_parameter": 1.0}, self.labelled, LABEL)


class TrainingTests(unittest.TestCase):
    """``train`` is what a hospital computed for a training round."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.records = load_site_records("ch-245")
        cls.labelled = _labelled(cls.records)
        cls.start = stroke_model.initial_params()

    def test_training_returns_the_change_in_parameters_and_the_losses(self) -> None:
        outcome = LOGISTIC_REGRESSION.train(self.start, self.records, LABEL)
        self.assertIsInstance(outcome, TrainingOutcome)
        local = stroke_model.train_local(self.start, self.labelled, LABEL)
        self.assertEqual(stroke_model.delta(local, self.start), outcome.update)
        self.assertEqual(
            {
                "start_loss": stroke_model.log_loss(self.start, self.labelled, LABEL),
                "train_loss": stroke_model.log_loss(local, self.labelled, LABEL),
                "record_count": float(len(self.labelled)),
            },
            outcome.metrics,
        )
        self.assertEqual(len(self.labelled), outcome.examples)
        self.assertLess(outcome.metrics["train_loss"], outcome.metrics["start_loss"])

    def test_training_is_deterministic(self) -> None:
        first = LOGISTIC_REGRESSION.train(self.start, self.records, LABEL)
        second = LOGISTIC_REGRESSION.train(self.start, list(self.records), LABEL)
        self.assertEqual(LOGISTIC_REGRESSION.update_digest(first.update), LOGISTIC_REGRESSION.update_digest(second.update))

    def test_no_labelled_record_sends_nothing(self) -> None:
        unlabelled = [{**record, LABEL: None} for record in self.records[:10]]
        outcome = LOGISTIC_REGRESSION.train(self.start, unlabelled, LABEL)
        self.assertEqual(TrainingOutcome(update=None, metrics={"record_count": 0.0}, examples=0), outcome)

    def test_training_from_parameters_that_do_not_verify_is_refused(self) -> None:
        with self.assertRaises(ModelVerificationError):
            LOGISTIC_REGRESSION.train({"not_a_parameter": 1.0}, self.records, LABEL)


class UpdateTests(unittest.TestCase):
    """How the GMA checks and combines the sites' updates."""

    def test_check_update_returns_floats(self) -> None:
        checked = LOGISTIC_REGRESSION.check_update({"intercept": 1, "age_band": 0.25})
        self.assertEqual({"intercept": 1.0, "age_band": 0.25}, checked)
        self.assertTrue(all(type(value) is float for value in checked.values()))

    def test_check_update_refuses_what_is_not_an_update(self) -> None:
        cases = {
            "unknown name": {"not_a_parameter": 1.0},
            "not finite": {"intercept": float("nan")},
            "infinite": {"intercept": float("-inf")},
            "yes or no": {"intercept": True},
            "text": {"intercept": "0.1"},
            "not a mapping": [("intercept", 0.1)],
        }
        for name, update in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(ValueError):
                    LOGISTIC_REGRESSION.check_update(update)  # type: ignore[arg-type]
                with self.assertRaises(ValueError):
                    LOGISTIC_REGRESSION.update_digest(update)  # type: ignore[arg-type]

    def test_combine_applies_the_planner_weights_as_given(self) -> None:
        """The weights are not recomputed or rescaled, even when they do not sum to 1."""
        start = stroke_model.initial_params()
        first, second = _update(1.0), _update(-3.0)
        for weights in ((0.25, 0.75), (0.5, 0.75)):
            with self.subTest(weights=weights):
                pairs = [(weights[0], first), (weights[1], second)]
                combined = LOGISTIC_REGRESSION.combine(start, pairs)
                self.assertEqual(stroke_model.apply_update(start, pairs), combined)
                for name in stroke_model.FEATURES:
                    self.assertAlmostEqual(
                        weights[0] * first[name] + weights[1] * second[name], combined[name], places=9
                    )

    def test_combine_from_a_trained_model_adds_to_its_parameters(self) -> None:
        start = _update(10.0)
        pairs = [(1.0, _update(1.0))]
        self.assertEqual(stroke_model.apply_update(start, pairs), LOGISTIC_REGRESSION.combine(start, pairs))
        self.assertEqual(start, LOGISTIC_REGRESSION.combine(start, []))

    def test_combine_refuses_an_update_or_a_start_that_does_not_verify(self) -> None:
        start = stroke_model.initial_params()
        with self.assertRaises(ValueError):
            LOGISTIC_REGRESSION.combine(start, [(1.0, {"not_a_parameter": 1.0})])
        with self.assertRaises(ValueError):
            LOGISTIC_REGRESSION.combine(start, [(1.0, {"intercept": float("nan")})])
        with self.assertRaises(ValueError):
            LOGISTIC_REGRESSION.combine({"not_a_parameter": 0.0}, [(1.0, _update(1.0))])

    def test_parameter_change_is_the_size_of_the_change(self) -> None:
        start = stroke_model.initial_params()
        candidate = stroke_model.apply_update(start, [(1.0, _update(2.0))])
        self.assertEqual(
            stroke_model.l2_norm(stroke_model.delta(candidate, start)),
            LOGISTIC_REGRESSION.parameter_change(candidate, start),
        )
        self.assertEqual(0.0, LOGISTIC_REGRESSION.parameter_change(start, start))


class ScoringTests(unittest.TestCase):
    """What the hospital views show for a record, with the model the hospital's LMA reports in use."""

    def test_the_rule_scores_and_explains_a_record_as_before(self) -> None:
        params = stroke_rule.rule_parameters()
        expected = stroke_rule.assess(RECORD).as_dict()
        expected.update(model_version=SEED_VERSION, model_kind=stroke_rule.RULE_KIND)
        self.assertEqual(expected, model_kinds.assess(SEED_VERSION, stroke_rule.RULE_KIND, params, RECORD))
        self.assertEqual(stroke_rule.rule_score(RECORD), model_kinds.score(stroke_rule.RULE_KIND, params, RECORD))
        suppressed = {"age_band": "70-79", "sex": "female"}
        self.assertIsNone(model_kinds.score(stroke_rule.RULE_KIND, params, suppressed))
        self.assertTrue(model_kinds.assess(SEED_VERSION, stroke_rule.RULE_KIND, params, suppressed)["suppressed"])

    def test_the_logistic_regression_explains_a_record_as_before(self) -> None:
        """The values were written down from the app before it used the agents."""
        assessment = model_kinds.assess("v", stroke_model.MODEL_KIND, SCORING_PARAMS, RECORD)
        self.assertEqual(
            {
                "model_version": "v",
                "model_kind": "logistic_regression",
                "score": 0.2227,
                "priority": "routine",
                "contributions": {
                    "age_band": -0.1,
                    "arm_weakness": 0.3,
                    "atrial_fibrillation": -0.6,
                    "conscious_level": -0.4,
                    "face_weakness": 0.25,
                    "hours_since_onset": 0.5,
                    "leg_weakness": 0.35,
                    "onset_on_waking": -0.65,
                    "sex_female": 0.15,
                    "speech_difficulty": -0.4,
                    "subtype_tacs": -1.4,
                    "systolic_bp": 1.1,
                    "visual_field_loss": -0.45,
                },
                "basis": [
                    "subtype TACS: -1.40 on the log-odds scale",
                    "systolic blood pressure: +1.10 on the log-odds scale",
                    "onset on waking: -0.65 on the log-odds scale",
                ],
                "warnings": [],
                "data_quality_issues": [],
                "suppressed": False,
            },
            assessment,
        )
        self.assertEqual(
            stroke_model.score(SCORING_PARAMS, RECORD),
            model_kinds.score(stroke_model.MODEL_KIND, SCORING_PARAMS, RECORD),
        )

    def test_the_starting_model_scores_every_record_one_half_with_no_basis(self) -> None:
        assessment = model_kinds.assess(INITIAL_VERSION, stroke_model.MODEL_KIND, stroke_model.initial_params(), RECORD)
        self.assertEqual((0.5, {}, []), (assessment["score"], assessment["contributions"], assessment["basis"]))

    def test_the_rules_data_checks_reach_both_models(self) -> None:
        unusual = {**RECORD, "systolic_bp": 250.0}
        warnings = stroke_rule.assess(unusual).warnings
        self.assertTrue(warnings)
        for kind, params in ((stroke_rule.RULE_KIND, stroke_rule.rule_parameters()), (stroke_model.MODEL_KIND, SCORING_PARAMS)):
            with self.subTest(kind=kind):
                self.assertEqual(list(warnings), model_kinds.assess("v", kind, params, unusual)["warnings"])

    def test_every_logistic_regression_coordinate_has_a_label(self) -> None:
        self.assertEqual(set(stroke_model.FEATURES[1:]), set(model_kinds.FEATURE_LABELS))

    def test_an_unknown_kind_cannot_score(self) -> None:
        with self.assertRaises(ModelVerificationError):
            model_kinds.score("neural_network", {}, RECORD)


class StartupRegistrationTests(unittest.TestCase):
    """The versions the app registers with the GMA when it starts."""

    def test_the_seed_rule_is_released_and_the_starting_model_is_a_candidate(self) -> None:
        registrations = model_kinds.startup_registrations()
        self.assertEqual([SEED_VERSION, INITIAL_VERSION], [version for version, _ in registrations])
        seed, initial = (registration for _, registration in registrations)
        self.assertEqual(
            ModelVersionRegistration(kind="points_rule", params=stroke_rule.rule_parameters(), requested_status="released"),
            seed,
        )
        self.assertEqual(
            ModelVersionRegistration(
                kind="logistic_regression", params=stroke_model.initial_params(), requested_status="candidate"
            ),
            initial,
        )
        self.assertEqual(SEED_DIGEST, POINTS_RULE.parameter_digest(seed.params))
        self.assertEqual(INITIAL_DIGEST, LOGISTIC_REGRESSION.parameter_digest(initial.params))
        self.assertEqual("stroke-outcome-logreg-init", INITIAL_VERSION)
        self.assertEqual(stroke_rule.RULE_VERSION, SEED_VERSION)

    def test_each_call_returns_new_objects(self) -> None:
        first = model_kinds.startup_registrations()
        first[0][1].params["version"] = "changed"
        self.assertEqual(SEED_DIGEST, POINTS_RULE.parameter_digest(model_kinds.startup_registrations()[0][1].params))

    @unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
    def test_the_gma_registry_accepts_them_and_a_second_start_changes_nothing(self) -> None:
        from agents.gma.application.model_registry import ModelRegistry
        from agents.gma.infrastructure.postgres_models import InMemoryModelStore

        registry = ExtensionRegistry()
        registry.register(HealthcareStrokeExtension())
        models = ModelRegistry(InMemoryModelStore(), registry)
        first = [models.register(version, registration) for version, registration in model_kinds.startup_registrations()]
        self.assertEqual([True, True], [result.created for result in first])
        self.assertEqual(
            [(SEED_VERSION, "released", SEED_DIGEST), (INITIAL_VERSION, "candidate", INITIAL_DIGEST)],
            [(result.model.version, result.model.status, result.model.digest) for result in first],
        )
        again = [models.register(version, registration) for version, registration in model_kinds.startup_registrations()]
        self.assertEqual([False, False], [result.created for result in again])
        self.assertEqual(SEED_VERSION, models.released().version)
        self.assertEqual(INITIAL_DIGEST, models.load(INITIAL_VERSION, INITIAL_DIGEST).digest)


class ConditionPackTests(unittest.TestCase):
    def test_the_pack_names_its_label_and_purposes(self) -> None:
        self.assertEqual(STROKE_LABEL_FIELD, STROKE_CONDITION.label_field)
        self.assertIn(STROKE_CONDITION.label_field, STROKE_FEATURE_CONTRACT.field_names())
        self.assertEqual(STROKE_APPROVED_PURPOSES, STROKE_CONDITION.approved_purposes)
        # A copy, so a change to the pack cannot change the list other code reads.
        self.assertIsNot(STROKE_APPROVED_PURPOSES, STROKE_CONDITION.approved_purposes)
        self.assertEqual(STROKE_RELEASE_GATES, STROKE_CONDITION.release_gates)


class ExtensionTests(unittest.TestCase):
    def registry(self) -> ExtensionRegistry:
        registry = ExtensionRegistry()
        registry.register(HealthcareStrokeExtension())
        return registry

    def test_the_extension_registers_in_a_fresh_registry(self) -> None:
        registry = self.registry()
        described = registry.describe()
        self.assertEqual(["dagents-healthcare-stroke-demo"], [item["extension_id"] for item in described["extensions"]])
        self.assertEqual(["stroke-triage-features"], [item["contract_id"] for item in described["feature_contracts"]])
        self.assertEqual(["stroke-triage-phi"], described["classifications"])
        self.assertEqual(["suspected_stroke"], [item["condition_id"] for item in described["condition_packs"]])
        self.assertEqual(["logistic_regression", "points_rule"], described["model_kinds"])
        self.assertEqual(["healthcare_site_store"], described["site_sources"])
        self.assertEqual(["evaluate_stroke_rule", "score_stroke_worklist"], described["pipeline_steps"])
        # The two untyped scorers were never called; the model kinds replace them.
        self.assertEqual([], described["model_adapters"])
        self.assertIsInstance(registry.model_kind("points_rule"), PointsRuleKind)
        self.assertIsInstance(registry.model_kind("logistic_regression"), LogisticRegressionKind)
        pack = registry.condition_pack("suspected_stroke")
        self.assertEqual((STROKE_LABEL_FIELD, STROKE_APPROVED_PURPOSES), (pack.label_field, pack.approved_purposes))

    def test_the_extension_loads_from_the_setting_an_agent_reads(self) -> None:
        registry = ExtensionRegistry()
        loaded = load_extensions("app.extension:HealthcareStrokeExtension", registry)
        self.assertEqual(["dagents-healthcare-stroke-demo"], [extension.extension_id for extension in loaded])
        self.assertEqual(["logistic_regression", "points_rule"], registry.list_model_kinds())

    def test_a_second_registration_is_refused_and_install_returns_the_first(self) -> None:
        registry = self.registry()
        with self.assertRaises(ExtensionError):
            registry.register(HealthcareStrokeExtension())
        first = registry.extensions()[0]
        self.assertIs(first, install(registry))

    def test_the_pipeline_steps_still_work(self) -> None:
        registry = self.registry()
        records = load_site_records("ch-245")
        ranked = registry.pipeline_step("score_stroke_worklist")({"records": records[:5]})["worklist"]
        self.assertEqual(5, len(ranked))
        metrics = registry.pipeline_step("evaluate_stroke_rule")({"records": records})["metrics"]
        self.assertEqual(stroke_rule.evaluate_against_labels(records), metrics)


class SiteSourceTests(unittest.TestCase):
    """The ``healthcare_site_store`` site source, which an LMA started on its own reads a hospital's records with."""

    def test_the_extension_contributes_it_by_name(self) -> None:
        sources = HealthcareStrokeExtension().site_sources()
        self.assertEqual({"healthcare_site_store": healthcare_site_store}, sources)

    def test_it_needs_the_hospitals_database(self) -> None:
        """Separate processes cannot share an in-memory store, so a source without a database is refused."""
        with self.assertRaises(ValueError) as caught:
            healthcare_site_store(None, "site_ch_245", "ch-245")
        self.assertIn("database", str(caught.exception))

    def test_it_acts_only_as_the_hospitals_own_role(self) -> None:
        with self.assertRaises(ValueError) as caught:
            healthcare_site_store(object(), "site_uk_134", "ch-245")
        self.assertIn("site_ch_245", str(caught.exception))
        for role in (None, "", "dagents_coordinator"):
            with self.subTest(role=role), self.assertRaises(ValueError):
                healthcare_site_store(object(), role, "ch-245")  # type: ignore[arg-type]

    def test_it_refuses_a_site_that_is_not_a_demo_hospital(self) -> None:
        with self.assertRaises(ValueError):
            healthcare_site_store(object(), "site_zz_999", "zz-999")

    def test_it_reads_through_the_apps_own_store_adapter(self) -> None:
        lease = object()
        built = object()
        adapter = mock.Mock(return_value=built)
        module = mock.Mock(site_store_source=adapter)
        with mock.patch.dict(sys.modules, {"app.services.site_source": module}):
            self.assertIs(built, healthcare_site_store(lease, "site_ch_245", "ch-245"))
        adapter.assert_called_once_with(lease, "site_ch_245", "ch-245")


if __name__ == "__main__":
    unittest.main()
