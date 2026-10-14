"""Tests for the dagentsc subprocess wrapper helpers."""

from __future__ import annotations

import json
import subprocess
import unittest
from unittest.mock import patch

from agents.common.infrastructure import dagents_runner
from agents.tests.test_governance import dagentsc_available


class DagentsRunnerTests(unittest.TestCase):
    def completed(self, payload: dict[str, object]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=["dagentsc"], returncode=0, stdout=json.dumps(payload), stderr="")

    @patch("agents.common.infrastructure.dagents_runner.subprocess.run")
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

    @patch("agents.common.infrastructure.dagents_runner.subprocess.run")
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

    def test_a_binary_that_cannot_be_run_raises_planner_unavailable(self) -> None:
        with patch(
            "agents.common.infrastructure.dagents_runner.subprocess.run", side_effect=PermissionError("denied")
        ):
            with self.assertRaises(dagents_runner.PlannerUnavailableError):
                dagents_runner.round_digest({"round_id": "r"})
            with self.assertRaises(dagents_runner.PlannerUnavailableError):
                dagents_runner.evaluate_dataset_quality([{"id": "a"}], [])

    @patch("agents.common.infrastructure.dagents_runner.subprocess.run")
    def test_a_non_zero_exit_raises_planner_unavailable(self, run) -> None:
        run.return_value = self.failed()
        with self.assertRaises(dagents_runner.PlannerUnavailableError):
            dagents_runner.round_digest({"round_id": "r"})
        with self.assertRaises(dagents_runner.PlannerUnavailableError):
            dagents_runner.evaluate_dataset_quality([{"id": "a"}], [])

    @patch("agents.common.infrastructure.dagents_runner.subprocess.run")
    def test_output_that_is_not_json_raises_planner_unavailable(self, run) -> None:
        run.return_value = subprocess.CompletedProcess(args=["dagentsc"], returncode=0, stdout="{not json", stderr="")
        with self.assertRaises(dagents_runner.PlannerUnavailableError):
            dagents_runner.round_digest({"round_id": "r"})
        with self.assertRaises(dagents_runner.PlannerUnavailableError):
            dagents_runner.evaluate_dataset_quality([{"id": "a"}], [])


class PlannerRejectedTests(unittest.TestCase):
    """Exit status 3 is the planner's answer that the input is wrong, not a failure."""

    def rejected(self, stdout: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["dagentsc"], returncode=dagents_runner.REJECTED_EXIT_CODE, stdout=stdout, stderr=""
        )

    def test_the_error_is_a_value_error_and_not_planner_unavailable(self) -> None:
        # A service maps PlannerUnavailableError (a RuntimeError) to 503. A
        # rejection must not be mistaken for that.
        self.assertTrue(issubclass(dagents_runner.PlannerRejectedError, ValueError))
        self.assertFalse(issubclass(dagents_runner.PlannerRejectedError, RuntimeError))

    @patch("agents.common.infrastructure.dagents_runner.subprocess.run")
    def test_a_report_on_exit_3_raises_planner_rejected(self, run) -> None:
        run.return_value = self.rejected(
            json.dumps({"code": "cyclic_dependency", "message": "Cyclic dependency: a -> a", "stepIds": ["a", "a"]})
        )
        with self.assertRaises(dagents_runner.PlannerRejectedError) as caught:
            dagents_runner.run_dagentsc(["pipeline", "compile", "--input", "-", "--output", "json"], {})
        self.assertEqual("cyclic_dependency", caught.exception.code)
        self.assertEqual("Cyclic dependency: a -> a", caught.exception.message)
        self.assertEqual("Cyclic dependency: a -> a", str(caught.exception))
        self.assertEqual({"step_ids": ["a", "a"]}, caught.exception.details)

    @patch("agents.common.infrastructure.dagents_runner.subprocess.run")
    def test_exit_3_without_a_usable_report_raises_planner_unavailable(self, run) -> None:
        for stdout in ("", "{not json", json.dumps(["a"]), json.dumps({"code": "x"})):
            with self.subTest(stdout=stdout):
                run.return_value = self.rejected(stdout)
                with self.assertRaises(dagents_runner.PlannerUnavailableError):
                    dagents_runner.run_dagentsc(["pipeline", "compile", "--input", "-", "--output", "json"], {})


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class PlannerRejectionContractTests(unittest.TestCase):
    """The real planner rejects a bad pipeline graph with exit status 3 and a report."""

    def compile(self, steps: list[dict[str, object]]) -> object:
        return dagents_runner.run_dagentsc(
            ["pipeline", "compile", "--input", "-", "--output", "json"],
            {"pipeline_id": "p", "steps": steps},
        )

    def test_each_graph_error_is_a_rejection_with_its_code(self) -> None:
        cases = {
            "cyclic_dependency": [
                {"step_id": "a", "kind": "enrich_context", "depends_on": ["b"]},
                {"step_id": "b", "kind": "enrich_context", "depends_on": ["a"]},
            ],
            "duplicate_step_id": [
                {"step_id": "a", "kind": "enrich_context"},
                {"step_id": "a", "kind": "enrich_context"},
            ],
            "unknown_dependency": [{"step_id": "a", "kind": "enrich_context", "depends_on": ["missing"]}],
        }
        for code, steps in cases.items():
            with self.subTest(code=code):
                with self.assertRaises(dagents_runner.PlannerRejectedError) as caught:
                    self.compile(steps)
                self.assertEqual(code, caught.exception.code)
                self.assertTrue(caught.exception.message)

    def test_a_valid_graph_compiles_in_dependency_order(self) -> None:
        plan = self.compile(
            [
                {"step_id": "b", "kind": "enrich_context", "depends_on": ["a"]},
                {"step_id": "a", "kind": "enrich_context"},
            ]
        )
        self.assertEqual(["a", "b"], [step["step_id"] for step in plan["steps"]])


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
