"""Async execution contracts."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from agents.common.domain.base import DagentsModel


JobStatus = Literal["queued", "running", "completed", "failed", "cancelled"]


class JobHandle(DagentsModel):
    job_id: str
    status: JobStatus
    submitted_at: int
    started_at: int | None = None
    completed_at: int | None = None


class ModelJob(DagentsModel):
    job: JobHandle
    job_type: Literal["model_execution", "training"] = "model_execution"
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class PipelineRun(DagentsModel):
    job: JobHandle
    pipeline_id: str
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
