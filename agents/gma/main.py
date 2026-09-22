"""FastAPI entrypoint for the Global Monitoring Agent."""

from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, TypeAdapter

from agents.common.domain.federation import (
    CandidateModel,
    ReleaseGate,
    RoundManifest,
    SiteRegistration,
    SiteResult,
)
from agents.common.domain.governance import DataClassification, RestrictionRequest
from agents.common.domain.models import SourceSpec
from agents.common.extensions import default_registry
from agents.gma.config import settings
from agents.gma.di import (
    build_aggregation_service,
    build_governance_service,
    build_round_controller,
)
from agents.gma.domain.models import (
    AgentIdentity,
    DatasetProfileRequest,
    DeploymentSyncRequest,
    DesiredDeploymentRequest,
    HeartbeatRequest,
    ModelExecutionRequest,
    RegisterRequest,
    RunDispatchRequest,
    TelemetryEnvelope,
)


service = build_aggregation_service()
governance = build_governance_service()
rounds = build_round_controller()

app = FastAPI(title=settings.app_name, version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    """Return the raw health payload used by the GMA's internal checks."""
    return service.health_payload()


@app.get("/api/v1/health")
def health_v1() -> dict[str, str]:
    """Versioned alias for the GMA health endpoint."""
    return health()


@app.post("/register")
def register(request: RegisterRequest):
    """Register one LMA with the aggregate control plane."""
    return service.register(request)


@app.put("/api/v1/agents/{agent_id}/registration")
def register_v1(agent_id: str, request: RegisterRequest):
    """Versioned registration endpoint keyed by path-level `agent_id`.

    The path id wins over any mismatched id in the request body so route-level
    identity stays authoritative.
    """
    payload = request if request.agent.agent_id == agent_id else request.model_copy(
        update={"agent": AgentIdentity(**{**request.agent.model_dump(), "agent_id": agent_id})}
    )
    return register(payload)


@app.post("/heartbeat")
def heartbeat(request: HeartbeatRequest):
    """Ingest one heartbeat update from a registered LMA."""
    return service.heartbeat(request)


@app.post("/api/v1/agents/{agent_id}/heartbeats")
def heartbeat_v1(agent_id: str, request: HeartbeatRequest):
    """Versioned heartbeat endpoint keyed by path-level `agent_id`."""
    payload = request if request.agent.agent_id == agent_id else request.model_copy(
        update={"agent": AgentIdentity(**{**request.agent.model_dump(), "agent_id": agent_id})}
    )
    return heartbeat(payload)


@app.post("/telemetry")
def telemetry(request: TelemetryEnvelope):
    """Ingest one telemetry envelope into the aggregate repository."""
    return service.ingest_telemetry(request)


@app.post("/api/v1/agents/{agent_id}/telemetry")
def telemetry_v1(agent_id: str, request: TelemetryEnvelope):
    """Versioned telemetry endpoint keyed by path-level `agent_id`."""
    payload = request if request.agent.agent_id == agent_id else request.model_copy(
        update={"agent": AgentIdentity(**{**request.agent.model_dump(), "agent_id": agent_id})}
    )
    return telemetry(payload)


@app.post("/deployments/sync")
def sync_deployment(request: DeploymentSyncRequest):
    """Compare an LMA's current bundle with the desired deployment plan."""
    return service.sync_deployment(request)


@app.post("/api/v1/agents/{agent_id}/deployment-sync")
def sync_deployment_v1(agent_id: str, request: DeploymentSyncRequest):
    """Versioned deployment-sync endpoint keyed by path-level `agent_id`."""
    payload = request if request.agent.agent_id == agent_id else request.model_copy(
        update={"agent": AgentIdentity(**{**request.agent.model_dump(), "agent_id": agent_id})}
    )
    return sync_deployment(payload)


@app.post("/deployments/plan")
def plan_deployment(request: DesiredDeploymentRequest):
    """Store the desired deployment state for one LMA."""
    return service.plan_deployment(request)


@app.put("/api/v1/agents/{agent_id}/desired-deployment")
def plan_deployment_v1(agent_id: str, request: DesiredDeploymentRequest):
    """Versioned desired-deployment endpoint keyed by path-level `agent_id`."""
    payload = request if request.agent_id == agent_id else request.model_copy(update={"agent_id": agent_id})
    return plan_deployment(payload)


@app.get("/deployments")
def list_deployments():
    """List current desired deployment plans known to the GMA."""
    return service.list_deployments()


@app.post("/runs/dispatch")
def dispatch_run(request: RunDispatchRequest):
    """Record one aggregate run dispatch for downstream execution systems."""
    return service.dispatch_run(request)


@app.get("/runs/dispatch")
def list_dispatched_runs(limit: int = 20):
    """List recent run dispatches issued by the GMA."""
    return service.list_dispatched_runs(limit=limit)


@app.get("/agents")
def list_agents():
    """List agents currently registered with the GMA."""
    return service.list_agents()


@app.get("/api/v1/agents")
def list_agents_v1():
    """Versioned alias for agent listing."""
    return list_agents()


@app.get("/api/v1/agents/{agent_id}")
def get_agent_v1(agent_id: str):
    """Fetch one registered agent snapshot by id."""
    return service.get_agent(agent_id)


@app.post("/datasets/profile")
def profile_assimilated_dataset(request: DatasetProfileRequest):
    """Profile an assimilated dataset before aggregate model selection."""
    return service.profile_assimilated_dataset(request)


@app.post("/api/v1/datasets:profile")
def profile_assimilated_dataset_v1(request: DatasetProfileRequest):
    """Versioned alias for assimilated dataset profiling."""
    return profile_assimilated_dataset(request)


@app.post("/models/run")
def run_assimilated_model(request: ModelExecutionRequest):
    """Execute an aggregate model run synchronously."""
    return service.run_assimilated_model(request)


@app.post("/api/v1/model-jobs", status_code=202)
def run_assimilated_model_v1(request: ModelExecutionRequest):
    """Versioned alias that presents aggregate execution as a model-job API."""
    return run_assimilated_model(request)


@app.get("/models/runs")
def list_model_runs(limit: int = 20):
    """List recent aggregate model runs tracked by the GMA."""
    return service.list_model_runs(limit=limit)


@app.get("/api/v1/model-jobs")
def list_model_runs_v1(limit: int = 20):
    """Versioned alias for aggregate model-job listing."""
    return list_model_runs(limit=limit)


@app.post("/api/v1/sources")
def register_source(payload: dict[str, Any]):
    """Register a reusable source definition for aggregate workflows."""
    source = TypeAdapter(SourceSpec).validate_python(payload)
    return service.register_source(source)


@app.get("/api/v1/sources")
def list_sources():
    """List all sources registered with the GMA."""
    return service.list_sources()


@app.get("/api/v1/sources/{source_id}")
def get_source(source_id: str):
    """Fetch one source registered with the GMA."""
    return service.get_source(source_id)


@app.post("/api/v1/sources/{source_id}:validate")
def validate_source(source_id: str):
    """Run shared source validation for a registered source id."""
    return service.validate_source(source_id)

@app.get("/telemetry/recent")
def recent_telemetry(limit: int = 20):
    """List recent telemetry envelopes received from registered agents."""
    return service.recent_telemetry(limit=limit)


@app.get("/telemetry/summary")
def telemetry_summary():
    """Return aggregate telemetry rollups by agent."""
    return service.telemetry_summary()


@app.get("/overview")
def overview():
    """Return a fleet-level summary of agent and deployment state."""
    return service.overview()


class GuardEnforcementRequest(BaseModel):
    """A restriction request plus the payload the Guard should enforce against."""

    request: RestrictionRequest
    payload: list[dict[str, Any]] = Field(default_factory=list)
    correlation_id: str | None = None


class ReleaseEvaluationRequest(BaseModel):
    """A candidate's measured metrics, with the gates it must clear.

    ``baseline_metrics`` are the current approved model's, needed by any gate
    that asks for an improvement rather than an absolute threshold.
    """

    gates: list[ReleaseGate] = Field(default_factory=list)
    candidate_version: str
    candidate_metrics: dict[str, float] = Field(default_factory=dict)
    baseline_metrics: dict[str, float] = Field(default_factory=dict)
    rollback_version: str | None = None


@app.get("/governance/classifications")
def list_classifications():
    """List the data classifications this agent can enforce against."""
    return governance.list_classifications()


@app.get("/api/v1/governance/classifications")
def list_classifications_v1():
    """Versioned alias for listing data classifications."""
    return list_classifications()


@app.put("/governance/classifications/{classification_id}")
def register_classification(classification_id: str, classification: DataClassification):
    """Register or replace one runtime data classification."""
    if classification.classification_id != classification_id:
        raise HTTPException(
            status_code=400,
            detail=f"path id {classification_id} does not match body id {classification.classification_id}",
        )
    return governance.register_classification(classification)


@app.put("/api/v1/governance/classifications/{classification_id}")
def register_classification_v1(classification_id: str, classification: DataClassification):
    """Versioned alias for registering a data classification."""
    return register_classification(classification_id, classification)


@app.post("/governance/restrictions:plan")
def plan_restriction(request: RestrictionRequest):
    """Ask what protection a request would need, without enforcing anything."""
    return governance.plan(request)


@app.post("/api/v1/governance/restrictions:plan")
def plan_restriction_v1(request: RestrictionRequest):
    """Versioned alias for restriction planning."""
    return plan_restriction(request)


@app.post("/governance/restrictions:enforce")
def enforce_restriction(payload: GuardEnforcementRequest):
    """Plan, apply, and record one governed request against a payload."""
    return governance.enforce(payload.request, payload.payload, correlation_id=payload.correlation_id)


@app.post("/api/v1/governance/restrictions:enforce")
def enforce_restriction_v1(payload: GuardEnforcementRequest):
    """Versioned alias for guard enforcement."""
    return enforce_restriction(payload)


@app.get("/governance/audit")
def governance_audit(limit: int = 50):
    """List recent guard decisions and whether the digest chain still verifies."""
    return {
        "chain_intact": governance.audit_chain_intact(),
        "records": governance.recent_audit(limit=limit),
    }


@app.get("/api/v1/governance/audit")
def governance_audit_v1(limit: int = 50):
    """Versioned alias for the guard audit log."""
    return governance_audit(limit=limit)


@app.put("/federation/sites/{site_id}")
def register_site(site_id: str, registration: SiteRegistration):
    """Enrol or update one site in the consortium."""
    if registration.site_id != site_id:
        raise HTTPException(
            status_code=400, detail=f"path id {site_id} does not match body id {registration.site_id}"
        )
    return rounds.register_site(registration)


@app.put("/api/v1/federation/sites/{site_id}")
def register_site_v1(site_id: str, registration: SiteRegistration):
    """Versioned alias for site enrolment."""
    return register_site(site_id, registration)


@app.get("/federation/sites")
def list_sites():
    """List enrolled sites."""
    return rounds.list_sites()


@app.get("/api/v1/federation/sites")
def list_sites_v1():
    """Versioned alias for listing enrolled sites."""
    return list_sites()


@app.post("/federation/rounds:plan")
def plan_round(manifest: RoundManifest):
    """Select the sites eligible for a round, without dispatching it."""
    return rounds.plan_round(manifest)


@app.post("/api/v1/federation/rounds:plan")
def plan_round_v1(manifest: RoundManifest):
    """Versioned alias for round planning."""
    return plan_round(manifest)


@app.post("/federation/rounds")
def dispatch_round(manifest: RoundManifest):
    """Plan a round and, if quorum allows, offer it to the selected sites."""
    return rounds.dispatch_round(manifest)


@app.post("/api/v1/federation/rounds", status_code=202)
def dispatch_round_v1(manifest: RoundManifest):
    """Versioned alias for round dispatch."""
    return dispatch_round(manifest)


@app.get("/federation/rounds")
def list_rounds(limit: int = 20):
    """List recent rounds, newest first."""
    return rounds.list_rounds(limit=limit)


@app.get("/api/v1/federation/rounds")
def list_rounds_v1(limit: int = 20):
    """Versioned alias for listing rounds."""
    return list_rounds(limit=limit)


@app.get("/federation/rounds/{round_id}")
def get_round(round_id: str):
    """Fetch one round's full lineage."""
    record = rounds.get_round(round_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"unknown round: {round_id}")
    return record


@app.get("/api/v1/federation/rounds/{round_id}")
def get_round_v1(round_id: str):
    """Versioned alias for fetching one round."""
    return get_round(round_id)


@app.post("/federation/rounds/{round_id}/results")
def submit_result(round_id: str, result: SiteResult):
    """Record one site's returned result against its round."""
    if result.round_id != round_id:
        raise HTTPException(
            status_code=400, detail=f"path round {round_id} does not match body round {result.round_id}"
        )
    try:
        return rounds.submit_result(result)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/v1/federation/rounds/{round_id}/results", status_code=202)
def submit_result_v1(round_id: str, result: SiteResult):
    """Versioned alias for submitting a site result."""
    return submit_result(round_id, result)


@app.get("/federation/rounds/{round_id}/readiness")
def round_readiness(round_id: str):
    """Report whether the returned contributions may be aggregated."""
    try:
        return rounds.evaluate_readiness(round_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/v1/federation/rounds/{round_id}/readiness")
def round_readiness_v1(round_id: str):
    """Versioned alias for aggregation readiness."""
    return round_readiness(round_id)


@app.post("/federation/rounds/{round_id}:aggregate")
def aggregate_round(round_id: str):
    """Produce a candidate model, if and only if aggregation is permitted.

    A candidate is not a release. It becomes one only after the release gates
    pass and a human committee approves it.
    """
    try:
        return rounds.aggregate(round_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/v1/federation/rounds/{round_id}:aggregate")
def aggregate_round_v1(round_id: str):
    """Versioned alias for candidate aggregation."""
    return aggregate_round(round_id)


@app.post("/federation/rounds/{round_id}:evaluate-release")
def evaluate_release(round_id: str, payload: ReleaseEvaluationRequest):
    """Evaluate release gates against a candidate and record the verdict.

    The Guard runs first, at the release boundary, so a release evaluation is
    itself a governed act with an audit record.
    """
    record = rounds.get_round(round_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"unknown round: {round_id}")
    candidate = CandidateModel(
        round_id=round_id,
        candidate_version=payload.candidate_version,
        parent_version=record.manifest.model_version,
        aggregation_method=record.manifest.aggregation.kind,
        metrics=payload.candidate_metrics,
    )
    return rounds.evaluate_release(
        round_id,
        payload.gates,
        candidate,
        baseline_metrics=payload.baseline_metrics,
        rollback_version=payload.rollback_version,
    )


@app.post("/api/v1/federation/rounds/{round_id}:evaluate-release")
def evaluate_release_v1(round_id: str, payload: ReleaseEvaluationRequest):
    """Versioned alias for release evaluation."""
    return evaluate_release(round_id, payload)


@app.get("/extensions")
def list_extensions():
    """Describe what registered extensions contribute to this agent."""
    return default_registry.describe()


@app.get("/api/v1/extensions")
def list_extensions_v1():
    """Versioned alias for the extension summary."""
    return list_extensions()
