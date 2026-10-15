"""The two GMA routes an LMA calls, through the framework's agent client.

- the released model (``GET /api/v1/model-versions:released``), for the site's
  model in use and local evaluation;
- the update upload (``PUT /api/v1/federation/rounds/{round_id}/updates/{site_id}``),
  for a training job's clipped update.

The LMA calls them with the GMA ``site`` token bound to its site. The link maps
the agent client's errors to the LMA's own errors, so a site route or a job
result never carries a host, an address or a token.
"""

from __future__ import annotations

from agents.common.domain.federation import ModelVersion, UpdateUpload
from agents.common.infrastructure.agent_client import AgentError, AgentNetwork, AgentUnavailableError
from agents.lma.application.errors import GmaRefused, GmaUnavailable, NoReleasedModel


class GmaLink:
    """Calls the GMA on one agent network, with the token the GMA gave this site.

    Params:
    - `network`: the network the GMA is on, in process or over HTTP.
    - `token`: the GMA ``site`` token bound to this site. ``None`` sends no
      token, for a GMA that runs without tokens.
    """

    def __init__(self, network: AgentNetwork, token: str | None) -> None:
        self._network = network
        self._token = token

    def released_model(self, known_digest: str | None) -> ModelVersion | None:
        """The released version with its parameters, or ``None`` when its digest is still ``known_digest``."""
        try:
            return self._network.gma(self._token).released_model(known_digest)
        except AgentUnavailableError:
            raise GmaUnavailable("the GMA did not answer") from None
        except AgentError as error:
            if error.status == 404:
                raise NoReleasedModel("the GMA has no released model") from None
            raise GmaRefused(f"the GMA refused to send the released model ({error.code})") from None

    def upload_update(self, round_id: str, site_id: str, job_digest: str, update: dict[str, float]) -> str:
        """Store one clipped update in the GMA's update store and return the digest the GMA computed."""
        try:
            receipt = self._network.gma(self._token).upload_update(
                round_id, site_id, UpdateUpload(job_digest=job_digest, update=update)
            )
        except AgentUnavailableError:
            raise GmaUnavailable("the GMA did not answer") from None
        except AgentError as error:
            raise GmaRefused(f"the GMA refused the update ({error.code}: {error.detail})") from None
        return receipt.digest
