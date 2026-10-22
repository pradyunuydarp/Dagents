"""Run each store's LMA and the coordinator's GMA, and reach them through their APIs.

In step 4 the application ran the round with framework classes in its own
code. Here the Dagents agents do that work:

- one LMA (local agent) per store holds the store's Guard, its audit log and
  its job log, reads the store's orders, and runs the store's part of a round;
- one GMA (global agent) enrols the stores, plans each round, sends each store
  its job over the LMA's API, and combines the results the stores return.

:func:`build_agents` runs the agents in this process, and every call goes
through ``AgentNetwork.in_process``: the request passes through the agent's
real routes, request checks and token checks, without a socket.
:func:`connect_agents` reaches agents that run as separate processes, started
with uvicorn from their settings, through ``AgentNetwork.over_http``. The calls
stay the same. :func:`open_agents` chooses between the two.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from typing import Mapping

from dagents.common.extensions import ExtensionRegistry
from dagents.common.infrastructure.agent_auth import parse_site_map
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
from shop.site_source import OrderStoreSource
from shop.store import SITE_IDS, load_store


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


def connect_agents(environ: Mapping[str, str]) -> Agents:
    """Reach a GMA and one LMA per store that run as separate processes.

    The addresses and the caller's tokens come from ``environ``. Each token
    is the one the agent was started with:

    | Variable | Holds | The agent's setting |
    |---|---|---|
    | `SHOP_GMA_URL` | The GMA's address | |
    | `SHOP_LMA_URLS` | Each store's LMA: `store-north=URL,store-south=URL` | The GMA's `GMA_SITE_ENDPOINTS` |
    | `SHOP_ADMIN_TOKEN` | The study operator's token | The GMA's `GMA_ADMIN_TOKEN` |
    | `SHOP_STORE_TOKENS` | Each store's staff token: `store-north=TOKEN,...` | Each LMA's `LMA_SITE_TOKEN` |
    """
    lma_urls = parse_site_map(environ.get("SHOP_LMA_URLS"), setting="SHOP_LMA_URLS")
    store_tokens = parse_site_map(environ.get("SHOP_STORE_TOKENS"), setting="SHOP_STORE_TOKENS")
    for setting, values in (("SHOP_LMA_URLS", lma_urls), ("SHOP_STORE_TOKENS", store_tokens)):
        if sorted(values) != sorted(SITE_IDS):
            raise ValueError(f"{setting} must name each store once: {', '.join(SITE_IDS)}")
    admin_token = environ.get("SHOP_ADMIN_TOKEN", "").strip()
    if not admin_token:
        raise ValueError("SHOP_ADMIN_TOKEN must hold the GMA's admin token")
    network = AgentNetwork.over_http(
        {GMA_AGENT: environ["SHOP_GMA_URL"], **{lma_agent(site_id): lma_urls[site_id] for site_id in SITE_IDS}}
    )
    return Agents(network=network, admin_token=admin_token, store_tokens=store_tokens)


def open_agents(environ: Mapping[str, str] | None = None) -> Agents:
    """The agents step 5 calls: separate processes when ``SHOP_GMA_URL`` is set, otherwise built here."""
    environ = os.environ if environ is None else environ
    if environ.get("SHOP_GMA_URL"):
        return connect_agents(environ)
    return build_agents()
