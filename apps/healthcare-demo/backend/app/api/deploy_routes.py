"""The routes that add a hospital while the demo runs.

- ``GET /api/v1/deployments``: whether this server can add a hospital, which
  hospitals can join, and the attempts so far. Every role that serves it
  answers, and a server that cannot add a hospital says why.
- ``POST /api/v1/deployments``: add one hospital. It starts a process or a
  container, so it is accepted only from this machine (the ``Origin`` check
  below), only when the deployer is on, and never while a pilot runs.

The combined process and the ``app`` process declare both. A hospital's LMA
answers 404 for both, because ``app.api.role_paths.lma_serves`` does not list
them.
"""

from __future__ import annotations

from typing import Any, Callable
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, HTTPException

from app.api.limits import SingleFlight
from app.models import Deployment, DeploymentRequest, DeploymentsView, ErrorResponse, ValidationErrorResponse
from app.services.deployment import DeploymentConflict, DeploymentUnavailable, HospitalDeployer

#: Hosts an ``Origin`` may name for the POST.
LOOPBACK_ORIGIN_HOSTS = ("127.0.0.1", "localhost", "::1")

FOREIGN_ORIGIN_MESSAGE = "Adding a hospital is accepted only from this machine."

#: What a request to add a hospital is told when the flight is held, by who
#: holds it. A pilot and the addition of a hospital share one flight.
DEPLOYMENT_BUSY_MESSAGES = {
    "pilot": "A pilot is running. Add the hospital when it has finished.",
    "deployment": "A hospital is already being added.",
}


def require_loopback_origin(
    origin: str | None = Header(
        default=None,
        description=(
            "Sent by a browser. When present, its host must be `127.0.0.1`, `localhost` or `::1`. A request "
            "without it, such as one from curl, passes."
        ),
    ),
) -> None:
    """Refuse a POST that a page on another host sent.

    A browser names the page's origin. A page on any other host, and a page
    that sends ``Origin: null``, gets 403, so a site a person visits cannot
    make their browser start a process on their machine.
    """
    if origin is None:
        return
    try:
        host = urlsplit(origin.strip()).hostname
    except ValueError:
        host = None
    if host not in LOOPBACK_ORIGIN_HOSTS:
        raise HTTPException(status_code=403, detail=FOREIGN_ORIGIN_MESSAGE)


def _errors(*statuses: tuple[int, str]) -> dict[int | str, dict[str, Any]]:
    return {
        status: {"model": ValidationErrorResponse if status == 422 else ErrorResponse, "description": meaning}
        for status, meaning in statuses
    }


def build_deploy_router(deployer: Callable[[], HospitalDeployer], flight: SingleFlight) -> APIRouter:
    """The two routes. ``deployer`` returns the app's deployer when a request arrives.

    ``flight`` is the pilot's :class:`~app.api.limits.SingleFlight`: an
    addition holds it, so no pilot runs while a hospital joins, and no
    hospital joins while a pilot runs.
    """
    router = APIRouter()

    @router.get("/api/v1/deployments")
    def list_deployments() -> DeploymentsView:
        """Whether this server can add a hospital now, and if not, why.

        Lists every hospital that can join, with the records it starts with and
        whether its service runs, and the attempts since the app started,
        newest first. The checks of core-service and the GMA are reused for 15
        seconds.
        """
        return deployer().view()

    @router.post(
        "/api/v1/deployments",
        dependencies=[Depends(require_loopback_origin)],
        responses=_errors(
            (
                403,
                "The request came from a page on another host, or adding a hospital is not available here. The "
                "detail says which, and why.",
            ),
            (
                409,
                "A pilot is running, a hospital is being added, or the hospital is already in the consortium and "
                "its LMA answers.",
            ),
            (422, "The body is not valid, or `site_id` is not a hospital that can join. The errors field says which."),
        ),
    )
    def add_hospital(request: DeploymentRequest) -> Deployment:
        """Add a hospital to the running consortium, and report each step.

        core-service renders the hospital's Compose file, the app starts that
        one service, the GMA accepts its address and enrols it, the app sends
        the hospital's pages to it, and the GMA's planner plans the next round.
        A step that fails gives `status: failed`, and the app undoes what this
        attempt did. A failed `check` keeps the hospital: `status` stays
        `joined`, and `next_round` gives the planner's reason.
        """
        current = deployer()
        try:
            reason = current.unavailable_reason()
            if reason is not None:
                raise HTTPException(status_code=403, detail=reason)
            if not flight.try_enter("deployment"):
                # The holder may have left since; then the message is an addition's.
                holder = flight.holder or "deployment"
                detail = DEPLOYMENT_BUSY_MESSAGES.get(holder, DEPLOYMENT_BUSY_MESSAGES["deployment"])
                raise HTTPException(status_code=409, detail=detail)
            try:
                return current.deploy(request.site_id)
            except DeploymentUnavailable as unavailable:
                raise HTTPException(status_code=403, detail=str(unavailable)) from None
            except DeploymentConflict as conflict:
                raise HTTPException(status_code=409, detail=str(conflict)) from None
            finally:
                flight.leave()
        finally:
            # The next view checks core-service and the GMA again, so a page
            # shows what changed since this request.
            current.clear_availability()

    return router
