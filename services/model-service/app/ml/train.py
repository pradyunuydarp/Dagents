"""CLI entry point for training benchmark anomaly models."""

from __future__ import annotations

from pathlib import Path

from app.core.config import settings
from app.ml.datasets import load_dataset
from app.ml.pipeline import PipelineConfig, UnifiedAnomalyTrainingPipeline


def main() -> None:
    training_plan = [
        ("kddcup99_http", "autoencoder", 20_000, "kddcup99-http-autoencoder.pt"),
        ("creditcard_openml", "autoencoder", 40_000, "creditcard-autoencoder.pt"),
        ("mammography_openml", "variational_autoencoder", None, "mammography-vae.pt"),
    ]

    for dataset_name, model_family, max_rows, artifact_name in training_plan:
        dataset = load_dataset(dataset_name, max_rows=max_rows, random_seed=settings.random_seed)
        pipeline = UnifiedAnomalyTrainingPipeline(
            PipelineConfig(
                dataset_name=dataset_name,
                model_family=model_family,
                test_size=0.2,
                tuning_strategy="stratified_kfold",
                n_splits=3,
                leave_one_out_max_samples=128,
                target_metric="average_precision",
                random_seed=settings.random_seed,
                device=settings.model_device,
            )
        )
        result = pipeline.train(
            dataset.features,
            dataset.labels,
            search_space={},
            artifact_path=Path(settings.model_artifact_dir) / artifact_name,
            use_pca=True,
        )
        print(
            "trained",
            dataset_name,
            model_family,
            result.metrics.average_precision,
            result.artifact_path,
        )


if __name__ == "__main__":
    main()
