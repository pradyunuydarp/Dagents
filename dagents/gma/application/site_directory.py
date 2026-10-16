"""The GMA's view of the sites whose LMAs it can call.

The configured sites are the LMAs in the GMA's agent network: the addresses in
``GMA_SITE_ENDPOINTS``, the addresses of joinable sites the study operator
sent (see ``site_addresses.py``), or the LMA apps an application added to an
in-process network. Enrolment never adds an address, so a site cannot point
the GMA at another host.

- :meth:`SiteDirectory.refresh` asks each configured LMA for its registration
  (``GET /api/v1/federation/registration``), in parallel, and enrols what it
  answers. An LMA that does not answer, or answers for another site, is left
  as it was and logged.
- :meth:`SiteDirectory.summaries` asks each LMA for its summary and, when
  asked, its audit chain state. It keeps the answer for
  ``GMA_SUMMARY_TTL_SECONDS``, because one page can ask several times. The
  source kind comes from ``GET /api/v1/site`` once per site.

The GMA sees what each site reports: counts, the label rate the site's planner
permits and the audit chain's state and length. It never reads a site's rows.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import logging
import threading
import time
from typing import Callable, Mapping, TypeVar

from dagents.common.application.federation import FederatedRoundController
from dagents.common.domain.federation import SiteRegistration
from dagents.common.domain.site import SiteDirectoryEntry
from dagents.common.infrastructure.agent_client import (
    GMA_AGENT,
    AgentError,
    AgentNetwork,
    AgentUnavailableError,
    LmaClient,
    lma_agent,
    without_addresses,
)

logger = logging.getLogger(__name__)

R = TypeVar("R")

_LMA_PREFIX = lma_agent("site")[: -len("site")]


class SiteDirectory:
    """Refresh enrolments from the LMAs and collect what they report.

    Params:
    - `network`: the GMA's agent network, or ``None`` when it calls no LMA.
    - `rounds`: where enrolments are stored.
    - `lma_tokens`: the coordinator token to send to each site's LMA.
    - `concurrency`: how many LMAs are called at once.
    - `ttl_seconds`: how long summaries are kept. ``0`` keeps nothing.
    - `timeout_s`: how long each summary, audit state and site description
      call to an LMA may take. An LMA that takes longer is reported as not
      reachable, so one frozen LMA cannot hold the caller for the network's
      longer timeout. ``None`` uses the network's timeout.
    """

    def __init__(
        self,
        network: AgentNetwork | None,
        rounds: FederatedRoundController,
        *,
        lma_tokens: Mapping[str, str] | None = None,
        concurrency: int = 3,
        ttl_seconds: float = 2.0,
        timeout_s: float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._network = network
        self._rounds = rounds
        self._tokens = dict(lma_tokens or {})
        self._concurrency = max(1, int(concurrency))
        self._ttl = float(ttl_seconds)
        self._timeout_s = timeout_s
        self._clock = clock
        self._cache: dict[bool, tuple[float, list[SiteDirectoryEntry]]] = {}
        self._source_kinds: dict[str, str] = {}
        self._lock = threading.Lock()

    def site_ids(self) -> list[str]:
        """The sites whose LMA the GMA can call, sorted."""
        if self._network is None:
            return []
        return sorted(
            name[len(_LMA_PREFIX) :]
            for name in self._network.agents
            if name != GMA_AGENT and name.startswith(_LMA_PREFIX)
        )

    def _client(self, site_id: str) -> LmaClient:
        assert self._network is not None
        return self._network.lma(site_id, self._tokens.get(site_id))

    def _each(self, call: Callable[[str], R]) -> list[R]:
        sites = self.site_ids()
        if not sites:
            return []
        with ThreadPoolExecutor(
            max_workers=min(self._concurrency, len(sites)), thread_name_prefix="gma-directory"
        ) as executor:
            return list(executor.map(call, sites))

    def forget(self, site_id: str) -> None:
        """Drop the cached summaries and the cached source kind of ``site_id``, after its address changed."""
        with self._lock:
            self._cache.clear()
            self._source_kinds.pop(site_id, None)

    # --- Enrolment --------------------------------------------------------------------

    def refresh(self) -> list[SiteRegistration]:
        """Enrol each configured LMA from its own registration. Returns the enrolments made, sorted by site."""

        def pull(site_id: str) -> SiteRegistration | None:
            try:
                registration = self._client(site_id).registration()
            except AgentError as error:
                logger.warning("the GMA could not refresh site %s: %s", site_id, without_addresses(str(error)))
                return None
            if registration.site_id != site_id:
                logger.warning("the LMA of site %s answered with the registration of another site", site_id)
                return None
            return registration

        enrolled = [self._rounds.register_site(registration) for registration in self._each(pull) if registration]
        with self._lock:
            self._cache.clear()
        return sorted(enrolled, key=lambda item: item.site_id)

    # --- Summaries ----------------------------------------------------------------------

    def summaries(self, include_audit: bool = False) -> list[SiteDirectoryEntry]:
        """What each configured site reports, sorted by site. Kept for the configured time."""
        now = self._clock()
        with self._lock:
            cached = self._cache.get(include_audit)
            if cached is not None and cached[0] > now:
                return [entry.model_copy(deep=True) for entry in cached[1]]
        entries = self._each(lambda site_id: self._entry(site_id, include_audit))
        if self._ttl > 0:
            with self._lock:
                self._cache[include_audit] = (self._clock() + self._ttl, [entry.model_copy(deep=True) for entry in entries])
        return entries

    def _entry(self, site_id: str, include_audit: bool) -> SiteDirectoryEntry:
        client = self._client(site_id)
        try:
            summary = client.site_summary(timeout_s=self._timeout_s)
        except AgentError as error:
            # An LMA that answered with an error still answered.
            return SiteDirectoryEntry(site_id=site_id, reachable=not isinstance(error, AgentUnavailableError))
        audit = None
        if include_audit:
            try:
                audit = client.audit_summary(timeout_s=self._timeout_s)
            except AgentError:
                audit = None
        return SiteDirectoryEntry(
            site_id=site_id,
            reachable=True,
            source_kind=self._source_kind(site_id, client),
            summary=summary,
            audit=audit,
        )

    def _source_kind(self, site_id: str, client: LmaClient) -> str | None:
        with self._lock:
            known = self._source_kinds.get(site_id)
        if known is not None:
            return known
        try:
            kind = client.site_info(timeout_s=self._timeout_s).source.kind
        except AgentError:
            return None
        with self._lock:
            self._source_kinds[site_id] = kind
        return kind


__all__ = ["SiteDirectory"]
