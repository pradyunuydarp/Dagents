"""Builders for the Global Monitoring Agent's services.

:func:`dagents.gma.app_factory.create_app` calls these with what the GMA's
settings and dependencies give it; they hold no module-level state.
"""

from __future__ import annotations

from dagents.common.application.ethical_guard import AuditSink
from dagents.common.application.federation import (
    FederatedRoundController,
    FederationEngine,
    InProcessFederationEngine,
)
from dagents.common.application.governance_service import GovernanceService
from dagents.common.extensions import ExtensionRegistry
from dagents.common.infrastructure.sources import DefaultSourceResolver
from dagents.gma.application.aggregation_service import AggregationService
from dagents.gma.infrastructure.persistence import (
    InMemoryAgentRegistryRepository,
    InMemoryControlPlaneRepository,
    InMemoryModelRunRepository,
    InMemoryTelemetryRepository,
)


def build_aggregation_service() -> AggregationService:
    """The LMA control plane: registrations, telemetry, deployments and aggregate model runs, in memory."""
    return AggregationService(
        registry=InMemoryAgentRegistryRepository(),
        telemetry_repository=InMemoryTelemetryRepository(),
        control_plane=InMemoryControlPlaneRepository(),
        model_runs=InMemoryModelRunRepository(),
        source_resolver=DefaultSourceResolver(),
    )


def build_governance_service(
    registry: ExtensionRegistry | None = None,
    audit: AuditSink | None = None,
    strict_classifications: bool = False,
) -> GovernanceService:
    """Build the GMA's Ethical Guard, which stands at the release boundary.

    Params:
    - `registry`: where extension classifications are looked up; the default
      registry when ``None``.
    - `audit`: the audit sink; in memory when ``None``.
    - `strict_classifications`: refuse a request whose classification is not
      the registered one (a GMA with condition packs runs strict).
    """
    return GovernanceService(audit=audit, registry=registry, strict_classifications=strict_classifications)


def build_round_controller(engine: FederationEngine | None = None) -> FederatedRoundController:
    """Build the federated round controller.

    The default engine is the in-process simulator, which runs every site in
    this process. ``GMA_ENGINE=http`` gives it the GMA's HTTP engine instead
    (:class:`dagents.gma.adapters.http_federation_engine.HttpFederationEngine`),
    and an adapter for another runtime such as NVIDIA FLARE fits the same
    :class:`~dagents.common.application.federation.FederationEngine`
    interface; nothing above it changes.
    """
    return FederatedRoundController(engine=engine or InProcessFederationEngine())
