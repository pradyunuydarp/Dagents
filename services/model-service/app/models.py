"""Pydantic API models for the model service.

The field descriptions appear in the service's OpenAPI document (`/docs`) and
in the API reference on the framework site, which is generated from it.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from dagents.common.domain.models import DatasetInput


class AppModel(BaseModel):
    model_config = ConfigDict(protected_namespaces=())


class HealthResponse(AppModel):
    status: str = Field(description="Always `ok` when the service answers.")
    service: str = Field(description="The service name, from `APP_NAME`. The default is `dagents-model-service`.")
    environment: str = Field(description="The deployment environment, from `APP_ENV`. The default is `development`.")
    transport: str = Field(description="How the service is reached. Always `http`.")


class ErrorResponse(AppModel):
    detail: str = Field(description="What went wrong, in one sentence.")


class UnsupportedModelErrorResponse(AppModel):
    """The body of a `400`: the model family cannot run for this task here."""

    detail: str = Field(
        description="Why the family cannot run, and which families can. For a `planned` family, what it would need."
    )
    model_family: str = Field(description="The family the request asked for.")
    task: str = Field(description="The task the endpoint runs, such as `classification` or `anomaly_detection`.")


class DatasetDescriptorResponse(AppModel):
    """One built-in benchmark dataset."""

    name: str = Field(description="The name to send as `dataset_name`.")
    source: str = Field(description="Where the data is downloaded from: `scikit-learn` or `OpenML`.")
    source_url: str = Field(description="A page that describes the dataset.")
    description: str = Field(description="What the data is.")
    anomaly_label: str = Field(description="The value of the original label that marks an anomaly.")
    rows: int | None = Field(
        default=None, description="Always null in this list: the service does not download the data to count it."
    )
    features: int | None = Field(default=None, description="Always null in this list, for the same reason.")


class DatasetCatalogResponse(AppModel):
    datasets: list[DatasetDescriptorResponse] = Field(description="The three benchmark datasets, sorted by name.")


class HyperparameterSearchRequest(AppModel):
    """Which hyperparameter values a training run tries."""

    values: dict[str, list[Any]] = Field(
        default_factory=dict,
        description=(
            "A list of values to try for each hyperparameter you name, such as `{\"epochs\": [10]}`. Names you "
            "leave out keep the service's default lists. Every combination is trained and scored, so the default "
            "search trains 8 candidates."
        ),
    )


class TrainRequest(AppModel):
    """A request to train an anomaly model. Set `dataset_name` or `dataset`."""

    dataset_name: str | None = Field(
        default=None,
        description=(
            "A built-in benchmark from `GET /api/v1/datasets`. The service downloads it on first use, so it needs "
            "network access. Takes precedence over `dataset`."
        ),
    )
    dataset: DatasetInput | None = Field(
        default=None, description="Records to train on: sent inline, a source definition, or a registered `source_id`."
    )
    feature_fields: list[str] = Field(
        default_factory=list,
        description=(
            "The fields to train on, with `dataset`. Empty means every field of the first record except "
            "`label_field`. Values that are not numbers become 0."
        ),
    )
    label_field: str | None = Field(
        default=None,
        description=(
            "The field that marks an anomaly with 1 and a normal record with 0, with `dataset`. The model trains on "
            "normal records only; the labels choose the threshold and score the model. Empty means every record "
            "is normal."
        ),
    )
    model_family: str = Field(
        default="autoencoder",
        description=(
            "An anomaly-detection family that `GET /api/v1/model-families` lists as implemented: `autoencoder` or "
            "`variational_autoencoder`."
        ),
    )
    max_rows: int | None = Field(
        default=20_000,
        ge=100,
        description=(
            "The most records to use. A benchmark is sampled down to it, keeping the share of anomalies; records "
            "from `dataset` are cut after the first `max_rows`."
        ),
    )
    test_size: float = Field(
        default=0.2,
        gt=0.05,
        lt=0.5,
        description="The share of records held out to score the final model, keeping the share of anomalies.",
    )
    tuning_strategy: str = Field(
        default="stratified_kfold",
        description=(
            "How each candidate is scored on the remaining records: `stratified_kfold` (`n_splits` folds), "
            "`holdout` (one 80/20 split) or `leave_one_out`."
        ),
    )
    n_splits: int = Field(default=3, ge=2, le=10, description="The number of folds for `stratified_kfold`.")
    leave_one_out_max_samples: int = Field(
        default=256,
        ge=16,
        le=5_000,
        description="For `leave_one_out`, the most records to use. A larger set is sampled down to this.",
    )
    target_metric: str = Field(
        default="average_precision",
        description=(
            "The metric that picks the best candidate: a field of the metrics object, such as `average_precision`, "
            "`roc_auc` or `f1`."
        ),
    )
    artifact_name: str | None = Field(
        default=None,
        description=(
            "The file name of the saved model, inside `MODEL_ARTIFACT_DIR`. Empty means "
            "`<dataset_name>-<model_family>.pt`, where `dataset_name` is `source-backed` for records from "
            "`dataset`. An existing file with that name is replaced."
        ),
    )
    use_pca: bool = Field(
        default=True,
        description=(
            "Allow candidates that reduce the features with PCA first. The default search tries one candidate "
            "set without PCA and one that keeps 95% of the variance."
        ),
    )
    search: HyperparameterSearchRequest = Field(
        default_factory=HyperparameterSearchRequest, description="The hyperparameter values to try."
    )

    @model_validator(mode="after")
    def validate_source(self) -> "TrainRequest":
        if not self.dataset_name and self.dataset is None:
            raise ValueError("TrainRequest requires dataset_name or dataset")
        return self


class MLCheckRequest(AppModel):
    """A request to fit and score one model once. Set `dataset_name` or `dataset`."""

    dataset_name: str | None = Field(
        default=None,
        description=(
            "A built-in benchmark from `GET /api/v1/datasets`. The service downloads it on first use, so it needs "
            "network access. Takes precedence over `dataset`."
        ),
    )
    dataset: DatasetInput | None = Field(
        default=None, description="Records to check on: sent inline, a source definition, or a registered `source_id`."
    )
    feature_fields: list[str] = Field(
        default_factory=list,
        description=(
            "The input fields. Empty means every field of the first record except `label_field`. Values that are "
            "not numbers become 0."
        ),
    )
    label_field: str = Field(
        description=(
            "The field to predict. For classification, class numbers such as 0 and 1; for regression and "
            "forecasting, a number. Values that are not numbers become 0."
        )
    )
    model_family: str = Field(
        default="random_forest",
        description=(
            "A family that `GET /api/v1/model-families` lists as implemented for the endpoint's task. "
            "Classification: `random_forest`, `naive_bayes`, `linear`. Regression: `random_forest`, `linear`. "
            "Forecasting: `gru`, `lstm`, so the forecasting endpoint needs this field set."
        ),
    )
    max_rows: int | None = Field(
        default=20_000,
        ge=100,
        description=(
            "The most records to use. A benchmark is sampled down to it; records from `dataset` past this number "
            "are left out."
        ),
    )
    test_size: float = Field(
        default=0.2,
        gt=0.05,
        lt=0.5,
        description=(
            "The share of records held out for scoring. Classification keeps the share of each class, regression "
            "picks them at random, and forecasting holds out the last records."
        ),
    )
    sequence_length: int = Field(
        default=12,
        ge=2,
        le=256,
        description="Forecasting only: how many earlier records each prediction looks at. Other checks ignore it.",
    )

    @model_validator(mode="after")
    def validate_source(self) -> "MLCheckRequest":
        if not self.dataset_name and self.dataset is None:
            raise ValueError("MLCheckRequest requires dataset_name or dataset")
        return self


class MetricSummary(AppModel):
    """How well an anomaly model separated anomalies from normal records on the held-out records."""

    roc_auc: float = Field(
        description="Area under the ROC curve of the anomaly scores. 1.0 ranks every anomaly above every normal record."
    )
    average_precision: float = Field(description="Average precision of the anomaly scores.")
    f1: float = Field(description="F1 score of the predictions made with `threshold`.")
    precision: float = Field(description="Share of records flagged as anomalies that are anomalies.")
    recall: float = Field(description="Share of anomalies that were flagged.")
    balanced_accuracy: float = Field(description="The mean of recall on anomalies and recall on normal records.")
    matthews_corrcoef: float = Field(description="Matthews correlation of the predictions, from -1 to 1.")
    precision_at_k: float = Field(
        description="Precision among the k highest scores, where k is the number of anomalies in the held-out records."
    )
    recall_at_k: float = Field(description="Recall among the same k highest scores.")
    threshold: float = Field(
        description=(
            "The anomaly score at or above which a record is flagged: the mean of the thresholds chosen during "
            "tuning."
        )
    )


class CrossValidationSummary(AppModel):
    """How the best candidate scored during tuning."""

    strategy: str = Field(description="The `tuning_strategy` from the request.")
    folds: int = Field(description="The number of scores averaged. `leave_one_out` gives one pooled score.")
    metric_mean: float = Field(description="The mean of `target_metric` over the folds.")
    metric_std: float = Field(description="Its standard deviation over the folds.")
    best_params: dict[str, Any] = Field(description="The winning hyperparameters. The same as the response's `best_params`.")


class TrainResponse(AppModel):
    """A trained and saved anomaly model."""

    dataset_name: str = Field(description="The benchmark name, or `source-backed` for records from `dataset`.")
    model_family: str = Field(description="The family that was trained.")
    artifact_path: str = Field(
        description="Where the model was saved, as `MODEL_ARTIFACT_DIR/<artifact_name>`. It holds the weights, "
        "the scaling and PCA settings, the threshold and the metrics."
    )
    model_version: str = Field(description="`<dataset_name>-<model_family>-v1`. Always ends in `v1`.")
    input_dim: int = Field(description="The number of model inputs after scaling and any PCA.")
    train_rows: int = Field(description="Records used for tuning and for the final fit. The model fits only the normal ones.")
    test_rows: int = Field(description="Records held out to compute `metrics`.")
    best_params: dict[str, Any] = Field(description="The hyperparameters of the candidate that won.")
    cross_validation: CrossValidationSummary = Field(description="How the winner scored during tuning.")
    metrics: MetricSummary = Field(description="How the final model scored on the held-out records.")


class ModelJobResponse(AppModel):
    """A training job that runs in the background."""

    job_id: str = Field(
        description="`model-job-<milliseconds since 1970>`, made from the clock when the job is submitted."
    )
    status: str = Field(description="`queued`, then `running`, then `completed` or `failed`.")
    submitted_at: int = Field(description="When the job was submitted, in seconds since 1970.")
    started_at: int | None = Field(default=None, description="When training started. Null while queued.")
    completed_at: int | None = Field(default=None, description="When training ended. Null until then.")
    result: TrainResponse | None = Field(
        default=None, description="What `POST /api/v1/train` would have returned. Null until the job completes."
    )
    error: str | None = Field(default=None, description="Why the job failed. Null otherwise.")


class ModelJobCatalogResponse(AppModel):
    jobs: list[ModelJobResponse] = Field(description="The stored jobs, newest first.")


class ClassificationMetrics(AppModel):
    """Scores of a classifier on the held-out records. Precision, recall and F1 treat class 1 as positive."""

    accuracy: float = Field(description="Share of held-out records predicted correctly.")
    precision: float = Field(description="Share of records predicted as class 1 that are class 1.")
    recall: float = Field(description="Share of class 1 records predicted as class 1.")
    f1: float = Field(description="The harmonic mean of precision and recall.")
    balanced_accuracy: float = Field(description="The mean recall over the classes.")
    matthews_corrcoef: float = Field(description="Matthews correlation of the predictions, from -1 to 1.")
    roc_auc: float | None = Field(
        default=None, description="Area under the ROC curve for class 1. Null when the model gives no scores."
    )
    average_precision: float | None = Field(
        default=None, description="Average precision for class 1. Null when the model gives no scores."
    )


class RegressionMetrics(AppModel):
    """Errors of a regressor on the held-out records."""

    mae: float = Field(description="Mean absolute error, in the label's units.")
    mse: float = Field(description="Mean squared error.")
    rmse: float = Field(description="Square root of `mse`, in the label's units.")
    r2: float = Field(description="Coefficient of determination. 1.0 is a perfect fit; below 0 is worse than the mean.")


class ClassificationCheckResponse(AppModel):
    """The result of one classification check."""

    dataset_name: str = Field(description="The benchmark name, or `source-backed` for records from `dataset`.")
    model_family: str = Field(description="The family that was fitted.")
    feature_fields: list[str] = Field(description="The input fields that were used.")
    label_field: str = Field(description="The field that was predicted.")
    train_rows: int = Field(description="Records the model was fitted on.")
    test_rows: int = Field(description="Records held out to compute `metrics`.")
    metrics: ClassificationMetrics = Field(description="Scores on the held-out records.")


class RegressionCheckResponse(AppModel):
    """The result of one regression check."""

    dataset_name: str = Field(description="The benchmark name, or `source-backed` for records from `dataset`.")
    model_family: str = Field(description="The family that was fitted.")
    feature_fields: list[str] = Field(description="The input fields that were used.")
    label_field: str = Field(description="The field that was predicted.")
    train_rows: int = Field(description="Records the model was fitted on.")
    test_rows: int = Field(description="Records held out to compute `metrics`.")
    metrics: RegressionMetrics = Field(description="Errors on the held-out records.")


class ForecastingMetrics(AppModel):
    """Errors of a forecasting model on the held-out sequences."""

    mae: float = Field(description="Mean absolute error, in the label's units.")
    mse: float = Field(description="Mean squared error.")
    rmse: float = Field(description="Square root of `mse`, in the label's units.")
    r2: float = Field(description="Coefficient of determination. 1.0 is a perfect fit; below 0 is worse than the mean.")


class ForecastingCheckResponse(AppModel):
    """The result of one forecasting check."""

    dataset_name: str = Field(description="The benchmark name, or `source-backed` for records from `dataset`.")
    model_family: str = Field(description="The recurrent family that was trained: `gru` or `lstm`.")
    feature_fields: list[str] = Field(description="The input fields that were used.")
    label_field: str = Field(description="The field that was predicted.")
    sequence_length: int = Field(description="How many earlier records each prediction looked at.")
    train_rows: int = Field(description="Training sequences. There is one sequence per record after the first `sequence_length`.")
    test_rows: int = Field(description="Held-out sequences: the last ones in record order.")
    metrics: ForecastingMetrics = Field(description="Errors on the held-out sequences.")


class ModelCapabilityResponse(AppModel):
    """One `(family, task)` entry from the model inventory."""

    family: str = Field(description="The model family, in the planner's words, such as `random_forest`.")
    task: str = Field(description="The task: `anomaly_detection`, `classification`, `embedding`, `forecasting` or `regression`.")
    provider: str = Field(
        description="What runs it: `pytorch`, `scikit_learn`, `huggingface`, or `extension` (an application supplies it)."
    )
    status: str = Field(description="`implemented` when this service can run it, `planned` when it cannot yet.")
    entrypoint: str | None = Field(
        default=None, description="The `module:attribute` that builds the model. Null for a planned entry."
    )
    default_checkpoint: str | None = Field(
        default=None, description="For a Hugging Face model, the checkpoint it loads. Null otherwise."
    )
    extra_requirements: list[str] = Field(
        default_factory=list, description="Python packages it needs beyond the service's own requirements."
    )
    requires_download: bool = Field(
        default=False,
        description="Whether running it means fetching weights first. Downloads are off unless `DAGENTS_ALLOW_MODEL_DOWNLOAD=1`.",
    )
    notes: str = Field(default="", description="Why the entry exists, or what implementing it would take.")


class ModelInventoryResponse(AppModel):
    """What this deployment can run, so a consumer need not hardcode a list.

    `implemented` maps each task to the families that work here today. `gaps`
    lists the families the OCaml router may choose that this service cannot run
    yet, with what each would need.
    """

    tasks: list[str] = Field(description="Every task name, sorted.")
    providers: list[str] = Field(description="Every provider name.")
    families: list[str] = Field(description="Every family the inventory names, implemented or planned, sorted.")
    implemented: dict[str, list[str]] = Field(
        description="For each task, the families this service can run for it."
    )
    capabilities: list[ModelCapabilityResponse] = Field(
        description="Every `(family, task)` entry, sorted by family and then task."
    )
    gaps: list[ModelCapabilityResponse] = Field(
        description="The `planned` entries: families the planner may choose that this service cannot run yet."
    )
