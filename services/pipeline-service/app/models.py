"""Pydantic API models for the pipeline service."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from agents.common.domain.models import DatasetInput


class HealthResponse(BaseModel):
    status: str
    service: str
    environment: str
    transport: str


class PipelineCondition(BaseModel):
    field: str
    operator: Literal["eq", "ne", "gt", "gte", "lt", "lte", "contains", "in"]
    value: Any


class AggregationSpec(BaseModel):
    operation: Literal["count", "sum", "avg", "min", "max"]
    field: str | None = None
    alias: str | None = None


class PipelineStep(BaseModel):
    step_id: str
    kind: Literal[
        "enrich_context",
        "filter_items",
        "summarize_items",
        "project_fields",
        "profile_dataset",
        "run_model_job",
    ]
    depends_on: list[str] = Field(default_factory=list)
    config: dict[str, Any] = Field(default_factory=dict)


class PipelineDefinition(BaseModel):
    pipeline_id: str
    description: str = ""
    steps: list[PipelineStep] = Field(default_factory=list)
    registered_at: int | None = None


class PipelineCatalogResponse(BaseModel):
    pipelines: list[PipelineDefinition]


class StepRunResult(BaseModel):
    step_id: str
    kind: str
    status: str
    output: dict[str, Any] = Field(default_factory=dict)
    started_at: int
    completed_at: int


class PipelineRunRequest(BaseModel):
    pipeline_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class PipelineRunResponse(BaseModel):
    run_id: str
    pipeline_id: str
    status: str
    started_at: int
    completed_at: int
    final_payload: dict[str, Any] = Field(default_factory=dict)
    step_results: list[StepRunResult] = Field(default_factory=list)
    error: str | None = None


class PipelineRunCatalogResponse(BaseModel):
    runs: list[PipelineRunResponse]


class SourceRegistrationRequest(BaseModel):
    source: DatasetInput | None = None
