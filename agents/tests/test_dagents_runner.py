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
        self.assertEqual(command[:5], ["dagentsc", "dataset", "source", "validate", "--input"])
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
        self.assertEqual(command[:4], ["dagentsc", "dataset", "quality", "evaluate"])
        self.assertNotEqual(command[5], "-")
        self.assertNotEqual(command[7], "-")
        self.assertFalse("input" in run.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
