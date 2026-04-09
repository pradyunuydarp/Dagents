"""Application orchestration for the Local Monitoring Agent."""

from __future__ import annotations

import hashlib
import time

from agents.common.application.ports import SourceResolver
from agents.common.application.ml_orchestration import build_dataset_profile, execute_model_run
from agents.common.infrastructure.sources import DefaultSourceResolver
from agents.lma.adapters.runner import MonitoringRunner
from agents.lma.domain.models import (
    BundleRecord,
    CommandStatus,
    DatasetProfile,
    DatasetProfileRequest,
    DeployBundleCommand,
    ModelExecutionRequest,
    ModelExecutionResponse,
    ModelRunRecord,
    RunExecutionResponse,
    RunRecord,
    RunRequest,
    TelemetryEnvelope,
)
from agents.lma.infrastructure.messaging import TelemetryPublisher
from agents.lma.infrastructure.state import BundleRepository, ModelRunRepository, RunHistoryRepository


class MonitoringService:
    """Coordinate a local monitoring run and publish its telemetry."""

    def __init__(
        self,
        runner: MonitoringRunner,
        publisher: TelemetryPublisher,
        bundles: BundleRepository,
        runs: RunHistoryRepository,
        model_runs: ModelRunRepository,
        source_resolver: SourceResolver | None = None,
    ) -> None:
        self._runner = runner
        self._publisher = publisher
        self._bundles = bundles
        self._runs = runs
        self._model_runs = model_runs
        self._source_resolver = source_resolver or DefaultSourceResolver()

    def deploy_bundle(self, command: DeployBundleCommand) -> CommandStatus:
        config_digest = hashlib.sha256(
            f"{command.bundle_id}:{command.bundle_version}:{command.bundle_uri}".encode("utf-8")
        ).hexdigest()
        self._bundles.save(command, deployed_at=int(time.time()), config_digest=config_digest)
        return CommandStatus(
            accepted=True,
            message=f"Deployed bundle {command.bundle_id}:{command.bundle_version}",
        )

    def run(self, request: RunRequest) -> RunExecutionResponse:
        started_at = int(time.time())
        bundle = self._bundles.get(request.bundle_id, request.bundle_version)
        envelope = self._runner.execute(request, bundle=bundle)
        self._publisher.publish(envelope)
        completed_at = int(time.time())
        record = RunRecord(
            run_id=f"run-{request.correlation_id}",
            correlation_id=request.correlation_id,
            bundle_id=request.bundle_id,
            bundle_version=request.bundle_version,
            scope=request.scope,
            started_at=started_at,
            completed_at=completed_at,
            metrics_emitted=len(envelope.metrics),
            pointers=envelope.pointers,
        )
        self._runs.append(record)
        return RunExecutionResponse(run=record, telemetry=envelope)

    def list_bundles(self) -> list[BundleRecord]:
        return self._bundles.list()

    def list_runs(self, limit: int = 20) -> list[RunRecord]:
        return self._runs.list_recent(limit=limit)

    def profile_source_dataset(self, request: DatasetProfileRequest) -> DatasetProfile:
        source_request = request.model_copy(update={"scope_kind": "source"})
        return build_dataset_profile(source_request, source_resolver=self._source_resolver)

    def run_source_model(self, request: ModelExecutionRequest) -> ModelExecutionResponse:
        source_request = request.model_copy(update={"scope_kind": "source"})
        response = execute_model_run(source_request, agent_role="LMA", source_resolver=self._source_resolver)
        self._model_runs.append(response.run)
        return response

    def register_source(self, source):
        return self._source_resolver.register(source)

    def get_source(self, source_id: str):
        return self._source_resolver.get(source_id)

    def list_sources(self):
        return self._source_resolver.list()

    def validate_source(self, source_id: str):
        return self._source_resolver.validate(source_id)

    def list_model_runs(self, limit: int = 20) -> list[ModelRunRecord]:
        return self._model_runs.list_recent(limit=limit)

    def recent_telemetry(self, limit: int = 20) -> list[TelemetryEnvelope]:
        return self._publisher.list_recent(limit=limit)

    def health_payload(self) -> dict[str, object]:
        latest_bundle = self._bundles.latest()
        return {
            "status": "ok",
            "agent_type": "LMA",
            "deployed_bundles": len(self._bundles.list()),
            "runs": len(self._runs.list_recent(limit=10_000)),
            "model_runs": len(self._model_runs.list_recent(limit=10_000)),
            "active_bundle": (
                f"{latest_bundle.bundle_id}:{latest_bundle.bundle_version}" if latest_bundle else None
            ),
        }
