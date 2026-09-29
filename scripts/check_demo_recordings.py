#!/usr/bin/env python3
"""Fail when a captured demo recording would publish a page that proves nothing.

`capture_demo_recordings.py` can succeed and still produce a useless capture.
If every guard probe came back denied — which is what happens when the planner
is unreachable, because the Guard fails closed — the published demo would show
three levers that change nothing, and look fine doing it. If every probe came
back permitted, the cohort floor would appear not to exist.

So the deploy checks the capture's *content*, not just that a file was written:
the guard matrix has to contain both permits and denials, the strategies have to
actually differ across the levers, the pilot has to have reached its release
gates, and the NL2SQL capture has to contain real SQL and a trace.

Usage::

    scripts/check_demo_recordings.py            # check both demos
    scripts/check_demo_recordings.py healthcare # check one
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
RECORDINGS = {
    "healthcare": REPO_ROOT / "apps/healthcare-demo/frontend/public/recording.json",
    "nl2sql": REPO_ROOT / "services/nl2sql-demo/frontend/public/recording.json",
}


def load(path: Path) -> dict[str, Any]:
    """Read one capture, failing loudly if it is missing or empty."""
    if not path.is_file():
        raise SystemExit(f"No capture at {path}. Run scripts/capture_demo_recordings.py first.")
    document = json.loads(path.read_text(encoding="utf-8"))
    if not document.get("entries"):
        raise SystemExit(f"{path} holds no entries.")
    return document


def entries_for(document: dict[str, Any], method: str, path: str) -> list[dict[str, Any]]:
    """Every captured entry for one endpoint."""
    return [
        entry
        for entry in document["entries"]
        if entry["method"] == method and entry["path"] == path
    ]


def check_healthcare(document: dict[str, Any]) -> list[str]:
    """Assert the capture actually demonstrates the governance layer.

    Returns:
    - A list of problems; empty means the capture is worth publishing.
    """
    problems: list[str] = []

    probes = entries_for(document, "POST", "/api/v1/governance:probe")
    if len(probes) < 20:
        problems.append(f"only {len(probes)} guard probes captured; the lever matrix is incomplete")

    permitted = [probe for probe in probes if probe["response"].get("permitted")]
    denied = [probe for probe in probes if not probe["response"].get("permitted")]
    if not permitted:
        problems.append(
            "every captured guard probe was denied. That is what a capture looks like when the "
            "planner was unreachable and the Guard failed closed, and publishing it would show "
            "three levers that change nothing"
        )
    if not denied:
        problems.append(
            "no captured guard probe was denied, so the cohort floor never fired and the demo "
            "would not show the guard refusing anything"
        )

    # The strategies are the substance: if lowering trust or widening granularity
    # does not change them, the levers are decorative.
    strategies = {
        restriction["strategy"]
        for probe in probes
        for restriction in probe["response"].get("plan", {}).get("field_restrictions", [])
    }
    if len(strategies) < 3:
        problems.append(
            f"the capture contains only {len(strategies)} distinct field strategies "
            f"({sorted(strategies)}); the levers are supposed to change the strategy"
        )

    pilots = entries_for(document, "POST", "/api/v1/pilot:run")
    if not pilots:
        problems.append("no federated pilot was captured")
    else:
        release = pilots[0]["response"].get("release")
        if not release or not release.get("gates", {}).get("gate_results"):
            problems.append("the captured pilot never reached its release gates")

    overview = entries_for(document, "GET", "/api/v1/overview")
    if not overview or not overview[0]["response"].get("hospitals"):
        problems.append("the captured overview has no hospitals, so the consortium table is empty")

    worklists = [
        entry for entry in document["entries"] if "/worklist" in entry["path"]
    ]
    if not worklists:
        problems.append("no hospital worklist was captured")

    return problems


def check_nl2sql(document: dict[str, Any]) -> list[str]:
    """Assert the capture actually demonstrates the framework integration."""
    problems: list[str] = []

    generations = entries_for(document, "POST", "/api/v1/generate")
    if not generations:
        problems.append("no SQL generation was captured")
    for generation in generations:
        response = generation["response"]
        if not str(response.get("sql", "")).strip():
            problems.append("a captured generation returned no SQL")
            break
        if not response.get("dagents_trace"):
            problems.append("a captured generation returned an empty Dagents trace")
            break

    samples = entries_for(document, "GET", "/api/v1/samples")
    if not samples or not samples[0]["response"]:
        problems.append("no sample prompts were captured, so the demo opens with nothing to run")

    status = entries_for(document, "GET", "/api/v1/dagents/status")
    if not status:
        problems.append("no framework service status was captured")
    else:
        services = status[0]["response"].get("services", [])
        reachable = [service for service in services if service.get("status") == "ok"]
        if not reachable:
            problems.append(
                "no framework service was reachable during the capture, so the published trace "
                "would show the integration failing rather than working"
            )

    return problems


def main(argv: list[str]) -> int:
    """Check one demo's capture or both."""
    wanted = argv or list(RECORDINGS)
    unknown = [name for name in wanted if name not in RECORDINGS]
    if unknown:
        print(f"Unknown demo(s): {unknown}. Known: {sorted(RECORDINGS)}", file=sys.stderr)
        return 2

    failed = False
    for name in wanted:
        document = load(RECORDINGS[name])
        checker = check_healthcare if name == "healthcare" else check_nl2sql
        problems = checker(document)
        header = f"{name}: {len(document['entries'])} entries, captured at {document['captured_at']}"
        if problems:
            failed = True
            print(f"{header} — NOT PUBLISHABLE", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
        else:
            print(f"{header} — ok")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
