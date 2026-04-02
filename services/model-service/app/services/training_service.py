"""Training service façade for the model service."""

from __future__ import annotations

from pathlib import Path

from app.core.config import settings
from app.ml.datasets import load_dataset, list_datasets
from app.ml.pipeline import PipelineConfig, UnifiedAnomalyTrainingPipeline
from app.models import CrossValidationSummary, DatasetDescriptorResponse, MetricSummary, TrainRequest, TrainResponse


class ModelTrainingService:
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
        dataset = load_dataset(request.dataset_name, max_rows=request.max_rows, random_seed=settings.random_seed)
        pipeline = UnifiedAnomalyTrainingPipeline(
            PipelineConfig(
                dataset_name=request.dataset_name,
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

        artifact_name = request.artifact_name or f"{request.dataset_name}-{request.model_family}.pt"
        result = pipeline.train(
            dataset.features,
            dataset.labels,
            search_space=request.search.values,
            artifact_path=Path(settings.model_artifact_dir) / artifact_name,
            use_pca=request.use_pca,
        )

        return TrainResponse(
            dataset_name=request.dataset_name,
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


training_service = ModelTrainingService()
