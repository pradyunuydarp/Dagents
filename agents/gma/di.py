"""Dependency container for the Global Monitoring Agent."""

from agents.common.application.federation import (
    FederatedRoundController,
    FederationEngine,
    InProcessFederationEngine,
)
from agents.common.application.governance_service import GovernanceService
from agents.common.infrastructure.federation_http import HttpFederationEngine, parse_site_urls
from agents.common.infrastructure.sources import DefaultSourceResolver
from agents.gma.application.aggregation_service import AggregationService
from agents.gma.config import Settings, settings
from agents.gma.infrastructure.persistence import (
    InMemoryAgentRegistryRepository,
    InMemoryControlPlaneRepository,
    InMemoryModelRunRepository,
    InMemoryTelemetryRepository,
)


def build_aggregation_service() -> AggregationService:
    return AggregationService(
        registry=InMemoryAgentRegistryRepository(),
        telemetry_repository=InMemoryTelemetryRepository(),
        control_plane=InMemoryControlPlaneRepository(),
        model_runs=InMemoryModelRunRepository(),
        source_resolver=DefaultSourceResolver(),
    )


def build_governance_service() -> GovernanceService:
    """Build the GMA's Ethical Guard, which stands at the release boundary."""
    return GovernanceService()


def build_federation_engine(config: Settings = settings) -> FederationEngine:
    """Build the engine that runs the sites' part of a round.

    With ``GMA_FEDERATION_SITES`` set, each listed site is an LMA in its own
    process, reached over HTTP with ``GMA_FEDERATION_TIMEOUT_SECONDS`` per call.
    Without it, the engine runs in this process with no site attached, and
    sites send their own results.
    """
    sites = parse_site_urls(config.federation_sites)
    if sites:
        return HttpFederationEngine(sites, timeout=config.federation_timeout_seconds)
    return InProcessFederationEngine()


def build_round_controller(config: Settings = settings) -> FederatedRoundController:
    """Build the federated round controller.

    Both engines are simulators, which make the control plane demonstrable
    without provisioning a distributed runtime. A deployment swaps in an
    adapter for NVIDIA FLARE or another approved runtime; nothing above
    :class:`~agents.common.application.federation.FederationEngine` has to
    change when it does.
    """
    return FederatedRoundController(engine=build_federation_engine(config))
