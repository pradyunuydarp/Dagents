"""Adapter placeholder for future broker-backed telemetry ingestion."""

from typing import Protocol

from agents.gma.domain.models import TelemetryEnvelope
from agents.gma.infrastructure.persistence import TelemetryRepository


class TelemetrySink(Protocol):
    def accept(self, envelope: TelemetryEnvelope) -> None:
        """Accept telemetry from an LMA or broker consumer."""


class RepositoryBackedTelemetrySink:
    """Minimal sink that forwards telemetry to the configured repository."""

    def __init__(self, repository: TelemetryRepository) -> None:
        self._repository = repository

    def accept(self, envelope: TelemetryEnvelope) -> None:
        self._repository.append(envelope)
