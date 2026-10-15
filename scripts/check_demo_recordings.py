#!/usr/bin/env python3
"""Fail when a captured demo recording would publish a page that shows nothing useful.

`capture_demo_recordings.py` can succeed and still produce a useless capture.
If the planner was unreachable, the Guard fails closed and every probe comes
back denied, so the published demo would show guard controls that change
nothing. If every probe came back permitted, the cohort minimum would never
show.

So the deploy checks the capture's content, not only that a file was written:

- the guard probes cover every hospital, with both permits and denials, and at
  least three different field strategies;
- no guard probe answer holds patient records. The consortium page is the
  coordinator's view, so a probe answer counts the records the Guard would
  release and names their fields, and the published file must not carry rows;
- the pilot reached its release gates;
- the recorded framework status says every hospital store is ready;
- the panel to add a hospital was recorded (`GET /api/v1/deployments`), from
  a server that offers no deployment and lists none, because a replay must
  not offer one or show one;
- every hospital has each request its pages send, at exactly the path and
  query string the UI uses, because the replay matches a request character
  for character: the dashboard, the worklist, the entered records, the audit
  log, the entry form and the chart list (the paths come from
  `capture_demo_recordings.hospital_view_paths`);
- every hospital has charts as SVG in both themes;
- the dashboard and the audit log were recorded again after the pilot;
- the consortium charts were recorded after the pilot, in both themes.

The NL2SQL capture has to contain real SQL and a trace.

Usage::

    scripts/check_demo_recordings.py            # check both demos
    scripts/check_demo_recordings.py healthcare # check one
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

# The capture script defines the paths the UI sends; both scripts must use the
# same ones. It imports only the standard library at module level.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from capture_demo_recordings import DEPLOYMENTS_PATH, after_pilot_hospital_paths, hospital_view_paths  # noqa: E402


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
        problems.append(f"only {len(probes)} guard probes captured; not every combination of the guard's controls was recorded")

    permitted = [probe for probe in probes if probe["response"].get("permitted")]
    denied = [probe for probe in probes if not probe["response"].get("permitted")]
    if not permitted:
        problems.append(
            "every captured guard probe was denied. That is what a capture looks like when the "
            "planner was unreachable and the Guard failed closed, and publishing it would show "
            "guard controls that change nothing"
        )
    if not denied:
        problems.append(
            "no captured guard probe was denied, so the cohort floor never fired and the demo "
            "would not show the guard refusing anything"
        )

    # The strategies are the substance: if lowering trust or widening granularity
    # does not change them, the controls show nothing.
    strategies = {
        restriction["strategy"]
        for probe in probes
        for restriction in probe["response"].get("plan", {}).get("field_restrictions", [])
    }
    if len(strategies) < 3:
        problems.append(
            f"the capture contains only {len(strategies)} distinct field strategies "
            f"({sorted(strategies)}); changing a control should change the strategy"
        )

    with_records = [probe for probe in probes if probe_records(probe.get("response"))]
    if with_records:
        problems.append(
            f"{len(with_records)} guard probe answers hold patient records. The consortium page is the "
            "coordinator's view, and a probe answer must give only released_count and released_fields"
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
        return problems
    site_ids = [hospital["site_id"] for hospital in overview[0]["response"]["hospitals"]]

    status = entries_for(document, "GET", "/api/v1/framework/status")
    if not status or "cohort_source" not in status[0]["response"]:
        problems.append("no framework status was captured, so the page cannot say where the records are stored")
    elif status[0]["response"].get("ready") is not True:
        problems.append(
            "the captured framework status says the hospital stores are not ready, so the page "
            "would open with a banner saying the records are not loaded"
        )

    deployments = entries_for(document, "GET", DEPLOYMENTS_PATH)
    if not any(entry.get("status") == 200 for entry in deployments):
        problems.append(
            f"the panel to add a hospital was not captured (GET {DEPLOYMENTS_PATH} with status 200), so the "
            "replay would say it was not recorded"
        )
    for entry in deployments:
        response = entry.get("response")
        if not isinstance(response, dict):
            continue
        if response.get("available") is not False or response.get("deployments"):
            problems.append(
                f"the captured GET {DEPLOYMENTS_PATH} offers or lists a deployment, and a replay must not offer "
                "or show a deployment. Capture from one process with HEALTHCARE_DEMO_DEPLOYER=off"
            )
            break

    probed = {probe.get("request", {}).get("site_id") for probe in probes}
    unprobed = [site_id for site_id in site_ids if site_id not in probed]
    if unprobed:
        problems.append(f"no guard probe was captured at {', '.join(unprobed)}")

    for site_id in site_ids:
        base = f"/api/v1/hospitals/{site_id}"
        dashboard, worklist, audit, entered, entry_form, chart_list = hospital_view_paths(site_id)
        if not _answered(document, dashboard):
            problems.append(f"no dashboard was captured for {site_id}")
        for path, what in (
            (worklist, "no worklist was captured"),
            (entered, "no entered records were captured"),
            (audit, "no audit log was captured"),
            (chart_list, "no chart list was captured"),
        ):
            if not _answered(document, path):
                problems.append(f"{what} for {site_id} (GET {path})")
        form = _answered(document, entry_form)
        if not form or not form[0]["response"].get("fields"):
            problems.append(f"no entry form was captured for {site_id}")
        for theme in ("light", "dark"):
            if not _svgs(document, f"{base}/charts/", theme):
                problems.append(f"no {theme} chart was captured for {site_id}")
        # After the pilot the replay serves these views from the entries
        # recorded after it, which hold what the pilot changed.
        after_dashboard, after_audit = after_pilot_hospital_paths(site_id)
        if not _answered(document, after_dashboard, phase="after_pilot"):
            problems.append(f"the dashboard for {site_id} was not captured after the pilot (GET {after_dashboard})")
        if not _answered(document, after_audit, phase="after_pilot"):
            problems.append(f"the audit log for {site_id} was not captured after the pilot (GET {after_audit})")

    after = [entry for entry in entries_for(document, "GET", "/api/v1/charts") if entry.get("phase") == "after_pilot"]
    if not after:
        problems.append("the consortium charts were not captured after the pilot")
    else:
        charts = after[0]["response"].get("charts", [])
        missing = [chart["chart_id"] for chart in charts if not chart.get("available")]
        if not charts or missing:
            problems.append(f"after the pilot, these consortium charts were not available: {missing or 'none listed'}")
        for chart in charts:
            for theme in ("light", "dark"):
                wanted = f"{chart['svg_path']}?theme={theme}"
                if not any(entry["path"] == wanted and entry.get("phase") == "after_pilot"
                           for entry in _svgs(document, chart["svg_path"], theme)):
                    problems.append(f"the consortium chart {chart['chart_id']} was not captured in the {theme} theme")

    return problems


def probe_records(response: Any) -> list[str]:
    """Where a probe answer holds records: a ``payload`` key, or a list of objects keyed by record fields.

    The record fields are the ones the answer names itself: each field the plan
    restricts, and the released, withheld and transformed fields.
    """
    if not isinstance(response, dict):
        return []
    names = {
        str(restriction.get("field"))
        for restriction in (response.get("plan") or {}).get("field_restrictions", [])
        if isinstance(restriction, dict)
    }
    for key in ("released_fields", "withheld_fields", "transformed_fields"):
        names.update(str(name) for name in response.get(key) or [])
    found = ["payload"] if "payload" in response else []

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                walk(item, f"{path}.{key}")
        elif isinstance(value, list):
            if any(isinstance(item, dict) and names & set(item) for item in value):
                found.append(path)
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")

    walk(response, "")
    return found


def _answered(document: dict[str, Any], path: str, phase: str | None = None) -> list[dict[str, Any]]:
    """Successful GETs recorded at exactly ``path``, optionally in one phase.

    The replay turns a recorded error status into an error on the page, so an
    entry with a status of 400 or more does not count.
    """
    return [
        entry
        for entry in entries_for(document, "GET", path)
        if entry.get("status", 200) < 400 and (phase is None or entry.get("phase") == phase)
    ]


def _svgs(document: dict[str, Any], prefix: str, theme: str) -> list[dict[str, Any]]:
    """Captured SVG responses whose path starts with ``prefix`` and asks for ``theme``."""
    found = []
    for entry in document["entries"]:
        if entry["method"] != "GET" or not entry["path"].startswith(prefix):
            continue
        if not entry["path"].endswith(f"?theme={theme}") or entry.get("status") != 200:
            continue
        response = entry.get("response")
        if entry.get("content_type") != "image/svg+xml" or not isinstance(response, dict):
            continue
        if "<svg" in str(response.get("text", "")):
            found.append(entry)
    return found


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
