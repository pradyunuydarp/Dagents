"""A pilot-shaped study through the real GMA and three real LMAs, in both transports.

The LMA tests and the GMA tests each check one agent against stand-ins for the
other. This file checks them together, the way an application uses them: it
calls only the agents' HTTP APIs, through :class:`AgentNetwork`, and the GMA
reaches each LMA the same way.

The study is the toy extension's (``agents/tests/fixtures/toy_extension.py``):
three sites with 30 records each, a fixed threshold rule as the released model,
and a small logistic regression that the sites train. In order:

1. register the rule (released) and the regression's starting point (candidate);
2. refresh the sites: the GMA reads each LMA's registration;
3. an analytics round, then an evaluation round of the rule (the baseline);
4. three training rounds, each starting from the previous candidate, each
   discarded when it ends;
5. a validation round of the last candidate;
6. the release evaluation at the GMA (pack gates, round metrics, the
   ``before_release`` Guard, the lifecycle rule), then the approval;
7. the GMA lists the model versions and rounds, plans a round without
   dispatching it, and refuses a result that arrives late;
8. each site's staff read their site, its profile and the released model from
   their own LMA, evaluate the model locally, probe the Guard and read the job
   log; the GMA reports each site's summary and audit chain.

The same study runs twice: once with every agent in this process
(``AgentNetwork.in_process``) and once with each agent in its own uvicorn
process, started from ``LMA_*`` and ``GMA_*`` settings and reached over
loopback HTTP (``AgentNetwork.over_http``). Both runs pass the same
assertions, and their numbers must be equal, because the two transports are
two implementations of one behaviour.

Every Guard decision, round plan, readiness check, gate and lifecycle event
comes from the real ``dagentsc``; the tests skip without it.
"""

from __future__ import annotations

import json
import os
import random
import socket
import subprocess
import sys
import threading
import time
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from agents.common.domain.federation import (
    ApprovalResult,
    CandidateModel,
    ModelVersion,
    ModelVersionList,
    ReleaseEvaluation,
    ReleaseEvaluationRequest,
    RoundPlan,
    RoundRecord,
    SiteRegistration,
    UpdateUpload,
)
from agents.common.domain.governance import KyuAttribute, Requester
from agents.common.domain.site import (
    AuditSummary,
    JobLogEntry,
    LocalEvaluation,
    ModelInUse,
    ProbeRequest,
    ProbeResponse,
    SiteDirectoryEntry,
    SiteInfo,
    SourceState,
)
from agents.common.infrastructure.agent_client import (
    GMA_AGENT,
    AgentCall,
    AgentError,
    AgentNetwork,
    CallLog,
    lma_agent,
)
from agents.gma.app_factory import GmaDeps
from agents.gma.app_factory import create_app as create_gma
from agents.gma.config import Settings as GmaSettings
from agents.lma.app_factory import LmaDeps
from agents.lma.app_factory import create_app as create_lma
from agents.lma.config import Settings as LmaSettings
from agents.tests.fixtures.gma_fixtures import (
    ADMIN_TOKEN,
    COORDINATOR_TOKENS,
    INITIAL_VERSION,
    SITE_TOKENS,
    SITES,
    THRESHOLD_VERSION,
    json_keys,
    register_models,
    toy_manifest,
    toy_registry,
)
from agents.tests.fixtures.toy_extension import TOY_CONDITION_ID, TOY_LABEL_FIELD, toy_records, toy_site_source
from agents.tests.test_gma_release import COMMITTEE
from agents.tests.test_governance import dagentsc_available, dagentsc_binary

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The extension each agent process loads by name.
EXTENSION = "agents.tests.fixtures.toy_extension:ToyExtension"

#: The token each site's own staff use with their LMA (scope ``site``).
STAFF_TOKENS = {site: f"lma-{site}-staff-token-dddddddddddddddd" for site in SITES}

CAPABILITIES = ("profile", "evaluate", "train")
TRAINING_ROUNDS = 3
STUDY_ID = "toy-study"

#: Keys of a site's records. None may appear in a body an LMA sends the GMA.
RECORD_KEYS = {"x1", "x2", "group", TOY_LABEL_FIELD}

#: The loopback ports this file may use for the agent processes.
PORT_RANGE = range(9800, 9900)

#: Who a site's staff ask the Guard about in a probe: the study's coordinator.
PROBE_REQUESTER = Requester(
    requester_id=STUDY_ID,
    requester_kind="coordinator",
    affiliation=STUDY_ID,
    stated_purpose=TOY_CONDITION_ID,
    compliance_history=1.0,
    attributes=[
        KyuAttribute(attribute_id="round_manifest_digest", verified=True),
        KyuAttribute(attribute_id="training_code_digest", verified=True),
    ],
)


# ---------------------------------------------------------------------------
# The study, as an application runs it
# ---------------------------------------------------------------------------


@dataclass
class Outcome:
    """Everything one run of the study returned, through the agents' APIs."""

    registered: dict[str, str]
    enrolments: list[SiteRegistration]
    rounds: dict[str, RoundRecord] = field(default_factory=dict)
    final_rounds: dict[str, RoundRecord] = field(default_factory=dict)
    readiness: dict[str, Any] = field(default_factory=dict)
    candidates: dict[str, CandidateModel] = field(default_factory=dict)
    training_ids: list[str] = field(default_factory=list)
    release: ReleaseEvaluation | None = None
    approval: ApprovalResult | None = None
    versions: dict[str, ModelVersion] = field(default_factory=dict)
    models_in_use: dict[str, ModelInUse] = field(default_factory=dict)
    local_evaluations: dict[str, LocalEvaluation] = field(default_factory=dict)
    job_logs: dict[str, list[JobLogEntry]] = field(default_factory=dict)
    site_infos: dict[str, SiteInfo] = field(default_factory=dict)
    profiles: dict[str, dict[str, Any]] = field(default_factory=dict)
    refreshes: dict[str, SourceState] = field(default_factory=dict)
    probes: dict[str, ProbeResponse] = field(default_factory=dict)
    directory: list[SiteDirectoryEntry] = field(default_factory=list)
    coordinator_audit: AuditSummary | None = None
    version_list: ModelVersionList | None = None
    listed_rounds: list[str] = field(default_factory=list)
    plan_only: RoundPlan | None = None
    late_result: tuple[int | None, str] | None = None
    calls: list[AgentCall] = field(default_factory=list)


def run_study(network: AgentNetwork, prefix: str) -> Outcome:
    """Run the study through the agents' APIs, as the study operator and each site's staff would."""
    gma = network.gma(ADMIN_TOKEN)
    with CallLog() as log:
        outcome = Outcome(registered=register_models(gma), enrolments=gma.refresh_sites())

        def run(round_id: str, phase: str, version: str, digest: str, *, discard: bool = False) -> CandidateModel:
            try:
                outcome.rounds[round_id] = gma.dispatch_round(toy_manifest(round_id, phase, version, digest))
                outcome.readiness[round_id] = gma.readiness(round_id)
                candidate = gma.aggregate(round_id)
            finally:
                if discard:
                    gma.discard_round(round_id, "the round is finished")
            outcome.candidates[round_id] = candidate
            return candidate

        rule = outcome.registered[THRESHOLD_VERSION]
        run(f"{prefix}-analytics", "analytics", THRESHOLD_VERSION, rule)
        run(f"{prefix}-evaluation", "evaluation", THRESHOLD_VERSION, rule)

        version, digest = INITIAL_VERSION, outcome.registered[INITIAL_VERSION]
        for number in range(1, TRAINING_ROUNDS + 1):
            round_id = f"{prefix}-training-{number}"
            candidate = run(round_id, "training", version, digest, discard=True)
            outcome.training_ids.append(round_id)
            version, digest = candidate.candidate_version, candidate.artifact_digest
        validation_id = f"{prefix}-candidate-validation"
        run(validation_id, "evaluation", version, digest)

        outcome.release = gma.evaluate_release(
            outcome.training_ids[-1],
            ReleaseEvaluationRequest(
                candidate_version=version,
                condition_id=TOY_CONDITION_ID,
                metrics_from_round=validation_id,
                baseline_from_round=f"{prefix}-evaluation",
                change_from_version=INITIAL_VERSION,
                rollback_version=THRESHOLD_VERSION,
                release_requester=COMMITTEE,
                declared_purpose="toy_research",
            ),
        )
        outcome.approval = gma.approve(version)
        outcome.final_rounds = {round_id: gma.get_round(round_id) for round_id in outcome.rounds}
        for name in [THRESHOLD_VERSION, INITIAL_VERSION, *(outcome.candidates[r].candidate_version for r in outcome.training_ids)]:
            outcome.versions[name] = gma.get_model_version(name)
        outcome.version_list = gma.list_model_versions(limit=50)
        outcome.listed_rounds = [record.manifest.round_id for record in gma.list_rounds(limit=50)]
        released = outcome.approval.released
        outcome.plan_only = gma.plan_round(toy_manifest(f"{prefix}-plan-only", "evaluation", released.version, released.digest))
        # A result that arrives after its round was aggregated is refused.
        first = outcome.rounds[outcome.training_ids[0]]
        try:
            gma.submit_result(first.manifest.round_id, first.results[0])
        except AgentError as error:
            outcome.late_result = (error.status, error.code)

        for site in SITES:
            staff = network.lma(site, STAFF_TOKENS[site])
            outcome.site_infos[site] = staff.site_info()
            outcome.profiles[site] = staff.site_profile()
            outcome.refreshes[site] = staff.refresh_source()
            outcome.models_in_use[site] = staff.model_in_use()
            outcome.local_evaluations[site] = staff.evaluate_model()
            outcome.probes[site] = staff.probe(
                ProbeRequest(
                    boundary="before_read",
                    granularity="row",
                    requested_fields=["x1", "x2", "group", TOY_LABEL_FIELD],
                    record_limit=5,
                    requester=PROBE_REQUESTER,
                    declared_purpose=TOY_CONDITION_ID,
                )
            )
            outcome.job_logs[site] = staff.job_log(limit=50)
        outcome.directory = gma.site_summaries(include_audit=True)
        outcome.coordinator_audit = gma.audit_summary()
    outcome.calls = log.calls
    return outcome


def comparable(outcome: Outcome) -> dict[str, Any]:
    """The parts of an outcome that must not depend on the transport: no times, no audit ids.

    Compare two runs that used the same round ids.
    """
    rounds = {}
    for round_id, record in outcome.rounds.items():
        rounds[round_id] = {
            "status": record.status,
            "selected": record.plan.selected_sites,
            "round_digest": record.plan.round_digest,
            "results": [
                (r.site_id, r.participation, r.contributed_examples, r.metrics, r.update_norm, r.update_digest)
                for r in record.results
            ],
            "dispatch": sorted((call.site_id, call.step, call.status_code, call.outcome) for call in record.dispatch),
            "weights": outcome.readiness[round_id].site_weights,
            "final_status": outcome.final_rounds[round_id].status,
            "candidate": (
                outcome.candidates[round_id].candidate_version,
                outcome.candidates[round_id].artifact_digest,
                outcome.candidates[round_id].metrics,
                outcome.candidates[round_id].contributed_examples,
            ),
        }
    release = outcome.release
    assert release is not None and outcome.approval is not None and outcome.coordinator_audit is not None
    return {
        "registered": outcome.registered,
        "enrolments": [(e.site_id, e.cohort_size, e.capabilities, e.feature_contract_version) for e in outcome.enrolments],
        "rounds": rounds,
        "gates": [(g.gate_id, g.outcome, g.observed, g.threshold) for g in release.decision.gate_results],
        "action": release.decision.action,
        "guard": (release.guard.permitted if release.guard else None, release.candidate_status, release.baseline_version),
        "versions": {name: (v.kind, v.digest, v.status, v.parent_version) for name, v in outcome.versions.items()},
        "in_use": {site: (m.kind, m.digest, m.status) for site, m in outcome.models_in_use.items()},
        "local": {site: (e.labelled_count, e.metrics) for site, e in outcome.local_evaluations.items()},
        "job_logs": {
            site: [(e.phase, e.participation, e.contributed_examples, e.update_digest, e.metrics) for e in entries]
            for site, entries in outcome.job_logs.items()
        },
        "directory": [
            (d.site_id, d.reachable, d.summary.label_rate_status if d.summary else None, d.audit.record_count if d.audit else None)
            for d in outcome.directory
        ],
        "coordinator_audit": (outcome.coordinator_audit.chain_intact, outcome.coordinator_audit.record_count),
        "version_list": [(v.version, v.status, v.digest) for v in outcome.version_list.versions] if outcome.version_list else None,
        "model_in_use": outcome.version_list.model_in_use.version if outcome.version_list and outcome.version_list.model_in_use else None,
        "listed_rounds": outcome.listed_rounds,
        "plan_only": outcome.plan_only.model_dump() if outcome.plan_only else None,
        "late_result": outcome.late_result,
        "site_infos": {site: info.model_dump() for site, info in outcome.site_infos.items()},
        "profiles": outcome.profiles,
        "refreshes": {site: state.model_dump() for site, state in outcome.refreshes.items()},
        "probes": {
            site: (p.permitted, p.released_count, p.released_fields, p.withheld_fields, p.transformed_fields, p.plan.decision)
            for site, p in outcome.probes.items()
        },
        "calls": [(c.service, c.site_id, c.method, c.path, c.status) for c in outcome.calls],
    }


# ---------------------------------------------------------------------------
# What every run must show, in either transport
# ---------------------------------------------------------------------------


class Shared:
    """Holds the shared test class, so the loader does not run it on its own."""

    class StudyAssertions(unittest.TestCase):
        """What every run of the study must show; one subclass per transport sets ``outcome`` and ``prefix``."""

        outcome: Outcome
        prefix: str

        def round(self, name: str) -> RoundRecord:
            return self.outcome.rounds[f"{self.prefix}-{name}"]

        def test_every_site_enrols_from_its_own_registration(self) -> None:
            self.assertEqual([e.site_id for e in self.outcome.enrolments], list(SITES))
            for enrolment in self.outcome.enrolments:
                self.assertEqual(enrolment.cohort_size, len(toy_records(enrolment.site_id)))
                self.assertEqual(sorted(enrolment.capabilities), sorted(CAPABILITIES))

        def test_every_site_completes_every_round_and_results_are_sorted(self) -> None:
            self.assertEqual(len(self.outcome.rounds), 3 + TRAINING_ROUNDS)
            for round_id, record in self.outcome.rounds.items():
                with self.subTest(round=round_id):
                    self.assertEqual(record.plan.selected_sites, list(SITES))
                    self.assertEqual([r.site_id for r in record.results], list(SITES))
                    self.assertEqual({r.participation for r in record.results}, {"completed"})
                    self.assertEqual([a.site_id for a in record.acceptances], list(SITES))
                    self.assertTrue(all(a.accepted for a in record.acceptances))
                    rows = sorted((call.site_id, call.step, call.status_code, call.outcome) for call in record.dispatch)
                    expected = sorted(
                        [(site, "offer", 200, "accepted") for site in SITES]
                        + [(site, "execute", 200, "completed") for site in SITES]
                    )
                    self.assertEqual(rows, expected)
                    self.assertTrue(self.outcome.readiness[round_id].aggregation_permitted)
                    self.assertEqual(sorted(self.outcome.readiness[round_id].site_weights), list(SITES))

        def test_analytics_jobs_return_profile_metrics_and_no_update(self) -> None:
            for result in self.round("analytics").results:
                self.assertIn("record_count", result.metrics)
                self.assertIsNone(result.update_digest)

        def test_training_rounds_send_updates_to_the_gma_never_in_the_result(self) -> None:
            for round_id in self.outcome.training_ids:
                record = self.outcome.rounds[round_id]
                with self.subTest(round=round_id):
                    for result in record.results:
                        self.assertTrue(result.update_digest)
                        self.assertIsNotNone(result.update_norm)
                        self.assertNotIn("update", result.model_dump())
                    final = self.outcome.final_rounds[round_id]
                    # The release decision closes the round it was made for; the others stay aggregated.
                    expected = "closed" if round_id == self.outcome.training_ids[-1] else "aggregated"
                    self.assertEqual(final.status, expected)
                    self.assertEqual(final.abandoned_reason, "the round is finished")
                    self.assertIsNotNone(final.candidate)

        def test_each_training_round_starts_from_the_previous_candidate(self) -> None:
            parent = INITIAL_VERSION
            for round_id in self.outcome.training_ids:
                candidate = self.outcome.candidates[round_id]
                with self.subTest(round=round_id):
                    self.assertEqual(candidate.parent_version, parent)
                    self.assertEqual(candidate.candidate_version, f"toy-linear+{round_id}")
                    self.assertTrue(candidate.artifact_digest.startswith("sha256:"))
                parent = candidate.candidate_version

        def test_the_lifecycle_rule_supersedes_parents_and_releases_the_approved_candidate(self) -> None:
            last = self.outcome.candidates[self.outcome.training_ids[-1]].candidate_version
            statuses = {name: version.status for name, version in self.outcome.versions.items()}
            expected = {name: "superseded" for name in statuses}
            expected[last] = "released"
            self.assertEqual(statuses, expected)
            approval = self.outcome.approval
            assert approval is not None
            self.assertEqual(approval.released.version, last)
            self.assertEqual(approval.previous, THRESHOLD_VERSION)

        def test_the_release_is_decided_at_the_gma_from_the_rounds(self) -> None:
            release = self.outcome.release
            assert release is not None and release.guard is not None
            self.assertEqual(release.decision.action, "release")
            self.assertTrue(release.guard.permitted, release.guard.message)
            self.assertEqual(release.metrics_source, "rounds")
            self.assertEqual(release.baseline_version, THRESHOLD_VERSION)
            self.assertEqual(release.candidate_status, "candidate")
            validation = self.outcome.candidates[f"{self.prefix}-candidate-validation"]
            accuracy = next(g for g in release.decision.gate_results if g.gate_id == "accuracy")
            self.assertEqual(accuracy.observed, validation.metrics["accuracy"])
            # The release Guard is the coordinator chain's only decision.
            audit = self.outcome.coordinator_audit
            assert audit is not None
            self.assertEqual((audit.chain_intact, audit.record_count), (True, 1))

        def test_each_site_uses_and_evaluates_the_released_model(self) -> None:
            assert self.outcome.approval is not None
            released = self.outcome.approval.released
            for site in SITES:
                with self.subTest(site=site):
                    model = self.outcome.models_in_use[site]
                    self.assertEqual((model.version, model.digest, model.status), (released.version, released.digest, "released"))
                    evaluation = self.outcome.local_evaluations[site]
                    self.assertEqual(evaluation.version, released.version)
                    self.assertEqual(evaluation.labelled_count, len(toy_records(site)))
                    self.assertIn("accuracy", evaluation.metrics)

        def test_each_site_logs_every_job_newest_first(self) -> None:
            order = list(reversed(list(self.outcome.rounds)))
            for site in SITES:
                entries = self.outcome.job_logs[site]
                with self.subTest(site=site):
                    self.assertEqual([e.round_id for e in entries], order)
                    self.assertEqual({e.participation for e in entries}, {"completed"})
                    for entry in entries:
                        self.assertEqual(bool(entry.update_digest), entry.phase == "training")

        def test_the_directory_reports_each_site_and_its_audit_chain(self) -> None:
            self.assertEqual([d.site_id for d in self.outcome.directory], list(SITES))
            counts = set()
            for entry in self.outcome.directory:
                with self.subTest(site=entry.site_id):
                    self.assertTrue(entry.reachable)
                    assert entry.summary is not None and entry.audit is not None
                    self.assertEqual(entry.summary.record_count, len(toy_records(entry.site_id)))
                    self.assertEqual(entry.summary.label_rate_status, "released")
                    self.assertTrue(entry.audit.chain_intact)
                    # Three Guard decisions per job (before_train, before_read and
                    # before_send) and one for the staff's probe.
                    self.assertEqual(entry.audit.record_count, 3 * len(self.outcome.rounds) + 1)
                    counts.add(entry.audit.record_count)
            self.assertEqual(len(counts), 1)

        def test_the_call_log_holds_route_templates_and_no_address(self) -> None:
            calls = self.outcome.calls
            self.assertTrue(calls)
            for call in calls:
                with self.subTest(call=(call.method, call.path)):
                    self.assertTrue(call.path.startswith("/api/v1/"))
                    self.assertNotIn("://", call.path)
                    self.assertNotIn(self.prefix, call.path, "a round id in a path means it is not a template")
                    late = (call.method, call.path) == ("POST", "/api/v1/federation/rounds/{round_id}/results")
                    self.assertIn(call.status, (409,) if late else (200, 201, 202, 204))
            dispatches = [c for c in calls if (c.method, c.path) == ("POST", "/api/v1/federation/rounds")]
            self.assertEqual(len(dispatches), len(self.outcome.rounds))
            # The study operator calls only the GMA; staff call only their own LMA.
            self.assertEqual({c.site_id for c in calls if c.service == "lma"}, set(SITES))
            lma_paths = {c.path for c in calls if c.service == "lma"}
            self.assertEqual(
                lma_paths,
                {
                    "/api/v1/site",
                    "/api/v1/site/profile",
                    "/api/v1/site/source:refresh",
                    "/api/v1/site/model",
                    "/api/v1/site/model:evaluate",
                    "/api/v1/governance/restrictions:probe",
                    "/api/v1/federation/jobs",
                },
            )

        def test_the_registry_lists_versions_newest_first_with_the_model_in_use(self) -> None:
            listing = self.outcome.version_list
            assert listing is not None and listing.model_in_use is not None
            assert self.outcome.approval is not None
            released = self.outcome.approval.released
            self.assertEqual(listing.model_in_use.version, released.version)
            self.assertEqual({v.version for v in listing.versions}, set(self.outcome.versions))
            self.assertTrue(all(v.params is None for v in listing.versions))
            self.assertEqual(self.outcome.listed_rounds, list(reversed(list(self.outcome.rounds))))

        def test_a_plan_selects_the_enrolled_sites_without_dispatching(self) -> None:
            plan = self.outcome.plan_only
            assert plan is not None
            self.assertEqual(plan.selected_sites, list(SITES))
            self.assertTrue(plan.quorum_met)
            self.assertNotIn(plan.round_id, self.outcome.listed_rounds)

        def test_a_late_result_is_refused(self) -> None:
            self.assertEqual(self.outcome.late_result, (409, "conflict"))

        def test_staff_see_their_site_and_a_probe_returns_no_rows(self) -> None:
            for site in SITES:
                with self.subTest(site=site):
                    info = self.outcome.site_infos[site]
                    self.assertEqual((info.site_id, info.condition_id, info.label_field), (site, TOY_CONDITION_ID, TOY_LABEL_FIELD))
                    self.assertEqual(info.record_count, len(toy_records(site)))
                    self.assertNotIn("://", json.dumps(info.model_dump()))
                    self.assertEqual(self.outcome.refreshes[site].record_count, len(toy_records(site)))
                    self.assertEqual(self.outcome.profiles[site]["record_count"], len(toy_records(site)))
                    probe = self.outcome.probes[site]
                    self.assertNotIn("records", probe.model_dump())
                    self.assertNotIn("payload", probe.model_dump())
                    self.assertLessEqual(probe.released_count, 5)


# ---------------------------------------------------------------------------
# In process
# ---------------------------------------------------------------------------


class Exchanges:
    """An ASGI wrapper that keeps each request's bearer token and JSON bodies, both ways."""

    def __init__(self, app: Any) -> None:
        self._app = app
        self.items: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return
        headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers", [])}
        token = headers.get("authorization", "").removeprefix("Bearer ").strip()
        incoming: list[bytes] = []
        messages: list[Any] = []
        more = True
        while more:
            message = await receive()
            messages.append(message)
            incoming.append(message.get("body", b""))
            more = message.get("more_body", False)
        replay = iter(messages)

        async def replayed() -> Any:
            try:
                return next(replay)
            except StopIteration:
                return await receive()

        outgoing: list[bytes] = []
        status: list[int] = []

        async def sending(message: Any) -> None:
            if message["type"] == "http.response.start":
                status.append(message["status"])
            elif message["type"] == "http.response.body":
                outgoing.append(message.get("body", b""))
            await send(message)

        await self._app(scope, replayed, sending)
        with self._lock:
            self.items.append(
                {
                    "method": scope.get("method"),
                    "path": scope.get("path"),
                    "token": token,
                    "request": _json(b"".join(incoming)),
                    "status": status[0] if status else None,
                    "response": _json(b"".join(outgoing)),
                }
            )


def _json(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode("utf-8", "replace")


def build_in_process(wrap: Callable[[Any], Any] = Exchanges) -> tuple[AgentNetwork, Any, Any, dict[str, Any]]:
    """The GMA and one LMA per site, built with ``create_app`` and joined on one in-process network.

    ``wrap`` wraps each app before it joins the network, such as :class:`Exchanges`.
    Returns the network, the GMA app, the wrapped GMA and each wrapped LMA.
    """
    registry = toy_registry()
    network = AgentNetwork.in_process(thread_tokens=64, timeout_s=30.0, dispatch_timeout_s=60.0)
    try:
        gma_app = create_gma(
            GmaSettings(
                engine="http",
                admin_token=ADMIN_TOKEN,
                site_tokens={site: SITE_TOKENS[site] for site in SITES},
                lma_tokens={site: COORDINATOR_TOKENS[site] for site in SITES},
                allow_source_reads=False,
                summary_ttl_seconds=0.0,
                require_tokens=True,
            ),
            GmaDeps(registry=registry, network=network),
        )
        gma_exchanges = wrap(gma_app)
        network.add_app(GMA_AGENT, gma_exchanges)
        lma_exchanges = {}
        for site in SITES:
            lma_app = create_lma(
                LmaSettings(
                    site_id=site,
                    condition_id=TOY_CONDITION_ID,
                    study_id=STUDY_ID,
                    capabilities=CAPABILITIES,
                    site_token=STAFF_TOKENS[site],
                    coordinator_token=COORDINATOR_TOKENS[site],
                    gma_token=SITE_TOKENS[site],
                    require_tokens=True,
                ),
                # The same source the ``toy_static`` factory gives an LMA started from settings.
                LmaDeps(registry=registry, source=toy_site_source(None, "unused", site), network=network),
            )
            lma_exchanges[site] = wrap(lma_app)
            network.add_app(lma_agent(site), lma_exchanges[site])
    except Exception:
        network.close()
        raise
    return network, gma_app, gma_exchanges, lma_exchanges


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class InProcessStudyTests(Shared.StudyAssertions):
    """The study with every agent in this process, called through ``httpx.ASGITransport``."""

    prefix = "inproc"

    @classmethod
    def setUpClass(cls) -> None:
        cls.network, cls.gma_app, cls.gma_exchanges, cls.lma_exchanges = build_in_process()
        cls.addClassCleanup(cls.network.close)
        cls.outcome = run_study(cls.network, cls.prefix)

    def test_no_record_or_label_reaches_the_gma(self) -> None:
        """Every body an LMA sent the GMA, and every answer it gave the GMA, holds no record key."""
        received = []
        for site, exchanges in self.lma_exchanges.items():
            # What each LMA answered the GMA's calls with (the GMA uses that LMA's coordinator token).
            received += [(site, item["path"], item["response"]) for item in exchanges.items if item["token"] == COORDINATOR_TOKENS[site]]
        uploads = [item for item in self.gma_exchanges.items if item["token"] in SITE_TOKENS.values()]
        # What each LMA sent the GMA itself: its update uploads and its reads of the released model.
        received += [("upload", item["path"], item["request"]) for item in uploads]
        self.assertGreater(len(received), 3 * len(self.outcome.rounds))
        for source, path, body in received:
            with self.subTest(source=source, path=path):
                self.assertEqual(json_keys(body) & RECORD_KEYS, set())
        upload_paths = sorted(item["path"] for item in uploads if item["method"] == "PUT")
        self.assertEqual(len(upload_paths), TRAINING_ROUNDS * len(SITES))
        for item in uploads:
            if item["method"] == "PUT":
                UpdateUpload.model_validate(item["request"])

    def test_the_app_never_receives_an_update_from_the_gma(self) -> None:
        answers = [item for item in self.gma_exchanges.items if item["token"] == ADMIN_TOKEN]
        self.assertTrue(answers)
        for item in answers:
            with self.subTest(path=item["path"]):
                self.assertNotIn("update", json_keys(item["response"]))
        # The GMA's update store keeps nothing once the training rounds are discarded.
        self.assertEqual(self.gma_app.state.gma.updates.pending_rounds(), [])

    def test_tokens_keep_each_caller_to_its_own_routes(self) -> None:
        site, other = SITES[0], SITES[1]
        record = self.outcome.rounds[self.outcome.training_ids[0]]
        cases = [
            # A site's staff token cannot offer jobs at its own LMA.
            (lambda: self.network.lma(site, STAFF_TOKENS[site]).registration(), 403),
            # The GMA's admin token means nothing to an LMA.
            (lambda: self.network.lma(site, ADMIN_TOKEN).site_summary(), 401),
            # An LMA's coordinator token means nothing at the GMA.
            (lambda: self.network.gma(COORDINATOR_TOKENS[site]).list_rounds(), 401),
            # A site's upload token is bound to that site.
            (
                lambda: self.network.gma(SITE_TOKENS[site]).upload_update(
                    record.manifest.round_id, other, UpdateUpload(job_digest=record.plan.round_digest, update={"bias": 0.0})
                ),
                403,
            ),
            # A site's upload token cannot run the study.
            (lambda: self.network.gma(SITE_TOKENS[site]).list_rounds(), 403),
            # No token at all.
            (lambda: self.network.gma().list_rounds(), 401),
        ]
        for number, (call, status) in enumerate(cases):
            with self.subTest(case=number):
                with self.assertRaises(AgentError) as caught:
                    call()
                self.assertEqual(caught.exception.status, status)


# ---------------------------------------------------------------------------
# Over HTTP: one uvicorn process per agent, started from its settings
# ---------------------------------------------------------------------------


def _bind_loopback_ports(count: int) -> list[socket.socket]:
    """Bind ``count`` listening sockets on free loopback ports in :data:`PORT_RANGE`.

    The sockets are handed to uvicorn with ``--fd``, so no other process can
    take a port between choosing it and serving on it.
    """
    sockets: list[socket.socket] = []
    ports = list(PORT_RANGE)
    random.shuffle(ports)
    for port in ports:
        if len(sockets) == count:
            break
        candidate = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            candidate.bind(("127.0.0.1", port))
            candidate.listen(64)
        except OSError:
            candidate.close()
            continue
        sockets.append(candidate)
    if len(sockets) < count:
        for bound in sockets:
            bound.close()
        raise RuntimeError(f"fewer than {count} free loopback ports in {PORT_RANGE.start}-{PORT_RANGE.stop - 1}")
    return sockets


def _site_map(values: dict[str, str]) -> str:
    return ",".join(f"{site}={value}" for site, value in values.items())


@dataclass
class AgentProcesses:
    """The four agent processes and the network that reaches them."""

    network: AgentNetwork
    processes: list[subprocess.Popen]
    logs: list[Path]

    def stop(self) -> None:
        self.network.close()
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
        for process in self.processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)


def start_over_http(log_dir: Path) -> AgentProcesses:
    """Start the GMA and one LMA per site with uvicorn, each from its environment settings."""
    sockets = _bind_loopback_ports(1 + len(SITES))
    ports = [bound.getsockname()[1] for bound in sockets]
    gma_url = f"http://127.0.0.1:{ports[0]}"
    lma_urls = {site: f"http://127.0.0.1:{port}" for site, port in zip(SITES, ports[1:])}
    base = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("LMA_", "GMA_")) and key not in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
    }
    base.update(
        PYTHONPATH=str(REPO_ROOT),
        DAGENTSC_BIN=dagentsc_binary(),
        NO_PROXY="127.0.0.1,localhost",
        no_proxy="127.0.0.1,localhost",
    )
    environments = [
        (
            "agents.gma.main:app",
            {
                "GMA_ENGINE": "http",
                "GMA_EXTENSIONS": EXTENSION,
                "GMA_SITE_ENDPOINTS": _site_map(lma_urls),
                "GMA_ADMIN_TOKEN": ADMIN_TOKEN,
                "GMA_SITE_TOKENS": _site_map({site: SITE_TOKENS[site] for site in SITES}),
                "GMA_LMA_TOKENS": _site_map({site: COORDINATOR_TOKENS[site] for site in SITES}),
                "GMA_ALLOW_SOURCE_READS": "0",
                "GMA_SUMMARY_TTL_SECONDS": "0",
                "GMA_REQUIRE_TOKENS": "1",
            },
        )
    ]
    for site in SITES:
        environments.append(
            (
                "agents.lma.main:app",
                {
                    "LMA_SITE_ID": site,
                    "LMA_CONDITION_ID": TOY_CONDITION_ID,
                    "LMA_STUDY_ID": STUDY_ID,
                    "LMA_CAPABILITIES": ",".join(CAPABILITIES),
                    "LMA_EXTENSIONS": EXTENSION,
                    "LMA_SITE_SOURCE": "toy_static",
                    "LMA_SITE_TOKEN": STAFF_TOKENS[site],
                    "LMA_COORDINATOR_TOKEN": COORDINATOR_TOKENS[site],
                    "LMA_GMA_ENDPOINT": gma_url,
                    "LMA_GMA_TOKEN": SITE_TOKENS[site],
                    "LMA_REQUIRE_TOKENS": "1",
                },
            )
        )
    processes: list[subprocess.Popen] = []
    logs: list[Path] = []
    try:
        for (target, settings), bound in zip(environments, sockets):
            log = log_dir / f"{target.split('.')[1]}-{bound.getsockname()[1]}.log"
            logs.append(log)
            with log.open("wb") as handle:
                processes.append(
                    subprocess.Popen(
                        [sys.executable, "-m", "uvicorn", target, "--fd", str(bound.fileno()), "--log-level", "warning"],
                        cwd=REPO_ROOT,
                        env={**base, **settings},
                        stdout=handle,
                        stderr=subprocess.STDOUT,
                        pass_fds=(bound.fileno(),),
                    )
                )
    finally:
        # Each child holds its own copy of its socket.
        for bound in sockets:
            bound.close()
    network = AgentNetwork.over_http(
        {GMA_AGENT: gma_url, **{lma_agent(site): url for site, url in lma_urls.items()}},
        timeout_s=30.0,
        dispatch_timeout_s=60.0,
    )
    agents = AgentProcesses(network=network, processes=processes, logs=logs)
    try:
        _wait_until_healthy(agents)
    except Exception:
        agents.stop()
        raise
    return agents


def _wait_until_healthy(agents: AgentProcesses, timeout_s: float = 60.0) -> None:
    deadline = time.monotonic() + timeout_s
    clients = [agents.network.gma()] + [agents.network.lma(site) for site in SITES]
    waiting = list(clients)
    while waiting:
        for process, log in zip(agents.processes, agents.logs):
            if process.poll() is not None:
                raise RuntimeError(f"an agent process exited early:\n{log.read_text(errors='replace')[-2000:]}")
        still = []
        for client in waiting:
            try:
                client.health()
            except AgentError:
                still.append(client)
        waiting = still
        if waiting and time.monotonic() > deadline:
            raise RuntimeError("the agent processes did not become healthy in time")
        if waiting:
            time.sleep(0.2)


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class OverHttpStudyTests(Shared.StudyAssertions):
    """The study with each agent in its own uvicorn process, reached over loopback HTTP."""

    prefix = "http"

    @classmethod
    def setUpClass(cls) -> None:
        import tempfile

        folder = tempfile.TemporaryDirectory(prefix="dagents-flow-")
        cls.addClassCleanup(folder.cleanup)
        cls.agents = start_over_http(Path(folder.name))
        cls.addClassCleanup(cls.agents.stop)
        cls.outcome = run_study(cls.agents.network, cls.prefix)
        # The same study in process, for the comparison below.
        network, _, _, _ = build_in_process()
        try:
            cls.in_process = run_study(network, cls.prefix)
        finally:
            network.close()

    def test_both_transports_give_the_same_study(self) -> None:
        over_http, in_process = comparable(self.outcome), comparable(self.in_process)
        for key in over_http:
            with self.subTest(part=key):
                self.assertEqual(over_http[key], in_process[key])

    def test_the_agents_refuse_calls_without_a_token(self) -> None:
        for client in [self.agents.network.gma()] + [self.agents.network.lma(site) for site in SITES]:
            with self.subTest(agent=repr(client)):
                self.assertEqual(client.health()["status"], "ok")
                with self.assertRaises(AgentError) as caught:
                    client.request("GET", "/api/v1/governance/audit:summary")
                self.assertEqual((caught.exception.status, caught.exception.code), (401, "unauthorized"))


if __name__ == "__main__":
    unittest.main()
