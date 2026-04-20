"""Pydantic API models for the model service."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agents.common.domain.models import DatasetInput


class AppModel(BaseModel):
    model_config = ConfigDict(protected_namespaces=())


class HealthResponse(AppModel):
    status: str
    service: str
    environment: str
    transport: str


class DatasetDescriptorResponse(AppModel):
    name: str
    source: str
    source_url: str
    description: str
    anomaly_label: str
    rows: int | None = None
    features: int | None = None


class DatasetCatalogResponse(AppModel):
    datasets: list[DatasetDescriptorResponse]


class HyperparameterSearchRequest(AppModel):
    values: dict[str, list[Any]] = Field(default_factory=dict)


class TrainRequest(AppModel):
    dataset_name: str | None = None
    dataset: DatasetInput | None = None
    feature_fields: list[str] = Field(default_factory=list)
    label_field: str | None = None
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

    @model_validator(mode="after")
    def validate_source(self) -> "TrainRequest":
        if not self.dataset_name and self.dataset is None:
            raise ValueError("TrainRequest requires dataset_name or dataset")
        return self


class MLCheckRequest(AppModel):
    dataset_name: str | None = None
    dataset: DatasetInput | None = None
    feature_fields: list[str] = Field(default_factory=list)
    label_field: str
    model_family: str = Field(default="random_forest")
    max_rows: int | None = Field(default=20_000, ge=100)
    test_size: float = Field(default=0.2, gt=0.05, lt=0.5)
    sequence_length: int = Field(default=12, ge=2, le=256)

    @model_validator(mode="after")
    def validate_source(self) -> "MLCheckRequest":
        if not self.dataset_name and self.dataset is None:
            raise ValueError("MLCheckRequest requires dataset_name or dataset")
        return self


class MetricSummary(AppModel):
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


class CrossValidationSummary(AppModel):
    strategy: str
    folds: int
    metric_mean: float
    metric_std: float
    best_params: dict[str, Any]


class TrainResponse(AppModel):
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


class ModelJobResponse(AppModel):
    job_id: str
    status: str
    submitted_at: int
    started_at: int | None = None
    completed_at: int | None = None
    result: TrainResponse | None = None
    error: str | None = None


class ModelJobCatalogResponse(AppModel):
    jobs: list[ModelJobResponse]


class ClassificationMetrics(AppModel):
    accuracy: float
    precision: float
    recall: float
    f1: float
    balanced_accuracy: float
    matthews_corrcoef: float
    roc_auc: float | None = None
    average_precision: float | None = None


class RegressionMetrics(AppModel):
    mae: float
    mse: float
    rmse: float
    r2: float


class ClassificationCheckResponse(AppModel):
    dataset_name: str
    model_family: str
    feature_fields: list[str]
    label_field: str
    train_rows: int
    test_rows: int
    metrics: ClassificationMetrics


class RegressionCheckResponse(AppModel):
    dataset_name: str
    model_family: str
    feature_fields: list[str]
    label_field: str
    train_rows: int
    test_rows: int
    metrics: RegressionMetrics


class ForecastingMetrics(AppModel):
    mae: float
    mse: float
    rmse: float
    r2: float


class ForecastingCheckResponse(AppModel):
    dataset_name: str
    model_family: str
    feature_fields: list[str]
    label_field: str
    sequence_length: int
    train_rows: int
    test_rows: int
    metrics: ForecastingMetrics
