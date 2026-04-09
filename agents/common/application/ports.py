"""Application ports shared across Dagents services."""

from __future__ import annotations

from typing import Any, Callable, Protocol

from agents.common.domain.execution import ModelJob, PipelineRun
from agents.common.domain.sources import DatasetInput, RecordBatch, SourceMetadata, SourceSpec, SourceValidationResult


class ConnectionResolver(Protocol):
    def resolve(self, connection_id: str) -> dict[str, Any]:
        """Resolve one named connection."""


class SourceCatalog(Protocol):
    def save(self, source: SourceSpec) -> SourceSpec:
        """Persist one source definition."""

    def get(self, source_id: str) -> SourceSpec | None:
        """Load one source definition."""

    def list(self) -> list[SourceSpec]:
        """List source definitions."""


class SourceAdapter(Protocol):
    kind: str

    def validate(self, source: SourceSpec, resolved_connection: dict[str, Any]) -> SourceValidationResult:
        """Validate connector configuration."""

    def discover(self, source: SourceSpec, resolved_connection: dict[str, Any]) -> SourceMetadata:
        """Return metadata for one source."""

    def scan(
        self,
        source: SourceSpec,
        resolved_connection: dict[str, Any],
        checkpoint: dict[str, Any] | None = None,
    ) -> list[RecordBatch]:
        """Materialize normalized record batches."""


class SourceResolver(Protocol):
    def register(self, source: SourceSpec) -> SourceSpec:
        """Persist one source definition."""

    def get(self, source_id: str) -> SourceSpec | None:
        """Load one source definition."""

    def validate(self, source_id: str) -> SourceValidationResult:
        """Validate one stored source."""

    def materialize(self, dataset: DatasetInput) -> list[RecordBatch]:
        """Resolve dataset input into normalized record batches."""


class JobExecutor(Protocol):
    def submit_pipeline(self, run_id: str, fn: Callable[[], PipelineRun]) -> PipelineRun:
        """Submit one pipeline run."""

    def submit_model_job(self, job_id: str, fn: Callable[[], ModelJob]) -> ModelJob:
        """Submit one model job."""


class ArtifactStore(Protocol):
    def save_metadata(self, artifact_id: str, payload: dict[str, Any]) -> None:
        """Persist artifact metadata."""
