"""End-to-end tests of the governed federated pilot on the IST hospitals.

These tests check that the app gets its guarantees from the framework and the
runtime, not from its own claims: a hospital can refuse a job, nothing at the
level of a patient leaves a hospital, model updates are clipped before they are
averaged, the coordinator works only from what hospitals report, and the
release decision follows from the gate results.

They use the in-memory stores loaded from the committed extract, and the real
planner. ``test_pilot_postgres.py`` runs the pilot against Postgres.
"""

from __future__ import annotations

import inspect
import json
import re
import unittest
from pathlib import Path
from unittest import mock

from agents.common.domain.federation import FederatedJob, GateComparison, ReleaseGate

from app.domain import stroke_model, stroke_rule
from app.domain.conditions import STROKE_FEATURE_CONTRACT, STROKE_FEATURE_CONTRACT_VERSION, STROKE_RELEASE_GATES
from app.domain.dataset import SITES
from app.services.consortium import ApprovalRefused, Consortium
from app.services.runtime import INITIAL_VERSION, SEED_VERSION, code_digest

from tests.support import dagentsc_available, tiny_site

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

#: One gate the federated candidate clears, so the planner decides ``release``.
LENIENT_GATES = [
    ReleaseGate(gate_id="discrimination", metric="auc", comparison=GateComparison(kind="at_least", value=0.5))
]


def _rounds(report: dict) -> list[dict]:
    rounds = report["rounds"]
    return [rounds["analytics"], rounds["evaluation"], *rounds["training"], rounds["candidate_validation"]]


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class PilotTests(unittest.TestCase):
    """One pilot, run once, and the rounds it produced."""

    consortium: Consortium
    report: dict

    @classmethod
    def setUpClass(cls) -> None:
        cls.consortium = Consortium()
        cls.report = cls.consortium.run_pilot("test")

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
        for site_id, hospital in self.consortium.hospitals.items():
            sends = [
                record
                for record in hospital.audit_trail(limit=500)["records"]
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
        registry = self.consortium.registry
        for round_report in training:
            model = registry.get(round_report["model_version"])
            self.assertEqual(model.digest, round_report["model_artifact_digest"])
            self.assertEqual(stroke_model.parameter_digest(model.params), round_report["model_artifact_digest"])
            self.assertEqual(stroke_model.training_code_digest(), round_report["training_code_digest"])

        candidate = self.report["candidate"]
        validation = self.report["rounds"]["candidate_validation"]
        self.assertEqual(training[-1]["candidate_version"], candidate["candidate_version"])
        self.assertEqual(candidate["candidate_version"], validation["model_version"])
        stored = registry.get(candidate["candidate_version"])
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
        self.assertEqual(status, self.consortium.registry.get(self.report["candidate"]["candidate_version"]).status)

    def test_the_model_in_use_does_not_change_without_an_approval(self) -> None:
        self.assertEqual(SEED_VERSION, self.consortium.model_in_use()["version"])
        statuses = {model["version"]: model["status"] for model in self.consortium.models()}
        for round_report in self.report["rounds"]["training"][:-1]:
            self.assertEqual("superseded", statuses[round_report["candidate_version"]])
        self.assertEqual("superseded", statuses[INITIAL_VERSION])

    def test_every_audit_chain_verifies_and_records_each_boundary(self) -> None:
        rounds = len(_rounds(self.report))
        for site_id, hospital in self.consortium.hospitals.items():
            audit = hospital.audit_trail(limit=500)
            self.assertTrue(audit["chain_intact"], site_id)
            boundaries = [record["boundary"] for record in audit["records"]]
            self.assertEqual(rounds * 3, len(boundaries), "three Guard decisions per round")
            self.assertEqual({"before_train", "before_read", "before_send"}, set(boundaries))
        self.assertTrue(self.report["coordinator_audit"]["chain_intact"])

    def test_the_report_shows_each_audit_chain_as_a_summary_without_records(self) -> None:
        """Each hospital's audit chain stays at the hospital; the coordinator sees whether it verifies and its size."""
        self.assertEqual([site.site_id for site in SITES], list(self.report["site_audits"]))
        rounds = len(_rounds(self.report))
        for site_id, summary in self.report["site_audits"].items():
            self.assertEqual({"site_id", "chain_intact", "record_count"}, set(summary), site_id)
            self.assertEqual(site_id, summary["site_id"])
            self.assertIs(True, summary["chain_intact"])
            self.assertEqual(rounds * 3, summary["record_count"], "the count is not capped at a page of records")
        self.assertEqual({"chain_intact", "record_count"}, set(self.report["coordinator_audit"]))
        self.assertEqual(1, self.report["coordinator_audit"]["record_count"], "one release check")
        text = json.dumps(self.report)
        for site_id, hospital in self.consortium.hospitals.items():
            for record in hospital.audit_trail(limit=500)["records"]:
                self.assertNotIn(record["audit_id"], text, f"an audit record of {site_id} is in the report")

    def test_each_hospital_records_what_it_sent_in_every_round(self) -> None:
        round_ids = [round_report["round_id"] for round_report in _rounds(self.report)]
        for site_id, hospital in self.consortium.hospitals.items():
            sent = [entry for entry in hospital.contributions(limit=50) if entry["round_id"] in round_ids]
            self.assertEqual(sorted(round_ids), sorted(entry["round_id"] for entry in sent), site_id)
            for entry in sent:
                self.assertEqual("completed", entry["participation"])
                if entry["phase"] == "training":
                    self.assertLessEqual(entry["update_norm"], CLIP_BOUND)
        stored = self.consortium.coordinator_store.contributions(round_ids[0])
        self.assertEqual(sorted(SITE_SIZES), sorted(entry["site_id"] for entry in stored))
        for entry in stored:
            self.assertEqual(set(), RECORD_FIELDS & set(entry["payload"]))

    def test_the_guard_costs_a_little_accuracy(self) -> None:
        """The before-read Guard rounds hours and blood pressure to the nearest 10.

        The candidate scores those guarded values a little differently from the
        exact ones. The cost is measured per site against the raw records: it
        is above zero at some site and below 0.01 AUC at every site.
        """
        candidate = self.consortium.registry.get(self.report["candidate"]["candidate_version"])
        manifest = self.consortium.manifest("guard-cost", "evaluation", candidate)
        differences = []
        for site_id, hospital in self.consortium.hospitals.items():
            records = [dict(record) for record in hospital._local_records()]
            job = FederatedJob(
                round_id="guard-cost", site_id=site_id, manifest=manifest, manifest_digest="x", task="evaluate"
            )
            worker = hospital.worker()._inner
            decision = hospital.governance.enforce(
                worker._request(job, "before_read", STROKE_FEATURE_CONTRACT.field_names(), "row", len(records)),
                records,
            )
            changed = sum(
                1
                for raw, guarded in zip(records, decision.payload, strict=True)
                if raw.get("hours_since_onset") != guarded.get("hours_since_onset")
            )
            self.assertGreater(changed, 0, "the Guard did not generalize hours since onset")
            raw_auc = stroke_rule.evaluate_against_labels(records, score_fn=candidate.score)["auc"]
            guarded_auc = stroke_rule.evaluate_against_labels(decision.payload, score_fn=candidate.score)["auc"]
            differences.append(abs(raw_auc - guarded_auc))
        self.assertGreater(max(differences), 0.0)
        self.assertLess(max(differences), 0.01)


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class RoundTests(unittest.TestCase):
    """Rounds that change one condition at a time."""

    def test_a_site_on_a_stale_feature_contract_is_excluded(self) -> None:
        """Matching field names are not a matching contract."""
        consortium = Consortium()
        stale = consortium.hospitals["ch-245"].registration()
        consortium.controller.register_site(
            stale.model_copy(update={"feature_contract_version": "stroke-triage-features-v2"})
        )
        result = consortium.run_round("r-stale", "analytics")
        self.assertNotIn("ch-245", result["plan"]["selected_sites"])
        excluded = {entry["site_id"]: entry["reason"] for entry in result["plan"]["excluded_sites"]}
        self.assertIn("feature contract", excluded["ch-245"])
        self.assertEqual(STROKE_FEATURE_CONTRACT_VERSION, consortium.manifest("r-next").feature_contract_version)

    def test_a_site_below_the_cohort_floor_is_not_selected(self) -> None:
        site, store = tiny_site(5)
        consortium = Consortium([*SITES, site], site_stores={site.site_id: store})
        result = consortium.run_round("r-floor", "analytics")
        self.assertNotIn(site.site_id, result["plan"]["selected_sites"])
        excluded = {entry["site_id"]: entry["reason"] for entry in result["plan"]["excluded_sites"]}
        self.assertIn(site.site_id, excluded)
        self.assertEqual(sorted(SITE_SIZES), sorted(result["plan"]["selected_sites"]))
        summary = consortium.hospital(site.site_id).summary()
        self.assertEqual(5, summary["record_count"])
        self.assertIsNone(summary["outcome_rate"], "an outcome rate over 5 records must not be reported")

    def test_a_short_consortium_has_no_quorum(self) -> None:
        consortium = Consortium(SITES[:2], secure_aggregation_threshold=3)
        result = consortium.run_round("r-short", "analytics")
        self.assertFalse(result["plan"]["quorum_met"])
        self.assertEqual(3, result["plan"]["required_participants"])
        self.assertEqual("closed", result["status"])
        self.assertEqual([], result["results"])
        self.assertIsNone(result["readiness"])

    def test_a_short_consortium_pilot_stops_without_a_candidate(self) -> None:
        report = Consortium(SITES[:2]).run_pilot("short", training_rounds=2)
        self.assertIsNone(report["candidate"])
        self.assertIsNone(report["release"])
        self.assertEqual({}, report["baseline_metrics"])
        self.assertIn("training round 1", report["stop_reason"])

    def test_hospitals_refuse_a_model_whose_parameters_changed(self) -> None:
        consortium = Consortium()
        initial = consortium.registry.ensure_initial()
        # Change the stored parameters behind the registry's back.
        stored = consortium.coordinator_store._models[INITIAL_VERSION]
        stored["params"]["intercept"] = 0.5
        result = consortium.run_round("r-tampered", "training", initial)
        self.assertEqual({"rejected"}, {item["participation"] for item in result["results"]})
        self.assertFalse(result["readiness"]["aggregation_permitted"])
        for hospital in consortium.hospitals.values():
            [entry] = hospital.contributions(limit=1)
            self.assertEqual("rejected", entry["participation"])
            self.assertIn("digest", entry["reason"])

    def test_hospitals_refuse_a_manifest_with_the_wrong_code_digest(self) -> None:
        consortium = Consortium()
        manifest = consortium.manifest("r-code", "evaluation")
        self.assertEqual(code_digest("points_rule"), manifest.training_code_digest)
        wrong = manifest.model_copy(update={"training_code_digest": stroke_model.training_code_digest()})
        reason = consortium.hospitals["uk-134"].verify_manifest(wrong)
        self.assertIn("code digest", reason)

    def test_a_pilot_reads_each_model_version_from_the_store_at_most_once(self) -> None:
        consortium = Consortium()
        store = consortium.coordinator_store
        reads: list[str] = []
        real = store.get_model

        def counting(version: str):
            reads.append(version)
            return real(version)

        with mock.patch.object(store, "get_model", side_effect=counting):
            report = consortium.run_pilot("reads", training_rounds=3)
            self.assertIsNotNone(report["candidate"])
            self.assertEqual([], [version for version in set(reads) if reads.count(version) > 1], reads)
            # Outside the pilot every load reads the store again.
            before = len(reads)
            consortium.run_round("after-reads", "evaluation")
            self.assertGreater(len(reads), before)

    def test_two_pilots_with_the_same_prefix_use_different_round_ids(self) -> None:
        consortium = Consortium()
        first = consortium.run_pilot("same", training_rounds=1)
        second = consortium.run_pilot("same", training_rounds=1)
        self.assertNotEqual(first["pilot_id"], second["pilot_id"])
        self.assertNotEqual(first["candidate"]["candidate_version"], second["candidate"]["candidate_version"])
        self.assertEqual(second, consortium.last_pilot())
        with self.assertRaises(ValueError):
            consortium.run_pilot("not a prefix")


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class CoordinatorBoundaryTests(unittest.TestCase):
    """The coordinator works from what hospitals report, never from their rows."""

    #: The hospital methods coordinator code may call. Everything else on a
    #: hospital is its own staff's view of its own records.
    COORDINATOR_FACING = {
        "site",
        "site_id",
        "display_name",
        "country",
        "summary",
        "registration",
        "worker",
        "audit_summary",
    }

    def test_the_pilot_and_overview_run_with_only_the_coordinator_facing_methods(self) -> None:
        consortium = Consortium()
        allowed = self.COORDINATOR_FACING

        class CoordinatorView:
            def __init__(self, hospital):
                self._hospital = hospital

            def __getattr__(self, name):
                if name not in allowed:
                    raise AssertionError(f"coordinator code used Hospital.{name}")
                return getattr(self._hospital, name)

        real = dict(consortium.hospitals)
        consortium.hospitals = {site_id: CoordinatorView(hospital) for site_id, hospital in real.items()}
        overview = consortium.overview()
        report = consortium.run_pilot("boundary", training_rounds=2)
        self.assertEqual(3, len(overview["hospitals"]))
        self.assertIsNotNone(report["candidate"])

    def test_only_hospital_code_reads_a_store(self) -> None:
        consortium = Consortium()
        callers: set[str] = set()
        for hospital in consortium.hospitals.values():
            store = hospital.store
            original = store.records

            def spy(original=original):
                callers.add(Path(inspect.stack()[1].filename).name)
                return original()

            store.records = spy  # type: ignore[method-assign]
        consortium.overview()
        consortium.run_pilot("spy", training_rounds=1)
        self.assertEqual({"hospital.py"}, callers)


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class ApprovalTests(unittest.TestCase):
    """A candidate becomes the model in use only after its gates pass and a person approves it."""

    def test_a_rejected_candidate_cannot_be_approved(self) -> None:
        consortium = Consortium()
        report = consortium.run_pilot("approve-reject", training_rounds=1)
        gates = report["release"]["gates"]
        version = report["candidate"]["candidate_version"]
        if gates["action"] == "release":
            self.skipTest("these gates released the candidate; the approval test below covers that path")
        decision = consortium.registry.get(version).release_decision
        self.assertEqual(gates["action"], decision["action"])
        self.assertEqual(gates["gate_results"], decision["gate_results"])
        with self.assertRaises(ApprovalRefused):
            consortium.approve(version)
        self.assertEqual(SEED_VERSION, consortium.model_in_use()["version"])

    def test_a_version_without_a_release_decision_cannot_be_approved(self) -> None:
        consortium = Consortium()
        report = consortium.run_pilot("approve-undecided", training_rounds=2)
        intermediate = report["training_curve"][0]["candidate_version"]
        for version in (INITIAL_VERSION, intermediate):
            with self.subTest(version=version):
                self.assertIsNone(consortium.registry.get(version).release_decision)
                with self.assertRaises(ApprovalRefused):
                    consortium.approve(version)

    def test_an_approval_works_after_a_restart(self) -> None:
        """The decision is stored with the model, so a new process can approve it."""
        consortium = Consortium()
        with mock.patch("app.services.consortium.STROKE_RELEASE_GATES", LENIENT_GATES):
            report = consortium.run_pilot("approve-restart", training_rounds=1)
        version = report["candidate"]["candidate_version"]
        self.assertEqual("release", report["release"]["gates"]["action"])
        # A restarted API builds a new consortium over the same coordinator store.
        restarted = Consortium(coordinator_store=consortium.coordinator_store)
        listed = {model["version"]: model for model in restarted.models()}
        self.assertEqual("release", listed[version]["release_decision"]["action"])
        self.assertTrue(listed[version]["release_decision"]["guard_permitted"])
        approved = restarted.approve(version)
        self.assertEqual(version, approved["released"]["version"])
        self.assertEqual(version, restarted.model_in_use()["version"])

    def test_an_approved_candidate_becomes_the_model_hospitals_use(self) -> None:
        """With one gate the candidate clears, the planner decides release; approval releases it."""
        consortium = Consortium()
        with mock.patch("app.services.consortium.STROKE_RELEASE_GATES", LENIENT_GATES):
            report = consortium.run_pilot("approve", training_rounds=2)
        version = report["candidate"]["candidate_version"]
        self.assertEqual("release", report["release"]["gates"]["action"])
        self.assertEqual("candidate", report["candidate"]["status"])
        self.assertEqual(SEED_VERSION, consortium.model_in_use()["version"], "a passing candidate is not yet released")

        approved = consortium.approve(version)
        self.assertEqual("released", approved["released"]["status"])
        self.assertEqual(SEED_VERSION, approved["previous"])
        self.assertEqual(version, consortium.model_in_use()["version"])
        statuses = {model["version"]: model["status"] for model in consortium.models()}
        self.assertEqual("superseded", statuses[SEED_VERSION])

        hospital = consortium.hospitals["it-002"]
        [top] = hospital.worklist(limit=1)
        self.assertEqual(version, top["assessment"]["model_version"])
        self.assertEqual("logistic_regression", top["assessment"]["model_kind"])
        self.assertEqual(version, hospital.dashboard()["current_model"]["version"])

        # The next pilot measures the released model and names its digests.
        evaluation = consortium.run_round("after-approval", "evaluation")
        self.assertEqual(version, evaluation["model_version"])
        self.assertEqual(stroke_model.training_code_digest(), evaluation["training_code_digest"])
        self.assertTrue(evaluation["readiness"]["aggregation_permitted"])

        with self.assertRaises(ApprovalRefused):
            consortium.approve(version)



@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class RestartTests(unittest.TestCase):
    """What a restarted API finds in the coordinator store: the last pilot and the release audit chain."""

    def test_a_new_process_shows_the_last_pilots_charts(self) -> None:
        from app.services.charts_service import ChartService

        consortium = Consortium()
        report = consortium.run_pilot("restart-charts", training_rounds=1)
        restarted = Consortium(coordinator_store=consortium.coordinator_store)
        self.assertEqual(report, restarted.last_pilot())
        charts = {chart["chart_id"]: chart for chart in ChartService().consortium_list(restarted)}
        for chart_id in ("site-auc", "training-curve", "release-gates"):
            self.assertTrue(charts[chart_id]["available"], charts[chart_id]["reason"])

    def test_the_newest_pilot_is_the_one_shown(self) -> None:
        consortium = Consortium()
        consortium.run_pilot("restart-first", training_rounds=1)
        second = consortium.run_pilot("restart-second", training_rounds=1)
        restarted = Consortium(coordinator_store=consortium.coordinator_store)
        self.assertEqual(second["pilot_id"], restarted.last_pilot()["pilot_id"])

    def test_the_release_audit_chain_survives_a_restart(self) -> None:
        consortium = Consortium()
        report = consortium.run_pilot("restart-audit", training_rounds=1)
        restarted = Consortium(coordinator_store=consortium.coordinator_store)
        records = restarted.governance.recent_audit()
        self.assertEqual(["before_release"], [record.boundary for record in records])
        self.assertEqual(report["release"]["guard"]["audit_id"], records[0].audit_id)
        self.assertTrue(restarted.governance.audit_chain_intact())


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class StaleApprovalTests(unittest.TestCase):
    """A candidate is approved only against the model it was compared with."""

    def test_a_candidate_checked_against_an_older_model_cannot_be_approved(self) -> None:
        consortium = Consortium()
        with mock.patch("app.services.consortium.STROKE_RELEASE_GATES", LENIENT_GATES):
            first = consortium.run_pilot("stale-a", training_rounds=1)
            second = consortium.run_pilot("stale-b", training_rounds=1)
        older = first["candidate"]["candidate_version"]
        newer = second["candidate"]["candidate_version"]
        for version in (older, newer):
            self.assertEqual(SEED_VERSION, consortium.registry.get(version).release_decision["baseline_version"])
        consortium.approve(newer)
        with self.assertRaises(ApprovalRefused) as raised:
            consortium.approve(older)
        self.assertIn(newer, str(raised.exception))
        self.assertEqual(newer, consortium.model_in_use()["version"])

    def test_a_decision_without_a_baseline_cannot_be_approved(self) -> None:
        consortium = Consortium()
        with mock.patch("app.services.consortium.STROKE_RELEASE_GATES", LENIENT_GATES):
            report = consortium.run_pilot("no-baseline", training_rounds=1)
        version = report["candidate"]["candidate_version"]
        decision = dict(consortium.registry.get(version).release_decision)
        decision.pop("baseline_version")
        decision.pop("decided_at", None)
        consortium.coordinator_store.set_release_decision(version, decision)
        with self.assertRaises(ApprovalRefused):
            consortium.approve(version)
        self.assertEqual(SEED_VERSION, consortium.model_in_use()["version"])


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class AbandonedRoundTests(unittest.TestCase):
    """A training round that cannot be aggregated leaves no update in the runtime."""

    def test_updates_are_deleted_when_a_round_is_not_aggregated(self) -> None:
        consortium = Consortium()
        hospital = consortium.hospitals["ch-245"]
        real = hospital.governance.enforce

        def deny_training_sends(request, payload=None, *, correlation_id=None):
            if request.boundary == "before_send" and "-training-" in (correlation_id or ""):
                request = request.model_copy(update={"declared_purpose": "marketing"})
            return real(request, payload, correlation_id=correlation_id)

        with mock.patch.object(hospital.governance, "enforce", side_effect=deny_training_sends):
            report = consortium.run_pilot("abandoned", training_rounds=2)
        self.assertIn("training round 1 could not be aggregated", report["stop_reason"])
        # The other two hospitals sent their clipped updates; none may stay behind.
        sent = [result for result in report["rounds"]["training"][0]["results"] if result["update_digest"]]
        self.assertEqual(2, len(sent))
        self.assertEqual([], consortium.runtime.pending_rounds())

    def test_updates_are_deleted_when_a_round_fails_part_way(self) -> None:
        consortium = Consortium()
        real = consortium.controller.evaluate_readiness

        def fail_training(round_id):
            if "-training-" in round_id:
                raise RuntimeError("the planner stopped answering")
            return real(round_id)

        with mock.patch.object(consortium.controller, "evaluate_readiness", side_effect=fail_training):
            with self.assertRaises(RuntimeError):
                consortium.run_pilot("failing", training_rounds=1)
        self.assertEqual([], consortium.runtime.pending_rounds())


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class ChangedUpdateTests(unittest.TestCase):
    """A hospital checks that the runtime stored the update it sent."""

    def test_a_runtime_that_changes_an_update_gets_no_contribution(self) -> None:
        from app.services.runtime import ParameterAveragingRuntime

        consortium = Consortium()
        real = ParameterAveragingRuntime.submit

        def doubling(runtime, job, update):
            return real(runtime, job, {name: value * 2 for name, value in update.items()})

        with mock.patch.object(ParameterAveragingRuntime, "submit", doubling):
            report = consortium.run_pilot("doubled", training_rounds=1)
        first = report["rounds"]["training"][0]
        self.assertEqual({"rejected"}, {result["participation"] for result in first["results"]})
        self.assertIsNone(report["candidate"])
        for hospital in consortium.hospitals.values():
            [entry] = [e for e in hospital.contributions(limit=50) if e["round_id"] == first["round_id"]]
            self.assertIn("different update", entry["reason"] or "")
        self.assertEqual([], consortium.runtime.pending_rounds())


if __name__ == "__main__":
    unittest.main()
