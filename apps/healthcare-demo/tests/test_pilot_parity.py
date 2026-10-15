"""The pilot gives the same numbers as the pilot recorded at commit 6fb62ac.

The fixture ``fixtures/pilot_parity_6fb62ac.json`` holds the numbers of a pilot
run at commit ``6fb62ac``, when the demo still ran its own site and coordinator
code instead of the Dagents LMA and GMA APIs. A change to how the pilot is run
must not change those numbers. This test runs a pilot through the public route,
``POST /api/v1/pilot:run``, with in-memory stores and the real planner, and
compares:

- per round and hospital: participation, the code and privacy checks,
  contributed examples, metrics, the update norm and the update digest;
- per round: the selected sites, the quorum, the readiness decision and the
  planner's site weights, and the digests of the model and code the round ran;
- the candidate: its digest, metrics, weights and status;
- the baseline metrics of the model in use;
- the release gate results and action, and the release Guard's decision;
- the training curve.

Numbers must match to 1e-9. Digests must match exactly, so an update digest
also checks every coordinate of the update the hospital sent.

Ids that hold the pilot id or a timestamp are left out: pilot, round and job
ids, round and job digests (they cover the round id), candidate version names,
audit ids and times. The metric ``outcome_rate`` is compared as ``label_rate``,
the generic name the LMA's profile job may give it. The engine name and the
audit record counts are not compared, because running the pilot through the
LMA and GMA APIs changes where they come from.

To see the values of a fresh pilot in the fixture's format, run from
``apps/healthcare-demo``::

    DAGENTSC_BIN=... PYTHONPATH=../..:backend python -m tests.test_pilot_parity --print

Capture the fixture again only in a change that is meant to change the pilot's
numbers, and say so in the commit message.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
import sys
from typing import Any
import unittest

from tests.api_support import ApiTestCase
from tests.support import dagentsc_available

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "pilot_parity_6fb62ac.json"

#: Relative and absolute tolerance for every number.
TOLERANCE = 1e-9

#: Metric names compared under another name. The value is the name in the fixture.
METRIC_ALIASES = {"outcome_rate": "label_rate"}


def _metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    renamed: dict[str, Any] = {}
    for name, value in metrics.items():
        key = METRIC_ALIASES.get(name, name)
        if key in renamed:
            names = sorted(original for original in metrics if METRIC_ALIASES.get(original, original) == key)
            raise AssertionError(f"the metrics hold {names}, which are compared as one metric, {key!r}")
        renamed[key] = value
    return renamed


def _round(report: dict[str, Any] | None) -> dict[str, Any] | None:
    if report is None:
        return None
    plan = report["plan"]
    readiness = report["readiness"]
    return {
        "phase": report["phase"],
        "model_artifact_digest": report["model_artifact_digest"],
        "training_code_digest": report["training_code_digest"],
        "plan": {
            "quorum_met": plan["quorum_met"],
            "required_participants": plan["required_participants"],
            "selected_sites": sorted(plan["selected_sites"]),
            "excluded_sites": sorted(json.dumps(site, sort_keys=True) for site in plan["excluded_sites"]),
            "stop_reason": plan["stop_reason"],
        },
        "sites": {
            result["site_id"]: {
                "participation": result["participation"],
                "code_verified": result["code_verified"],
                "privacy_checks_passed": result["privacy_checks_passed"],
                "contributed_examples": result["contributed_examples"],
                "metrics": _metrics(result["metrics"]),
                "update_norm": result["update_norm"],
                "update_digest": result["update_digest"],
            }
            for result in report["results"]
        },
        "readiness": None
        if readiness is None
        else {
            "aggregation_permitted": readiness["aggregation_permitted"],
            "accepted_sites": sorted(readiness["accepted_sites"]),
            "accepted_examples": readiness["accepted_examples"],
            "site_weights": readiness["site_weights"],
            "rejected_contributions": sorted(
                json.dumps(item, sort_keys=True) for item in readiness["rejected_contributions"]
            ),
            "stop_reason": readiness["stop_reason"],
        },
    }


def _candidate(candidate: dict[str, Any] | None) -> dict[str, Any] | None:
    if candidate is None:
        return None
    return {
        "artifact_digest": candidate["artifact_digest"],
        "aggregation_method": candidate["aggregation_method"],
        "contributed_examples": candidate["contributed_examples"],
        "contributing_sites": sorted(candidate["contributing_sites"]),
        "site_weights": candidate["site_weights"],
        "metrics": _metrics(candidate["metrics"]),
        "status": candidate["status"],
    }


def _release(release: dict[str, Any] | None) -> dict[str, Any] | None:
    if release is None:
        return None
    gates = release["gates"]
    guard = release["guard"]
    return {
        "gates": {
            "action": gates["action"],
            "rollback_version": gates["rollback_version"],
            # The planner writes each failure as "<gate id>: <detail>".
            "blocking_failures": sorted(text.split(":", 1)[0] for text in gates["blocking_failures"]),
            "gate_results": {
                result["gate_id"]: {
                    "outcome": result["outcome"],
                    "observed": result["observed"],
                    "threshold": result["threshold"],
                }
                for result in gates["gate_results"]
            },
        },
        "guard": {
            "permitted": guard["permitted"],
            "boundary": guard["boundary"],
            "decision": guard["plan"]["decision"],
            "strategies": {item["field"]: item["strategy"] for item in guard["plan"]["field_restrictions"]},
            "payload": guard["payload"],
            "transformed_fields": sorted(guard["transformed_fields"]),
            "withheld_fields": sorted(guard["withheld_fields"]),
        },
    }


def _curve(curve: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "round": point["round"],
            "loss": point["loss"],
            "start_loss": point["start_loss"],
            "local_loss": point["local_loss"],
            "update_norms": point["update_norms"],
            "max_update_norm": point["max_update_norm"],
            "contributing_sites": sorted(point["contributing_sites"]),
            "contributed_examples": point["contributed_examples"],
        }
        for point in curve
    ]


def parity_view(report: dict[str, Any]) -> dict[str, Any]:
    """The parts of a pilot report that the fixture holds, with ids left out."""
    rounds = report["rounds"]
    named = {
        "analytics": rounds["analytics"],
        "evaluation": rounds["evaluation"],
        **{f"training-{number}": item for number, item in enumerate(rounds["training"], start=1)},
        "candidate_validation": rounds["candidate_validation"],
    }
    model_in_use = report["model_in_use"]
    return {
        "model_in_use": {key: model_in_use[key] for key in ("version", "kind", "digest", "status")},
        "training_settings": report["training_settings"],
        "rounds": {name: _round(item) for name, item in named.items()},
        "baseline_metrics": _metrics(report["baseline_metrics"]),
        "candidate": _candidate(report["candidate"]),
        "release": _release(report["release"]),
        "training_curve": _curve(report["training_curve"]),
        "stop_reason": report["stop_reason"],
    }


def differences(expected: Any, actual: Any, path: str = "") -> list[str]:
    """Every place where ``actual`` differs from ``expected``, as readable lines.

    Numbers match within :data:`TOLERANCE`; every other value must be equal.
    """
    if isinstance(expected, dict) and isinstance(actual, dict):
        found = []
        for key in sorted(set(expected) | set(actual)):
            if key not in actual:
                found.append(f"{path}/{key}: missing")
            elif key not in expected:
                found.append(f"{path}/{key}: not in the fixture")
            else:
                found.extend(differences(expected[key], actual[key], f"{path}/{key}"))
        return found
    if isinstance(expected, list) and isinstance(actual, list):
        if len(expected) != len(actual):
            return [f"{path}: {len(actual)} items, the fixture has {len(expected)}"]
        found = []
        for index, (left, right) in enumerate(zip(expected, actual, strict=True)):
            found.extend(differences(left, right, f"{path}[{index}]"))
        return found
    numbers = (int, float)
    if (
        isinstance(expected, numbers)
        and isinstance(actual, numbers)
        and not isinstance(expected, bool)
        and not isinstance(actual, bool)
    ):
        if math.isclose(expected, actual, rel_tol=TOLERANCE, abs_tol=TOLERANCE):
            return []
        return [f"{path}: {actual!r}, the fixture has {expected!r}"]
    if type(expected) is not type(actual) or expected != actual:
        return [f"{path}: {actual!r}, the fixture has {expected!r}"]
    return []


def load_fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["view"]


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class PilotParityTests(ApiTestCase):
    """One pilot through the public route, compared with the recorded pilot."""

    report: dict[str, Any]
    view: dict[str, Any]

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        try:
            response = cls.client.post("/api/v1/pilot:run", json={"round_prefix": "api"})
            if response.status_code != 200:
                raise AssertionError(f"the pilot failed with {response.status_code}: {response.text[:500]}")
            cls.report = response.json()
            cls.view = parity_view(cls.report)
        except BaseException:
            cls.tearDownClass()
            raise

    def assertMatches(self, expected: Any, actual: Any, path: str) -> None:
        found = differences(expected, actual, path)
        self.assertEqual([], found[:20], f"{len(found)} values differ from the recorded pilot")

    def test_the_records_come_from_the_in_memory_stores(self) -> None:
        """The fixture was recorded with in-memory stores loaded from the committed extract."""
        status = self.ok(self.client.get("/api/v1/framework/status"))
        self.assertEqual("in-memory", status["cohort_source"])

    def test_the_fixture_has_every_round(self) -> None:
        expected = load_fixture()
        self.assertEqual(sorted(expected["rounds"]), sorted(self.view["rounds"]))
        self.assertEqual(8, len(expected["rounds"]), "analytics, evaluation, five training rounds, validation")

    def test_each_round_matches_the_recorded_pilot(self) -> None:
        expected = load_fixture()["rounds"]
        for name, recorded in expected.items():
            with self.subTest(round=name):
                self.assertMatches(recorded, self.view["rounds"].get(name), f"/rounds/{name}")

    def test_the_model_in_use_and_settings_match(self) -> None:
        expected = load_fixture()
        self.assertMatches(expected["model_in_use"], self.view["model_in_use"], "/model_in_use")
        self.assertMatches(expected["training_settings"], self.view["training_settings"], "/training_settings")

    def test_the_baseline_matches(self) -> None:
        self.assertMatches(load_fixture()["baseline_metrics"], self.view["baseline_metrics"], "/baseline_metrics")

    def test_the_candidate_matches(self) -> None:
        self.assertMatches(load_fixture()["candidate"], self.view["candidate"], "/candidate")

    def test_the_release_gates_and_guard_match(self) -> None:
        self.assertMatches(load_fixture()["release"], self.view["release"], "/release")

    def test_the_training_curve_and_stop_reason_match(self) -> None:
        expected = load_fixture()
        self.assertMatches(expected["training_curve"], self.view["training_curve"], "/training_curve")
        self.assertEqual(expected["stop_reason"], self.view["stop_reason"])

    def test_the_whole_view_matches(self) -> None:
        """Nothing outside the sections above was added to or dropped from the view."""
        self.assertMatches(load_fixture(), self.view, "")


def _print_view() -> None:
    """Run one pilot as the test does and print its view in the fixture's format."""
    if not dagentsc_available():
        raise SystemExit("dagentsc was not found; set DAGENTSC_BIN")
    PilotParityTests.setUpClass()
    try:
        json.dump(PilotParityTests.view, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    finally:
        PilotParityTests.tearDownClass()


if __name__ == "__main__":
    if sys.argv[1:] == ["--print"]:
        _print_view()
    else:
        unittest.main()
