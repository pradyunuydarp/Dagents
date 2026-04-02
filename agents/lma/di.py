"""Dependency container for the Local Monitoring Agent."""

from agents.lma.adapters.runner import InMemoryMonitoringRunner
from agents.lma.application.monitoring_service import MonitoringService
from agents.lma.infrastructure.messaging import InMemoryTelemetryPublisher
from agents.lma.infrastructure.state import InMemoryBundleRepository, InMemoryRunHistoryRepository


def build_monitoring_service() -> MonitoringService:
    return MonitoringService(
        runner=InMemoryMonitoringRunner(),
        publisher=InMemoryTelemetryPublisher(),
        bundles=InMemoryBundleRepository(),
        runs=InMemoryRunHistoryRepository(),
    )
