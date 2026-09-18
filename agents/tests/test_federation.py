"""Tests for the federated round control plane.

These exercise the full round lifecycle through the in-process engine: plan,
dispatch, refuse, collect, check readiness, aggregate, and decide a release.
The deterministic decisions come from the real OCaml planner.
"""

from __future__ import annotations

import unittest

from agents.common.application.federation import (
    FederatedRoundController,
    InProcessFederationEngine,
)
from agents.common.application.federation_worker import (
    LocalFederatedWorker,
    LocalRunOutcome,
)
from agents.common.application.governance_service import GovernanceService
from agents.common.domain.federation import (
    AggregationMethod,
    GateComparison,
    ReleaseGate,
    RoundManifest,
    SiteRegistration,
)
from agents.common.domain.governance import DataClassification
from agents.tests.test_governance import dagentsc_available

CLASSIFICATION = DataClassification(
    classification_id="stroke-triage-v1",
    field_sensitivity={"nihss_total": "high", "age_band": "medium"},
    default_sensitivity="high",
    regulations=["HIPAA"],
    minimum_cohort=3,
)


class StubDataSource:
    """A site's local records, which never leave the site in these tests."""

    def __init__(self, records: list[dict]) -> None:
        self._records = records

    def records(self, job) -> list[dict]:  # noqa: ANN001 - test double
        return list(self._records)


class StubRunner:
    """A local run that reports metrics and a contribution norm."""

    def __init__(self, auc: float, norm: float = 0.8) -> None:
        self._auc = auc
        self._norm = norm

    def run(self, job, records) -> LocalRunOutcome:  # noqa: ANN001 - test double
        return LocalRunOutcome(
            metrics={"auc": self._auc, "alerts_per_1000": 30.0},
            examples=len(records),
            update_norm=self._norm,
        )


def build_manifest(**overrides) -> RoundManifest:
    payload = {
        "round_id": "round-0042",
        "study_id": "stroke-triage",
        "condition_id": "suspected_stroke",
        "phase": "training",
        "model_version": "seed-v3",
        "model_artifact_digest": "sha256:seed",
        "training_code_digest": "sha256:code",
        "feature_contract_version": "stroke-triage-features-v2",
        "privacy_profile": "consortium-profile-v1",
        "aggregation": AggregationMethod(kind="fedavg"),
        "minimum_participants": 2,
        "minimum_cohort_per_site": 3,
        "required_capabilities": ["local_training"],
        "invited_sites": ["hospital-a", "hospital-b", "hospital-c"],
    }
    payload.update(overrides)
    return RoundManifest(**payload)


def build_registration(site_id: str, **overrides) -> SiteRegistration:
    payload = {
        "site_id": site_id,
        "capabilities": ["local_training"],
        "feature_contract_version": "stroke-triage-features-v2",
        "approved_conditions": ["suspected_stroke"],
        "cohort_size": 300,
        "enrolled": True,
    }
    payload.update(overrides)
    return SiteRegistration(**payload)


def build_worker(site_id: str, records: int = 5, auc: float = 0.9, norm: float = 0.8, **overrides):
    """Build a site worker with its own governance service and local data."""
    governance = GovernanceService()
    governance.register_classification(CLASSIFICATION)
    payload = {
        "site_id": site_id,
        "governance": governance,
        "data_source": StubDataSource(
            [{"nihss_total": 10.0 + index, "age_band": "70-79"} for index in range(records)]
        ),
        "runner": StubRunner(auc, norm),
        "classification_id": "stroke-triage-v1",
        "feature_contract_version": "stroke-triage-features-v2",
        "approved_fields": ["nihss_total", "age_band"],
        "approved_purposes": ["suspected_stroke"],
    }
    payload.update(overrides)
    return LocalFederatedWorker(**payload)


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class RoundLifecycleTests(unittest.TestCase):
    """The full path from an enrolled consortium to a release decision."""

    def setUp(self) -> None:
        self.engine = InProcessFederationEngine()
        self.controller = FederatedRoundController(engine=self.engine)
        for site_id in ("hospital-a", "hospital-b", "hospital-c"):
            self.controller.register_site(build_registration(site_id))
        self.manifest = build_manifest()
        digest = self.controller.digest(self.manifest)
        for site_id in ("hospital-a", "hospital-b", "hospital-c"):
            self.engine.register_worker(build_worker(site_id, expected_digest=digest))

    def test_round_plan_selects_every_eligible_site(self) -> None:
        plan = self.controller.plan_round(self.manifest)
        self.assertEqual(plan.selected_sites, ["hospital-a", "hospital-b", "hospital-c"])
        self.assertTrue(plan.quorum_met)
        self.assertTrue(plan.round_digest.startswith("fnv1a:"))

    def test_a_round_without_quorum_is_not_dispatched(self) -> None:
        """Too few eligible sites closes the round rather than offering it.

        Dispatching to one site would let the coordinator resolve that site's
        contribution exactly, which is what a participation threshold exists to
        prevent.
        """
        controller = FederatedRoundController(engine=InProcessFederationEngine())
        controller.register_site(build_registration("hospital-a"))
        record = controller.dispatch_round(build_manifest(round_id="short"))
        self.assertEqual(record.status, "closed")
        self.assertFalse(record.plan.quorum_met)
        self.assertEqual(record.results, [])
        self.assertEqual(record.plan.stop_reason, "quorum_not_met")

    def test_full_round_produces_a_candidate_and_a_release_decision(self) -> None:
        record = self.controller.dispatch_round(self.manifest)
        self.assertEqual(record.status, "collecting")
        self.assertEqual(len(record.results), 3)
        self.assertTrue(all(result.participation == "completed" for result in record.results))

        readiness = self.controller.evaluate_readiness(self.manifest.round_id)
        self.assertTrue(readiness.aggregation_permitted)
        self.assertEqual(len(readiness.accepted_sites), 3)
        self.assertAlmostEqual(sum(readiness.site_weights.values()), 1.0, places=9)

        candidate = self.controller.aggregate(self.manifest.round_id)
        self.assertEqual(candidate.parent_version, "seed-v3")
        self.assertIn("auc", candidate.metrics)
        self.assertAlmostEqual(candidate.metrics["auc"], 0.9, places=6)

        decision = self.controller.evaluate_release(
            self.manifest.round_id,
            [
                ReleaseGate(
                    gate_id="discrimination",
                    metric="auc",
                    comparison=GateComparison(kind="at_least", value=0.85),
                ),
                ReleaseGate(
                    gate_id="alert_burden",
                    metric="alerts_per_1000",
                    comparison=GateComparison(kind="at_most", value=40),
                ),
            ],
            candidate,
        )
        self.assertEqual(decision.action, "release")
        self.assertEqual(decision.blocking_failures, [])
        stored = self.controller.get_round(self.manifest.round_id)
        self.assertEqual(stored.status, "closed")
        self.assertIsNotNone(stored.decision)

    def test_unreported_blocking_metric_rejects_the_candidate(self) -> None:
        """A gate the candidate never measured must not pass by omission."""
        self.controller.dispatch_round(self.manifest)
        candidate = self.controller.aggregate(self.manifest.round_id)
        decision = self.controller.evaluate_release(
            self.manifest.round_id,
            [
                ReleaseGate(
                    gate_id="subgroup_gap",
                    metric="subgroup_auc_gap",
                    comparison=GateComparison(kind="at_most", value=0.05),
                )
            ],
            candidate,
        )
        self.assertEqual(decision.action, "reject")
        self.assertEqual(decision.gate_results[0].outcome, "not_evaluated")

    def test_aggregation_is_refused_without_enough_contributions(self) -> None:
        """Losing contributions blocks aggregation instead of proceeding."""
        record = self.controller.dispatch_round(self.manifest)
        dropped = [
            result.model_copy(update={"participation": "dropped"}) if index else result
            for index, result in enumerate(record.results)
        ]
        for result in dropped:
            self.controller.submit_result(result)
        readiness = self.controller.evaluate_readiness(self.manifest.round_id)
        self.assertFalse(readiness.aggregation_permitted)
        with self.assertRaises(PermissionError):
            self.controller.aggregate(self.manifest.round_id)

    def test_a_selected_site_with_no_worker_still_appears_in_the_evidence(self) -> None:
        """A round must not lose a site by omitting it.

        An eligible site whose worker is unreachable is an infrastructure gap.
        Recording nothing for it would make the round's evidence look identical
        to one where the site was never selected, which is the difference
        between a short round and a silently broken one.
        """
        engine = InProcessFederationEngine()
        controller = FederatedRoundController(engine=engine)
        for site_id in ("hospital-a", "hospital-b", "hospital-c"):
            controller.register_site(build_registration(site_id))
        manifest = build_manifest(round_id="missing-worker")
        digest = controller.digest(manifest)
        # Only two of the three selected sites have a worker attached.
        engine.register_worker(build_worker("hospital-a", expected_digest=digest))
        engine.register_worker(build_worker("hospital-b", expected_digest=digest))

        record = controller.dispatch_round(manifest)
        by_site = {result.site_id: result for result in record.results}
        self.assertEqual(set(by_site), {"hospital-a", "hospital-b", "hospital-c"})
        self.assertEqual(by_site["hospital-c"].participation, "failed")
        self.assertIn("no worker", by_site["hospital-c"].local_evidence_pointer or "")

        readiness = controller.evaluate_readiness(manifest.round_id)
        self.assertNotIn("hospital-c", readiness.accepted_sites)
        self.assertTrue(
            any(rejection.site_id == "hospital-c" for rejection in readiness.rejected_contributions)
        )

    def test_secure_aggregation_threshold_raises_the_participation_floor(self) -> None:
        manifest = build_manifest(
            round_id="secure", aggregation=AggregationMethod(kind="secure_aggregation", threshold=5)
        )
        plan = self.controller.plan_round(manifest)
        self.assertEqual(plan.required_participants, 5)
        self.assertFalse(plan.quorum_met)


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class SiteRefusalTests(unittest.TestCase):
    """A site's right to refuse, exercised through the worker."""

    def _controller_with(self, workers) -> FederatedRoundController:
        engine = InProcessFederationEngine()
        controller = FederatedRoundController(engine=engine)
        for site_id in ("hospital-a", "hospital-b", "hospital-c"):
            controller.register_site(build_registration(site_id))
        for worker in workers:
            engine.register_worker(worker)
        return controller

    def test_a_site_refuses_a_job_whose_digest_it_cannot_verify(self) -> None:
        """A tampered job is refused before any code touches local data."""
        manifest = build_manifest()
        workers = [
            build_worker("hospital-a", expected_digest="fnv1a:something-else"),
            build_worker("hospital-b"),
            build_worker("hospital-c"),
        ]
        controller = self._controller_with(workers)
        record = controller.dispatch_round(manifest)
        by_site = {result.site_id: result for result in record.results}
        self.assertEqual(by_site["hospital-a"].participation, "rejected")
        self.assertEqual(by_site["hospital-b"].participation, "completed")

    def test_a_site_refuses_a_round_on_a_different_feature_contract(self) -> None:
        """Matching field names are not a matching contract."""
        workers = [
            build_worker("hospital-a", feature_contract_version="stroke-triage-features-v1"),
            build_worker("hospital-b"),
            build_worker("hospital-c"),
        ]
        controller = self._controller_with(workers)
        record = controller.dispatch_round(build_manifest())
        by_site = {result.site_id: result for result in record.results}
        self.assertEqual(by_site["hospital-a"].participation, "rejected")

    def test_outbound_contribution_is_bounded_by_the_guard(self) -> None:
        """What leaves a site is what the Guard returned, not what the runner made."""
        workers = [build_worker(site, norm=9.9) for site in ("hospital-a", "hospital-b", "hospital-c")]
        controller = self._controller_with(workers)
        record = controller.dispatch_round(build_manifest())
        for result in record.results:
            self.assertEqual(result.participation, "completed")
            self.assertIsNotNone(result.update_norm)
            self.assertLess(result.update_norm, 9.9)

    def test_a_round_requesting_only_unapproved_fields_is_refused(self) -> None:
        """An empty approved-field intersection must close, never open.

        The intersection being empty is falsy, and falling through to "read
        whatever is in the records" turns the site's approved-field list from a
        guarantee into a suggestion — it widened the read to every local key
        precisely when the site had approved none of what was asked for.
        """
        worker = build_worker("hospital-a")
        manifest = build_manifest()
        digest = "d"
        from agents.common.domain.federation import FederatedJob

        job = FederatedJob(
            round_id=manifest.round_id,
            site_id="hospital-a",
            manifest=manifest,
            manifest_digest=digest,
            task="train",
            feature_fields=["ssn", "hiv_status"],
        )
        acceptance = worker.offer(job)
        self.assertFalse(acceptance.accepted)
        self.assertIn("approved field list", acceptance.reason)

        result = worker.execute(job)
        self.assertEqual(result.participation, "rejected")
        self.assertEqual(result.contributed_examples, 0)

    def test_a_partially_approved_round_is_narrowed_rather_than_refused(self) -> None:
        """Asking for more than a site approved gets less, not nothing."""
        worker = build_worker("hospital-a")
        manifest = build_manifest()
        from agents.common.domain.federation import FederatedJob

        job = FederatedJob(
            round_id=manifest.round_id,
            site_id="hospital-a",
            manifest=manifest,
            manifest_digest="d",
            task="evaluate",
            feature_fields=["nihss_total", "ssn"],
        )
        self.assertTrue(worker.offer(job).accepted)
        self.assertEqual(worker._fields_for(job), ["nihss_total"])  # noqa: SLF001

    def test_a_site_with_too_few_local_records_does_not_contribute(self) -> None:
        """A site below the round's per-site floor is excluded from the aggregate."""
        workers = [
            build_worker("hospital-a", records=1),
            build_worker("hospital-b"),
            build_worker("hospital-c"),
        ]
        controller = self._controller_with(workers)
        manifest = build_manifest()
        controller.dispatch_round(manifest)
        readiness = controller.evaluate_readiness(manifest.round_id)
        self.assertNotIn("hospital-a", readiness.accepted_sites)
        self.assertTrue(
            any(rejection.site_id == "hospital-a" for rejection in readiness.rejected_contributions)
        )


if __name__ == "__main__":
    unittest.main()
