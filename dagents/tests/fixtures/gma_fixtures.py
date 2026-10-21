"""Fixture LMAs and an in-process agent network for the GMA tests.

The GMA's HTTP engine calls each site's LMA through its HTTP API. The GMA tests
must not depend on the real LMA, so this module serves the LMA routes the GMA
calls, with the toy extension and the real planner:

| Route | What the fixture does |
|---|---|
| `GET /api/v1/federation/registration` | The site's enrolment, with its record count as the cohort size |
| `POST /api/v1/federation/jobs:offer` | Checks the job's site, model digest and code digest, computes the round digest with the planner, then runs `LocalFederatedWorker.offer` (the `before_train` Guard) |
| `POST /api/v1/federation/jobs:execute` | 409 for a job it did not accept; otherwise `LocalFederatedWorker.execute`, which trains through the toy model kind and sends the clipped update to the GMA's update store with the site's token |
| `POST /api/v1/federation/jobs:run` | The offer, then, when it accepted, the execution, in one call (what the GMA's HTTP engine sends) |
| `GET /api/v1/site`, `GET /api/v1/site/summary` | Fixed descriptions of the site and its counts |
| `GET /api/v1/governance/audit:summary` | The site's audit chain state and length |

Every Guard decision is made by the real ``dagentsc`` planner. A test can make
a fixture refuse offers, wait inside execute, or crash, and it can read every
body the fixture sent to the GMA or answered the GMA with.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse, Response

from dagents.common.application.federation_worker import LocalFederatedWorker, LocalRunOutcome
from dagents.common.application.governance_service import GovernanceService
from dagents.common.domain.federation import (
    FederatedJob,
    JobAcceptance,
    JobModel,
    JobRun,
    SiteRegistration,
    UpdateUpload,
)
from dagents.common.domain.site import AuditSummary, SiteInfo, SiteSourceInfo, SiteSummary
from dagents.common.extensions import ExtensionRegistry
from dagents.common.infrastructure.agent_auth import AgentAccess, TokenGrant, install_agent_access, require_scope
from dagents.common.infrastructure.agent_client import AgentNetwork
from dagents.common.infrastructure.dagents_runner import round_digest
from dagents.tests.fixtures.toy_extension import (
    TOY_CLASSIFICATION,
    TOY_CONDITION_ID,
    TOY_CONTRACT,
    TOY_LABEL_FIELD,
    TOY_PURPOSES,
    ToySiteSource,
    toy_records,
)

SITES = ("site-a", "site-b", "site-c")

#: The GMA's tokens. ``site-x`` has a token but no LMA and is never selected.
ADMIN_TOKEN = "gma-admin-token-aaaaaaaaaaaaaaaaaaaaaaaa"
SITE_TOKENS = {site: f"gma-{site}-token-bbbbbbbbbbbbbbbbbbbb" for site in (*SITES, "site-x")}

#: Each LMA's coordinator token, which the GMA sends to it.
COORDINATOR_TOKENS = {site: f"lma-{site}-coordinator-cccccccccccccccc" for site in (*SITES, "site-d")}

CAPABILITIES = ["profile", "evaluate", "train"]


def _conflict(detail: str) -> JSONResponse:
    return JSONResponse({"detail": detail, "code": "conflict"}, status_code=409)


#: The detail of a store error that a careless site store could write.
STORE_ERROR_DETAIL = (
    "could not connect to postgresql://admin:hunter2@db.example.internal:5432/site "
    "(ExampleCloud direct host; ExampleHost has IPv4 only)"
)


def _store_unavailable() -> JSONResponse:
    return JSONResponse({"detail": STORE_ERROR_DETAIL, "code": "store_unavailable"}, status_code=503)


class ConcurrencyMeter:
    """Counts how many execute calls run at once, across every fixture LMA."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.current = 0
        self.highest = 0

    def __enter__(self) -> ConcurrencyMeter:
        with self._lock:
            self.current += 1
            self.highest = max(self.highest, self.current)
        return self

    def __exit__(self, *exc_info: object) -> None:
        with self._lock:
            self.current -= 1


class _KindRunner:
    """Runs a job through the model kind named in the job, on the Guard's payload."""

    def __init__(self, registry: ExtensionRegistry, seen: list[list[dict[str, Any]]]) -> None:
        self._registry = registry
        self._seen = seen

    def run(self, job: FederatedJob, records: list[dict[str, Any]]) -> LocalRunOutcome:
        self._seen.append([dict(record) for record in records])
        model = JobModel.from_parameters(job.parameters)
        assert model is not None  # the offer refused a job without a model
        kind = self._registry.model_kind(model.kind)
        if job.task == "train":
            outcome = kind.train(model.params, records, TOY_LABEL_FIELD)
            if outcome.update is None:
                return LocalRunOutcome(metrics=outcome.metrics, examples=outcome.examples, update_norm=0.0)
            return LocalRunOutcome(metrics=outcome.metrics, examples=outcome.examples, update=outcome.update)
        if job.task == "evaluate":
            metrics = kind.evaluate(model.params, records, TOY_LABEL_FIELD)
            return LocalRunOutcome(metrics=metrics, examples=int(metrics.get("labelled_count", 0)), update_norm=0.0)
        return LocalRunOutcome(metrics={"record_count": float(len(records))}, examples=len(records), update_norm=0.0)


@dataclass
class FixtureLma:
    """One fixture LMA. Tests may change the switches between calls."""

    site_id: str
    app: FastAPI
    source: ToySiteSource
    governance: GovernanceService
    #: Make every offer refuse with this reason.
    refuse_with: str | None = None
    #: Seconds to wait inside execute, to show that sites run at the same time.
    execute_delay: float = 0.0
    #: Raise inside execute, with a connection string in the message.
    crash_on_execute: bool = False
    #: Report this update digest in the result instead of the one the upload got.
    report_update_digest: str | None = None
    #: Seconds to wait inside the summary route, as a frozen LMA would.
    summary_delay: float = 0.0
    #: Answer this step ("offer", "execute" or "run") with the 503 a site store
    #: that cannot connect gives: its detail names a host, a password and a
    #: vendor. A ``jobs:run`` call fails at its offer or execute step too.
    store_error_on: str | None = None
    #: Put these metrics into the result a ``jobs:run`` answer carries, written
    #: by ``json.dumps``, so a NaN or an infinite value reaches the GMA as the
    #: text ``NaN`` or ``Infinity``, as a faulty LMA would send it.
    answer_metrics: dict[str, float] | None = None
    #: The jobs offered, the payloads the runner received, and the update
    #: uploads sent to the GMA (as JSON bodies).
    offers: list[FederatedJob] = field(default_factory=list)
    runner_payloads: list[list[dict[str, Any]]] = field(default_factory=list)
    uploads: list[dict[str, Any]] = field(default_factory=list)
    #: Every JSON body this LMA answered the GMA with.
    answers: list[Any] = field(default_factory=list)


def build_fixture_lma(
    site_id: str,
    *,
    registry: ExtensionRegistry,
    network: AgentNetwork,
    meter: ConcurrencyMeter | None = None,
    records: list[dict[str, Any]] | None = None,
    upload: Callable[[FederatedJob, dict[str, float]], str] | None = None,
) -> FixtureLma:
    """Build a fixture LMA for ``site_id`` that uploads to the GMA in ``network``."""
    app = FastAPI(title=f"fixture LMA {site_id}")
    install_agent_access(
        app, AgentAccess([TokenGrant("coordinator", COORDINATOR_TOKENS[site_id])]), agent_name=f"fixture LMA {site_id}"
    )
    coordinator = [Depends(require_scope("coordinator"))]
    governance = GovernanceService(registry=registry, strict_classifications=True)
    lma = FixtureLma(
        site_id=site_id,
        app=app,
        source=ToySiteSource(records if records is not None else toy_records(site_id)),
        governance=governance,
    )
    meter = meter or ConcurrencyMeter()
    accepted: set[tuple[str, str]] = set()
    lock = threading.Lock()

    class Channel:
        def submit(self, job: FederatedJob, update: dict[str, float]) -> str:
            body = UpdateUpload(job_digest=job.manifest_digest, update=update)
            lma.uploads.append(body.model_dump(mode="json"))
            if upload is not None:
                return upload(job, update)
            receipt = network.gma(SITE_TOKENS[site_id]).upload_update(job.round_id, site_id, body)
            return receipt.digest

    def worker(job: FederatedJob, expected_digest: str | None) -> LocalFederatedWorker:
        model = JobModel.from_parameters(job.parameters)
        kind = registry.model_kind(model.kind) if model is not None else None
        return LocalFederatedWorker(
            site_id=site_id,
            governance=governance,
            data_source=_Records(lma.source),
            runner=_KindRunner(registry, lma.runner_payloads),
            classification_id=TOY_CLASSIFICATION.classification_id,
            feature_contract_version=TOY_CONTRACT.version,
            approved_fields=["x1", "x2", "group", TOY_LABEL_FIELD],
            approved_purposes=list(TOY_PURPOSES),
            expected_digest=expected_digest,
            update_channel=Channel(),
            update_digest=getattr(kind, "update_digest", None),
        )

    def answered(value: Any) -> Any:
        body = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
        lma.answers.append(json.loads(json.dumps(body)))
        return value

    def refusal(job: FederatedJob, reason: str) -> JobAcceptance:
        return answered(
            JobAcceptance(
                round_id=job.round_id,
                site_id=site_id,
                accepted=False,
                reason=reason,
                feature_contract_version=TOY_CONTRACT.version,
            )
        )

    @app.get("/api/v1/federation/registration", dependencies=coordinator)
    def registration() -> SiteRegistration:
        return answered(
            SiteRegistration(
                site_id=site_id,
                capabilities=list(CAPABILITIES),
                feature_contract_version=TOY_CONTRACT.version,
                approved_conditions=[TOY_CONDITION_ID],
                policy_version="toy-policy-1",
                cohort_size=lma.source.count(),
                enrolled=True,
            )
        )

    def offer_job(job: FederatedJob) -> Any:
        lma.offers.append(job)
        if lma.store_error_on == "offer":
            return _store_unavailable()
        if lma.refuse_with:
            return refusal(job, lma.refuse_with)
        if job.site_id != site_id:
            return refusal(job, "the job is addressed to another site")
        model = JobModel.from_parameters(job.parameters)
        if model is None:
            return refusal(job, "the job carries no model")
        if model.digest != job.manifest.model_artifact_digest:
            return refusal(job, "the job's model is not the model the manifest names")
        kind = registry.model_kind(model.kind)
        if kind.parameter_digest(model.params) != model.digest:
            return refusal(job, "the job's model parameters do not match their digest")
        if kind.code_digest() != job.manifest.training_code_digest:
            return refusal(job, "the manifest names other training code")
        expected = str(round_digest(job.manifest.model_dump(mode="json"))["round_digest"])
        acceptance = worker(job, expected).offer(job)
        if acceptance.accepted:
            with lock:
                accepted.add((job.round_id, job.manifest_digest))
        return answered(acceptance)

    def execute_job(job: FederatedJob) -> Any:
        if lma.crash_on_execute:
            raise RuntimeError("could not reach postgresql://admin:secret@db.example.internal:5432/site")
        if lma.store_error_on == "execute":
            return _store_unavailable()
        with lock:
            key = (job.round_id, job.manifest_digest)
            if key not in accepted:
                return _conflict(f"site {site_id} did not accept this job")
            accepted.discard(key)
        with meter:
            if lma.execute_delay:
                time.sleep(lma.execute_delay)
            result = worker(job, job.manifest_digest).execute(job)
            if lma.report_update_digest is not None:
                result = result.model_copy(update={"update_digest": lma.report_update_digest})
            return answered(result)

    @app.post("/api/v1/federation/jobs:offer", dependencies=coordinator)
    def offer(job: FederatedJob) -> Any:
        return offer_job(job)

    @app.post("/api/v1/federation/jobs:execute", dependencies=coordinator)
    def execute(job: FederatedJob) -> Any:
        return execute_job(job)

    @app.post("/api/v1/federation/jobs:run", dependencies=coordinator)
    def run(job: FederatedJob) -> Any:
        if lma.store_error_on == "run":
            return _store_unavailable()
        acceptance = offer_job(job)
        if isinstance(acceptance, JSONResponse) or not acceptance.accepted:
            return acceptance if isinstance(acceptance, JSONResponse) else JobRun(acceptance=acceptance)
        result = execute_job(job)
        if isinstance(result, JSONResponse):
            return result
        if lma.answer_metrics is not None:
            body = JobRun(acceptance=acceptance, result=result).model_dump(mode="json")
            body["result"]["metrics"].update(lma.answer_metrics)
            return Response(json.dumps(body), media_type="application/json")
        return JobRun(acceptance=acceptance, result=result)

    @app.get("/api/v1/site", dependencies=coordinator)
    def site() -> SiteInfo:
        return answered(
            SiteInfo(
                site_id=site_id,
                condition_id=TOY_CONDITION_ID,
                classification_id=TOY_CLASSIFICATION.classification_id,
                feature_contract_version=TOY_CONTRACT.version,
                label_field=TOY_LABEL_FIELD,
                source=SiteSourceInfo(kind="memory", location=lma.source.location()),
                record_count=lma.source.count(),
                source_version=lma.source.version(),
                capabilities=list(CAPABILITIES),
                policy_version="toy-policy-1",
                minimum_cohort=TOY_CLASSIFICATION.minimum_cohort,
            )
        )

    @app.get("/api/v1/site/summary", dependencies=coordinator)
    def summary() -> SiteSummary:
        if lma.summary_delay:
            time.sleep(lma.summary_delay)
        records = lma.source.records()
        labelled = [record for record in records if isinstance(record.get(TOY_LABEL_FIELD), bool)]
        return answered(
            SiteSummary(
                site_id=site_id,
                record_count=len(records),
                labelled_count=len(labelled),
                label_rate=sum(1 for record in labelled if record[TOY_LABEL_FIELD]) / len(labelled) if labelled else None,
                label_rate_status="released" if labelled else "no_labels",
                minimum_cohort=TOY_CLASSIFICATION.minimum_cohort,
            )
        )

    @app.get("/api/v1/governance/audit:summary", dependencies=coordinator)
    def audit_summary() -> AuditSummary:
        return answered(
            AuditSummary(chain_intact=governance.audit_chain_intact(), record_count=governance.audit_record_count())
        )

    return lma


class _Records:
    """The fixture's records, as the worker's data source reads them."""

    def __init__(self, source: ToySiteSource) -> None:
        self._source = source

    def records(self, job: FederatedJob) -> list[dict[str, Any]]:
        return self._source.records()

    def cohort_size(self, job: FederatedJob) -> int:
        return self._source.count()


class BodyRecorder:
    """An ASGI wrapper that keeps the JSON body of every request an app receives."""

    def __init__(self, app: Any) -> None:
        self._app = app
        self.bodies: list[tuple[str, str, Any]] = []
        self._lock = threading.Lock()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return
        chunks: list[bytes] = []
        more = True
        messages: list[Any] = []
        while more:
            message = await receive()
            messages.append(message)
            chunks.append(message.get("body", b""))
            more = message.get("more_body", False)
        raw = b"".join(chunks)
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            body = raw.decode("utf-8", "replace")
        with self._lock:
            self.bodies.append((scope.get("method", ""), scope.get("path", ""), body))
        replay = iter(messages)

        async def replayed() -> Any:
            try:
                return next(replay)
            except StopIteration:
                return await receive()

        await self._app(scope, replayed, send)


def json_keys(value: Any) -> set[str]:
    """Every key of every object inside a JSON value."""
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            found.add(str(key))
            found |= json_keys(item)
    elif isinstance(value, list):
        for item in value:
            found |= json_keys(item)
    return found


# ---------------------------------------------------------------------------
# A study: one GMA with the HTTP engine and three fixture LMAs, in process
# ---------------------------------------------------------------------------

THRESHOLD_VERSION = "toy-threshold-v1"
THRESHOLD_PARAMS = {"threshold": 0.0}
INITIAL_VERSION = "toy-linear-init"
INITIAL_PARAMS = {"bias": 0.0, "w.x1": 0.0, "w.x2": 0.0}


def toy_registry(extension: Any | None = None) -> ExtensionRegistry:
    """A fresh registry with the toy extension, or the given one."""
    from dagents.tests.fixtures.toy_extension import ToyExtension

    registry = ExtensionRegistry()
    registry.register(extension if extension is not None else ToyExtension())
    return registry


@dataclass
class GmaStudy:
    """A GMA with the HTTP engine and fixture LMAs on one in-process network."""

    network: AgentNetwork
    gma_app: FastAPI
    recorder: BodyRecorder
    lmas: dict[str, FixtureLma]
    registry: ExtensionRegistry
    meter: ConcurrencyMeter

    @property
    def state(self) -> Any:
        """The GMA's services, as ``create_app`` wired them."""
        return self.gma_app.state.gma

    def gma(self, token: str | None = ADMIN_TOKEN) -> Any:
        return self.network.gma(token)

    def close(self) -> None:
        self.network.close()


def build_study(
    *,
    registry: ExtensionRegistry | None = None,
    sites: tuple[str, ...] = SITES,
    stores: dict[str, Any] | None = None,
    **settings: Any,
) -> GmaStudy:
    """Build a GMA (``GMA_ENGINE=http``, tokens on, source reads off) and one fixture LMA per site.

    ``stores`` go into the GMA's :class:`dagents.gma.app_factory.GmaDeps`
    (``rounds``, ``enrolments``, ``updates``); the GMA builds memory stores
    for the ones left out. ``settings`` override the GMA's
    :class:`dagents.gma.config.Settings`.
    """
    from dagents.common.infrastructure.agent_client import GMA_AGENT, lma_agent
    from dagents.gma.app_factory import GmaDeps, create_app
    from dagents.gma.config import Settings

    registry = registry if registry is not None else toy_registry()
    network = AgentNetwork.in_process(thread_tokens=64, timeout_s=30.0, dispatch_timeout_s=60.0)
    try:
        values: dict[str, Any] = {
            "engine": "http",
            "admin_token": ADMIN_TOKEN,
            "site_tokens": dict(SITE_TOKENS),
            "lma_tokens": dict(COORDINATOR_TOKENS),
            "dispatch_concurrency": 3,
            "summary_ttl_seconds": 0.0,
            "allow_source_reads": False,
        }
        values.update(settings)
        gma_app = create_app(Settings(**values), GmaDeps(registry=registry, network=network, **(stores or {})))
        recorder = BodyRecorder(gma_app)
        network.add_app(GMA_AGENT, recorder)
        meter = ConcurrencyMeter()
        lmas = {}
        for site_id in sites:
            lma = build_fixture_lma(site_id, registry=registry, network=network, meter=meter)
            network.add_app(lma_agent(site_id), lma.app)
            lmas[site_id] = lma
    except Exception:
        network.close()
        raise
    return GmaStudy(network=network, gma_app=gma_app, recorder=recorder, lmas=lmas, registry=registry, meter=meter)


def register_models(gma: Any) -> dict[str, str]:
    """Register the toy rule as released and the toy linear model's start as a candidate.

    Returns each version's digest.
    """
    from dagents.common.domain.federation import ModelVersionRegistration

    rule = gma.register_model_version(
        THRESHOLD_VERSION,
        ModelVersionRegistration(kind="toy_threshold", params=dict(THRESHOLD_PARAMS), requested_status="released"),
    )
    start = gma.register_model_version(
        INITIAL_VERSION, ModelVersionRegistration(kind="toy_linear", params=dict(INITIAL_PARAMS))
    )
    return {THRESHOLD_VERSION: rule.digest, INITIAL_VERSION: start.digest}


def toy_manifest(
    round_id: str,
    phase: str,
    model_version: str,
    model_digest: str,
    *,
    invited: tuple[str, ...] = SITES,
    **overrides: Any,
) -> Any:
    """A round manifest the fixture LMAs accept, for the toy condition."""
    from dagents.common.domain.federation import AggregationMethod, RoundManifest
    from dagents.tests.fixtures.toy_extension import ToyLinearKind, ToyThresholdKind

    kind = ToyLinearKind() if model_version != THRESHOLD_VERSION else ToyThresholdKind()
    payload: dict[str, Any] = {
        "round_id": round_id,
        "study_id": "toy-study",
        "condition_id": TOY_CONDITION_ID,
        "phase": phase,
        "model_version": model_version,
        "model_artifact_digest": model_digest,
        "training_code_digest": kind.code_digest(),
        "feature_contract_version": TOY_CONTRACT.version,
        "privacy_profile": "toy-profile-v1",
        "aggregation": AggregationMethod(kind="fedavg"),
        "minimum_participants": 2,
        "minimum_cohort_per_site": 5,
        "required_capabilities": [{"analytics": "profile", "evaluation": "evaluate", "training": "train"}[phase]],
        "invited_sites": list(invited),
    }
    payload.update(overrides)
    return RoundManifest(**payload)


__all__ = [
    "ADMIN_TOKEN",
    "CAPABILITIES",
    "COORDINATOR_TOKENS",
    "INITIAL_PARAMS",
    "INITIAL_VERSION",
    "SITES",
    "SITE_TOKENS",
    "THRESHOLD_PARAMS",
    "THRESHOLD_VERSION",
    "BodyRecorder",
    "ConcurrencyMeter",
    "FixtureLma",
    "GmaStudy",
    "build_fixture_lma",
    "build_study",
    "json_keys",
    "register_models",
    "toy_manifest",
    "toy_registry",
]
