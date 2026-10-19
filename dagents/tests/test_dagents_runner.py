"""Tests for the dagentsc subprocess wrapper helpers."""

from __future__ import annotations

import json
import logging
import math
import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from dagents.common.domain.federation import GateResult, ModelReleaseDecision
from dagents.common.infrastructure import dagents_runner
from dagents.tests.test_governance import dagentsc_available


class DagentsRunnerTests(unittest.TestCase):
    def completed(self, payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=["dagentsc"], returncode=0, stdout=json.dumps(payload), stderr="")

    @patch("dagents.common.infrastructure.dagents_runner.subprocess.run")
    def test_source_validation_uses_stdin_payload(self, run) -> None:
        run.return_value = self.completed({"valid": True, "errors": [], "warnings": []})

        result = dagents_runner.validate_dataset_source(
            {
                "source_id": "orders",
                "kind": "inline",
                "selection": {"records": [{"id": "a"}]},
            }
        )

        self.assertTrue(result["valid"])
        command = run.call_args.args[0]
        self.assertEqual(
            command[:5],
            [dagents_runner.dagentsc_binary(), "dataset", "source", "validate", "--input"],
        )
        stdin_payload = json.loads(run.call_args.kwargs["input"])
        self.assertEqual(stdin_payload["sourceId"], "orders")

    @patch("dagents.common.infrastructure.dagents_runner.subprocess.run")
    def test_quality_evaluation_uses_temp_files_and_converts_output(self, run) -> None:
        run.return_value = self.completed(
            {
                "blocking": True,
                "warningCount": 1,
                "errorCount": 1,
                "totalViolations": 2,
                "results": [],
            }
        )

        result = dagents_runner.evaluate_dataset_quality(
            [{"id": "a", "amount": -1}],
            [{"rule_id": "amount_positive", "field": "amount", "operator": {"kind": "min_value", "value": 0}}],
        )

        self.assertTrue(result["blocking"])
        self.assertEqual(result["warning_count"], 1)
        command = run.call_args.args[0]
        self.assertEqual(
            command[:4], [dagents_runner.dagentsc_binary(), "dataset", "quality", "evaluate"]
        )
        self.assertNotEqual(command[5], "-")
        self.assertNotEqual(command[7], "-")
        self.assertFalse("input" in run.call_args.kwargs)


class PlannerUnavailableTests(unittest.TestCase):
    """Every way the planner can fail raises one error a service can map to a 503."""

    def failed(self, stderr: str = "boom") -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=["dagentsc"], returncode=2, stdout="", stderr=stderr)

    def test_the_error_is_a_runtime_error(self) -> None:
        # Callers that already catch RuntimeError, such as the Guard, keep working.
        self.assertTrue(issubclass(dagents_runner.PlannerUnavailableError, RuntimeError))

    def test_a_missing_binary_raises_planner_unavailable(self) -> None:
        with patch.dict("os.environ", {"DAGENTSC_BIN": "/nonexistent/dagentsc"}):
            with self.assertRaises(dagents_runner.PlannerUnavailableError):
                dagents_runner.round_digest({"round_id": "r"})
            with self.assertRaises(dagents_runner.PlannerUnavailableError):
                dagents_runner.evaluate_dataset_quality([{"id": "a"}], [])
            with self.assertRaises(dagents_runner.PlannerUnavailableError):
                dagents_runner.model_lifecycle("v", {"kind": "registered"})

    def test_a_binary_that_cannot_be_run_raises_planner_unavailable(self) -> None:
        with patch(
            "dagents.common.infrastructure.dagents_runner.subprocess.run", side_effect=PermissionError("denied")
        ):
            with self.assertRaises(dagents_runner.PlannerUnavailableError):
                dagents_runner.round_digest({"round_id": "r"})
            with self.assertRaises(dagents_runner.PlannerUnavailableError):
                dagents_runner.evaluate_dataset_quality([{"id": "a"}], [])

    @patch("dagents.common.infrastructure.dagents_runner.subprocess.run")
    def test_a_non_zero_exit_raises_planner_unavailable(self, run) -> None:
        run.return_value = self.failed()
        with self.assertRaises(dagents_runner.PlannerUnavailableError):
            dagents_runner.round_digest({"round_id": "r"})
        with self.assertRaises(dagents_runner.PlannerUnavailableError):
            dagents_runner.evaluate_dataset_quality([{"id": "a"}], [])

    @patch("dagents.common.infrastructure.dagents_runner.subprocess.run")
    def test_output_that_is_not_json_raises_planner_unavailable(self, run) -> None:
        run.return_value = subprocess.CompletedProcess(args=["dagentsc"], returncode=0, stdout="{not json", stderr="")
        with self.assertRaises(dagents_runner.PlannerUnavailableError):
            dagents_runner.round_digest({"round_id": "r"})
        with self.assertRaises(dagents_runner.PlannerUnavailableError):
            dagents_runner.evaluate_dataset_quality([{"id": "a"}], [])


class PlannerLookupOrderTests(unittest.TestCase):
    """The runner looks for the planner in DAGENTSC_BIN, then the bundled binary, then PATH.

    A platform wheel bundles the planner at ``dagents/_bin/dagentsc``. These
    tests put a stand-in at a temporary path instead, so they never depend on
    how this checkout was installed.
    """

    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = pathlib.Path(folder.name)
        self.bundled = self.root / "_bin" / "dagentsc"
        self.empty_path = self.root / "empty-path"
        self.empty_path.mkdir()
        environ = {key: value for key, value in os.environ.items() if key != "DAGENTSC_BIN"}
        environ["PATH"] = str(self.empty_path)
        patcher = patch.dict(os.environ, environ, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        bundled = patch.object(dagents_runner, "BUNDLED_PLANNER", self.bundled)
        bundled.start()
        self.addCleanup(bundled.stop)

    def executable(self, path: pathlib.Path) -> pathlib.Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
        return path

    def test_the_setting_comes_first(self) -> None:
        self.executable(self.bundled)
        os.environ["DAGENTSC_BIN"] = "/opt/planner/dagentsc"
        self.assertEqual("/opt/planner/dagentsc", dagents_runner.dagentsc_binary())
        self.assertEqual(("/opt/planner/dagentsc", "DAGENTSC_BIN"), dagents_runner.find_planner())

    def test_a_setting_that_names_a_missing_file_is_still_used(self) -> None:
        """A wrong setting fails closed instead of quietly running another planner."""
        self.executable(self.bundled)
        os.environ["DAGENTSC_BIN"] = str(self.root / "missing" / "dagentsc")
        self.assertEqual(str(self.root / "missing" / "dagentsc"), dagents_runner.dagentsc_binary())
        with self.assertRaises(dagents_runner.PlannerUnavailableError):
            dagents_runner.round_digest({"round_id": "r"})

    def test_the_bundled_planner_comes_second(self) -> None:
        self.executable(self.bundled)
        self.executable(self.empty_path / "dagentsc")
        self.assertEqual(str(self.bundled), dagents_runner.dagentsc_binary())
        self.assertEqual((str(self.bundled), "bundled"), dagents_runner.find_planner())

    def test_an_empty_setting_counts_as_unset(self) -> None:
        self.executable(self.bundled)
        os.environ["DAGENTSC_BIN"] = ""
        self.assertEqual(str(self.bundled), dagents_runner.dagentsc_binary())

    def test_a_bundled_file_that_cannot_run_is_skipped(self) -> None:
        self.executable(self.bundled).chmod(0o644)
        self.assertIsNone(dagents_runner.bundled_planner())
        self.assertEqual("dagentsc", dagents_runner.dagentsc_binary())

    def test_path_comes_last(self) -> None:
        on_path = self.executable(self.empty_path / "dagentsc")
        self.assertEqual("dagentsc", dagents_runner.dagentsc_binary())
        self.assertEqual((str(on_path), "PATH"), dagents_runner.find_planner())

    def test_without_a_planner_the_runner_raises_and_reports_none(self) -> None:
        self.assertEqual((None, "none"), dagents_runner.find_planner())
        with self.assertRaises(dagents_runner.PlannerUnavailableError):
            dagents_runner.round_digest({"round_id": "r"})

    def test_a_planner_the_runner_can_start_has_no_problem(self) -> None:
        self.executable(self.bundled)
        self.assertIsNone(dagents_runner.planner_problem())
        os.environ["DAGENTSC_BIN"] = str(self.executable(self.root / "opt" / "dagentsc"))
        self.assertIsNone(dagents_runner.planner_problem())

    def test_the_problem_names_a_setting_that_cannot_run(self) -> None:
        missing = self.root / "missing" / "dagentsc"
        os.environ["DAGENTSC_BIN"] = str(missing)
        self.assertEqual(f"DAGENTSC_BIN names {missing}, which is not a file this process can run", dagents_runner.planner_problem())
        plain = self.executable(self.root / "plain" / "dagentsc")
        plain.chmod(0o644)
        os.environ["DAGENTSC_BIN"] = str(plain)
        self.assertIn(f"DAGENTSC_BIN names {plain}", dagents_runner.planner_problem())

    def test_the_problem_says_when_no_planner_was_found(self) -> None:
        self.assertIn("no planner was found", dagents_runner.planner_problem())

    def test_a_missing_planner_is_logged_once_with_the_remedy(self) -> None:
        """An install without a planner starts without an error, so the agents say why the Guard will deny."""
        log = logging.getLogger("dagents.tests.missing-planner")
        with self.assertLogs(log, level="WARNING") as captured:
            self.assertIsNotNone(dagents_runner.warn_if_no_planner(log, "dagents-lma"))
        self.assertEqual(1, len(captured.records))
        message = captured.records[0].getMessage()
        for words in ("dagents-lma has no planner", "no planner was found", "denies every request", "DAGENTSC_BIN", "python -m dagents"):
            self.assertIn(words, message)
        self.executable(self.bundled)
        with self.assertNoLogs(log, level="WARNING"):
            self.assertIsNone(dagents_runner.warn_if_no_planner(log, "dagents-lma"))

    def test_without_a_planner_the_guard_denies(self) -> None:
        """No setting, no bundled binary and nothing on PATH: the Guard fails closed."""
        from dagents.common.application.ethical_guard import DagentscRestrictionPlanner, EthicalGuard, InMemoryAuditLog
        from dagents.tests.test_governance import RECORDS, request_for

        guard = EthicalGuard(DagentscRestrictionPlanner(), InMemoryAuditLog())
        with self.assertLogs("dagents.common.application.ethical_guard", level="ERROR"):
            decision = guard.enforce(request_for(), RECORDS)
        self.assertFalse(decision.permitted)
        self.assertEqual([], decision.payload)
        self.assertEqual("The planner could not be reached, so the Guard denied the request.", decision.message)
        self.assertEqual(1, len(guard.audit.list_recent()), "the denial must still be audited")



class BundledPlannerPathTests(unittest.TestCase):
    def test_the_bundled_path_is_inside_the_package(self) -> None:
        """The build hook writes the binary to dagents/_bin/dagentsc; the runner must look there."""
        package = pathlib.Path(dagents_runner.__file__).resolve().parents[2]
        self.assertEqual("dagents", package.name)
        self.assertEqual(package / "_bin" / "dagentsc", dagents_runner.BUNDLED_PLANNER)


#: A round manifest in the snake_case form services send to the runner.
MANIFEST = {
    "round_id": "round-1",
    "study_id": "study-1",
    "condition_id": "condition-a",
    "phase": "evaluation",
    "model_version": "model-1",
    "model_artifact_digest": "sha256:4f1c0e9a",
    "training_code_digest": "sha256:9b27d3e1",
    "feature_contract_version": "contract-v1",
    "privacy_profile": "guarded-rows",
    "aggregation": "fedavg",
    "minimum_participants": 2,
    "minimum_cohort_per_site": 20,
    "required_capabilities": ["evaluate"],
    "stop_conditions": ["schema_failure", "quorum_not_met"],
    "invited_sites": ["Site_Alpha", "site-beta"],
}

NOT_SELECTED = "site was not selected for this round"


def completed_result(site_id: str, examples: int, job_digest: str) -> dict[str, object]:
    return {
        "round_id": MANIFEST["round_id"],
        "site_id": site_id,
        "job_digest": job_digest,
        "participation": "completed",
        "code_verified": True,
        "privacy_checks_passed": True,
        "contributed_examples": examples,
        "metrics": {"accuracy": 0.8},
    }


class ReadinessPayloadTests(unittest.TestCase):
    """The runner sends selectedSites only when the caller gives a selection."""

    @patch("dagents.common.infrastructure.dagents_runner.subprocess.run")
    def test_no_selection_sends_no_key(self, run) -> None:
        run.return_value = subprocess.CompletedProcess(args=["dagentsc"], returncode=0, stdout="{}", stderr="")
        dagents_runner.evaluate_aggregation_readiness(MANIFEST, [])
        self.assertNotIn("selectedSites", json.loads(run.call_args.kwargs["input"]))

    @patch("dagents.common.infrastructure.dagents_runner.subprocess.run")
    def test_a_selection_is_sent_unchanged(self, run) -> None:
        run.return_value = subprocess.CompletedProcess(args=["dagentsc"], returncode=0, stdout="{}", stderr="")
        dagents_runner.evaluate_aggregation_readiness(MANIFEST, [], selected_sites=("Site_Alpha",))
        self.assertEqual(["Site_Alpha"], json.loads(run.call_args.kwargs["input"])["selectedSites"])

    @patch("dagents.common.infrastructure.dagents_runner.subprocess.run")
    def test_an_empty_selection_is_still_sent(self, run) -> None:
        # An empty selection accepts no result; dropping it would accept every result.
        run.return_value = subprocess.CompletedProcess(args=["dagentsc"], returncode=0, stdout="{}", stderr="")
        dagents_runner.evaluate_aggregation_readiness(MANIFEST, [], selected_sites=[])
        self.assertEqual([], json.loads(run.call_args.kwargs["input"])["selectedSites"])

    def test_one_string_is_not_a_selection(self) -> None:
        with self.assertRaises(TypeError):
            dagents_runner.evaluate_aggregation_readiness(MANIFEST, [], selected_sites="Site_Alpha")


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class ReadinessSelectionTests(unittest.TestCase):
    """A result from a site the plan did not select counts for nothing.

    The GMA's probe showed a site named ``intruder``, never enrolled or
    invited, accepted with most of the weight.
    """

    def setUp(self) -> None:
        self.digest = dagents_runner.round_digest(MANIFEST)["round_digest"]
        self.results = [
            completed_result("Site_Alpha", 60, self.digest),
            completed_result("site-beta", 40, self.digest),
            completed_result("intruder", 900, self.digest),
        ]

    def test_without_a_selection_the_answer_is_unchanged(self) -> None:
        readiness = dagents_runner.evaluate_aggregation_readiness(MANIFEST, self.results)
        self.assertEqual(["Site_Alpha", "intruder", "site-beta"], readiness["accepted_sites"])

    def test_an_unselected_site_is_rejected_and_gets_no_weight(self) -> None:
        readiness = dagents_runner.evaluate_aggregation_readiness(
            MANIFEST, self.results, selected_sites=["Site_Alpha", "site-beta"]
        )
        self.assertEqual(["Site_Alpha", "site-beta"], readiness["accepted_sites"])
        self.assertEqual(
            [{"site_id": "intruder", "reason": NOT_SELECTED}], readiness["rejected_contributions"]
        )
        self.assertEqual(100, readiness["accepted_examples"])
        # Site ids are data: "Site_Alpha" keeps its spelling both ways.
        self.assertEqual({"Site_Alpha": 0.6, "site-beta": 0.4}, readiness["site_weights"])
        self.assertTrue(readiness["aggregation_permitted"])

    def test_an_unselected_site_does_not_make_the_quorum(self) -> None:
        readiness = dagents_runner.evaluate_aggregation_readiness(
            MANIFEST, [self.results[0], self.results[2]], selected_sites=["Site_Alpha", "site-beta"]
        )
        self.assertFalse(readiness["aggregation_permitted"])
        self.assertEqual("quorum_not_met", readiness["stop_reason"])


#: A release decision as a pilot stores it when the gates and the Guard passed.
PASSING_DECISION = {
    "action": "release",
    "guard_permitted": True,
    "metrics_source": "rounds",
    "baseline_version": "rule-v1",
    # Keys the rule does not read are ignored, nested ones included.
    "gate_results": [{"gate_id": "auc", "outcome": "passed"}],
    "round_id": "pilot-training-5",
}

CANDIDATE = "logreg+pilot-training-5"


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class ModelLifecycleTests(unittest.TestCase):
    """Every model status change comes from ``dagentsc federation model lifecycle``."""

    def approve(self, *, status: str = "candidate", released: str | None = "rule-v1", **decision) -> dict:
        event = {
            "kind": "approval_requested",
            "status": status,
            "decision": {**PASSING_DECISION, **decision},
            "released_version": released,
        }
        return dagents_runner.model_lifecycle(CANDIDATE, event)

    def test_registration(self) -> None:
        self.assertEqual(
            {
                "allowed": True,
                "refusal": None,
                "reason": "rule-v1 is registered as released, because no version is released",
                "status_changes": [{"version": "rule-v1", "from_status": None, "to_status": "released"}],
            },
            dagents_runner.model_lifecycle("rule-v1", {"kind": "registered", "requested_status": "released"}),
        )
        refused = dagents_runner.model_lifecycle(
            "rule-v2", {"kind": "registered", "requested_status": "released", "released_version": "rule-v1"}
        )
        self.assertEqual((False, "already_released", []), (refused["allowed"], refused["refusal"], refused["status_changes"]))

    def test_aggregation_supersedes_a_candidate_parent(self) -> None:
        outcome = dagents_runner.model_lifecycle(
            "logreg+pilot-training-2",
            {"kind": "aggregated", "parent_version": "logreg+pilot-training-1", "parent_status": "candidate"},
        )
        self.assertEqual(
            [
                {"version": "logreg+pilot-training-1", "from_status": "candidate", "to_status": "superseded"},
                {"version": "logreg+pilot-training-2", "from_status": None, "to_status": "candidate"},
            ],
            outcome["status_changes"],
        )

    def test_a_failed_release_evaluation_rejects_the_candidate(self) -> None:
        outcome = dagents_runner.model_lifecycle(
            CANDIDATE,
            {"kind": "release_evaluated", "status": "candidate", "action": "reject", "guard_permitted": True},
        )
        self.assertTrue(outcome["allowed"])
        self.assertEqual(
            [{"version": CANDIDATE, "from_status": "candidate", "to_status": "rejected"}], outcome["status_changes"]
        )
        self.assertIn("the release gates decided reject", outcome["reason"])

    def test_an_approval_releases_and_supersedes(self) -> None:
        outcome = self.approve()
        self.assertTrue(outcome["allowed"])
        self.assertEqual(
            [
                {"version": CANDIDATE, "from_status": "candidate", "to_status": "released"},
                {"version": "rule-v1", "from_status": "released", "to_status": "superseded"},
            ],
            outcome["status_changes"],
        )

    def test_each_approval_refusal_keeps_the_demo_wording(self) -> None:
        cases = {
            "no_decision": (
                dagents_runner.model_lifecycle(
                    CANDIDATE, {"kind": "approval_requested", "status": "candidate", "decision": None}
                ),
                f"{CANDIDATE} has no release decision; only a pilot's candidate can be approved",
            ),
            "gates_decided": (self.approve(action="reject"), f"the release gates decided reject for {CANDIDATE}"),
            "guard_denied": (self.approve(guard_permitted=False), f"the release Guard did not permit {CANDIDATE}"),
            "metrics_not_from_rounds": (
                self.approve(metrics_source="request"),
                f"the release decision for {CANDIDATE} did not take its metrics from the rounds",
            ),
            "not_candidate": (self.approve(status="rejected"), f"{CANDIDATE} is rejected, not a candidate"),
            "baseline_changed": (
                self.approve(released="logreg+pilot-training-9"),
                f"{CANDIDATE} was checked against rule-v1, but logreg+pilot-training-9 is now in use. "
                "Run the pilot again.",
            ),
        }
        for refusal, (outcome, reason) in cases.items():
            with self.subTest(refusal=refusal):
                self.assertEqual(
                    {"allowed": False, "refusal": refusal, "reason": reason, "status_changes": []}, outcome
                )

    def test_a_decision_without_a_metrics_source_cannot_be_approved(self) -> None:
        decision = {key: value for key, value in PASSING_DECISION.items() if key != "metrics_source"}
        outcome = dagents_runner.model_lifecycle(
            CANDIDATE,
            {"kind": "approval_requested", "status": "candidate", "decision": decision, "released_version": "rule-v1"},
        )
        self.assertEqual("metrics_not_from_rounds", outcome["refusal"])

    def test_an_event_the_planner_cannot_read_raises_planner_unavailable(self) -> None:
        for event in ({"kind": "deployed"}, {"kind": "registered", "requested_status": "rejected"}, {}):
            with self.subTest(event=event):
                with self.assertRaises(dagents_runner.PlannerUnavailableError):
                    dagents_runner.model_lifecycle(CANDIDATE, event)


#: Gate results as a release decision stores them, with every key. The gate
#: ids and the detail are caller data and must reach the planner unchanged.
GATE_RESULTS = [
    {
        "gate_id": "AUC_Overall",
        "outcome": "passed",
        "observed": 0.84,
        "threshold": 0.8,
        "detail": "AUC_Overall=0.84 required >= 0.8",
    },
    {"gate_id": "alerts_per_1000", "outcome": "not_evaluated", "observed": None, "threshold": None, "detail": ""},
]

#: One round's release decision: the six fields the planner compares.
ROUND_DECISION = {
    "round_id": "pilot-training-5",
    "action": "release",
    "guard_permitted": True,
    "baseline_version": "rule-v1",
    "metrics_source": "rounds",
    "gate_results": GATE_RESULTS,
}

MODEL_STATUSES = ("candidate", "released", "rejected", "superseded")


def release_evaluated(status: str, stored: dict | None, **changes) -> dict:
    """A ``release_evaluated`` event whose new decision is ROUND_DECISION with ``changes``."""
    event = {"kind": "release_evaluated", "status": status, **ROUND_DECISION, **changes}
    if stored is not None:
        event["stored_decision"] = stored
    return event


def todays_answer(status: str, action: str, guard_permitted: bool) -> dict:
    """The answer to a ``release_evaluated`` event with only the keys it had before stored decisions."""
    return dagents_runner.model_lifecycle(
        CANDIDATE, {"kind": "release_evaluated", "status": status, "action": action, "guard_permitted": guard_permitted}
    )


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class ModelLifecycleStoredDecisionTests(unittest.TestCase):
    """A repeated ``release_evaluated`` event is compared with the decision the version holds."""

    SAME = f"{CANDIDATE} already has the same release decision for round pilot-training-5, so nothing changes"

    def differs(self, fields: str) -> dict:
        return {
            "allowed": False,
            "refusal": "decision_differs",
            "reason": f"{CANDIDATE} already has a release decision for round pilot-training-5, and this one differs in {fields}",
            "status_changes": [],
        }

    def test_the_same_decision_for_the_same_round_changes_nothing_on_every_status(self) -> None:
        for status in MODEL_STATUSES:
            with self.subTest(status=status):
                self.assertEqual(
                    {"allowed": True, "refusal": None, "reason": self.SAME, "status_changes": []},
                    dagents_runner.model_lifecycle(CANDIDATE, release_evaluated(status, ROUND_DECISION)),
                )

    def test_a_retried_rejection_is_no_longer_refused_as_not_candidate(self) -> None:
        rejected = {**ROUND_DECISION, "action": "reject"}
        self.assertEqual("not_candidate", todays_answer("rejected", "reject", True)["refusal"])
        outcome = dagents_runner.model_lifecycle(CANDIDATE, release_evaluated("rejected", rejected, action="reject"))
        self.assertEqual((True, None, []), (outcome["allowed"], outcome["refusal"], outcome["status_changes"]))
        self.assertEqual(self.SAME, outcome["reason"])

    def test_each_differing_field_is_refused_and_named(self) -> None:
        changes = {
            "action": {"action": "reject"},
            "guard_permitted": {"guard_permitted": False},
            "baseline_version": {"baseline_version": None},
            "metrics_source": {"metrics_source": "request"},
            "gate_results": {"gate_results": GATE_RESULTS[:1]},
        }
        for field, change in changes.items():
            for status in MODEL_STATUSES:
                with self.subTest(field=field, status=status):
                    self.assertEqual(
                        self.differs(field),
                        dagents_runner.model_lifecycle(CANDIDATE, release_evaluated(status, ROUND_DECISION, **change)),
                    )

    def test_several_differing_fields_are_named_in_field_order(self) -> None:
        outcome = dagents_runner.model_lifecycle(
            CANDIDATE,
            release_evaluated("candidate", ROUND_DECISION, gate_results=[], metrics_source=None, action="reject"),
        )
        self.assertEqual(self.differs("action, metrics_source and gate_results"), outcome)

    def test_gate_numbers_are_compared_exactly(self) -> None:
        nudged = [{**GATE_RESULTS[0], "observed": math.nextafter(0.84, 1.0)}, GATE_RESULTS[1]]
        self.assertEqual(
            self.differs("gate_results"),
            dagents_runner.model_lifecycle(CANDIDATE, release_evaluated("candidate", ROUND_DECISION, gate_results=nudged)),
        )
        # A number stored as an integer is the same number: jsonb may write 1.0 as 1.
        whole = [{**GATE_RESULTS[0], "threshold": 1.0}, GATE_RESULTS[1]]
        stored = {**ROUND_DECISION, "gate_results": [{**GATE_RESULTS[0], "threshold": 1}, GATE_RESULTS[1]]}
        self.assertEqual(
            self.SAME,
            dagents_runner.model_lifecycle(CANDIDATE, release_evaluated("candidate", stored, gate_results=whole))["reason"],
        )

    def test_a_stored_decision_for_another_round_gives_todays_answer(self) -> None:
        earlier = {**ROUND_DECISION, "round_id": "pilot-training-4"}
        for status in MODEL_STATUSES:
            for action, guard_permitted in (("release", True), ("release", False), ("reject", True), ("another_round", True)):
                with self.subTest(status=status, action=action, guard_permitted=guard_permitted):
                    self.assertEqual(
                        todays_answer(status, action, guard_permitted),
                        dagents_runner.model_lifecycle(
                            CANDIDATE,
                            release_evaluated(status, earlier, action=action, guard_permitted=guard_permitted),
                        ),
                    )

    def test_without_a_stored_decision_the_new_keys_change_nothing(self) -> None:
        for status in MODEL_STATUSES:
            with self.subTest(status=status):
                expected = todays_answer(status, "release", True)
                self.assertEqual(expected, dagents_runner.model_lifecycle(CANDIDATE, release_evaluated(status, None)))
                self.assertEqual(
                    expected,
                    dagents_runner.model_lifecycle(CANDIDATE, {**release_evaluated(status, None), "stored_decision": None}),
                )

    def test_the_registry_models_dump_to_a_stored_decision_the_planner_reads(self) -> None:
        """The registry's stored ``ModelReleaseDecision`` can be sent as it is: the planner ignores ``decided_at``."""
        stored = ModelReleaseDecision(
            **(ROUND_DECISION | {"gate_results": [GateResult(**gate) for gate in GATE_RESULTS]}),
            decided_at="2026-10-19T09:00:00+00:00",
        ).model_dump(mode="json")
        self.assertEqual(
            self.SAME,
            dagents_runner.model_lifecycle(CANDIDATE, release_evaluated("released", stored))["reason"],
        )

    def test_a_stored_decision_without_the_new_decision_raises_planner_unavailable(self) -> None:
        incomplete_gate = {key: value for key, value in GATE_RESULTS[0].items() if key != "threshold"}
        events = {
            "no new round id": {
                key: value for key, value in release_evaluated("candidate", ROUND_DECISION).items() if key != "round_id"
            },
            "no new gate results": {
                key: value for key, value in release_evaluated("candidate", ROUND_DECISION).items() if key != "gate_results"
            },
            "no guard verdict": {
                key: value
                for key, value in release_evaluated("candidate", ROUND_DECISION).items()
                if key != "guard_permitted"
            },
            "stored decision without a baseline key": release_evaluated(
                "candidate", {key: value for key, value in ROUND_DECISION.items() if key != "baseline_version"}
            ),
            "stored gate without a threshold": release_evaluated(
                "candidate", {**ROUND_DECISION, "gate_results": [incomplete_gate]}
            ),
            "unknown gate outcome": release_evaluated(
                "candidate", {**ROUND_DECISION, "gate_results": [{**GATE_RESULTS[0], "outcome": "skipped"}]}
            ),
            "part of the new keys without a stored decision": {
                key: value for key, value in release_evaluated("candidate", None).items() if key != "metrics_source"
            },
        }
        for name, event in events.items():
            with self.subTest(event=name):
                with self.assertRaises(dagents_runner.PlannerUnavailableError):
                    dagents_runner.model_lifecycle(CANDIDATE, event)


#: What recovery knows about a collecting training round whose updates are
#: kept and fresh: the inputs of row 8.
OPEN_TRAINING_ROUND = {
    "status": "collecting",
    "phase": "training",
    "readable": True,
    "updates_kept": True,
    "candidate_registered": False,
    "oldest_update_age_seconds": 3600.0,
    "update_max_age_seconds": 86400.0,
}

RUN_AGAIN = "so it is closed; run it again under a new round id."


def recovery(**changes) -> dict:
    return dagents_runner.round_recovery(**{**OPEN_TRAINING_ROUND, **changes})


def keep(message: str) -> dict:
    return {"verdict": "keep", "reason": None, "message": message}


def close(reason: str, message: str) -> dict:
    return {"verdict": "close", "reason": reason, "message": message}


class RoundRecoveryPayloadTests(unittest.TestCase):
    """The runner sends every key in camelCase and reads the answer back in snake_case."""

    @patch("dagents.common.infrastructure.dagents_runner.subprocess.run")
    def test_keys_are_converted_both_ways(self, run) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=["dagentsc"],
            returncode=0,
            stdout=json.dumps({"verdict": "keep", "reason": None, "message": "kept"}),
            stderr="",
        )
        answer = recovery(oldest_update_age_seconds=None)
        self.assertEqual(["federation", "round", "recover", "--input", "-"], run.call_args.args[0][1:])
        self.assertEqual(
            {
                "status": "collecting",
                "phase": "training",
                "readable": True,
                "updatesKept": True,
                "candidateRegistered": False,
                # None is sent as null, never dropped: every key is required.
                "oldestUpdateAgeSeconds": None,
                "updateMaxAgeSeconds": 86400.0,
            },
            json.loads(run.call_args.kwargs["input"]),
        )
        self.assertEqual({"verdict": "keep", "reason": None, "message": "kept"}, answer)

    def test_every_input_is_required(self) -> None:
        for key in OPEN_TRAINING_ROUND:
            with self.subTest(key=key):
                with self.assertRaises(TypeError):
                    dagents_runner.round_recovery(**{name: value for name, value in OPEN_TRAINING_ROUND.items() if name != key})


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class RoundRecoveryTests(unittest.TestCase):
    """``dagentsc federation round recover`` keeps or closes a round stored before a restart."""

    def test_row_1_an_aggregated_or_closed_round_is_kept_whatever_else_is_given(self) -> None:
        for status in ("aggregated", "closed"):
            for phase in ("analytics", "evaluation", "training"):
                for readable, updates_kept, candidate_registered in ((True, True, False), (False, False, True)):
                    with self.subTest(status=status, phase=phase, readable=readable, updates_kept=updates_kept):
                        self.assertEqual(
                            keep(f"The round is already {status}, so it stays as it is."),
                            recovery(
                                status=status,
                                phase=phase,
                                readable=readable,
                                updates_kept=updates_kept,
                                candidate_registered=candidate_registered,
                                oldest_update_age_seconds=999999.0,
                            ),
                        )

    def test_row_2_an_unreadable_open_round_is_closed(self) -> None:
        for status in ("planned", "dispatched", "collecting"):
            for phase in ("analytics", "training"):
                with self.subTest(status=status, phase=phase):
                    self.assertEqual(
                        close(
                            "unreadable_record",
                            f"The round is {status}, but its stored record cannot be read, so it is closed.",
                        ),
                        recovery(status=status, phase=phase, readable=False, candidate_registered=True),
                    )

    def test_row_3_a_planned_or_dispatched_round_is_closed(self) -> None:
        for status in ("planned", "dispatched"):
            for phase in ("analytics", "evaluation", "training"):
                with self.subTest(status=status, phase=phase):
                    self.assertEqual(
                        close(
                            "dispatch_interrupted",
                            f"The round is {status}, and a dispatch cannot resume after a restart, {RUN_AGAIN}",
                        ),
                        recovery(status=status, phase=phase, candidate_registered=True),
                    )

    def test_row_4_a_collecting_round_that_does_not_train_is_kept(self) -> None:
        for phase in ("analytics", "evaluation"):
            with self.subTest(phase=phase):
                # Training inputs that would close a training round do not matter here.
                self.assertEqual(
                    keep(f"The {phase} round already holds its results, so it stays open and can be aggregated."),
                    recovery(phase=phase, updates_kept=False, oldest_update_age_seconds=999999.0),
                )

    def test_row_5_a_training_round_with_a_registered_candidate_is_kept(self) -> None:
        for updates_kept in (True, False):
            with self.subTest(updates_kept=updates_kept):
                self.assertEqual(
                    keep(
                        "The round's candidate is already registered, so it stays open and a retried aggregate "
                        "returns that candidate."
                    ),
                    recovery(candidate_registered=True, updates_kept=updates_kept, oldest_update_age_seconds=999999.0),
                )

    def test_row_6_a_training_round_whose_updates_were_not_kept_is_closed(self) -> None:
        for age in (None, 10.0, 999999.0):
            with self.subTest(age=age):
                self.assertEqual(
                    close(
                        "updates_not_kept",
                        f"The round's training updates were not kept across the restart, {RUN_AGAIN}",
                    ),
                    recovery(updates_kept=False, oldest_update_age_seconds=age),
                )

    def test_row_7_a_training_round_with_an_expired_update_is_closed(self) -> None:
        self.assertEqual(
            close(
                "updates_expired",
                "The round's oldest stored update is 90061 seconds old, more than the limit of 86400 seconds, "
                + RUN_AGAIN,
            ),
            recovery(oldest_update_age_seconds=90061.0),
        )
        self.assertEqual(
            "updates_expired", recovery(oldest_update_age_seconds=math.nextafter(86400.0, math.inf))["reason"]
        )

    def test_row_8_any_other_training_round_is_kept(self) -> None:
        cases = {
            "fresh update": {},
            "age equal to the limit": {"oldest_update_age_seconds": 86400.0},
            "age 0": {"oldest_update_age_seconds": 0},
            "no stored update": {"oldest_update_age_seconds": None},
            "no age limit": {"update_max_age_seconds": None, "oldest_update_age_seconds": 999999999.0},
            "neither": {"update_max_age_seconds": None, "oldest_update_age_seconds": None},
        }
        for name, changes in cases.items():
            with self.subTest(case=name):
                self.assertEqual(keep("The round's training updates are kept, so it stays open."), recovery(**changes))

    def test_the_same_input_gives_the_same_answer(self) -> None:
        for changes in ({}, {"oldest_update_age_seconds": 90061.5}, {"status": "dispatched"}):
            with self.subTest(changes=changes):
                self.assertEqual(recovery(**changes), recovery(**changes))

    def test_input_the_planner_cannot_read_raises_planner_unavailable(self) -> None:
        cases = {
            "unknown status": {"status": "running"},
            "unknown phase": {"phase": "inference"},
            "negative age": {"oldest_update_age_seconds": -1.0},
            "infinite age": {"oldest_update_age_seconds": math.inf},
            "age that is not a number": {"oldest_update_age_seconds": math.nan},
            "zero limit": {"update_max_age_seconds": 0},
            "negative limit": {"update_max_age_seconds": -60.0},
            "infinite limit": {"update_max_age_seconds": math.inf},
            "readable that is not a bool": {"readable": "yes"},
            "updates kept as a number": {"updates_kept": 1},
            "age as text": {"oldest_update_age_seconds": "3600"},
        }
        for name, changes in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(dagents_runner.PlannerUnavailableError):
                    recovery(**changes)

    def test_a_missing_key_raises_planner_unavailable(self) -> None:
        """The function needs every input, so this sends the payload past it."""
        for key in OPEN_TRAINING_ROUND:
            with self.subTest(key=key):
                with self.assertRaises(dagents_runner.PlannerUnavailableError) as raised:
                    dagents_runner.run_dagentsc(
                        ["federation", "round", "recover", "--input", "-"],
                        {name: value for name, value in OPEN_TRAINING_ROUND.items() if name != key},
                    )
                self.assertIn(dagents_runner.to_camel_case(key), str(raised.exception))


class StoredDecisionPayloadTests(unittest.TestCase):
    """A stored decision reaches the planner in camelCase, with its caller data unchanged."""

    @patch("dagents.common.infrastructure.dagents_runner.subprocess.run")
    def test_gate_result_keys_are_converted_and_their_values_are_not(self, run) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=["dagentsc"],
            returncode=0,
            stdout=json.dumps({"allowed": False, "refusal": "decision_differs", "reason": "r", "statusChanges": []}),
            stderr="",
        )
        stored = {**ROUND_DECISION, "round_id": "pilot_training_5", "baseline_version": "rule_V1"}
        answer = dagents_runner.model_lifecycle(
            CANDIDATE, release_evaluated("candidate", stored, round_id="pilot_training_5")
        )
        sent = json.loads(run.call_args.kwargs["input"])["event"]
        camel_gates = [
            {
                "gateId": "AUC_Overall",
                "outcome": "passed",
                "observed": 0.84,
                "threshold": 0.8,
                "detail": "AUC_Overall=0.84 required >= 0.8",
            },
            {"gateId": "alerts_per_1000", "outcome": "not_evaluated", "observed": None, "threshold": None, "detail": ""},
        ]
        self.assertEqual(
            {
                "kind": "release_evaluated",
                "status": "candidate",
                "roundId": "pilot_training_5",
                "action": "release",
                "guardPermitted": True,
                "baselineVersion": "rule-v1",
                "metricsSource": "rounds",
                "gateResults": camel_gates,
                "storedDecision": {
                    "roundId": "pilot_training_5",
                    "action": "release",
                    "guardPermitted": True,
                    "baselineVersion": "rule_V1",
                    "metricsSource": "rounds",
                    "gateResults": camel_gates,
                },
            },
            sent,
        )
        self.assertEqual(
            {"allowed": False, "refusal": "decision_differs", "reason": "r", "status_changes": []}, answer
        )


class KeyConversionTests(unittest.TestCase):
    """Maps whose keys are caller data must survive both directions.

    The naive guard compared the already-converted key against a camelCase-only
    set, which held outbound and failed inbound. A site called "Mercy_General"
    came back as "mercy__general", the weight lookup missed it, and every
    aggregated metric silently became 0.0 on its way into the release gates.
    """

    def test_site_weight_keys_survive_the_return_trip(self) -> None:
        response = {
            "siteWeights": {"Mercy_General": 0.5, "siteBeta": 0.5},
            "acceptedSites": ["Mercy_General", "siteBeta"],
            "aggregationPermitted": True,
        }
        converted = dagents_runner.convert_keys(response, dagents_runner.to_snake_case)
        self.assertEqual(sorted(converted["site_weights"]), ["Mercy_General", "siteBeta"])
        self.assertTrue(converted["aggregation_permitted"])

    def test_metric_and_classification_keys_survive_the_return_trip(self) -> None:
        response = {
            "candidateMetrics": {"subgroup_auc_gap": 0.1},
            "baselineMetrics": {"AUC_Overall": 0.8},
            "metrics": {"alerts_per_1000": 42.0},
        }
        converted = dagents_runner.convert_keys(response, dagents_runner.to_snake_case)
        self.assertIn("subgroup_auc_gap", converted["candidate_metrics"])
        self.assertIn("AUC_Overall", converted["baseline_metrics"])
        self.assertIn("alerts_per_1000", converted["metrics"])

    def test_field_names_survive_the_outbound_trip(self) -> None:
        request = {
            "field_sensitivity": {"nihss_total": "high", "last_known_well_minutes": "high"},
            "request_id": "r-1",
        }
        converted = dagents_runner.convert_keys(request, dagents_runner.to_camel_case)
        self.assertEqual(
            sorted(converted["fieldSensitivity"]), ["last_known_well_minutes", "nihss_total"]
        )
        self.assertEqual(converted["requestId"], "r-1")

    def test_config_map_keys_survive_the_outbound_trip(self) -> None:
        """A ConfigMap key is chosen by whoever authored the workload.

        Converting it renames the entry: a component asking for "router_mode"
        deployed a ConfigMap keyed "routerMode", which the workload reading it
        would not find.
        """
        request = {"config_map_data": {"router_mode": "edge", "log_level": "debug"}}
        converted = dagents_runner.convert_keys(request, dagents_runner.to_camel_case)
        self.assertEqual(sorted(converted["configMapData"]), ["log_level", "router_mode"])

    def test_a_key_outside_the_data_sets_is_still_converted(self) -> None:
        """The guard must not become a blanket exemption."""
        converted = dagents_runner.convert_keys({"roundId": {"innerKey": 1}}, dagents_runner.to_snake_case)
        self.assertEqual(converted, {"round_id": {"inner_key": 1}})


if __name__ == "__main__":
    unittest.main()
