"""FastAPI entrypoint for the Dagents healthcare stroke-triage demo.

This app is a consumer of the Dagents framework, not part of it. It contributes
a feature contract, a data classification, a condition pack, and a scoring rule;
the framework supplies validation, restriction planning, guard enforcement,
round planning, quorum, release gates, and workload compilation.

**Every patient record here is synthetic, and nothing this app returns is
clinical advice.** The scoring rule is a transparent demonstration of where a
condition-specific model plugs in, not a validated triage model.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from agents.common.domain.governance import KyuAttribute, Requester, RestrictionRequest
from agents.common.extensions import default_registry

from app.core.config import settings
from app.domain import stroke_rule
from app.domain.conditions import (
    PLANNED_CONDITIONS,
    STROKE_APPROVED_PURPOSES,
    STROKE_CLASSIFICATION,
    STROKE_CONDITION,
    STROKE_FEATURE_CONTRACT,
)
from app.domain.fhir import map_bundle, map_encounter
from app.extension import install
from app.models import (
    FhirIngestRequest,
    GuardProbeRequest,
    PilotRequest,
    TriageRequest,
    WorklistRequest,
)
from app.services.consortium import Consortium
from app.services.framework_client import FrameworkClient

if settings.register_extension:
    install()

consortium = Consortium(
    cohort_size=settings.cohort_size,
    secure_aggregation_threshold=settings.secure_aggregation_threshold,
)
framework = FrameworkClient(settings)

app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    description=(
        "A Dagents demo app for federated stroke-triage support. All data is synthetic; "
        "nothing here is a validated clinical model or clinical advice."
    ),
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _hospital(site_id: str):
    hospital = consortium.hospitals.get(site_id)
    if hospital is None:
        raise HTTPException(status_code=404, detail=f"unknown hospital: {site_id}")
    return hospital


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness plus a standing reminder that the data here is synthetic."""
    return settings.as_health_payload()


@app.get("/api/v1/health")
def health_v1() -> dict[str, str]:
    """Versioned alias for the health endpoint."""
    return health()


@app.get("/api/v1/overview")
def overview() -> dict[str, Any]:
    """The consortium, its hospitals, and the gates a candidate must clear."""
    return consortium.overview()


@app.get("/api/v1/conditions")
def conditions() -> dict[str, Any]:
    """What this app implements, and what it deliberately does not.

    One condition is built. The rest are declared as planned so the boundary
    stays visible: the coordination platform is reusable across conditions, and
    each clinical application is not.
    """
    return {
        "implemented": [STROKE_CONDITION.model_dump(mode="json")],
        "planned": PLANNED_CONDITIONS,
        "feature_contract": STROKE_FEATURE_CONTRACT.model_dump(mode="json"),
        "classification": STROKE_CLASSIFICATION.model_dump(mode="json"),
    }


@app.get("/api/v1/extension")
def extension() -> dict[str, Any]:
    """Exactly what this app contributes to the framework.

    Worth reading next to the endpoint list: everything below is declarative,
    and none of it required changing the framework.
    """
    return {
        "registry": default_registry.describe(),
        "boundary": {
            "framework_owns": [
                "source and schema validation",
                "data-quality rule evaluation",
                "restriction planning (Ethical-Restriction Rails)",
                "guard enforcement and audit",
                "federated round planning, quorum, and aggregation readiness",
                "release gate evaluation",
                "pipeline DAG planning and Kubernetes workload compilation",
            ],
            "app_owns": [
                "the stroke feature contract and its units",
                "which fields are sensitive and under which regulations",
                "the triage scoring rule and its thresholds",
                "FHIR and HL7 mapping into the contract",
                "the clinical intended-use statement and its limitations",
                "the release gates this condition requires",
            ],
        },
    }


@app.post("/api/v1/triage:assess")
def assess(request: TriageRequest) -> dict[str, Any]:
    """Score one canonical record, with the basis for the recommendation."""
    return stroke_rule.assess(request.record).as_dict()


@app.post("/api/v1/triage:worklist")
def worklist(request: WorklistRequest) -> dict[str, Any]:
    """Rank a set of records by triage priority.

    Ranking only. Nothing is removed from the queue, and suppressed cases stay
    in the list rather than disappearing from view.
    """
    return {"worklist": stroke_rule.rank_worklist(request.records)}


@app.post("/api/v1/ingest:fhir")
def ingest_fhir(request: FhirIngestRequest) -> dict[str, Any]:
    """Map a FHIR-shaped bundle into the stroke feature contract.

    Codes the mapper does not recognize are reported, not dropped: a source
    that changed shape should show up as a gap, not as records that quietly
    score lower.
    """
    results = map_bundle(request.bundle)
    return {
        "mapped": [result.as_dict() for result in results],
        "unmapped_codes": sorted({code for result in results for code in result.unmapped}),
        "record_count": len(results),
    }


@app.get("/api/v1/hospitals")
def hospitals() -> list[dict[str, Any]]:
    """The simulated hospitals and how their populations differ."""
    return [
        {
            "site_id": hospital.site_id,
            "display_name": hospital.display_name,
            "cohort_size": len(hospital.data_source.records_held),
            "local_metrics": hospital.local_metrics(),
        }
        for hospital in sorted(consortium.hospitals.values(), key=lambda h: h.site_id)
    ]


@app.get("/api/v1/hospitals/{site_id}/worklist")
def hospital_worklist(site_id: str, limit: int = 25) -> dict[str, Any]:
    """One hospital's ranked worklist, which never leaves the hospital."""
    hospital = _hospital(site_id)
    return {"site_id": site_id, "worklist": hospital.local_worklist(limit)}


@app.get("/api/v1/hospitals/{site_id}/thresholds")
def hospital_thresholds(site_id: str) -> dict[str, Any]:
    """How sensitivity and alert burden trade off at one hospital.

    This is what turns a failed sensitivity gate into a decision a clinical
    owner can actually make, rather than a verdict they can only accept.
    """
    hospital = _hospital(site_id)
    return {"site_id": site_id, "sweep": hospital.threshold_sweep()}


@app.get("/api/v1/hospitals/{site_id}/audit")
def hospital_audit(site_id: str, limit: int = 50) -> dict[str, Any]:
    """One hospital's guard decisions and whether its audit chain verifies."""
    return _hospital(site_id).audit_trail(limit)


@app.post("/api/v1/governance:probe")
def governance_probe(request: GuardProbeRequest) -> dict[str, Any]:
    """Run one request through the Guard and show exactly what came back.

    Change the requester's verification, the boundary, or the granularity and
    the decision changes with it, because the decision is a lookup in the typed
    planner rather than a branch in this app's code.
    """
    hospital = next(iter(sorted(consortium.hospitals.values(), key=lambda h: h.site_id)))
    records = hospital.data_source.records_held[: max(request.record_limit, 0)]
    restriction = RestrictionRequest(
        request_id=f"probe:{request.boundary}:{request.granularity}",
        boundary=request.boundary,  # type: ignore[arg-type]
        requester=Requester(
            requester_id=request.requester_id,
            requester_kind=request.requester_kind,  # type: ignore[arg-type]
            affiliation="stroke-triage-consortium",
            stated_purpose=request.declared_purpose,
            compliance_history=request.compliance_history,
            attributes=[
                KyuAttribute(attribute_id="verified_identity", verified=request.verified),
                KyuAttribute(attribute_id="signed_dua", verified=request.verified),
            ],
        ),
        classification=STROKE_CLASSIFICATION,
        requested_fields=request.requested_fields,
        granularity=request.granularity,  # type: ignore[arg-type]
        cohort_size=request.cohort_size if request.cohort_size is not None else len(records),
        declared_purpose=request.declared_purpose,
        approved_purposes=STROKE_APPROVED_PURPOSES,
    )
    decision = consortium.governance.enforce(restriction, [dict(record) for record in records])
    return decision.model_dump(mode="json")


@app.post("/api/v1/pilot:run")
def run_pilot(request: PilotRequest) -> dict[str, Any]:
    """Run the full governed pilot across every hospital.

    Analytics, then baseline evaluation, then training, then cross-site
    validation of the candidate, then the release gates. The response carries
    the evidence for each step, including the exclusions and rejections, so the
    outcome can be checked rather than taken on trust.
    """
    return consortium.run_pilot(request.round_prefix)


@app.get("/api/v1/framework/status")
def framework_status() -> dict[str, Any]:
    """Which Dagents services are reachable from here."""
    return {"services": framework.service_status()}


@app.get("/api/v1/framework/trace")
def framework_trace(limit: int = 50) -> dict[str, Any]:
    """Run stroke records through the framework's planners, step by step.

    None of these steps know what a stroke is. They work from the contract and
    classification the extension declared.
    """
    hospital = next(iter(sorted(consortium.hospitals.values(), key=lambda h: h.site_id)))
    records = [dict(record) for record in hospital.data_source.records_held[:limit]]
    return {"site_id": hospital.site_id, "record_count": len(records), "trace": framework.planner_trace(records)}
