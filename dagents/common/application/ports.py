"""Application ports shared across Dagents services."""

from __future__ import annotations

from typing import Any, Callable, Protocol

from dagents.common.domain.execution import ModelJob, PipelineRun
from dagents.common.domain.site import JobLogEntry
from dagents.common.domain.sources import DatasetInput, RecordBatch, SourceMetadata, SourceSpec, SourceValidationResult


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


class SiteRecordSource(Protocol):
    """Where an LMA started for a site reads that site's records.

    A source reads the site's own store, acting as the site's role. An
    extension contributes a factory for one through ``site_sources()``, and an
    application can hand an LMA its own source directly.
    """

    def records(self) -> list[dict[str, Any]]:
        """Return copies of every record, in a stable order."""

    def count(self) -> int:
        """Return how many records the source holds."""

    def version(self) -> str:
        """Return a value that changes whenever the records change.

        The LMA compares it with the version it last read and reads the
        records again when it differs. It must be cheap: the LMA asks for it
        often.
        """

    def location(self) -> dict[str, str]:
        """Return names that place the records, such as a schema.

        Never a host, a password or a connection string: an LMA returns this
        to callers.
        """

    def close(self) -> None:
        """Release any connection the source holds."""


class JobLog(Protocol):
    """Where an LMA records each federated job it was offered and what it returned."""

    def append(self, entry: JobLogEntry) -> JobLogEntry:
        """Store one entry and return it with ``submitted_at`` set."""

    def recent(self, limit: int = 20) -> list[JobLogEntry]:
        """Return the newest entries first, at most ``limit`` of them."""
