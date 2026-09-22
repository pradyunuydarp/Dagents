"""Dependency container for the Global Monitoring Agent."""

from agents.common.application.federation import (
    FederatedRoundController,
    InProcessFederationEngine,
)
from agents.common.application.governance_service import GovernanceService
from agents.common.infrastructure.sources import DefaultSourceResolver
from agents.gma.application.aggregation_service import AggregationService
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


def build_round_controller() -> FederatedRoundController:
    """Build the federated round controller.

    The default engine runs every site in this process, which makes the control
    plane demonstrable without provisioning a distributed runtime. A deployment
    swaps in an adapter for NVIDIA FLARE or another approved runtime; nothing
    above :class:`~agents.common.application.federation.FederationEngine` has to
    change when it does.
    """
    return FederatedRoundController(engine=InProcessFederationEngine())
