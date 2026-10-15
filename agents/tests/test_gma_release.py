"""Release evaluation, approval, source reads and strict governance at the GMA.

A pilot-shaped study runs on one in-process network: a GMA with the HTTP
engine and three fixture LMAs (``agents/tests/fixtures/gma_fixtures.py``). An
evaluation round measures the released rule (the baseline), two training
rounds produce a candidate, and a validation round measures the candidate.
Then the GMA evaluates the release:

- the gates come from the condition pack, the metrics from the validation
  round's aggregate and the baseline from the evaluation round's;
- the ``before_release`` Guard runs with the pack's classification and
  purposes, and its decision joins the coordinator audit chain;
- the planner's lifecycle rule keeps or rejects the candidate, and the
  decision is stored with the model version;
- an approval is checked by the planner's lifecycle rule, and each refusal
  carries the planner's kind and sentence.

Every Guard decision, gate and lifecycle event comes from the real ``dagentsc``.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass

from fastapi.testclient import TestClient

from agents.common.domain.federation import (
    GateComparison,
    ModelReleaseDecision,
    ModelVersionRegistration,
    ReleaseEvaluationRequest,
    ReleaseGate,
    SiteRegistration,
    SiteResult,
)
from agents.common.domain.governance import KyuAttribute, Requester
from agents.common.extensions import ExtensionRegistry
from agents.common.infrastructure.agent_client import AgentError
from agents.gma.app_factory import GmaDeps, create_app
from agents.gma.config import Settings
from agents.tests.fixtures.gma_fixtures import (
    ADMIN_TOKEN,
    CAPABILITIES,
    INITIAL_PARAMS,
    INITIAL_VERSION,
    SITES,
    THRESHOLD_PARAMS,
    THRESHOLD_VERSION,
    build_study,
    register_models,
    toy_manifest,
    toy_registry,
)
from agents.tests.fixtures.toy_extension import (
    TOY_CLASSIFICATION,
    TOY_CONDITION,
    TOY_CONDITION_ID,
    TOY_CONTRACT,
    ToyExtension,
)
from agents.tests.test_governance import dagentsc_available

BASELINE_MARGIN = -0.5

RELEASE_GATES = [
    ReleaseGate(gate_id="accuracy-floor", metric="accuracy", comparison=GateComparison(kind="at_least", value=0.6)),
    ReleaseGate(
        gate_id="accuracy-vs-rule",
        metric="accuracy",
        comparison=GateComparison(kind="improves_on_baseline", margin=BASELINE_MARGIN),
        blocking=False,
    ),
]

COMMITTEE = Requester(
    requester_id="toy-release-committee",
    requester_kind="human",
    affiliation="toy-consortium",
    stated_purpose="toy_research",
    compliance_history=0.95,
    attributes=[
        KyuAttribute(attribute_id="verified_committee_identity", verified=True),
        KyuAttribute(attribute_id="signed_study_protocol", verified=True),
        KyuAttribute(attribute_id="governance_training", verified=True),
    ],
)


class ReleaseToyExtension(ToyExtension):
    """The toy extension, with a pack whose second gate compares with the baseline."""

    def condition_packs(self):  # noqa: ANN201 - same signature as the parent
        return [TOY_CONDITION.model_copy(update={"release_gates": list(RELEASE_GATES)})]


@dataclass
class Pilot:
    """The rounds of one pilot-shaped study."""

    evaluation_id: str
    training_ids: list[str]
    validation_id: str
    candidate_version: str
    baseline_metrics: dict[str, float]
    validation_metrics: dict[str, float]

    @property
    def last_training_id(self) -> str:
        return self.training_ids[-1]


def run_pilot(gma, digests: dict[str, str], prefix: str, *, training_rounds: int = 2, validate: bool = True) -> Pilot:
    """Run evaluation, training and validation rounds as an application would."""
    evaluation_id = f"{prefix}-evaluation"
    gma.dispatch_round(toy_manifest(evaluation_id, "evaluation", THRESHOLD_VERSION, digests[THRESHOLD_VERSION]))
    baseline = gma.aggregate(evaluation_id).metrics

    version, digest = INITIAL_VERSION, digests[INITIAL_VERSION]
    training_ids = []
    for number in range(1, training_rounds + 1):
        round_id = f"{prefix}-training-{number}"
        try:
            gma.dispatch_round(toy_manifest(round_id, "training", version, digest))
            candidate = gma.aggregate(round_id)
        finally:
            gma.discard_round(round_id, "the round is finished")
        version, digest = candidate.candidate_version, candidate.artifact_digest
        training_ids.append(round_id)

    validation_id = f"{prefix}-candidate-validation"
    gma.dispatch_round(toy_manifest(validation_id, "evaluation", version, digest))
    validation = gma.aggregate(validation_id).metrics if validate else {}
    return Pilot(evaluation_id, training_ids, validation_id, version, dict(baseline), dict(validation))


def round_flow(pilot: Pilot, **overrides) -> ReleaseEvaluationRequest:
    payload = {
        "candidate_version": pilot.candidate_version,
        "condition_id": TOY_CONDITION_ID,
        "metrics_from_round": pilot.validation_id,
        "baseline_from_round": pilot.evaluation_id,
        "change_from_version": INITIAL_VERSION,
        "rollback_version": THRESHOLD_VERSION,
        "release_requester": COMMITTEE,
        "declared_purpose": "toy_research",
    }
    payload.update(overrides)
    return ReleaseEvaluationRequest(**payload)


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class ReleaseEvaluationTests(unittest.TestCase):
    """The round flow: pack gates, round metrics, the release Guard and the lifecycle rule."""

    def setUp(self) -> None:
        self.study = build_study(registry=toy_registry(ReleaseToyExtension()))
        self.addCleanup(self.study.close)
        self.gma = self.study.gma()
        self.digests = register_models(self.gma)
        self.gma.refresh_sites()
        self.pilot = run_pilot(self.gma, self.digests, "pilot-1")
        self.client = TestClient(self.study.gma_app, headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})

    def evaluate(self, pilot: Pilot | None = None, **overrides):
        pilot = pilot or self.pilot
        return self.gma.evaluate_release(pilot.last_training_id, round_flow(pilot, **overrides))

    def raw_evaluate(self, round_id: str, body: dict):
        return self.client.post(f"/api/v1/federation/rounds/{round_id}:evaluate-release", json=body)

    def test_gates_come_from_the_pack_and_metrics_from_the_rounds(self) -> None:
        evaluation = self.evaluate()
        decision = evaluation.decision
        self.assertEqual([gate.gate_id for gate in decision.gate_results], ["accuracy-floor", "accuracy-vs-rule"])
        floor, versus = decision.gate_results
        self.assertEqual(floor.observed, self.pilot.validation_metrics["accuracy"])
        self.assertAlmostEqual(versus.threshold, self.pilot.baseline_metrics["accuracy"] + BASELINE_MARGIN, places=12)
        self.assertEqual(decision.action, "release")
        self.assertEqual(decision.candidate_version, self.pilot.candidate_version)
        self.assertEqual(decision.rollback_version, THRESHOLD_VERSION)
        self.assertEqual(evaluation.metrics_source, "rounds")
        self.assertEqual(evaluation.baseline_version, THRESHOLD_VERSION)
        self.assertEqual(evaluation.candidate_status, "candidate")
        saved = self.gma.get_round(self.pilot.last_training_id)
        self.assertEqual(saved.decision, decision)
        self.assertEqual(saved.status, "closed")

    def test_the_release_guard_runs_once_and_joins_the_coordinator_chain(self) -> None:
        before = self.gma.audit_summary()
        evaluation = self.evaluate()
        after = self.gma.audit_summary()
        self.assertEqual(after.record_count, before.record_count + 1)
        self.assertTrue(after.chain_intact)
        self.assertTrue(evaluation.guard.permitted, evaluation.guard.message)
        newest = self.client.get("/api/v1/governance/audit", params={"limit": 1}).json()["records"][0]
        self.assertEqual(newest["audit_id"], evaluation.guard.audit_id)
        self.assertEqual(newest["boundary"], "before_release")
        self.assertEqual(newest["requester_id"], COMMITTEE.requester_id)
        self.assertEqual(newest["request_id"], f"{self.pilot.last_training_id}:release:{self.pilot.candidate_version}")

    def test_the_decision_is_stored_with_the_model_version(self) -> None:
        self.evaluate()
        stored = self.gma.get_model_version(self.pilot.candidate_version)
        decision = stored.release_decision
        self.assertEqual(decision.action, "release")
        self.assertTrue(decision.guard_permitted)
        self.assertEqual(decision.metrics_source, "rounds")
        self.assertEqual(decision.baseline_version, THRESHOLD_VERSION)
        self.assertEqual(decision.round_id, self.pilot.last_training_id)
        self.assertEqual(len(decision.gate_results), 2)
        self.assertIsNotNone(decision.decided_at)

    def test_a_version_that_is_not_the_rounds_candidate_is_refused_before_the_guard(self) -> None:
        before = self.gma.audit_summary().record_count
        for overrides in ({}, {"metrics_from_round": None, "baseline_from_round": None}):
            with self.subTest(overrides=overrides), self.assertRaises(AgentError) as caught:
                self.evaluate(candidate_version="toy-linear+someone-else", **overrides)
            self.assertEqual((caught.exception.status, caught.exception.code), (409, "conflict"))
            self.assertIn("is not the candidate", caught.exception.detail)
        self.assertEqual(self.gma.audit_summary().record_count, before)
        # A round with no candidate has nothing to release in the round flow.
        with self.assertRaises(AgentError) as caught:
            self.gma.evaluate_release(self.pilot.evaluation_id.replace("evaluation", "nothing"), round_flow(self.pilot))
        self.assertEqual(caught.exception.status, 404)

    def test_the_round_flow_refuses_gates_and_metrics_from_the_body(self) -> None:
        body = round_flow(self.pilot).model_dump(mode="json")
        for extra in (
            {"gates": [gate.model_dump(mode="json") for gate in RELEASE_GATES]},
            {"candidate_metrics": {"accuracy": 1.0}},
            {"baseline_metrics": {"accuracy": 0.0}},
        ):
            with self.subTest(extra=sorted(extra)):
                response = self.raw_evaluate(self.pilot.last_training_id, {**body, **extra})
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["code"], "invalid_request")

    def test_the_round_flow_needs_a_requester_and_a_known_pack(self) -> None:
        body = round_flow(self.pilot).model_dump(mode="json")
        response = self.raw_evaluate(self.pilot.last_training_id, {**body, "release_requester": None})
        self.assertEqual((response.status_code, response.json()["code"]), (422, "invalid_request"))
        response = self.raw_evaluate(self.pilot.last_training_id, {**body, "condition_id": "no_such_condition"})
        self.assertEqual((response.status_code, response.json()["code"]), (404, "not_found"))
        response = self.raw_evaluate(self.pilot.last_training_id, {**body, "metrics_from_round": "no-such-round"})
        self.assertEqual((response.status_code, response.json()["code"]), (404, "not_found"))

    def test_the_metrics_round_must_have_measured_the_candidate(self) -> None:
        # The evaluation round measured the rule, not the candidate.
        response = self.raw_evaluate(
            self.pilot.last_training_id,
            round_flow(self.pilot, metrics_from_round=self.pilot.evaluation_id).model_dump(mode="json"),
        )
        self.assertEqual((response.status_code, response.json()["code"]), (409, "conflict"))
        self.assertIn(THRESHOLD_VERSION, response.json()["detail"])
        self.assertIsNone(self.gma.get_model_version(self.pilot.candidate_version).release_decision)

    def test_the_baseline_version_is_the_version_the_baseline_round_measured(self) -> None:
        # Both pilots measured the rule as their baseline. The first candidate
        # is released before the second one is evaluated, so the second one's
        # gates compared it with a model that is no longer in use.
        second = run_pilot(self.gma, self.digests, "pilot-4")
        self.evaluate()
        self.gma.approve(self.pilot.candidate_version)
        evaluation = self.evaluate(second)
        self.assertEqual(evaluation.baseline_version, THRESHOLD_VERSION)
        self.assertEqual(self.gma.get_model_version(second.candidate_version).release_decision.baseline_version, THRESHOLD_VERSION)
        self.assert_approval_refused(
            second.candidate_version,
            "baseline_changed",
            f"{second.candidate_version} was checked against {THRESHOLD_VERSION}, but {self.pilot.candidate_version} "
            "is now in use. Run the pilot again.",
        )

    def test_a_validation_round_that_was_not_aggregated_blocks_the_release(self) -> None:
        pilot = run_pilot(self.gma, self.digests, "pilot-2", validate=False)
        evaluation = self.evaluate(pilot)
        self.assertEqual([gate.outcome for gate in evaluation.decision.gate_results], ["not_evaluated"] * 2)
        self.assertEqual(evaluation.decision.action, "reject")
        self.assertEqual(evaluation.candidate_status, "rejected")
        self.assert_approval_refused(
            pilot.candidate_version, "gates_decided", f"the release gates decided reject for {pilot.candidate_version}"
        )

    def test_a_guard_denial_rejects_the_candidate(self) -> None:
        evaluation = self.evaluate(declared_purpose="marketing")
        self.assertFalse(evaluation.guard.permitted)
        self.assertEqual(evaluation.decision.action, "release")
        self.assertEqual(evaluation.candidate_status, "rejected")
        self.assertFalse(self.gma.get_model_version(self.pilot.candidate_version).release_decision.guard_permitted)
        self.assert_approval_refused(
            self.pilot.candidate_version, "guard_denied", f"the release Guard did not permit {self.pilot.candidate_version}"
        )

    def test_a_round_takes_one_release_evaluation(self) -> None:
        """A second evaluation would run the Guard again and replace the stored evidence."""
        self.evaluate(declared_purpose="marketing")
        round_before = self.gma.get_round(self.pilot.last_training_id)
        model_before = self.gma.get_model_version(self.pilot.candidate_version)
        audit_before = self.gma.audit_summary().record_count
        response = self.raw_evaluate(self.pilot.last_training_id, round_flow(self.pilot).model_dump(mode="json"))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "conflict")
        self.assertNotIn("refusal", response.json())
        self.assertIn("already holds a release decision", response.json()["detail"])
        self.assertEqual(self.gma.audit_summary().record_count, audit_before)
        self.assertEqual(self.gma.get_round(self.pilot.last_training_id), round_before)
        self.assertEqual(self.gma.get_model_version(self.pilot.candidate_version), model_before)
        self.assertFalse(model_before.release_decision.guard_permitted)

    def test_a_lifecycle_refusal_leaves_the_round_as_it_was(self) -> None:
        """The first training round's candidate was superseded, so the lifecycle rule refuses it."""
        first_round = self.pilot.training_ids[0]
        before = self.gma.get_round(first_round)
        superseded = before.candidate.candidate_version
        self.assertEqual(self.gma.get_model_version(superseded).status, "superseded")
        response = self.raw_evaluate(
            first_round,
            round_flow(self.pilot, candidate_version=superseded, metrics_from_round=None).model_dump(mode="json"),
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["refusal"], "not_candidate")
        after = self.gma.get_round(first_round)
        self.assertEqual(after, before)
        self.assertIsNone(after.decision)
        self.assertEqual(after.status, "aggregated")
        self.assertIsNone(self.gma.get_model_version(superseded).release_decision)

    def test_posted_results_cannot_replace_the_sites_validation_results(self) -> None:
        """With the HTTP engine every result comes from a site; an admin cannot post better metrics."""
        pilot = run_pilot(self.gma, self.digests, "forged", validate=False)
        before = self.gma.get_round(pilot.validation_id)
        for result in before.results:
            forged = result.model_copy(update={"metrics": {"accuracy": 0.99}, "contributed_examples": 5000})
            with self.subTest(site=result.site_id), self.assertRaises(AgentError) as caught:
                self.gma.submit_result(pilot.validation_id, forged)
            self.assertEqual((caught.exception.status, caught.exception.code), (409, "conflict"))
            self.assertIn("http engine", caught.exception.detail)
        self.assertEqual(self.gma.get_round(pilot.validation_id), before)
        validated = self.gma.aggregate(pilot.validation_id)
        self.assertLess(validated.metrics["accuracy"], 0.99)
        self.assertEqual(validated.contributed_examples, sum(result.contributed_examples for result in before.results))

    # --- Approval ---------------------------------------------------------------

    def assert_approval_refused(self, version: str, refusal: str, reason: str) -> None:
        response = self.client.post(f"/api/v1/model-versions/{version}:approve")
        self.assertEqual(response.status_code, 409, response.text)
        body = response.json()
        self.assertEqual((body["code"], body["refusal"], body["detail"]), ("conflict", refusal, reason))

    def test_approval_releases_the_candidate_and_supersedes_the_rule(self) -> None:
        self.evaluate()
        old_etag = self.client.get("/api/v1/model-versions:released").headers["etag"]
        result = self.gma.approve(self.pilot.candidate_version)
        self.assertEqual(result.released.version, self.pilot.candidate_version)
        self.assertEqual(result.released.status, "released")
        self.assertIsNone(result.released.params)
        self.assertEqual(result.previous, THRESHOLD_VERSION)
        self.assertEqual(self.gma.get_model_version(THRESHOLD_VERSION).status, "superseded")
        released = self.gma.released_model()
        self.assertEqual(released.version, self.pilot.candidate_version)
        self.assertEqual(set(released.params), set(INITIAL_PARAMS))
        self.assertNotEqual(self.client.get("/api/v1/model-versions:released").headers["etag"], old_etag)
        self.assertEqual(self.gma.list_model_versions().model_in_use.version, self.pilot.candidate_version)

    def test_each_approval_refusal_comes_from_the_planner_with_its_wording(self) -> None:
        spare = "toy-linear-spare"
        self.gma.register_model_version(spare, ModelVersionRegistration(kind="toy_linear", params={"bias": 0.5, "w.x1": 0.0, "w.x2": 0.0}))
        self.assert_approval_refused(spare, "no_decision", f"{spare} has no release decision; only a pilot's candidate can be approved")

        # A decision whose metrics a caller sent: no route stores one, but a
        # store can hold one written by other code, and the planner refuses it.
        sent = "toy-linear-sent-metrics"
        self.gma.register_model_version(sent, ModelVersionRegistration(kind="toy_linear", params={"bias": 0.25, "w.x1": 0.0, "w.x2": 0.0}))
        self.study.state.models.record_release_decision(
            sent,
            ModelReleaseDecision(
                action="release",
                guard_permitted=True,
                round_id=self.pilot.last_training_id,
                baseline_version=THRESHOLD_VERSION,
                metrics_source="request",
            ),
        )
        self.assert_approval_refused(
            sent, "metrics_not_from_rounds", f"the release decision for {sent} did not take its metrics from the rounds"
        )

        # Two candidates, both checked against the rule. The first one approved
        # changes the model in use, so the second one must be tested again.
        second = run_pilot(self.gma, self.digests, "pilot-3")
        self.evaluate()
        self.evaluate(second)
        self.gma.approve(self.pilot.candidate_version)
        self.assert_approval_refused(
            second.candidate_version,
            "baseline_changed",
            f"{second.candidate_version} was checked against {THRESHOLD_VERSION}, but {self.pilot.candidate_version} "
            "is now in use. Run the pilot again.",
        )
        self.assert_approval_refused(
            self.pilot.candidate_version,
            "not_candidate",
            f"{self.pilot.candidate_version} is released, not a candidate",
        )
        with self.assertRaises(AgentError) as caught:
            self.gma.approve("no-such-version")
        self.assertEqual((caught.exception.status, caught.exception.code), (404, "not_found"))


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class BareFlowTests(unittest.TestCase):
    """A GMA with no settings evaluates gates sent in the body, as the guide does."""

    def setUp(self) -> None:
        self.client = TestClient(create_app(Settings()))
        for site_id in ("store-a", "store-b"):
            response = self.client.put(
                f"/api/v1/federation/sites/{site_id}",
                json=SiteRegistration(
                    site_id=site_id,
                    capabilities=["evaluate"],
                    feature_contract_version="shop-v1",
                    approved_conditions=["returns"],
                    cohort_size=50,
                    enrolled=True,
                ).model_dump(mode="json"),
            )
            self.assertEqual(response.status_code, 200)
        manifest = {
            "round_id": "shop-round-1",
            "study_id": "shop",
            "condition_id": "returns",
            "phase": "evaluation",
            "model_version": "shop-rule-1",
            "feature_contract_version": "shop-v1",
            "minimum_participants": 2,
            "minimum_cohort_per_site": 10,
            "required_capabilities": ["evaluate"],
            "invited_sites": ["store-a", "store-b"],
        }
        record = self.client.post("/api/v1/federation/rounds", json=manifest).json()
        for site_id, accuracy in (("store-a", 0.8), ("store-b", 0.9)):
            result = SiteResult(
                round_id="shop-round-1",
                site_id=site_id,
                job_digest=record["plan"]["round_digest"],
                participation="completed",
                code_verified=True,
                privacy_checks_passed=True,
                contributed_examples=50,
                metrics={"accuracy": accuracy},
            )
            self.assertEqual(
                self.client.post("/api/v1/federation/rounds/shop-round-1/results", json=result.model_dump(mode="json")).status_code,
                202,
            )
        self.candidate = self.client.post("/api/v1/federation/rounds/shop-round-1:aggregate").json()

    def body(self, **overrides) -> dict:
        payload = {
            "gates": [{"gate_id": "accuracy-floor", "metric": "accuracy", "comparison": {"kind": "at_least", "value": 0.8}}],
            "candidate_version": self.candidate["candidate_version"],
            "candidate_metrics": {"accuracy": 0.85},
        }
        payload.update(overrides)
        return payload

    def test_the_bare_flow_reads_gates_and_metrics_from_the_body_and_runs_no_guard(self) -> None:
        response = self.client.post("/api/v1/federation/rounds/shop-round-1:evaluate-release", json=self.body())
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["decision"]["action"], "release")
        self.assertEqual(body["decision"]["gate_results"][0]["observed"], 0.85)
        self.assertEqual(body["decision"]["rollback_version"], "shop-rule-1")
        self.assertIsNone(body["guard"])
        self.assertIsNone(body["candidate_status"])
        self.assertIsNone(body["baseline_version"])
        self.assertEqual(body["metrics_source"], "request")
        self.assertEqual(self.client.get("/api/v1/governance/audit:summary").json(), {"chain_intact": True, "record_count": 0})

    def test_the_bare_flow_still_checks_the_stored_candidate(self) -> None:
        response = self.client.post(
            "/api/v1/federation/rounds/shop-round-1:evaluate-release", json=self.body(candidate_version="shop-rule-1+other")
        )
        self.assertEqual((response.status_code, response.json()["code"]), (409, "conflict"))
        response = self.client.post("/api/v1/federation/rounds/shop-round-9:evaluate-release", json=self.body())
        self.assertEqual(response.status_code, 404)


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class SimulatorRoundFlowTests(unittest.TestCase):
    """A GMA on the simulator holds only results that callers posted.

    Its round flow therefore records ``metrics_source: request``, and the
    lifecycle rule refuses to approve the candidate, however good the posted
    metrics are.
    """

    def setUp(self) -> None:
        self.client = TestClient(create_app(Settings(), GmaDeps(registry=toy_registry(ReleaseToyExtension()))))
        self.digests = {}
        for version, kind, params, status in (
            (THRESHOLD_VERSION, "toy_threshold", dict(THRESHOLD_PARAMS), "released"),
            (INITIAL_VERSION, "toy_linear", dict(INITIAL_PARAMS), "candidate"),
        ):
            response = self.client.put(
                f"/api/v1/model-versions/{version}",
                json={"kind": kind, "params": params, "requested_status": status},
            )
            self.assertIn(response.status_code, (200, 201), response.text)
            self.digests[version] = response.json()["digest"]
        for site_id in SITES:
            response = self.client.put(
                f"/api/v1/federation/sites/{site_id}",
                json=SiteRegistration(
                    site_id=site_id,
                    capabilities=list(CAPABILITIES),
                    feature_contract_version=TOY_CONTRACT.version,
                    approved_conditions=[TOY_CONDITION_ID],
                    cohort_size=30,
                    enrolled=True,
                ).model_dump(mode="json"),
            )
            self.assertEqual(response.status_code, 200, response.text)

    def run_posted_round(self, round_id: str, phase: str, version: str, accuracy: float) -> dict:
        manifest = toy_manifest(round_id, phase, version, self.digests.get(version, "sha256:none"))
        record = self.client.post("/api/v1/federation/rounds", json=manifest.model_dump(mode="json"))
        self.assertEqual(record.status_code, 202, record.text)
        for site_id in SITES:
            result = SiteResult(
                round_id=round_id,
                site_id=site_id,
                job_digest=record.json()["plan"]["round_digest"],
                participation="completed",
                code_verified=True,
                privacy_checks_passed=True,
                contributed_examples=30,
                update_norm=0.1,
                metrics={"accuracy": accuracy},
            )
            posted = self.client.post(f"/api/v1/federation/rounds/{round_id}/results", json=result.model_dump(mode="json"))
            self.assertEqual(posted.status_code, 202, posted.text)
        candidate = self.client.post(f"/api/v1/federation/rounds/{round_id}:aggregate")
        self.assertEqual(candidate.status_code, 200, candidate.text)
        return candidate.json()

    def test_round_flow_metrics_from_posted_results_cannot_be_approved(self) -> None:
        self.run_posted_round("sim-evaluation", "evaluation", THRESHOLD_VERSION, 0.5)
        candidate = self.run_posted_round("sim-train-1", "training", INITIAL_VERSION, 0.5)
        version = candidate["candidate_version"]
        trained = {"bias": 0.25, "w.x1": 0.5, "w.x2": -0.5}
        registered = self.client.put(f"/api/v1/model-versions/{version}", json={"kind": "toy_linear", "params": trained})
        self.assertEqual(registered.status_code, 201, registered.text)
        self.digests[version] = registered.json()["digest"]
        self.run_posted_round("sim-validation-1", "evaluation", version, 0.99)

        body = round_flow(Pilot("sim-evaluation", ["sim-train-1"], "sim-validation-1", version, {}, {})).model_dump(
            mode="json"
        )
        response = self.client.post("/api/v1/federation/rounds/sim-train-1:evaluate-release", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        evaluation = response.json()
        self.assertEqual(evaluation["decision"]["action"], "release")
        self.assertTrue(evaluation["guard"]["permitted"], evaluation["guard"]["message"])
        self.assertEqual(evaluation["metrics_source"], "request")
        stored = self.client.get(f"/api/v1/model-versions/{version}").json()
        self.assertEqual(stored["release_decision"]["metrics_source"], "request")

        approval = self.client.post(f"/api/v1/model-versions/{version}:approve")
        self.assertEqual(approval.status_code, 409, approval.text)
        self.assertEqual(approval.json()["refusal"], "metrics_not_from_rounds")
        self.assertEqual(self.client.get("/api/v1/model-versions:released").json()["version"], THRESHOLD_VERSION)


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class BareFlowOnRegisteredCandidateTests(unittest.TestCase):
    """The bare flow never changes the model registry."""

    def test_a_bare_evaluation_leaves_a_registered_candidate_as_it_was(self) -> None:
        study = build_study(registry=toy_registry(ReleaseToyExtension()))
        self.addCleanup(study.close)
        gma = study.gma()
        digests = register_models(gma)
        gma.refresh_sites()
        pilot = run_pilot(gma, digests, "bare", training_rounds=1)
        evaluation = gma.evaluate_release(
            pilot.last_training_id,
            ReleaseEvaluationRequest(
                gates=list(RELEASE_GATES),
                candidate_version=pilot.candidate_version,
                candidate_metrics={"accuracy": 0.99},
                baseline_metrics={"accuracy": 0.5},
            ),
        )
        self.assertEqual(evaluation.metrics_source, "request")
        self.assertIsNone(evaluation.guard)
        self.assertEqual(evaluation.candidate_status, "candidate")
        self.assertEqual(evaluation.baseline_version, THRESHOLD_VERSION)
        self.assertIsNone(gma.get_model_version(pilot.candidate_version).release_decision)


class SourceReadTests(unittest.TestCase):
    """``GMA_ALLOW_SOURCE_READS=0`` answers 403 on every route that reads a source."""

    ROUTES = (
        ("POST", "/api/v1/sources", {"source_id": "s", "kind": "inline", "selection": {"records": [{"a": 1}]}}),
        ("GET", "/api/v1/sources", None),
        ("GET", "/api/v1/sources/s", None),
        ("POST", "/api/v1/sources/s:validate", None),
        ("POST", "/api/v1/datasets:profile", {"scope_id": "s", "dataset": {"source_id": "s"}}),
        ("POST", "/datasets/profile", {"scope_id": "s", "records": [{"a": 1}]}),
        ("POST", "/api/v1/model-jobs", {"scope_id": "s", "dataset": {"source_id": "s"}}),
        ("POST", "/models/run", {"scope_id": "s", "dataset": {"source_id": "s"}}),
        ("GET", "/api/v1/model-jobs", None),
        ("GET", "/models/runs", None),
    )

    def test_switched_off_source_reads_answer_403(self) -> None:
        client = TestClient(create_app(Settings(allow_source_reads=False)))
        for method, path, body in self.ROUTES:
            with self.subTest(method=method, path=path):
                response = client.request(method, path, json=body)
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(response.json()["code"], "forbidden")

    def test_source_reads_are_on_by_default(self) -> None:
        client = TestClient(create_app(Settings()))
        response = client.post("/api/v1/sources", json=self.ROUTES[0][2])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(client.get("/api/v1/sources").status_code, 200)


def enforce_body(classification: dict) -> dict:
    """A release-boundary enforcement request for the committee, with this classification."""
    return {
        "request": {
            "request_id": "toy-round:release",
            "boundary": "before_release",
            "requester": COMMITTEE.model_dump(mode="json"),
            "classification": classification,
            "requested_fields": ["model_update"],
            "granularity": "model_update",
            "cohort_size": 90,
            "declared_purpose": "toy_research",
            "approved_purposes": ["toy_research"],
        },
        "payload": [{"model_update": 0.5}],
    }


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class StrictGovernanceTests(unittest.TestCase):
    """A GMA with condition packs plans and enforces only against registered classifications."""

    def test_a_gma_with_packs_refuses_a_differing_inline_classification(self) -> None:
        client = TestClient(create_app(Settings(), GmaDeps(registry=toy_registry())))
        registered = TOY_CLASSIFICATION.model_dump(mode="json")
        weaker = {**registered, "field_sensitivity": {name: "low" for name in registered["field_sensitivity"]}}
        refused = client.post("/api/v1/governance/restrictions:enforce", json=enforce_body(weaker))
        self.assertEqual((refused.status_code, refused.json()["code"]), (409, "conflict"))
        accepted = client.post("/api/v1/governance/restrictions:enforce", json=enforce_body(registered))
        self.assertEqual(accepted.status_code, 200, accepted.text)
        replaced = client.put(f"/api/v1/governance/classifications/{TOY_CLASSIFICATION.classification_id}", json=weaker)
        self.assertEqual((replaced.status_code, replaced.json()["code"]), (409, "conflict"))

    def test_a_gma_without_packs_accepts_an_inline_classification(self) -> None:
        client = TestClient(create_app(Settings()))
        inline = {**TOY_CLASSIFICATION.model_dump(mode="json"), "classification_id": "inline-only"}
        response = client.post("/api/v1/governance/restrictions:enforce", json=enforce_body(inline))
        self.assertEqual(response.status_code, 200, response.text)


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class GmaPostgresWiringTests(unittest.TestCase):
    """A GMA started from settings keeps its model versions and audit chain in Postgres.

    The tables are made in a scratch database (``DAGENTS_TEST_DATABASE_URL``)
    with the columns of the healthcare demo's coordinator tables. A second GMA
    on the same tables, as after a restart, sees what the first one stored.
    """

    @classmethod
    def setUpClass(cls) -> None:
        from psycopg import connect, sql

        from agents.tests.test_model_registry import MODELS_TABLE
        from agents.tests.test_postgres_roles import ScratchDatabase, admin_dsn_or_skip

        cls.db = ScratchDatabase(admin_dsn_or_skip())
        try:
            cls.db.start()
            role = cls.db.alpha
            with connect(cls.db.dsn, autocommit=True) as conn, conn.transaction():
                conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
                conn.execute(sql.SQL(MODELS_TABLE).format(table=sql.Identifier(role, "models")))
                conn.execute(
                    sql.SQL("CREATE UNIQUE INDEX models_one_release ON {} ((true)) WHERE status = 'released'").format(
                        sql.Identifier(role, "models")
                    )
                )
        except BaseException:
            cls.db.stop()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.stop()

    def gma(self) -> TestClient:
        role = self.db.alpha
        app = create_app(
            Settings(
                extensions="agents.tests.fixtures.toy_extension:ToyExtension",
                database_url=self.db.dsn,
                database_role=role,
                model_table=f"{role}.models",
                audit_table=f"{role}.audit",
                audit_anchor_table=f"{role}.audit_anchor",
            )
        )
        self.addCleanup(app.state.gma.close)
        return TestClient(app)

    def test_model_versions_and_the_audit_chain_outlive_the_gma(self) -> None:
        first = self.gma()
        registered = first.put(
            f"/api/v1/model-versions/{THRESHOLD_VERSION}",
            json={"kind": "toy_threshold", "params": {"threshold": 0.0}, "requested_status": "released"},
        )
        self.assertEqual(registered.status_code, 201, registered.text)
        decision = first.post(
            "/api/v1/governance/restrictions:enforce",
            json=enforce_body(TOY_CLASSIFICATION.model_dump(mode="json")),
        )
        self.assertEqual(decision.status_code, 200, decision.text)
        self.assertEqual(first.get("/api/v1/governance/audit:summary").json(), {"chain_intact": True, "record_count": 1})

        second = self.gma()
        released = second.get("/api/v1/model-versions:released")
        self.assertEqual(released.status_code, 200, released.text)
        self.assertEqual(released.json()["version"], THRESHOLD_VERSION)
        again = second.put(
            f"/api/v1/model-versions/{THRESHOLD_VERSION}",
            json={"kind": "toy_threshold", "params": {"threshold": 0.0}, "requested_status": "released"},
        )
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(second.get("/api/v1/governance/audit:summary").json(), {"chain_intact": True, "record_count": 1})
        # The connection string is never in an answer.
        for response in (registered, decision, released, again):
            self.assertNotIn(self.db.dsn, response.text)


class DefaultGmaTests(unittest.TestCase):
    """A GMA with no settings runs open, with the in-process simulator, as before."""

    def setUp(self) -> None:
        self.client = TestClient(create_app(Settings()))

    def test_health_names_the_simulator(self) -> None:
        self.assertEqual(
            self.client.get("/api/v1/health").json(),
            {"status": "ok", "agent_type": "GMA", "engine": "in-process-simulator"},
        )
        self.assertEqual(self.client.get("/health").json(), self.client.get("/api/v1/health").json())

    def test_new_routes_answer_without_tokens_and_with_nothing_configured(self) -> None:
        self.assertEqual(self.client.post("/api/v1/federation/sites:refresh").json(), [])
        self.assertEqual(self.client.post("/federation/sites:refresh").json(), [])
        self.assertEqual(self.client.get("/api/v1/federation/sites/summaries").json(), [])
        self.assertEqual(self.client.get("/api/v1/model-versions").json(), {"model_in_use": None, "versions": []})
        released = self.client.get("/api/v1/model-versions:released")
        self.assertEqual((released.status_code, released.json()["code"]), (404, "not_found"))
        self.assertEqual(self.client.get("/governance/audit:summary").json(), {"chain_intact": True, "record_count": 0})

    def test_a_model_version_needs_a_kind_an_extension_contributes(self) -> None:
        client = TestClient(create_app(Settings(), GmaDeps(registry=ExtensionRegistry())))
        response = client.put("/api/v1/model-versions/v1", json={"kind": "toy_linear", "params": INITIAL_PARAMS})
        self.assertEqual((response.status_code, response.json()["code"]), (404, "not_found"))


if __name__ == "__main__":
    unittest.main()
