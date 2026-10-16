"""Shared source adapters and registries.

Besides the adapters, this module has three helpers for an LMA that serves a
configured site:

- :func:`redact_source` returns a copy of a source definition that is safe to
  return to a caller: connection options other than a few safe keys are
  replaced, so a password sent in a definition is never echoed back.
- :func:`check_site_request_source` and :func:`check_site_request_dataset`
  refuse what a request may not ask of a site's LMA: a source that reads
  through a connection or a file, a role, a credential, a connection target,
  embedded SQL, or a source definition embedded in a dataset request. The
  site's records come from its configured source only.

The LMA decides when to call the two checks; this module does not know whether
a site is configured.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, ClassVar

try:  # pragma: no cover - exercised indirectly by integration tests when installed
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row
except ModuleNotFoundError:  # pragma: no cover - dependency is optional in some local environments
    psycopg = None
    sql = None
    dict_row = None

from agents.common.application.ports import ConnectionResolver, SourceAdapter, SourceCatalog, SourceResolver
from agents.common.domain.errors import DagentsError
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
from agents.common.infrastructure.postgres_roles import RoleScopeError, acting_as, connect, validate_role_name

#: Statement timeout for one Postgres source read, in milliseconds. The same as
#: a store operation's (``postgres_lease.DEFAULT_STATEMENT_TIMEOUT_MS``).
DEFAULT_SOURCE_STATEMENT_TIMEOUT_MS = 15_000

#: What replaces a hidden connection option in a returned source definition.
REDACTED = "[redacted]"

#: Connection options a returned source definition shows as they are. None of
#: them is a credential or says where the database is. Every other value,
#: including keys this version does not know, is replaced with ``REDACTED``.
SHOWN_CONNECTION_OPTIONS = frozenset({"role", "sslmode", "connect_timeout"})

#: Connection options a request may send to a site's LMA: fixed records, which
#: are data, not a way to reach a database.
SITE_REQUEST_CONNECTION_OPTIONS = frozenset({"rows", "documents"})

#: Source kinds a request may register with, or send to, a site's LMA. Every
#: other kind reads through a connection or a local file.
SITE_REQUEST_SOURCE_KINDS = frozenset({"inline"})


class SourceRequestRefused(DagentsError, PermissionError):
    """A request asked a site's LMA for something only its configuration may set.

    The message names what was refused, never a value the request sent.
    """

    code: ClassVar[str] = "forbidden"
    http_status: ClassVar[int] = 403


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
    """Read a Postgres table or query, in one transaction, as the source's role.

    Params:
    - `statement_timeout_ms`: the statement timeout for each read, set with
      ``SET LOCAL`` in the read's transaction. A query that runs longer is
      cancelled with ``QueryCanceled``.
    - `omit_null_keys`: leave a NULL value out of its record instead of
      returning it as ``None``. For consumers whose records mark a missing
      value by an absent key.

    Both are configuration of the service that builds the adapter. A source
    definition cannot change them.
    """

    kind = "postgres"

    def __init__(
        self, *, statement_timeout_ms: int = DEFAULT_SOURCE_STATEMENT_TIMEOUT_MS, omit_null_keys: bool = False
    ) -> None:
        if int(statement_timeout_ms) <= 0:
            raise ValueError("the statement timeout must be a positive number of milliseconds")
        self.statement_timeout_ms = int(statement_timeout_ms)
        self.omit_null_keys = bool(omit_null_keys)

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
        try:
            _postgres_role(resolved_connection)
        except RoleScopeError as error:
            errors.append(str(error))
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
          The connection is autocommit and the query runs in one transaction
          with the statement timeout set by ``SET LOCAL``.
        - When the resolved connection names a `role`, runs the query as that
          role through :func:`postgres_roles.acting_as`: ``SET LOCAL ROLE`` in
          the same transaction, with `current_user` checked first. The role and
          the timeout end when the transaction ends.
        - Leaves NULL values out of the records when `omit_null_keys` is set.

        Returns:
        - A fully materialized list of row dicts.
        """
        role = _postgres_role(resolved_connection)
        if "rows" in source.options:
            return self._records(source.options["rows"])
        if "rows" in resolved_connection:
            return self._records(resolved_connection["rows"])
        if psycopg is None or dict_row is None:
            raise ValueError("Postgres adapter requires psycopg to execute live SQL scans")
        connection_args = _postgres_connection_args(resolved_connection)
        query = _postgres_query(source)
        timeout = {"statement_timeout": f"{self.statement_timeout_ms}ms"}
        # The adapter intentionally materializes the full query result before
        # chunking so every downstream service receives the same batch format.
        with connect(connection_args, row_factory=dict_row) as connection:
            if role is not None:
                with acting_as(connection, role, timeout):
                    rows = connection.execute(query).fetchall()
            else:
                with connection.transaction():
                    connection.execute(
                        sql.SQL("SET LOCAL statement_timeout = {}").format(sql.Literal(timeout["statement_timeout"]))
                    )
                    rows = connection.execute(query).fetchall()
        return self._records([dict(row) for row in rows])

    def _records(self, rows: Any) -> list[dict[str, Any]]:
        """The rows as a list, without NULL values when `omit_null_keys` is set."""
        if self.omit_null_keys:
            return [{key: value for key, value in dict(row).items() if value is not None} for row in rows]
        return list(rows)


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


def default_adapters(
    *, statement_timeout_ms: int = DEFAULT_SOURCE_STATEMENT_TIMEOUT_MS, omit_null_keys: bool = False
) -> list[SourceAdapter]:
    """Return the standard adapter set shipped with Dagents.

    Params:
    - `statement_timeout_ms`, `omit_null_keys`: passed to the
      :class:`PostgresSourceAdapter`, for a service that configures them.
    """
    return [
        InlineSourceAdapter(),
        PostgresSourceAdapter(statement_timeout_ms=statement_timeout_ms, omit_null_keys=omit_null_keys),
        MongoSourceAdapter(),
        ObjectStorageSourceAdapter(),
    ]


def _default_adapters() -> list[SourceAdapter]:
    """The standard adapter set with its default settings."""
    return default_adapters()


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
    """Translate a resolved connection into libpq keywords.

    The mapping is an allowlist, so a connection payload cannot smuggle
    arbitrary keywords into the driver. `connect_timeout` is on it because a
    host that accepts packets and never answers — a wrong region, a firewall
    that drops rather than refuses, a database that has gone away — otherwise
    holds the caller indefinitely. A source adapter that can hang forever makes
    every service above it able to hang forever, so the bound belongs here
    rather than in each consumer.
    """
    connection_args: dict[str, Any] = {}
    aliases = {
        "dbname": ("dbname", "database"),
        "user": ("user", "username"),
        "password": ("password",),
        "host": ("host",),
        "port": ("port",),
        "sslmode": ("sslmode",),
        "connect_timeout": ("connect_timeout",),
    }
    for target, keys in aliases.items():
        for key in keys:
            value = resolved_connection.get(key)
            if value not in (None, ""):
                connection_args[target] = value
                break
    for integer_key in ("port", "connect_timeout"):
        if integer_key in connection_args:
            connection_args[integer_key] = int(connection_args[integer_key])
    return connection_args


def _postgres_role(resolved_connection: dict[str, Any]) -> str | None:
    """Return the validated `role` option, or None when there is none.

    `role` is not a libpq keyword, so it is not on the allowlist above. The
    adapter applies it with `SET LOCAL ROLE` after connecting.
    """
    role = resolved_connection.get("role")
    if role in (None, ""):
        return None
    return validate_role_name(role)


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


def redact_source(source: SourceSpec) -> SourceSpec:
    """Return a copy of ``source`` that is safe to return to a caller.

    Params:
    - `source`: a stored or submitted source definition.

    What it does:
    - Keeps every key of ``connection_ref.options`` and replaces each value
      with ``REDACTED``, except the keys in ``SHOWN_CONNECTION_OPTIONS``. A
      password, user, host or database name sent in a definition is then never
      echoed back, and neither are fixed records.

    Returns:
    - The same definition when it has no connection options, otherwise a copy.
      The stored definition is not changed.
    """
    connection_ref = getattr(source, "connection_ref", None)
    if not isinstance(connection_ref, ConnectionRef) or not connection_ref.options:
        return source
    options = {
        key: value if key in SHOWN_CONNECTION_OPTIONS else REDACTED
        for key, value in connection_ref.options.items()
    }
    return source.model_copy(update={"connection_ref": connection_ref.model_copy(update={"options": options})})


def check_site_request_source(source: SourceSpec) -> None:
    """Refuse a source definition that a request may not send to a site's LMA.

    Params:
    - `source`: a source definition from a request body, to register or to read
      once.

    What it does:
    - Refuses every kind outside ``SITE_REQUEST_SOURCE_KINDS``: a Postgres,
      MongoDB or object storage source reads through a connection or a local
      file, and a site's records come from its configured source only. For a
      Postgres source it also names the embedded SQL: every part of its
      selection is pasted into the query text.
    - Refuses connection options outside ``SITE_REQUEST_CONNECTION_OPTIONS``,
      in any kind: a role, a credential or a connection target is
      configuration, never part of a request.

    Raises:
    - :class:`SourceRequestRefused` naming every problem found. The message
      names keys and kinds, never a value the request sent.
    """
    problems: list[str] = []
    if source.kind not in SITE_REQUEST_SOURCE_KINDS:
        allowed = ", ".join(sorted(SITE_REQUEST_SOURCE_KINDS))
        problems.append(
            f"a request may not send a {source.kind} source to a site's LMA, only: {allowed}; "
            "the site's records come from its configured source"
        )
    if isinstance(source, PostgresSourceSpec):
        # Every part of a Postgres selection is pasted into the query text,
        # the table name included.
        problems.append(
            "a request may not send embedded SQL (a query, table, columns, condition or order) to a site's LMA"
        )
    connection_ref = getattr(source, "connection_ref", None)
    if isinstance(connection_ref, ConnectionRef):
        refused = sorted(set(connection_ref.options) - SITE_REQUEST_CONNECTION_OPTIONS)
        if refused:
            problems.append(
                "a request may not set these connection options on a site's LMA: "
                + ", ".join(refused)
                + "; roles, credentials and connection targets come from the site's configuration"
            )
    if problems:
        raise SourceRequestRefused("; ".join(problems))


def check_site_request_dataset(dataset: DatasetInput) -> None:
    """Refuse a dataset request that embeds a source definition, for a site's LMA.

    Params:
    - `dataset`: the ``dataset`` of a profiling, pipeline or model request.

    What it does:
    - Refuses ``dataset.source``. Inline records (the caller's own data) and
      ``source_id`` (a source registered with the LMA, which was checked when
      it was registered) are accepted.

    Raises:
    - :class:`SourceRequestRefused`.
    """
    if dataset.source is not None:
        raise SourceRequestRefused(
            "a request may not embed a source definition in a dataset sent to a site's LMA; "
            "send inline_records or the source_id of a registered source"
        )
