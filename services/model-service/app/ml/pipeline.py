"""Unified train/validate/test anomaly training pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from pathlib import Path
import random
from typing import Any

import numpy as np
import pandas as pd
import torch
from sklearn.model_selection import LeaveOneOut, StratifiedKFold, train_test_split
from torch.utils.data import DataLoader, TensorDataset

from app.ml.artifacts import ArtifactMetadata, ArtifactStore, ModelArtifact
from app.ml.metrics import MetricBundle, compute_metrics
from app.ml.modules import ModelConfig, SUPPORTED_MODEL_FAMILIES, build_model
from app.ml.preprocessing import PreprocessingConfig, TabularPreprocessor


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    dataset_name: str
    model_family: str
    test_size: float
    tuning_strategy: str
    n_splits: int
    leave_one_out_max_samples: int
    target_metric: str
    random_seed: int
    device: str


@dataclass(frozen=True, slots=True)
class CrossValidationSummary:
    strategy: str
    folds: int
    metric_mean: float
    metric_std: float
    best_params: dict[str, Any]


@dataclass(frozen=True, slots=True)
class TrainingResult:
    artifact_path: str
    model_version: str
    input_dim: int
    train_rows: int
    test_rows: int
    best_params: dict[str, Any]
    cross_validation: CrossValidationSummary
    metrics: MetricBundle


class UnifiedAnomalyTrainingPipeline:
    """Generic PyTorch anomaly training engine for tabular benchmarks."""

    def __init__(self, config: PipelineConfig) -> None:
        if config.model_family not in SUPPORTED_MODEL_FAMILIES:
            raise ValueError(f"Unsupported model family: {config.model_family}")
        self.config = config
        self._device = self._resolve_device(config.device)
        random.seed(config.random_seed)
        np.random.seed(config.random_seed)
        torch.manual_seed(config.random_seed)

    def train(
        self,
        features: pd.DataFrame,
        labels: np.ndarray,
        *,
        search_space: dict[str, list[Any]],
        artifact_path: Path,
        use_pca: bool,
    ) -> TrainingResult:
        x_values = features.to_numpy(dtype=np.float32)
        y_values = np.asarray(labels, dtype=np.int64)

        x_dev, x_test, y_dev, y_test = train_test_split(
            x_values,
            y_values,
            test_size=self.config.test_size,
            random_state=self.config.random_seed,
            stratify=y_values,
        )

        candidates = list(self._expand_search_space(search_space))
        best_params: dict[str, Any] | None = None
        best_score = float("-inf")
        best_threshold = 0.0
        fold_scores: list[float] = []

        for candidate in candidates:
            candidate_scores, candidate_thresholds = self._evaluate_candidate(
                x_dev,
                y_dev,
                candidate,
                use_pca=use_pca,
            )
            mean_score = float(np.mean(candidate_scores))
            if mean_score > best_score:
                best_score = mean_score
                best_params = candidate
                best_threshold = float(np.mean(candidate_thresholds))
                fold_scores = candidate_scores

        if best_params is None:
            raise RuntimeError("No candidate configuration was selected.")

        final_model, final_preprocessor = self._fit_candidate(x_dev, y_dev, best_params, use_pca=use_pca)
        test_scores = self._score(final_model, final_preprocessor, x_test)
        test_metrics = compute_metrics(y_test, test_scores, threshold=best_threshold)

        artifact = ModelArtifact(
            metadata=ArtifactMetadata(
                model_version=f"{self.config.dataset_name}-{self.config.model_family}-v1",
                model_family=self.config.model_family,
                dataset_name=self.config.dataset_name,
                input_dim=self._transformed_dim(final_preprocessor, x_dev[:1]),
                feature_names=list(features.columns),
                threshold=test_metrics.threshold,
                best_params=best_params,
                test_metrics={
                    "roc_auc": test_metrics.roc_auc,
                    "average_precision": test_metrics.average_precision,
                    "f1": test_metrics.f1,
                    "precision": test_metrics.precision,
                    "recall": test_metrics.recall,
                    "balanced_accuracy": test_metrics.balanced_accuracy,
                    "matthews_corrcoef": test_metrics.matthews_corrcoef,
                    "precision_at_k": test_metrics.precision_at_k,
                    "recall_at_k": test_metrics.recall_at_k,
                },
            ),
            preprocessing=final_preprocessor.to_payload(),
            model_config=best_params,
            state_dict=final_model.state_dict(),
        )
        ArtifactStore.save(artifact_path, artifact)

        return TrainingResult(
            artifact_path=str(artifact_path),
            model_version=artifact.metadata.model_version,
            input_dim=artifact.metadata.input_dim,
            train_rows=len(x_dev),
            test_rows=len(x_test),
            best_params=best_params,
            cross_validation=CrossValidationSummary(
                strategy=self.config.tuning_strategy,
                folds=len(fold_scores),
                metric_mean=float(np.mean(fold_scores)),
                metric_std=float(np.std(fold_scores)),
                best_params=best_params,
            ),
            metrics=test_metrics,
        )

    def _evaluate_candidate(
        self,
        x_dev: np.ndarray,
        y_dev: np.ndarray,
        candidate: dict[str, Any],
        *,
        use_pca: bool,
    ) -> tuple[list[float], list[float]]:
        if self.config.tuning_strategy == "leave_one_out":
            out_of_fold_scores = np.zeros(len(y_dev), dtype=np.float64)
            evaluated_indices: list[int] = []
            for train_indices, val_indices in self._iter_tuning_splits(y_dev):
                x_train, x_val = x_dev[train_indices], x_dev[val_indices]
                y_train = y_dev[train_indices]
                model, preprocessor = self._fit_candidate(x_train, y_train, candidate, use_pca=use_pca)
                val_scores = self._score(model, preprocessor, x_val)
                out_of_fold_scores[val_indices] = val_scores
                evaluated_indices.extend(val_indices.tolist())

            unique_indices = np.asarray(sorted(set(evaluated_indices)), dtype=np.int64)
            metrics = compute_metrics(y_dev[unique_indices], out_of_fold_scores[unique_indices])
            return [getattr(metrics, self.config.target_metric)], [metrics.threshold]

        candidate_scores: list[float] = []
        candidate_thresholds: list[float] = []
        for train_indices, val_indices in self._iter_tuning_splits(y_dev):
            x_train, x_val = x_dev[train_indices], x_dev[val_indices]
            y_train, y_val = y_dev[train_indices], y_dev[val_indices]

            model, preprocessor = self._fit_candidate(x_train, y_train, candidate, use_pca=use_pca)
            val_scores = self._score(model, preprocessor, x_val)
            metrics = compute_metrics(y_val, val_scores)
            candidate_scores.append(getattr(metrics, self.config.target_metric))
            candidate_thresholds.append(metrics.threshold)
        return candidate_scores, candidate_thresholds

    def _fit_candidate(
        self,
        train_values: np.ndarray,
        train_labels: np.ndarray,
        params: dict[str, Any],
        *,
        use_pca: bool,
    ):
        normal_values = train_values[train_labels == 0]
        preprocessor = TabularPreprocessor(
            PreprocessingConfig(
                use_scaler=True,
                use_pca=use_pca and params.get("pca_components") is not None,
                pca_components=params.get("pca_components"),
            )
        )
        transformed_train = preprocessor.fit_transform(normal_values)

        model = build_model(
            transformed_train.shape[1],
            ModelConfig(
                family=self.config.model_family,
                hidden_dims=list(params["hidden_dims"]),
                latent_dim=int(params["latent_dim"]),
                dropout=float(params.get("dropout", 0.0)),
                beta=float(params.get("beta", 1.0)),
            ),
        ).to(self._device)

        dataset = TensorDataset(torch.tensor(transformed_train, dtype=torch.float32))
        loader = DataLoader(dataset, batch_size=int(params["batch_size"]), shuffle=True)
        optimizer = torch.optim.Adam(
            model.parameters(),
            lr=float(params["learning_rate"]),
            weight_decay=float(params.get("weight_decay", 0.0)),
        )

        best_state = None
        best_loss = float("inf")
        patience = int(params.get("patience", 5))
        epochs_without_improvement = 0
        input_noise_std = float(params.get("input_noise_std", 0.0))

        for _ in range(int(params["epochs"])):
            epoch_loss = 0.0
            model.train()
            for (batch,) in loader:
                batch = batch.to(self._device)
                noisy_batch = batch
                if input_noise_std > 0:
                    noisy_batch = batch + input_noise_std * torch.randn_like(batch)

                optimizer.zero_grad()
                loss = model.compute_loss(noisy_batch if input_noise_std > 0 else batch, target=batch)
                loss.backward()
                optimizer.step()
                epoch_loss += float(loss.item()) * len(batch)

            mean_loss = epoch_loss / max(len(dataset), 1)
            if mean_loss < best_loss:
                best_loss = mean_loss
                best_state = {key: value.detach().cpu().clone() for key, value in model.state_dict().items()}
                epochs_without_improvement = 0
            else:
                epochs_without_improvement += 1
                if epochs_without_improvement >= patience:
                    break

        if best_state is not None:
            model.load_state_dict(best_state)
        model.eval()
        return model, preprocessor

    def _score(self, model, preprocessor: TabularPreprocessor, values: np.ndarray) -> np.ndarray:
        transformed = preprocessor.transform(values)
        tensor = torch.tensor(transformed, dtype=torch.float32, device=self._device)
        with torch.no_grad():
            scores = model.score_samples(tensor).detach().cpu().numpy()
        return scores.astype(np.float64)

    def _iter_tuning_splits(self, labels: np.ndarray):
        if self.config.tuning_strategy == "holdout":
            train_indices, val_indices = train_test_split(
                np.arange(len(labels)),
                test_size=0.2,
                random_state=self.config.random_seed,
                stratify=labels,
            )
            yield np.sort(train_indices), np.sort(val_indices)
            return

        if self.config.tuning_strategy == "leave_one_out":
            subset_indices = np.arange(len(labels))
            if len(subset_indices) > self.config.leave_one_out_max_samples:
                subset_indices, _ = train_test_split(
                    subset_indices,
                    train_size=self.config.leave_one_out_max_samples,
                    random_state=self.config.random_seed,
                    stratify=labels,
                )
                subset_indices = np.sort(subset_indices)
            loo = LeaveOneOut()
            for train_indices, val_indices in loo.split(subset_indices):
                yield subset_indices[train_indices], subset_indices[val_indices]
            return

        if self.config.tuning_strategy != "stratified_kfold":
            raise ValueError(f"Unsupported tuning strategy: {self.config.tuning_strategy}")

        splitter = StratifiedKFold(
            n_splits=self.config.n_splits,
            shuffle=True,
            random_state=self.config.random_seed,
        )
        yield from splitter.split(np.zeros(len(labels)), labels)

    @staticmethod
    def _expand_search_space(search_space: dict[str, list[Any]]):
        defaults = {
            "hidden_dims": [[64, 32], [128, 64]],
            "latent_dim": [8],
            "dropout": [0.0, 0.1],
            "learning_rate": [0.001],
            "weight_decay": [0.0],
            "batch_size": [256],
            "epochs": [15],
            "patience": [4],
            "input_noise_std": [0.0],
            "pca_components": [None, 0.95],
            "beta": [1.0],
        }
        merged = {**defaults, **(search_space or {})}
        keys = list(merged.keys())
        for values in product(*(merged[key] for key in keys)):
            yield dict(zip(keys, values, strict=True))

    def _resolve_device(self, device: str) -> str:
        if device == "cuda" and torch.cuda.is_available():
            return "cuda"
        return "cpu"

    @staticmethod
    def _transformed_dim(preprocessor: TabularPreprocessor, sample: np.ndarray) -> int:
        return int(preprocessor.transform(sample).shape[1])
