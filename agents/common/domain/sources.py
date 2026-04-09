"""Source contracts for generic dataset ingress."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from agents.common.domain.base import DagentsModel


class ConnectionRef(DagentsModel):
    connection_id: str
    options: dict[str, Any] = Field(default_factory=dict)


class InlineSelection(DagentsModel):
    records: list[dict[str, Any]] = Field(default_factory=list)


class PostgresSelection(DagentsModel):
    sql: str | None = None
    table: str | None = None
    columns: list[str] = Field(default_factory=list)
    where: str | None = None
    order_by: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_shape(self) -> "PostgresSelection":
        if not self.sql and not self.table:
            raise ValueError("Postgres selection requires either sql or table")
        return self


class MongoSelection(DagentsModel):
    database: str
    collection: str
    filter: dict[str, Any] = Field(default_factory=dict)
    projection: dict[str, Any] = Field(default_factory=dict)
    sort: list[dict[str, Any]] = Field(default_factory=list)


class ObjectStorageSelection(DagentsModel):
    uri: str | None = None
    prefix: str | None = None
    glob: str | None = None
    compression: str | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> "ObjectStorageSelection":
        if not self.uri and not self.prefix:
            raise ValueError("Object storage selection requires either uri or prefix")
        return self


class SourceBatching(DagentsModel):
    batch_size: int = Field(default=1000, ge=1)
    max_records: int | None = Field(default=None, ge=1)


class InlineSourceSpec(DagentsModel):
    source_id: str
    kind: Literal["inline"] = "inline"
    connection_ref: ConnectionRef | None = None
    selection: InlineSelection = Field(default_factory=InlineSelection)
    format: Literal["rows", "json"] = "rows"
    schema_hint: dict[str, str] = Field(default_factory=dict)
    batching: SourceBatching = Field(default_factory=SourceBatching)
    checkpoint: dict[str, Any] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)


class PostgresSourceSpec(DagentsModel):
    source_id: str
    kind: Literal["postgres"] = "postgres"
    connection_ref: ConnectionRef
    selection: PostgresSelection
    format: Literal["rows"] = "rows"
    schema_hint: dict[str, str] = Field(default_factory=dict)
    batching: SourceBatching = Field(default_factory=SourceBatching)
    checkpoint: dict[str, Any] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)


class MongoSourceSpec(DagentsModel):
    source_id: str
    kind: Literal["mongodb"] = "mongodb"
    connection_ref: ConnectionRef
    selection: MongoSelection
    format: Literal["rows", "bson", "json"] = "rows"
    schema_hint: dict[str, str] = Field(default_factory=dict)
    batching: SourceBatching = Field(default_factory=SourceBatching)
    checkpoint: dict[str, Any] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)


class ObjectStorageSourceSpec(DagentsModel):
    source_id: str
    kind: Literal["object_storage"] = "object_storage"
    connection_ref: ConnectionRef
    selection: ObjectStorageSelection
    format: Literal["json", "jsonl", "csv", "parquet"] = "json"
    schema_hint: dict[str, str] = Field(default_factory=dict)
    batching: SourceBatching = Field(default_factory=SourceBatching)
    checkpoint: dict[str, Any] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)


SourceSpec = Annotated[
    InlineSourceSpec | PostgresSourceSpec | MongoSourceSpec | ObjectStorageSourceSpec,
    Field(discriminator="kind"),
]


class DatasetInput(DagentsModel):
    inline_records: list[dict[str, Any]] = Field(default_factory=list)
    source: SourceSpec | None = None
    source_id: str | None = None

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
    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class SourceMetadata(DagentsModel):
    source_id: str
    kind: str
    schema_fields: list[RecordSchemaField] = Field(default_factory=list, alias="schema")
    estimated_records: int | None = None
