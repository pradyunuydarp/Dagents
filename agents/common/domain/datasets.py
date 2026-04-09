"""Dataset and model execution contracts."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from agents.common.domain.base import DagentsModel
from agents.common.domain.sources import DatasetInput


class DatasetProfileRequest(DagentsModel):
    scope_id: str
    scope_kind: Literal["source", "assimilated"] = "source"
    extraction_strategy: Literal["tabular", "time_series", "text", "hybrid"] = "tabular"
    dataset: DatasetInput | None = None
    records: list[dict[str, Any]] = Field(default_factory=list)
    feature_fields: list[str] = Field(default_factory=list)
    label_field: str | None = None
    batch_size: int = Field(default=1000, ge=1)


class DatasetProfile(DagentsModel):
    scope_id: str
    scope_kind: Literal["source", "assimilated"]
    extraction_strategy: str
    record_count: int
    feature_fields: list[str] = Field(default_factory=list)
    label_field: str | None = None
    numeric_fields: list[str] = Field(default_factory=list)
    categorical_fields: list[str] = Field(default_factory=list)
    partition_count: int = 0
    suggested_models: list[str] = Field(default_factory=list)


class ModelExecutionRequest(DagentsModel):
    scope_id: str
    scope_kind: Literal["source", "assimilated"] = "source"
    task_type: Literal["anomaly_detection", "classification", "forecasting", "embedding", "regression"] = (
        "anomaly_detection"
    )
    model_family: Literal[
        "autoencoder",
        "variational_autoencoder",
        "gru",
        "lstm",
        "naive_bayes",
        "transformer",
        "random_forest",
        "xgboost",
        "linear",
        "custom",
    ] = "autoencoder"
    dataset: DatasetInput | None = None
    records: list[dict[str, Any]] = Field(default_factory=list)
    feature_fields: list[str] = Field(default_factory=list)
    label_field: str | None = None
    hyperparameters: dict[str, Any] = Field(default_factory=dict)
    artifact_prefix: str = "artifacts"
    model_version: str = "v1"


class ModelRunRecord(DagentsModel):
    run_id: str
    agent_role: Literal["LMA", "GMA", "SERVICE"]
    scope_id: str
    scope_kind: Literal["source", "assimilated"]
    task_type: str
    model_family: str
    record_count: int
    feature_count: int
    batch_count: int
    status: Literal["completed", "failed"] = "completed"
    metrics: dict[str, float] = Field(default_factory=dict)
    artifact_uri: str
    started_at: int
    completed_at: int


class ModelExecutionResponse(DagentsModel):
    dataset_profile: DatasetProfile
    run: ModelRunRecord
