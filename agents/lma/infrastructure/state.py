"""In-memory state repositories for the Local Monitoring Agent."""

from __future__ import annotations

from typing import Protocol

from agents.lma.domain.models import BundleRecord, DeployBundleCommand, ModelRunRecord, RunRecord


class BundleRepository(Protocol):
    def save(self, command: DeployBundleCommand, *, deployed_at: int, config_digest: str) -> BundleRecord:
        """Persist a deployed bundle."""

    def get(self, bundle_id: str, bundle_version: str) -> BundleRecord | None:
        """Load one deployed bundle."""

    def list(self) -> list[BundleRecord]:
        """List deployed bundles in reverse chronological order."""

    def latest(self) -> BundleRecord | None:
        """Return the most recently deployed bundle."""


class RunHistoryRepository(Protocol):
    def append(self, record: RunRecord) -> None:
        """Persist a completed run."""

    def list_recent(self, limit: int = 20) -> list[RunRecord]:
        """Return the most recent runs."""


class ModelRunRepository(Protocol):
    def append(self, record: ModelRunRecord) -> None:
        """Persist a completed model run."""

    def list_recent(self, limit: int = 20) -> list[ModelRunRecord]:
        """Return the most recent model runs."""


class InMemoryBundleRepository:
    """Stores bundles keyed by id and version."""

    def __init__(self) -> None:
        self._bundles: dict[tuple[str, str], BundleRecord] = {}
        self._ordered_keys: list[tuple[str, str]] = []

    def save(self, command: DeployBundleCommand, *, deployed_at: int, config_digest: str) -> BundleRecord:
        key = (command.bundle_id, command.bundle_version)
        record = BundleRecord(
            bundle_id=command.bundle_id,
            bundle_version=command.bundle_version,
            bundle_uri=command.bundle_uri,
            signature=command.signature,
            deployed_at=deployed_at,
            config_digest=config_digest,
        )
        self._bundles[key] = record
        if key in self._ordered_keys:
            self._ordered_keys.remove(key)
        self._ordered_keys.insert(0, key)
        return record

    def get(self, bundle_id: str, bundle_version: str) -> BundleRecord | None:
        return self._bundles.get((bundle_id, bundle_version))

    def list(self) -> list[BundleRecord]:
        return [self._bundles[key] for key in self._ordered_keys]

    def latest(self) -> BundleRecord | None:
        if not self._ordered_keys:
            return None
        return self._bundles[self._ordered_keys[0]]


class InMemoryRunHistoryRepository:
    """Stores recent run outcomes for local inspection."""

    def __init__(self) -> None:
        self._runs: list[RunRecord] = []

    def append(self, record: RunRecord) -> None:
        self._runs.append(record)

    def list_recent(self, limit: int = 20) -> list[RunRecord]:
        return list(reversed(self._runs[-limit:]))


class InMemoryModelRunRepository:
    """Stores recent model runs for local source execution."""

    def __init__(self) -> None:
        self._runs: list[ModelRunRecord] = []

    def append(self, record: ModelRunRecord) -> None:
        self._runs.append(record)

    def list_recent(self, limit: int = 20) -> list[ModelRunRecord]:
        return list(reversed(self._runs[-limit:]))
