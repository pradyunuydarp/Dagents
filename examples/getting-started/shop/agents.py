"""Run each store's LMA and the coordinator's GMA, and reach them through their APIs.

In step 4 the application ran the round with framework classes in its own
code. Here the Dagents agents do that work:

- one LMA (local agent) per store holds the store's Guard, its audit log and
  its job log, reads the store's orders, and runs the store's part of a round;
- one GMA (global agent) enrols the stores, plans each round, sends each store
  its job over the LMA's API, and combines the results the stores return.

The agents run in this process, and every call goes through
``AgentNetwork.in_process``: the request passes through the agent's real
routes, request checks and token checks, without a socket. To run the agents
as separate processes, start each one with uvicorn and build the network with
``AgentNetwork.over_http`` instead. The calls stay the same.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Any

from dagents.common.extensions import ExtensionRegistry
from dagents.common.infrastructure.agent_client import GMA_AGENT, AgentNetwork, GmaClient, LmaClient, lma_agent
from dagents.gma.app_factory import GmaDeps
from dagents.gma.app_factory import create_app as create_gma
from dagents.gma.config import Settings as GmaSettings
from dagents.lma.app_factory import LmaDeps
from dagents.lma.app_factory import create_app as create_lma
from dagents.lma.config import Settings as LmaSettings
from dagents.lma.domain.site_config import SiteConfig

from shop.contract import CONDITION
from shop.coordinator import STUDY_ID
from shop.extension import install
from shop.store import SITE_IDS, OrderStore, load_store


class OrderStoreSource:
    """One store's orders, in the form an LMA reads them (a ``SiteRecordSource``)."""

    def __init__(self, store: OrderStore) -> None:
        self._store = store

    def records(self) -> list[dict[str, Any]]:
        return self._store.orders()

    def count(self) -> int:
        return self._store.count()

    def version(self) -> str:
        # Orders are only added, so the count changes whenever the orders do.
        # The LMA reads the orders again when the version changes.
        return str(self._store.count())

    def location(self) -> dict[str, str]:
        # The LMA shows this to its callers, so it names the store, never a host.
        return {"store": self._store.site_id}

    def close(self) -> None:
        pass


def _token() -> str:
    return secrets.token_urlsafe(24)


@dataclass
class Agents:
    """The agents of one study, and the tokens each caller uses.

    - ``admin_token``: the study operator's token for the GMA.
    - ``store_tokens``: each store's staff token for its own LMA.
    """

    network: AgentNetwork
    admin_token: str
    store_tokens: dict[str, str] = field(repr=False)

    def gma(self) -> GmaClient:
        """The study operator's client for the GMA."""
        return self.network.gma(self.admin_token)

    def lma(self, site_id: str) -> LmaClient:
        """A store's own client for its LMA."""
        return self.network.lma(site_id, self.store_tokens[site_id])

    def close(self) -> None:
        self.network.close()


def build_agents() -> Agents:
    """Build one LMA per store and one GMA, with a token for each caller.

    Each agent checks a bearer token on every route except its health check:

    | Caller | Calls | Token |
    |---|---|---|
    | The study operator | the GMA | the GMA's admin token |
    | The GMA | each store's LMA, to offer and run jobs | that LMA's coordinator token |
    | Each LMA | the GMA, for training updates and the released model | a GMA token bound to that store |
    | A store's staff | their own LMA | that LMA's site token |

    The tokens are new for each run and are never printed.
    """
    registry = ExtensionRegistry()
    install(registry)
    admin_token = _token()
    coordinator_tokens = {site_id: _token() for site_id in SITE_IDS}
    upload_tokens = {site_id: _token() for site_id in SITE_IDS}
    store_tokens = {site_id: _token() for site_id in SITE_IDS}

    network = AgentNetwork.in_process()
    try:
        gma = create_gma(
            GmaSettings(
                # The GMA sends each job to the store's LMA over its API.
                engine="http",
                admin_token=admin_token,
                lma_tokens=coordinator_tokens,
                site_tokens=upload_tokens,
                # The coordinator never reads orders, so its source routes are off.
                allow_source_reads=False,
            ),
            GmaDeps(registry=registry, network=network),
        )
        network.add_app(GMA_AGENT, gma)
        for site_id in SITE_IDS:
            lma = create_lma(
                LmaSettings(
                    site_token=store_tokens[site_id],
                    coordinator_token=coordinator_tokens[site_id],
                    gma_token=upload_tokens[site_id],
                ),
                LmaDeps(
                    registry=registry,
                    site=SiteConfig(
                        site_id=site_id,
                        condition_id=CONDITION.condition_id,
                        study_id=STUDY_ID,
                        capabilities=("evaluate",),
                    ),
                    source=OrderStoreSource(load_store(site_id)),
                    network=network,
                ),
            )
            network.add_app(lma_agent(site_id), lma)
    except Exception:
        network.close()
        raise
    return Agents(network=network, admin_token=admin_token, store_tokens=store_tokens)
