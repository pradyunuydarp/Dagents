"""Tests for the demo's clinical scoring rule and ingestion mapping.

These cover the app's own logic. The framework's behaviour is tested in the
framework's suite; what matters here is that the domain layer is correct and,
above all, that it is honest about what it does not know.
"""

from __future__ import annotations

import unittest

from app.domain import stroke_rule
from app.domain.fhir import map_bundle, map_encounter
from app.domain.synthetic import DEFAULT_HOSPITALS, as_fhir_bundle, generate_cohort

SEVERE = {
    "nihss_total": 22.0,
    "face_droop": True,
    "arm_weakness": True,
    "speech_difficulty": True,
    "last_known_well_minutes": 40.0,
    "age_band": "70-79",
}

MILD = {
    "nihss_total": 1.0,
    "face_droop": False,
    "arm_weakness": False,
    "speech_difficulty": False,
    "last_known_well_minutes": 900.0,
    "age_band": "50-59",
}


class StrokeRuleTests(unittest.TestCase):
    def test_a_severe_recent_case_outranks_a_mild_late_one(self) -> None:
        self.assertGreater(stroke_rule.assess(SEVERE).score, stroke_rule.assess(MILD).score)
        self.assertEqual(stroke_rule.assess(SEVERE).priority, "urgent")
        self.assertEqual(stroke_rule.assess(MILD).priority, "routine")

    def test_every_recommendation_reports_its_basis(self) -> None:
        """A recommendation a clinician cannot interrogate cannot be acted on."""
        assessment = stroke_rule.assess(SEVERE)
        self.assertTrue(assessment.basis)
        self.assertIn("nihss_total", assessment.contributions)
        self.assertIn("fast_findings", assessment.contributions)
        self.assertIn("recency", assessment.contributions)
        self.assertAlmostEqual(sum(assessment.contributions.values()), assessment.score, places=6)

    def test_a_record_with_no_clinical_signal_is_suppressed(self) -> None:
        """Absence must not read as low priority.

        Scoring an empty record zero would place it at the bottom of the
        worklist as though it had been assessed and found low risk. It has not
        been assessed at all, and the difference matters.
        """
        assessment = stroke_rule.assess({"age_band": "60-69"})
        self.assertTrue(assessment.suppressed)
        self.assertEqual(assessment.contributions, {})
        self.assertTrue(any("no clinical signal" in issue for issue in assessment.data_quality_issues))

    def test_missing_inputs_are_reported_not_silently_defaulted(self) -> None:
        partial = {"nihss_total": 18.0, "last_known_well_minutes": 30.0}
        assessment = stroke_rule.assess(partial)
        self.assertFalse(assessment.suppressed)
        self.assertTrue(any("face_droop" in issue for issue in assessment.data_quality_issues))
        self.assertTrue(any("less complete" in warning for warning in assessment.warnings))

    def test_an_out_of_range_nihss_is_clamped_and_flagged(self) -> None:
        assessment = stroke_rule.assess({**SEVERE, "nihss_total": 99.0})
        self.assertTrue(any("0-42" in issue for issue in assessment.data_quality_issues))
        self.assertLessEqual(assessment.score, 1.0)

    def test_hypoglycaemia_is_warned_about_not_used_to_exclude(self) -> None:
        """Ruling a patient out is a clinical decision, not a triage-support one."""
        low_glucose = {**SEVERE, "blood_glucose": 2.4}
        assessment = stroke_rule.assess(low_glucose)
        self.assertEqual(assessment.priority, "urgent", "a mimic warning must not demote the case")
        self.assertTrue(any("hypoglycaemia" in warning for warning in assessment.warnings))

    def test_ranking_keeps_suppressed_cases_in_the_list(self) -> None:
        """Nothing is removed from the queue, including what could not be scored."""
        ranked = stroke_rule.rank_worklist([MILD, {"age_band": "60-69"}, SEVERE])
        self.assertEqual(len(ranked), 3)
        self.assertEqual(ranked[0]["nihss_total"], SEVERE["nihss_total"])
        self.assertTrue(ranked[-1]["assessment"]["suppressed"])

    def test_evaluation_reports_subgroup_gap_when_bands_differ(self) -> None:
        records = generate_cohort(DEFAULT_HOSPITALS[0], 250)
        metrics = stroke_rule.evaluate_against_labels(records)
        self.assertIn("auc", metrics)
        self.assertIn("subgroup_auc_gap", metrics)
        self.assertGreaterEqual(metrics["auc"], 0.0)
        self.assertLessEqual(metrics["auc"], 1.0)

    def test_evaluation_of_unlabelled_records_returns_nothing(self) -> None:
        """No labels means no metrics, not a metric computed from nothing."""
        unlabelled = [{k: v for k, v in SEVERE.items()} for _ in range(10)]
        self.assertEqual(stroke_rule.evaluate_against_labels(unlabelled), {})

    def test_lowering_the_threshold_raises_sensitivity_and_alert_burden(self) -> None:
        """The tradeoff the sweep exists to show must actually hold."""
        records = generate_cohort(DEFAULT_HOSPITALS[1], 300)
        sweep = stroke_rule.sweep_alert_thresholds(records)
        self.assertGreater(len(sweep), 5)
        low, high = sweep[0], sweep[-1]
        self.assertGreater(low["sensitivity"], high["sensitivity"])
        self.assertGreater(low["alerts_per_1000"], high["alerts_per_1000"])
        self.assertLess(low["specificity"], high["specificity"])


class FhirMappingTests(unittest.TestCase):
    def test_glucose_is_converted_from_mg_per_dl(self) -> None:
        """Mixing units silently would put a normal reading in the low band."""
        result = map_encounter(
            {"id": "e1", "age": 74, "minutesSinceLastKnownWell": 60},
            [
                {
                    "code": {"coding": [{"code": "2339-0"}]},
                    "valueQuantity": {"value": 90, "unit": "mg/dL"},
                }
            ],
        )
        self.assertAlmostEqual(result.record["blood_glucose"], 5.0, places=3)

    def test_ages_are_reduced_to_bands_at_ingestion(self) -> None:
        """The cheapest way not to disclose a field is never to derive it."""
        result = map_encounter({"id": "e1", "age": 74}, [])
        self.assertEqual(result.record["age_band"], "70-79")
        self.assertNotIn("age", result.record)

    def test_unknown_codes_are_reported_rather_than_dropped(self) -> None:
        result = map_encounter(
            {"id": "e1", "age": 60},
            [{"code": {"coding": [{"code": "99999-9"}]}, "valueQuantity": {"value": 1, "unit": "x"}}],
        )
        self.assertIn("99999-9", result.unmapped)

    def test_missing_required_contract_fields_are_reported(self) -> None:
        result = map_encounter({"id": "e1", "age": 60}, [])
        self.assertTrue(any("nihss_total" in issue for issue in result.issues))

    def test_a_mismatched_unit_is_flagged_rather_than_assumed(self) -> None:
        result = map_encounter(
            {"id": "e1", "age": 60},
            [
                {
                    "code": {"coding": [{"code": "8480-6"}]},
                    "valueQuantity": {"value": 150, "unit": "kPa"},
                }
            ],
        )
        self.assertTrue(any("kPa" in issue for issue in result.issues))

    def test_a_synthetic_bundle_round_trips_through_the_mapper(self) -> None:
        """The mapping path is exercised against real demo data, not a mock."""
        profile = DEFAULT_HOSPITALS[0]
        records = generate_cohort(profile, 5)
        results = map_bundle(as_fhir_bundle(profile, records))
        self.assertEqual(len(results), 5)
        self.assertTrue(any("age_band" in result.record for result in results))


if __name__ == "__main__":
    unittest.main()
