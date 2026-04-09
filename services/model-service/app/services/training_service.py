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
from app.ml.datasets import DatasetDescriptor, TabularDataset, load_dataset, list_datasets
from app.models import (
    CrossValidationSummary,
    DatasetDescriptorResponse,
    MetricSummary,
    ModelJobResponse,
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
    def __init__(self) -> None:
        self._jobs: dict[str, ModelJobResponse] = {}
        self._ordered_ids: list[str] = []

    def save(self, job: ModelJobResponse) -> ModelJobResponse:
        self._jobs[job.job_id] = job
        if job.job_id in self._ordered_ids:
            self._ordered_ids.remove(job.job_id)
        self._ordered_ids.append(job.job_id)
        return job

    def update(self, job: ModelJobResponse) -> ModelJobResponse:
        self._jobs[job.job_id] = job
        if job.job_id not in self._ordered_ids:
            self._ordered_ids.append(job.job_id)
        return job

    def get(self, job_id: str) -> ModelJobResponse | None:
        return self._jobs.get(job_id)

    def list_recent(self, limit: int = 20) -> list[ModelJobResponse]:
        ordered = [self._jobs[job_id] for job_id in self._ordered_ids]
        return list(reversed(ordered[-limit:]))


class ModelTrainingService:
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
        catalog = []
        for descriptor in list_datasets():
            dataset = load_dataset(descriptor.name, max_rows=256, random_seed=settings.random_seed)
            catalog.append(
                DatasetDescriptorResponse(
                    name=descriptor.name,
                    source=descriptor.source,
                    source_url=descriptor.source_url,
                    description=descriptor.description,
                    anomaly_label=descriptor.anomaly_label,
                    rows=len(dataset.features),
                    features=dataset.features.shape[1],
                )
            )
        return catalog

    def train(self, request: TrainRequest) -> TrainResponse:
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

    def submit_job(self, request: TrainRequest) -> ModelJobResponse:
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
        current = self._jobs.get(job_id)
        if current is None:
            return
        self._jobs.update(current.model_copy(update={"status": "running", "started_at": int(time.time())}))
        try:
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
        return self._jobs.get(job_id)

    def list_jobs(self, limit: int = 20) -> list[ModelJobResponse]:
        return self._jobs.list_recent(limit=limit)

    def register_source(self, source):
        return self._source_resolver.register(source)

    def get_source(self, source_id: str):
        return self._source_resolver.get(source_id)

    def list_sources(self):
        return self._source_resolver.list()

    def validate_source(self, source_id: str):
        return self._source_resolver.validate(source_id)

    def _load_dataset(self, request: TrainRequest) -> TabularDataset:
        if request.dataset_name:
            return load_dataset(request.dataset_name, max_rows=request.max_rows, random_seed=settings.random_seed)

        dataset_input = request.dataset
        if dataset_input is None:
            raise ValueError("dataset input is required")
        records: list[dict[str, object]] = []
        for batch in self._source_resolver.materialize(dataset_input):
            records.extend(batch.records)
        if request.max_rows is not None:
            records = records[: request.max_rows]
        if not records:
            raise ValueError("Dataset input resolved to no records")
        label_field = request.label_field
        feature_fields = request.feature_fields or [field for field in records[0] if field != label_field]
        frame = pd.DataFrame(records)
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


training_service = ModelTrainingService()
