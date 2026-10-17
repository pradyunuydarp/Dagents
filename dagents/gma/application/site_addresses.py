"""The addresses of the LMAs a GMA calls, and the sites that may join while it runs.

At start the GMA's agent network holds the addresses in ``GMA_SITE_ENDPOINTS``
(the configured sites). ``GMA_JOINABLE_SITE_ENDPOINTS`` names the sites that
may join later, each with the one address the GMA accepts for it. The site
directory and the HTTP engine both read the agent network, so adding a joined
site's address to the network is all the GMA needs to refresh its enrolment,
collect its summary and send it a round's job.

:meth:`SiteAddressBook.attach` applies these rules, in this order:

1. A configured site at its own address is accepted, as ``configured``.
2. A configured site at another address is refused (409 ``configured_elsewhere``).
3. A site that is not joinable is refused (403 ``not_joinable``).
4. An address that differs from the joinable entry is refused (403 ``address_differs``).
5. A joinable site that joined already is accepted again, with no second client.

A trailing ``/`` is ignored on both sides, and an address that is not a base
address is refused first (422). Enrolment never adds an address, and nothing
but these rules does, so a caller cannot point the GMA at another host.

Joined addresses live in memory: a GMA that restarts forgets them, and the
study operator sends them again. No answer, error or log line holds an address.
"""

from __future__ import annotations

import threading
from typing import Callable, Mapping

from dagents.common.domain.federation import SiteAddressList, SiteAddressState
from dagents.common.infrastructure.agent_client import AgentNetwork, lma_agent, normalize_base_url
from dagents.gma.domain.errors import (
    InvalidSiteAddress,
    SiteAddressDiffers,
    SiteConfigured,
    SiteConfiguredElsewhere,
    SiteNotJoinable,
)


class SiteAddressBook:
    """Accept, remove and list the addresses of the LMAs a GMA calls.

    Params:
    - `network`: the GMA's agent network, over HTTP. ``None`` for a GMA that
      calls no LMA over HTTP; it then lists nothing and refuses every site.
    - `configured`: ``GMA_SITE_ENDPOINTS``, site to address.
    - `joinable`: ``GMA_JOINABLE_SITE_ENDPOINTS``, site to its one accepted address.
    - `on_change`: called with a site id after its address is added or
      removed, so the site directory can drop what it cached for the site.
    """

    def __init__(
        self,
        network: AgentNetwork | None,
        *,
        configured: Mapping[str, str],
        joinable: Mapping[str, str],
        on_change: Callable[[str], None] | None = None,
    ) -> None:
        self._network = network
        self._configured = {site_id: normalize_base_url(lma_agent(site_id), url) for site_id, url in configured.items()}
        self._joinable = {site_id: normalize_base_url(lma_agent(site_id), url) for site_id, url in joinable.items()}
        self._on_change = on_change
        self._joined: set[str] = set()
        self._lock = threading.Lock()

    def attach(self, site_id: str, url: str) -> SiteAddressState:
        """Accept ``url`` for ``site_id`` by the rules in the module docstring, or raise a ``SiteAddressRefused``."""
        try:
            checked = normalize_base_url(lma_agent(site_id), url)
        except ValueError:
            raise InvalidSiteAddress(
                site_id, "the address must be an http or https base address with no user, password, query or fragment"
            ) from None
        if site_id in self._configured:
            if self._configured[site_id] == checked:
                return SiteAddressState(site_id=site_id, source="configured")
            raise SiteConfiguredElsewhere(site_id, f"site {site_id} has another address in GMA_SITE_ENDPOINTS")
        if self._network is None or site_id not in self._joinable:
            raise SiteNotJoinable(site_id, f"site {site_id} is not one this GMA may add while it runs")
        if self._joinable[site_id] != checked:
            raise SiteAddressDiffers(
                site_id, f"the address differs from the one GMA_JOINABLE_SITE_ENDPOINTS lists for site {site_id}"
            )
        with self._lock:
            self._network.add_address(lma_agent(site_id), checked)
            self._joined.add(site_id)
        self._changed(site_id)
        return SiteAddressState(site_id=site_id, source="joined")

    def detach(self, site_id: str) -> None:
        """Remove a joined site's address. A joinable site that has not joined is left as it is.

        Raises :class:`SiteConfigured` for a configured site and
        :class:`SiteNotJoinable` for any other site that is not joinable.
        """
        if site_id in self._configured:
            raise SiteConfigured(site_id, f"site {site_id} is in GMA_SITE_ENDPOINTS; its address cannot be removed")
        if self._network is None or site_id not in self._joinable:
            raise SiteNotJoinable(site_id, f"site {site_id} is not one this GMA may add while it runs")
        with self._lock:
            if site_id not in self._joined:
                return
            self._network.remove_address(lma_agent(site_id))
            self._joined.discard(site_id)
        self._changed(site_id)

    def listing(self) -> SiteAddressList:
        """The joinable sites, and every site whose address the GMA holds, without addresses."""
        if self._network is None:
            return SiteAddressList(joinable=[], sites=[])
        with self._lock:
            joined = set(self._joined)
        sites = [SiteAddressState(site_id=site_id, source="configured") for site_id in self._configured]
        sites += [SiteAddressState(site_id=site_id, source="joined") for site_id in joined]
        return SiteAddressList(
            joinable=sorted(self._joinable), sites=sorted(sites, key=lambda state: state.site_id)
        )

    def _changed(self, site_id: str) -> None:
        if self._on_change is not None:
            self._on_change(site_id)


__all__ = ["SiteAddressBook"]
