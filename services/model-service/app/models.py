"""Pydantic API models for the model service."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str
    service: str
    environment: str
    transport: str


class DatasetDescriptorResponse(BaseModel):
    name: str
    source: str
    source_url: str
    description: str
    anomaly_label: str
    rows: int | None = None
    features: int | None = None


class DatasetCatalogResponse(BaseModel):
    datasets: list[DatasetDescriptorResponse]


class HyperparameterSearchRequest(BaseModel):
    values: dict[str, list[Any]] = Field(default_factory=dict)


class TrainRequest(BaseModel):
    dataset_name: str
    model_family: str = Field(default="autoencoder")
    max_rows: int | None = Field(default=20_000, ge=100)
    test_size: float = Field(default=0.2, gt=0.05, lt=0.5)
    tuning_strategy: str = Field(default="stratified_kfold")
    n_splits: int = Field(default=3, ge=2, le=10)
    leave_one_out_max_samples: int = Field(default=256, ge=16, le=5_000)
    target_metric: str = Field(default="average_precision")
    artifact_name: str | None = None
    use_pca: bool = True
    search: HyperparameterSearchRequest = Field(default_factory=HyperparameterSearchRequest)


class MetricSummary(BaseModel):
    roc_auc: float
    average_precision: float
    f1: float
    precision: float
    recall: float
    balanced_accuracy: float
    matthews_corrcoef: float
    precision_at_k: float
    recall_at_k: float
    threshold: float


class CrossValidationSummary(BaseModel):
    strategy: str
    folds: int
    metric_mean: float
    metric_std: float
    best_params: dict[str, Any]


class TrainResponse(BaseModel):
    dataset_name: str
    model_family: str
    artifact_path: str
    model_version: str
    input_dim: int
    train_rows: int
    test_rows: int
    best_params: dict[str, Any]
    cross_validation: CrossValidationSummary
    metrics: MetricSummary
