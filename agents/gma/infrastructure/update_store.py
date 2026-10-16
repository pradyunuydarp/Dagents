"""The GMA's write-only store for the clipped model updates sites send.

A site's LMA sends its clipped update here while it runs a training job
(``PUT /api/v1/federation/rounds/{round_id}/updates/{site_id}``). The GMA's
HTTP engine reads the updates when it aggregates the round, then deletes them.
No route reads an update back, and a ``SiteResult`` never carries one.

``put`` checks each upload against the round as the controller stored it:

| Check | Error |
|---|---|
| The plan selected the site | :class:`NotSelected` (403) |
| The round is a training round that is ``dispatched`` or ``collecting`` and was not discarded | :class:`RoundConflict` (409) |
| ``job_digest`` equals the plan's ``round_digest`` | :class:`RoundConflict` (409) |
| No update from this site is stored for the round yet | :class:`RoundConflict` (409) |
| The model kind's ``check_update`` accepts the update | :class:`RoundConflict` (409) |

The store answers with the digest the model kind computes for the stored
update. The site compares it with its own, so a store that changed an update
gets no contribution.

Updates live in memory only. After a restart the pending updates are gone, so
an aggregation that needs them is refused: the round fails closed.

Once a round is discarded, the store refuses later uploads for it as well. An
LMA that is still running its job when the round is given up would otherwise
leave its update behind after the deletion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import threading
import time

from agents.common.domain.errors import NotSelected, RoundConflict
from agents.common.domain.federation import RoundRecord, UpdateReceipt, UpdateUpload
from agents.common.extensions.registry import ModelKind

#: Round statuses in which a site may still upload its update.
_OPEN_STATUSES = ("dispatched", "collecting")


@dataclass(frozen=True)
class StoredUpdate:
    """One site's stored update for one round.

    ``update`` is a copy; changing it does not change what the store holds.
    """

    round_id: str
    site_id: str
    update: dict[str, float]
    digest: str
    job_digest: str
    received_at: float = field(default=0.0)

    def copy(self) -> StoredUpdate:
        return StoredUpdate(
            round_id=self.round_id,
            site_id=self.site_id,
            update=dict(self.update),
            digest=self.digest,
            job_digest=self.job_digest,
            received_at=self.received_at,
        )


class InMemoryUpdateStore:
    """Clipped updates in this process, keyed by round and site."""

    def __init__(self) -> None:
        self._updates: dict[tuple[str, str], StoredUpdate] = {}
        self._discarded: set[str] = set()
        self._lock = threading.Lock()

    def put(self, record: RoundRecord, site_id: str, upload: UpdateUpload, kind: ModelKind) -> UpdateReceipt:
        """Store one site's update for a round and return its digest.

        Params:
        - `record`: the round, as the controller stores it. The caller answers
          404 when there is none.
        - `site_id`: the site the upload is for. The caller checks that the
          request's token belongs to this site.
        - `upload`: the round digest from the site's job and the update.
        - `kind`: the model kind of the round's starting model. It checks the
          update's names and computes the digest.
        """
        round_id = record.manifest.round_id
        if site_id not in record.plan.selected_sites:
            raise NotSelected(f"site {site_id} was not selected for round {round_id}")
        if record.manifest.phase != "training":
            raise RoundConflict(
                f"round {round_id} is in the {record.manifest.phase} phase; only a training round takes updates"
            )
        if record.abandoned_reason is not None:
            raise RoundConflict(f"round {round_id} was discarded; it takes no more updates")
        if record.status not in _OPEN_STATUSES:
            raise RoundConflict(f"round {round_id} is {record.status}; it takes no more updates")
        if upload.job_digest != record.plan.round_digest:
            raise RoundConflict(f"the job digest does not match the round digest of {round_id}")
        if not getattr(kind, "trainable", False):
            raise RoundConflict(f"round {round_id} starts from a {kind.kind} model, which cannot be trained")
        try:
            vector = {name: float(value) for name, value in kind.check_update(upload.update).items()}
            digest = kind.update_digest(vector)
        except ValueError as error:
            raise RoundConflict(f"the update for {round_id} was refused: {error}") from error

        key = (round_id, site_id)
        with self._lock:
            if round_id in self._discarded:
                raise RoundConflict(f"round {round_id} was discarded; it takes no more updates")
            if key in self._updates:
                raise RoundConflict(f"an update from {site_id} is already stored for {round_id}")
            self._updates[key] = StoredUpdate(
                round_id=round_id,
                site_id=site_id,
                update=vector,
                digest=digest,
                job_digest=upload.job_digest,
                received_at=time.time(),
            )
        return UpdateReceipt(digest=digest)

    def get(self, round_id: str, site_id: str) -> StoredUpdate | None:
        """One site's stored update for a round, as a copy, or ``None``."""
        with self._lock:
            stored = self._updates.get((round_id, site_id))
            return stored.copy() if stored is not None else None

    def updates(self, round_id: str) -> dict[str, StoredUpdate]:
        """Every stored update for a round, by site id in id order, as copies."""
        with self._lock:
            found = {
                site: stored.copy()
                for (stored_round, site), stored in self._updates.items()
                if stored_round == round_id
            }
        return {site: found[site] for site in sorted(found)}

    def discard(self, round_id: str) -> int:
        """Delete every update stored for a round and refuse later uploads for it.

        Returns how many updates were deleted.
        """
        with self._lock:
            self._discarded.add(round_id)
            keys = [key for key in self._updates if key[0] == round_id]
            for key in keys:
                del self._updates[key]
        return len(keys)

    def pending_rounds(self) -> list[str]:
        """Rounds that still have stored updates, in id order."""
        with self._lock:
            return sorted({round_id for round_id, _ in self._updates})


__all__ = ["InMemoryUpdateStore", "StoredUpdate"]
