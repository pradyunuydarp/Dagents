"""Tests for the dagentsc subprocess wrapper helpers."""

from __future__ import annotations

import json
import subprocess
import unittest
from unittest.mock import patch

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
