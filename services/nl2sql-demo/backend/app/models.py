"""API and orchestration models for the Dagents NL2SQL demo."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from agents.common.domain.base import DagentsModel


class HealthResponse(DagentsModel):
    status: str
    service: str
    environment: str
    transport: str


class SchemaColumn(DagentsModel):
    name: str
    dtype: str = "TEXT"
    description: str | None = None


class SchemaTable(DagentsModel):
    name: str
    columns: list[SchemaColumn] = Field(default_factory=list)


class SamplePrompt(DagentsModel):
    sample_id: str
    title: str
    question: str
    tables: list[SchemaTable]


class ModelDescriptor(DagentsModel):
    model_id: str
    label: str
    artifact_path: str | None = None
    adapter_kind: Literal["codeqwen_lora", "codet5_seq2seq", "heuristic"]
    prompt_format: str
    available: bool = True
    notes: str = ""


class DagentsTraceStep(DagentsModel):
    name: str
    status: Literal["ok", "warning", "error", "skipped"]
    detail: str
    payload: dict[str, Any] | list[Any] | None = None


class NL2SQLRequest(DagentsModel):
    question: str
    tables: list[SchemaTable]
    model_id: str | None = None
    max_new_tokens: int = Field(default=128, ge=16, le=512)
    use_dagents_services: bool = True


class NL2SQLResponse(DagentsModel):
    sql: str
    model_id: str
    model_label: str
    prompt: str
    schema_ddl: str
    adapter_kind: str
    used_fallback: bool = False
    fallback_detail: str | None = None
    dagents_trace: list[DagentsTraceStep] = Field(default_factory=list)


class DagentsServiceStatus(DagentsModel):
    name: str
    url: str
    status: Literal["ok", "unavailable"]
    detail: str


class DagentsBackendStatus(DagentsModel):
    services: list[DagentsServiceStatus]
