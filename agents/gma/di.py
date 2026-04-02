"""Dependency container for the Global Monitoring Agent."""

from agents.gma.application.aggregation_service import AggregationService
from agents.gma.infrastructure.persistence import (
    InMemoryAgentRegistryRepository,
    InMemoryControlPlaneRepository,
    InMemoryTelemetryRepository,
)


def build_aggregation_service() -> AggregationService:
    return AggregationService(
        registry=InMemoryAgentRegistryRepository(),
        telemetry_repository=InMemoryTelemetryRepository(),
        control_plane=InMemoryControlPlaneRepository(),
    )
