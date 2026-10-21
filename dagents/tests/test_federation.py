"""Tests for the federated round control plane.

These exercise the full round lifecycle through the in-process engine: plan,
dispatch, refuse, collect, check readiness, aggregate, and decide a release.
The deterministic decisions come from the real OCaml planner.
"""

from __future__ import annotations

import math
import os
import stat
import tempfile
import threading
import time
import typing
import unittest
from unittest.mock import patch

from dagents.common.application.federation import (
    ENGINE_FAILED_REASON,
    FederatedRoundController,
    InMemoryRoundRepository,
    InProcessFederationEngine,
    weighted_metrics,
)
from dagents.common.application.federation_worker import (
    LocalFederatedWorker,
    LocalRunOutcome,
)
from dagents.common.application.governance_service import GovernanceService
from dagents.common.domain.errors import AggregationRefused, InvalidRoundRecord, NotSelected, RoundConflict
from dagents.common.domain.federation import (
    PHASE_TASKS,
    AggregationMethod,
    AggregationReadiness,
    CandidateModel,
    DispatchCall,
    FederatedJob,
    GateComparison,
    ReleaseGate,
    RoundManifest,
    RoundPhase,
    RoundRecord,
    SiteRegistration,
    SiteResult,
)
from dagents.common.domain.governance import DataClassification
from dagents.common.infrastructure.dagents_runner import PlannerUnavailableError
from dagents.tests.test_governance import dagentsc_available

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


class ControllerFactory:
    """How the controller tests below build each controller.

    Each call gives a controller with an enrolment store and a round store of
    its own. Here they are the memory stores; ``test_round_store.py``
    subclasses the tests with a factory that gives Postgres stores, so the same
    assertions run on both.
    """

    def new_controller(self, engine: typing.Any, *, slow_reads: bool = False) -> FederatedRoundController:
        """A controller on ``engine``. ``slow_reads`` gives a round store whose reads take 50 ms."""
        return FederatedRoundController(engine=engine, rounds=SlowReadRepository() if slow_reads else None)


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class RoundLifecycleTests(ControllerFactory, unittest.TestCase):
    """The full path from an enrolled consortium to a release decision."""

    def setUp(self) -> None:
        self.engine = InProcessFederationEngine()
        self.controller = self.new_controller(self.engine)
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
        controller = self.new_controller(InProcessFederationEngine())
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

    def test_each_gate_result_carries_the_threshold_the_planner_used(self) -> None:
        """The planner states each threshold, so no caller recomputes baseline + margin."""
        self.controller.dispatch_round(self.manifest)
        candidate = self.controller.aggregate(self.manifest.round_id)
        gates = [
            ReleaseGate(gate_id="discrimination", metric="auc", comparison=GateComparison(kind="at_least", value=0.85)),
            ReleaseGate(
                gate_id="improves", metric="auc", comparison=GateComparison(kind="improves_on_baseline", margin=0.02)
            ),
            ReleaseGate(
                gate_id="alert_burden", metric="alerts_per_1000", comparison=GateComparison(kind="at_most", value=40)
            ),
        ]
        # A round takes one saved decision, so the decision without a baseline
        # is asked for first and not saved.
        without_baseline = self.controller.decide_release(self.manifest.round_id, gates, candidate)
        decision = self.controller.evaluate_release(
            self.manifest.round_id, gates, candidate, baseline_metrics={"auc": 0.8}
        )
        thresholds = {result.gate_id: result.threshold for result in decision.gate_results}
        self.assertAlmostEqual(thresholds["discrimination"], 0.85)
        self.assertAlmostEqual(thresholds["improves"], 0.82)
        self.assertAlmostEqual(thresholds["alert_burden"], 40.0)
        improves = next(result for result in without_baseline.gate_results if result.gate_id == "improves")
        self.assertIsNone(improves.threshold)
        self.assertEqual("not_evaluated", improves.outcome)

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
        controller = self.new_controller(engine)
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
class SiteRefusalTests(ControllerFactory, unittest.TestCase):
    """A site's right to refuse, exercised through the worker."""

    def _controller_with(self, workers) -> FederatedRoundController:
        engine = InProcessFederationEngine()
        controller = self.new_controller(engine)
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
        from dagents.common.domain.federation import FederatedJob

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
        from dagents.common.domain.federation import FederatedJob

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


class PlannerOutputTests(unittest.TestCase):
    """A planner that is missing or answers with the wrong shape is reported as unavailable.

    These tests need no real planner: they check what the controller does when
    the planner's output cannot be used, which a real planner never produces.
    """

    def setUp(self) -> None:
        self.controller = FederatedRoundController(engine=InProcessFederationEngine())
        for site_id in ("hospital-a", "hospital-b", "hospital-c"):
            self.controller.register_site(build_registration(site_id))
        self.manifest = build_manifest()
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)

    def _stub(self, output: str) -> str:
        path = os.path.join(self._dir.name, "dagentsc")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(f"#!/bin/sh\ncat >/dev/null\nprintf '%s' '{output}'\n")
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        return path

    def test_a_missing_planner_raises_planner_unavailable(self) -> None:
        with patch.dict(os.environ, {"DAGENTSC_BIN": os.path.join(self._dir.name, "missing")}):
            with self.assertRaises(PlannerUnavailableError):
                self.controller.digest(self.manifest)
            with self.assertRaises(PlannerUnavailableError):
                self.controller.plan_round(self.manifest)

    def test_output_with_the_wrong_shape_raises_planner_unavailable(self) -> None:
        with patch.dict(os.environ, {"DAGENTSC_BIN": self._stub('{"decision":"permit"}')}):
            with self.assertRaises(PlannerUnavailableError):
                self.controller.digest(self.manifest)
            with self.assertRaises(PlannerUnavailableError):
                self.controller.plan_round(self.manifest)
            with self.assertRaises(PlannerUnavailableError):
                self.controller.dispatch_round(self.manifest)

    def test_readiness_and_release_with_the_wrong_shape_raise_planner_unavailable(self) -> None:
        from dagents.common.domain.federation import CandidateModel, RoundPlan, RoundRecord

        record = RoundRecord(
            manifest=self.manifest,
            plan=RoundPlan(
                round_id=self.manifest.round_id,
                study_id=self.manifest.study_id,
                phase=self.manifest.phase,
                quorum_met=False,
                required_participants=2,
                aggregation=self.manifest.aggregation,
                round_digest="d",
            ),
            created_at=0,
            updated_at=0,
        )
        self.controller._rounds.save(record)  # noqa: SLF001 - a round as dispatch would leave it
        with patch.dict(os.environ, {"DAGENTSC_BIN": self._stub('{"decision":"permit"}')}):
            with self.assertRaises(PlannerUnavailableError):
                self.controller.evaluate_readiness(self.manifest.round_id)
            with self.assertRaises(PlannerUnavailableError):
                self.controller.evaluate_release(
                    self.manifest.round_id,
                    [],
                    CandidateModel(
                        round_id=self.manifest.round_id,
                        candidate_version="v+1",
                        parent_version="v",
                        aggregation_method="fedavg",
                        produced_at=0,
                    ),
                )


SITES = ("hospital-a", "hospital-b", "hospital-c")


class ScriptedEngine:
    """The in-process engine with hooks a test can set.

    - ``on_submit(manifest, plan)`` runs before the jobs are offered, so a test
      can look at what the controller stored at that moment.
    - ``extra_results`` are added to what the engine collected.
    - ``dispatch_rows`` are returned by ``dispatch_calls``, as the GMA's HTTP
      engine returns its calls to the LMAs.
    - ``fail_with`` makes ``submit_round`` raise after ``on_submit``.
    - ``discarded`` records every ``discard`` call.
    """

    name = "scripted"

    def __init__(self) -> None:
        self.inner = InProcessFederationEngine()
        self.on_submit = None
        self.extra_results: list[SiteResult] = []
        self.dispatch_rows: list[DispatchCall] = []
        self.fail_with: Exception | None = None
        self.discarded: list[str] = []

    def register_worker(self, worker) -> None:  # noqa: ANN001 - test double
        self.inner.register_worker(worker)

    def submit_round(self, manifest, plan):  # noqa: ANN001, ANN201 - test double
        if self.on_submit is not None:
            self.on_submit(manifest, plan)
        if self.fail_with is not None:
            raise self.fail_with
        return self.inner.submit_round(manifest, plan)

    def collect_results(self, round_id: str) -> list[SiteResult]:
        return self.inner.collect_results(round_id) + list(self.extra_results)

    def aggregate(self, manifest, readiness, results):  # noqa: ANN001, ANN201 - test double
        return self.inner.aggregate(manifest, readiness, results)

    def dispatch_calls(self, round_id: str) -> list[DispatchCall]:
        return list(self.dispatch_rows)

    def discard(self, round_id: str) -> None:
        self.discarded.append(round_id)


class SlowReadRepository(InMemoryRoundRepository):
    """A round repository whose reads take a while, like a store across a network.

    A read-modify-write that is not atomic loses updates here: every thread
    reads the same record, then each saves its own copy over the others.
    """

    def get(self, round_id: str) -> RoundRecord | None:
        record = super().get(round_id)
        time.sleep(0.05)
        return record


def completed_result(site_id: str, digest: str, round_id: str = "round-0042", examples: int = 5) -> SiteResult:
    return SiteResult(
        round_id=round_id,
        site_id=site_id,
        job_digest=digest,
        participation="completed",
        code_verified=True,
        privacy_checks_passed=True,
        contributed_examples=examples,
        update_norm=0.5,
        metrics={"auc": 0.9},
    )


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class RoundStateTests(ControllerFactory, unittest.TestCase):
    """What the controller stores while a round moves from dispatch to a candidate."""

    def setUp(self) -> None:
        self.engine = ScriptedEngine()
        self.controller = self.new_controller(self.engine)
        for site_id in SITES:
            self.controller.register_site(build_registration(site_id))
        self.manifest = build_manifest()
        self.digest = self.controller.digest(self.manifest)
        for site_id in SITES:
            self.engine.register_worker(build_worker(site_id, expected_digest=self.digest))

    def test_a_reused_round_id_is_refused_and_the_stored_round_is_kept(self) -> None:
        """A second dispatch under one id would erase the first round's evidence and decision."""
        self.controller.dispatch_round(self.manifest)
        candidate = self.controller.aggregate(self.manifest.round_id)
        before = self.controller.get_round(self.manifest.round_id)
        with self.assertRaises(RoundConflict) as raised:
            self.controller.dispatch_round(self.manifest.model_copy(update={"phase": "evaluation"}))
        self.assertIn(self.manifest.round_id, str(raised.exception))
        after = self.controller.get_round(self.manifest.round_id)
        self.assertEqual(before, after)
        self.assertEqual("aggregated", after.status)
        self.assertEqual(candidate, after.candidate)

    def test_a_reused_round_id_is_refused_for_a_round_without_quorum(self) -> None:
        manifest = build_manifest(round_id="short", invited_sites=["hospital-a"])
        self.assertEqual("closed", self.controller.dispatch_round(manifest).status)
        with self.assertRaises(RoundConflict):
            self.controller.dispatch_round(manifest)

    def test_the_round_is_stored_as_dispatched_with_its_plan_before_the_engine_runs(self) -> None:
        """The update store checks uploads against the stored plan while the jobs run."""
        seen: list[RoundRecord | None] = []
        self.engine.on_submit = lambda manifest, plan: seen.append(self.controller.get_round(manifest.round_id))
        record = self.controller.dispatch_round(self.manifest)
        [during] = seen
        self.assertIsNotNone(during)
        self.assertEqual("dispatched", during.status)
        self.assertEqual(record.plan, during.plan)
        self.assertEqual([], during.results)
        self.assertEqual("collecting", record.status)

    def test_acceptances_and_results_are_stored_sorted_by_site(self) -> None:
        engine = ScriptedEngine()
        controller = self.new_controller(engine)
        for site_id in reversed(SITES):
            controller.register_site(build_registration(site_id))
            engine.register_worker(build_worker(site_id, expected_digest=self.digest))
        record = controller.dispatch_round(self.manifest)
        self.assertEqual(list(SITES), [acceptance.site_id for acceptance in record.acceptances])
        self.assertTrue(all(acceptance.accepted for acceptance in record.acceptances))
        self.assertEqual(list(SITES), [result.site_id for result in record.results])
        self.assertEqual(record, controller.get_round(self.manifest.round_id))

    def test_a_round_without_quorum_has_no_acceptances(self) -> None:
        record = self.controller.dispatch_round(build_manifest(round_id="short", invited_sites=["hospital-a"]))
        self.assertEqual([], record.acceptances)
        self.assertEqual([], record.dispatch)

    def test_the_engine_dispatch_rows_are_stored_with_the_round(self) -> None:
        rows = [
            DispatchCall(site_id="hospital-a", step="offer", status_code=200, ms=4.0, outcome="accepted"),
            DispatchCall(site_id="hospital-a", step="execute", status_code=200, ms=9.5, outcome="completed"),
        ]
        self.engine.dispatch_rows = rows
        record = self.controller.dispatch_round(self.manifest)
        self.assertEqual(rows, record.dispatch)

    def test_a_result_from_a_site_the_plan_did_not_select_is_refused(self) -> None:
        """A site that was never enrolled or invited must not enter the round's evidence."""
        self.controller.dispatch_round(self.manifest)
        before = self.controller.get_round(self.manifest.round_id)
        with self.assertRaises(NotSelected) as raised:
            self.controller.submit_result(completed_result("intruder", self.digest))
        self.assertIsInstance(raised.exception, PermissionError)
        self.assertIn("intruder", str(raised.exception))
        self.assertEqual(before, self.controller.get_round(self.manifest.round_id))

    def test_a_result_for_an_unknown_round_raises_key_error(self) -> None:
        with self.assertRaises(KeyError):
            self.controller.submit_result(completed_result("hospital-a", self.digest, round_id="missing"))

    def test_a_late_result_cannot_reopen_an_aggregated_or_closed_round(self) -> None:
        self.controller.dispatch_round(self.manifest)
        self.controller.aggregate(self.manifest.round_id)
        late = completed_result("hospital-a", self.digest).model_copy(update={"contributed_examples": 99})
        with self.assertRaises(RoundConflict):
            self.controller.submit_result(late)
        candidate = self.controller.get_round(self.manifest.round_id).candidate
        self.controller.evaluate_release(self.manifest.round_id, [], candidate)
        closed = self.controller.get_round(self.manifest.round_id)
        self.assertEqual("closed", closed.status)
        with self.assertRaises(RoundConflict):
            self.controller.submit_result(late)
        self.assertEqual(closed, self.controller.get_round(self.manifest.round_id))

    def test_concurrent_results_keep_every_result(self) -> None:
        """Results posted at the same time must not overwrite each other."""
        controller = self.new_controller(InProcessFederationEngine(), slow_reads=True)
        for site_id in SITES:
            controller.register_site(build_registration(site_id))
        record = controller.dispatch_round(self.manifest)
        self.assertEqual(3, len(record.results))  # failed: the engine has no workers
        barrier = threading.Barrier(len(SITES))
        errors: list[BaseException] = []

        def post(site_id: str) -> None:
            try:
                barrier.wait()
                controller.submit_result(completed_result(site_id, self.digest))
            except BaseException as error:  # noqa: BLE001 - reported below
                errors.append(error)

        threads = [threading.Thread(target=post, args=(site_id,)) for site_id in SITES]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual([], errors)
        stored = controller.get_round(self.manifest.round_id)
        self.assertEqual({"completed"}, {result.participation for result in stored.results})
        self.assertEqual(list(SITES), [result.site_id for result in stored.results])

    def test_readiness_rejects_a_result_from_a_site_outside_the_selection(self) -> None:
        """Even a result the engine hands back counts only for a site the plan selected."""
        self.engine.extra_results = [completed_result("intruder", self.digest, examples=500)]
        record = self.controller.dispatch_round(self.manifest)
        self.assertIn("intruder", [result.site_id for result in record.results])
        readiness = self.controller.evaluate_readiness(self.manifest.round_id)
        self.assertEqual(list(SITES), readiness.accepted_sites)
        self.assertNotIn("intruder", readiness.site_weights)
        rejected = {item.site_id: item.reason for item in readiness.rejected_contributions}
        self.assertEqual("site was not selected for this round", rejected["intruder"])
        self.assertEqual(15, readiness.accepted_examples)

    def test_a_readiness_check_without_save_writes_nothing(self) -> None:
        self.controller.dispatch_round(self.manifest)
        before = self.controller.get_round(self.manifest.round_id)
        readiness = self.controller.evaluate_readiness(self.manifest.round_id, save=False)
        self.assertTrue(readiness.aggregation_permitted)
        self.assertEqual(before, self.controller.get_round(self.manifest.round_id))
        self.assertEqual(readiness, self.controller.evaluate_readiness(self.manifest.round_id))
        self.assertEqual(readiness, self.controller.get_round(self.manifest.round_id).readiness)

    def test_aggregate_stores_the_candidate_and_the_readiness_on_the_round(self) -> None:
        self.controller.dispatch_round(self.manifest)
        candidate = self.controller.aggregate(self.manifest.round_id)
        stored = self.controller.get_round(self.manifest.round_id)
        self.assertEqual("aggregated", stored.status)
        self.assertEqual(candidate, stored.candidate)
        self.assertTrue(stored.readiness.aggregation_permitted)

    def test_a_refused_aggregation_is_an_aggregation_refused_error(self) -> None:
        record = self.controller.dispatch_round(self.manifest)
        for result in record.results[1:]:
            self.controller.submit_result(result.model_copy(update={"participation": "dropped"}))
        with self.assertRaises(AggregationRefused) as raised:
            self.controller.aggregate(self.manifest.round_id)
        self.assertIsInstance(raised.exception, PermissionError)
        self.assertIn("aggregation is not permitted for round-0042", str(raised.exception))
        stored = self.controller.get_round(self.manifest.round_id)
        self.assertIsNone(stored.candidate)
        self.assertEqual("collecting", stored.status)

    def test_discard_deletes_the_engine_updates_and_records_the_reason(self) -> None:
        self.controller.dispatch_round(self.manifest)
        record = self.controller.discard(self.manifest.round_id, "the study stopped")
        self.assertEqual([self.manifest.round_id], self.engine.discarded)
        self.assertEqual("the study stopped", record.abandoned_reason)
        self.assertEqual("closed", record.status)
        self.assertEqual(record, self.controller.get_round(self.manifest.round_id))
        with self.assertRaises(RoundConflict):
            self.controller.submit_result(completed_result("hospital-a", self.digest))
        with self.assertRaises(RoundConflict) as raised:
            self.controller.aggregate(self.manifest.round_id)
        self.assertIn("discarded", str(raised.exception))

    def test_discard_keeps_the_first_reason(self) -> None:
        self.controller.dispatch_round(self.manifest)
        self.controller.discard(self.manifest.round_id, "first")
        record = self.controller.discard(self.manifest.round_id, "second")
        self.assertEqual("first", record.abandoned_reason)
        self.assertEqual([self.manifest.round_id] * 2, self.engine.discarded)

    def test_discard_of_an_aggregated_round_keeps_its_status_and_candidate(self) -> None:
        self.controller.dispatch_round(self.manifest)
        candidate = self.controller.aggregate(self.manifest.round_id)
        record = self.controller.discard(self.manifest.round_id, "the pilot finished")
        self.assertEqual("aggregated", record.status)
        self.assertEqual(candidate, record.candidate)
        self.assertEqual("the pilot finished", record.abandoned_reason)

    def test_discard_of_an_unknown_round_raises_key_error(self) -> None:
        with self.assertRaises(KeyError):
            self.controller.discard("missing", "no such round")
        self.assertEqual([], self.engine.discarded)

    def test_an_engine_without_discard_can_still_discard_a_round(self) -> None:
        controller = self.new_controller(InProcessFederationEngine())
        for site_id in SITES:
            controller.register_site(build_registration(site_id))
        controller.dispatch_round(self.manifest)
        self.assertEqual("stopped", controller.discard(self.manifest.round_id, "stopped").abandoned_reason)

    def test_an_engine_that_fails_closes_the_round_and_deletes_its_updates(self) -> None:
        """A round the engine could not finish must not keep taking uploads or results."""
        self.engine.fail_with = RuntimeError("the runtime stopped")
        with self.assertRaises(RuntimeError):
            self.controller.dispatch_round(self.manifest)
        stored = self.controller.get_round(self.manifest.round_id)
        self.assertEqual("closed", stored.status)
        self.assertEqual("the engine failed while it dispatched the round", stored.abandoned_reason)
        self.assertEqual([self.manifest.round_id], self.engine.discarded)
        with self.assertRaises(RoundConflict):
            self.controller.dispatch_round(self.manifest)

    def test_a_result_no_store_can_keep_closes_the_round_and_the_engine_deletes_its_updates(self) -> None:
        """A result that holds NaN or infinity, built without validation, ends the dispatch like an engine failure.

        Neither round store can keep it, so both refuse the write with
        ``InvalidRoundRecord``. The round is closed and the engine's updates
        are deleted, with or without an engine that collects the results.
        """
        cases = (
            ("a NaN metric", False, {"metrics": {"auc": math.nan}}),
            ("an infinite norm from an engine that collects the results", True, {"update_norm": math.inf}),
        )
        for name, collects, bad in cases:
            with self.subTest(name):
                engine = ScriptedEngine()
                engine.collects_results = collects
                controller = self.new_controller(engine)
                for site_id in SITES:
                    controller.register_site(build_registration(site_id))
                    engine.register_worker(build_worker(site_id, expected_digest=self.digest))
                engine.extra_results = [completed_result("hospital-a", self.digest).model_copy(update=bad)]
                with self.assertRaises(InvalidRoundRecord) as raised:
                    controller.dispatch_round(self.manifest)
                self.assertIn(self.manifest.round_id, str(raised.exception))
                stored = controller.get_round(self.manifest.round_id)
                self.assertEqual(("closed", ENGINE_FAILED_REASON), (stored.status, stored.abandoned_reason))
                self.assertEqual([], stored.results)
                self.assertEqual([self.manifest.round_id], engine.discarded)
                self.assertEqual([], controller._rounds.unfinished())  # noqa: SLF001 - the store under test

    def test_a_posted_result_no_store_can_keep_is_refused_and_the_round_is_unchanged(self) -> None:
        self.controller.dispatch_round(self.manifest)
        before = self.controller.get_round(self.manifest.round_id)
        for name, bad in (("NaN metric", {"metrics": {"auc": math.nan}}), ("NaN norm", {"update_norm": math.nan})):
            with self.subTest(name):
                result = completed_result("hospital-a", self.digest).model_copy(update=bad)
                with self.assertRaises(InvalidRoundRecord):
                    self.controller.submit_result(result)
        self.assertEqual(before, self.controller.get_round(self.manifest.round_id))

    def test_an_engine_that_collects_results_refuses_posted_results(self) -> None:
        """A posted result must not replace what a site returned through the engine."""
        self.engine.collects_results = True
        record = self.controller.dispatch_round(self.manifest)
        self.assertTrue(self.controller.engine_collects_results)
        forged = completed_result("hospital-a", self.digest, examples=100000).model_copy(update={"metrics": {"auc": 1.0}})
        with self.assertRaises(RoundConflict) as raised:
            self.controller.submit_result(forged)
        self.assertIn("scripted engine", str(raised.exception))
        self.assertEqual(record, self.controller.get_round(self.manifest.round_id))
        # A site the plan did not select is still refused as not selected.
        with self.assertRaises(NotSelected):
            self.controller.submit_result(completed_result("intruder", self.digest))

    def test_an_engine_without_the_flag_takes_posted_results(self) -> None:
        self.assertFalse(self.controller.engine_collects_results)
        self.controller.dispatch_round(self.manifest)
        stored = self.controller.submit_result(completed_result("hospital-a", self.digest, examples=7))
        self.assertEqual(7, stored.results[0].contributed_examples)

    def test_a_finished_round_cannot_be_aggregated_again(self) -> None:
        """A second aggregate would replace the candidate, or reopen a round a decision closed."""
        self.controller.dispatch_round(self.manifest)
        candidate = self.controller.aggregate(self.manifest.round_id)
        aggregated = self.controller.get_round(self.manifest.round_id)
        with self.assertRaises(RoundConflict) as raised:
            self.controller.aggregate(self.manifest.round_id)
        self.assertIn("aggregated", str(raised.exception))
        self.assertEqual(aggregated, self.controller.get_round(self.manifest.round_id))

        self.controller.evaluate_release(self.manifest.round_id, [], candidate)
        closed = self.controller.get_round(self.manifest.round_id)
        self.assertEqual("closed", closed.status)
        with self.assertRaises(RoundConflict) as raised:
            self.controller.aggregate(self.manifest.round_id)
        self.assertIn("closed", str(raised.exception))
        self.assertEqual(closed, self.controller.get_round(self.manifest.round_id))

    def test_a_round_without_quorum_cannot_be_aggregated(self) -> None:
        manifest = build_manifest(round_id="short", invited_sites=["hospital-a"])
        closed = self.controller.dispatch_round(manifest)
        with self.assertRaises(RoundConflict):
            self.controller.aggregate("short")
        self.assertEqual(closed, self.controller.get_round("short"))

    def test_a_round_takes_one_release_decision(self) -> None:
        """A second decision would replace the evidence the first one left."""
        self.controller.dispatch_round(self.manifest)
        candidate = self.controller.aggregate(self.manifest.round_id)
        first = self.controller.evaluate_release(self.manifest.round_id, [], candidate)
        stored = self.controller.get_round(self.manifest.round_id)
        with self.assertRaises(RoundConflict) as raised:
            self.controller.evaluate_release(
                self.manifest.round_id, [], candidate.model_copy(update={"metrics": {"auc": 0.1}})
            )
        self.assertIn("release decision", str(raised.exception))
        self.assertEqual(stored, self.controller.get_round(self.manifest.round_id))
        self.assertEqual(first, stored.decision)

    def test_a_closed_round_takes_no_release_decision(self) -> None:
        self.controller.dispatch_round(self.manifest)
        self.controller.discard(self.manifest.round_id, "the study stopped")
        closed = self.controller.get_round(self.manifest.round_id)
        candidate = CandidateModel(
            round_id=self.manifest.round_id,
            candidate_version="model+round-0042",
            parent_version=self.manifest.model_version,
            aggregation_method="fedavg",
        )
        with self.assertRaises(RoundConflict) as raised:
            self.controller.evaluate_release(self.manifest.round_id, [], candidate)
        self.assertIn("closed", str(raised.exception))
        self.assertEqual(closed, self.controller.get_round(self.manifest.round_id))

    def test_a_decision_can_be_made_first_and_saved_after(self) -> None:
        """The GMA saves a decision only after the model registry accepted it."""
        self.controller.dispatch_round(self.manifest)
        candidate = self.controller.aggregate(self.manifest.round_id)
        before = self.controller.get_round(self.manifest.round_id)
        decision = self.controller.decide_release(self.manifest.round_id, [], candidate)
        self.assertEqual(before, self.controller.get_round(self.manifest.round_id))
        saved = self.controller.save_release_decision(self.manifest.round_id, decision)
        self.assertEqual(decision, saved.decision)
        self.assertEqual("closed", saved.status)
        with self.assertRaises(RoundConflict):
            self.controller.save_release_decision(self.manifest.round_id, decision)
        with self.assertRaises(RoundConflict):
            self.controller.decide_release(self.manifest.round_id, [], candidate)


class PhaseTaskTests(unittest.TestCase):
    """One map from a round's phase to the task its jobs carry, used by every engine and the LMA."""

    def test_every_round_phase_has_a_job_task(self) -> None:
        self.assertEqual(set(typing.get_args(RoundPhase)), set(PHASE_TASKS))
        tasks = set(typing.get_args(FederatedJob.model_fields["task"].annotation))
        self.assertLessEqual(set(PHASE_TASKS.values()), tasks)

    def test_the_http_engine_and_the_lma_read_the_shared_map(self) -> None:
        from dagents.gma.adapters import http_federation_engine
        from dagents.lma.application import federation_jobs

        self.assertIs(PHASE_TASKS, http_federation_engine.PHASE_TASKS)
        self.assertIs(PHASE_TASKS, federation_jobs.PHASE_TASKS)


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class InProcessEngineResultTests(unittest.TestCase):
    """The simulator's refused and failed results say why, in ``refusal_reason``."""

    def test_refused_and_failed_results_carry_a_refusal_reason(self) -> None:
        engine = InProcessFederationEngine()
        controller = FederatedRoundController(engine=engine)
        for site_id in SITES:
            controller.register_site(build_registration(site_id))
        manifest = build_manifest()
        digest = controller.digest(manifest)
        engine.register_worker(build_worker("hospital-a", expected_digest=digest))
        engine.register_worker(build_worker("hospital-b", expected_digest="fnv1a:something-else"))
        record = controller.dispatch_round(manifest)
        by_site = {result.site_id: result for result in record.results}
        self.assertIsNone(by_site["hospital-a"].refusal_reason)
        self.assertNotIn("refusal_reason", by_site["hospital-a"].model_dump(mode="json"))
        self.assertEqual("rejected", by_site["hospital-b"].participation)
        refused = {acceptance.site_id: acceptance.reason for acceptance in record.acceptances}
        self.assertTrue(refused["hospital-b"])
        self.assertEqual(refused["hospital-b"], by_site["hospital-b"].refusal_reason)
        self.assertEqual("failed", by_site["hospital-c"].participation)
        self.assertEqual("no worker is attached for this site", by_site["hospital-c"].refusal_reason)
        self.assertIn("no worker", by_site["hospital-c"].local_evidence_pointer)


class WeightedMetricsTests(unittest.TestCase):
    """The metric averaging both engines use."""

    def readiness(self, weights: dict[str, float]) -> AggregationReadiness:
        return AggregationReadiness(
            round_id="r", accepted_sites=sorted(weights), aggregation_permitted=True, site_weights=weights
        )

    def result(self, site_id: str, **metrics: float) -> SiteResult:
        return SiteResult(round_id="r", site_id=site_id, participation="completed", metrics=metrics)

    def test_each_shared_metric_is_weighted_by_the_planner_weights(self) -> None:
        metrics = weighted_metrics(
            self.readiness({"a": 0.25, "b": 0.75}),
            [self.result("a", auc=0.8, loss=0.4), self.result("b", auc=0.6, loss=0.2)],
        )
        self.assertEqual(["auc", "loss"], list(metrics))
        self.assertAlmostEqual(0.65, metrics["auc"], places=12)
        self.assertAlmostEqual(0.25, metrics["loss"], places=12)

    def test_a_metric_only_some_sites_reported_is_left_out(self) -> None:
        metrics = weighted_metrics(
            self.readiness({"a": 0.5, "b": 0.5}), [self.result("a", auc=0.8, extra=1.0), self.result("b", auc=0.6)]
        )
        self.assertEqual({"auc"}, set(metrics))

    def test_results_from_sites_that_were_not_accepted_are_ignored(self) -> None:
        metrics = weighted_metrics(
            self.readiness({"a": 1.0}), [self.result("a", auc=0.8), self.result("intruder", auc=0.0, other=1.0)]
        )
        self.assertEqual({"auc": 0.8}, metrics)

    def test_no_accepted_site_gives_no_metrics(self) -> None:
        self.assertEqual({}, weighted_metrics(self.readiness({}), [self.result("a", auc=0.8)]))


if __name__ == "__main__":
    unittest.main()
