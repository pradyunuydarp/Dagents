"""End-to-end tests of the governed federated pilot on the IST hospitals.

These tests check that the app gets its guarantees from the framework, not
from its own claims: a hospital's LMA can refuse a job, nothing at the level of
a patient leaves a hospital, model updates are clipped before the GMA combines
them, the coordinator works only from what hospitals report, and the release
decision follows from the gate results.

The pilot runs as the app runs it: three Dagents LMAs and a GMA in process,
called through their HTTP routes. Some tests look inside one agent, or change
how it behaves, through the agent apps the app built (``tests.support``). They
use the in-memory stores loaded from the committed extract, and the real
planner. ``test_pilot_postgres.py`` runs the pilot against Postgres.
"""

from __future__ import annotations

import inspect
import json
import re
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from agents.common.domain.federation import GateComparison, ReleaseGate

from app.domain import stroke_model, stroke_rule
from app.domain.conditions import STROKE_FEATURE_CONTRACT, STROKE_FEATURE_CONTRACT_VERSION, STROKE_RELEASE_GATES
from app.domain.dataset import SITES
from app.domain.model_kinds import INITIAL_VERSION, LOGISTIC_REGRESSION, POINTS_RULE, SEED_VERSION
from app.services.agents import AgentError, HealthcareDemo
from app.services.charts_service import ChartService
from app.services.overview import ApprovalRefused, StudyOverview
from app.services.pilot import ModelRef, PilotService

from tests.support import BuiltAgent, built_demo, dagentsc_available, gated_extension, tiny_site

#: Every v3 record field and row key. None of them may appear in a round result.
RECORD_FIELDS = set(STROKE_FEATURE_CONTRACT.field_names()) | {
    "encounter_id",
    "source_row",
    "origin",
    "age",
    "recorded_at",
    "client_request_id",
    "records",
}

#: The keys a site result may have.
RESULT_KEYS = {
    "round_id",
    "site_id",
    "job_digest",
    "participation",
    "code_verified",
    "privacy_checks_passed",
    "contributed_examples",
    "update_norm",
    "update_digest",
    "metrics",
    "local_evidence_pointer",
}

#: An encounter id of any of the three hospitals, from the extract or entered.
ENCOUNTER_ID = re.compile(r"\b(uk-134|it-002|ch-245)-(\d{5}|e\d+)\b")

#: The L2 bound the planner gives ``clip_contribution`` for a model update from
#: a high-trust coordinator. The test reads it back from the audit records.
CLIP_BOUND = 1.0

SITE_SIZES = {site.site_id: site.expected_rows for site in SITES}
SITE_IDS = [site.site_id for site in SITES]

#: One gate the federated candidate clears, so the planner decides ``release``.
LENIENT_GATES = [
    ReleaseGate(gate_id="discrimination", metric="auc", comparison=GateComparison(kind="at_least", value=0.5))
]

#: One gate no candidate clears, so the planner decides ``reject``.
IMPOSSIBLE_GATES = [
    ReleaseGate(gate_id="impossible", metric="auc", comparison=GateComparison(kind="at_least", value=1.01))
]


def _rounds(report: dict) -> list[dict]:
    rounds = report["rounds"]
    return [rounds["analytics"], rounds["evaluation"], *rounds["training"], rounds["candidate_validation"]]


def _ref(model: Any) -> ModelRef:
    return ModelRef(model.version, model.kind, model.digest)


class DemoCase(unittest.TestCase):
    """Builds app services for one test and stops them when it ends."""

    def demo(self, **kwargs: Any) -> HealthcareDemo:
        demo = HealthcareDemo(**kwargs)
        self.addCleanup(demo.close)
        return demo

    def demo_and_agents(self, **kwargs: Any) -> tuple[HealthcareDemo, dict[str, BuiltAgent]]:
        demo = self.demo(**kwargs)
        return demo, built_demo(demo)

    @staticmethod
    def stored_model(agents: dict[str, BuiltAgent], version: str) -> Any:
        """A model version as the GMA's model store holds it, with its parameters."""
        return agents["gma"].state.models.store.get(version)


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class PilotTests(unittest.TestCase):
    """One pilot, run once, and the rounds it produced."""

    demo: HealthcareDemo
    agents: dict[str, BuiltAgent]
    report: dict
    guarded: dict[str, list[tuple[Any, list[dict], Any]]]

    @classmethod
    def setUpClass(cls) -> None:
        cls.demo = HealthcareDemo()
        try:
            cls.agents = built_demo(cls.demo)
            # Keep each hospital's before_read Guard decisions: what its runner was given.
            cls.guarded = {site_id: [] for site_id in SITE_IDS}
            patches = []
            for site_id in SITE_IDS:
                governance = cls.agents[f"lma:{site_id}"].state.site.governance
                patches.append(mock.patch.object(governance, "enforce", side_effect=cls._spy(site_id, governance.enforce)))
            for patch in patches:
                patch.start()
            try:
                cls.report = cls.demo.pilot.run_pilot("test")
            finally:
                for patch in patches:
                    patch.stop()
        except BaseException:
            cls.demo.close()
            raise

    @classmethod
    def _spy(cls, site_id: str, real: Any) -> Any:
        def enforce(request, payload=None, *, correlation_id=None):
            decision = real(request, payload, correlation_id=correlation_id)
            if request.boundary == "before_read":
                cls.guarded[site_id].append((correlation_id, [dict(row) for row in payload or []], decision))
            return decision

        return enforce

    @classmethod
    def tearDownClass(cls) -> None:
        cls.demo.close()

    def test_the_analytics_round_selects_every_hospital(self) -> None:
        analytics = self.report["rounds"]["analytics"]
        plan = analytics["plan"]
        self.assertTrue(plan["quorum_met"])
        self.assertEqual(sorted(SITE_SIZES), sorted(plan["selected_sites"]))
        self.assertEqual([], plan["excluded_sites"])
        self.assertEqual(3, plan["required_participants"], "the secure aggregation threshold sets the quorum")
        self.assertTrue(analytics["readiness"]["aggregation_permitted"])
        for result in analytics["results"]:
            self.assertEqual("completed", result["participation"])
            self.assertEqual(SITE_SIZES[result["site_id"]], result["contributed_examples"])
            self.assertEqual(float(SITE_SIZES[result["site_id"]]), result["metrics"]["record_count"])
            self.assertIn("outcome_rate", result["metrics"], "every hospital has more than 20 labelled records")

    def test_no_patient_level_data_crosses_the_boundary(self) -> None:
        """A result carries counts, metrics, a norm and digests. Never a record or an update."""
        for round_report in _rounds(self.report):
            for result in round_report["results"]:
                with self.subTest(round=round_report["round_id"], site=result["site_id"]):
                    self.assertLessEqual(set(result), RESULT_KEYS)
                    self.assertEqual(set(), RECORD_FIELDS & set(result["metrics"]))
                    for value in result["metrics"].values():
                        self.assertIsInstance(value, float)
                    self.assertTrue(result["local_evidence_pointer"].startswith("site-local://"))
                    if round_report["phase"] == "training":
                        self.assertRegex(result["update_digest"], r"^sha256:[0-9a-f]{64}$")
                        self.assertIsInstance(result["update_norm"], float)
                    else:
                        self.assertIsNone(result["update_digest"])

        text = json.dumps(self.report)
        self.assertIsNone(ENCOUNTER_ID.search(text), "an encounter id appears in the pilot report")
        # Field names appear in Guard plans and audit records. The coordinates
        # below exist only in a parameter or update vector.
        vector_only = set(stroke_model.FEATURES) - set(STROKE_FEATURE_CONTRACT.field_names())
        self.assertIn("subtype_tacs", vector_only)
        for coordinate in sorted(vector_only):
            self.assertNotIn(f'"{coordinate}"', text, "an update or parameter vector appears in the pilot report")

    def test_training_lowers_the_loss_and_every_update_is_clipped(self) -> None:
        curve = self.report["training_curve"]
        self.assertEqual(stroke_model.DEFAULT_ROUNDS, len(curve))
        losses = [point["loss"] for point in curve]
        self.assertTrue(all(isinstance(loss, float) for loss in losses))
        self.assertLess(losses[0], curve[0]["start_loss"], "the first round did not lower the loss")
        for earlier, later in zip(losses, losses[1:], strict=False):
            self.assertLess(later, earlier)
        self.assertAlmostEqual(curve[0]["start_loss"], 0.693147, places=5, msg="training starts from zero parameters")

        for round_report in self.report["rounds"]["training"]:
            for result in round_report["results"]:
                self.assertLessEqual(result["update_norm"], CLIP_BOUND)
        # Round 1 starts from zero, so every site's raw update is larger than the
        # bound: the clip applies, and the audit record shows it.
        first = self.report["rounds"]["training"][0]
        for result in first["results"]:
            self.assertAlmostEqual(CLIP_BOUND, result["update_norm"], places=9)
        for site_id in SITE_IDS:
            sends = [
                record
                for record in self.demo.hospital(site_id).audit_trail(limit=500)["records"]
                if record["boundary"] == "before_send" and record["correlation_id"] == first["round_id"]
            ]
            self.assertEqual(1, len(sends), site_id)
            self.assertEqual([f"clip_contribution:{CLIP_BOUND:g}"], sends[0]["contribution_strategies"])
            self.assertGreater(sends[0]["contribution_norm_before"], CLIP_BOUND)
            self.assertLessEqual(sends[0]["contribution_norm_after"], CLIP_BOUND)

    def test_each_training_round_starts_from_the_previous_version(self) -> None:
        training = self.report["rounds"]["training"]
        self.assertEqual(INITIAL_VERSION, training[0]["model_version"])
        for earlier, later in zip(training, training[1:], strict=False):
            self.assertEqual(earlier["candidate_version"], later["model_version"])
        for round_report in training:
            model = DemoCase.stored_model(self.agents, round_report["model_version"])
            self.assertEqual(model.digest, round_report["model_artifact_digest"])
            self.assertEqual(stroke_model.parameter_digest(model.params), round_report["model_artifact_digest"])
            self.assertEqual(stroke_model.training_code_digest(), round_report["training_code_digest"])

        candidate = self.report["candidate"]
        validation = self.report["rounds"]["candidate_validation"]
        self.assertEqual(training[-1]["candidate_version"], candidate["candidate_version"])
        self.assertEqual(candidate["candidate_version"], validation["model_version"])
        stored = DemoCase.stored_model(self.agents, candidate["candidate_version"])
        self.assertEqual(stored.digest, candidate["artifact_digest"])
        self.assertEqual(stroke_model.parameter_digest(stored.params), candidate["artifact_digest"])
        self.assertEqual(training[-1]["model_version"], candidate["parent_version"])
        self.assertEqual(f"registry://models/{candidate['candidate_version']}", candidate["artifact_uri"])

    def test_the_seed_rounds_name_the_rule_and_its_digest(self) -> None:
        for name in ("analytics", "evaluation"):
            round_report = self.report["rounds"][name]
            self.assertEqual(SEED_VERSION, round_report["model_version"])
            self.assertEqual(stroke_rule.rule_digest(), round_report["model_artifact_digest"])
            self.assertEqual(stroke_rule.rule_digest(), round_report["training_code_digest"])
            self.assertEqual(POINTS_RULE.code_digest(), round_report["training_code_digest"])

    def test_the_baseline_is_the_size_weighted_site_metrics_of_the_model_in_use(self) -> None:
        evaluation = self.report["rounds"]["evaluation"]
        weights = evaluation["readiness"]["site_weights"]
        total = sum(SITE_SIZES.values())
        for site_id, weight in weights.items():
            self.assertAlmostEqual(SITE_SIZES[site_id] / total, weight, places=9)
        by_site = {result["site_id"]: result["metrics"] for result in evaluation["results"]}
        for metric in ("auc", "sensitivity", "alerts_per_1000", "subgroup_auc_gap"):
            expected = sum(weights[site] * by_site[site][metric] for site in weights)
            self.assertAlmostEqual(expected, self.report["baseline_metrics"][metric], places=9)

    def test_the_release_decision_follows_from_the_gate_results(self) -> None:
        """Each gate's outcome matches its numbers, and the action matches the outcomes."""
        gates = self.report["release"]["gates"]
        candidate = self.report["candidate"]["metrics"]
        baseline = self.report["baseline_metrics"]
        by_id = {gate.gate_id: gate for gate in STROKE_RELEASE_GATES}
        self.assertEqual(sorted(by_id), sorted(result["gate_id"] for result in gates["gate_results"]))
        for result in gates["gate_results"]:
            gate = by_id[result["gate_id"]]
            observed = candidate[gate.metric]
            self.assertAlmostEqual(observed, result["observed"], places=9)
            comparison = gate.comparison
            if comparison.kind == "at_least":
                passed = observed >= comparison.value
            elif comparison.kind == "at_most":
                passed = observed <= comparison.value
            else:
                passed = observed >= baseline[gate.metric] + comparison.margin
            self.assertEqual("passed" if passed else "failed", result["outcome"], result["detail"])

        blocking = [r for r in gates["gate_results"] if by_id[r["gate_id"]].blocking and r["outcome"] != "passed"]
        if blocking:
            expected = "reject"
        elif any(r["outcome"] != "passed" for r in gates["gate_results"]):
            expected = "another_round"
        else:
            expected = "release"
        self.assertEqual(expected, gates["action"])
        self.assertEqual(len(blocking), len(gates["blocking_failures"]))
        self.assertEqual(SEED_VERSION, gates["rollback_version"])
        guard = self.report["release"]["guard"]
        self.assertEqual("before_release", guard["boundary"])
        status = "candidate" if gates["action"] == "release" and guard["permitted"] else "rejected"
        self.assertEqual(status, self.report["candidate"]["status"])
        gma = self.demo.agents.gma()
        self.assertEqual(status, gma.get_model_version(self.report["candidate"]["candidate_version"]).status)
        # The GMA read the metrics and the baseline from the rounds its engine collected.
        self.assertEqual("rounds", self.report["release"]["metrics_source"])
        self.assertEqual(SEED_VERSION, self.report["release"]["baseline_version"])

    def test_the_model_in_use_does_not_change_without_an_approval(self) -> None:
        self.assertEqual(SEED_VERSION, self.demo.overview.model_in_use()["version"])
        statuses = {model["version"]: model["status"] for model in self.demo.overview.models(50)["models"]}
        for round_report in self.report["rounds"]["training"][:-1]:
            self.assertEqual("superseded", statuses[round_report["candidate_version"]])
        self.assertEqual("superseded", statuses[INITIAL_VERSION])

    def test_every_audit_chain_verifies_and_records_each_boundary(self) -> None:
        rounds = len(_rounds(self.report))
        for site_id in SITE_IDS:
            audit = self.demo.hospital(site_id).audit_trail(limit=500)
            self.assertTrue(audit["chain_intact"], site_id)
            boundaries = [record["boundary"] for record in audit["records"]]
            self.assertEqual(rounds * 3, len(boundaries), "three Guard decisions per round")
            self.assertEqual({"before_train", "before_read", "before_send"}, set(boundaries))
        self.assertTrue(self.report["coordinator_audit"]["chain_intact"])

    def test_the_report_shows_each_audit_chain_as_a_summary_without_records(self) -> None:
        """Each hospital's audit chain stays at the hospital; the coordinator sees whether it verifies and its size."""
        self.assertEqual(SITE_IDS, list(self.report["site_audits"]))
        rounds = len(_rounds(self.report))
        for site_id, summary in self.report["site_audits"].items():
            self.assertEqual({"site_id", "chain_intact", "record_count"}, set(summary), site_id)
            self.assertEqual(site_id, summary["site_id"])
            self.assertIs(True, summary["chain_intact"])
            self.assertEqual(rounds * 3, summary["record_count"], "the count is not capped at a page of records")
        self.assertEqual({"chain_intact", "record_count"}, set(self.report["coordinator_audit"]))
        self.assertEqual(1, self.report["coordinator_audit"]["record_count"], "one release check")
        text = json.dumps(self.report)
        for site_id in SITE_IDS:
            for record in self.demo.hospital(site_id).audit_trail(limit=500)["records"]:
                self.assertNotIn(record["audit_id"], text, f"an audit record of {site_id} is in the report")

    def test_each_hospital_records_what_it_sent_in_every_round(self) -> None:
        round_ids = [round_report["round_id"] for round_report in _rounds(self.report)]
        for site_id in SITE_IDS:
            sent = [entry for entry in self.demo.hospital(site_id).lma().job_log(50) if entry.round_id in round_ids]
            self.assertEqual(sorted(round_ids), sorted(entry.round_id for entry in sent), site_id)
            for entry in sent:
                self.assertEqual("completed", entry.participation)
                # The job log keeps counts, metrics, a norm and a digest: no record and no update.
                self.assertEqual(set(), RECORD_FIELDS & set(entry.metrics), site_id)
                if entry.phase == "training":
                    self.assertLessEqual(entry.update_norm, CLIP_BOUND)
                    self.assertRegex(entry.update_digest or "", r"^sha256:[0-9a-f]{64}$")

    def test_the_guard_costs_a_little_accuracy(self) -> None:
        """The before-read Guard rounds hours and blood pressure to the nearest 10.

        Each hospital's LMA hands its runner the Guard's payload, not its raw
        records. The candidate scores those guarded values a little differently
        from the exact ones. The cost is measured per site, on the records the
        LMA gave its Guard in the candidate validation round: it is above zero
        at some site and below 0.01 AUC at every site.
        """
        validation_id = self.report["rounds"]["candidate_validation"]["round_id"]
        candidate = DemoCase.stored_model(self.agents, self.report["candidate"]["candidate_version"])

        def score(record: dict) -> float | None:
            return LOGISTIC_REGRESSION.score(candidate.params, record)

        differences = []
        for site_id in SITE_IDS:
            [(_, records, decision)] = [entry for entry in self.guarded[site_id] if entry[0] == validation_id]
            self.assertEqual(len(records), len(decision.payload), site_id)
            changed = sum(
                1
                for raw, guarded in zip(records, decision.payload, strict=True)
                if raw.get("hours_since_onset") != guarded.get("hours_since_onset")
            )
            self.assertGreater(changed, 0, "the Guard did not generalize hours since onset")
            raw_auc = stroke_rule.evaluate_against_labels(records, score_fn=score)["auc"]
            guarded_auc = stroke_rule.evaluate_against_labels(decision.payload, score_fn=score)["auc"]
            differences.append(abs(raw_auc - guarded_auc))
        self.assertGreater(max(differences), 0.0)
        self.assertLess(max(differences), 0.01)

    def test_no_update_stays_in_the_gma(self) -> None:
        self.assertEqual([], self.agents["gma"].state.updates.pending_rounds())


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class RoundTests(DemoCase):
    """Rounds that change one condition at a time."""

    def test_a_site_on_a_stale_feature_contract_is_excluded(self) -> None:
        """Matching field names are not a matching contract."""
        demo = self.demo()
        demo.ready()
        gma = demo.agents.gma()
        stale = {site.site_id: site for site in gma.refresh_sites()}["ch-245"]
        gma.register_site(stale.model_copy(update={"feature_contract_version": "stroke-triage-features-v2"}))
        result = demo.pilot.run_round("r-stale", "analytics", _ref(gma.released_model()))
        self.assertNotIn("ch-245", result["plan"]["selected_sites"])
        excluded = {entry["site_id"]: entry["reason"] for entry in result["plan"]["excluded_sites"]}
        self.assertIn("feature contract", excluded["ch-245"])
        self.assertNotIn("ch-245", [call["site_id"] for call in result["dispatch"]], "an excluded site gets no job")
        manifest = demo.pilot.manifest("r-next", "analytics", _ref(gma.released_model()))
        self.assertEqual(STROKE_FEATURE_CONTRACT_VERSION, manifest.feature_contract_version)

    def test_a_site_below_the_cohort_floor_is_not_selected(self) -> None:
        site, store = tiny_site(5)
        demo = self.demo(sites=[*SITES, site], site_stores={site.site_id: store})
        demo.ready()
        gma = demo.agents.gma()
        gma.refresh_sites()
        result = demo.pilot.run_round("r-floor", "analytics", _ref(gma.released_model()))
        self.assertNotIn(site.site_id, result["plan"]["selected_sites"])
        excluded = {entry["site_id"]: entry["reason"] for entry in result["plan"]["excluded_sites"]}
        self.assertIn(site.site_id, excluded)
        self.assertEqual(sorted(SITE_SIZES), sorted(result["plan"]["selected_sites"]))
        summary = demo.hospital(site.site_id).summary()
        self.assertEqual(5, summary["record_count"])
        self.assertIsNone(summary["outcome_rate"], "an outcome rate over 5 records must not be reported")

    def test_a_short_consortium_has_no_quorum(self) -> None:
        demo = self.demo(sites=SITES[:2], secure_aggregation_threshold=3)
        demo.ready()
        gma = demo.agents.gma()
        gma.refresh_sites()
        result = demo.pilot.run_round("r-short", "analytics", _ref(gma.released_model()))
        self.assertFalse(result["plan"]["quorum_met"])
        self.assertEqual(3, result["plan"]["required_participants"])
        self.assertEqual("closed", result["status"])
        self.assertEqual([], result["results"])
        self.assertIsNone(result["readiness"])
        self.assertEqual([], result["dispatch"], "a round without quorum sends no job")

    def test_a_short_consortium_pilot_stops_without_a_candidate(self) -> None:
        report = self.demo(sites=SITES[:2]).pilot.run_pilot("short", training_rounds=2)
        self.assertIsNone(report["candidate"])
        self.assertIsNone(report["release"])
        self.assertEqual({}, report["baseline_metrics"])
        self.assertIn("training round 1", report["stop_reason"])

    def test_a_model_whose_parameters_changed_reaches_no_hospital(self) -> None:
        """The GMA checks the stored parameters against their digest before it sends a job.

        Each LMA also checks the digest of the model it receives
        (``agents/tests/test_lma_federation_jobs.py``).
        """
        demo, agents = self.demo_and_agents()
        gma = demo.agents.gma()
        gma.refresh_sites()
        initial = _ref(gma.get_model_version(INITIAL_VERSION))
        audits = {site_id: len(demo.hospital(site_id).audit_trail(500)["records"]) for site_id in SITE_IDS}
        # Change the stored parameters behind the registry's back.
        store = agents["gma"].state.models.store
        with store._lock:  # noqa: SLF001 - the test tampers with the stored row on purpose
            store._rows[INITIAL_VERSION]["params"]["intercept"] = 0.5  # noqa: SLF001
        with self.assertRaises(AgentError) as refused:
            demo.pilot.run_round("r-tampered", "training", initial)
        self.assertEqual(409, refused.exception.status)
        self.assertIn("digest", refused.exception.detail)
        for site_id in SITE_IDS:
            hospital = demo.hospital(site_id)
            self.assertNotIn("r-tampered", [entry.round_id for entry in hospital.lma().job_log(50)])
            self.assertEqual(audits[site_id], len(hospital.audit_trail(500)["records"]), "no Guard ran for it")
        self.assertEqual([], agents["gma"].state.updates.pending_rounds())

    def test_hospitals_refuse_a_manifest_with_the_wrong_code_digest(self) -> None:
        demo = self.demo()
        demo.ready()
        gma = demo.agents.gma()
        gma.refresh_sites()
        manifest = demo.pilot.manifest("r-code", "evaluation", _ref(gma.released_model()))
        self.assertEqual(POINTS_RULE.code_digest(), manifest.training_code_digest)
        wrong = manifest.model_copy(update={"training_code_digest": stroke_model.training_code_digest()})
        record = gma.dispatch_round(wrong)
        self.assertEqual(sorted(SITE_IDS), sorted(acceptance.site_id for acceptance in record.acceptances))
        for acceptance in record.acceptances:
            self.assertFalse(acceptance.accepted)
            self.assertIn("code digest", acceptance.reason)
        self.assertEqual({"rejected"}, {result.participation for result in record.results})
        for site_id in SITE_IDS:
            [entry] = demo.hospital(site_id).lma().job_log(1)
            self.assertEqual(("r-code", "rejected"), (entry.round_id, entry.participation))
            self.assertIn("code digest", entry.reason)

    def test_a_pilot_calls_the_gma_a_fixed_number_of_times(self) -> None:
        """The study operator makes one set of GMA calls per round, and no call that repeats work.

        The app no longer reads model versions itself; the GMA's model store
        reads during a pilot are bounded in
        :meth:`test_a_pilot_reads_each_model_version_a_bounded_number_of_times`.
        A dispatched round carries its readiness, so the app asks for it no
        second time, and an aggregated training round's updates are already
        deleted, so the app discards only a training round that was not
        aggregated.
        """
        from app.services.agents import FrameworkCalls

        demo, agents = self.demo_and_agents()
        rounds = 3
        with FrameworkCalls() as calls:
            report = demo.pilot.run_pilot("calls", training_rounds=rounds)
        self.assertIsNotNone(report["candidate"])
        app_calls = [(c["method"], c["path"]) for c in calls.entries() if c["via"] == "app"]
        self.assertEqual({"gma"}, {c["service"] for c in calls.entries() if c["via"] == "app"})
        counts = {key: app_calls.count(key) for key in set(app_calls)}
        rounds_run = 3 + rounds  # analytics, evaluation, training, candidate validation
        self.assertEqual(
            {
                ("POST", "/api/v1/federation/sites:refresh"): 1,
                ("GET", "/api/v1/model-versions:released"): 1,
                ("GET", "/api/v1/health"): 1,
                ("POST", "/api/v1/federation/rounds"): rounds_run,
                ("POST", "/api/v1/federation/rounds/{round_id}:aggregate"): rounds_run - 1,
                ("GET", "/api/v1/model-versions/{version}"): 1,
                ("POST", "/api/v1/federation/rounds/{round_id}:evaluate-release"): 1,
                ("GET", "/api/v1/federation/sites/summaries"): 1,
                ("GET", "/api/v1/governance/audit:summary"): 1,
            },
            counts,
        )
        # Aggregation deleted every training round's updates: none stays behind.
        self.assertEqual([], agents["gma"].state.updates.pending_rounds())

    def test_a_pilot_reads_each_model_version_a_bounded_number_of_times(self) -> None:
        """The GMA reads each model version from its store a fixed number of times per pilot.

        On Postgres each read is one database operation, so a read per
        hospital, or per round for the same version, would add to every
        pilot. None of the ceilings depends on the number of hospitals. Before the agents, the app read each version at most once per
        pilot from its own copy. The GMA reads more often, because each
        lifecycle step must see a version's current status in the store; the
        ceilings below name each read. Parameters loaded for a round's jobs
        are kept, so a version's parameters are read once per GMA start.
        """
        demo, agents = self.demo_and_agents()
        demo.pilot.run_pilot("warm", training_rounds=1)  # loads the starting model's parameters once
        store = agents["gma"].state.models.store
        reads: dict[str, int] = {}
        real_get, real_released = store.get, store.released

        def counting_get(version: str) -> Any:
            reads[version] = reads.get(version, 0) + 1
            return real_get(version)

        def counting_released() -> Any:
            reads["released()"] = reads.get("released()", 0) + 1
            return real_released()

        rounds = 3
        with mock.patch.object(store, "get", side_effect=counting_get), mock.patch.object(
            store, "released", side_effect=counting_released
        ):
            report = demo.pilot.run_pilot("reads", training_rounds=rounds)
        trained = [point["candidate_version"] for point in report["training_curve"]]
        self.assertEqual(rounds, len(trained))
        ceilings = {
            # The pilot asks once for the model in use.
            "released()": 1,
            # The app asks for the starting version once, and registering the
            # first candidate reads it as the parent.
            INITIAL_VERSION: 2,
        }
        for version in trained[:-1]:
            # Registering it checks that the name is free and reads it back;
            # the next round's jobs load its parameters; registering the next
            # candidate reads it as the parent.
            ceilings[version] = 4
        # The last candidate is registered (2 reads) and loaded for
        # validation (1). The release evaluation reads it once, and storing
        # the decision reads its current status and reads it back (3).
        ceilings[trained[-1]] = 6
        over = {version: count for version, count in reads.items() if count > ceilings.get(version, 0)}
        self.assertEqual({}, over, f"reads per version: {reads}; ceilings: {ceilings}")
        self.assertLessEqual(sum(reads.values()), sum(ceilings.values()))

    def test_two_pilots_with_the_same_prefix_use_different_round_ids(self) -> None:
        demo = self.demo()
        first = demo.pilot.run_pilot("same", training_rounds=1)
        second = demo.pilot.run_pilot("same", training_rounds=1)
        self.assertNotEqual(first["pilot_id"], second["pilot_id"])
        self.assertNotEqual(first["candidate"]["candidate_version"], second["candidate"]["candidate_version"])
        self.assertEqual(second, demo.overview.last_pilot())
        with self.assertRaises(ValueError):
            demo.pilot.run_pilot("not a prefix")


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class CoordinatorBoundaryTests(DemoCase):
    """The coordinator works from what hospitals report, never from their rows."""

    #: What coordinator code may use of a hospital view: who the hospital is,
    #: its count of entered records and where its store is. Everything else on
    #: a view is its own staff's view of its own records.
    COORDINATOR_FACING = {"site", "site_id", "display_name", "country", "counts", "store"}

    #: What coordinator code may use of a hospital's store: its kind and location, never its rows.
    STORE_FACING = {"kind", "location"}

    def test_the_study_operator_is_given_no_hospital_view_or_store(self) -> None:
        parameters = set(inspect.signature(PilotService).parameters)
        self.assertEqual(
            {"sites", "agents", "coordinator_store", "secure_aggregation_threshold", "on_report", "note_dispatch",
             "models_ready"},
            parameters,
        )

    def test_the_pilot_and_overview_run_with_only_the_coordinator_facing_parts(self) -> None:
        demo = self.demo()
        demo.ready()
        allowed, store_allowed = self.COORDINATOR_FACING, self.STORE_FACING

        class StoreView:
            def __init__(self, store):
                self._store = store

            def __getattr__(self, name):
                if name not in store_allowed:
                    raise AssertionError(f"coordinator code used the store's {name}")
                return getattr(self._store, name)

        class CoordinatorView:
            def __init__(self, hospital):
                self._hospital = hospital

            def __getattr__(self, name):
                if name not in allowed:
                    raise AssertionError(f"coordinator code used HospitalView.{name}")
                if name == "store":
                    return StoreView(self._hospital.store)
                return getattr(self._hospital, name)

        views = {site_id: CoordinatorView(view) for site_id, view in demo.views.items()}
        overview = StudyOverview(demo.sites, views, demo.agents, demo.coordinator_store, models_ready=demo.ready)
        pilot = PilotService(demo.sites, demo.agents, demo.coordinator_store, models_ready=demo.ready)
        self.assertEqual(3, len(overview.overview()["hospitals"]))
        self.assertTrue(overview.status()["ready"])
        report = pilot.run_pilot("boundary", training_rounds=2)
        self.assertIsNotNone(report["candidate"])

    def test_only_hospital_code_reads_a_store(self) -> None:
        """The hospital's LMA reads its store through the site source; the coordinator's code never does."""
        demo = self.demo()
        callers: set[str] = set()
        for store in demo.stores.values():
            original = store.records

            def spy(original=original):
                callers.add(Path(inspect.stack()[1].filename).name)
                return original()

            store.records = spy  # type: ignore[method-assign]
        demo.overview.overview()
        demo.pilot.run_pilot("spy", training_rounds=1)
        self.assertIn("site_source.py", callers, "the LMAs read the stores")
        self.assertLessEqual(callers, {"site_source.py", "hospital_views.py"})


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class ApprovalTests(DemoCase):
    """A candidate becomes the model in use only after its gates pass and a person approves it."""

    def test_a_rejected_candidate_cannot_be_approved(self) -> None:
        demo = self.demo(extension=gated_extension(IMPOSSIBLE_GATES))
        report = demo.pilot.run_pilot("approve-reject", training_rounds=1)
        gates = report["release"]["gates"]
        version = report["candidate"]["candidate_version"]
        self.assertEqual("reject", gates["action"])
        decision = demo.agents.gma().get_model_version(version).release_decision
        self.assertEqual(gates["action"], decision.action)
        self.assertEqual(gates["gate_results"], [result.model_dump(mode="json") for result in decision.gate_results])
        with self.assertRaises(ApprovalRefused):
            demo.overview.approve(version)
        self.assertEqual(SEED_VERSION, demo.overview.model_in_use()["version"])

    def test_a_version_without_a_release_decision_cannot_be_approved(self) -> None:
        demo = self.demo()
        report = demo.pilot.run_pilot("approve-undecided", training_rounds=2)
        intermediate = report["training_curve"][0]["candidate_version"]
        gma = demo.agents.gma()
        for version in (INITIAL_VERSION, intermediate):
            with self.subTest(version=version):
                self.assertIsNone(gma.get_model_version(version).release_decision)
                with self.assertRaises(ApprovalRefused):
                    demo.overview.approve(version)

    # An approval after a restart needs the model registry in the database:
    # test_pilot_postgres.PostgresApprovalAfterRestartTests.

    def test_an_approved_candidate_becomes_the_model_hospitals_use(self) -> None:
        """With one gate the candidate clears, the planner decides release; approval releases it."""
        demo = self.demo(extension=gated_extension(LENIENT_GATES))
        report = demo.pilot.run_pilot("approve", training_rounds=2)
        version = report["candidate"]["candidate_version"]
        self.assertEqual("release", report["release"]["gates"]["action"])
        self.assertEqual("candidate", report["candidate"]["status"])
        self.assertEqual(SEED_VERSION, demo.overview.model_in_use()["version"], "a passing candidate is not yet released")

        approved = demo.overview.approve(version)
        self.assertEqual("released", approved["released"]["status"])
        self.assertEqual(SEED_VERSION, approved["previous"])
        self.assertEqual(version, demo.overview.model_in_use()["version"])
        statuses = {model["version"]: model["status"] for model in demo.overview.models(50)["models"]}
        self.assertEqual("superseded", statuses[SEED_VERSION])

        hospital = demo.hospital("it-002")
        [top] = hospital.worklist(limit=1)
        self.assertEqual(version, top["assessment"]["model_version"])
        self.assertEqual("logistic_regression", top["assessment"]["model_kind"])
        self.assertEqual(version, hospital.dashboard()["current_model"]["version"])

        # The next pilot measures the released model and names its digests.
        gma = demo.agents.gma()
        gma.refresh_sites()
        evaluation = demo.pilot.run_round("after-approval", "evaluation", _ref(gma.released_model()))
        self.assertEqual(version, evaluation["model_version"])
        self.assertEqual(stroke_model.training_code_digest(), evaluation["training_code_digest"])
        self.assertTrue(evaluation["readiness"]["aggregation_permitted"])

        with self.assertRaises(ApprovalRefused):
            demo.overview.approve(version)


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class RestartTests(DemoCase):
    """What a restarted API finds in the coordinator store: the last pilot and the release audit chain.

    In memory, the restarted services share the first ones' coordinator store,
    as a restarted API shares the database. The model registry lives in the
    GMA, and in the database only when there is one.
    """

    def test_a_new_process_shows_the_last_pilots_charts(self) -> None:
        demo = self.demo()
        report = demo.pilot.run_pilot("restart-charts", training_rounds=1)
        restarted = self.demo(coordinator_store=demo.coordinator_store)
        self.assertEqual(report, restarted.overview.last_pilot())
        charts = {chart["chart_id"]: chart for chart in ChartService().consortium_list(restarted.overview)}
        for chart_id in ("site-auc", "training-curve", "release-gates"):
            self.assertTrue(charts[chart_id]["available"], charts[chart_id]["reason"])

    def test_the_newest_pilot_is_the_one_shown(self) -> None:
        demo = self.demo()
        demo.pilot.run_pilot("restart-first", training_rounds=1)
        second = demo.pilot.run_pilot("restart-second", training_rounds=1)
        restarted = self.demo(coordinator_store=demo.coordinator_store)
        self.assertEqual(second["pilot_id"], restarted.overview.last_pilot()["pilot_id"])

    def test_the_release_audit_chain_survives_a_restart(self) -> None:
        demo = self.demo()
        report = demo.pilot.run_pilot("restart-audit", training_rounds=1)
        restarted = self.demo(coordinator_store=demo.coordinator_store)
        audit = restarted.agents.gma().request("GET", "/api/v1/governance/audit", params={"limit": 50}).body
        self.assertEqual(["before_release"], [record["boundary"] for record in audit["records"]])
        self.assertEqual(report["release"]["guard"]["audit_id"], audit["records"][0]["audit_id"])
        self.assertTrue(audit["chain_intact"])


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class StaleApprovalTests(DemoCase):
    """A candidate is approved only against the model it was compared with."""

    def test_a_candidate_checked_against_an_older_model_cannot_be_approved(self) -> None:
        demo = self.demo(extension=gated_extension(LENIENT_GATES))
        first = demo.pilot.run_pilot("stale-a", training_rounds=1)
        second = demo.pilot.run_pilot("stale-b", training_rounds=1)
        older = first["candidate"]["candidate_version"]
        newer = second["candidate"]["candidate_version"]
        gma = demo.agents.gma()
        for version in (older, newer):
            self.assertEqual(SEED_VERSION, gma.get_model_version(version).release_decision.baseline_version)
        demo.overview.approve(newer)
        with self.assertRaises(ApprovalRefused) as raised:
            demo.overview.approve(older)
        self.assertIn(newer, str(raised.exception))
        self.assertEqual(newer, demo.overview.model_in_use()["version"])

    def test_a_decision_without_a_baseline_cannot_be_approved(self) -> None:
        demo, agents = self.demo_and_agents(extension=gated_extension(LENIENT_GATES))
        report = demo.pilot.run_pilot("no-baseline", training_rounds=1)
        version = report["candidate"]["candidate_version"]
        store = agents["gma"].state.models.store
        with store._lock:  # noqa: SLF001 - the test removes the stored baseline on purpose
            decision = store._rows[version]["release_decision"]  # noqa: SLF001
            self.assertEqual(SEED_VERSION, decision.pop("baseline_version"))
        self.assertIsNone(demo.agents.gma().get_model_version(version).release_decision.baseline_version)
        with self.assertRaises(ApprovalRefused):
            demo.overview.approve(version)
        self.assertEqual(SEED_VERSION, demo.overview.model_in_use()["version"])


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class AbandonedRoundTests(DemoCase):
    """A training round that cannot be aggregated leaves no update in the GMA."""

    def test_updates_are_deleted_when_a_round_is_not_aggregated(self) -> None:
        demo, agents = self.demo_and_agents()
        governance = agents["lma:ch-245"].state.site.governance
        real = governance.enforce

        def deny_training_sends(request, payload=None, *, correlation_id=None):
            if request.boundary == "before_send" and "-training-" in (correlation_id or ""):
                request = request.model_copy(update={"declared_purpose": "marketing"})
            return real(request, payload, correlation_id=correlation_id)

        with mock.patch.object(governance, "enforce", side_effect=deny_training_sends):
            report = demo.pilot.run_pilot("abandoned", training_rounds=2)
        self.assertIn("training round 1 could not be aggregated", report["stop_reason"])
        # The other two hospitals sent their clipped updates; none may stay behind.
        sent = [result for result in report["rounds"]["training"][0]["results"] if result["update_digest"]]
        self.assertEqual(2, len(sent))
        self.assertEqual([], agents["gma"].state.updates.pending_rounds())

    def test_updates_are_deleted_when_a_round_fails_part_way(self) -> None:
        demo, agents = self.demo_and_agents()
        rounds = agents["gma"].state.rounds
        real = rounds.evaluate_readiness

        def fail_training(round_id, *args, **kwargs):
            if "-training-" in round_id:
                raise RuntimeError("the planner stopped answering")
            return real(round_id, *args, **kwargs)

        with mock.patch.object(rounds, "evaluate_readiness", side_effect=fail_training):
            # The GMA logs its own failure; the app gets a 500 and gives up the pilot.
            with self.assertRaises(AgentError) as failed, self.assertLogs(level="ERROR"):
                demo.pilot.run_pilot("failing", training_rounds=1)
        self.assertEqual(500, failed.exception.status)
        self.assertEqual([], agents["gma"].state.updates.pending_rounds())


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class ChangedUpdateTests(DemoCase):
    """A hospital checks that the GMA stored the update it sent."""

    def test_a_gma_that_changes_an_update_gets_no_contribution(self) -> None:
        demo, agents = self.demo_and_agents()
        updates = agents["gma"].state.updates
        real = updates.put

        def doubling(record, site_id, upload, kind):
            changed = upload.model_copy(update={"update": {name: value * 2 for name, value in upload.update.items()}})
            return real(record, site_id, changed, kind)

        with mock.patch.object(updates, "put", side_effect=doubling):
            report = demo.pilot.run_pilot("doubled", training_rounds=1)
        first = report["rounds"]["training"][0]
        self.assertEqual({"rejected"}, {result["participation"] for result in first["results"]})
        self.assertIsNone(report["candidate"])
        for site_id in SITE_IDS:
            [entry] = [e for e in demo.hospital(site_id).lma().job_log(50) if e.round_id == first["round_id"]]
            self.assertIn("different update", entry.reason or "")
        self.assertEqual([], updates.pending_rounds())


if __name__ == "__main__":
    unittest.main()
