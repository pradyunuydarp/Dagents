"""Tests for the agent client (``agent_client.py``) and agent tokens (``agent_auth.py``).

The client reaches an LMA or a GMA in two ways:

- **in process**: every agent app runs on one event-loop thread and is called
  through ``httpx.ASGITransport``, so a call goes through the real routes and
  request validation without a socket;
- **over HTTP**: the same client code calls agents served by uvicorn.

Each test of the typed methods runs against both transports, with fake agent
apps that answer the routes in sections 6.8 and 6.9 of the plan with fixed
objects. ``TransportParityTests`` checks that the two transports give equal
status codes and bodies. The tests need no planner and no database.
"""

from __future__ import annotations

import hashlib
import json
import logging
import socket
import subprocess
import sys
import textwrap
import threading
import time
import unittest
from pathlib import Path
from typing import Annotated, Any, Callable
from unittest import mock

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel
from starlette.applications import Starlette
from starlette.routing import Mount

from dagents.common.domain.federation import (
    AggregationMethod,
    AggregationReadiness,
    ApprovalResult,
    CandidateModel,
    FederatedJob,
    JobAcceptance,
    JobRun,
    ModelVersion,
    ModelVersionList,
    ModelVersionRegistration,
    ReleaseDecision,
    ReleaseEvaluation,
    ReleaseEvaluationRequest,
    ReleaseGuardCheck,
    RoundManifest,
    RoundPlan,
    RoundRecord,
    SiteRegistration,
    SiteResult,
    UpdateReceipt,
    UpdateUpload,
)
from dagents.common.domain.governance import KyuAssessment, Requester, RestrictionPlan
from dagents.common.domain.site import (
    AuditSummary,
    JobLogEntry,
    LocalEvaluation,
    ModelInUse,
    ProbeRequest,
    ProbeResponse,
    SiteDirectoryEntry,
    SiteInfo,
    SiteSourceInfo,
    SiteSummary,
    SourceState,
)
from dagents.common.infrastructure import agent_auth
from dagents.common.infrastructure.agent_auth import (
    AccessConfigurationError,
    AgentAccess,
    Caller,
    TokenGrant,
    install_agent_access,
    parse_site_map,
    require_scope,
)
from dagents.common.infrastructure.agent_client import (
    GMA_AGENT,
    AgentError,
    AgentNetwork,
    AgentUnavailableError,
    CallLog,
    lma_agent,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

SITE = "site-a"
OTHER_SITE = "site-b"

# Tokens of the length secrets.token_urlsafe(24) gives. Fixed, so a failure
# message can be read; never real credentials.
LMA_SITE_TOKEN = "lma-site-token-aaaaaaaaaaaaaaaaaaaa"
LMA_COORDINATOR_TOKEN = "lma-coordinator-token-bbbbbbbbbbbbbb"
GMA_ADMIN_TOKEN = "gma-admin-token-cccccccccccccccccccc"
GMA_SITE_TOKEN = "gma-site-a-token-dddddddddddddddddddd"
GMA_OTHER_SITE_TOKEN = "gma-site-b-token-eeeeeeeeeeeeeeeeeeee"

DIGEST = "d" * 64


# ---------------------------------------------------------------------------
# Fixed contract objects the fake agents answer with
# ---------------------------------------------------------------------------


def restriction_plan(boundary: str = "before_read") -> RestrictionPlan:
    return RestrictionPlan(
        request_id="request-1",
        boundary=boundary,
        assessment=KyuAssessment(requester_id="tester", kyu_score=0.9, trust="high"),
        granularity="row",
        decision="permit",
        filtering_score=0.0,
    )


MANIFEST = RoundManifest(
    round_id="round-1",
    study_id="study-1",
    condition_id="toy_condition",
    phase="training",
    model_version="toy-linear-init",
    feature_contract_version="toy-v1",
)

ROUND_PLAN = RoundPlan(
    round_id="round-1",
    study_id="study-1",
    phase="training",
    selected_sites=[SITE],
    quorum_met=True,
    required_participants=1,
    aggregation=AggregationMethod(),
    round_digest=DIGEST,
)

SITE_INFO = SiteInfo(
    site_id=SITE,
    condition_id="toy_condition",
    classification_id="toy-classification",
    feature_contract_version="toy-v1",
    label_field="label",
    source=SiteSourceInfo(kind="memory", location={"schema": "site_a"}, role=None),
    record_count=30,
    source_version="30:12",
    capabilities=["evaluate", "train"],
    policy_version="policy-1",
    minimum_cohort=5,
)

SITE_SUMMARY = SiteSummary(
    site_id=SITE,
    record_count=30,
    labelled_count=12,
    label_rate=0.25,
    label_rate_status="released",
    minimum_cohort=5,
)

MODEL_PARAMS = {"bias": 0.5, "w.x1": -1.25, "w.x2": 2.0}

RELEASED = ModelVersion(
    version="toy-linear+round-1",
    kind="toy_linear",
    digest=DIGEST,
    status="released",
    params=MODEL_PARAMS,
)


def update_digest(update: dict[str, float]) -> str:
    return hashlib.sha256(json.dumps(update, sort_keys=True).encode()).hexdigest()


def conflict(detail: str, code: str = "conflict", status: int = 409) -> JSONResponse:
    return JSONResponse({"detail": detail, "code": code}, status_code=status)


class Echo(BaseModel):
    name: str
    count: int


# ---------------------------------------------------------------------------
# Fake agents
# ---------------------------------------------------------------------------


def build_fake_lma(site_id: str, access: AgentAccess) -> FastAPI:
    """An LMA that answers the site and job routes with fixed objects."""

    app = FastAPI()
    install_agent_access(app, access, agent_name=f"fake LMA {site_id}")
    site_or_coordinator = [Depends(require_scope("site", "coordinator"))]
    site_only = [Depends(require_scope("site"))]
    coordinator_only = [Depends(require_scope("coordinator"))]

    @app.get("/api/v1/health")
    def health():
        return {"status": "ok", "agent_type": "LMA"}

    @app.get("/api/v1/site", dependencies=site_or_coordinator)
    def site():
        time.sleep(app.state.site_delay_s)
        return SITE_INFO.model_copy(update={"site_id": site_id})

    @app.get("/api/v1/site/summary", dependencies=site_or_coordinator)
    def summary():
        return SITE_SUMMARY.model_copy(update={"site_id": site_id})

    @app.get("/api/v1/site/profile", dependencies=site_only)
    def profile():
        return {"record_count": 30, "tracked_fields": ["x1"], "fields": {}}

    @app.post("/api/v1/site/source:refresh", dependencies=site_only)
    def refresh():
        return SourceState(record_count=31, source_version="31:12")

    @app.get("/api/v1/site/model", dependencies=site_only)
    def model():
        return ModelInUse(version=RELEASED.version, kind="toy_linear", digest=DIGEST, status="released", params=MODEL_PARAMS)

    @app.post("/api/v1/site/model:evaluate", dependencies=site_only)
    def evaluate():
        return LocalEvaluation(version=RELEASED.version, labelled_count=12, metrics={"accuracy": 0.75})

    @app.post("/api/v1/governance/restrictions:probe", dependencies=site_only)
    def probe(request: ProbeRequest):
        return ProbeResponse(
            request_id="probe-1",
            boundary=request.boundary,
            permitted=True,
            plan=restriction_plan(request.boundary),
            released_count=request.record_limit,
            released_fields=request.requested_fields or [],
            audit_id="audit-1",
        )

    @app.get("/api/v1/governance/audit:summary", dependencies=site_or_coordinator)
    def audit_summary():
        return AuditSummary(chain_intact=True, record_count=4)

    @app.get("/api/v1/federation/registration", dependencies=coordinator_only)
    def registration():
        return SiteRegistration(site_id=site_id, feature_contract_version="toy-v1", cohort_size=30)

    @app.post("/api/v1/federation/jobs:offer", dependencies=coordinator_only)
    def offer(job: FederatedJob):
        return JobAcceptance(round_id=job.round_id, site_id=job.site_id, accepted=True, verified_digest=True)

    @app.post("/api/v1/federation/jobs:execute", dependencies=coordinator_only)
    def execute(job: FederatedJob):
        if job.round_id == "not-accepted":
            return conflict("this LMA did not accept a job for round not-accepted")
        return SiteResult(round_id=job.round_id, site_id=job.site_id, participation="completed", contributed_examples=12)

    @app.post("/api/v1/federation/jobs:run", dependencies=coordinator_only)
    def run(job: FederatedJob):
        accepted = job.round_id != "refused"
        acceptance = JobAcceptance(round_id=job.round_id, site_id=job.site_id, accepted=accepted, verified_digest=accepted)
        if not accepted:
            return JobRun(acceptance=acceptance)
        result = SiteResult(round_id=job.round_id, site_id=job.site_id, participation="completed", contributed_examples=12)
        return JobRun(acceptance=acceptance, result=result)

    @app.get("/api/v1/federation/jobs", dependencies=site_only)
    def jobs(limit: int = 20):
        entries = [JobLogEntry(round_id=f"round-{n}", phase="training", participation="completed") for n in range(3)]
        return entries[:limit]

    app.state.site_delay_s = 0.0
    return app


def build_fake_gma(access: AgentAccess, holder: dict[str, Any]) -> FastAPI:
    """A GMA that answers the round, update and model version routes with fixed objects.

    ``holder["network"]`` is the network the nested route calls the LMA through.
    """

    app = FastAPI()
    install_agent_access(app, access, agent_name="fake GMA")
    admin = [Depends(require_scope("admin"))]
    rounds: dict[str, RoundRecord] = {}
    updates: dict[tuple[str, str], dict[str, float]] = {}

    def record(manifest: RoundManifest, results: list[SiteResult] | None = None) -> RoundRecord:
        return RoundRecord(
            manifest=manifest,
            plan=ROUND_PLAN.model_copy(update={"round_id": manifest.round_id}),
            results=results or [],
            created_at=1,
            updated_at=2,
            status="collecting",
        )

    @app.get("/api/v1/health")
    def health():
        return {"status": "ok", "agent_type": "GMA", "engine": "http"}

    @app.post("/api/v1/federation/sites:refresh", dependencies=admin)
    def refresh_sites():
        return [SiteRegistration(site_id=SITE, feature_contract_version="toy-v1", cohort_size=30)]

    @app.get("/api/v1/federation/sites/summaries", dependencies=admin)
    def summaries(include: str | None = None):
        audit = AuditSummary(chain_intact=True, record_count=4) if include == "audit" else None
        return [SiteDirectoryEntry(site_id=SITE, reachable=True, source_kind="memory", summary=SITE_SUMMARY, audit=audit)]

    @app.get("/api/v1/federation/sites", dependencies=admin)
    def list_sites():
        return [SiteRegistration(site_id=SITE, feature_contract_version="toy-v1")]

    @app.put("/api/v1/federation/sites/{site_id}", dependencies=admin)
    def register_site(site_id: str, registration: SiteRegistration):
        if registration.site_id != site_id:
            raise HTTPException(status_code=400, detail="path id does not match body id")
        return registration

    @app.post("/api/v1/federation/rounds:plan", dependencies=admin)
    def plan_round(manifest: RoundManifest):
        return ROUND_PLAN.model_copy(update={"round_id": manifest.round_id})

    @app.post("/api/v1/federation/rounds", status_code=202, dependencies=admin)
    def dispatch_round(manifest: RoundManifest):
        if manifest.round_id in rounds:
            return conflict(f"round {manifest.round_id} is already stored")
        if manifest.round_id.startswith("slow-"):
            time.sleep(1.5)  # longer than the contract tests' 1 s timeout
        rounds[manifest.round_id] = record(manifest)
        return rounds[manifest.round_id]

    @app.get("/api/v1/federation/rounds", dependencies=admin)
    def list_rounds(limit: int = 20):
        return list(rounds.values())[:limit]

    @app.get("/api/v1/federation/rounds/{round_id}", dependencies=admin)
    def get_round(round_id: str):
        if round_id not in rounds:
            raise HTTPException(status_code=404, detail=f"unknown round: {round_id}")
        return rounds[round_id]

    @app.post("/api/v1/federation/rounds/{round_id}/results", status_code=202, dependencies=admin)
    def submit_result(round_id: str, result: SiteResult):
        return record(MANIFEST.model_copy(update={"round_id": round_id}), [result])

    @app.get("/api/v1/federation/rounds/{round_id}/readiness", dependencies=admin)
    def readiness(round_id: str):
        return AggregationReadiness(round_id=round_id, aggregation_permitted=True, accepted_sites=[SITE])

    @app.post("/api/v1/federation/rounds/{round_id}:aggregate", dependencies=admin)
    def aggregate(round_id: str):
        if round_id == "refused":
            return conflict("quorum was not met", code="aggregation_refused")
        return CandidateModel(
            round_id=round_id,
            candidate_version=f"toy-linear+{round_id}",
            parent_version="toy-linear-init",
            aggregation_method="fedavg",
        )

    @app.put(
        "/api/v1/federation/rounds/{round_id}/updates/{site_id}",
        dependencies=[Depends(require_scope("site", bind_site="site_id"))],
    )
    def upload_update(round_id: str, site_id: str, upload: UpdateUpload):
        if (round_id, site_id) in updates:
            return conflict("an update is already stored for this site")
        updates[(round_id, site_id)] = dict(upload.update)
        return UpdateReceipt(digest=update_digest(dict(upload.update)))

    @app.post("/api/v1/federation/rounds/{round_id}:discard", status_code=204, dependencies=admin)
    def discard(round_id: str, body: dict[str, str]):
        app.state.discarded.append((round_id, body.get("reason")))
        return Response(status_code=204)

    @app.post("/api/v1/federation/rounds/{round_id}:evaluate-release", dependencies=admin)
    def evaluate_release(round_id: str, request: ReleaseEvaluationRequest):
        return ReleaseEvaluation(
            decision=ReleaseDecision(round_id=round_id, candidate_version=request.candidate_version, action="release"),
            guard=ReleaseGuardCheck(permitted=True, plan=restriction_plan("before_release"), audit_id="audit-9"),
            candidate_status="candidate",
            metrics_source="rounds",
            baseline_version="toy-threshold-v1",
        )

    @app.get("/api/v1/governance/audit:summary", dependencies=admin)
    def audit_summary():
        return AuditSummary(chain_intact=True, record_count=2)

    @app.get("/api/v1/model-versions", dependencies=admin)
    def list_versions(limit: int = 20):
        bare = RELEASED.model_copy(update={"params": None})
        return ModelVersionList(model_in_use=bare, versions=[bare][:limit])

    @app.get("/api/v1/model-versions:released", dependencies=[Depends(require_scope("admin", "site"))])
    def released(if_none_match: Annotated[str | None, Header()] = None):
        etag = f'"{RELEASED.digest}"'
        if if_none_match == etag:
            return Response(status_code=304, headers={"ETag": etag})
        return JSONResponse(RELEASED.model_dump(mode="json"), headers={"ETag": etag})

    @app.get("/api/v1/model-versions/{version}", dependencies=admin)
    def get_version(version: str):
        if version == "missing":
            raise HTTPException(status_code=404, detail="unknown model version: missing")
        return RELEASED.model_copy(update={"version": version, "params": None})

    @app.put("/api/v1/model-versions/{version}", status_code=201, dependencies=admin)
    def register_version(version: str, registration: ModelVersionRegistration):
        return ModelVersion(version=version, kind=registration.kind, digest=DIGEST, status=registration.requested_status)

    @app.post("/api/v1/model-versions/{version}:approve", dependencies=admin)
    def approve(version: str):
        released = RELEASED.model_copy(update={"version": version, "params": None})
        return ApprovalResult(released=released, previous="toy-threshold-v1")

    # Routes that test the error mapping, timeouts and nested calls.

    @app.get("/test/not-found")
    def not_found():
        raise HTTPException(status_code=404, detail="unknown round: r-9")

    @app.get("/test/conflict")
    def test_conflict():
        return conflict("round r-1 is already stored")

    @app.post("/test/validate")
    def validate(body: Echo):
        return body

    @app.get("/test/crash")
    def crash():
        raise RuntimeError("could not reach postgresql://admin:secret@db.example.internal:5432/site")

    @app.get("/test/planner")
    def planner():
        return conflict("the planner could not be run", code="planner_unavailable", status=503)

    @app.get("/test/plain-503")
    def plain_503():
        return PlainTextResponse("upstream db.example.internal:5432 is down", status_code=503)

    @app.get("/test/slow")
    def slow(seconds: Annotated[float, Query()] = 1.0):
        time.sleep(seconds)
        return {"slept": seconds}

    @app.get("/test/nested")
    def nested():
        network: AgentNetwork = holder["network"]
        info = network.lma(SITE, LMA_COORDINATOR_TOKEN).site_info()
        return {"site_id": info.site_id}

    @app.get("/test/echo/{value}")
    def echo(value: str, q: str | None = None):
        return {"value": value, "q": q}

    app.state.discarded = []
    return app


def full_access() -> tuple[AgentAccess, AgentAccess]:
    lma_access = AgentAccess(
        [TokenGrant("site", LMA_SITE_TOKEN), TokenGrant("coordinator", LMA_COORDINATOR_TOKEN)]
    )
    gma_access = AgentAccess(
        [
            TokenGrant("admin", GMA_ADMIN_TOKEN),
            TokenGrant("site", GMA_SITE_TOKEN, site_id=SITE),
            TokenGrant("site", GMA_OTHER_SITE_TOKEN, site_id=OTHER_SITE),
        ]
    )
    return lma_access, gma_access


# ---------------------------------------------------------------------------
# A uvicorn server on a loopback port
# ---------------------------------------------------------------------------


def bound_loopback_socket() -> socket.socket:
    """A socket bound to a free port in 9800-9899, so no other process can take the port first."""
    for port in range(9800, 9900):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            sock.close()
            continue
        return sock
    raise RuntimeError("no free port in 9800-9899")


class LoopbackServer:
    """Serve an ASGI app with uvicorn on a loopback port, in a thread."""

    def __init__(self, app: Any) -> None:
        self.sock = bound_loopback_socket()
        self.port = self.sock.getsockname()[1]
        config = uvicorn.Config(app, log_config=None, log_level="warning", access_log=False, lifespan="off")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.sock]}, daemon=True)

    def start(self) -> LoopbackServer:
        self.thread.start()
        deadline = time.monotonic() + 15
        while not self.server.started:
            if time.monotonic() > deadline or not self.thread.is_alive():
                raise RuntimeError("uvicorn did not start")
            time.sleep(0.02)
        return self

    def url(self, prefix: str) -> str:
        return f"http://127.0.0.1:{self.port}{prefix}"

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(15)
        self.sock.close()


class NetworkFixture:
    """A fake GMA and two fake LMAs, reachable in process or over HTTP."""

    def __init__(self, mode: str, *, timeout_s: float = 5.0, dispatch_timeout_s: float = 10.0, thread_tokens: int = 64):
        lma_access, gma_access = full_access()
        self.holder: dict[str, Any] = {}
        self.gma_app = build_fake_gma(gma_access, self.holder)
        self.lma_apps = {SITE: build_fake_lma(SITE, lma_access), OTHER_SITE: build_fake_lma(OTHER_SITE, lma_access)}
        self.server: LoopbackServer | None = None
        if mode == "in-process":
            self.network = AgentNetwork.in_process(
                timeout_s=timeout_s, dispatch_timeout_s=dispatch_timeout_s, thread_tokens=thread_tokens
            )
            # Apps are added after the network exists, as an agent that calls
            # other agents needs the network when it is built.
            self.network.add_app(GMA_AGENT, self.gma_app)
            for site_id, app in self.lma_apps.items():
                self.network.add_app(lma_agent(site_id), app)
        else:
            routes = [Mount("/gma", app=self.gma_app)]
            routes += [Mount(f"/lma/{site_id}", app=app) for site_id, app in self.lma_apps.items()]
            self.server = LoopbackServer(Starlette(routes=routes)).start()
            base_urls = {GMA_AGENT: self.server.url("/gma")}
            base_urls.update({lma_agent(site_id): self.server.url(f"/lma/{site_id}") for site_id in self.lma_apps})
            self.network = AgentNetwork.over_http(base_urls, timeout_s=timeout_s, dispatch_timeout_s=dispatch_timeout_s)
        self.holder["network"] = self.network

    def close(self) -> None:
        self.network.close()
        if self.server is not None:
            self.server.stop()


# ---------------------------------------------------------------------------
# The same assertions against both transports
# ---------------------------------------------------------------------------


class ClientContract:
    """Assertions every transport must pass. Subclasses set ``MODE``."""

    MODE = ""
    fixture: NetworkFixture

    @classmethod
    def setUpClass(cls) -> None:
        cls.fixture = NetworkFixture(cls.MODE, timeout_s=1.0, dispatch_timeout_s=5.0)
        cls.network = cls.fixture.network

    @classmethod
    def tearDownClass(cls) -> None:
        cls.fixture.close()

    def test_mode_is_reported(self) -> None:
        self.assertEqual(self.network.mode, self.MODE)

    def test_lma_site_routes_return_the_domain_models(self) -> None:
        lma = self.network.lma(SITE, LMA_SITE_TOKEN)
        self.assertEqual(lma.site_info(), SITE_INFO)
        self.assertEqual(lma.site_summary(), SITE_SUMMARY)
        self.assertEqual(lma.site_profile(), {"record_count": 30, "tracked_fields": ["x1"], "fields": {}})
        self.assertEqual(lma.refresh_source(), SourceState(record_count=31, source_version="31:12"))
        self.assertEqual(lma.model_in_use().params, MODEL_PARAMS)
        self.assertEqual(lma.evaluate_model().metrics, {"accuracy": 0.75})
        self.assertEqual(lma.audit_summary(), AuditSummary(chain_intact=True, record_count=4))
        self.assertEqual([entry.round_id for entry in lma.job_log(limit=2)], ["round-0", "round-1"])
        self.assertEqual(lma.health()["agent_type"], "LMA")

    def test_lma_probe_sends_the_request_body(self) -> None:
        lma = self.network.lma(SITE, LMA_SITE_TOKEN)
        response = lma.probe(
            ProbeRequest(
                boundary="before_send",
                granularity="table",
                requested_fields=["x1", "x2"],
                record_limit=7,
                requester=Requester(requester_id="tester"),
            )
        )
        self.assertIsInstance(response, ProbeResponse)
        self.assertEqual(response.boundary, "before_send")
        self.assertEqual(response.released_count, 7)
        self.assertEqual(response.released_fields, ["x1", "x2"])

    def test_lma_job_routes(self) -> None:
        lma = self.network.lma(SITE, LMA_COORDINATOR_TOKEN)
        self.assertEqual(lma.registration().cohort_size, 30)
        job = FederatedJob(round_id="round-1", site_id=SITE, manifest=MANIFEST, manifest_digest=DIGEST, task="train")
        acceptance = lma.offer_job(job)
        self.assertEqual((acceptance.round_id, acceptance.site_id, acceptance.accepted), ("round-1", SITE, True))
        result = lma.execute_job(job)
        self.assertEqual((result.participation, result.contributed_examples), ("completed", 12))
        with self.assertRaises(AgentError) as caught:
            lma.execute_job(job.model_copy(update={"round_id": "not-accepted"}))
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "conflict"))
        ran = lma.run_job(job)
        self.assertIsInstance(ran, JobRun)
        self.assertTrue(ran.acceptance.accepted)
        self.assertEqual(("completed", 12), (ran.result.participation, ran.result.contributed_examples))
        refused = lma.run_job(job.model_copy(update={"round_id": "refused"}))
        self.assertEqual((False, None), (refused.acceptance.accepted, refused.result))

    def test_each_lma_answers_for_its_own_site(self) -> None:
        self.assertEqual(self.network.lma(OTHER_SITE, LMA_SITE_TOKEN).site_info().site_id, OTHER_SITE)

    def test_gma_round_routes(self) -> None:
        gma = self.network.gma(GMA_ADMIN_TOKEN)
        round_id = f"round-{self.MODE}"
        manifest = MANIFEST.model_copy(update={"round_id": round_id})
        self.assertEqual([site.site_id for site in gma.refresh_sites()], [SITE])
        self.assertIsNone(gma.site_summaries()[0].audit)
        self.assertEqual(gma.site_summaries(include_audit=True)[0].audit, AuditSummary(chain_intact=True, record_count=4))
        self.assertEqual(gma.list_sites()[0].site_id, SITE)
        registration = SiteRegistration(site_id=OTHER_SITE, feature_contract_version="toy-v1")
        self.assertEqual(gma.register_site(registration), registration)
        self.assertEqual(gma.plan_round(manifest).round_id, round_id)
        dispatched = gma.dispatch_round(manifest)
        self.assertIsInstance(dispatched, RoundRecord)
        self.assertEqual(dispatched.manifest, manifest)
        self.assertEqual(gma.get_round(round_id).manifest.round_id, round_id)
        self.assertIn(round_id, [record.manifest.round_id for record in gma.list_rounds(limit=50)])
        result = SiteResult(round_id=round_id, site_id=SITE, participation="completed")
        self.assertEqual(gma.submit_result(round_id, result).results, [result])
        self.assertTrue(gma.readiness(round_id).aggregation_permitted)
        self.assertEqual(gma.aggregate(round_id).candidate_version, f"toy-linear+{round_id}")
        self.assertIsNone(gma.discard_round(round_id, "the pilot stopped"))
        self.assertIn((round_id, "the pilot stopped"), self.fixture.gma_app.state.discarded)
        evaluation = gma.evaluate_release(round_id, ReleaseEvaluationRequest(candidate_version=f"toy-linear+{round_id}"))
        self.assertEqual((evaluation.decision.action, evaluation.metrics_source), ("release", "rounds"))
        self.assertEqual(gma.audit_summary().record_count, 2)

    def test_gma_reused_round_id_and_refused_aggregation(self) -> None:
        gma = self.network.gma(GMA_ADMIN_TOKEN)
        manifest = MANIFEST.model_copy(update={"round_id": f"reused-{self.MODE}"})
        gma.dispatch_round(manifest)
        with self.assertRaises(AgentError) as caught:
            gma.dispatch_round(manifest)
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "conflict"))
        with self.assertRaises(AgentError) as caught:
            gma.aggregate("refused")
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "aggregation_refused"))
        self.assertEqual(caught.exception.detail, "quorum was not met")

    def test_gma_update_upload_is_bound_to_the_site_token(self) -> None:
        round_id = f"upload-{self.MODE}"
        update = {"bias": 0.25, "w.x1": -0.5}
        receipt = self.network.gma(GMA_SITE_TOKEN).upload_update(
            round_id, SITE, UpdateUpload(job_digest=DIGEST, update=update)
        )
        self.assertEqual(receipt, UpdateReceipt(digest=update_digest(update)))
        with self.assertRaises(AgentError) as caught:
            self.network.gma(GMA_OTHER_SITE_TOKEN).upload_update(
                round_id, SITE, UpdateUpload(job_digest=DIGEST, update=update)
            )
        self.assertEqual((caught.exception.status, caught.exception.code), (403, "forbidden"))
        with self.assertRaises(AgentError) as caught:
            self.network.gma(GMA_SITE_TOKEN).upload_update(
                round_id, SITE, UpdateUpload(job_digest=DIGEST, update=update)
            )
        self.assertEqual((caught.exception.status, caught.exception.code), (409, "conflict"))

    def test_gma_model_version_routes(self) -> None:
        gma = self.network.gma(GMA_ADMIN_TOKEN)
        listing = gma.list_model_versions(limit=5)
        self.assertIsInstance(listing, ModelVersionList)
        self.assertIsNone(listing.model_in_use.params)
        # A version with "+" and ":" reaches the route unchanged.
        self.assertEqual(gma.get_model_version("toy-linear+round:1").version, "toy-linear+round:1")
        registered = gma.register_model_version(
            "toy-linear-init", ModelVersionRegistration(kind="toy_linear", params=MODEL_PARAMS, requested_status="released")
        )
        self.assertEqual((registered.version, registered.status), ("toy-linear-init", "released"))
        approval = gma.approve("toy-linear+round-2")
        self.assertEqual((approval.released.version, approval.previous), ("toy-linear+round-2", "toy-threshold-v1"))
        with self.assertRaises(AgentError) as caught:
            gma.get_model_version("missing")
        self.assertEqual((caught.exception.status, caught.exception.code), (404, "not_found"))

    def test_released_model_with_etag(self) -> None:
        for token in (GMA_ADMIN_TOKEN, GMA_SITE_TOKEN):
            gma = self.network.gma(token)
            released = gma.released_model()
            self.assertEqual(released, RELEASED)
            self.assertIsNone(gma.released_model(known_digest=released.digest))
            self.assertEqual(gma.released_model(known_digest="0" * 64), RELEASED)

    def test_generic_request_quotes_path_values_and_sends_the_query(self) -> None:
        # A value with "?", "#", "+" or a space reaches one path segment
        # unchanged. ("/" cannot: servers decode the path before routing.)
        response = self.network.gma().request(
            "GET", "/test/echo/{value}", path_params={"value": "a b+d?e#f"}, params={"q": "x&y"}
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, {"value": "a b+d?e#f", "q": "x&y"})

    def test_generic_request_refuses_a_missing_path_value(self) -> None:
        with self.assertRaises(KeyError):
            self.network.gma().request("GET", "/test/echo/{value}")

    def test_error_without_code_takes_the_code_of_its_status(self) -> None:
        gma = self.network.gma()
        with self.assertRaises(AgentError) as caught:
            gma.request("GET", "/test/not-found")
        error = caught.exception
        self.assertNotIsInstance(error, AgentUnavailableError)
        self.assertEqual((error.status, error.code, error.detail), (404, "not_found", "unknown round: r-9"))
        self.assertEqual((error.service, error.site_id, error.path), ("gma", None, "/test/not-found"))

    def test_error_code_in_the_body_is_kept(self) -> None:
        with self.assertRaises(AgentError) as caught:
            self.network.gma().request("GET", "/test/conflict")
        self.assertEqual(
            (caught.exception.status, caught.exception.code, caught.exception.detail),
            (409, "conflict", "round r-1 is already stored"),
        )

    def test_invalid_body_is_a_422_with_the_field_named(self) -> None:
        with self.assertRaises(AgentError) as caught:
            self.network.gma().request("POST", "/test/validate", json={"name": "a", "count": "many"})
        self.assertEqual((caught.exception.status, caught.exception.code), (422, "invalid_request"))
        self.assertIn("count", caught.exception.detail)

    def test_planner_unavailable_keeps_its_code(self) -> None:
        with self.assertRaises(AgentError) as caught:
            self.network.gma().request("GET", "/test/planner")
        self.assertNotIsInstance(caught.exception, AgentUnavailableError)
        self.assertEqual((caught.exception.status, caught.exception.code), (503, "planner_unavailable"))

    def test_503_without_a_code_is_unavailable_and_hides_the_body(self) -> None:
        with self.assertRaises(AgentUnavailableError) as caught:
            self.network.gma().request("GET", "/test/plain-503")
        error = caught.exception
        self.assertEqual((error.status, error.code), (503, "agent_unavailable"))
        self.assertNotIn("db.example.internal", str(error))
        self.assertNotIn("db.example.internal", error.detail)

    def call_crash_route(self) -> AgentError:
        with self.assertRaises(AgentError) as caught:
            self.network.gma().request("GET", "/test/crash")
        return caught.exception

    def test_an_agent_crash_becomes_a_500(self) -> None:
        if self.MODE == "in-process":
            # ASGITransport turns the error into a 500 without logging it, so
            # the network logs it. (uvicorn logs it over HTTP, from its own
            # thread, possibly after the client has its answer.)
            with self.assertLogs("dagents.common.infrastructure.agent_client", level="ERROR") as logs:
                error = self.call_crash_route()
            self.assertTrue(any("could not reach" in line for line in logs.output))
        else:
            logging.disable(logging.CRITICAL)
            self.addCleanup(logging.disable, logging.NOTSET)
            error = self.call_crash_route()
        self.assertNotIsInstance(error, AgentUnavailableError)
        self.assertEqual((error.status, error.code, error.detail), (500, "server_error", "Internal Server Error"))
        self.assertNotIn("secret", str(error))

    def test_calls_right_after_a_crash_are_answered(self) -> None:
        # uvicorn logs an app error, then closes the connection. A log sink
        # that takes 50 ms keeps the closing connection in the client's pool
        # while the next call starts, as a slow log handler does in a real
        # deployment. The next calls must not fail on that connection.
        class SlowQuietSink(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                time.sleep(0.05)

        sink = SlowQuietSink(level=logging.ERROR)
        for name in ("uvicorn.error", "dagents.common.infrastructure.agent_client"):
            log = logging.getLogger(name)
            log.addHandler(sink)
            self.addCleanup(log.removeHandler, sink)
            previous = log.propagate
            log.propagate = False
            self.addCleanup(setattr, log, "propagate", previous)
        gma = self.network.gma(GMA_ADMIN_TOKEN)
        for _ in range(3):
            self.call_crash_route()
            self.assertEqual(gma.plan_round(MANIFEST).round_id, "round-1")
            self.call_crash_route()
            self.assertEqual(gma.list_sites()[0].site_id, SITE)

    def test_missing_or_wrong_token(self) -> None:
        for token in (None, "not-a-token-of-this-gma-xxxxxxx"):
            with self.assertRaises(AgentError) as caught:
                self.network.gma(token).list_sites()
            self.assertEqual((caught.exception.status, caught.exception.code), (401, "unauthorized"))
        with self.assertRaises(AgentError) as caught:
            self.network.gma(GMA_SITE_TOKEN).list_sites()
        self.assertEqual((caught.exception.status, caught.exception.code), (403, "forbidden"))
        with self.assertRaises(AgentError) as caught:
            self.network.lma(SITE, LMA_SITE_TOKEN).registration()
        self.assertEqual((caught.exception.status, caught.exception.code), (403, "forbidden"))

    def test_a_slow_call_times_out(self) -> None:
        started = time.monotonic()
        with self.assertRaises(AgentUnavailableError) as caught:
            self.network.gma().request("GET", "/test/slow", params={"seconds": 3.0})
        elapsed = time.monotonic() - started
        self.assertIsNone(caught.exception.status)
        self.assertEqual(caught.exception.code, "agent_unavailable")
        self.assertIn("no answer within 1 s", caught.exception.detail)
        # The wait ends at the timeout, even though the handler keeps running.
        self.assertLess(elapsed, 2.5)

    def test_round_dispatch_uses_the_longer_dispatch_timeout(self) -> None:
        manifest = MANIFEST.model_copy(update={"round_id": f"slow-{self.MODE}"})
        record = self.network.gma(GMA_ADMIN_TOKEN).dispatch_round(manifest)
        self.assertEqual(record.manifest.round_id, f"slow-{self.MODE}")

    def test_an_unknown_agent_is_unavailable(self) -> None:
        with CallLog() as log:
            with self.assertRaises(AgentUnavailableError) as caught:
                self.network.lma("no-such-site", LMA_SITE_TOKEN).site_info()
        self.assertIsNone(caught.exception.status)
        self.assertIn("no-such-site", caught.exception.detail)
        self.assertEqual(log.as_list()[0]["status"], None)

    def test_call_log_records_route_templates_without_a_host(self) -> None:
        with CallLog() as log:
            self.network.gma(GMA_ADMIN_TOKEN).get_model_version("toy-linear+round-7")
            self.network.lma(SITE, LMA_SITE_TOKEN).site_summary()
            with self.assertRaises(AgentError):
                self.network.gma(GMA_ADMIN_TOKEN).aggregate("refused")
        calls = log.as_list()
        self.assertEqual(
            [(c["service"], c["site_id"], c["method"], c["path"], c["status"]) for c in calls],
            [
                ("gma", None, "GET", "/api/v1/model-versions/{version}", 200),
                ("lma", SITE, "GET", "/api/v1/site/summary", 200),
                ("gma", None, "POST", "/api/v1/federation/rounds/{round_id}:aggregate", 409),
            ],
        )
        for call in calls:
            self.assertEqual(set(call), {"service", "site_id", "method", "path", "status", "ms"})
            self.assertGreaterEqual(call["ms"], 0)
        text = json.dumps(calls)
        for host_part in ("127.0.0.1", "agents.internal", "http", "round-7"):
            self.assertNotIn(host_part, text)
        if self.fixture.server is not None:
            self.assertNotIn(str(self.fixture.server.port), text)

    def test_call_log_leaves_out_the_calls_an_agent_makes_itself(self) -> None:
        with CallLog() as log:
            body = self.network.gma().request("GET", "/test/nested").body
        self.assertEqual(body, {"site_id": SITE})
        self.assertEqual([(c["service"], c["path"]) for c in log.as_list()], [("gma", "/test/nested")])

    def test_nested_call_logs_both_record(self) -> None:
        with CallLog() as outer:
            self.network.gma().health()
            with CallLog() as inner:
                self.network.lma(SITE).health()
        self.assertEqual([c["service"] for c in outer.as_list()], ["gma", "lma"])
        self.assertEqual([c["service"] for c in inner.as_list()], ["lma"])
        self.assertIsNone(CallLog.current())


class InProcessClientTests(ClientContract, unittest.TestCase):
    MODE = "in-process"

    def test_agents_run_on_one_daemon_thread(self) -> None:
        names = [thread.name for thread in threading.enumerate() if thread.name == "dagents-agent-network"]
        self.assertGreaterEqual(len(names), 1)
        for thread in threading.enumerate():
            if thread.name == "dagents-agent-network":
                self.assertTrue(thread.daemon)

    def test_add_app_refuses_a_name_twice(self) -> None:
        with self.assertRaises(ValueError):
            self.network.add_app(GMA_AGENT, FastAPI())


class NetworkClientTests(ClientContract, unittest.TestCase):
    MODE = "network"

    def test_add_app_is_only_for_in_process_networks(self) -> None:
        with self.assertRaises(TypeError):
            self.network.add_app(lma_agent("site-z"), FastAPI())


class TransportParityTests(unittest.TestCase):
    """The two transports give equal status codes and bodies for the same calls."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.in_process = NetworkFixture("in-process", timeout_s=5.0)
        cls.over_http = NetworkFixture("network", timeout_s=5.0)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.in_process.close()
        cls.over_http.close()

    CALLS: list[tuple[str, str | None, str | None, str, str, dict[str, Any]]] = [
        # (service, site, token, method, template, keyword arguments)
        ("gma", None, None, "GET", "/api/v1/health", {}),
        ("gma", None, GMA_ADMIN_TOKEN, "GET", "/api/v1/federation/sites", {}),
        ("gma", None, None, "GET", "/api/v1/federation/sites", {}),
        ("gma", None, GMA_SITE_TOKEN, "GET", "/api/v1/federation/sites", {}),
        ("gma", None, GMA_ADMIN_TOKEN, "GET", "/api/v1/federation/rounds/{round_id}", {"path_params": {"round_id": "nope"}}),
        ("gma", None, GMA_ADMIN_TOKEN, "POST", "/api/v1/federation/rounds:plan", {"json": {"round_id": "only-an-id"}}),
        ("gma", None, GMA_ADMIN_TOKEN, "POST", "/api/v1/federation/rounds:plan", {"json": MANIFEST.model_dump(mode="json")}),
        ("gma", None, GMA_ADMIN_TOKEN, "GET", "/api/v1/model-versions:released", {}),
        ("gma", None, GMA_ADMIN_TOKEN, "GET", "/api/v1/model-versions:released", {"headers": {"If-None-Match": f'"{DIGEST}"'}}),
        ("gma", None, GMA_ADMIN_TOKEN, "POST", "/api/v1/federation/rounds/{round_id}:discard", {"path_params": {"round_id": "r"}, "json": {"reason": "stop"}}),
        ("gma", None, None, "GET", "/test/conflict", {}),
        ("gma", None, None, "GET", "/test/planner", {}),
        ("gma", None, None, "GET", "/test/plain-503", {}),
        ("gma", None, None, "GET", "/test/crash", {}),
        ("gma", None, None, "POST", "/test/validate", {"json": {"name": "a"}}),
        ("gma", None, None, "GET", "/test/nested", {}),
        ("lma", SITE, LMA_SITE_TOKEN, "GET", "/api/v1/site", {}),
        ("lma", SITE, LMA_SITE_TOKEN, "GET", "/api/v1/federation/jobs", {"params": {"limit": 1}}),
        ("lma", SITE, LMA_SITE_TOKEN, "POST", "/api/v1/federation/jobs:offer", {"json": {}}),
        ("lma", SITE, "wrong-token-wrong-token-wrong", "GET", "/api/v1/site", {}),
    ]

    @staticmethod
    def outcome(network: AgentNetwork, call: tuple) -> tuple[Any, ...]:
        service, site_id, token, method, template, kwargs = call
        client = network.gma(token) if service == "gma" else network.lma(site_id, token)
        try:
            response = client.request(method, template, **kwargs)
        except AgentError as error:
            return ("error", type(error).__name__, error.status, error.code, error.detail)
        etag = response.headers.get("etag")
        return ("ok", response.status, response.body, etag)

    def test_both_transports_answer_alike(self) -> None:
        logging.disable(logging.CRITICAL)
        try:
            for call in self.CALLS:
                with self.subTest(method=call[3], path=call[4], token=bool(call[2])):
                    self.assertEqual(self.outcome(self.in_process.network, call), self.outcome(self.over_http.network, call))
        finally:
            logging.disable(logging.NOTSET)


# ---------------------------------------------------------------------------
# Thread limits, nested calls and shutdown
# ---------------------------------------------------------------------------


def run_in_threads(count: int, target: Callable[[], Any], watchdog_s: float) -> tuple[list[Any], list[threading.Thread]]:
    """Run ``target`` in ``count`` threads at once and collect each result or exception."""
    results: list[Any] = [None] * count
    start = threading.Barrier(count)

    def worker(index: int) -> None:
        start.wait()
        try:
            results[index] = target()
        except Exception as error:  # noqa: BLE001 - the test inspects it
            results[index] = error

    threads = [threading.Thread(target=worker, args=(n,), daemon=True) for n in range(count)]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + watchdog_s
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    return results, [thread for thread in threads if thread.is_alive()]


class ConcurrencyTests(unittest.TestCase):
    def test_sixty_concurrent_requests_with_nested_calls_finish(self) -> None:
        fixture = NetworkFixture("in-process", timeout_s=30.0, thread_tokens=64)
        self.addCleanup(fixture.close)
        # Each LMA call holds its worker thread for a moment, so the outer
        # GMA handlers really do wait on the nested calls at the same time.
        fixture.lma_apps[SITE].state.site_delay_s = 0.05
        gma = fixture.network.gma()
        results, alive = run_in_threads(60, lambda: gma.request("GET", "/test/nested").body, watchdog_s=60)
        self.assertEqual(alive, [], "requests were still waiting after 60 s: the thread limit deadlocked")
        self.assertEqual(results, [{"site_id": SITE}] * 60)

    def test_too_few_thread_tokens_end_in_timeouts_not_a_hang(self) -> None:
        fixture = NetworkFixture("in-process", timeout_s=1.0, thread_tokens=4)
        self.addCleanup(fixture.close)
        fixture.lma_apps[SITE].state.site_delay_s = 0.05
        gma = fixture.network.gma()
        logging.disable(logging.CRITICAL)
        self.addCleanup(logging.disable, logging.NOTSET)
        started = time.monotonic()
        results, alive = run_in_threads(12, lambda: gma.request("GET", "/test/nested").body, watchdog_s=30)
        self.assertEqual(alive, [])
        self.assertLess(time.monotonic() - started, 15)
        self.assertTrue(any(isinstance(result, AgentError) for result in results), results)
        # Once the abandoned calls drain, the network answers again.
        deadline = time.monotonic() + 15
        while True:
            try:
                self.assertEqual(gma.request("GET", "/test/nested").body, {"site_id": SITE})
                break
            except AgentError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.2)

    def test_concurrent_calls_over_http_share_kept_clients(self) -> None:
        fixture = NetworkFixture("network", timeout_s=10.0)
        self.addCleanup(fixture.close)
        lma = fixture.network.lma(SITE, LMA_SITE_TOKEN)
        results, alive = run_in_threads(20, lambda: lma.site_summary().site_id, watchdog_s=30)
        self.assertEqual(alive, [])
        self.assertEqual(results, [SITE] * 20)


EXIT_SCRIPT = textwrap.dedent(
    """
    import sys, threading, time
    from fastapi import FastAPI
    from dagents.common.infrastructure.agent_client import GMA_AGENT, AgentNetwork

    app = FastAPI()

    @app.get("/api/v1/health")
    def health():  # a sync handler, so an AnyIO worker thread starts
        return {"status": "ok", "agent_type": "GMA"}

    network = AgentNetwork.in_process({GMA_AGENT: app})
    print(network.gma().health()["status"])
    if sys.argv[1] == "close":
        network.close()
        network.close()  # a second close does nothing
        # anyio's worker threads are told to stop when the loop ends and exit
        # on their own a moment later.
        deadline = time.monotonic() + 5
        while True:
            left = [t.name for t in threading.enumerate() if t is not threading.main_thread() and t.is_alive()]
            if not left or time.monotonic() > deadline:
                break
            time.sleep(0.05)
        print("threads left:", left)
        try:
            network.gma().health()
        except Exception as error:
            print("after close:", type(error).__name__)
    """
)


class ShutdownTests(unittest.TestCase):
    def run_script(self, mode: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-c", EXIT_SCRIPT, mode],
            cwd=REPO_ROOT,
            env={"PYTHONPATH": str(REPO_ROOT), "PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_the_process_exits_after_close(self) -> None:
        completed = self.run_script("close")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("ok", completed.stdout)
        self.assertIn("threads left: []", completed.stdout)
        self.assertIn("after close: AgentUnavailableError", completed.stdout)

    def test_the_process_exits_when_close_is_forgotten(self) -> None:
        completed = self.run_script("forget")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("ok", completed.stdout)

    def test_close_stops_the_loop_thread(self) -> None:
        before = {thread.ident for thread in threading.enumerate()}
        network = AgentNetwork.in_process()
        loop_threads = [t for t in threading.enumerate() if t.ident not in before and t.name == "dagents-agent-network"]
        self.assertEqual(len(loop_threads), 1)
        network.close()
        self.assertFalse(loop_threads[0].is_alive())


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class ConfigurationTests(unittest.TestCase):
    def test_base_urls_must_be_plain_http(self) -> None:
        for url in (
            "ftp://agents.example/gma",
            "http://user:pass-word-123@agents.example",
            "http://agents.example/gma?token=x",
            "http://agents.example/gma#x",
            "not a url",
            "",
        ):
            with self.subTest(url=url):
                with self.assertRaises(ValueError) as caught:
                    AgentNetwork.over_http({GMA_AGENT: url})
                self.assertNotIn("pass-word-123", str(caught.exception))
                self.assertIn(GMA_AGENT, str(caught.exception))

    def test_agent_names_are_checked(self) -> None:
        with self.assertRaises(ValueError):
            AgentNetwork.over_http({"coordinator": "http://agents.example"})
        with self.assertRaises(ValueError):
            lma_agent("")
        self.assertEqual(lma_agent("site-a"), "lma:site-a")

    def test_unreachable_agent_names_no_host_or_port(self) -> None:
        sock = bound_loopback_socket()
        port = sock.getsockname()[1]
        sock.close()  # nothing listens on the port now
        network = AgentNetwork.over_http({GMA_AGENT: f"http://127.0.0.1:{port}"}, timeout_s=2.0)
        self.addCleanup(network.close)
        with self.assertRaises(AgentUnavailableError) as caught:
            network.gma().health()
        self.assertIsNone(caught.exception.status)
        self.assertNotIn("127.0.0.1", str(caught.exception))
        self.assertNotIn(str(port), str(caught.exception))

    def test_proxy_settings_from_the_environment_are_ignored(self) -> None:
        # A token must never go through a proxy that an environment variable names.
        with self.assertLogs("dagents.common.infrastructure.agent_auth", level="WARNING"):
            open_gma = build_fake_gma(AgentAccess.open(), {})
        fixture_server = LoopbackServer(open_gma).start()
        self.addCleanup(fixture_server.stop)
        with mock.patch.dict("os.environ", {"HTTP_PROXY": "http://127.0.0.1:9", "http_proxy": "http://127.0.0.1:9", "NO_PROXY": "", "no_proxy": ""}):
            network = AgentNetwork.over_http({GMA_AGENT: fixture_server.url("")}, timeout_s=5.0)
        self.addCleanup(network.close)
        self.assertEqual(network.gma().health()["status"], "ok")

    def test_parse_site_map(self) -> None:
        self.assertEqual(parse_site_map("site-a=one, site-b = two", setting="X"), {"site-a": "one", "site-b": "two"})
        self.assertEqual(parse_site_map("site-a=http://h:8082/x=y", setting="X"), {"site-a": "http://h:8082/x=y"})
        self.assertEqual(parse_site_map(None, setting="X"), {})
        self.assertEqual(parse_site_map("  ", setting="X"), {})
        for raw in ("site-a", "=secret-value-1", "site-a=", "site-a=one,site-a=two-secret"):
            with self.subTest(raw=raw):
                with self.assertRaises(AccessConfigurationError) as caught:
                    parse_site_map(raw, setting="GMA_SITE_TOKENS")
                self.assertIn("GMA_SITE_TOKENS", str(caught.exception))
                self.assertNotIn("secret", str(caught.exception))


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


def guarded_app(access: AgentAccess) -> FastAPI:
    app = FastAPI()
    install_agent_access(app, access, agent_name="test agent")

    @app.get("/site-or-coordinator")
    def site_or_coordinator(caller: Annotated[Caller, Depends(require_scope("site", "coordinator"))]):
        return {"scope": caller.scope, "site_id": caller.site_id, "open": caller.is_open}

    @app.put("/rounds/{round_id}/updates/{site_id}")
    def upload(round_id: str, site_id: str, caller: Annotated[Caller, Depends(require_scope("site", bind_site="site_id"))]):
        return {"site_id": site_id, "may_act": caller.may_act_for(site_id)}

    @app.get("/released")
    def released(caller: Annotated[Caller, Depends(require_scope("admin", "site"))]):
        return {"scope": caller.scope, "site_id": caller.site_id}

    return app


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class AccessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.access = AgentAccess(
            [
                TokenGrant("site", LMA_SITE_TOKEN),
                TokenGrant("coordinator", LMA_COORDINATOR_TOKEN),
                TokenGrant("admin", GMA_ADMIN_TOKEN),
                TokenGrant("site", GMA_SITE_TOKEN, site_id=SITE),
            ]
        )
        self.client = TestClient(guarded_app(self.access))

    def test_open_agent_lets_every_caller_in_and_warns_once(self) -> None:
        with self.assertLogs("dagents.common.infrastructure.agent_auth", level="WARNING") as logs:
            client = TestClient(guarded_app(AgentAccess.open()))
        self.assertEqual(len(logs.output), 1)
        self.assertIn("without tokens", logs.output[0])
        self.assertEqual(client.get("/site-or-coordinator").json(), {"scope": None, "site_id": None, "open": True})
        self.assertTrue(client.put("/rounds/r/updates/any-site").json()["may_act"])

    def test_required_tokens_refuse_to_start_without_tokens(self) -> None:
        app = FastAPI()
        with self.assertRaises(AccessConfigurationError) as caught:
            install_agent_access(app, AgentAccess.open(), agent_name="LMA", require_tokens=True)
        self.assertIn("LMA", str(caught.exception))
        install_agent_access(FastAPI(), self.access, agent_name="LMA", require_tokens=True)

    def test_a_scope_without_a_token_is_named_at_startup(self) -> None:
        with self.assertLogs("dagents.common.infrastructure.agent_auth", level="WARNING") as logs:
            install_agent_access(
                FastAPI(),
                AgentAccess([TokenGrant("site", LMA_SITE_TOKEN)]),
                agent_name="LMA",
                expected_scopes=("site", "coordinator"),
            )
        self.assertTrue(any("coordinator" in line for line in logs.output))
        self.assertFalse(any(LMA_SITE_TOKEN in line for line in logs.output))

    def test_missing_malformed_and_unknown_tokens_get_401(self) -> None:
        for headers in ({}, {"Authorization": f"Basic {LMA_SITE_TOKEN}"}, bearer("unknown-token-unknown-token")):
            with self.subTest(headers=list(headers.values())):
                response = self.client.get("/site-or-coordinator", headers=headers)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json()["code"], "unauthorized")
                self.assertEqual(response.headers["www-authenticate"], "Bearer")
                self.assertEqual(set(response.json()), {"detail", "code"})

    def test_a_token_of_another_scope_gets_403(self) -> None:
        response = self.client.get("/site-or-coordinator", headers=bearer(GMA_ADMIN_TOKEN))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json(), {"detail": "this token may not call this route", "code": "forbidden"})

    def test_each_scope_is_accepted(self) -> None:
        self.assertEqual(self.client.get("/site-or-coordinator", headers=bearer(LMA_SITE_TOKEN)).json()["scope"], "site")
        self.assertEqual(
            self.client.get("/site-or-coordinator", headers=bearer(LMA_COORDINATOR_TOKEN)).json()["scope"], "coordinator"
        )
        self.assertEqual(self.client.get("/released", headers=bearer(GMA_ADMIN_TOKEN)).json(), {"scope": "admin", "site_id": None})
        self.assertEqual(self.client.get("/released", headers=bearer(GMA_SITE_TOKEN)).json(), {"scope": "site", "site_id": SITE})

    def test_a_site_token_acts_only_for_its_own_site(self) -> None:
        own = self.client.put(f"/rounds/r/updates/{SITE}", headers=bearer(GMA_SITE_TOKEN))
        self.assertEqual(own.json(), {"site_id": SITE, "may_act": True})
        other = self.client.put(f"/rounds/r/updates/{OTHER_SITE}", headers=bearer(GMA_SITE_TOKEN))
        self.assertEqual(other.status_code, 403)
        self.assertEqual(other.json()["code"], "forbidden")
        self.assertNotIn(SITE, other.json()["detail"])
        # A site token that names no site (an LMA's own site token) is not bound.
        unbound = self.client.put(f"/rounds/r/updates/{OTHER_SITE}", headers=bearer(LMA_SITE_TOKEN))
        self.assertEqual(unbound.status_code, 200)

    def test_every_grant_is_compared_with_compare_digest(self) -> None:
        with mock.patch.object(agent_auth.hmac, "compare_digest", wraps=agent_auth.hmac.compare_digest) as spy:
            response = self.client.get("/site-or-coordinator", headers=bearer(LMA_SITE_TOKEN))
        self.assertEqual(response.status_code, 200)
        # Every grant is compared, also after a match, so the time taken does
        # not tell which grant matched.
        self.assertEqual(spy.call_count, 4)
        for call in spy.call_args_list:
            self.assertIsInstance(call.args[0], bytes)

    def test_bad_grants_are_refused_without_echoing_the_token(self) -> None:
        cases = [
            [TokenGrant("site", "short")],
            [TokenGrant("site", LMA_SITE_TOKEN), TokenGrant("coordinator", LMA_SITE_TOKEN)],
            [TokenGrant("", LMA_SITE_TOKEN)],
            [TokenGrant("site", LMA_SITE_TOKEN + " with space")],
        ]
        for grants in cases:
            with self.subTest(scopes=[grant.scope for grant in grants]):
                with self.assertRaises(AccessConfigurationError) as caught:
                    AgentAccess(grants)
                self.assertNotIn(LMA_SITE_TOKEN, str(caught.exception))
                self.assertNotIn("short", str(caught.exception))

    def test_grants_do_not_show_their_token(self) -> None:
        grant = TokenGrant("site", LMA_SITE_TOKEN, site_id=SITE)
        self.assertNotIn(LMA_SITE_TOKEN, repr(grant))
        self.assertNotIn(LMA_SITE_TOKEN, repr(self.access))

    def test_an_app_without_access_settings_fails_closed(self) -> None:
        app = FastAPI()

        @app.get("/guarded", dependencies=[Depends(require_scope("site"))])
        def guarded():
            return {"reached": True}

        response = TestClient(app, raise_server_exceptions=False).get("/guarded")
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("reached", response.text)

    def test_require_scope_needs_a_scope(self) -> None:
        with self.assertRaises(ValueError):
            require_scope()


if __name__ == "__main__":
    unittest.main()
