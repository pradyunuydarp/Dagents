"""Smoke tests for the unified anomaly training pipeline."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import numpy as np
import pandas as pd

TORCH_AVAILABLE = importlib.util.find_spec("torch") is not None

if TORCH_AVAILABLE:
    from app.ml.pipeline import PipelineConfig, UnifiedAnomalyTrainingPipeline


class UnifiedAnomalyTrainingPipelineTests(unittest.TestCase):
    @unittest.skipUnless(TORCH_AVAILABLE, "torch is required for model-service training smoke tests")
    def test_pipeline_trains_and_persists_artifact(self) -> None:
        rng = np.random.default_rng(42)
        normal = rng.normal(0, 1, size=(200, 6))
        anomalies = rng.normal(4, 1, size=(20, 6))
        features = pd.DataFrame(np.vstack([normal, anomalies]), columns=[f"f{i}" for i in range(6)])
        labels = np.asarray([0] * len(normal) + [1] * len(anomalies))

        pipeline = UnifiedAnomalyTrainingPipeline(
            PipelineConfig(
                dataset_name="synthetic",
                model_family="autoencoder",
                test_size=0.2,
                tuning_strategy="stratified_kfold",
                n_splits=3,
                leave_one_out_max_samples=64,
                target_metric="average_precision",
                random_seed=42,
                device="cpu",
            )
        )

        with TemporaryDirectory() as temp_dir:
            artifact_path = Path(temp_dir) / "synthetic-autoencoder.pt"
            result = pipeline.train(
                features,
                labels,
                search_space={
                    "hidden_dims": [[16, 8]],
                    "latent_dim": [4],
                    "dropout": [0.0],
                    "learning_rate": [0.001],
                    "weight_decay": [0.0],
                    "batch_size": [32],
                    "epochs": [5],
                    "patience": [2],
                    "input_noise_std": [0.0],
                    "pca_components": [None],
                    "beta": [1.0],
                },
                artifact_path=artifact_path,
                use_pca=False,
            )

            self.assertTrue(artifact_path.exists())
            self.assertGreaterEqual(result.metrics.average_precision, 0.5)
            self.assertGreaterEqual(result.metrics.roc_auc, 0.5)
