"""Messaging abstractions for the Local Monitoring Agent."""

from __future__ import annotations

from typing import Protocol

from agents.lma.domain.models import TelemetryEnvelope


class TelemetryPublisher(Protocol):
    def publish(self, envelope: TelemetryEnvelope) -> None:
        """Publish a telemetry envelope."""

    def list_recent(self, limit: int = 20) -> list[TelemetryEnvelope]:
        """Return recently published envelopes."""


class InMemoryTelemetryPublisher:
    """No-op publisher for local development and early integration."""

    def __init__(self) -> None:
        self.events: list[TelemetryEnvelope] = []

    def publish(self, envelope: TelemetryEnvelope) -> None:
        self.events.append(envelope)

    def list_recent(self, limit: int = 20) -> list[TelemetryEnvelope]:
        return list(reversed(self.events[-limit:]))
