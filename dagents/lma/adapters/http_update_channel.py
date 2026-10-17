"""The update channel of an LMA: a training job's clipped update goes to the GMA over HTTP.

:class:`LocalFederatedWorker` hands the channel the vector the ``before_send``
Guard returned, never the runner's raw output. The channel sends it to the
GMA's write-only update store and returns the digest the GMA computed. It
checks that digest against the model kind's own digest of what it sent, and
the worker checks it again against the site's digest, so a transport that
changed the update refuses the job.

An error message names what went wrong, never an address: the worker puts it
in the result's ``refusal_reason``, which reaches the GMA and the pages that
show rounds.
"""

from __future__ import annotations

from typing import Mapping, Protocol

from dagents.common.domain.federation import FederatedJob
from dagents.common.extensions import ModelKind


class UpdateTransport(Protocol):
    """Where the channel sends the update: the GMA's update store."""

    def upload_update(self, round_id: str, site_id: str, job_digest: str, update: dict[str, float]) -> str:
        """Store one update and return the digest the store computed."""


class UpdateChannelError(RuntimeError):
    """The update was not stored as the site sent it."""


class HttpUpdateChannel:
    """Sends a training job's clipped update to the GMA and checks the digest it answers with.

    Params:
    - `transport`: the GMA link (:class:`dagents.lma.adapters.gma_link.GmaLink`).
    - `kind`: the model kind of the job's model. Its ``check_update`` checks
      the update before it is sent, and its ``update_digest`` is the digest
      the GMA must answer with.
    """

    def __init__(self, transport: UpdateTransport, kind: ModelKind) -> None:
        self._transport = transport
        self._kind = kind

    def submit(self, job: FederatedJob, update: Mapping[str, float]) -> str:
        """Store the update for this job at the GMA and return the GMA's digest of it."""
        if job.task != "train":
            raise UpdateChannelError("only a training job sends a model update")
        checked = self._kind.check_update(update)  # type: ignore[attr-defined]
        digest = self._transport.upload_update(job.round_id, job.site_id, job.manifest_digest, dict(checked))
        if digest != self._kind.update_digest(checked):  # type: ignore[attr-defined]
            raise UpdateChannelError("the GMA stored a different update than the site sent")
        return digest
