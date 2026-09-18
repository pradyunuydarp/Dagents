"""End-to-end tests for the governed federated pilot.

These are the tests that matter most for the demo's claim. They check that the
app really does get its guarantees from the framework rather than asserting them
in its own code: that a hospital can refuse, that nothing patient-level crosses
a boundary, and that a candidate which beats its baseline is still rejected when
a safety gate fails.
"""

from __future__ import annotations

import unittest

from agents.common.domain.federation import AggregationMethod

from app.domain.conditions import STROKE_FEATURE_CONTRACT_VERSION
from app.domain.synthetic import DEFAULT_HOSPITALS, HospitalProfile
from app.services.consortium import Consortium
from app.services.hospital import Hospital

from tests.support import dagentsc_available

COHORT = 250


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class PilotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.consortium = Consortium(cohort_size=COHORT)

    def test_every_hospital_is_eligible_and_contributes(self) -> None:
        result = self.consortium.run_round("r-analytics", "analytics")
        self.assertTrue(result["plan"]["quorum_met"])
        self.assertEqual(len(result["plan"]["selected_sites"]), 3)
        self.assertEqual(result["plan"]["excluded_sites"], [])
        self.assertTrue(result["readiness"]["aggregation_permitted"])

    def test_no_patient_level_data_crosses_the_boundary(self) -> None:
        """The central claim of the architecture, checked field by field.

        A site result may carry counts, metrics, a bounded norm, and a pointer
        to local evidence. It may not carry an encounter id, an age band, a
        NIHSS score, or an outcome. If this test ever fails, the demo is
        claiming something it does not do.
        """
        result = self.consortium.run_round("r-egress", "evaluation")
        forbidden = {
            "encounter_id",
            "age_band",
            "nihss_total",
            "confirmed_stroke",
            "blood_glucose",
            "systolic_bp",
            "records",
            "patient_id",
            "mrn",
        }
        for site_result in result["results"]:
            self.assertEqual(forbidden & set(site_result), set())
            for key in site_result.get("metrics", {}):
                self.assertNotIn(key, forbidden)
            pointer = site_result.get("local_evidence_pointer") or ""
            self.assertTrue(pointer.startswith("site-local://"), "evidence must stay addressed to the site")

    def test_a_site_on_a_stale_feature_contract_is_excluded(self) -> None:
        """Matching field names are not a matching contract."""
        consortium = Consortium(cohort_size=COHORT)
        stale = consortium.hospitals["lakeside-community"].registration()
        consortium.controller.register_site(
            stale.model_copy(update={"feature_contract_version": "stroke-triage-features-v1"})
        )
        result = consortium.run_round("r-stale", "analytics")
        self.assertNotIn("lakeside-community", result["plan"]["selected_sites"])
        excluded = {entry["site_id"]: entry["reason"] for entry in result["plan"]["excluded_sites"]}
        self.assertIn("feature contract", excluded["lakeside-community"])

    def test_a_site_below_the_cohort_floor_is_excluded(self) -> None:
        tiny = HospitalProfile(site_id="tiny-clinic", display_name="Tiny Clinic", seed=99)
        consortium = Consortium(profiles=[*DEFAULT_HOSPITALS, tiny], cohort_size=COHORT)
        consortium.hospitals["tiny-clinic"] = Hospital(tiny, cohort_size=5)
        consortium.controller.register_site(consortium.hospitals["tiny-clinic"].registration())
        result = consortium.run_round("r-tiny", "analytics")
        self.assertNotIn("tiny-clinic", result["plan"]["selected_sites"])

    def test_secure_aggregation_threshold_blocks_a_short_consortium(self) -> None:
        """A threshold nobody can meet stops the round rather than shrinking it."""
        consortium = Consortium(profiles=DEFAULT_HOSPITALS[:2], cohort_size=COHORT, secure_aggregation_threshold=3)
        result = consortium.run_round("r-short", "analytics")
        self.assertFalse(result["plan"]["quorum_met"])
        self.assertEqual(result["plan"]["required_participants"], 3)
        self.assertEqual(result["status"], "closed")
        self.assertEqual(result["results"], [])

    def test_the_full_pilot_rejects_a_candidate_that_fails_a_safety_gate(self) -> None:
        """Beating the baseline is not sufficient. That is the point.

        The candidate improves on the baseline AUC and clears the alert budget,
        and is still rejected, because the subgroup gap and the sensitivity
        floor are blocking gates. A framework that released it would be worse
        than no framework.
        """
        report = self.consortium.run_pilot("pilot")
        self.assertIsNotNone(report["candidate"])
        gates = report["release"]["gates"]
        outcomes = {result["gate_id"]: result["outcome"] for result in gates["gate_results"]}
        self.assertEqual(outcomes["discrimination"], "passed")
        self.assertEqual(outcomes["improves_on_current_release"], "passed")
        self.assertEqual(gates["action"], "reject")
        self.assertTrue(gates["blocking_failures"])
        self.assertEqual(gates["rollback_version"], "stroke-rule-seed-v1")

    def test_the_candidate_is_validated_at_every_site_not_at_the_coordinator(self) -> None:
        """Candidate metrics come from cross-site validation, not from training."""
        report = self.consortium.run_pilot("pilot")
        validation = report["rounds"]["candidate_validation"]
        self.assertIsNotNone(validation)
        self.assertEqual(len(validation["readiness"]["accepted_sites"]), 3)
        self.assertIn("auc", report["candidate"]["metrics"])
        weights = validation["readiness"]["site_weights"]
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=9)

    def test_a_training_round_never_reads_the_outcome_label(self) -> None:
        """A training round has no business reading what it should be learning."""
        hospital = self.consortium.hospitals["riverside-general"]
        manifest = self.consortium.manifest("r-labels", "training")
        digest = self.consortium.controller.digest(manifest)
        from agents.common.domain.federation import FederatedJob

        job = FederatedJob(
            round_id="r-labels",
            site_id=hospital.site_id,
            manifest=manifest,
            manifest_digest=digest,
            task="train",
        )
        for record in hospital.data_source.records(job):
            self.assertNotIn("confirmed_stroke", record)

    def test_every_site_writes_an_audit_record_for_each_round(self) -> None:
        """Three guard boundaries per site, each leaving a verifiable trace."""
        self.consortium.run_round("r-audit", "evaluation")
        for site_id, hospital in self.consortium.hospitals.items():
            audit = hospital.audit_trail()
            self.assertTrue(audit["chain_intact"], f"{site_id} audit chain is broken")
            boundaries = {record["boundary"] for record in audit["records"]}
            self.assertIn("before_train", boundaries)
            self.assertIn("before_read", boundaries)
            self.assertIn("before_send", boundaries)

    def test_the_guard_costs_accuracy_and_the_cost_is_measurable(self) -> None:
        """Generalization is not free, and the README says how much it costs.

        A site's raw local AUC and the AUC it reports through the Guard must
        differ, in that direction. If they stopped differing, either the
        classification changed or generalization silently became a no-op again
        — and the second is the bug that made this test necessary.
        """
        from agents.common.domain.federation import FederatedJob

        from app.domain.stroke_rule import evaluate_against_labels

        manifest = self.consortium.manifest("cost", "evaluation")
        digest = self.consortium.controller.digest(manifest)
        for hospital in self.consortium.hospitals.values():
            raw = evaluate_against_labels(hospital.data_source.records_held)["auc"]
            worker = hospital.worker(expected_digest=digest)
            job = FederatedJob(
                round_id="cost",
                site_id=hospital.site_id,
                manifest=manifest,
                manifest_digest=digest,
                task="evaluate",
                feature_fields=worker._approved_fields,  # noqa: SLF001
            )
            guarded = worker.execute(job).metrics["auc"]
            self.assertLess(
                guarded, raw, f"{hospital.site_id}: the Guard did not change what the runner saw"
            )
            self.assertGreater(guarded, raw - 0.15, f"{hospital.site_id}: the cost is implausibly large")

    def test_the_round_digest_changes_when_the_contract_changes(self) -> None:
        """A tampered round contract cannot present itself as the approved one."""
        original = self.consortium.controller.digest(self.consortium.manifest("r-1", "training"))
        altered = self.consortium.manifest("r-1", "training").model_copy(
            update={"training_code_digest": "sha256:tampered"}
        )
        self.assertNotEqual(original, self.consortium.controller.digest(altered))

    def test_the_overview_reports_the_contract_every_site_must_match(self) -> None:
        overview = self.consortium.overview()
        self.assertEqual(overview["feature_contract"]["version"], STROKE_FEATURE_CONTRACT_VERSION)
        self.assertEqual(len(overview["hospitals"]), 3)
        self.assertTrue(overview["release_gates"])

    def test_a_fedavg_round_still_requires_the_stated_minimum(self) -> None:
        """Dropping secure aggregation must not drop the participation floor."""
        consortium = Consortium(cohort_size=COHORT)
        manifest = consortium.manifest("r-fedavg", "analytics").model_copy(
            update={"aggregation": AggregationMethod(kind="fedavg")}
        )
        plan = consortium.controller.plan_round(manifest)
        self.assertEqual(plan.required_participants, manifest.minimum_participants)
        self.assertTrue(plan.quorum_met)


if __name__ == "__main__":
    unittest.main()
