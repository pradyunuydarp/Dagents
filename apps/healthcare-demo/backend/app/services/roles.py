"""What each process of the demo runs, by ``HEALTHCARE_DEMO_ROLE``.

- ``combined``: :class:`~app.services.consortium.Consortium` with every
  hospital in this process. The hosted demo and most tests use it.
- ``lma``: :class:`SiteNode`, one hospital. It loads models from the app
  process with :class:`~app.services.remote.RemoteModelRegistry`, sends its
  bounded updates there with ``HttpUpdateChannel``, and keeps its round
  contributions in a store of its own.
- ``app``: a ``Consortium`` built by :func:`build_app_consortium`. Its
  hospitals are the LMAs (:class:`~app.services.remote.RemoteHospital`), the
  Dagents GMA runs its rounds (:class:`~app.services.remote.GmaRoundClient`)
  and its release Guard (:class:`~app.services.remote.GmaReleaseGuard`). It
  keeps the model registry and the update store.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

import httpx

from agents.common.infrastructure.federation_http import HttpUpdateChannel
from agents.common.infrastructure.charts import MAX_SERIES as MAX_CHART_SERIES

from app.core.config import Settings
from app.domain.conditions import STROKE_CLASSIFICATION
from app.domain.dataset import SITES, Site, UnknownSiteError, site_for
from app.services.consortium import Consortium, build_site_store
from app.services.hospital import Hospital
from app.services.remote import GmaReleaseGuard, GmaRoundClient, RemoteHospital, RemoteModelRegistry
from app.services.site_store import (
    CoordinatorStore,
    MemoryCoordinatorStore,
    PostgresCoordinatorStore,
    SiteStore,
    site_database_url,
)

#: The roles a process can take.
ROLES = ("combined", "lma", "app")

#: Calls a hospital makes to the app while it answers one call from the GMA:
#: at most the model a job names and the job's update.
CALLS_TO_THE_APP_PER_JOB_CALL = 2


def round_timeout(job_timeout: float, hospitals: int) -> float:
    """Seconds the app waits for the Dagents GMA to dispatch one round.

    The GMA offers the job to each hospital and then runs it there, one
    hospital after another, and waits up to ``job_timeout`` for each call
    (``GMA_FEDERATION_TIMEOUT_SECONDS``). So a round with one slow hospital
    takes up to two calls per hospital. One more ``job_timeout`` covers the
    GMA's own planning. A shorter wait gives up while the GMA is still
    recording the slow hospital as refused or failed, and the pilot loses its
    report.
    """
    return (2 * hospitals + 1) * job_timeout


def coordinator_timeout(job_timeout: float) -> float:
    """Seconds a hospital waits for each call to the app.

    Every call a hospital makes to the app, for one call from the GMA, must end
    before the GMA stops waiting for the hospital, so a slow app is reported
    by the hospital rather than by a GMA timeout. Half of ``job_timeout`` is
    left for the hospital's own work.
    """
    return job_timeout / (2 * CALLS_TO_THE_APP_PER_JOB_CALL)


def check_role(settings: Settings) -> str:
    """``settings.role``, or ``ValueError`` naming the roles that exist."""
    if settings.role not in ROLES:
        raise ValueError(f"HEALTHCARE_DEMO_ROLE is {settings.role!r}; it must be one of {', '.join(ROLES)}")
    return settings.role


def _series_slot(site: Site) -> int | None:
    """An LMA's chart colour: its place among the demo's hospitals, as in one process.

    The default ``HEALTHCARE_DEMO_LMA_URLS`` lists the hospitals in this order,
    so a hospital's own charts and the consortium charts agree.
    """
    if len(SITES) > MAX_CHART_SERIES or site not in SITES:
        return None
    return SITES.index(site) + 1


class SiteNode:
    """One hospital's LMA: what ``app.main`` serves in the ``lma`` role.

    It has the lookups the hospital routes use (``sites``, ``hospitals``,
    :meth:`hospital`), so those routes run unchanged, and :meth:`report`, which
    the app process reads instead of any record.

    Params:
    - `site`: the hospital this process runs.
    - `coordinator_url`: the app process, where models are loaded from and
      updates are sent to.
    - `database_url`: ``HEALTHCARE_DEMO_DATABASE_URL``, or empty for an
      in-memory store loaded from the committed extract. The hospital's own
      ``HEALTHCARE_DEMO_SITE_<SITE>_DATABASE_URL`` takes precedence, as in one
      process.
    - `max_entered`: most records the data-entry form may add.
    - `timeout`: seconds to wait for each call to the app process. See
      :func:`coordinator_timeout`.
    - `environ`: where per-site connection strings are read from.
    - `site_store`, `contribution_store`: stores to use instead of building
      them, for tests.
    - `client`: an ``httpx.Client`` for calls to the app process, for tests.
    """

    def __init__(
        self,
        site: Site,
        *,
        coordinator_url: str,
        database_url: str = "",
        max_entered: int = 300,
        timeout: float = 30.0,
        environ: Mapping[str, str] | None = None,
        site_store: SiteStore | None = None,
        contribution_store: CoordinatorStore | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.site = site
        self.sites: tuple[Site, ...] = (site,)
        self._coordinator_url = coordinator_url
        self._timeout = timeout
        self._client = client
        self._database_url = database_url.strip()
        self._environ = environ
        self._contribution_store = contribution_store
        self.registry = RemoteModelRegistry(coordinator_url, timeout=timeout, client=client)
        store_factory: Callable[[], SiteStore] = (
            (lambda: site_store) if site_store is not None else (lambda: build_site_store(site, database_url, environ))
        )
        self.hospitals: dict[str, Hospital] = {
            site.site_id: Hospital(
                site,
                store_factory=store_factory,
                registry=self.registry,  # type: ignore[arg-type]
                contributions=self._contributions,
                max_entered=max_entered,
                series_slot=_series_slot(site),
            )
        }

    def _contributions(self) -> CoordinatorStore:
        """Where the hospital records what it sent in each round.

        In Postgres, the coordinator schema reached with the hospital's own
        connection string, acting as the hospital, as in one process. In
        memory, a store of this process.
        """
        if self._contribution_store is None:
            dsn = site_database_url(self.site, self._database_url, self._environ)
            if dsn:
                self._contribution_store = PostgresCoordinatorStore(dsn, self.sites)
            else:
                self._contribution_store = MemoryCoordinatorStore()
        return self._contribution_store

    @property
    def site_id(self) -> str:
        return self.site.site_id

    def hospital(self, site_id: str) -> Hospital:
        """This process's hospital. Any other id raises ``UnknownSiteError``."""
        hospital = self.hospitals.get(site_id)
        if hospital is None:
            raise UnknownSiteError(site_id)
        return hospital

    def update_channel(self) -> HttpUpdateChannel:
        """Where a training job sends its bounded update: the app process."""
        return HttpUpdateChannel(self._coordinator_url, timeout=self._timeout, client=self._client)

    def report(self) -> dict[str, Any]:
        """What the hospital reports to the app: no record, only what one process would show it.

        ``{site_id, summary, registration, counts, store_kind, audit_summary}``.
        """
        hospital = self.hospital(self.site_id)
        return {
            "site_id": self.site_id,
            "summary": hospital.summary(),
            "registration": hospital.registration().model_dump(mode="json"),
            "counts": hospital.counts(),
            "store_kind": hospital.store.kind,
            "audit_summary": hospital.audit_summary(),
        }

    def close(self) -> None:
        """Release the hospital's database connections."""
        for hospital in self.hospitals.values():
            hospital.close()


def build_site_node(
    settings: Settings,
    *,
    client: httpx.Client | None = None,
    environ: Mapping[str, str] | None = None,
    site_store: SiteStore | None = None,
    contribution_store: CoordinatorStore | None = None,
) -> SiteNode:
    """The ``lma`` role's hospital, from ``HEALTHCARE_DEMO_SITE_ID`` and the other settings."""
    try:
        site = site_for(settings.site_id)
    except UnknownSiteError:
        known = ", ".join(site.site_id for site in SITES)
        raise ValueError(f"HEALTHCARE_DEMO_SITE_ID is {settings.site_id!r}; it must be one of {known}") from None
    return SiteNode(
        site,
        coordinator_url=settings.coordinator_url,
        database_url=settings.database_url,
        max_entered=settings.max_entered_per_site,
        timeout=coordinator_timeout(settings.federation_timeout_seconds),
        environ=environ,
        site_store=site_store,
        contribution_store=contribution_store,
        client=client,
    )


def build_app_consortium(
    settings: Settings,
    *,
    gma_client: httpx.Client | None = None,
    lma_clients: Mapping[str, httpx.Client] | None = None,
    coordinator_store: CoordinatorStore | None = None,
) -> Consortium:
    """The ``app`` role's consortium.

    Its hospitals are the LMAs at ``HEALTHCARE_DEMO_LMA_URLS``, in that order.
    The Dagents GMA at ``GMA_PUBLIC_URL`` runs the rounds and the release
    Guard. The model registry, the update store and the pilot reports stay in
    the app's coordinator store (Postgres when ``HEALTHCARE_DEMO_DATABASE_URL``
    is set, otherwise memory).

    Params:
    - `gma_client`, `lma_clients`: ``httpx.Client`` objects to send through,
      for tests. ``lma_clients`` is keyed by site id.
    - `coordinator_store`: a store to use instead of building one, for tests.
    """
    clients = dict(lma_clients or {})
    urls = settings.lma_url_map()
    if not urls:
        raise ValueError("HEALTHCARE_DEMO_LMA_URLS names no hospital")
    hospitals: dict[str, RemoteHospital] = {}
    # A hospital's chart colour is its place in the consortium, as in one process.
    slots = len(urls) <= MAX_CHART_SERIES
    for position, (site_id, url) in enumerate(urls.items(), start=1):
        try:
            site = site_for(site_id)
        except UnknownSiteError:
            known = ", ".join(site.site_id for site in SITES)
            raise ValueError(f"HEALTHCARE_DEMO_LMA_URLS names {site_id!r}; each site must be one of {known}") from None
        hospitals[site_id] = RemoteHospital(
            site,
            url,
            series_slot=position if slots else None,
            timeout=settings.upstream_timeout_seconds,
            federation_timeout=settings.federation_timeout_seconds,
            client=clients.get(site_id),
        )
    return Consortium(
        database_url=settings.database_url,
        secure_aggregation_threshold=settings.secure_aggregation_threshold,
        max_entered_per_site=settings.max_entered_per_site,
        coordinator_store=coordinator_store,
        hospitals=hospitals,
        round_controller=lambda runtime: GmaRoundClient(
            settings.gma_url,
            runtime,
            timeout=settings.federation_timeout_seconds,
            round_timeout=round_timeout(settings.federation_timeout_seconds, len(hospitals)),
            client=gma_client,
        ),
        release_guard=GmaReleaseGuard(
            settings.gma_url, STROKE_CLASSIFICATION, timeout=settings.federation_timeout_seconds, client=gma_client
        ),
    )
