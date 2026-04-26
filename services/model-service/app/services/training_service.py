"""Training service façade for the model service."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import time
from typing import Protocol

import numpy as np
import pandas as pd
from agents.common.infrastructure.sources import DefaultSourceResolver
from app.core.config import settings
from app.ml.checks import run_classification_check, run_forecasting_check, run_regression_check
from app.ml.datasets import DatasetDescriptor, TabularDataset, load_dataset, list_datasets
from app.models import (
    ClassificationCheckResponse,
    ClassificationMetrics,
    CrossValidationSummary,
    DatasetDescriptorResponse,
    ForecastingCheckResponse,
    ForecastingMetrics,
    MetricSummary,
    MLCheckRequest,
    ModelJobResponse,
    RegressionCheckResponse,
    RegressionMetrics,
    TrainRequest,
    TrainResponse,
)


class ModelJobRepository(Protocol):
    def save(self, job: ModelJobResponse) -> ModelJobResponse:
        """Persist one model job."""

    def update(self, job: ModelJobResponse) -> ModelJobResponse:
        """Update one model job."""

    def get(self, job_id: str) -> ModelJobResponse | None:
        """Load one model job."""

    def list_recent(self, limit: int = 20) -> list[ModelJobResponse]:
        """List recent model jobs."""


class InMemoryModelJobRepository:
    """Development repository for async model-job state."""

    def __init__(self) -> None:
        self._jobs: dict[str, ModelJobResponse] = {}
        self._ordered_ids: list[str] = []

    def save(self, job: ModelJobResponse) -> ModelJobResponse:
        """Persist a new job and keep insertion order for recent-job listing."""
        self._jobs[job.job_id] = job
        if job.job_id in self._ordered_ids:
            self._ordered_ids.remove(job.job_id)
        self._ordered_ids.append(job.job_id)
        return job

    def update(self, job: ModelJobResponse) -> ModelJobResponse:
        """Overwrite a job record in place and keep it discoverable."""
        self._jobs[job.job_id] = job
        if job.job_id not in self._ordered_ids:
            self._ordered_ids.append(job.job_id)
        return job

    def get(self, job_id: str) -> ModelJobResponse | None:
        """Fetch one job by id if it exists."""
        return self._jobs.get(job_id)

    def list_recent(self, limit: int = 20) -> list[ModelJobResponse]:
        """Return the newest jobs first."""
        ordered = [self._jobs[job_id] for job_id in self._ordered_ids]
        return list(reversed(ordered[-limit:]))


class ModelTrainingService:
    """Application façade for dataset loading, checks, and model-job execution.

    Params:
    - `jobs`: repository that stores async job state.
    - `source_resolver`: shared resolver used for source-backed datasets.
    - `executor`: background executor for async job submission.

    What it does:
    - Exposes synchronous training and evaluation methods.
    - Wraps those operations in queued/running/completed job semantics.
    - Bridges Dagents source adapters into the model pipeline.

    Returns:
    - The service is consumed through its public methods rather than direct data.
    """

    def __init__(
        self,
        *,
        jobs: ModelJobRepository | None = None,
        source_resolver: DefaultSourceResolver | None = None,
        executor: ThreadPoolExecutor | None = None,
    ) -> None:
        self._jobs = jobs or InMemoryModelJobRepository()
        self._source_resolver = source_resolver or DefaultSourceResolver()
        self._executor = executor or ThreadPoolExecutor(max_workers=2, thread_name_prefix="dagents-models")

    def list_datasets(self) -> list[DatasetDescriptorResponse]:
        """List bundled benchmark datasets without forcing materialization.

        Params:
        - None.

        What it does:
        - Returns the static registry metadata for each bundled dataset.
        - Avoids downloading or materializing datasets in request/health paths.

        Returns:
        - `list[DatasetDescriptorResponse]`.
        """
        return [
            DatasetDescriptorResponse(
                name=descriptor.name,
                source=descriptor.source,
                source_url=descriptor.source_url,
                description=descriptor.description,
                anomaly_label=descriptor.anomaly_label,
            )
            for descriptor in list_datasets()
        ]

    def train(self, request: TrainRequest) -> TrainResponse:
        """Run the full synchronous anomaly-training pipeline.

        Params:
        - `request`: training configuration, including dataset selection,
          model family, tuning strategy, and artifact naming.

        What it does:
        - Resolves the dataset from either a benchmark name or Dagents source input.
        - Builds the unified anomaly training pipeline.
        - Trains the requested model family and persists the artifact.

        Returns:
        - `TrainResponse` with artifact metadata and evaluation summaries.
        """
        from app.ml.pipeline import PipelineConfig, UnifiedAnomalyTrainingPipeline

        dataset = self._load_dataset(request)
        dataset_name = request.dataset_name or "source-backed"
        pipeline = UnifiedAnomalyTrainingPipeline(
            PipelineConfig(
                dataset_name=dataset_name,
                model_family=request.model_family,
                test_size=request.test_size,
                tuning_strategy=request.tuning_strategy,
                n_splits=request.n_splits,
                leave_one_out_max_samples=request.leave_one_out_max_samples,
                target_metric=request.target_metric,
                random_seed=settings.random_seed,
                device=settings.model_device,
            )
        )

        artifact_name = request.artifact_name or f"{dataset_name}-{request.model_family}.pt"
        result = pipeline.train(
            dataset.features,
            dataset.labels,
            search_space=request.search.values,
            artifact_path=Path(settings.model_artifact_dir) / artifact_name,
            use_pca=request.use_pca,
        )

        return TrainResponse(
            dataset_name=dataset_name,
            model_family=request.model_family,
            artifact_path=result.artifact_path,
            model_version=result.model_version,
            input_dim=result.input_dim,
            train_rows=result.train_rows,
            test_rows=result.test_rows,
            best_params=result.best_params,
            cross_validation=CrossValidationSummary(
                strategy=result.cross_validation.strategy,
                folds=result.cross_validation.folds,
                metric_mean=result.cross_validation.metric_mean,
                metric_std=result.cross_validation.metric_std,
                best_params=result.cross_validation.best_params,
            ),
            metrics=MetricSummary(
                roc_auc=result.metrics.roc_auc,
                average_precision=result.metrics.average_precision,
                f1=result.metrics.f1,
                precision=result.metrics.precision,
                recall=result.metrics.recall,
                balanced_accuracy=result.metrics.balanced_accuracy,
                matthews_corrcoef=result.metrics.matthews_corrcoef,
                precision_at_k=result.metrics.precision_at_k,
                recall_at_k=result.metrics.recall_at_k,
                threshold=result.metrics.threshold,
            ),
        )

    def classification_check(self, request: MLCheckRequest) -> ClassificationCheckResponse:
        """Evaluate a classification model family against a resolved dataset."""
        features, labels, feature_fields, dataset_name = self._load_check_dataset(request, target_dtype="int64")
        result = run_classification_check(
            features.to_numpy(dtype=np.float32),
            labels.astype(np.int64),
            model_family=request.model_family,
            test_size=request.test_size,
            random_seed=settings.random_seed,
        )
        return ClassificationCheckResponse(
            dataset_name=dataset_name,
            model_family=request.model_family,
            feature_fields=feature_fields,
            label_field=request.label_field,
            train_rows=result.train_rows,
            test_rows=result.test_rows,
            metrics=ClassificationMetrics(**result.metrics),
        )

    def regression_check(self, request: MLCheckRequest) -> RegressionCheckResponse:
        """Evaluate a regression model family against a resolved dataset."""
        features, labels, feature_fields, dataset_name = self._load_check_dataset(request, target_dtype="float64")
        result = run_regression_check(
            features.to_numpy(dtype=np.float32),
            labels.astype(np.float64),
            model_family=request.model_family,
            test_size=request.test_size,
            random_seed=settings.random_seed,
        )
        return RegressionCheckResponse(
            dataset_name=dataset_name,
            model_family=request.model_family,
            feature_fields=feature_fields,
            label_field=request.label_field,
            train_rows=result.train_rows,
            test_rows=result.test_rows,
            metrics=RegressionMetrics(**result.metrics),
        )

    def forecasting_check(self, request: MLCheckRequest) -> ForecastingCheckResponse:
        """Evaluate an LSTM/GRU-style forecasting model against ordered data."""
        features, labels, feature_fields, dataset_name = self._load_check_dataset(request, target_dtype="float64")
        result = run_forecasting_check(
            features.to_numpy(dtype=np.float32),
            labels.astype(np.float64),
            model_family=request.model_family,
            test_size=request.test_size,
            random_seed=settings.random_seed,
            sequence_length=request.sequence_length,
        )
        return ForecastingCheckResponse(
            dataset_name=dataset_name,
            model_family=request.model_family,
            feature_fields=feature_fields,
            label_field=request.label_field,
            sequence_length=request.sequence_length,
            train_rows=result.train_rows,
            test_rows=result.test_rows,
            metrics=ForecastingMetrics(**result.metrics),
        )

    def submit_job(self, request: TrainRequest) -> ModelJobResponse:
        """Queue a background training job and return its initial status.

        Params:
        - `request`: the same training payload accepted by `train`.

        What it does:
        - Creates a queued job record.
        - Schedules `_run_async` on the thread pool.

        Returns:
        - Initial `ModelJobResponse` with status `queued`.
        """
        job_id = f"model-job-{int(time.time() * 1000)}"
        queued = ModelJobResponse(
            job_id=job_id,
            status="queued",
            submitted_at=int(time.time()),
        )
        self._jobs.save(queued)
        self._executor.submit(self._run_async, job_id, request)
        return queued

    def _run_async(self, job_id: str, request: TrainRequest) -> None:
        """Drive the queued job through running/completed/failed transitions."""
        current = self._jobs.get(job_id)
        if current is None:
            return
        self._jobs.update(current.model_copy(update={"status": "running", "started_at": int(time.time())}))
        try:
            # Reuse the synchronous training implementation so the async path and
            # direct API path stay behaviorally identical.
            result = self.train(request)
            current = self._jobs.get(job_id)
            if current is None:
                return
            self._jobs.update(
                current.model_copy(
                    update={
                        "status": "completed",
                        "completed_at": int(time.time()),
                        "result": result,
                    }
                )
            )
        except Exception as exc:  # pragma: no cover - defensive path
            current = self._jobs.get(job_id)
            if current is None:
                return
            self._jobs.update(
                current.model_copy(
                    update={
                        "status": "failed",
                        "completed_at": int(time.time()),
                        "error": str(exc),
                    }
                )
            )

    def get_job(self, job_id: str) -> ModelJobResponse | None:
        """Return one stored model job by id."""
        return self._jobs.get(job_id)

    def list_jobs(self, limit: int = 20) -> list[ModelJobResponse]:
        """List recent model jobs, newest first."""
        return self._jobs.list_recent(limit=limit)

    def register_source(self, source):
        """Store a reusable source definition for later training or checks."""
        return self._source_resolver.register(source)

    def get_source(self, source_id: str):
        """Fetch one registered source definition by id."""
        return self._source_resolver.get(source_id)

    def list_sources(self):
        """List all registered source definitions."""
        return self._source_resolver.list()

    def validate_source(self, source_id: str):
        """Validate one stored source definition through the shared resolver."""
        return self._source_resolver.validate(source_id)

    def _load_dataset(self, request: TrainRequest) -> TabularDataset:
        """Resolve a `TrainRequest` into a `TabularDataset`.

        Params:
        - `request`: training request that may reference a benchmark dataset or a
          Dagents `DatasetInput`.

        What it does:
        - Uses benchmark loaders when `dataset_name` is provided.
        - Otherwise materializes source-backed records through Dagents adapters.
        - Builds numeric feature matrices and labels expected by the training pipeline.

        Returns:
        - `TabularDataset`.
        """
        if request.dataset_name:
            return load_dataset(request.dataset_name, max_rows=request.max_rows, random_seed=settings.random_seed)

        if request.dataset is None:
            raise ValueError("dataset input is required")
        records = self._resolve_source_records(request.dataset, request.max_rows)
        if not records:
            raise ValueError("Dataset input resolved to no records")
        label_field = request.label_field
        feature_fields = request.feature_fields or [field for field in records[0] if field != label_field]
        frame = pd.DataFrame(records)
        # Non-numeric values are coerced and filled so upstream heterogeneous
        # sources do not explode the training pipeline.
        features = frame[feature_fields].apply(pd.to_numeric, errors="coerce").fillna(0.0).astype("float32")
        if label_field and label_field in frame:
            labels = frame[label_field].fillna(0).astype(int).to_numpy(dtype=np.int64)
        else:
            labels = np.zeros(len(features), dtype=np.int64)
        descriptor = DatasetDescriptor(
            name="source-backed",
            source="dagents",
            source_url="inline://dataset-input",
            description="Dataset resolved from shared Dagents source adapters.",
            anomaly_label="1",
        )
        return TabularDataset(descriptor=descriptor, features=features.reset_index(drop=True), labels=labels)

    def _load_check_dataset(
        self,
        request: MLCheckRequest,
        *,
        target_dtype: str,
    ) -> tuple[pd.DataFrame, np.ndarray, list[str], str]:
        """Resolve a check request into numeric features and labels.

        Params:
        - `request`: classification/regression/forecasting check payload.
        - `target_dtype`: numeric dtype expected by the downstream evaluator.

        What it does:
        - Loads benchmark data or source-backed records.
        - Validates that the target label field exists.
        - Produces numeric features and label arrays for metric computation.

        Returns:
        - Tuple of `(features, labels, feature_fields, dataset_name)`.
        """
        if request.dataset_name:
            dataset = load_dataset(request.dataset_name, max_rows=request.max_rows, random_seed=settings.random_seed)
            feature_fields = request.feature_fields or list(dataset.features.columns)
            if not feature_fields:
                raise ValueError("feature_fields are required for dataset_name-backed checks")
            return dataset.features[feature_fields], dataset.labels.astype(target_dtype), feature_fields, request.dataset_name

        if request.dataset is None:
            raise ValueError("dataset input is required")
        records = self._resolve_source_records(request.dataset, request.max_rows)
        if not records:
            raise ValueError("Dataset input resolved to no records")
        feature_fields = request.feature_fields or [field for field in records[0] if field != request.label_field]
        frame = pd.DataFrame(records)
        features = frame[feature_fields].apply(pd.to_numeric, errors="coerce").fillna(0.0).astype("float32")
        if request.label_field not in frame:
            raise ValueError(f"Label field {request.label_field} was not found in dataset")
        labels = frame[request.label_field].apply(pd.to_numeric, errors="coerce").fillna(0.0).astype(target_dtype).to_numpy()
        return features.reset_index(drop=True), labels, feature_fields, "source-backed"

    def _resolve_source_records(self, dataset_input, max_rows: int | None) -> list[dict[str, object]]:
        """Materialize all records referenced by a Dagents `DatasetInput`.

        Params:
        - `dataset_input`: inline records, embedded source spec, or stored source id.
        - `max_rows`: optional cap applied after batch materialization.

        What it does:
        - Iterates through all `RecordBatch` objects returned by the resolver.
        - Flattens them into one record list for the current model-service implementation.

        Returns:
        - `list[dict[str, object]]` records.
        """
        records: list[dict[str, object]] = []
        for batch in self._source_resolver.materialize(dataset_input):
            records.extend(batch.records)
        if max_rows is not None:
            records = records[:max_rows]
        return records


training_service = ModelTrainingService()
