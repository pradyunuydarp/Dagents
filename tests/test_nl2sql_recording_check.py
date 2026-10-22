"""`check_nl2sql` refuses a recording whose trace has a warning the recording does not explain.

The published NL2SQL page replays a recording. The capture runs model-service
only when torch is installed, and writes the services that did not run into
the recording's `planner` note ("...; not running during capture:
model-service"). A trace step that calls such a service is a warning in the
replay, and the note explains it. Any other warning means a framework call
failed during the capture, and the published page would show the integration
failing. The checker refuses that recording.
"""

from __future__ import annotations

import copy
from pathlib import Path
import sys
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_demo_recordings as checker  # noqa: E402  (needs the sys.path entry)

STEP_NAMES = (
    "Dagents SourceSpec validation",
    "Dagents model route planning",
    "core-service workload compile",
    "pipeline-service register and run",
    "model-service catalog",
    "GMA register NL2SQL LMA",
    "LMA source profile",
    "Dagents service status",
)
SERVICES = ("core-service", "pipeline-service", "model-service", "lma", "gma")


def recording(*, warnings: tuple[str, ...] = (), not_running: tuple[str, ...] = ()) -> dict:
    """A recording of two generations whose `warnings` steps failed."""
    note = "dagentsc at /usr/local/bin/dagentsc"
    if not_running:
        note += f"; not running during capture: {', '.join(not_running)}"
    trace = [
        {
            "name": name,
            "status": "warning" if name in warnings else "ok",
            "detail": "Connection refused" if name in warnings else "completed",
        }
        for name in STEP_NAMES
    ]
    generation = {
        "method": "POST",
        "path": "/api/v1/generate",
        "request": {"question": "How many critical incidents are open?", "model_id": "heuristic_demo"},
        "status": 200,
        "response": {"sql": "SELECT COUNT(*) FROM incidents", "dagents_trace": trace},
    }
    second = copy.deepcopy(generation)
    second["request"]["question"] = "List the open incidents."
    return {
        "captured_at": "2026-10-21T10:00:00Z",
        "commit": "0123456789abcdef",
        "planner": note,
        "entries": [
            {"method": "GET", "path": "/api/v1/samples", "status": 200, "response": [{"sample_id": "incident-count"}]},
            {
                "method": "GET",
                "path": "/api/v1/dagents/status",
                "status": 200,
                "response": {
                    "services": [
                        {"name": name, "status": "unavailable" if name in not_running else "ok"} for name in SERVICES
                    ]
                },
            },
            generation,
            second,
        ],
    }


class UnexplainedWarningTests(unittest.TestCase):
    def test_a_recording_without_warnings_passes(self) -> None:
        self.assertEqual([], checker.check_nl2sql(recording()))

    def test_a_warning_for_a_service_the_note_lists_passes(self) -> None:
        document = recording(warnings=("model-service catalog",), not_running=("model-service",))
        self.assertEqual([], checker.check_nl2sql(document))

    def test_a_warning_without_a_note_is_refused(self) -> None:
        problems = checker.check_nl2sql(recording(warnings=("model-service catalog",)))
        self.assertEqual(1, len(problems), problems)
        self.assertIn("model-service catalog", problems[0])
        self.assertIn("Connection refused", problems[0])

    def test_a_warning_for_another_service_than_the_note_lists_is_refused(self) -> None:
        document = recording(
            warnings=("model-service catalog", "pipeline-service register and run"), not_running=("model-service",)
        )
        problems = checker.check_nl2sql(document)
        self.assertEqual(1, len(problems), problems)
        self.assertIn("pipeline-service register and run", problems[0])

    def test_an_agent_step_is_matched_to_its_agent(self) -> None:
        """The steps name the agents in capitals ("GMA ..."); the status names them in lower case."""
        explained = recording(warnings=("GMA register NL2SQL LMA",), not_running=("gma",))
        self.assertEqual([], checker.check_nl2sql(explained))
        unexplained = recording(warnings=("GMA register NL2SQL LMA",), not_running=("lma",))
        self.assertEqual(1, len(checker.check_nl2sql(unexplained)))

    def test_a_planner_step_is_never_explained(self) -> None:
        """No service runs the planner steps; the capture refuses to run without the planner."""
        document = recording(warnings=("Dagents model route planning",), not_running=SERVICES)
        problems = [problem for problem in checker.check_nl2sql(document) if "trace step" in problem]
        self.assertEqual(1, len(problems), problems)
        self.assertIn("Dagents model route planning", problems[0])

    def test_each_unexplained_step_is_reported_once(self) -> None:
        """Both generations fail the same step; the message names it once."""
        problems = checker.check_nl2sql(recording(warnings=("core-service workload compile", "LMA source profile")))
        self.assertEqual(2, len(problems), problems)

    def test_the_checks_before_this_one_still_apply(self) -> None:
        document = recording()
        document["entries"][2]["response"]["sql"] = " "
        self.assertTrue(any("no SQL" in problem for problem in checker.check_nl2sql(document)))


if __name__ == "__main__":
    unittest.main()
