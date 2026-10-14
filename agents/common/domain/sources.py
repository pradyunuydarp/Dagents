"""Source contracts for generic dataset ingress."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from agents.common.domain.base import DagentsModel


class ConnectionRef(DagentsModel):
    """A named connection, with settings that apply to one source only."""

    connection_id: str = Field(
        description=(
            "The name of a connection the service's connection resolver knows. The resolver returns that "
            "connection's settings, such as host, database and user. An unknown name resolves to no settings."
        )
    )
    options: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Settings merged over the resolved connection's settings, for this source only; these win. A "
            "Postgres source reads `role` here: the role every query runs as."
        ),
    )


class InlineSelection(DagentsModel):
    """The records of an inline source."""

    records: list[dict[str, Any]] = Field(
        default_factory=list, description="The records themselves, one JSON object per record."
    )


class PostgresSelection(DagentsModel):
    """What a Postgres source reads: a SQL query, or a table with optional columns, filter and order."""

    sql: str | None = Field(
        default=None,
        description="A complete SQL query. When it is set, `table`, `columns`, `where` and `order_by` are ignored.",
    )
    table: str | None = Field(
        default=None, description="The table to read when `sql` is not set. One of `sql` and `table` is required."
    )
    columns: list[str] = Field(
        default_factory=list, description="The columns to read from `table`. Empty reads every column (`*`)."
    )
    where: str | None = Field(default=None, description="A SQL condition added to the table query as `WHERE`.")
    order_by: list[str] = Field(default_factory=list, description="Columns for the table query's `ORDER BY`.")

    @model_validator(mode="after")
    def validate_shape(self) -> "PostgresSelection":
        if not self.sql and not self.table:
            raise ValueError("Postgres selection requires either sql or table")
        return self


class MongoSelection(DagentsModel):
    """What a MongoDB source reads.

    This version has no live MongoDB connector. The adapter returns the
    documents in the source's `options.documents` or in its connection's
    `documents`, so `filter`, `projection` and `sort` are stored but not applied.
    """

    database: str = Field(description="The database to read.")
    collection: str = Field(description="The collection to read.")
    filter: dict[str, Any] = Field(
        default_factory=dict, description="A MongoDB query filter. Stored with the source; not applied in this version."
    )
    projection: dict[str, Any] = Field(
        default_factory=dict, description="A MongoDB projection. Stored with the source; not applied in this version."
    )
    sort: list[dict[str, Any]] = Field(
        default_factory=list, description="A MongoDB sort order. Stored with the source; not applied in this version."
    )


class ObjectStorageSelection(DagentsModel):
    """The file an object-storage source reads. This version reads local files only."""

    uri: str | None = Field(
        default=None,
        description="The file to read: a local path or a `file://` URI. One of `uri` and `prefix` is required.",
    )
    prefix: str | None = Field(default=None, description="Read as the file's path when `uri` is not set.")
    glob: str | None = Field(default=None, description="A file name pattern. Stored; not applied in this version.")
    compression: str | None = Field(
        default=None, description="The file's compression. Stored; not applied in this version."
    )

    @model_validator(mode="after")
    def validate_shape(self) -> "ObjectStorageSelection":
        if not self.uri and not self.prefix:
            raise ValueError("Object storage selection requires either uri or prefix")
        return self


class SourceBatching(DagentsModel):
    """How a source's records are split into batches when it is read."""

    batch_size: int = Field(default=1000, ge=1, description="The number of records in each batch. At least 1.")
    max_records: int | None = Field(
        default=None,
        ge=1,
        description=(
            "The most records to read; the rest are dropped. A Postgres table query also gets it as `LIMIT`. "
            "Empty reads every record."
        ),
    )


# Descriptions shared by the four source kinds below.
_SOURCE_ID = "The id the source is stored under. Registering another source with the same id replaces it."
_KIND = (
    "Which adapter reads the source: `inline`, `postgres`, `mongodb` or `object_storage`. It also decides "
    "which of the four source shapes the body must have."
)
_SCHEMA_HINT = (
    "Field names mapped to type names. The planner's `dataset source` commands read it when a service sends "
    "them the source; the source adapters do not."
)
_BATCHING = "How the records are split into batches when the source is read."
_CHECKPOINT = "Where to resume reading. Passed to the adapter; the four adapters in this version ignore it."


class InlineSourceSpec(DagentsModel):
    """Records sent inside the source definition itself."""

    source_id: str = Field(description=_SOURCE_ID)
    kind: Literal["inline"] = Field(default="inline", description=_KIND)
    connection_ref: ConnectionRef | None = Field(default=None, description="Not used by inline sources.")
    selection: InlineSelection = Field(default_factory=InlineSelection, description="The records themselves.")
    format: Literal["rows", "json"] = Field(
        default="rows", description="`rows` or `json`. Stored; the inline adapter reads `selection.records` either way."
    )
    schema_hint: dict[str, str] = Field(default_factory=dict, description=_SCHEMA_HINT)
    batching: SourceBatching = Field(default_factory=SourceBatching, description=_BATCHING)
    checkpoint: dict[str, Any] = Field(default_factory=dict, description=_CHECKPOINT)
    options: dict[str, Any] = Field(default_factory=dict, description="Settings for the adapter. The inline adapter reads none.")


class PostgresSourceSpec(DagentsModel):
    """A PostgreSQL table or query, read through psycopg."""

    source_id: str = Field(description=_SOURCE_ID)
    kind: Literal["postgres"] = Field(default="postgres", description=_KIND)
    connection_ref: ConnectionRef = Field(
        description="The connection to read through. Its `options.role` sets the role each query runs as."
    )
    selection: PostgresSelection = Field(description="The query, or the table, to read.")
    format: Literal["rows"] = Field(default="rows", description="Always `rows`.")
    schema_hint: dict[str, str] = Field(default_factory=dict, description=_SCHEMA_HINT)
    batching: SourceBatching = Field(default_factory=SourceBatching, description=_BATCHING)
    checkpoint: dict[str, Any] = Field(default_factory=dict, description=_CHECKPOINT)
    options: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Settings for the adapter. `rows` holds fixed records that are returned instead of running the "
            "query; tests and demos use it."
        ),
    )


class MongoSourceSpec(DagentsModel):
    """A MongoDB collection. This version returns fixed documents instead of querying a server."""

    source_id: str = Field(description=_SOURCE_ID)
    kind: Literal["mongodb"] = Field(default="mongodb", description=_KIND)
    connection_ref: ConnectionRef = Field(
        description="The connection to read through. Its settings may hold `documents` to return."
    )
    selection: MongoSelection = Field(description="The database and collection to read.")
    format: Literal["rows", "bson", "json"] = Field(
        default="rows", description="`rows`, `bson` or `json`. Stored; the adapter returns the documents as they are."
    )
    schema_hint: dict[str, str] = Field(default_factory=dict, description=_SCHEMA_HINT)
    batching: SourceBatching = Field(default_factory=SourceBatching, description=_BATCHING)
    checkpoint: dict[str, Any] = Field(default_factory=dict, description=_CHECKPOINT)
    options: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Settings for the adapter. `documents` holds the documents to return. Without it, and without "
            "`documents` in the connection, reading the source fails."
        ),
    )


class ObjectStorageSourceSpec(DagentsModel):
    """A file of records. This version reads local paths and `file://` URIs only."""

    source_id: str = Field(description=_SOURCE_ID)
    kind: Literal["object_storage"] = Field(default="object_storage", description=_KIND)
    connection_ref: ConnectionRef = Field(description="Required, but not used by the local file adapter.")
    selection: ObjectStorageSelection = Field(description="The file to read.")
    format: Literal["json", "jsonl", "csv", "parquet"] = Field(
        default="json",
        description=(
            "How the file is parsed: `json` (a list of objects, or one object), `jsonl` (one object per line) "
            "or `csv` (a header row, then one record per row). `parquet` is accepted, but reading it fails in "
            "this version."
        ),
    )
    schema_hint: dict[str, str] = Field(default_factory=dict, description=_SCHEMA_HINT)
    batching: SourceBatching = Field(default_factory=SourceBatching, description=_BATCHING)
    checkpoint: dict[str, Any] = Field(default_factory=dict, description=_CHECKPOINT)
    options: dict[str, Any] = Field(default_factory=dict, description="Settings for the adapter. The file adapter reads none.")


SourceSpec = Annotated[
    InlineSourceSpec | PostgresSourceSpec | MongoSourceSpec | ObjectStorageSourceSpec,
    Field(discriminator="kind"),
]


class DatasetInput(DagentsModel):
    """Where a request's records come from. At least one of the three fields must be set."""

    inline_records: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Records sent in the request. When not empty they are used, and `source` and `source_id` are ignored.",
    )
    source: SourceSpec | None = Field(
        default=None,
        description="A source definition sent with the request. It is read for this request and not stored.",
    )
    source_id: str | None = Field(
        default=None,
        description=(
            "The id of a source registered with the same service (`POST /api/v1/sources`). Used when "
            "`inline_records` and `source` are empty. An unknown id fails the request."
        ),
    )

    @model_validator(mode="after")
    def validate_source(self) -> "DatasetInput":
        has_inline = bool(self.inline_records)
        has_source = self.source is not None or self.source_id is not None
        if not has_inline and not has_source:
            raise ValueError("Dataset input requires inline_records, source, or source_id")
        return self


class RecordSchemaField(DagentsModel):
    name: str
    dtype: str


class RecordBatchStats(DagentsModel):
    record_count: int
    truncated: bool = False


class RecordBatch(DagentsModel):
    records: list[dict[str, Any]] = Field(default_factory=list)
    schema_fields: list[RecordSchemaField] = Field(default_factory=list, alias="schema")
    next_checkpoint: dict[str, Any] = Field(default_factory=dict)
    stats: RecordBatchStats = Field(default_factory=lambda: RecordBatchStats(record_count=0))


class SourceValidationResult(DagentsModel):
    """What a source's adapter found when it checked the stored definition."""

    valid: bool = Field(description="True when the adapter found no errors. Warnings do not make a source invalid.")
    errors: list[str] = Field(
        default_factory=list,
        description="Problems that stop the source from being read, such as a role name that is not a valid identifier.",
    )
    warnings: list[str] = Field(
        default_factory=list,
        description=(
            "Problems that do not make the source invalid, such as an inline source with no records yet, or no "
            "connection settings to read through."
        ),
    )


class SourceMetadata(DagentsModel):
    source_id: str
    kind: str
    schema_fields: list[RecordSchemaField] = Field(default_factory=list, alias="schema")
    estimated_records: int | None = None
