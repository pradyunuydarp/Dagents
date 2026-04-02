"""Runner contracts and in-memory stub implementation for the LMA."""

from __future__ import annotations

import time
from typing import Protocol

from agents.lma.domain.models import AgentIdentity, BundleRecord, RunRequest, TelemetryEnvelope, TelemetryPoint


class MonitoringRunner(Protocol):
    def execute(self, request: RunRequest, bundle: BundleRecord | None = None) -> TelemetryEnvelope:
        """Execute a local monitoring run."""


class InMemoryMonitoringRunner:
    """Developer-friendly stub runner for early LMA integration."""

    def execute(self, request: RunRequest, bundle: BundleRecord | None = None) -> TelemetryEnvelope:
        observed_at = int(time.time())
        scope_size = max(1, len(request.scope))
        metadata_size = len(request.metadata)
        return TelemetryEnvelope(
            agent=AgentIdentity(
                agent_id="lma-dev-1",
                workspace_id=request.metadata.get("workspace_id", "dagents-dev"),
                name="dagents-local-monitoring-agent",
            ),
            metrics=[
                TelemetryPoint(
                    key="local_checks_total",
                    value=float(scope_size),
                    labels={"bundle_id": request.bundle_id, "bundle_version": request.bundle_version},
                    observed_at=observed_at,
                ),
                TelemetryPoint(
                    key="metadata_fields_total",
                    value=float(metadata_size),
                    labels={"correlation_id": request.correlation_id},
                    observed_at=observed_at,
                )
            ],
            pointers={
                "scope": str(request.scope),
                "bundle_uri": bundle.bundle_uri if bundle else "",
                "correlation_id": request.correlation_id,
            },
        )
