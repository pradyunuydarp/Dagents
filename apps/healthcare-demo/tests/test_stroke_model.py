"""Tests for the federated candidate model.

Training here always runs on one site's records at a time, the way a site runs
it inside a round. The tests check that training is deterministic and lowers
the loss, that a delta and its update are exact inverses, that digests are
stable, and that the encoding reads the values the Guard produces.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import unittest

from app.domain import stroke_model, stroke_rule
from app.domain.conditions import STROKE_FIELD_DOMAINS
from app.domain.dataset import SITES, load_site_records

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


def _features(record: dict) -> dict[str, float]:
    return dict(zip(stroke_model.FEATURES, stroke_model.encode(record)))


class EncodingTests(unittest.TestCase):
    def test_the_encoding_uses_the_fixed_constants(self) -> None:
        features = _features(RECORD)
        self.assertEqual(len(stroke_model.encode(RECORD)), len(stroke_model.FEATURES))
        self.assertEqual(features["intercept"], 1.0)
        self.assertEqual(features["age_band"], 0.5)
        self.assertEqual(features["sex_female"], 0.5)
        self.assertEqual(features["conscious_level"], 1.0)
        self.assertEqual(features["face_weakness"], 0.5)
        self.assertEqual(features["arm_weakness"], -0.5)
        self.assertEqual(features["hours_since_onset"], (12.0 - 24.0) / 24.0)
        self.assertEqual(features["systolic_bp"], (190.0 - 160.0) / 30.0)
        self.assertEqual(features["subtype_tacs"], 1.0)

    def test_a_missing_value_adds_nothing(self) -> None:
        features = _features({})
        self.assertEqual(features["intercept"], 1.0)
        self.assertTrue(all(value == 0.0 for name, value in features.items() if name != "intercept"))

    def test_every_contract_option_has_its_own_encoding(self) -> None:
        """The model's categories must stay in step with the contract's options."""
        missing = tuple(stroke_model.encode({}))
        for field in ("age_band", "sex", "conscious_level", "stroke_subtype"):
            encodings = {tuple(stroke_model.encode({field: option})) for option in STROKE_FIELD_DOMAINS[field].options}
            self.assertEqual(len(encodings), len(STROKE_FIELD_DOMAINS[field].options), field)
            if field != "conscious_level":
                self.assertNotIn(missing, encodings, field)
        # By design, a missing conscious level encodes as alert.
        self.assertEqual(tuple(stroke_model.encode({"conscious_level": "alert"})), missing)
        self.assertLess(
            _features({"conscious_level": "drowsy"})["conscious_level"],
            _features({"conscious_level": "unconscious"})["conscious_level"],
        )

    def test_values_shortened_by_the_guard_encode_as_their_category(self) -> None:
        shortened = {**RECORD, "age_band": "70-*", "sex": "fem*", "conscious_level": "dr*", "stroke_subtype": "T*"}
        self.assertEqual(stroke_model.encode(shortened), stroke_model.encode(RECORD))

    def test_an_ambiguous_shortened_value_counts_as_missing(self) -> None:
        # "P*" could be PACS or POCS.
        features = _features({"stroke_subtype": "P*"})
        self.assertEqual([features[name] for name in stroke_model.FEATURES if name.startswith("subtype_")], [0.0] * 5)

    def test_numbers_are_clamped_to_the_contract_range(self) -> None:
        self.assertEqual(_features({"systolic_bp": 900.0})["systolic_bp"], (300.0 - 160.0) / 30.0)
        self.assertEqual(_features({"hours_since_onset": -5})["hours_since_onset"], -1.0)
        self.assertEqual(_features({"systolic_bp": True})["systolic_bp"], 0.0)


class TrainingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.site_records = load_site_records("it-002")

    def test_initial_params_score_everyone_one_half(self) -> None:
        params = stroke_model.initial_params()
        self.assertEqual(set(params), set(stroke_model.FEATURES))
        self.assertEqual(stroke_model.score(params, RECORD), 0.5)

    def test_local_training_is_deterministic_and_lowers_the_loss(self) -> None:
        start = stroke_model.initial_params()
        first = stroke_model.train_local(start, self.site_records, epochs=20)
        second = stroke_model.train_local(start, self.site_records, epochs=20)
        self.assertEqual(first, second)
        self.assertEqual(stroke_model.parameter_digest(first), stroke_model.parameter_digest(second))
        before = stroke_model.log_loss(start, self.site_records)
        after = stroke_model.log_loss(first, self.site_records)
        self.assertAlmostEqual(before, math.log(2), places=9)
        self.assertLess(after, before - 0.05)

    def test_training_does_not_change_its_inputs(self) -> None:
        start = stroke_model.initial_params()
        records = [dict(record) for record in self.site_records[:50]]
        snapshot = [dict(record) for record in records]
        stroke_model.train_local(start, records, epochs=3)
        self.assertEqual(start, stroke_model.initial_params())
        self.assertEqual(records, snapshot)

    def test_unlabelled_records_are_skipped(self) -> None:
        start = stroke_model.initial_params()
        unlabelled = [
            {key: value for key, value in record.items() if key != "poor_outcome_6m"} for record in self.site_records
        ]
        self.assertEqual(stroke_model.train_local(start, unlabelled), start)
        self.assertIsNone(stroke_model.log_loss(start, unlabelled))
        mixed = self.site_records[:30] + unlabelled[30:]
        self.assertEqual(
            stroke_model.train_local(start, mixed, epochs=5),
            stroke_model.train_local(start, self.site_records[:30], epochs=5),
        )

    def test_invalid_settings_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            stroke_model.train_local(stroke_model.initial_params(), self.site_records, learning_rate=0)
        with self.assertRaises(ValueError):
            stroke_model.train_local({"unknown": 1.0}, self.site_records)

    def test_the_score_is_a_probability(self) -> None:
        params = stroke_model.train_local(stroke_model.initial_params(), self.site_records, epochs=10)
        for record in self.site_records:
            self.assertTrue(0.0 < stroke_model.score(params, record) < 1.0)


class UpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.global_params = stroke_model.initial_params()
        self.local = stroke_model.train_local(self.global_params, load_site_records("ch-245"), epochs=10)

    def test_delta_and_apply_update_round_trip(self) -> None:
        change = stroke_model.delta(self.local, self.global_params)
        restored = stroke_model.apply_update(self.global_params, [(1.0, change)])
        for name in stroke_model.FEATURES:
            self.assertAlmostEqual(restored[name], self.local[name], places=9)

    def test_weighted_deltas_average(self) -> None:
        other = stroke_model.train_local(self.global_params, load_site_records("it-002"), epochs=10)
        updated = stroke_model.apply_update(
            self.global_params,
            [
                (0.25, stroke_model.delta(self.local, self.global_params)),
                (0.75, stroke_model.delta(other, self.global_params)),
            ],
        )
        for name in stroke_model.FEATURES:
            self.assertAlmostEqual(updated[name], 0.25 * self.local[name] + 0.75 * other[name], places=9)

    def test_unknown_coordinates_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            stroke_model.apply_update(self.global_params, [(1.0, {"bias": 1.0})])

    def test_l2_norm(self) -> None:
        self.assertEqual(stroke_model.l2_norm({"a": 3.0, "b": 4.0}), 5.0)


class DigestTests(unittest.TestCase):
    def test_parameter_digest_is_sha256_of_canonical_json(self) -> None:
        params = stroke_model.initial_params()
        canonical = json.dumps(params, sort_keys=True, separators=(",", ":"))
        expected = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
        self.assertEqual(stroke_model.parameter_digest(params), expected)

    def test_parameter_digest_ignores_key_order_and_float_noise(self) -> None:
        params = {name: 0.1 * index for index, name in enumerate(stroke_model.FEATURES)}
        reordered = dict(reversed(list(params.items())))
        noisy = {name: value + 1e-13 for name, value in params.items()}
        digest = stroke_model.parameter_digest(params)
        self.assertEqual(stroke_model.parameter_digest(reordered), digest)
        self.assertEqual(stroke_model.parameter_digest(noisy), digest)
        self.assertNotEqual(stroke_model.parameter_digest({**params, "intercept": 0.5}), digest)

    def test_training_code_digest_is_the_digest_of_the_module_source(self) -> None:
        source = inspect.getsourcefile(stroke_model)
        assert source is not None
        with open(source, "rb") as handle:
            expected = "sha256:" + hashlib.sha256(handle.read()).hexdigest()
        self.assertEqual(stroke_model.training_code_digest(), expected)


class SimulatedRoundsTests(unittest.TestCase):
    """A small simulation of the pilot: each site trains on its own records only."""

    def test_clipped_federated_rounds_lower_the_loss_at_every_round(self) -> None:
        records = {site.site_id: load_site_records(site.site_id) for site in SITES}
        total = sum(len(rows) for rows in records.values())
        params = stroke_model.initial_params()
        losses = []
        for _ in range(3):
            contributions = []
            for rows in records.values():
                change = stroke_model.delta(stroke_model.train_local(params, rows), params)
                norm = stroke_model.l2_norm(change)
                scale = min(1.0, 1.0 / norm) if norm else 1.0
                contributions.append((len(rows) / total, {name: value * scale for name, value in change.items()}))
            params = stroke_model.apply_update(params, contributions)
            losses.append(sum(stroke_model.log_loss(params, rows) * len(rows) for rows in records.values()) / total)
        self.assertEqual(losses, sorted(losses, reverse=True))
        for site_id, rows in records.items():
            metrics = stroke_rule.evaluate_against_labels(rows, lambda record: stroke_model.score(params, record))
            self.assertGreater(metrics["auc"], 0.70, site_id)


if __name__ == "__main__":
    unittest.main()
