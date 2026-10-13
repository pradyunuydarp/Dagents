"""Tests for model updates leaving a site through the worker.

A runner that trains returns a parameter update. The worker sends it through
the ``before_send`` Guard, hands the clipped vector to an update channel, and
returns only the norm and the channel's digest in the ``SiteResult``. The
coordinator sees the result; the vector goes to the channel.

The deterministic decisions come from the real ``dagentsc`` binary, including
the check that the planner accepts results that carry ``update_digest``.
"""

from __future__ import annotations

import hashlib
import json
import math
import unittest

from agents.common.application.ethical_guard import contribution_norm
from agents.common.application.federation import FederatedRoundController, InProcessFederationEngine
from agents.common.application.federation_worker import (
    LocalFederatedWorker,
    LocalRunOutcome,
    UpdateChannel,
)
from agents.common.application.governance_service import GovernanceService
from agents.common.domain.federation import (
    AggregationMethod,
    AggregationReadiness,
    CandidateModel,
    FederatedJob,
    RoundManifest,
    SiteRegistration,
    SiteResult,
)
from agents.common.domain.governance import DataClassification
from agents.common.infrastructure.dagents_runner import evaluate_aggregation_readiness
from agents.tests.test_governance import dagentsc_available

SITES = ("site-a", "site-b", "site-c")
CONTRACT = "features-v1"
PURPOSE = "test_condition"

CLASSIFICATION = DataClassification(
    classification_id="update-test",
    field_sensitivity={"reading": "high", "group": "medium", "model_update": "high"},
    default_sensitivity="high",
    minimum_cohort=3,
)

RAW_UPDATE = {"w.reading": 30.0, "w.group": -40.0, "bias": 0.0}


class StubDataSource:
    """A site's local records, which never leave the site in these tests."""

    def __init__(self, records: int) -> None:
        self._records = [
            {"reading": 10.0 + index, "group": "a" if index % 2 else "b"} for index in range(records)
        ]

    def records(self, job) -> list[dict]:  # noqa: ANN001 - test double
        return [dict(record) for record in self._records]


class UpdateRunner:
    """A local training run that returns a parameter update."""

    def __init__(self, update: dict | None) -> None:
        self._update = update

    def run(self, job, records) -> LocalRunOutcome:  # noqa: ANN001 - test double
        return LocalRunOutcome(
            metrics={"train_loss": 0.5, "record_count": float(len(records))},
            examples=len(records),
            # A runner's own norm is ignored on the update path; the worker
            # measures the vector the Guard returned.
            update_norm=123.0,
            update=None if self._update is None else dict(self._update),
        )


class ScalarRunner:
    """A runner that reports only a norm, as runners did before updates existed."""

    def run(self, job, records) -> LocalRunOutcome:  # noqa: ANN001 - test double
        return LocalRunOutcome(metrics={"auc": 0.8}, examples=len(records), update_norm=9.9)


class RecordingChannel:
    """Keeps what each site submitted and returns a content digest."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, float]]] = []

    def submit(self, job: FederatedJob, update: dict[str, float]) -> str:
        self.calls.append((job.round_id, job.site_id, dict(update)))
        material = json.dumps(update, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return "sha256:" + hashlib.sha256(material).hexdigest()


class DoublingChannel(RecordingChannel):
    """A transport that changes the update and reports the digest of what it stored."""

    def submit(self, job: FederatedJob, update: dict[str, float]) -> str:
        return super().submit(job, {name: value * 2 for name, value in update.items()})


class PrefixedChannel(RecordingChannel):
    """A channel that digests the update with its own rule."""

    def submit(self, job: FederatedJob, update: dict[str, float]) -> str:
        return "custom:" + super().submit(job, update)


class FailingChannel:
    """A channel whose storage is down."""

    def submit(self, job: FederatedJob, update: dict[str, float]) -> str:
        raise ConnectionError("update store unavailable")


def build_manifest(**overrides) -> RoundManifest:
    payload = {
        "round_id": "round-0007",
        "study_id": "update-study",
        "condition_id": PURPOSE,
        "phase": "training",
        "model_version": "seed-v1",
        "model_artifact_digest": "sha256:seed",
        "training_code_digest": "sha256:code",
        "feature_contract_version": CONTRACT,
        "privacy_profile": "study-profile-v1",
        "aggregation": AggregationMethod(kind="fedavg"),
        "minimum_participants": 2,
        "minimum_cohort_per_site": 3,
        "required_capabilities": ["local_training"],
        "invited_sites": list(SITES),
    }
    payload.update(overrides)
    return RoundManifest(**payload)


def build_job(manifest: RoundManifest, site_id: str = "site-a") -> FederatedJob:
    return FederatedJob(
        round_id=manifest.round_id,
        site_id=site_id,
        manifest=manifest,
        manifest_digest="fnv1a:test",
        task="train",
    )


def build_worker(site_id: str = "site-a", runner=None, records: int = 6, **overrides) -> LocalFederatedWorker:
    governance = GovernanceService()
    governance.register_classification(CLASSIFICATION)
    payload = {
        "site_id": site_id,
        "governance": governance,
        "data_source": StubDataSource(records),
        "runner": runner if runner is not None else UpdateRunner(RAW_UPDATE),
        "classification_id": CLASSIFICATION.classification_id,
        "feature_contract_version": CONTRACT,
        "approved_fields": ["reading", "group"],
        "approved_purposes": [PURPOSE],
    }
    payload.update(overrides)
    return LocalFederatedWorker(**payload)


def build_registration(site_id: str) -> SiteRegistration:
    return SiteRegistration(
        site_id=site_id,
        capabilities=["local_training"],
        feature_contract_version=CONTRACT,
        approved_conditions=[PURPOSE],
        cohort_size=6,
        enrolled=True,
    )


class UpdateChannelProtocolTests(unittest.TestCase):
    """The channel is a structural protocol with one method."""

    def test_a_recording_channel_satisfies_the_protocol(self) -> None:
        channel: UpdateChannel = RecordingChannel()
        self.assertTrue(callable(channel.submit))

    def test_an_outcome_without_an_update_keeps_the_old_shape(self) -> None:
        outcome = LocalRunOutcome({"auc": 0.8}, 10, 0.4, "site-local://x")
        self.assertIsNone(outcome.update)
        self.assertEqual(outcome.update_norm, 0.4)
        self.assertEqual(outcome.evidence_pointer, "site-local://x")


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class WorkerUpdateTests(unittest.TestCase):
    """The worker's update path, one site at a time."""

    def setUp(self) -> None:
        self.channel = RecordingChannel()
        self.manifest = build_manifest()

    def test_the_channel_receives_the_clipped_update(self) -> None:
        worker = build_worker(update_channel=self.channel)
        result = worker.execute(build_job(self.manifest))

        self.assertEqual(result.participation, "completed")
        self.assertTrue(result.privacy_checks_passed)
        self.assertEqual(len(self.channel.calls), 1)
        round_id, site_id, sent = self.channel.calls[0]
        self.assertEqual((round_id, site_id), ("round-0007", "site-a"))
        self.assertEqual(sorted(sent), sorted(RAW_UPDATE))
        self.assertLessEqual(contribution_norm(sent), 1.0)
        self.assertAlmostEqual(sent["w.reading"], 0.6)
        self.assertAlmostEqual(sent["w.group"], -0.8)
        self.assertEqual(sent["bias"], 0.0)

        self.assertEqual(result.update_norm, contribution_norm(sent))
        material = json.dumps(sent, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.assertEqual(result.update_digest, "sha256:" + hashlib.sha256(material).hexdigest())
        self.assertEqual(result.contributed_examples, 6)

    def test_the_vector_is_not_stored_in_the_result(self) -> None:
        worker = build_worker(update_channel=self.channel)
        result = worker.execute(build_job(self.manifest))
        dumped = result.model_dump(mode="json")
        self.assertEqual(dumped["metrics"], {"train_loss": 0.5, "record_count": 6.0})
        for name in RAW_UPDATE:
            self.assertNotIn(name, json.dumps(dumped))
        self.assertIsInstance(dumped["update_norm"], float)
        self.assertIsInstance(dumped["update_digest"], str)

    def test_the_site_audit_records_privacy_accounting(self) -> None:
        governance = GovernanceService()
        governance.register_classification(CLASSIFICATION)
        worker = build_worker(governance=governance, update_channel=self.channel)
        worker.execute(build_job(self.manifest))

        sent = [record for record in governance.recent_audit() if record.boundary == "before_send"]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0].contribution_norm_before, 50.0)
        self.assertAlmostEqual(sent[0].contribution_norm_after, 1.0)
        self.assertLessEqual(sent[0].contribution_norm_after, 1.0)
        self.assertEqual(sent[0].contribution_strategies, ["clip_contribution:1"])
        self.assertTrue(governance.audit_chain_intact())

    def test_an_update_without_a_channel_is_refused(self) -> None:
        result = build_worker().execute(build_job(self.manifest))
        self.assertEqual(result.participation, "rejected")
        self.assertFalse(result.privacy_checks_passed)
        self.assertEqual(result.contributed_examples, 0)
        self.assertIsNone(result.update_norm)
        self.assertIsNone(result.update_digest)
        self.assertIn("channel", result.local_evidence_pointer)

    def test_an_update_the_guard_withholds_is_refused(self) -> None:
        worker = build_worker(runner=UpdateRunner({"w.reading": "0.3"}), update_channel=self.channel)
        result = worker.execute(build_job(self.manifest))
        self.assertEqual(result.participation, "rejected")
        self.assertIsNone(result.update_digest)
        self.assertEqual(self.channel.calls, [])
        self.assertIn("withheld", result.local_evidence_pointer)

    def test_an_update_the_channel_cannot_store_is_refused(self) -> None:
        worker = build_worker(update_channel=FailingChannel())
        result = worker.execute(build_job(self.manifest))
        self.assertEqual(result.participation, "rejected")
        self.assertIsNone(result.update_digest)
        self.assertIn("update store unavailable", result.local_evidence_pointer)

    def test_a_channel_that_stores_a_different_update_is_refused(self) -> None:
        """The site digests what it sent, so a transport that changed the update is caught."""
        channel = DoublingChannel()
        result = build_worker(update_channel=channel).execute(build_job(self.manifest))
        self.assertEqual(result.participation, "rejected")
        self.assertIsNone(result.update_digest)
        self.assertIn("different update", result.local_evidence_pointer)
        self.assertEqual(len(channel.calls), 1)

    def test_the_result_carries_the_sites_own_digest(self) -> None:
        worker = build_worker(update_channel=self.channel)
        result = worker.execute(build_job(self.manifest))
        sent = self.channel.calls[0][2]
        self.assertEqual(result.update_digest, LocalFederatedWorker.default_update_digest(sent))

    def test_a_channel_with_its_own_digest_rule_needs_the_same_rule_at_the_site(self) -> None:
        refused = build_worker(update_channel=PrefixedChannel()).execute(build_job(self.manifest))
        self.assertEqual(refused.participation, "rejected")
        accepted = build_worker(
            update_channel=PrefixedChannel(),
            update_digest=lambda update: "custom:" + LocalFederatedWorker.default_update_digest(update),
        ).execute(build_job(self.manifest))
        self.assertEqual(accepted.participation, "completed")
        self.assertTrue(accepted.update_digest.startswith("custom:sha256:"))

    def test_moderate_trust_sends_a_reproducible_noised_update(self) -> None:
        """Without a verified code digest the planner selects noise instead of clipping."""
        manifest = build_manifest(training_code_digest="", privacy_profile="default")
        first = RecordingChannel()
        second = RecordingChannel()
        small = {"w.reading": 0.3, "w.group": -0.4}
        result = build_worker(runner=UpdateRunner(small), update_channel=first).execute(build_job(manifest))
        build_worker(runner=UpdateRunner(small), update_channel=second).execute(build_job(manifest))

        self.assertEqual(result.participation, "completed")
        noised = first.calls[0][2]
        self.assertEqual(noised, second.calls[0][2])
        self.assertNotEqual(noised, small)
        self.assertAlmostEqual(result.update_norm, math.hypot(*noised.values()))

    def test_a_runner_without_an_update_keeps_the_scalar_path(self) -> None:
        worker = build_worker(runner=ScalarRunner(), update_channel=self.channel)
        result = worker.execute(build_job(self.manifest))
        self.assertEqual(result.participation, "completed")
        self.assertEqual(result.update_norm, 1.0)
        self.assertIsNone(result.update_digest)
        self.assertEqual(self.channel.calls, [])

    def test_a_scalar_runner_needs_no_channel(self) -> None:
        result = build_worker(runner=ScalarRunner()).execute(build_job(self.manifest))
        self.assertEqual(result.participation, "completed")
        self.assertEqual(result.update_norm, 1.0)


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class PlannerAcceptsUpdateDigestTests(unittest.TestCase):
    """The planner's site-result codec ignores ``update_digest``.

    The OCaml codec reads the keys it knows and skips the rest. If it ever
    starts rejecting unknown keys, these tests fail before a round does.
    """

    def _result(self, site_id: str, **overrides) -> dict:
        payload = {
            "round_id": "round-0007",
            "site_id": site_id,
            "job_digest": "",
            "participation": "completed",
            "code_verified": True,
            "privacy_checks_passed": True,
            "contributed_examples": 10,
            "update_norm": 0.9,
            "metrics": {"train_loss": 0.4},
            "update_digest": f"sha256:{site_id}",
        }
        payload.update(overrides)
        return SiteResult(**payload).model_dump(mode="json")

    def test_readiness_accepts_results_that_carry_an_update_digest(self) -> None:
        controller = FederatedRoundController(engine=InProcessFederationEngine())
        methods = (AggregationMethod(kind="fedavg"), AggregationMethod(kind="secure_aggregation", threshold=3))
        for aggregation in methods:
            with self.subTest(aggregation=aggregation.kind):
                manifest = build_manifest(aggregation=aggregation)
                digest = controller.digest(manifest)
                results = [self._result(site, job_digest=digest) for site in SITES]
                self.assertIn("update_digest", results[0])
                readiness = AggregationReadiness.model_validate(
                    evaluate_aggregation_readiness(manifest.model_dump(mode="json"), results)
                )
                self.assertTrue(readiness.aggregation_permitted)
                self.assertEqual(readiness.accepted_sites, list(SITES))
                self.assertEqual(readiness.rejected_contributions, [])
                self.assertAlmostEqual(sum(readiness.site_weights.values()), 1.0)

    def test_a_null_update_digest_is_accepted_too(self) -> None:
        controller = FederatedRoundController(engine=InProcessFederationEngine())
        manifest = build_manifest()
        digest = controller.digest(manifest)
        results = [self._result(site, job_digest=digest, update_digest=None) for site in SITES]
        readiness = AggregationReadiness.model_validate(
            evaluate_aggregation_readiness(manifest.model_dump(mode="json"), results)
        )
        self.assertTrue(readiness.aggregation_permitted)

    def test_a_round_with_updates_reaches_a_candidate(self) -> None:
        engine = InProcessFederationEngine()
        controller = FederatedRoundController(engine=engine)
        channel = RecordingChannel()
        manifest = build_manifest()
        digest = controller.digest(manifest)
        for site_id in SITES:
            controller.register_site(build_registration(site_id))
            engine.register_worker(build_worker(site_id, update_channel=channel, expected_digest=digest))

        record = controller.dispatch_round(manifest)
        self.assertEqual([result.participation for result in record.results], ["completed"] * 3)
        self.assertEqual(sorted(call[1] for call in channel.calls), list(SITES))
        for result in record.results:
            self.assertTrue(result.update_digest.startswith("sha256:"))
            self.assertLessEqual(result.update_norm, 1.0)

        readiness = controller.evaluate_readiness(manifest.round_id)
        self.assertTrue(readiness.aggregation_permitted)
        self.assertEqual(readiness.accepted_sites, list(SITES))

        candidate = controller.aggregate(manifest.round_id)
        self.assertIsInstance(candidate, CandidateModel)
        self.assertEqual(candidate.artifact_digest, "")
        stored = controller.get_round(manifest.round_id)
        self.assertEqual(
            {result.site_id: result.update_digest for result in stored.results},
            {result.site_id: result.update_digest for result in record.results},
        )


if __name__ == "__main__":
    unittest.main()
