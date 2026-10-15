"""The federated round contracts over HTTP, for sites that run in their own process.

:class:`~agents.common.application.federation.InProcessFederationEngine` calls
each site's worker as a Python object. When each site is its own service (an
LMA per site), the coordinator reaches it over HTTP instead. This module holds
both ends of that transport:

- :class:`HttpSiteWorker` is the coordinator's handle on one remote site. It
  implements ``SiteWorker``, so an engine dispatches to it like any other
  worker. It sends the job and returns what the site answered. It never
  decides anything for the site.
- :class:`HttpUpdateChannel` is the site's handle on the coordinator's update
  store. It implements ``UpdateChannel``, so ``LocalFederatedWorker`` sends its
  Guard-bounded update through it exactly as it would in one process.

The paths are fixed here, so both ends agree:

- ``POST {site}/api/v1/federation/jobs:offer``: body a ``FederatedJob``,
  answer a ``JobAcceptance``.
- ``POST {site}/api/v1/federation/jobs:execute``: body a ``FederatedJob``,
  answer a ``SiteResult``.
- ``POST {coordinator}/api/v1/federation/rounds/{round_id}/updates``: body an
  :class:`UpdateSubmission`, answer ``{"update_digest": ...}``.

A site that cannot be reached is recorded, never skipped: an offer that fails
is a refusal and an execution that fails is a ``failed`` result, so a round's
evidence names every selected site. The transport has no authentication. It is
meant for a private network, such as a compose network; a deployment across
organisations needs mutual TLS or signed requests in front of it.
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import Field

from agents.common.domain.base import DagentsModel
from agents.common.domain.federation import FederatedJob, JobAcceptance, SiteResult

#: Where a site accepts or refuses a job.
OFFER_PATH = "/api/v1/federation/jobs:offer"

#: Where a site runs a job it accepted.
EXECUTE_PATH = "/api/v1/federation/jobs:execute"

#: Where the coordinator stores a site's bounded update for a round.
UPDATE_PATH = "/api/v1/federation/rounds/{round_id}/updates"

#: Seconds to wait for a site or the coordinator. A training job runs locally
#: before it answers, so this is longer than a health check's timeout.
DEFAULT_TIMEOUT_SECONDS = 60.0


class UpdateSubmission(DagentsModel):
    """One site's bounded update, as it travels to the coordinator."""

    job: FederatedJob = Field(description="The job that produced the update.")
    update: dict[str, float] = Field(description="The vector the site's `before_send` Guard returned.")


class UpdateReceipt(DagentsModel):
    """What the coordinator stored."""

    update_digest: str = Field(description="The coordinator's digest of the update it stored.")


def _base(url: str) -> str:
    return url.rstrip("/")


def _short(error: object, limit: int = 200) -> str:
    text = " ".join(str(error).split()) or type(error).__name__
    return text if len(text) <= limit else text[:limit] + "…"


def _why_unreachable(error: Exception, timeout: float) -> str:
    """Say why a call failed, in words that never include the address.

    The reason is stored in a round's evidence, which the coordinator shows.
    httpx's own messages name the URL, so they are not used for HTTP errors.
    """
    if isinstance(error, httpx.HTTPStatusError):
        return f"the site answered with status {error.response.status_code}"
    if isinstance(error, httpx.TimeoutException):
        return f"the site did not answer within {timeout:g} seconds"
    if isinstance(error, httpx.HTTPError):
        return "the connection to the site failed"
    return "the site's answer was not in the expected format"


class HttpSiteWorker:
    """A site worker that runs in another process, reached over HTTP.

    Params:
    - `site_id`: the site's id in the study.
    - `base_url`: the site's address, such as ``http://lma-uk-134:8082``.
    - `timeout`: seconds to wait for each call.
    - `client`: an ``httpx.Client`` to use instead of opening one per call,
      for tests (a FastAPI ``TestClient`` works).
    """

    def __init__(
        self,
        site_id: str,
        base_url: str,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        client: httpx.Client | None = None,
    ) -> None:
        self.site_id = site_id
        self._base_url = _base(base_url)
        self._timeout = timeout
        self._client = client

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        url = f"{self._base_url}{path}"
        if self._client is not None:
            response = self._client.post(url, json=body, timeout=self._timeout)
        else:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.post(url, json=body)
        response.raise_for_status()
        return response.json()

    def offer(self, job: FederatedJob) -> JobAcceptance:
        """Send the job to the site. A site that does not answer has refused it."""
        try:
            acceptance = JobAcceptance.model_validate(self._post(OFFER_PATH, job.model_dump(mode="json")))
        except (httpx.HTTPError, ValueError) as error:
            return self._refusal(job, _why_unreachable(error, self._timeout))
        return acceptance

    def execute(self, job: FederatedJob) -> SiteResult:
        """Ask the site to run the job. A site that does not answer has failed it."""
        try:
            result = SiteResult.model_validate(self._post(EXECUTE_PATH, job.model_dump(mode="json")))
        except (httpx.HTTPError, ValueError) as error:
            return self._failure(job, _why_unreachable(error, self._timeout))
        return result

    def _refusal(self, job: FederatedJob, reason: str) -> JobAcceptance:
        return JobAcceptance(round_id=job.round_id, site_id=self.site_id, accepted=False, reason=reason)

    def _failure(self, job: FederatedJob, reason: str) -> SiteResult:
        return SiteResult(
            round_id=job.round_id,
            site_id=self.site_id,
            job_digest=job.manifest_digest,
            participation="failed",
            local_evidence_pointer=f"unreachable://{self.site_id}#{reason}",
        )


class HttpUpdateChannel:
    """The coordinator's update store, reached over HTTP from a site.

    ``submit`` raises when the coordinator refuses or cannot be reached.
    ``LocalFederatedWorker`` turns that into a refused job, so a lost update
    never becomes a completed result.
    """

    def __init__(
        self,
        coordinator_url: str,
        *,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = _base(coordinator_url)
        self._timeout = timeout
        self._client = client

    def submit(self, job: FederatedJob, update: dict[str, float]) -> str:
        """Send one bounded update and return the digest the coordinator stored."""
        url = f"{self._base_url}{UPDATE_PATH.format(round_id=job.round_id)}"
        body = UpdateSubmission(job=job, update=dict(update)).model_dump(mode="json")
        if self._client is not None:
            response = self._client.post(url, json=body, timeout=self._timeout)
        else:
            with httpx.Client(timeout=self._timeout) as client:
                response = client.post(url, json=body)
        if response.status_code >= 400:
            detail: object = response.text
            try:
                answer = response.json()
            except ValueError:
                answer = None
            if isinstance(answer, dict) and "detail" in answer:
                detail = answer["detail"]
            raise RuntimeError(f"the coordinator answered {response.status_code}: {_short(detail)}")
        return UpdateReceipt.model_validate(response.json()).update_digest
