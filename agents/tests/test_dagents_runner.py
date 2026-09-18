"""Tests for the dagentsc subprocess wrapper helpers."""

from __future__ import annotations

import json
import subprocess
import unittest
from unittest.mock import patch

from agents.common.infrastructure import dagents_runner


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

    def test_a_key_outside_the_data_sets_is_still_converted(self) -> None:
        """The guard must not become a blanket exemption."""
        converted = dagents_runner.convert_keys({"roundId": {"innerKey": 1}}, dagents_runner.to_snake_case)
        self.assertEqual(converted, {"round_id": {"inner_key": 1}})


if __name__ == "__main__":
    unittest.main()
