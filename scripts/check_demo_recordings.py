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
- the consortium charts were recorded after the pilot, in both themes;
- the answers show the Dagents agent calls behind them. Each hospital is
  served by an LMA and the study by a GMA, and every successful answer whose
  body is a JSON object lists the calls it made in ``framework_calls``. The
  overview, the framework status, every guard probe and every hospital
  dashboard list calls (a probe and a dashboard to their own hospital's LMA). The pilot lists the app's
  ``POST /api/v1/federation/rounds`` call to the GMA and, for every hospital,
  the GMA's ``POST /api/v1/federation/jobs:run`` call to its LMA (or
  ``jobs:execute``, after a separate offer). For every hospital, the pilot's
  round reports also hold a ``run`` row the hospital accepted, or an
  ``execute`` row, in ``dispatch``, which the pilot panel's dispatch table
  shows. The framework status says where the agents run (``agents.mode``);
- no ``framework_calls`` entry holds more than an agent, a method and a route
  template: no address, query string, token or body, because the recording
  is a public file.

``capture_demo_recordings.py`` runs the agent-call checks
(:func:`agent_call_problems`) itself and writes no recording that fails them.

The NL2SQL capture has to contain real SQL and a trace, and every trace step
must be ``ok``. The one exception is a step that calls a service the
recording's ``planner`` note lists as not running during the capture
(model-service, when the capture had no torch): the published page shows that
step as a warning, and the note says why.

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

#: Where the healthcare demo's agents can run, as ``GET /api/v1/framework/status``
#: reports it in ``agents.mode``. The page says which one in its "Who does what" table.
AGENT_MODES = ("in-process", "network")
#: The GMA route the app calls to run a federated round.
ROUNDS_ROUTE = "/api/v1/federation/rounds"
#: The LMA routes the GMA calls to run a hospital's job in a round: one
#: ``jobs:run`` call (the HTTP engine), or ``jobs:execute`` after a separate offer.
RUN_ROUTE = "/api/v1/federation/jobs:run"
EXECUTE_ROUTE = "/api/v1/federation/jobs:execute"
JOB_ROUTES = (RUN_ROUTE, EXECUTE_ROUTE)
#: The outcomes of a ``run`` dispatch row in which the hospital did not run the job.
NOT_RUN_OUTCOMES = frozenset({"refused", "failed"})
#: The fields of one ``framework_calls`` entry. Any other field could carry an
#: address, a token or a body.
CALL_FIELDS = frozenset({"service", "site_id", "method", "path", "status", "ms", "via"})


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
                "or show a deployment. Capture with HEALTHCARE_DEMO_DEPLOYER=off"
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

    problems.extend(agent_call_problems(document, site_ids))
    return problems


def site_ids_in(document: dict[str, Any]) -> list[str]:
    """The hospitals the captured overview lists, in its order; empty without an overview."""
    for entry in entries_for(document, "GET", "/api/v1/overview"):
        response = entry.get("response")
        if isinstance(response, dict) and isinstance(response.get("hospitals"), list):
            return [hospital["site_id"] for hospital in response["hospitals"] if isinstance(hospital, dict)]
    return []


def framework_calls(response: Any) -> list[dict[str, Any]]:
    """The agent calls an answer lists in ``framework_calls``; empty when it lists none."""
    calls = response.get("framework_calls") if isinstance(response, dict) else None
    return [call for call in calls if isinstance(call, dict)] if isinstance(calls, list) else []


def round_reports(response: Any) -> list[dict[str, Any]]:
    """Every round report in a pilot answer: ``rounds`` holds one per phase, and a list for the training rounds."""
    rounds = response.get("rounds") if isinstance(response, dict) else None
    if not isinstance(rounds, dict):
        return []
    reports: list[dict[str, Any]] = []
    for value in rounds.values():
        reports.extend(report for report in (value if isinstance(value, list) else [value]) if isinstance(report, dict))
    return reports


def calls_to_lma(response: Any, site_id: str | None) -> list[dict[str, Any]]:
    """The calls an answer lists to one hospital's LMA."""
    return [
        call for call in framework_calls(response) if call.get("service") == "lma" and call.get("site_id") == site_id
    ]


def call_shows_too_much(call: Any) -> bool:
    """Whether one ``framework_calls`` entry holds more than an agent, a method and a route template.

    A field outside :data:`CALL_FIELDS`, a path that is not a route template
    (it must start with one ``/`` and hold no query string), or any text
    holding ``://`` could put an address, a token or a body in the public file.
    """
    if not isinstance(call, dict) or set(call) - CALL_FIELDS:
        return True
    path = call.get("path")
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//") or "?" in path or "#" in path:
        return True
    return any(isinstance(value, str) and "://" in value for value in call.values())


def agent_call_problems(document: dict[str, Any], site_ids: list[str]) -> list[str]:
    """Where the capture fails to show the Dagents agent calls behind the demo's answers.

    Params:
    - `document`: the capture, ``{"entries": [...]}``.
    - `site_ids`: the hospitals the overview lists.

    Returns:
    - A list of problems; empty when every answer the page explains lists its
      agent calls and no call shows more than its agent, method and route.
      An answer that is missing altogether is reported by
      :func:`check_healthcare`, not here.
    """
    problems: list[str] = []

    status = entries_for(document, "GET", "/api/v1/framework/status")
    status_response = status[0].get("response") if status else None
    agents = status_response.get("agents") if isinstance(status_response, dict) else None
    mode = agents.get("mode") if isinstance(agents, dict) else None
    if mode not in AGENT_MODES:
        problems.append(
            f"the captured framework status does not say where the agents run (agents.mode is {mode!r}, "
            f"expected one of {', '.join(AGENT_MODES)}), so the 'Who does what' table could not say where they run"
        )

    for path, what in (("/api/v1/framework/status", "framework status"), ("/api/v1/overview", "overview")):
        if any(not framework_calls(entry.get("response")) for entry in _answered(document, path)):
            problems.append(
                f"the captured {what} lists no agent call in framework_calls, so the page would not show "
                "the agent calls behind it"
            )

    probes = entries_for(document, "POST", "/api/v1/governance:probe")
    silent = [
        probe for probe in probes
        if not calls_to_lma(probe.get("response"), (probe.get("request") or {}).get("site_id"))
    ]
    if silent:
        problems.append(
            f"{len(silent)} guard probe answers do not list the call to their hospital's LMA in framework_calls, "
            "so the guard panel would not show which agent decided"
        )

    for site_id in site_ids:
        dashboard = hospital_view_paths(site_id)[0]
        for phase, words in (("before_pilot", "before the pilot"), ("after_pilot", "after the pilot")):
            recorded = _answered(document, dashboard, phase=phase)
            if any(not calls_to_lma(entry.get("response"), site_id) for entry in recorded):
                problems.append(
                    f"the dashboard for {site_id} recorded {words} lists no call to its LMA in framework_calls"
                )

    pilots = entries_for(document, "POST", "/api/v1/pilot:run")
    if pilots:
        calls = framework_calls(pilots[0].get("response"))
        if not any(
            call.get("service") == "gma" and call.get("via") == "app"
            and call.get("method") == "POST" and call.get("path") == ROUNDS_ROUTE
            for call in calls
        ):
            problems.append(
                f"the captured pilot lists no call to POST {ROUNDS_ROUTE} at the GMA, so the page would not "
                "show the GMA running the rounds"
            )
        executed = {
            call.get("site_id")
            for call in calls
            if call.get("service") == "lma" and call.get("via") == "gma"
            and call.get("method") == "POST" and call.get("path") in JOB_ROUTES
        }
        missing = [site_id for site_id in site_ids if site_id not in executed]
        if missing:
            problems.append(
                f"the captured pilot lists no call from the GMA to POST {RUN_ROUTE} or {EXECUTE_ROUTE} at "
                f"{', '.join(missing)}, so the page would not show those hospitals running a job"
            )
        # The pilot panel's dispatch table reads the round reports, not framework_calls.
        ran = {
            row.get("site_id")
            for report in round_reports(pilots[0].get("response"))
            for row in report.get("dispatch") or []
            if isinstance(row, dict)
            and (
                row.get("step") == "execute"
                or (row.get("step") == "run" and row.get("outcome") not in NOT_RUN_OUTCOMES)
            )
        }
        not_run = [site_id for site_id in site_ids if site_id not in ran]
        if not_run:
            problems.append(
                f"the captured pilot's round reports have no run or execute row in dispatch for {', '.join(not_run)}, "
                "so the pilot panel's dispatch table would not show those hospitals running a job"
            )

    too_much = 0
    for entry in document["entries"]:
        response = entry.get("response")
        if not isinstance(response, dict) or "framework_calls" not in response:
            continue
        listed = response["framework_calls"]
        too_much += sum(1 for call in listed if call_shows_too_much(call)) if isinstance(listed, list) else 1
    if too_much:
        problems.append(
            f"{too_much} framework_calls entries show more than an agent, a method and a route "
            f"(only {', '.join(sorted(CALL_FIELDS))}, and no address, query string, token or body); "
            "the recording is a public file"
        )

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


#: Where the capture lists the services it could not start, at the end of the
#: recording's ``planner`` note: ``...; not running during capture: model-service``.
NOT_RUNNING_MARKER = "not running during capture:"


def services_not_running(note: str) -> set[str]:
    """The service names a recording's ``planner`` note lists as not running during the capture."""
    if NOT_RUNNING_MARKER not in note:
        return set()
    listed = note.split(NOT_RUNNING_MARKER, 1)[1]
    return {name.strip() for name in listed.split(",") if name.strip()}


def step_service(step_name: str, service_names: list[str]) -> str | None:
    """The service a trace step calls, or ``None`` for a planner step.

    A step that calls a service starts with that service's name, as the status
    check reports it: "pipeline-service register and run", "GMA register NL2SQL
    LMA" (the agents are written in capitals in the step names).
    """
    lowered = step_name.lower()
    return next((name for name in service_names if lowered.startswith(f"{name.lower()} ")), None)


def check_nl2sql(document: dict[str, Any]) -> list[str]:
    """Assert that the capture shows the framework integration.

    Every trace step must be ``ok``, except a step that calls a service the
    recording's note lists as not running during the capture (model-service,
    when the capture had no torch). Any other warning means a framework call
    failed while the capture ran, and the published page would show it.
    """
    problems: list[str] = []
    status_entries = entries_for(document, "GET", "/api/v1/dagents/status")
    service_names = [
        str(service.get("name", ""))
        for entry in status_entries[:1]
        for service in (entry["response"] or {}).get("services", [])
    ]
    not_running = services_not_running(str(document.get("planner", "")))
    unexplained: dict[str, str] = {}

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
        for step in response["dagents_trace"]:
            name = str(step.get("name", ""))
            if step.get("status") == "ok" or name in unexplained:
                continue
            if step_service(name, service_names) not in not_running:
                unexplained[name] = str(step.get("detail", ""))[:200]
    for name, detail in unexplained.items():
        problems.append(
            f"the trace step '{name}' is a warning ({detail}), and the recording's note does not list its "
            "service as not running during the capture, so a framework call failed while recording"
        )

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
