"""How a hospital's LMA reads the hospital's store.

The framework's LMA reads a site's records through a site record source
(``dagents.common.application.ports.SiteRecordSource``). :class:`StoreSiteSource`
is that source for this app: it reads the hospital's own store
(:mod:`app.services.site_store`), so the LMA and the app's hospital views read
the same rows through the same code, acting as the same role.

- In process, the app hands each LMA ``StoreSiteSource(store)`` on the store it
  writes entries through.
- An LMA started on its own (``LMA_SITE_SOURCE=healthcare_site_store``) builds
  its source with :func:`site_store_source`, on the LMA's own lease.

The version
-----------

The LMA keeps the records and the running profile it built from them until the
source's version changes, so the version must change with every write, and it
must be cheap: the LMA asks for it before every federated job. The version is
``"<writes>:<total>:<labelled>"``:

- ``writes`` is the store's :attr:`~app.services.site_store.SiteStore.write_count`,
  which every write through the same store object raises at once.
- ``total`` and ``labelled`` come from one count query, read at most once every
  ``recount_seconds``. They catch writes by another process, such as the seed
  script, or the demo API when the LMA runs in its own process. Entries are only
  added and an outcome is set once, so each such write changes one of the two
  counts.

In process the app writes through the store the LMA reads, so the count query
is needed only for other processes, and it runs at most every
:data:`RECOUNT_SECONDS`. An LMA started on its own sees no write through its
own store object, so :func:`site_store_source` reads the counts every time the
LMA asks for the version. That is at most once per staleness interval for a
read, and once per refresh or federated job.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from app.domain.dataset import UnknownSiteError, site_for
from app.services.site_store import PostgresSiteStore, SiteStore

#: The name the app's extension contributes this source under (``site_sources()``).
SITE_SOURCE_NAME = "healthcare_site_store"

#: Seconds between count queries for a source the app builds in process.
RECOUNT_SECONDS = 30.0


class StoreSiteSource:
    """One hospital's store, as the framework's site record source.

    Params:
    - `store`: the hospital's store.
    - `recount_seconds`: how long the counts in the version are used before
      they are read again. ``0`` reads them every time.
    - `close_store`: whether :meth:`close` closes the store. Only a source
      that built its own store sets it.
    - `clock`: monotonic seconds; tests replace it.

    ``kind`` is the store's kind (``postgres`` or ``in-memory``) and ``role``
    the role it acts as (``None`` in memory). The LMA reports both in
    ``GET /api/v1/site``.
    """

    def __init__(
        self,
        store: SiteStore,
        *,
        recount_seconds: float = RECOUNT_SECONDS,
        close_store: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if recount_seconds < 0:
            raise ValueError(f"recount_seconds must be 0 or more, got {recount_seconds!r}")
        self._store = store
        self._recount_seconds = float(recount_seconds)
        self._close_store = close_store
        self._clock = clock
        self._lock = threading.Lock()
        self._counts: tuple[int, int] | None = None
        self._counted_at = 0.0
        self.kind: str = store.kind
        self.role: str | None = getattr(store, "role", None)

    @property
    def store(self) -> SiteStore:
        """The store this source reads."""
        return self._store

    def records(self) -> list[dict[str, Any]]:
        """Every record, in the store's order: dataset rows in source order, then entries."""
        return self._store.records()

    def count(self) -> int:
        """How many records the store holds now."""
        return int(self._store.count()["total"])

    def version(self) -> str:
        """``"<writes>:<total>:<labelled>"``. It changes with every write through the store."""
        with self._lock:
            now = self._clock()
            if self._counts is None or now - self._counted_at >= self._recount_seconds:
                counts = self._store.count()
                self._counts = (int(counts["total"]), int(counts["labelled"]))
                self._counted_at = now
            total, labelled = self._counts
        return f"{self._store.write_count}:{total}:{labelled}"

    def location(self) -> dict[str, str]:
        """Where the records are: the schema and role, or ``{"memory": "process"}``. Never a host."""
        return dict(self._store.location)

    def close(self) -> None:
        """Close the store when this source built it; otherwise do nothing."""
        if self._close_store:
            self._store.close()


def site_store_source(lease: Any, role: str, site_id: str) -> StoreSiteSource:
    """Build the source an LMA started on its own reads a hospital with.

    Params:
    - `lease`: the LMA's kept connection
      (``dagents.common.infrastructure.postgres_lease.KeptConnection``). The
      store runs on it and leaves it open; the LMA closes it.
    - `role`: the LMA's ``LMA_SITE_ROLE``. It must be the hospital's own role.
    - `site_id`: the LMA's ``LMA_SITE_ID``, one of the demo hospitals.

    Returns a source that reads the counts every time the LMA asks for the
    version, because the demo API writes from another process.

    Raises ``ValueError`` when there is no database (separate processes cannot
    share an in-memory store, so an entry made through the demo API would never
    reach the LMA), when the hospital is unknown, or when the role is not the
    hospital's own. Nothing is opened until the LMA reads.
    """
    if lease is None:
        raise ValueError(
            f"the {SITE_SOURCE_NAME} source for {site_id} needs the hospital's database: set LMA_DATABASE_URL"
        )
    try:
        site = site_for(site_id)
    except UnknownSiteError:
        raise ValueError(f"{site_id!r} is not one of the demo hospitals") from None
    if role != site.role:
        raise ValueError(f"the store of {site_id} is read as role {site.role}, not {role!r}")
    store = PostgresSiteStore(site, lease=lease)
    return StoreSiteSource(store, recount_seconds=0.0, close_store=True)


__all__ = ["RECOUNT_SECONDS", "SITE_SOURCE_NAME", "StoreSiteSource", "site_store_source"]
