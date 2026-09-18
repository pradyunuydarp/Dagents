"""Dependency container for the Local Monitoring Agent."""

from agents.common.application.governance_service import GovernanceService
from agents.common.infrastructure.sources import DefaultSourceResolver
from agents.lma.adapters.runner import InMemoryMonitoringRunner
from agents.lma.application.monitoring_service import MonitoringService
from agents.lma.infrastructure.messaging import InMemoryTelemetryPublisher
from agents.lma.infrastructure.state import (
    InMemoryBundleRepository,
    InMemoryModelRunRepository,
    InMemoryRunHistoryRepository,
)


def build_monitoring_service() -> MonitoringService:
    return MonitoringService(
        runner=InMemoryMonitoringRunner(),
        publisher=InMemoryTelemetryPublisher(),
        bundles=InMemoryBundleRepository(),
        runs=InMemoryRunHistoryRepository(),
        model_runs=InMemoryModelRunRepository(),
        source_resolver=DefaultSourceResolver(),
    )


def build_governance_service() -> GovernanceService:
    """Build the LMA's Ethical Guard and its audit log.

    The LMA is where three of the four guard boundaries stand: before local data
    is read, before training touches it, and before anything leaves the site.
    """
    return GovernanceService()
