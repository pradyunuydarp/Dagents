"""Internal routes of a hospital's LMA (``HEALTHCARE_DEMO_ROLE=lma``).

The app process and the Dagents GMA call these; the frontend never does. They
are left out of the OpenAPI document, which describes the public API.

- ``GET /api/v1/site/report``: what the hospital reports to the app. No record.
- ``POST /api/v1/federation/jobs:offer`` and ``:execute``: a round job from the
  Dagents GMA (see ``agents.common.infrastructure.federation_http``).

The hospital computes each job's digest itself with the planner and never
trusts the digest the job carries. ``:execute`` runs only a job that
``:offer`` accepted (``Hospital.accepted_jobs``), so a job sent straight to
``:execute`` cannot skip the offer's checks. A training job sends its bounded
update to the app process, not to the GMA.
"""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter

from agents.common.application.federation import FederatedRoundController, InProcessFederationEngine
from agents.common.domain.federation import FederatedJob
from agents.common.infrastructure.federation_http import EXECUTE_PATH, OFFER_PATH

from app.services.remote import SITE_REPORT_PATH
from app.services.roles import SiteNode


def build_site_router(node: Callable[[], SiteNode]) -> APIRouter:
    """The LMA's internal routes. ``node`` returns the process's :class:`SiteNode`."""
    router = APIRouter(include_in_schema=False)
    # Used only for its digest, which the planner computes. Its engine runs nothing.
    digests = FederatedRoundController(engine=InProcessFederationEngine())

    def worker(job: FederatedJob) -> Any:
        current = node()
        hospital = current.hospital(job.site_id)
        return hospital.worker(expected_digest=digests.digest(job.manifest), update_channel=current.update_channel())

    @router.get(SITE_REPORT_PATH)
    def site_report() -> dict[str, Any]:
        return node().report()

    @router.post(OFFER_PATH)
    def offer_job(job: FederatedJob) -> dict[str, Any]:
        return worker(job).offer(job).model_dump(mode="json")

    @router.post(EXECUTE_PATH)
    def execute_job(job: FederatedJob) -> dict[str, Any]:
        return worker(job).execute(job).model_dump(mode="json")

    return router
