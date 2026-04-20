"""Shared source adapters and registries."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

try:  # pragma: no cover - exercised indirectly by integration tests when installed
    import psycopg
    from psycopg.rows import dict_row
except ModuleNotFoundError:  # pragma: no cover - dependency is optional in some local environments
    psycopg = None
    dict_row = None

from agents.common.application.ports import ConnectionResolver, SourceAdapter, SourceCatalog, SourceResolver
from agents.common.domain.sources import (
    ConnectionRef,
    DatasetInput,
    InlineSourceSpec,
    MongoSourceSpec,
    ObjectStorageSourceSpec,
    PostgresSourceSpec,
    RecordBatch,
    RecordBatchStats,
    RecordSchemaField,
    SourceMetadata,
    SourceSpec,
    SourceValidationResult,
)


def infer_schema(records: list[dict[str, Any]]) -> list[RecordSchemaField]:
    """Infer a minimal schema from the first record in a normalized record set.

    Params:
    - `records`: materialized records already normalized into `dict[str, Any]`.

    What it does:
    - Uses the first row as a representative sample.
    - Converts Python runtime value types into Dagents schema field entries.

    Returns:
    - A list of `RecordSchemaField` objects.
    - An empty list when no records are available.
    """
    if not records:
        return []
    fields: list[RecordSchemaField] = []
    sample = records[0]
    # The execution layer only needs a lightweight schema hint, so sampling the
    # first row is enough for the current record-oriented batch model.
    for key, value in sample.items():
        fields.append(RecordSchemaField(name=key, dtype=type(value).__name__))
    return fields


class InMemoryConnectionResolver:
    """Resolve named connections from an in-memory catalog."""

    def __init__(self, connections: dict[str, dict[str, Any]] | None = None) -> None:
        self._connections = connections or {}

    def register(self, connection_id: str, payload: dict[str, Any]) -> None:
        """Store one resolved connection payload under a connection id.

        Params:
        - `connection_id`: stable logical name referenced by `ConnectionRef`.
        - `payload`: driver-specific connection arguments or mocked connector data.

        What it does:
        - Saves the connection in memory for later lookup.

        Returns:
        - `None`.
        """
        self._connections[connection_id] = payload

    def resolve(self, connection_id: str) -> dict[str, Any]:
        """Return a copy of the stored connection payload for one id.

        Params:
        - `connection_id`: name of the registered connection.

        What it does:
        - Looks up the connection and returns a shallow copy.
        - Returns an empty dict when the connection does not exist.

        Returns:
        - `dict[str, Any]` connection details.
        """
        return dict(self._connections.get(connection_id, {}))


class InMemorySourceCatalog:
    """Simple in-memory source registry."""

    def __init__(self) -> None:
        self._sources: dict[str, SourceSpec] = {}

    def save(self, source: SourceSpec) -> SourceSpec:
        """Persist one source definition in memory and return it unchanged."""
        self._sources[source.source_id] = source
        return source

    def get(self, source_id: str) -> SourceSpec | None:
        """Load one source definition by id, if it exists."""
        return self._sources.get(source_id)

    def list(self) -> list[SourceSpec]:
        """List sources in a stable source-id order for deterministic tests."""
        return [self._sources[key] for key in sorted(self._sources)]


class InlineSourceAdapter:
    kind = "inline"

    def validate(self, source: SourceSpec, resolved_connection: dict[str, Any]) -> SourceValidationResult:
        """Validate an inline source definition.

        Params:
        - `source`: the registered `InlineSourceSpec`.
        - `resolved_connection`: ignored for inline data because the payload lives
          directly inside the source definition.

        What it does:
        - Confirms the source is the expected inline type.
        - Emits a warning instead of an error when no records are present so the
          source can still be created and filled later.

        Returns:
        - `SourceValidationResult`.
        """
        del resolved_connection
        inline = _ensure_source_type(source, InlineSourceSpec)
        return SourceValidationResult(valid=True, warnings=[] if inline.selection.records else ["No inline records supplied"])

    def discover(self, source: SourceSpec, resolved_connection: dict[str, Any]) -> SourceMetadata:
        """Describe an inline dataset without materializing any external connector."""
        del resolved_connection
        inline = _ensure_source_type(source, InlineSourceSpec)
        records = inline.selection.records
        return SourceMetadata(
            source_id=inline.source_id,
            kind=inline.kind,
            schema=infer_schema(records),
            estimated_records=len(records),
        )

    def scan(
        self,
        source: SourceSpec,
        resolved_connection: dict[str, Any],
        checkpoint: dict[str, Any] | None = None,
    ) -> list[RecordBatch]:
        """Convert inline records into normalized `RecordBatch` chunks.

        Params:
        - `source`: inline source definition.
        - `resolved_connection`: unused for inline data.
        - `checkpoint`: unused in the in-memory inline implementation.

        What it does:
        - Reads records from the source payload.
        - Splits them into execution batches using configured batching hints.

        Returns:
        - A list of `RecordBatch` values.
        """
        del resolved_connection, checkpoint
        inline = _ensure_source_type(source, InlineSourceSpec)
        return _chunk_records(inline.selection.records, inline.batching.batch_size, inline.batching.max_records)


class PostgresSourceAdapter:
    kind = "postgres"

    def validate(self, source: SourceSpec, resolved_connection: dict[str, Any]) -> SourceValidationResult:
        """Validate a Postgres source definition.

        Params:
        - `source`: the registered `PostgresSourceSpec`.
        - `resolved_connection`: resolved connection options or mocked rows.

        What it does:
        - Verifies a `connection_ref.connection_id` exists.
        - Warns when no live connection or mocked rows are available.

        Returns:
        - `SourceValidationResult`.
        """
        postgres = _ensure_source_type(source, PostgresSourceSpec)
        errors: list[str] = []
        warnings: list[str] = []
        if not postgres.connection_ref.connection_id:
            errors.append("connection_ref.connection_id is required")
        if not resolved_connection and "rows" not in postgres.options:
            warnings.append("No resolved connection found; scan requires a configured connection or mock rows")
        return SourceValidationResult(valid=not errors, errors=errors, warnings=warnings)

    def discover(self, source: SourceSpec, resolved_connection: dict[str, Any]) -> SourceMetadata:
        """Load enough rows to describe a Postgres source for planning."""
        records = self._read_rows(_ensure_source_type(source, PostgresSourceSpec), resolved_connection)
        return SourceMetadata(
            source_id=source.source_id,
            kind=source.kind,
            schema=infer_schema(records[:1]),
            estimated_records=len(records),
        )

    def scan(
        self,
        source: SourceSpec,
        resolved_connection: dict[str, Any],
        checkpoint: dict[str, Any] | None = None,
    ) -> list[RecordBatch]:
        """Execute the Postgres selection and normalize the result into batches."""
        del checkpoint
        postgres = _ensure_source_type(source, PostgresSourceSpec)
        records = self._read_rows(postgres, resolved_connection)
        return _chunk_records(records, postgres.batching.batch_size, postgres.batching.max_records)

    def _read_rows(self, source: PostgresSourceSpec, resolved_connection: dict[str, Any]) -> list[dict[str, Any]]:
        """Resolve Postgres data from mocks first, then from a live SQL query.

        Params:
        - `source`: concrete Postgres source spec.
        - `resolved_connection`: merged connection settings from the resolver.

        What it does:
        - Prefers mocked rows in tests for determinism and speed.
        - Falls back to a live psycopg query when a real connection is provided.

        Returns:
        - A fully materialized list of row dicts.
        """
        if "rows" in source.options:
            return list(source.options["rows"])
        if "rows" in resolved_connection:
            return list(resolved_connection["rows"])
        if psycopg is None or dict_row is None:
            raise ValueError("Postgres adapter requires psycopg to execute live SQL scans")
        connection_args = _postgres_connection_args(resolved_connection)
        query = _postgres_query(source)
        # The adapter intentionally materializes the full query result before
        # chunking so every downstream service receives the same batch format.
        with psycopg.connect(row_factory=dict_row, **connection_args) as connection:
            with connection.cursor() as cursor:
                cursor.execute(query)
                rows = cursor.fetchall()
        return [dict(row) for row in rows]


class MongoSourceAdapter:
    kind = "mongodb"

    def validate(self, source: SourceSpec, resolved_connection: dict[str, Any]) -> SourceValidationResult:
        """Validate a Mongo source definition against mocked or live inputs."""
        mongo = _ensure_source_type(source, MongoSourceSpec)
        warnings: list[str] = []
        if not resolved_connection and "documents" not in mongo.options:
            warnings.append("No resolved connection found; scan requires configured documents or a live connector implementation")
        return SourceValidationResult(valid=True, warnings=warnings)

    def discover(self, source: SourceSpec, resolved_connection: dict[str, Any]) -> SourceMetadata:
        """Describe a Mongo source by inspecting resolved documents."""
        documents = self._read_documents(_ensure_source_type(source, MongoSourceSpec), resolved_connection)
        return SourceMetadata(
            source_id=source.source_id,
            kind=source.kind,
            schema=infer_schema(documents[:1]),
            estimated_records=len(documents),
        )

    def scan(
        self,
        source: SourceSpec,
        resolved_connection: dict[str, Any],
        checkpoint: dict[str, Any] | None = None,
    ) -> list[RecordBatch]:
        """Normalize resolved Mongo documents into shared record batches."""
        del checkpoint
        mongo = _ensure_source_type(source, MongoSourceSpec)
        documents = self._read_documents(mongo, resolved_connection)
        return _chunk_records(documents, mongo.batching.batch_size, mongo.batching.max_records)

    def _read_documents(self, source: MongoSourceSpec, resolved_connection: dict[str, Any]) -> list[dict[str, Any]]:
        """Resolve Mongo documents from source options or connection payloads."""
        if "documents" in source.options:
            return list(source.options["documents"])
        if "documents" in resolved_connection:
            return list(resolved_connection["documents"])
        raise ValueError("Mongo adapter requires resolved documents or a live connector implementation")


class ObjectStorageSourceAdapter:
    kind = "object_storage"

    def validate(self, source: SourceSpec, resolved_connection: dict[str, Any]) -> SourceValidationResult:
        """Validate an object-storage source against the local file-backed adapter.

        Params:
        - `source`: object storage source definition.
        - `resolved_connection`: unused in the local file implementation.

        What it does:
        - Warns when the referenced file path cannot be resolved locally.

        Returns:
        - `SourceValidationResult`.
        """
        del resolved_connection
        object_source = _ensure_source_type(source, ObjectStorageSourceSpec)
        warnings: list[str] = []
        uri = object_source.selection.uri or object_source.selection.prefix or ""
        if not uri.startswith("file://") and not Path(uri).exists() and not Path(uri.replace("file://", "")).exists():
            warnings.append("Object storage adapter currently resolves local file paths and file:// URIs only")
        return SourceValidationResult(valid=True, warnings=warnings)

    def discover(self, source: SourceSpec, resolved_connection: dict[str, Any]) -> SourceMetadata:
        """Describe object-storage records after local file materialization."""
        del resolved_connection
        object_source = _ensure_source_type(source, ObjectStorageSourceSpec)
        records = self._read_records(object_source)
        return SourceMetadata(
            source_id=source.source_id,
            kind=source.kind,
            schema=infer_schema(records[:1]),
            estimated_records=len(records),
        )

    def scan(
        self,
        source: SourceSpec,
        resolved_connection: dict[str, Any],
        checkpoint: dict[str, Any] | None = None,
    ) -> list[RecordBatch]:
        """Load a supported local file and split the rows into record batches."""
        del resolved_connection, checkpoint
        object_source = _ensure_source_type(source, ObjectStorageSourceSpec)
        records = self._read_records(object_source)
        return _chunk_records(records, object_source.batching.batch_size, object_source.batching.max_records)

    def _read_records(self, source: ObjectStorageSourceSpec) -> list[dict[str, Any]]:
        """Read JSON, JSONL, or CSV content from a local object-storage target.

        Params:
        - `source`: object storage spec whose `uri` or `prefix` resolves to a file.

        What it does:
        - Interprets the path as a local path or `file://` URI.
        - Parses the configured file format into normalized record dicts.

        Returns:
        - A list of records extracted from the file.
        """
        raw_uri = source.selection.uri or source.selection.prefix or ""
        path = Path(raw_uri.replace("file://", ""))
        if source.format == "json":
            payload = json.loads(path.read_text())
            if isinstance(payload, list):
                return [item for item in payload if isinstance(item, dict)]
            if isinstance(payload, dict):
                return [payload]
            return []
        if source.format == "jsonl":
            records: list[dict[str, Any]] = []
            for line in path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                payload = json.loads(line)
                if isinstance(payload, dict):
                    records.append(payload)
            return records
        if source.format == "csv":
            with path.open(newline="") as handle:
                return [dict(row) for row in csv.DictReader(handle)]
        raise ValueError(f"Unsupported object storage format for local materialization: {source.format}")


class DefaultSourceResolver:
    """Resolve source specs through a registry plus adapter catalog."""

    def __init__(
        self,
        *,
        catalog: SourceCatalog | None = None,
        connections: ConnectionResolver | None = None,
        adapters: list[SourceAdapter] | None = None,
    ) -> None:
        self._catalog = catalog or InMemorySourceCatalog()
        self._connections = connections or InMemoryConnectionResolver()
        # The resolver is the single dispatch point that maps `source.kind`
        # values onto concrete adapter implementations.
        self._adapters = {adapter.kind: adapter for adapter in (adapters or _default_adapters())}

    def register(self, source: SourceSpec) -> SourceSpec:
        """Persist one source definition in the configured source catalog."""
        return self._catalog.save(source)

    def get(self, source_id: str) -> SourceSpec | None:
        """Fetch one registered source definition by id."""
        return self._catalog.get(source_id)

    def list(self) -> list[SourceSpec]:
        """Return all registered source definitions."""
        return self._catalog.list()

    def validate(self, source_id: str) -> SourceValidationResult:
        """Validate a stored source using the adapter for its declared kind."""
        source = self._require_source(source_id)
        adapter = self._adapters[source.kind]
        return adapter.validate(source, self._resolve_connection(source))

    def discover(self, source_id: str) -> SourceMetadata:
        """Return schema and record-estimate metadata for one stored source."""
        source = self._require_source(source_id)
        adapter = self._adapters[source.kind]
        return adapter.discover(source, self._resolve_connection(source))

    def materialize(self, dataset: DatasetInput) -> list[RecordBatch]:
        """Turn a `DatasetInput` into normalized execution batches.

        Params:
        - `dataset`: inline records, embedded source, or stored source reference.

        What it does:
        - Handles the inline-record special case directly.
        - Resolves a stored or embedded source through the adapter registry.

        Returns:
        - A list of `RecordBatch` values used by profiling, pipelines, and model execution.
        """
        if dataset.inline_records:
            # Inline records bypass the source catalog so test and compatibility
            # callers can submit payloads directly.
            inline = InlineSourceSpec(source_id="inline", selection={"records": dataset.inline_records})
            return self._adapters["inline"].scan(inline, {}, {})
        source = dataset.source if dataset.source is not None else self._require_source(dataset.source_id or "")
        adapter = self._adapters[source.kind]
        return adapter.scan(source, self._resolve_connection(source), source.checkpoint)

    def _resolve_connection(self, source: SourceSpec) -> dict[str, Any]:
        """Merge resolved connection settings with per-source overrides."""
        connection_ref = getattr(source, "connection_ref", None)
        if isinstance(connection_ref, ConnectionRef):
            resolved = self._connections.resolve(connection_ref.connection_id)
            # Per-source options take precedence so one logical connection can be
            # reused with small request-specific overrides.
            return {**resolved, **connection_ref.options}
        return {}

    def _require_source(self, source_id: str) -> SourceSpec:
        """Load a source or raise a user-facing error when it does not exist."""
        source = self._catalog.get(source_id)
        if source is None:
            raise ValueError(f"Unknown source: {source_id}")
        return source


def _default_adapters() -> list[SourceAdapter]:
    """Return the standard adapter set shipped with Dagents."""
    return [
        InlineSourceAdapter(),
        PostgresSourceAdapter(),
        MongoSourceAdapter(),
        ObjectStorageSourceAdapter(),
    ]


def _ensure_source_type(source: SourceSpec, expected_type: type[Any]) -> Any:
    """Assert the runtime source variant before adapter-specific access."""
    if not isinstance(source, expected_type):
        raise TypeError(f"Expected source type {expected_type.__name__}, got {type(source).__name__}")
    return source


def _chunk_records(records: list[dict[str, Any]], batch_size: int, max_records: int | None) -> list[RecordBatch]:
    if max_records is not None:
        records = records[:max_records]
    schema = infer_schema(records)
    chunks: list[RecordBatch] = []
    if not records:
        return [RecordBatch(records=[], schema=schema, stats=RecordBatchStats(record_count=0))]
    for index in range(0, len(records), batch_size):
        chunk = records[index : index + batch_size]
        next_offset = index + len(chunk)
        chunks.append(
            RecordBatch(
                records=chunk,
                schema=schema,
                next_checkpoint={"offset": next_offset} if next_offset < len(records) else {},
                stats=RecordBatchStats(record_count=len(chunk), truncated=max_records is not None and len(records) == max_records),
            )
        )
    return chunks


def _postgres_connection_args(resolved_connection: dict[str, Any]) -> dict[str, Any]:
    connection_args: dict[str, Any] = {}
    aliases = {
        "dbname": ("dbname", "database"),
        "user": ("user", "username"),
        "password": ("password",),
        "host": ("host",),
        "port": ("port",),
        "sslmode": ("sslmode",),
    }
    for target, keys in aliases.items():
        for key in keys:
            value = resolved_connection.get(key)
            if value not in (None, ""):
                connection_args[target] = value
                break
    if "port" in connection_args:
        connection_args["port"] = int(connection_args["port"])
    return connection_args


def _postgres_query(source: PostgresSourceSpec) -> str:
    if source.selection.sql:
        return source.selection.sql

    table = source.selection.table
    if not table:
        raise ValueError("Postgres selection requires sql or table")

    columns = source.selection.columns or ["*"]
    query = f"SELECT {', '.join(columns)} FROM {table}"
    if source.selection.where:
        query += f" WHERE {source.selection.where}"
    if source.selection.order_by:
        query += f" ORDER BY {', '.join(source.selection.order_by)}"
    if source.batching.max_records is not None:
        query += f" LIMIT {int(source.batching.max_records)}"
    return query
