"""Tests for the checks that run before a demo is published.

Two generated artifacts reach users directly: the shared design system, and the
recordings the published demos replay. Each has a check, and a check that stops
matching lets a broken page through while CI stays green. So each check is
tested against the problem it is meant to catch.

`capture_demo_recordings.py` can exit zero and still produce a capture whose
guard controls change nothing. A capture made without the planner looks like
that, because the Guard fails closed and denies every request. These tests
build such captures on purpose and assert that the checker refuses them. They
also compare the GET paths the healthcare UI sends with the paths the capture
records and the checker requires, because the replay matches a path exactly.

The healthcare demo calls a Dagents LMA per hospital and a GMA, and each JSON
answer lists those calls in ``framework_calls``. A capture whose answers list
no agent calls would publish a demo that hides the framework, so the checker
and the capture both refuse it.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import capture_demo_recordings as capture_script  # noqa: E402  (needs the sys.path entry)
import check_demo_recordings as guard  # noqa: E402
import sync_design_system  # noqa: E402


SITE = "uk-134"

ROUNDS = "/api/v1/federation/rounds"
OFFER = "/api/v1/federation/jobs:offer"
EXECUTE = "/api/v1/federation/jobs:execute"
RUN = "/api/v1/federation/jobs:run"


def call(service: str, site_id: str | None, method: str, path: str, via: str = "app", status: int = 200) -> dict:
    """One ``framework_calls`` entry, shaped as the backend writes it."""
    return {"service": service, "site_id": site_id, "method": method, "path": path,
            "status": status, "ms": 1.5, "via": via}


def lma_dashboard_calls(site_id: str = SITE) -> list[dict]:
    """The calls behind one hospital dashboard: all to that hospital's LMA."""
    return [call("lma", site_id, "GET", "/api/v1/site/summary"),
            call("lma", site_id, "POST", "/api/v1/site/model:evaluate")]


def pilot_calls(site_ids: tuple[str, ...] = (SITE,), *, two_calls: bool = False) -> list[dict]:
    """The calls behind one pilot: the app calls the GMA, and the GMA calls each LMA.

    The GMA's HTTP engine sends each LMA one ``jobs:run`` call per round;
    ``two_calls`` gives the ``jobs:offer`` then ``jobs:execute`` pair another
    engine may send.
    """
    calls = [call("gma", None, "POST", "/api/v1/federation/sites:refresh"), call("gma", None, "POST", ROUNDS)]
    for site_id in site_ids:
        if two_calls:
            calls += [call("lma", site_id, "POST", OFFER, via="gma"), call("lma", site_id, "POST", EXECUTE, via="gma")]
        else:
            calls += [call("lma", site_id, "POST", RUN, via="gma")]
    return calls + [call("gma", None, "POST", "/api/v1/federation/rounds/{round_id}:aggregate")]


def dispatch_rows(site_ids: tuple[str, ...] = (SITE,), *, two_calls: bool = False) -> list[dict]:
    """The GMA's calls to each LMA in one round, as a round report lists them in ``dispatch``."""
    rows: list[dict] = []
    for site_id in site_ids:
        if two_calls:
            rows += [{"site_id": site_id, "step": "offer", "status_code": 200, "ms": 2.0, "outcome": "accepted"},
                     {"site_id": site_id, "step": "execute", "status_code": 200, "ms": 9.0, "outcome": "completed"}]
        else:
            rows += [{"site_id": site_id, "step": "run", "status_code": 200, "ms": 11.0, "outcome": "completed"}]
    return rows


def pilot_rounds(site_ids: tuple[str, ...] = (SITE,)) -> dict:
    """The pilot's round reports, shaped as the backend returns them: one per phase, a list for training."""
    return {"evaluation": {"round_id": "ui-evaluation", "dispatch": dispatch_rows(site_ids)},
            "training": [{"round_id": "ui-training-1", "dispatch": dispatch_rows(site_ids)}]}


def probe(*, permitted: bool, strategy: str, cohort: int, granularity: str = "row", site_id: str = SITE) -> dict:
    """One captured guard probe, shaped as the backend returns it."""
    return {
        "method": "POST",
        "path": "/api/v1/governance:probe",
        "phase": "before_pilot",
        "content_type": "application/json",
        "request": {
            "site_id": site_id,
            "verified": True,
            "granularity": granularity,
            "cohort_size": cohort,
            "boundary": "before_read",
        },
        "status": 200,
        "response": {
            "site_id": site_id,
            "permitted": permitted,
            "plan": {"field_restrictions": [{"field": "systolic_bp", "strategy": strategy}]},
            "released_count": 5 if permitted else 0,
            "released_fields": ["systolic_bp"] if permitted else [],
            "withheld_fields": [] if permitted else ["systolic_bp"],
            "transformed_fields": [],
            "framework_calls": [call("lma", site_id, "POST", "/api/v1/governance/restrictions:probe")],
        },
    }


def json_entry(path: str, response, phase: str = "before_pilot", method: str = "GET") -> dict:
    return {"method": method, "path": path, "phase": phase, "content_type": "application/json",
            "status": 200, "response": response}


def svg_entry(path: str, theme: str, phase: str = "before_pilot") -> dict:
    return {
        "method": "GET",
        "path": f"{path}?theme={theme}",
        "phase": phase,
        "content_type": "image/svg+xml",
        "status": 200,
        "response": {"content_type": "image/svg+xml", "text": '<?xml version="1.0"?><svg role="img"></svg>'},
    }


def chart_listing(paths: list[str], available: bool = True) -> dict:
    return {"charts": [
        {"chart_id": path.rsplit("/", 1)[1].removesuffix(".svg"), "available": available, "svg_path": path}
        for path in paths
    ]}


CONSORTIUM_CHARTS = ["/api/v1/charts/hospital-records.svg", "/api/v1/charts/release-gates.svg"]
SITE_CHART = f"/api/v1/hospitals/{SITE}/charts/presentation.svg"
BASE = f"/api/v1/hospitals/{SITE}"
WORKLIST = f"{BASE}/worklist?limit={capture_script.WORKLIST_LIMIT}"
AUDIT = f"{BASE}/audit?limit={capture_script.AUDIT_LIMIT}"
ENTERED = f"{BASE}/encounters?origin=entered&limit={capture_script.ENTERED_LIMIT}"


def deployments_view(**changes) -> dict:
    """``GET /api/v1/deployments`` as the published demo answers it: the agents run in process, nothing is offered or tried."""
    return {
        "available": False,
        "reason": "This server runs the Dagents agents inside the API process.",
        "applier": "off",
        "hospitals": [
            {
                "site_id": "pl-290",
                "display_name": "Hospital 290",
                "country": "Poland",
                "records": 263,
                "records_source": "dataset",
                "in_consortium": False,
                "service": "unknown",
            }
        ],
        "deployments": [],
        "framework_calls": [],
        **changes,
    }


def healthy_healthcare_capture() -> dict:
    """A capture that is worth publishing, used as the baseline to break."""
    strategies = ["generalize:1", "redact", "aggregate_only:20", "clip_contribution:1"]
    probes = [
        probe(permitted=True, strategy=strategies[index % len(strategies)], cohort=25)
        for index in range(24)
    ]
    probes += [probe(permitted=False, strategy="deny", cohort=5) for _ in range(4)]
    base = BASE
    return {
        "captured_at": "2026-09-29T10:00:00Z",
        "commit": "abc123",
        "planner": "dagentsc",
        "entries": [
            json_entry(
                "/api/v1/framework/status",
                {"cohort_source": "in-memory", "ready": True,
                 "hospitals": [{"site_id": SITE, "record_count": 524, "labelled_count": 524}],
                 "agents": {"mode": "in-process", "engine": "http",
                            "lmas": [{"site_id": SITE, "reachable": True, "source_kind": "in-memory"}]},
                 "framework_calls": [call("gma", None, "GET", "/api/v1/health"),
                                     call("lma", SITE, "GET", "/api/v1/health")]},
            ),
            json_entry(
                "/api/v1/overview",
                {"hospitals": [{"site_id": SITE}],
                 "framework_calls": [call("gma", None, "GET", "/api/v1/federation/sites/summaries")]},
            ),
            json_entry(capture_script.DEPLOYMENTS_PATH, deployments_view()),
            json_entry(base, {"site_id": SITE, "framework_calls": lma_dashboard_calls()}),
            json_entry(WORKLIST, {"worklist": []}),
            json_entry(AUDIT, {"records": []}),
            json_entry(ENTERED, {"encounters": []}),
            json_entry(f"{base}/entry-form", {"fields": [{"name": "age"}]}),
            json_entry(f"{base}/charts", chart_listing([SITE_CHART])),
            svg_entry(SITE_CHART, "light"),
            svg_entry(SITE_CHART, "dark"),
            json_entry("/api/v1/charts", chart_listing(CONSORTIUM_CHARTS[:1])),
            *probes,
            {
                "method": "POST",
                "path": "/api/v1/pilot:run",
                "phase": "after_pilot",
                "content_type": "application/json",
                "request": {"round_prefix": "ui"},
                "status": 200,
                "response": {
                    "release": {"gates": {"gate_results": [{"gate_id": "fairness"}]}},
                    "rounds": pilot_rounds(),
                    "framework_calls": pilot_calls(),
                },
            },
            json_entry("/api/v1/charts", chart_listing(CONSORTIUM_CHARTS), phase="after_pilot"),
            *[svg_entry(path, theme, "after_pilot") for path in CONSORTIUM_CHARTS for theme in ("light", "dark")],
            json_entry(base, {"site_id": SITE, "framework_calls": lma_dashboard_calls()}, phase="after_pilot"),
            json_entry(AUDIT, {"records": [{"boundary": "before_send"}]}, phase="after_pilot"),
        ],
    }


def without(capture: dict, predicate) -> dict:
    capture["entries"] = [entry for entry in capture["entries"] if not predicate(entry)]
    return capture


def healthy_nl2sql_capture() -> dict:
    """The NL2SQL equivalent."""
    return {
        "captured_at": "2026-09-29T10:00:00Z",
        "commit": "abc123",
        "planner": "dagentsc",
        "entries": [
            {
                "method": "GET",
                "path": "/api/v1/samples",
                "status": 200,
                "response": [{"sample_id": "invoices"}],
            },
            {
                "method": "GET",
                "path": "/api/v1/dagents/status",
                "status": 200,
                "response": {"services": [{"name": "core-service", "status": "ok"}]},
            },
            {
                "method": "POST",
                "path": "/api/v1/generate",
                "request": {"question": "how many"},
                "status": 200,
                "response": {
                    "sql": "SELECT COUNT(*) FROM invoices",
                    "dagents_trace": [{"name": "Dagents SourceSpec validation", "status": "ok"}],
                },
            },
        ],
    }


class HealthcareCaptureTests(unittest.TestCase):
    """What the checker must refuse to publish."""

    def test_a_good_capture_passes(self) -> None:
        self.assertEqual([], guard.check_healthcare(healthy_healthcare_capture()))

    def test_an_all_denied_capture_is_refused(self) -> None:
        """The signature of a capture taken without the planner.

        The Guard fails closed, so with `dagentsc` unreachable every probe comes
        back denied, and the published demo would show guard controls that
        change nothing.
        """
        capture = healthy_healthcare_capture()
        for entry in capture["entries"]:
            if entry["path"] == "/api/v1/governance:probe":
                entry["response"]["permitted"] = False
                entry["response"]["plan"]["field_restrictions"][0]["strategy"] = "deny"
        problems = guard.check_healthcare(capture)
        self.assertTrue(any("every captured guard probe was denied" in p for p in problems))

    def test_a_capture_with_no_denial_is_refused(self) -> None:
        """If nothing is ever denied, the cohort floor appears not to exist."""
        capture = healthy_healthcare_capture()
        for entry in capture["entries"]:
            if entry["path"] == "/api/v1/governance:probe":
                entry["response"]["permitted"] = True
        problems = guard.check_healthcare(capture)
        self.assertTrue(any("no captured guard probe was denied" in p for p in problems))

    def test_uniform_strategies_are_refused(self) -> None:
        """Levers that do not change the strategy are decorative."""
        capture = healthy_healthcare_capture()
        for entry in capture["entries"]:
            if entry["path"] == "/api/v1/governance:probe":
                entry["response"]["plan"]["field_restrictions"][0]["strategy"] = "generalize:1"
        problems = guard.check_healthcare(capture)
        self.assertTrue(any("distinct field strategies" in p for p in problems))

    def test_a_thin_matrix_is_refused(self) -> None:
        capture = healthy_healthcare_capture()
        capture["entries"] = [
            entry for entry in capture["entries"] if entry["path"] != "/api/v1/governance:probe"
        ] + [probe(permitted=True, strategy="generalize:1", cohort=25)]
        problems = guard.check_healthcare(capture)
        self.assertTrue(any("not every combination of the guard's controls was recorded" in p for p in problems))

    def test_a_probe_answer_with_patient_records_is_refused(self) -> None:
        """The consortium page is the coordinator's view, so no probe answer may carry rows."""
        for key, rows in (
            ("payload", [{"systolic_bp": 130.0}]),
            ("payload", []),
            ("rows", [{"systolic_bp": 130.0, "age_band": "<50"}]),
        ):
            with self.subTest(key=key, rows=rows):
                capture = healthy_healthcare_capture()
                first = next(e for e in capture["entries"] if e["path"] == "/api/v1/governance:probe")
                first["response"][key] = rows
                problems = guard.check_healthcare(capture)
                self.assertTrue(any("guard probe answers hold patient records" in p for p in problems), problems)

    def test_the_record_finder_ignores_the_plan(self) -> None:
        answer = probe(permitted=True, strategy="redact", cohort=25)["response"]
        self.assertEqual([], guard.probe_records(answer))

    def test_the_capture_stops_when_a_probe_returns_records(self) -> None:
        recorder = mock.Mock()
        recorder.phase = None
        recorder.get.side_effect = lambda path, **_: (
            {"hospitals": [{"site_id": SITE}]} if path == "/api/v1/overview" else {"charts": []}
        )
        recorder.post.return_value = {"permitted": True, "payload": [{"systolic_bp": 130.0}]}
        recorder.entries = []
        with self.assertRaises(SystemExit) as stopped:
            capture_script.capture_healthcare(recorder)
        self.assertIn("payload", str(stopped.exception))

    def test_a_pilot_that_never_reached_its_gates_is_refused(self) -> None:
        capture = healthy_healthcare_capture()
        for entry in capture["entries"]:
            if entry["path"] == "/api/v1/pilot:run":
                entry["response"]["release"] = None
        problems = guard.check_healthcare(capture)
        self.assertTrue(any("never reached its release gates" in p for p in problems))

    def test_a_hospital_without_a_dashboard_is_refused(self) -> None:
        capture = without(healthy_healthcare_capture(), lambda e: e["path"] == f"/api/v1/hospitals/{SITE}")
        self.assertIn(f"no dashboard was captured for {SITE}", guard.check_healthcare(capture))

    def test_a_hospital_without_an_entry_form_is_refused(self) -> None:
        capture = without(healthy_healthcare_capture(), lambda e: e["path"].endswith("/entry-form"))
        self.assertIn(f"no entry form was captured for {SITE}", guard.check_healthcare(capture))

    def test_a_hospital_chart_missing_a_theme_is_refused(self) -> None:
        capture = without(healthy_healthcare_capture(), lambda e: e["path"] == f"{SITE_CHART}?theme=dark")
        self.assertIn(f"no dark chart was captured for {SITE}", guard.check_healthcare(capture))

    def test_a_chart_recorded_as_json_does_not_count(self) -> None:
        """The replay serves the stored text as SVG, so a JSON body there is a broken chart."""
        capture = healthy_healthcare_capture()
        for entry in capture["entries"]:
            if entry["path"] == f"{SITE_CHART}?theme=light":
                entry["content_type"] = "application/json"
                entry["response"] = {"detail": "not found"}
        self.assertIn(f"no light chart was captured for {SITE}", guard.check_healthcare(capture))

    def test_consortium_charts_must_be_captured_after_the_pilot(self) -> None:
        capture = without(
            healthy_healthcare_capture(),
            lambda e: e["path"].startswith("/api/v1/charts") and e.get("phase") == "after_pilot",
        )
        self.assertIn("the consortium charts were not captured after the pilot", guard.check_healthcare(capture))

    def test_an_unavailable_consortium_chart_after_the_pilot_is_refused(self) -> None:
        capture = healthy_healthcare_capture()
        for entry in capture["entries"]:
            if entry["path"] == "/api/v1/charts" and entry["phase"] == "after_pilot":
                entry["response"]["charts"][1]["available"] = False
        problems = guard.check_healthcare(capture)
        self.assertTrue(any("were not available: ['release-gates']" in p for p in problems), problems)

    def test_a_consortium_chart_missing_a_theme_is_refused(self) -> None:
        capture = without(
            healthy_healthcare_capture(), lambda e: e["path"] == "/api/v1/charts/release-gates.svg?theme=light"
        )
        self.assertIn(
            "the consortium chart release-gates was not captured in the light theme", guard.check_healthcare(capture)
        )

    def test_a_hospital_without_probes_is_refused(self) -> None:
        capture = healthy_healthcare_capture()
        capture["entries"][1]["response"]["hospitals"].append({"site_id": "it-002"})
        problems = guard.check_healthcare(capture)
        self.assertIn("no guard probe was captured at it-002", problems)
        self.assertIn("no dashboard was captured for it-002", problems)

    # The replay finds a response by its exact path and query string, so each
    # hospital-page request must be recorded at exactly the path the UI sends.

    def test_a_hospital_without_its_audit_log_is_refused(self) -> None:
        capture = without(healthy_healthcare_capture(), lambda e: e["path"] == AUDIT)
        self.assertIn(f"no audit log was captured for {SITE} (GET {AUDIT})", guard.check_healthcare(capture))

    def test_a_hospital_without_its_entered_records_is_refused(self) -> None:
        capture = without(healthy_healthcare_capture(), lambda e: e["path"] == ENTERED)
        self.assertIn(
            f"no entered records were captured for {SITE} (GET {ENTERED})", guard.check_healthcare(capture)
        )

    def test_a_worklist_recorded_with_another_limit_is_refused(self) -> None:
        capture = healthy_healthcare_capture()
        for entry in capture["entries"]:
            if entry["path"] == WORKLIST:
                entry["path"] = f"{BASE}/worklist?limit=99"
        self.assertIn(f"no worklist was captured for {SITE} (GET {WORKLIST})", guard.check_healthcare(capture))

    def test_a_hospital_without_its_chart_list_is_refused(self) -> None:
        """The page lists the charts first; without the list it draws none of the recorded SVGs."""
        capture = without(healthy_healthcare_capture(), lambda e: e["path"] == f"{BASE}/charts")
        self.assertIn(
            f"no chart list was captured for {SITE} (GET {BASE}/charts)", guard.check_healthcare(capture)
        )

    def test_a_failed_response_does_not_count(self) -> None:
        """The replay turns a recorded error status into an error on the page."""
        capture = healthy_healthcare_capture()
        for entry in capture["entries"]:
            if entry["path"] == WORKLIST:
                entry["status"] = 503
        self.assertIn(f"no worklist was captured for {SITE} (GET {WORKLIST})", guard.check_healthcare(capture))

    def test_the_audit_log_must_be_captured_after_the_pilot(self) -> None:
        """After the pilot the replay shows the after-pilot audit log, which holds the pilot's decisions."""
        capture = without(
            healthy_healthcare_capture(), lambda e: e["path"] == AUDIT and e.get("phase") == "after_pilot"
        )
        self.assertIn(
            f"the audit log for {SITE} was not captured after the pilot (GET {AUDIT})",
            guard.check_healthcare(capture),
        )

    def test_the_dashboard_must_be_captured_after_the_pilot(self) -> None:
        capture = without(
            healthy_healthcare_capture(), lambda e: e["path"] == BASE and e.get("phase") == "after_pilot"
        )
        self.assertIn(
            f"the dashboard for {SITE} was not captured after the pilot (GET {BASE})",
            guard.check_healthcare(capture),
        )

    def deployments_entry(self, capture: dict) -> dict:
        return next(entry for entry in capture["entries"] if entry["path"] == capture_script.DEPLOYMENTS_PATH)

    def test_a_capture_without_the_deploy_panel_is_refused(self) -> None:
        """The replay would say the panel to add a hospital was not recorded."""
        capture = without(healthy_healthcare_capture(), lambda e: e["path"] == capture_script.DEPLOYMENTS_PATH)
        problems = guard.check_healthcare(capture)
        self.assertTrue(any("add a hospital was not captured" in p for p in problems), problems)

    def test_a_failed_deploy_panel_answer_does_not_count(self) -> None:
        capture = healthy_healthcare_capture()
        self.deployments_entry(capture)["status"] = 404
        problems = guard.check_healthcare(capture)
        self.assertTrue(any("add a hospital was not captured" in p for p in problems), problems)

    def test_a_deploy_panel_that_offers_a_deployment_is_refused(self) -> None:
        """A replay that showed the button would offer a deployment it cannot run."""
        capture = healthy_healthcare_capture()
        self.deployments_entry(capture)["response"] = deployments_view(available=True, reason=None, applier="process")
        problems = guard.check_healthcare(capture)
        self.assertTrue(any("a replay must not offer or show a deployment" in p for p in problems), problems)

    def test_a_deploy_panel_that_lists_a_deployment_is_refused(self) -> None:
        """A replay must not show an attempt as though it happened in the visitor's session."""
        capture = healthy_healthcare_capture()
        attempt = {"deployment_id": "dep-0123456789ab", "site_id": "pl-290", "status": "joined", "steps": []}
        self.deployments_entry(capture)["response"] = deployments_view(deployments=[attempt])
        problems = guard.check_healthcare(capture)
        self.assertTrue(any("a replay must not offer or show a deployment" in p for p in problems), problems)

    def test_the_capture_records_the_deploy_panel_with_the_deployer_off(self) -> None:
        """The capture's server runs the agents in process with the deployer off, as the published demo does."""
        recorder = mock.Mock()
        recorder.phase = None
        recorded: list[tuple[str | None, str]] = []

        def get(path: str, **_: object):
            recorded.append((recorder.phase, path))
            if path == "/api/v1/overview":
                return {"hospitals": [{"site_id": SITE}]}
            return {"charts": []}

        recorder.get.side_effect = get
        recorder.post.return_value = {}
        recorder.entries = []
        capture_script.capture_healthcare(recorder)
        self.assertIn(("before_pilot", capture_script.DEPLOYMENTS_PATH), recorded)
        env = capture_script.HEALTHCARE_SERVICES[-1].env
        self.assertEqual("in-process", env["HEALTHCARE_DEMO_AGENTS"])
        self.assertEqual("off", env["HEALTHCARE_DEMO_DEPLOYER"])

    def test_a_status_that_says_the_stores_are_not_ready_is_refused(self) -> None:
        """The page would open with a banner saying the records are not loaded."""
        capture = healthy_healthcare_capture()
        capture["entries"][0]["response"]["ready"] = False
        problems = guard.check_healthcare(capture)
        self.assertTrue(any("not ready" in p for p in problems), problems)


def without_agent_calls(capture: dict) -> dict:
    """``capture`` with no agent call in it: a demo that hides the agents.

    ``framework_calls`` is removed from every answer, and ``dispatch`` from
    every round report of the pilot.
    """
    for entry in capture["entries"]:
        if isinstance(entry.get("response"), dict):
            entry["response"].pop("framework_calls", None)
            for report in all_round_reports(entry["response"]):
                report.pop("dispatch", None)
    return capture


def all_round_reports(response: dict) -> list[dict]:
    """Every round report in a pilot answer; empty for any other answer."""
    reports: list[dict] = []
    for value in (response.get("rounds") or {}).values():
        reports += value if isinstance(value, list) else [value]
    return reports


def entry_at(capture: dict, method: str, path: str, phase: str | None = None) -> dict:
    """The first entry for one request, optionally in one phase."""
    return next(
        entry for entry in capture["entries"]
        if entry["method"] == method and entry["path"] == path and (phase is None or entry.get("phase") == phase)
    )


class AgentCallTests(unittest.TestCase):
    """A capture must show the Dagents agent calls behind the demo's answers.

    Each hospital is served by an LMA and the study by a GMA. The page lists
    the calls behind each panel from ``framework_calls``, and the "Who does
    what" table says where the agents run from the status's ``agents.mode``.
    A capture without them would publish a demo that looks like it runs
    without the framework.
    """

    def test_a_capture_without_agent_calls_is_refused(self) -> None:
        problems = guard.check_healthcare(without_agent_calls(healthy_healthcare_capture()))
        for expected in (
            "guard probe answers do not list the call to their hospital's LMA",
            f"the dashboard for {SITE} recorded before the pilot lists no call to its LMA",
            f"the dashboard for {SITE} recorded after the pilot lists no call to its LMA",
            "the captured pilot lists no call to POST /api/v1/federation/rounds at the GMA",
            f"the captured pilot lists no call from the GMA to POST {RUN} or {EXECUTE} at {SITE}",
            f"the captured pilot's round reports have no run or execute row in dispatch for {SITE}",
            "the captured overview lists no agent call",
            "the captured framework status lists no agent call",
        ):
            with self.subTest(expected=expected):
                self.assertTrue(any(expected in problem for problem in problems), problems)

    def test_main_refuses_a_capture_without_agent_calls(self) -> None:
        """The deploy stops: the checker exits 1, not 0 with a warning."""
        with mock.patch.object(guard, "load", return_value=without_agent_calls(healthy_healthcare_capture())):
            self.assertEqual(1, guard.main(["healthcare"]))

    def test_a_probe_answer_without_its_lma_call_is_refused(self) -> None:
        """The guard panel's strip shows the call to the probed hospital's LMA, and only that one."""
        for calls in ([], [call("lma", "it-002", "POST", "/api/v1/governance/restrictions:probe")],
                      [call("gma", None, "GET", "/api/v1/health")]):
            with self.subTest(calls=calls):
                capture = healthy_healthcare_capture()
                entry_at(capture, "POST", "/api/v1/governance:probe")["response"]["framework_calls"] = calls
                problems = guard.check_healthcare(capture)
                self.assertTrue(
                    any("1 guard probe answers do not list the call to their hospital's LMA" in p for p in problems),
                    problems,
                )

    def test_a_dashboard_without_its_lma_call_is_refused(self) -> None:
        for phase, words in (("before_pilot", "before the pilot"), ("after_pilot", "after the pilot")):
            for calls in ([], [call("lma", "it-002", "GET", "/api/v1/site/summary")]):
                with self.subTest(phase=phase, calls=calls):
                    capture = healthy_healthcare_capture()
                    entry_at(capture, "GET", BASE, phase)["response"]["framework_calls"] = calls
                    self.assertIn(
                        f"the dashboard for {SITE} recorded {words} lists no call to its LMA in framework_calls",
                        guard.check_healthcare(capture),
                    )

    def test_a_pilot_without_a_round_dispatched_by_the_gma_is_refused(self) -> None:
        for calls in (
            [entry for entry in pilot_calls() if entry["path"] != ROUNDS],
            [call("gma", None, "GET", ROUNDS)] + [entry for entry in pilot_calls() if entry["path"] != ROUNDS],
        ):
            with self.subTest(calls=[entry["path"] for entry in calls]):
                capture = healthy_healthcare_capture()
                entry_at(capture, "POST", "/api/v1/pilot:run")["response"]["framework_calls"] = calls
                problems = guard.check_healthcare(capture)
                self.assertTrue(
                    any("the captured pilot lists no call to POST /api/v1/federation/rounds at the GMA" in p
                        for p in problems),
                    problems,
                )

    def test_a_pilot_without_a_job_call_at_every_hospital_is_refused(self) -> None:
        """Each hospital runs its jobs; the GMA's run (or execute) call to its LMA is the evidence."""
        job_paths = (RUN, EXECUTE)
        cases = {
            "no run call": [entry for entry in pilot_calls() if entry["path"] != RUN],
            "an offer only": [entry for entry in pilot_calls(two_calls=True) if entry["path"] != EXECUTE],
            "the app, not the GMA, made it": [
                {**entry, "via": "app"} if entry["path"] in job_paths else entry for entry in pilot_calls()
            ],
            "another hospital's LMA": [
                {**entry, "site_id": "it-002"} if entry["path"] in job_paths else entry for entry in pilot_calls()
            ],
        }
        for name, calls in cases.items():
            with self.subTest(name):
                capture = healthy_healthcare_capture()
                entry_at(capture, "POST", "/api/v1/pilot:run")["response"]["framework_calls"] = calls
                self.assertIn(
                    f"the captured pilot lists no call from the GMA to POST {RUN} or {EXECUTE} at {SITE}, "
                    "so the page would not show those hospitals running a job",
                    guard.check_healthcare(capture),
                )

    def test_an_offer_then_an_execute_call_is_also_evidence(self) -> None:
        """Another engine may send the two steps as two calls; the page shows those as well."""
        capture = healthy_healthcare_capture()
        pilot = entry_at(capture, "POST", "/api/v1/pilot:run")["response"]
        pilot["framework_calls"] = pilot_calls(two_calls=True)
        for report in all_round_reports(pilot):
            report["dispatch"] = dispatch_rows(two_calls=True)
        self.assertEqual([], guard.check_healthcare(capture))

    def test_every_hospital_needs_its_job_call(self) -> None:
        capture = healthy_healthcare_capture()
        capture["entries"][1]["response"]["hospitals"].append({"site_id": "it-002"})
        problems = guard.check_healthcare(capture)
        self.assertTrue(any(f"POST {RUN} or {EXECUTE} at it-002," in problem for problem in problems), problems)
        self.assertFalse(any(f"{EXECUTE} at {SITE}" in problem for problem in problems), problems)
        self.assertTrue(any("run or execute row in dispatch for it-002," in problem for problem in problems), problems)
        self.assertFalse(any(f"execute row in dispatch for {SITE}" in problem for problem in problems), problems)

    def test_a_pilot_whose_round_reports_show_no_job_run_is_refused(self) -> None:
        """The pilot panel's dispatch table reads each round report's ``dispatch`` rows, not ``framework_calls``."""
        offers_only = [row for row in dispatch_rows(two_calls=True) if row["step"] == "offer"]
        refused_run = [{**dispatch_rows()[0], "outcome": "refused"}]
        failed_run = [{**dispatch_rows()[0], "status_code": 503, "outcome": "failed"}]
        cases = {
            "no dispatch rows": None,
            "offers only": offers_only,
            "a run the hospital refused": refused_run,
            "a run call that failed": failed_run,
            "another hospital's job run": offers_only + [{**dispatch_rows()[0], "site_id": "it-002"}],
        }
        for name, rows in cases.items():
            with self.subTest(name):
                capture = healthy_healthcare_capture()
                for report in all_round_reports(entry_at(capture, "POST", "/api/v1/pilot:run")["response"]):
                    if rows is None:
                        del report["dispatch"]
                    else:
                        report["dispatch"] = rows
                self.assertIn(
                    f"the captured pilot's round reports have no run or execute row in dispatch for {SITE}, "
                    "so the pilot panel's dispatch table would not show those hospitals running a job",
                    guard.check_healthcare(capture),
                )

    def test_one_job_run_per_hospital_is_enough(self) -> None:
        """A hospital may refuse a round's job; the table then shows the refusal, which is worth publishing."""
        capture = healthy_healthcare_capture()
        rounds = entry_at(capture, "POST", "/api/v1/pilot:run")["response"]["rounds"]
        rounds["training"][0]["dispatch"] = [
            {"site_id": SITE, "step": "run", "status_code": 200, "ms": 2.0, "outcome": "refused"}
        ]
        self.assertEqual([], guard.check_healthcare(capture))

    def test_a_status_that_does_not_say_where_the_agents_run_is_refused(self) -> None:
        for agents in (None, {}, {"mode": ""}, {"mode": "somewhere"}):
            with self.subTest(agents=agents):
                capture = healthy_healthcare_capture()
                status = entry_at(capture, "GET", "/api/v1/framework/status")["response"]
                if agents is None:
                    del status["agents"]
                else:
                    status["agents"] = agents
                problems = guard.check_healthcare(capture)
                self.assertTrue(
                    any("the captured framework status does not say where the agents run" in p for p in problems),
                    problems,
                )

    def test_both_agent_modes_are_accepted(self) -> None:
        for mode in ("in-process", "network"):
            with self.subTest(mode=mode):
                capture = healthy_healthcare_capture()
                entry_at(capture, "GET", "/api/v1/framework/status")["response"]["agents"]["mode"] = mode
                self.assertEqual([], guard.check_healthcare(capture))

    def test_a_call_that_shows_an_address_or_a_secret_is_refused(self) -> None:
        """The recording is a public file: a call names its agent, method and route, nothing more."""
        cases = {
            "an address in the path": {"path": "http://127.0.0.1:8082/api/v1/site/summary"},
            "an internal host in the path": {"path": "//lma-uk-134.internal/api/v1/site/summary"},
            "a path that is not a route": {"path": "site/summary"},
            "a token": {"authorization": "Bearer abc"},
            "a request body": {"body": {"records": []}},
            "an address in another field": {"site_id": "uk-134@https://lma.example.com"},
        }
        for name, change in cases.items():
            with self.subTest(name):
                capture = healthy_healthcare_capture()
                charts = entry_at(capture, "GET", "/api/v1/charts")["response"]
                charts["framework_calls"] = [{**call("gma", None, "GET", "/api/v1/health"), **change}]
                problems = guard.check_healthcare(capture)
                self.assertTrue(
                    any("1 framework_calls entries show more than an agent, a method and a route" in p
                        for p in problems),
                    problems,
                )

    def test_a_response_without_agent_calls_is_fine_where_the_app_calls_no_agent(self) -> None:
        """The entry form is the app's own: it lists no call, and that is correct."""
        capture = healthy_healthcare_capture()
        entry_at(capture, "GET", f"{BASE}/entry-form")["response"]["framework_calls"] = []
        self.assertEqual([], guard.check_healthcare(capture))


class CaptureRequiresAgentCallsTests(unittest.TestCase):
    """The capture itself refuses to write a healthcare recording without the agent calls."""

    def run_capture(
        self, entries: list[dict], write: mock.Mock | None = None, **options: object
    ) -> tuple[mock.Mock, mock.Mock]:
        """Run ``capture`` with no processes: the walk records ``entries``. Returns the stack and writer mocks."""
        live = mock.Mock()
        live.missing = []
        write = write or mock.Mock()

        def walk(recorder) -> None:
            recorder.entries.extend(entries)

        with mock.patch.object(capture_script, "running_stack", return_value=live) as stack, \
                mock.patch.object(capture_script, "capture_healthcare", side_effect=walk), \
                mock.patch.object(capture_script, "write_recording", write), \
                contextlib.redirect_stdout(io.StringIO()):
            capture_script.capture("healthcare", "python", "dagentsc", **options)
        return stack, write

    def test_a_recording_without_agent_calls_is_not_written(self) -> None:
        entries = without_agent_calls(healthy_healthcare_capture())["entries"]
        write = mock.Mock()
        with self.assertRaises(SystemExit) as stopped:
            self.run_capture(entries, write)
        write.assert_not_called()
        self.assertIn("does not show the Dagents agent calls", str(stopped.exception))
        self.assertIn("POST /api/v1/federation/rounds", str(stopped.exception))

    def test_a_recording_with_agent_calls_is_written(self) -> None:
        _, write = self.run_capture(healthy_healthcare_capture()["entries"])
        write.assert_called_once()

    def test_require_agent_calls_names_every_problem(self) -> None:
        capture = without_agent_calls(healthy_healthcare_capture())
        del entry_at(capture, "GET", "/api/v1/framework/status")["response"]["agents"]
        with self.assertRaises(SystemExit) as stopped:
            capture_script.require_agent_calls(capture["entries"])
        message = str(stopped.exception)
        self.assertEqual(len(guard.agent_call_problems(capture, [SITE])), message.count("\n  - "))
        self.assertIn("guard probe answers do not list the call to their hospital's LMA", message)
        self.assertIn("agents.mode", message)

    def test_require_agent_calls_accepts_a_good_capture(self) -> None:
        capture_script.require_agent_calls(healthy_healthcare_capture()["entries"])

    def test_the_capture_runs_the_agents_inside_the_demo_server(self) -> None:
        """As the published demo does. Network mode would also need a database, which the capture does not use."""
        self.assertEqual("in-process", capture_script.HEALTHCARE_CAPTURE_ENV["HEALTHCARE_DEMO_AGENTS"])
        service = capture_script.HEALTHCARE_SERVICES[-1]
        self.assertEqual("in-process", service.env["HEALTHCARE_DEMO_AGENTS"])

    def test_the_backend_port_can_be_chosen(self) -> None:
        """So a capture can run next to other servers. The recorder calls the port the backend was started on."""
        stack, write = self.run_capture(healthy_healthcare_capture()["entries"], port=9850)
        services = stack.call_args.args[0]
        self.assertEqual(9850, services[-1].port)
        self.assertEqual("http://127.0.0.1:9850", write.call_args.args[1].base_url)

    def test_the_usual_port_is_the_default(self) -> None:
        stack, write = self.run_capture(healthy_healthcare_capture()["entries"])
        default = capture_script.HEALTHCARE_SERVICES[-1].port
        self.assertEqual(default, stack.call_args.args[0][-1].port)
        self.assertEqual(f"http://127.0.0.1:{default}", write.call_args.args[1].base_url)

    def test_main_passes_the_port_on(self) -> None:
        with mock.patch.object(capture_script, "planner_path", return_value="dagentsc"), \
                mock.patch.object(capture_script, "capture") as run, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, capture_script.main(["--demo", "healthcare", "--port", "9851"]))
        run.assert_called_once_with("healthcare", sys.executable, "dagentsc", port=9851)

    def test_a_relative_interpreter_path_is_made_absolute(self) -> None:
        """Each backend starts in its own directory, where a relative path would name no file.

        A virtual environment's interpreter is a symlink, so the path is made
        absolute without following it; following it would leave the venv.
        """
        for given, expected in (
            (".venv/bin/python", os.path.abspath(".venv/bin/python")),
            ("python3", "python3"),
            ("/usr/bin/python3", "/usr/bin/python3"),
        ):
            with self.subTest(given=given), \
                    mock.patch.object(capture_script, "planner_path", return_value="dagentsc"), \
                    mock.patch.object(capture_script, "capture") as run, \
                    contextlib.redirect_stdout(io.StringIO()):
                capture_script.main(["--demo", "healthcare", "--python", given])
                run.assert_called_once_with("healthcare", expected, "dagentsc", port=None)


def ui_get_paths(site_id: str) -> dict[str, str]:
    """Every GET path the healthcare UI sends, resolved for one hospital.

    Reads the calls to ``useResource`` and ``getJson`` in the frontend source.
    A page defines ``base`` as the hospital's path; ``${base}`` and the bare
    ``base`` argument resolve to it. Returns ``{path: file it came from}``.
    An argument this cannot resolve fails the test, so a new kind of call is
    noticed instead of skipped.
    """
    import re

    source = REPO_ROOT / "apps" / "healthcare-demo" / "frontend" / "src"
    call = re.compile(r"\b(?:useResource|getJson)\s*(?:<.*?>)?\(\s*(?:([`\"'])(.*?)\1|(base)\s*[,)])")
    base_definition = re.compile(r"const base = `([^`]*)`")
    hospital = f"/api/v1/hospitals/{site_id}"
    found: dict[str, str] = {}
    for path in sorted(source.rglob("*.ts*")):
        text = path.read_text(encoding="utf-8")
        relative = str(path.relative_to(REPO_ROOT))
        base = None
        definition = base_definition.search(text)
        if definition:
            if definition.group(1) != "/api/v1/hospitals/${encodeURIComponent(siteId)}":
                raise AssertionError(f"{relative} defines base as {definition.group(1)!r}; teach ui_get_paths it")
            base = hospital
        for match in call.finditer(text):
            if match.group(3):
                literal = "${base}"
            else:
                literal = match.group(2)
            if "${base}" in literal:
                if base is None:
                    raise AssertionError(f"{relative} uses ${{base}} without defining it")
                literal = literal.replace("${base}", base)
            if "${" in literal or not literal.startswith("/api/v1/"):
                raise AssertionError(f"{relative}: cannot resolve the GET path {literal!r}")
            found[literal] = relative
    return found


class ReplayCoversTheUiTests(unittest.TestCase):
    """The UI, the capture and the checker must agree on every GET path.

    The replay looks up each request by its exact `METHOD path?query`. If the
    UI changes a query string and the capture does not, the published hospital
    pages say the request was not recorded, while every check stays green.
    """

    def test_the_ui_paths_are_found(self) -> None:
        paths = ui_get_paths(SITE)
        self.assertIn(WORKLIST, paths)
        self.assertIn("/api/v1/overview", paths)

    def test_the_checker_requires_every_get_the_ui_sends(self) -> None:
        healthy = healthy_healthcare_capture()
        for path, source in ui_get_paths(SITE).items():
            with self.subTest(path=path, source=source):
                recorded = [entry for entry in healthy["entries"] if entry["method"] == "GET" and entry["path"] == path]
                self.assertTrue(recorded, f"the UI sends GET {path} ({source}) and the test capture lacks it")
                broken = without(
                    healthy_healthcare_capture(), lambda e, p=path: e["method"] == "GET" and e["path"] == p
                )
                self.assertTrue(
                    guard.check_healthcare(broken),
                    f"the UI sends GET {path} ({source}), and the checker accepts a capture without it",
                )

    def test_the_capture_records_every_hospital_path_the_ui_sends(self) -> None:
        hospital = f"/api/v1/hospitals/{SITE}"
        ui = {path for path in ui_get_paths(SITE) if path.startswith(hospital)}
        self.assertEqual(sorted(ui), sorted(capture_script.hospital_view_paths(SITE)))

    def test_capture_hospital_records_the_hospital_view_paths(self) -> None:
        recorder = mock.Mock()
        recorder.get.return_value = {"charts": []}
        capture_script.capture_hospital(recorder, SITE)
        recorded = [call.args[0] for call in recorder.get.call_args_list]
        self.assertEqual(sorted(recorded), sorted(capture_script.hospital_view_paths(SITE)))

    def test_capture_healthcare_records_the_after_pilot_paths(self) -> None:
        recorder = mock.Mock()
        recorder.phase = None
        phases: list[tuple[str | None, str]] = []

        def get(path: str, **_: object):
            phases.append((recorder.phase, path))
            if path == "/api/v1/overview":
                return {"hospitals": [{"site_id": SITE}]}
            return {"charts": []}

        recorder.get.side_effect = get
        recorder.post.return_value = {}
        recorder.entries = []
        capture_script.capture_healthcare(recorder)
        after = {path for phase, path in phases if phase == "after_pilot"}
        for path in capture_script.after_pilot_hospital_paths(SITE):
            with self.subTest(path=path):
                self.assertIn(path, after)


class RecorderTests(unittest.TestCase):
    """The capture stores a chart as text with its content type, instead of failing on it."""

    class FakeResponse:
        def __init__(self, status: int, content_type: str, text: str) -> None:
            self.status_code = status
            self.headers = {"content-type": content_type}
            self.text = text

        def json(self):
            return json.loads(self.text)

    def recorder(self, response):
        import capture_demo_recordings as capture

        client = mock.Mock()
        client.get.return_value = response
        client.post.return_value = response
        return capture.Recorder(client, "http://127.0.0.1:8080")

    def test_an_svg_is_stored_as_text(self) -> None:
        recorder = self.recorder(self.FakeResponse(200, "image/svg+xml", "<svg></svg>"))
        recorder.phase = "before_pilot"
        self.assertEqual("<svg></svg>", recorder.get("/api/v1/charts/x.svg?theme=dark"))
        entry = recorder.entries[0]
        self.assertEqual("image/svg+xml", entry["content_type"])
        self.assertEqual({"content_type": "image/svg+xml", "text": "<svg></svg>"}, entry["response"])
        self.assertEqual("before_pilot", entry["phase"])

    def test_json_is_decoded(self) -> None:
        recorder = self.recorder(self.FakeResponse(200, "application/json; charset=utf-8", '{"a": 1}'))
        self.assertEqual({"a": 1}, recorder.get("/api/v1/overview"))
        self.assertEqual("application/json", recorder.entries[0]["content_type"])
        self.assertNotIn("phase", recorder.entries[0])

    def test_a_failed_request_stops_the_capture(self) -> None:
        recorder = self.recorder(self.FakeResponse(404, "application/json", '{"detail": "no"}'))
        with self.assertRaises(SystemExit):
            recorder.get("/api/v1/charts/x.svg")


class Nl2sqlCaptureTests(unittest.TestCase):
    def test_a_good_capture_passes(self) -> None:
        self.assertEqual([], guard.check_nl2sql(healthy_nl2sql_capture()))

    def test_a_generation_with_no_sql_is_refused(self) -> None:
        capture = healthy_nl2sql_capture()
        capture["entries"][2]["response"]["sql"] = "   "
        self.assertTrue(any("no SQL" in p for p in guard.check_nl2sql(capture)))

    def test_an_empty_trace_is_refused(self) -> None:
        """The trace is the proof that the app consumes the framework."""
        capture = healthy_nl2sql_capture()
        capture["entries"][2]["response"]["dagents_trace"] = []
        self.assertTrue(any("empty Dagents trace" in p for p in guard.check_nl2sql(capture)))

    def test_a_capture_with_no_reachable_service_is_refused(self) -> None:
        capture = healthy_nl2sql_capture()
        capture["entries"][1]["response"]["services"][0]["status"] = "unavailable"
        problems = guard.check_nl2sql(capture)
        self.assertTrue(any("no framework service was reachable" in p for p in problems))


class CaptureFileTests(unittest.TestCase):
    """The loader's own failure modes."""

    def test_a_missing_capture_is_an_error(self) -> None:
        with self.assertRaises(SystemExit):
            guard.load(REPO_ROOT / "does-not-exist.json")

    def test_an_empty_capture_is_an_error(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recording.json"
            path.write_text(json.dumps({"entries": []}), encoding="utf-8")
            with self.assertRaises(SystemExit):
                guard.load(path)

    def test_main_reports_the_demo_it_refused(self) -> None:
        """A failing capture must fail the deploy, not warn and continue."""
        broken = healthy_healthcare_capture()
        for entry in broken["entries"]:
            if entry["path"] == "/api/v1/governance:probe":
                entry["response"]["permitted"] = False
        with mock.patch.object(guard, "load", return_value=broken):
            self.assertEqual(1, guard.main(["healthcare"]))


class DesignSystemTests(unittest.TestCase):
    """The three frontends must not drift apart visually."""

    def test_every_copy_matches_the_source(self) -> None:
        """Editing a copy instead of the source is the mistake this catches."""
        self.assertEqual(
            0,
            sync_design_system.main(["--check"]),
            "A design-system copy is stale. Run: python scripts/sync_design_system.py --write",
        )

    def test_every_frontend_has_a_copy(self) -> None:
        for relative in sync_design_system.COPIES:
            with self.subTest(copy=str(relative)):
                self.assertTrue((REPO_ROOT / relative).is_file())

    def test_a_copy_carries_the_do_not_edit_banner(self) -> None:
        for relative in sync_design_system.COPIES:
            with self.subTest(copy=str(relative)):
                text = (REPO_ROOT / relative).read_text(encoding="utf-8")
                self.assertIn("GENERATED FILE", text)

    def test_the_source_defines_every_token_on_bare_root(self) -> None:
        """A token defined only inside a dark block is the classic theme bug.

        It leaves one theme's text on the other theme's background, and it is
        invisible until someone opens the page in the theme nobody tested.
        """
        source = sync_design_system.SOURCE.read_text(encoding="utf-8")
        base, _, rest = source.partition("@media (prefers-color-scheme: dark)")
        self.assertTrue(rest, "the design system has no dark theme block")
        import re

        defined_in_base = set(re.findall(r"(--ds-[a-z0-9-]+):", base))
        defined_in_dark = set(re.findall(r"(--ds-[a-z0-9-]+):", rest))
        missing = sorted(defined_in_dark - defined_in_base)
        self.assertEqual(
            [],
            missing,
            f"these tokens are only defined in a dark block: {missing}",
        )


def workflow_step_script(text: str, name: str) -> str:
    """The shell body of one `run:` step, lifted out of a workflow.

    Deliberately not a YAML parse: PyYAML is not a declared dependency of this
    repository, and a guard that skips itself when an import is missing is
    green without having run. The slice is strict instead — a step that stops
    matching raises here rather than quietly testing nothing.
    """
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == f"- name: {name}")
    run = next(i for i in range(start, len(lines)) if lines[i].strip() == "run: |")
    indent = len(lines[run]) - len(lines[run].lstrip()) + 2
    body = []
    for line in lines[run + 1 :]:
        if line.strip() and not line.startswith(" " * indent):
            break
        body.append(line[indent:] if line.strip() else "")
    if not any(body):
        raise AssertionError(f"found no script under {name!r}")
    return "\n".join(body)


class ApiAddressResolutionTests(unittest.TestCase):
    """What the published demo is pointed at, and what it refuses to be.

    This runs the workflow's own resolution step rather than a copy of it. The
    case that matters is the third: a deploy hook pasted into the override
    variable instead of the service URL. Using it would publish a demo whose
    every request is rejected, and — because repository variables are not
    masked — print its key in a public log.
    """

    WORKFLOW = REPO_ROOT / ".github" / "workflows" / "pages.yml"
    STEP = "Resolve the deployed healthcare API"

    def resolve(self, override: str) -> tuple[str, str]:
        """Run the step with that override; return (resolved address, stdout)."""
        import subprocess
        import tempfile

        script = workflow_step_script(self.WORKFLOW.read_text(encoding="utf-8"), self.STEP)
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "env"
            summary = Path(tmp) / "summary"
            env_file.touch()
            summary.touch()
            proc = subprocess.run(
                ["bash", "-c", script],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                env={
                    "PATH": "/usr/bin:/bin",
                    "OVERRIDE": override,
                    "GITHUB_ENV": str(env_file),
                    "GITHUB_STEP_SUMMARY": str(summary),
                },
            )
            self.assertEqual(0, proc.returncode, proc.stderr)
            written = env_file.read_text(encoding="utf-8").strip()
        prefix = "HEALTHCARE_API_BASE="
        self.assertTrue(written.startswith(prefix), written)
        return written[len(prefix) :], proc.stdout

    def test_without_an_override_it_uses_the_committed_address(self) -> None:
        resolved, out = self.resolve("")
        self.assertTrue(resolved.startswith("https://"), resolved)
        self.assertIn("env/.env.published", out)

    def test_a_plain_override_wins(self) -> None:
        resolved, out = self.resolve("https://staging-api.example.com")
        self.assertEqual("https://staging-api.example.com", resolved)
        self.assertIn("repository variable", out)

    def test_an_override_carrying_a_key_is_refused_and_not_used(self) -> None:
        committed, _ = self.resolve("")
        resolved, out = self.resolve("https://api.render.com/sync/exs-abc?key=secret")
        self.assertEqual(committed, resolved, "a deploy hook was used as the API base")
        self.assertIn("::warning::", out)
        self.assertIn("rotate", out)

    def test_an_override_carrying_credentials_is_refused(self) -> None:
        committed, _ = self.resolve("")
        resolved, _ = self.resolve("https://user:pass@api.example.com")
        self.assertEqual(committed, resolved)


class LocalEnvFileTests(unittest.TestCase):
    """Local copies of an env file can hold a database password, so git ignores them.

    The committed env files (`env/.env.shared`, `env/.env.published` and the
    others) hold no secrets and stay tracked.
    """

    #: Copies a person might make: the healthcare example without `.example`,
    #: and Vite's local files.
    LOCAL_COPIES = (
        ".env.local",
        "env/.env.local",
        "apps/healthcare-demo/env/.env.healthcare-supabase",
        "apps/healthcare-demo/frontend/.env.local",
        "apps/healthcare-demo/frontend/.env.production.local",
        "site/.env.development.local",
    )
    #: The same copies, as paths inside the healthcare app once it is its own
    #: repository (`scripts/extract_repo.sh`), where only its own .gitignore applies.
    APP_LOCAL_COPIES = (
        ".env.local",
        "env/.env.healthcare-supabase",
        "frontend/.env.local",
        "frontend/.env.production.local",
    )

    def git(self, *arguments: str, cwd: Path = REPO_ROOT) -> "subprocess.CompletedProcess[str]":
        import subprocess

        if not (REPO_ROOT / ".git").exists():
            self.skipTest("not a git checkout")
        return subprocess.run(["git", *arguments], cwd=cwd, capture_output=True, text=True)

    def test_local_copies_are_ignored(self) -> None:
        for path in self.LOCAL_COPIES:
            with self.subTest(path=path):
                self.assertEqual(0, self.git("check-ignore", "-q", "--no-index", path).returncode,
                                 f"{path} is not ignored")

    def test_no_tracked_file_is_ignored(self) -> None:
        """A pattern broad enough to catch a tracked env file would hide its next change."""
        listing = self.git("ls-files", "-ci", "--exclude-standard")
        self.assertEqual(0, listing.returncode, listing.stderr)
        self.assertEqual("", listing.stdout)

    def test_the_extracted_app_ignores_its_local_copies(self) -> None:
        import shutil
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            self.git("init", "-q", str(repo))
            shutil.copy(REPO_ROOT / "apps" / "healthcare-demo" / ".gitignore", repo / ".gitignore")
            for path in self.APP_LOCAL_COPIES:
                with self.subTest(path=path):
                    result = self.git("check-ignore", "-q", "--no-index", path, cwd=repo)
                    self.assertEqual(0, result.returncode, f"{path} is not ignored in the extracted app")
            for tracked in ("env/.env.healthcare-demo", "env/.env.healthcare-supabase.example"):
                with self.subTest(tracked=tracked):
                    result = self.git("check-ignore", "-q", "--no-index", tracked, cwd=repo)
                    self.assertEqual(1, result.returncode, f"{tracked} would be ignored in the extracted app")


class LiveApiProbeTests(unittest.TestCase):
    """The Pages build's probe of the deployed API, run against a local stand-in.

    The probe decides whether the published demo calls the API or replays its
    recording. The stand-in answers like the API; each test changes one answer.
    """

    WORKFLOW = REPO_ROOT / ".github" / "workflows" / "pages.yml"
    STEP = "The live API is up and reading Postgres"
    ORIGIN = "https://example.github.io"

    def serve(self, status: dict) -> str:
        """Start a stand-in API that reports ``status``; return its base URL."""
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading

        bodies = {
            "/api/v1/health": {"status": "ok"},
            "/api/v1/framework/status": status,
            "/api/v1/overview": {"hospitals": [{"site_id": SITE}]},
            f"/api/v1/hospitals/{SITE}/worklist?limit=5": {"worklist": [{"encounter_id": "e1"}]},
        }

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - the name http.server calls
                body = bodies.get(self.path)
                payload = json.dumps(body if body is not None else {"detail": "Not Found"}).encode()
                self.send_response(200 if body is not None else 404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_address[1]}"

    def probe(self, status: dict) -> tuple[str, str]:
        """Run the step against the stand-in; return (what it wrote to GITHUB_ENV, the summary)."""
        import subprocess
        import tempfile

        script = workflow_step_script(self.WORKFLOW.read_text(encoding="utf-8"), self.STEP)
        api = self.serve(status)
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "env"
            summary = Path(tmp) / "summary"
            env_file.touch()
            summary.touch()
            proc = subprocess.run(
                ["bash", "-c", script],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                timeout=120,
                env={
                    "PATH": "/usr/bin:/bin",
                    "API": api,
                    "ORIGIN": self.ORIGIN,
                    "GITHUB_ENV": str(env_file),
                    "GITHUB_STEP_SUMMARY": str(summary),
                },
            )
            self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
            return env_file.read_text(encoding="utf-8"), summary.read_text(encoding="utf-8")

    def ready_status(self, **changes: object) -> dict:
        status = {
            "cohort_source": "postgres",
            "store_layout": "schema-per-site",
            "ready": True,
            "hospitals": [{"site_id": SITE, "record_count": 524, "labelled_count": 524}],
        }
        status.update(changes)
        return status

    def test_a_ready_api_is_published_live(self) -> None:
        written, summary = self.probe(self.ready_status())
        self.assertNotIn("HEALTHCARE_API_BASE=", written)
        self.assertIn("published live", summary)

    def test_an_api_whose_stores_are_not_ready_is_replayed(self) -> None:
        """Right after a deploy, before the data workflow has loaded every hospital."""
        written, summary = self.probe(self.ready_status(ready=False))
        self.assertIn("HEALTHCARE_API_BASE=\n", written)
        self.assertIn("not ready", summary)

    def test_an_api_that_cannot_read_its_stores_is_replayed(self) -> None:
        written, summary = self.probe(self.ready_status(ready=False, hospitals=None))
        self.assertIn("HEALTHCARE_API_BASE=\n", written)
        self.assertIn("cannot read", summary)

    def test_the_previous_api_version_is_replayed(self) -> None:
        """The synthetic-data API reports another source and no readiness."""
        written, summary = self.probe({"cohort_source": "supabase"})
        self.assertIn("HEALTHCARE_API_BASE=\n", written)
        self.assertIn("cohort_source=supabase", summary)


class ApiCheckReportTests(unittest.TestCase):
    """The Healthcare API check workflow fails when the API's stores are not ready.

    Runs the workflow's report script on saved responses instead of a live API.
    """

    WORKFLOW = REPO_ROOT / ".github" / "workflows" / "healthcare-api-check.yml"

    def report(self, status: dict) -> "subprocess.CompletedProcess[str]":
        import os
        import subprocess
        import tempfile

        step = self.WORKFLOW.read_text(encoding="utf-8").split("- name: What it says about itself", 1)[1]
        block = step.split("python3 - <<'PY'\n", 1)[1].split("\n          PY", 1)[0]
        code = "\n".join(line[10:] for line in block.splitlines())
        with tempfile.TemporaryDirectory() as tmp:
            status_path, overview_path = Path(tmp) / "status.json", Path(tmp) / "overview.json"
            status_path.write_text(json.dumps(status), encoding="utf-8")
            overview_path.write_text(json.dumps({"study_id": "stroke", "hospitals": []}), encoding="utf-8")
            code = code.replace("/tmp/status.json", str(status_path)).replace("/tmp/overview.json", str(overview_path))
            return subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True,
                text=True,
                env={**os.environ, "STATUS_CODE": "200", "OVERVIEW_CODE": "200"},
            )

    def status(self, **changes: object) -> dict:
        status = {
            "cohort_source": "postgres",
            "store_layout": "schema-per-site",
            "ready": True,
            "hospitals": [{"site_id": SITE, "record_count": 524, "labelled_count": 524}],
        }
        status.update(changes)
        return status

    def test_a_ready_api_passes(self) -> None:
        result = self.report(self.status())
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn("ready=True", result.stdout)

    def test_an_empty_store_fails_and_is_named(self) -> None:
        result = self.report(
            self.status(ready=False, hospitals=[{"site_id": SITE, "record_count": 0, "labelled_count": 0}])
        )
        self.assertEqual(1, result.returncode)
        self.assertIn(f"These hospital stores hold no records: {SITE}", result.stdout)

    def test_stores_that_cannot_be_read_fail(self) -> None:
        result = self.report(self.status(ready=False, hospitals=None))
        self.assertEqual(1, result.returncode)
        self.assertIn("cannot read the hospital stores", result.stdout)


class PublishedEndpointTests(unittest.TestCase):
    """`env/.env.published` holds the deployed addresses, and it is the only place.

    The Pages build needs the healthcare API's address, so the address is in the
    repository once, in this file. These tests check four things:

    - the file holds no credentials;
    - the Pages workflow still reads the key the file defines (a rename in only
      one of the two files would publish the demo as a replay);
    - the README links every published site;
    - the API's host appears in no other tracked file. The docs run the API
      locally, and the UI never shows the address.
    """

    PUBLISHED = REPO_ROOT / "env" / ".env.published"
    WORKFLOW = REPO_ROOT / ".github" / "workflows" / "pages.yml"
    # The workflows read the address from env/, so both directories may name it.
    API_HOST_ALLOWED_PREFIXES = ("env/", ".github/")

    def values(self) -> dict[str, str]:
        text = self.PUBLISHED.read_text(encoding="utf-8")
        pairs = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.partition("=")
            pairs[key.strip()] = value.strip()
        return pairs

    def test_the_healthcare_api_is_declared(self) -> None:
        url = self.values().get("HEALTHCARE_DEMO_PUBLIC_API_URL", "")
        self.assertTrue(url.startswith("https://"), f"expected an https URL, got {url!r}")
        self.assertFalse(
            url.endswith("/"),
            "a trailing slash doubles the separator in every URL the workflow builds",
        )

    def test_nothing_in_it_looks_like_a_credential(self) -> None:
        """The file is committed, so a secret here is a secret in every clone."""
        for key, value in self.values().items():
            with self.subTest(key=key):
                lowered = key.lower()
                self.assertFalse(
                    any(word in lowered for word in ("secret", "password", "token", "_key")),
                    f"{key} belongs in Actions secrets, not in a committed file",
                )
                self.assertNotIn("@", value, f"{key} looks like it carries credentials")

    def test_the_pages_workflow_reads_the_api_address(self) -> None:
        """The build has to resolve the one key it needs from this file."""
        workflow = self.WORKFLOW.read_text(encoding="utf-8")
        self.assertIn(
            "HEALTHCARE_DEMO_PUBLIC_API_URL",
            workflow,
            "the API address is declared but the Pages build does not read it, so the "
            "published demo would fall back to its recording",
        )

    def test_every_published_site_is_linked_from_the_readme(self) -> None:
        """A site that moves fails this test instead of leaving a dead link.

        No build reads the `*_SITE_URL` values: the workflow takes the base path
        from `configure-pages`. They are declared so that the README's links have
        one source, and this test compares the two.
        """
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        sites = {key: value for key, value in self.values().items() if key.endswith("_SITE_URL")}
        self.assertTrue(sites, "no published sites are declared")
        for key, value in sites.items():
            with self.subTest(key=key):
                self.assertIn(value, readme, f"{key} is {value} and the README does not link it")

    def api_host(self) -> str:
        from urllib.parse import urlsplit

        url = self.values().get("HEALTHCARE_DEMO_PUBLIC_API_URL", "")
        host = (urlsplit(url).hostname or "").lower()
        self.assertTrue(host, f"HEALTHCARE_DEMO_PUBLIC_API_URL has no host: {url!r}")
        return host

    def tracked_files(self) -> list[str]:
        """Tracked files, plus new files git does not ignore, relative to the root."""
        import subprocess

        if not (REPO_ROOT / ".git").exists():
            self.skipTest("not a git checkout, so there is no list of tracked files")
        proc = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=REPO_ROOT,
            capture_output=True,
            check=True,
        )
        return sorted({name for name in proc.stdout.decode("utf-8").split("\0") if name})

    def files_naming(self, host: str) -> list[str]:
        needle = host.encode("ascii")
        found = []
        for name in self.tracked_files():
            if name.startswith(self.API_HOST_ALLOWED_PREFIXES):
                continue
            path = REPO_ROOT / name
            try:
                data = path.read_bytes()
            except (FileNotFoundError, IsADirectoryError):
                continue  # deleted in the working tree, or a submodule
            if b"\0" in data[:8192]:
                continue  # binary
            if needle in data.lower():
                found.append(name)
        return found

    def test_the_api_host_appears_only_in_env_and_workflows(self) -> None:
        """The API's address lives in `env/.env.published` and nowhere else.

        The docs run the API locally, and the published page never shows the
        address. A host copied into a README or a component is a second place to
        update when the service moves, and it puts the address on public pages.
        """
        host = self.api_host()
        found = self.files_naming(host)
        self.assertEqual(
            [],
            found,
            f"{host} appears outside env/ and .github/. Keep the address only in "
            "env/.env.published; docs use the API run locally "
            "(apps/healthcare-demo/scripts/run_local_demo.sh).",
        )

    def test_the_healthcare_build_uses_the_resolved_value(self) -> None:
        """The probe clears the address when the API will not wake.

        It does that by rewriting `HEALTHCARE_API_BASE` in the job environment,
        which only works if the build step reads `env.` and not `vars.`. Reading
        the repository variable directly would publish a page pointing at a
        backend the same workflow just found to be down.
        """
        workflow = self.WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("VITE_HEALTHCARE_API_BASE: ${{ env.HEALTHCARE_API_BASE }}", workflow)


if __name__ == "__main__":
    unittest.main()
