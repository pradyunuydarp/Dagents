"""Tests for the seed rule, the shared evaluation and the FHIR mapping (contract v3).

These cover the app's own logic. The framework's behaviour is tested in the
framework's suite. Here the questions are whether the rule scores what it says
it scores, reports what it does not know, and whether every model is measured
the same way.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import random
import unittest

from app.domain import stroke_rule
from app.domain.conditions import (
    GUARD_PROBE_DEFAULT_FIELDS,
    STROKE_CLASSIFICATION,
    STROKE_CONDITION,
    STROKE_ENTRY_FIELDS,
    STROKE_FEATURE_CONTRACT,
    STROKE_FIELD_DOMAINS,
    STROKE_LABEL_FIELD,
    STROKE_RELEASE_GATES,
)
from app.domain.dataset import SITES, load_site_records
from app.domain.fhir import as_fhir_bundle, map_bundle, map_encounter

SEVERE = {
    "age_band": "90+",
    "sex": "female",
    "conscious_level": "unconscious",
    "face_weakness": True,
    "arm_weakness": True,
    "leg_weakness": True,
    "speech_difficulty": True,
    "visual_field_loss": True,
    "atrial_fibrillation": True,
    "stroke_subtype": "TACS",
    "hours_since_onset": 3.0,
    "systolic_bp": 170.0,
}

MILD = {
    "age_band": "50-59",
    "sex": "male",
    "conscious_level": "alert",
    "face_weakness": False,
    "arm_weakness": True,
    "leg_weakness": False,
    "speech_difficulty": False,
    "visual_field_loss": False,
    "atrial_fibrillation": False,
    "stroke_subtype": "LACS",
    "hours_since_onset": 20.0,
    "systolic_bp": 150.0,
}


class SeedRuleTests(unittest.TestCase):
    def test_a_severe_case_outranks_a_mild_one(self) -> None:
        severe, mild = stroke_rule.assess(SEVERE), stroke_rule.assess(MILD)
        self.assertGreater(severe.score, mild.score)
        self.assertEqual(severe.priority, "urgent")
        self.assertEqual(mild.priority, "routine")
        self.assertEqual(severe.points, stroke_rule.MAX_POINTS)
        self.assertEqual(mild.points, 2)

    def test_the_score_is_the_fixed_curve_of_the_points(self) -> None:
        for record in (SEVERE, MILD):
            assessment = stroke_rule.assess(record)
            self.assertEqual(sum(assessment.contributions.values()), assessment.points)
            self.assertAlmostEqual(assessment.score, stroke_rule.points_to_score(assessment.points), places=12)
        self.assertAlmostEqual(stroke_rule.points_to_score(stroke_rule.RISK_MIDPOINT_POINTS), 0.5)
        self.assertEqual(stroke_rule.MAX_POINTS, 17)

    def test_every_score_reports_its_basis(self) -> None:
        """A clinician must be able to see why a case scored as it did."""
        assessment = stroke_rule.assess(SEVERE)
        self.assertEqual(
            set(assessment.contributions),
            {"age_band", "conscious_level", "face_weakness", "arm_weakness", "leg_weakness",
             "speech_difficulty", "visual_field_loss", "atrial_fibrillation", "stroke_subtype"},
        )
        self.assertTrue(any("unconscious" in line for line in assessment.basis))
        self.assertTrue(any("TACS" in line for line in assessment.basis))
        self.assertEqual(assessment.as_dict()["model_version"], stroke_rule.RULE_VERSION)

    def test_blood_pressure_and_onset_add_no_points(self) -> None:
        """The rule gives points only to the predictors it names."""
        base = stroke_rule.assess(MILD).points
        for change in ({"systolic_bp": 210.0}, {"hours_since_onset": 1.0}, {"onset_on_waking": True}):
            self.assertEqual(stroke_rule.assess({**MILD, **change}).points, base, change)

    def test_a_record_with_no_examination_finding_is_suppressed(self) -> None:
        """No findings means no score, not a low score."""
        assessment = stroke_rule.assess({"age_band": "80-89", "sex": "male"})
        self.assertTrue(assessment.suppressed)
        self.assertEqual(assessment.contributions, {})
        self.assertIsNone(stroke_rule.rule_score({"age_band": "80-89"}))
        self.assertTrue(any("no examination finding" in issue for issue in assessment.data_quality_issues))

    def test_missing_inputs_are_reported_not_silently_defaulted(self) -> None:
        partial = {key: value for key, value in SEVERE.items() if key not in {"face_weakness", "age_band"}}
        assessment = stroke_rule.assess(partial)
        self.assertFalse(assessment.suppressed)
        self.assertTrue(any("face_weakness" in issue for issue in assessment.data_quality_issues))
        self.assertTrue(any("age_band" in issue for issue in assessment.data_quality_issues))
        self.assertTrue(any("less complete" in warning for warning in assessment.warnings))

    def test_out_of_range_and_unknown_values_are_reported(self) -> None:
        assessment = stroke_rule.assess(
            {**MILD, "systolic_bp": 400.0, "hours_since_onset": 72.0, "conscious_level": "asleep"}
        )
        issues = " ".join(assessment.data_quality_issues)
        self.assertIn("systolic_bp", issues)
        self.assertIn("hours_since_onset", issues)
        self.assertIn("conscious_level", issues)

    def test_very_high_blood_pressure_is_a_warning(self) -> None:
        assessment = stroke_rule.assess({**MILD, "systolic_bp": 230.0})
        self.assertTrue(any("markedly raised" in warning for warning in assessment.warnings))
        self.assertEqual(assessment.priority, "routine", "a warning must not change the score")

    def test_values_shortened_by_the_guard_score_the_same(self) -> None:
        """The evaluation round reads the Guard's payload, which may shorten text values."""
        shortened = {**SEVERE, "age_band": "9*", "conscious_level": "unc*", "stroke_subtype": "TAC*", "sex": "fem*"}
        self.assertEqual(stroke_rule.assess(shortened).points, stroke_rule.assess(SEVERE).points)

    def test_the_alert_rule_matches_the_priority_bands(self) -> None:
        self.assertEqual(stroke_rule.ELEVATED_THRESHOLD, stroke_rule.ALERT_THRESHOLD)
        for points in range(stroke_rule.MAX_POINTS + 1):
            score = stroke_rule.points_to_score(points)
            self.assertEqual(stroke_rule.is_alert(score), stroke_rule.priority_for(score) != "routine", points)
        self.assertFalse(stroke_rule.is_alert(None))

    def test_points_tables_cover_the_contract_options(self) -> None:
        self.assertEqual(tuple(stroke_rule.AGE_BAND_POINTS), STROKE_FIELD_DOMAINS["age_band"].options)
        self.assertEqual(tuple(stroke_rule.CONSCIOUS_POINTS), STROKE_FIELD_DOMAINS["conscious_level"].options)
        self.assertEqual(set(stroke_rule.SUBTYPE_POINTS), set(STROKE_FIELD_DOMAINS["stroke_subtype"].options))

    def test_the_rule_digest_is_the_canonical_json_of_its_parameters(self) -> None:
        canonical = json.dumps(stroke_rule.rule_parameters(), sort_keys=True, separators=(",", ":"))
        self.assertEqual(stroke_rule.rule_digest(), "sha256:" + hashlib.sha256(canonical.encode()).hexdigest())

    def test_ranking_keeps_every_case_and_puts_suppressed_ones_last(self) -> None:
        ranked = stroke_rule.rank_worklist([MILD, {"age_band": "60-69"}, SEVERE])
        self.assertEqual(len(ranked), 3)
        self.assertEqual(ranked[0]["conscious_level"], "unconscious")
        self.assertTrue(ranked[-1]["assessment"]["suppressed"])


class ConditionPackTests(unittest.TestCase):
    """The contract, the classification, the form domains and the gates must agree."""

    def test_every_contract_field_has_a_sensitivity_and_a_domain(self) -> None:
        for name in STROKE_FEATURE_CONTRACT.field_names():
            self.assertIn(name, STROKE_CLASSIFICATION.field_sensitivity, name)
            self.assertIn(name, STROKE_FIELD_DOMAINS, name)
        self.assertEqual(STROKE_CLASSIFICATION.field_sensitivity["model_update"], "high")
        self.assertNotIn("age", STROKE_FEATURE_CONTRACT.field_names(), "the exact age stays at the site")

    def test_entry_fields_have_domains_and_exclude_the_derived_band_and_the_label(self) -> None:
        for name in STROKE_ENTRY_FIELDS:
            self.assertIn(name, STROKE_FIELD_DOMAINS, name)
        self.assertNotIn("age_band", STROKE_ENTRY_FIELDS)
        self.assertNotIn(STROKE_LABEL_FIELD, STROKE_ENTRY_FIELDS)

    def test_the_probe_defaults_span_three_sensitivities(self) -> None:
        levels = {STROKE_CLASSIFICATION.field_sensitivity[name] for name in GUARD_PROBE_DEFAULT_FIELDS}
        self.assertEqual(levels, {"high", "medium", "low"})

    def test_every_gate_reads_a_metric_the_evaluation_produces(self) -> None:
        produced = set(stroke_rule.evaluate_against_labels(load_site_records("uk-134")))
        gate_ids = [gate.gate_id for gate in STROKE_RELEASE_GATES]
        self.assertEqual(len(gate_ids), len(set(gate_ids)))
        for gate in STROKE_RELEASE_GATES:
            self.assertIn(gate.metric, produced, gate.gate_id)
        self.assertEqual(STROKE_CONDITION.release_gates, STROKE_RELEASE_GATES)
        self.assertEqual(STROKE_FEATURE_CONTRACT.version, "stroke-triage-features-v3")


class AucTests(unittest.TestCase):
    def test_known_values(self) -> None:
        self.assertEqual(stroke_rule.auc([(0.9, True), (0.1, False)]), 1.0)
        self.assertEqual(stroke_rule.auc([(0.1, True), (0.9, False)]), 0.0)
        self.assertEqual(stroke_rule.auc([(0.5, True), (0.5, False)]), 0.5)
        self.assertIsNone(stroke_rule.auc([(0.5, True), (0.7, True)]))

    def test_matches_the_pairwise_definition_with_ties(self) -> None:
        rng = random.Random(3)
        pairs = [(rng.choice([0.1, 0.2, 0.3, 0.4]), rng.random() < 0.6) for _ in range(200)]
        positives = [score for score, label in pairs if label]
        negatives = [score for score, label in pairs if not label]
        wins = sum(1.0 if p > n else 0.5 if p == n else 0.0 for p, n in itertools.product(positives, negatives))
        self.assertAlmostEqual(stroke_rule.auc(pairs), wins / (len(positives) * len(negatives)), places=12)


def _labelled(label: bool, sex: str, score_hint: float) -> dict:
    return {"sex": sex, "poor_outcome_6m": label, "hint": score_hint}


class EvaluationTests(unittest.TestCase):
    def test_the_seed_rule_is_measured_at_every_hospital(self) -> None:
        for site in SITES:
            metrics = stroke_rule.evaluate_against_labels(load_site_records(site.site_id))
            self.assertEqual(
                set(metrics), {"auc", "sensitivity", "specificity", "alerts_per_1000", "subgroup_auc_gap"}, site.site_id
            )
            # The seed must rank clearly better than chance to serve as a baseline.
            self.assertGreater(metrics["auc"], 0.70, site.site_id)
            self.assertGreaterEqual(metrics["subgroup_auc_gap"], 0.0)
            self.assertLessEqual(metrics["alerts_per_1000"], 1000.0)

    def test_alerts_use_the_one_alert_rule(self) -> None:
        records = load_site_records("it-002")
        metrics = stroke_rule.evaluate_against_labels(records)
        alerts = sum(stroke_rule.is_alert(stroke_rule.rule_score(record)) for record in records)
        self.assertAlmostEqual(metrics["alerts_per_1000"], 1000.0 * alerts / len(records))

    def test_any_score_function_is_measured_the_same_way(self) -> None:
        records = load_site_records("ch-245")
        constant = stroke_rule.evaluate_against_labels(records, score_fn=lambda record: 0.6)
        self.assertEqual(constant["auc"], 0.5)
        self.assertEqual(constant["alerts_per_1000"], 1000.0)
        self.assertEqual(constant["sensitivity"], 1.0)
        self.assertEqual(constant["specificity"], 0.0)
        unscored = stroke_rule.evaluate_against_labels(records, score_fn=lambda record: None)
        self.assertNotIn("auc", unscored)
        self.assertEqual(unscored["sensitivity"], 0.0, "a record the model cannot score is not an alert")

    def test_a_different_label_field_can_be_named(self) -> None:
        records = [{**SEVERE, "outcome": True}, {**MILD, "outcome": False}]
        metrics = stroke_rule.evaluate_against_labels(records, label_field="outcome")
        self.assertEqual(metrics["auc"], 1.0)

    def test_unlabelled_records_give_no_metrics(self) -> None:
        self.assertEqual(stroke_rule.evaluate_against_labels([dict(SEVERE) for _ in range(10)]), {})

    def test_the_subgroup_gap_needs_ten_positives_and_ten_negatives_in_each_group(self) -> None:
        score = lambda record: record["hint"]  # noqa: E731

        def cohort(female_negatives: int) -> list[dict]:
            rows = [_labelled(True, "male", 0.8) for _ in range(10)]
            rows += [_labelled(False, "male", 0.2) for _ in range(10)]
            rows += [_labelled(True, "female", 0.7) for _ in range(10)]
            rows += [_labelled(False, "female", 0.75 if index % 2 else 0.3) for index in range(female_negatives)]
            return rows

        self.assertNotIn("subgroup_auc_gap", stroke_rule.evaluate_against_labels(cohort(9), score_fn=score))
        metrics = stroke_rule.evaluate_against_labels(cohort(10), score_fn=score)
        self.assertAlmostEqual(metrics["subgroup_auc_gap"], 0.5)
        self.assertEqual(stroke_rule.subgroup_aucs(cohort(10), score), {"female": 0.5, "male": 1.0})

    def test_lowering_the_threshold_raises_sensitivity_and_alert_burden(self) -> None:
        sweep = stroke_rule.sweep_alert_thresholds(load_site_records("uk-134"))
        self.assertGreater(len(sweep), 5)
        for low, high in zip(sweep, sweep[1:]):
            self.assertGreaterEqual(low["sensitivity"], high["sensitivity"])
            self.assertGreaterEqual(low["alerts_per_1000"], high["alerts_per_1000"])
            self.assertLessEqual(low["specificity"], high["specificity"])
        self.assertGreater(sweep[0]["sensitivity"], sweep[-1]["sensitivity"])


class FhirMappingTests(unittest.TestCase):
    def test_dataset_records_round_trip_through_the_mapper(self) -> None:
        """The mapping is checked against real rows, not a mock."""
        records = load_site_records("it-002")[:40]
        results = map_bundle(as_fhir_bundle(records))
        self.assertEqual(len(results), len(records))
        compared = set(STROKE_FEATURE_CONTRACT.field_names()) - {"poor_outcome_6m"} | {"age"}
        for record, result in zip(records, results):
            self.assertEqual(result.unmapped, [])
            expected = {key: value for key, value in record.items() if key in compared}
            self.assertEqual(result.record, expected, record["encounter_id"])

    def test_systolic_pressure_in_kpa_is_converted(self) -> None:
        result = map_encounter(
            {"id": "e1", "age": 74},
            [{"code": {"coding": [{"code": "8480-6"}]}, "valueQuantity": {"value": 20, "unit": "kPa"}}],
        )
        self.assertAlmostEqual(result.record["systolic_bp"], 150.0124, places=3)

    def test_an_unknown_unit_is_reported_and_not_used(self) -> None:
        result = map_encounter(
            {"id": "e1", "age": 60},
            [{"code": {"coding": [{"code": "8480-6"}]}, "valueQuantity": {"value": 3, "unit": "psi"}}],
        )
        self.assertNotIn("systolic_bp", result.record)
        self.assertTrue(any("psi" in issue for issue in result.issues))

    def test_unknown_codes_are_reported_rather_than_dropped(self) -> None:
        result = map_encounter(
            {"id": "e1", "age": 60},
            [{"code": {"coding": [{"code": "9269-2"}]}, "valueQuantity": {"value": 14, "unit": "{score}"}}],
        )
        self.assertIn("9269-2", result.unmapped)

    def test_missing_required_contract_fields_are_reported(self) -> None:
        result = map_encounter({"id": "e1", "age": 60}, [])
        issues = " ".join(result.issues)
        for name in ("conscious_level", "face_weakness", "systolic_bp", "hours_since_onset"):
            self.assertIn(name, issues)

    def test_ages_become_bands_and_bad_values_are_reported(self) -> None:
        result = map_encounter({"id": "e1", "age": 74, "gender": "unknown", "minutesSinceOnset": 90}, [])
        self.assertEqual(result.record["age_band"], "70-79")
        self.assertEqual(result.record["hours_since_onset"], 1.5)
        self.assertNotIn("sex", result.record)
        self.assertTrue(any("gender" in issue for issue in result.issues))
        too_young = map_encounter({"id": "e2", "age": 12}, [])
        self.assertNotIn("age_band", too_young.record)


if __name__ == "__main__":
    unittest.main()
