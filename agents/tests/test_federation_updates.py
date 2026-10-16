"""Tests for model updates leaving a site through the worker.

A runner that trains returns a parameter update. The worker sends it through
the ``before_send`` Guard, hands the clipped vector to an update channel, and
returns only the norm and the channel's digest in the ``SiteResult``. The
coordinator sees the result; the vector goes to the channel.

The deterministic decisions come from the real ``dagentsc`` binary, including
the check that the planner accepts results that carry ``update_digest``.
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import pathlib
import unittest

from pydantic import BaseModel, ValidationError

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

    def test_a_refused_result_names_its_reason(self) -> None:
        """The reason travels in its own field; the evidence pointer keeps its old form."""
        cases = {
            "channel": build_worker(),
            "withheld": build_worker(runner=UpdateRunner({"w.reading": "0.3"}), update_channel=self.channel),
            "update store unavailable": build_worker(update_channel=FailingChannel()),
            "different update": build_worker(update_channel=DoublingChannel()),
            "approved field list": build_worker(update_channel=self.channel, approved_fields=["other"]),
        }
        for expected, worker in cases.items():
            with self.subTest(expected=expected):
                job = build_job(self.manifest)
                if expected == "approved field list":
                    job = job.model_copy(update={"feature_fields": ["reading"]})
                result = worker.execute(job)
                self.assertEqual(result.participation, "rejected")
                self.assertIn(expected, result.refusal_reason)
                self.assertTrue(result.local_evidence_pointer.endswith(f"#refused:{result.refusal_reason}"))

    def test_a_completed_result_has_no_refusal_reason(self) -> None:
        result = build_worker(update_channel=self.channel).execute(build_job(self.manifest))
        self.assertEqual(result.participation, "completed")
        self.assertIsNone(result.refusal_reason)


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

    def test_readiness_accepts_results_that_carry_a_refusal_reason(self) -> None:
        controller = FederatedRoundController(engine=InProcessFederationEngine())
        manifest = build_manifest()
        digest = controller.digest(manifest)
        results = [self._result(site, job_digest=digest) for site in SITES[:2]]
        results.append(
            self._result(
                "site-c",
                job_digest=digest,
                participation="rejected",
                privacy_checks_passed=False,
                contributed_examples=0,
                update_norm=None,
                update_digest=None,
                refusal_reason="the guard withheld the outbound model update",
            )
        )
        self.assertIn("refusal_reason", results[2])
        readiness = AggregationReadiness.model_validate(
            evaluate_aggregation_readiness(manifest.model_dump(mode="json"), results)
        )
        self.assertTrue(readiness.aggregation_permitted)
        self.assertEqual(readiness.accepted_sites, ["site-a", "site-b"])
        self.assertEqual([item.site_id for item in readiness.rejected_contributions], ["site-c"])

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


DOMAIN_DIR = pathlib.Path(__file__).resolve().parents[1] / "common" / "domain"

#: What a domain module may import: typing, pydantic and other domain modules.
#: Anything else (a database driver, a subprocess, the network, the planner
#: runner) would put I/O into the contracts.
ALLOWED_DOMAIN_IMPORTS = {
    "__future__",
    "typing",
    "pydantic",
    "datetime",
    "enum",
    "dataclasses",
    "math",
    "re",
    "collections",
    "collections.abc",
}


class DomainContractTests(unittest.TestCase):
    """The shared contracts the LMA, the GMA and the agent client exchange."""

    def test_domain_modules_import_no_io(self) -> None:
        for path in sorted(DOMAIN_DIR.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                for name in names:
                    with self.subTest(module=path.name, imports=name):
                        self.assertTrue(
                            name in ALLOWED_DOMAIN_IMPORTS or name.startswith("agents.common.domain."),
                            f"{path.name} imports {name}",
                        )

    def test_every_new_contract_field_has_a_description(self) -> None:
        """The API reference test needs one for every field an endpoint sends or returns."""
        from agents.common.domain import federation, site

        models = [
            federation.FederatedJob,
            federation.JobAcceptance,
            federation.JobModel,
            federation.DispatchCall,
            federation.RoundRecord,
            federation.UpdateUpload,
            federation.UpdateReceipt,
            federation.ReleaseEvaluationRequest,
            federation.ReleaseGuardCheck,
            federation.ReleaseEvaluation,
            federation.ModelReleaseDecision,
            federation.ModelVersion,
            federation.ModelVersionRegistration,
            federation.ModelVersionList,
            federation.ApprovalResult,
            federation.ModelStatusChange,
            federation.ModelLifecycleDecision,
            site.AuditSummary,
            site.SiteSourceInfo,
            site.SiteInfo,
            site.SourceState,
            site.SiteSummary,
            site.ProbeRequest,
            site.ProbeResponse,
            site.ModelInUse,
            site.LocalEvaluation,
            site.JobLogEntry,
            site.SiteDirectoryEntry,
        ]
        for model in models:
            for name, field in model.model_fields.items():
                with self.subTest(model=model.__name__, field=name):
                    self.assertTrue(field.description, f"{model.__name__}.{name} has no description")

    def test_site_result_carries_an_optional_refusal_reason(self) -> None:
        """A result without a reason keeps the keys results had before the field existed."""
        result = SiteResult(round_id="r", site_id="s", participation="completed")
        self.assertIsNone(result.refusal_reason)
        self.assertNotIn("refusal_reason", result.model_dump(mode="json"))
        self.assertNotIn("refusal_reason", json.loads(result.model_dump_json()))
        refused = result.model_copy(update={"participation": "rejected", "refusal_reason": "no channel"})
        self.assertEqual(refused.model_dump(mode="json")["refusal_reason"], "no channel")
        self.assertEqual(SiteResult.model_validate_json(refused.model_dump_json()), refused)
        self.assertIn("refusal_reason", SiteResult.model_json_schema(mode="serialization")["properties"])

    def test_a_round_record_dump_leaves_out_empty_refusal_reasons_only(self) -> None:
        from agents.common.domain.federation import RoundPlan, RoundRecord

        manifest = build_manifest()
        plan = RoundPlan(
            round_id=manifest.round_id,
            study_id=manifest.study_id,
            phase=manifest.phase,
            quorum_met=True,
            required_participants=2,
            aggregation=manifest.aggregation,
            round_digest="fnv1a:0000000000000000",
        )
        record = RoundRecord(
            manifest=manifest,
            plan=plan,
            created_at=1,
            updated_at=1,
            results=[
                SiteResult(round_id="r", site_id="site-a", participation="completed"),
                SiteResult(round_id="r", site_id="site-b", participation="rejected", refusal_reason="withheld"),
            ],
        )
        dumped = record.model_dump(mode="json")["results"]
        self.assertNotIn("refusal_reason", dumped[0])
        self.assertEqual(dumped[1]["refusal_reason"], "withheld")

    def test_a_round_record_keeps_acceptances_candidate_and_dispatch_rows(self) -> None:
        from agents.common.domain.federation import (
            CandidateModel,
            DispatchCall,
            JobAcceptance,
            RoundPlan,
            RoundRecord,
        )

        manifest = build_manifest()
        plan = RoundPlan(
            round_id=manifest.round_id,
            study_id=manifest.study_id,
            phase=manifest.phase,
            selected_sites=list(SITES),
            quorum_met=True,
            required_participants=2,
            aggregation=manifest.aggregation,
            round_digest="fnv1a:0000000000000000",
        )
        bare = RoundRecord(manifest=manifest, plan=plan, created_at=1, updated_at=1)
        self.assertEqual((bare.acceptances, bare.candidate, bare.dispatch, bare.abandoned_reason), ([], None, [], None))

        full = bare.model_copy(
            update={
                "status": "dispatched",
                "acceptances": [JobAcceptance(round_id=manifest.round_id, site_id="site-a", accepted=True)],
                "candidate": CandidateModel(
                    round_id=manifest.round_id,
                    candidate_version="toy-linear+round-0007",
                    parent_version="seed-v1",
                    aggregation_method="fedavg",
                ),
                "dispatch": [
                    DispatchCall(site_id="site-a", step="offer", status_code=200, ms=12.5, outcome="accepted"),
                    DispatchCall(site_id="site-b", step="offer", status_code=None, ms=30000.0, outcome="failed"),
                ],
                "abandoned_reason": "a later round replaced it",
            }
        )
        again = RoundRecord.model_validate_json(full.model_dump_json())
        self.assertEqual(again, full)
        with self.assertRaises(ValidationError):
            DispatchCall(site_id="site-a", step="aggregate", status_code=200, ms=1.0, outcome="accepted")
        with self.assertRaises(ValidationError):
            DispatchCall(site_id="site-a", step="offer", status_code=200, ms=-1.0, outcome="accepted")

    def test_a_job_carries_its_model_in_parameters(self) -> None:
        from agents.common.domain.federation import JobModel

        model = JobModel(version="toy-linear-init", kind="toy_linear", params={"bias": 0.0}, digest="sha256:abc")
        job = build_job(build_manifest()).model_copy(update={"parameters": {"model": model.model_dump(mode="json")}})
        self.assertEqual(JobModel.from_parameters(job.parameters), model)
        self.assertIsNone(JobModel.from_parameters({}))
        with self.assertRaises(ValueError):
            JobModel.from_parameters({"model": {"version": "v"}})

    def test_an_update_upload_refuses_values_that_are_not_finite_and_unknown_keys(self) -> None:
        from agents.common.domain.federation import UpdateReceipt, UpdateUpload

        upload = UpdateUpload(job_digest="fnv1a:1", update={"w": 0.5, "b": -1})
        self.assertEqual(upload.update, {"w": 0.5, "b": -1.0})
        self.assertEqual(UpdateReceipt(digest="sha256:x").digest, "sha256:x")
        for bad in ({"w": math.nan}, {"w": math.inf}, {"w": "high"}, {"w": "0.5"}, {"w": True}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    UpdateUpload(job_digest="fnv1a:1", update=bad)
        with self.assertRaises(ValidationError):
            UpdateUpload.model_validate({"job_digest": "fnv1a:1", "update": {"w": 0.1}, "site_id": "other"})

    def test_the_release_request_keeps_the_bare_flow_and_adds_the_round_flow(self) -> None:
        from agents.common.domain.federation import ReleaseEvaluationRequest

        bare = ReleaseEvaluationRequest.model_validate(
            {
                "gates": [{"gate_id": "g", "metric": "auc", "comparison": {"kind": "at_least", "value": 0.8}}],
                "candidate_version": "v+r",
                "candidate_metrics": {"auc": 0.9},
            }
        )
        self.assertIsNone(bare.condition_id)
        self.assertIsNone(bare.metrics_from_round)
        self.assertIsNone(bare.release_requester)
        rounds = ReleaseEvaluationRequest.model_validate(
            {
                "candidate_version": "toy-linear+training-5",
                "condition_id": "toy_condition",
                "metrics_from_round": "validation",
                "baseline_from_round": "evaluation",
                "change_from_version": "toy-linear-init",
                "rollback_version": "toy-threshold-v1",
                "release_requester": {"requester_id": "committee", "requester_kind": "human"},
                "declared_purpose": "toy_research",
            }
        )
        self.assertEqual(rounds.gates, [])
        self.assertEqual(rounds.release_requester.requester_kind, "human")

    def test_model_versions_leave_parameters_out_unless_asked(self) -> None:
        from agents.common.domain.federation import ModelVersion, ModelVersionRegistration

        version = ModelVersion(version="v1", kind="toy_linear", digest="sha256:a", status="candidate")
        self.assertIsNone(version.params)
        self.assertIsNone(version.release_decision)
        with self.assertRaises(ValidationError):
            ModelVersion(version="v1", kind="toy_linear", digest="sha256:a", status="approved")
        registration = ModelVersionRegistration(kind="toy_linear", params={"bias": 0.0})
        self.assertEqual(registration.requested_status, "candidate")
        with self.assertRaises(ValidationError):
            ModelVersionRegistration(kind="toy_linear", params={}, requested_status="superseded")

    def test_a_stored_release_decision_without_the_newer_keys_still_reads(self) -> None:
        """Decisions stored before `baseline_version` and `metrics_source` existed lack them."""
        from agents.common.domain.federation import ModelReleaseDecision

        older = ModelReleaseDecision.model_validate(
            {"action": "release", "guard_permitted": True, "gate_results": [], "round_id": "r"}
        )
        self.assertIsNone(older.baseline_version)
        self.assertIsNone(older.metrics_source)

    def test_a_probe_request_accepts_only_its_own_fields(self) -> None:
        """A caller cannot send its own classification or purposes with a probe."""
        from agents.common.domain.site import ProbeRequest

        body = {
            "boundary": "before_read",
            "granularity": "row",
            "requester": {"requester_id": "someone"},
            "declared_purpose": "toy_research",
        }
        probe = ProbeRequest.model_validate(body)
        self.assertEqual(probe.record_limit, 5)
        self.assertIsNone(probe.requested_fields)
        for extra in ({"classification": {"classification_id": "x"}}, {"approved_purposes": ["x"]}):
            with self.subTest(extra=extra):
                with self.assertRaises(ValidationError):
                    ProbeRequest.model_validate({**body, **extra})
        for limit in (-1, 21):
            with self.subTest(limit=limit):
                with self.assertRaises(ValidationError):
                    ProbeRequest.model_validate({**body, "record_limit": limit})

    def test_a_site_summary_names_why_a_rate_is_missing(self) -> None:
        from agents.common.domain.site import SiteSummary

        summary = SiteSummary(
            site_id="site-a",
            record_count=10,
            labelled_count=3,
            label_rate=None,
            label_rate_status="below_minimum_cohort",
            minimum_cohort=5,
        )
        self.assertIsNone(summary.planner_reason)
        with self.assertRaises(ValidationError):
            SiteSummary(
                site_id="site-a",
                record_count=10,
                labelled_count=3,
                label_rate_status="maybe",
                minimum_cohort=5,
            )

    def test_site_info_has_no_field_for_a_connection_string(self) -> None:
        from agents.common.domain.site import SiteInfo, SiteSourceInfo

        names = set(SiteInfo.model_fields) | set(SiteSourceInfo.model_fields)
        for forbidden in ("dsn", "url", "database_url", "connection", "password", "options"):
            self.assertNotIn(forbidden, names)

    def test_every_error_names_its_code_and_status(self) -> None:
        from agents.common.domain import errors

        expected = {
            errors.StoreUnavailableError: ("store_unavailable", 503),
            errors.StoreNotSetUpError: ("store_not_set_up", 503),
            errors.RoundConflict: ("conflict", 409),
            errors.NotSelected: ("forbidden", 403),
            errors.AggregationRefused: ("aggregation_refused", 409),
            errors.ClassificationConflict: ("conflict", 409),
            errors.ModelVerificationError: ("conflict", 409),
        }
        for error, (code, status) in expected.items():
            with self.subTest(error=error.__name__):
                self.assertEqual((error.code, error.http_status), (code, status))
                self.assertTrue(issubclass(error, errors.DagentsError))
        self.assertTrue(issubclass(errors.StoreNotSetUpError, errors.StoreUnavailableError))
        # The GMA answers a PermissionError from aggregation with 409, and a
        # model kind raises ValueError for bad parameters; both keep working.
        self.assertTrue(issubclass(errors.AggregationRefused, PermissionError))
        self.assertTrue(issubclass(errors.ModelVerificationError, ValueError))

    def test_new_models_are_pydantic_models(self) -> None:
        from agents.common.domain import site

        for name in ("SiteInfo", "SiteSummary", "JobLogEntry", "SiteDirectoryEntry"):
            self.assertTrue(issubclass(getattr(site, name), BaseModel))


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class PlannerLifecycleContractTests(unittest.TestCase):
    """The planner's lifecycle answer parses as `ModelLifecycleDecision`."""

    def test_allowed_and_refused_answers_parse(self) -> None:
        from agents.common.domain.federation import ModelLifecycleDecision
        from agents.common.infrastructure.dagents_runner import model_lifecycle

        registered = ModelLifecycleDecision.model_validate(
            model_lifecycle("v2", {"kind": "registered", "requested_status": "candidate", "released_version": "v1"})
        )
        self.assertTrue(registered.allowed)
        self.assertEqual(
            [(c.version, c.from_status, c.to_status) for c in registered.status_changes], [("v2", None, "candidate")]
        )
        refused = ModelLifecycleDecision.model_validate(
            model_lifecycle("v1", {"kind": "registered", "requested_status": "released", "released_version": "v0"})
        )
        self.assertFalse(refused.allowed)
        self.assertEqual(refused.refusal, "already_released")
        self.assertEqual(refused.status_changes, [])


class SiteRecordSourcePortTests(unittest.TestCase):
    """The ports an LMA reads a site's records through, and records its jobs in."""

    def test_the_toy_source_satisfies_the_site_record_source_port(self) -> None:
        from agents.common.application.ports import JobLog, SiteRecordSource
        from agents.tests.fixtures.toy_extension import toy_site_source

        source: SiteRecordSource = toy_site_source(None, "role", "site-a")
        for member in ("records", "count", "version", "location", "close"):
            self.assertTrue(callable(getattr(source, member)))
        self.assertTrue(hasattr(JobLog, "append") and hasattr(JobLog, "recent"))


if __name__ == "__main__":
    unittest.main()
