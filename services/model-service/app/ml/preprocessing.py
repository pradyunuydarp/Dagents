"""Preprocessing utilities for tabular anomaly pipelines."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from sklearn.decomposition import PCA
from sklearn.preprocessing import StandardScaler


@dataclass(frozen=True, slots=True)
class PreprocessingConfig:
    use_scaler: bool = True
    use_pca: bool = False
    pca_components: int | float | None = None


class TabularPreprocessor:
    """Fit/transform standardization and optional PCA."""

    def __init__(self, config: PreprocessingConfig) -> None:
        self.config = config
        self._scaler = StandardScaler() if config.use_scaler else None
        self._pca = None

    def fit(self, values: np.ndarray) -> None:
        transformed = values
        if self._scaler is not None:
            self._scaler.fit(values)
            transformed = self._scaler.transform(values)
        if self.config.use_pca and self.config.pca_components is not None:
            self._pca = PCA(n_components=self.config.pca_components, svd_solver="auto", random_state=42)
            self._pca.fit(transformed)

    def transform(self, values: np.ndarray) -> np.ndarray:
        transformed = values
        if self._scaler is not None:
            transformed = self._scaler.transform(transformed)
        if self._pca is not None:
            transformed = self._pca.transform(transformed)
        return transformed.astype("float32")

    def fit_transform(self, values: np.ndarray) -> np.ndarray:
        self.fit(values)
        return self.transform(values)

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "config": {
                "use_scaler": self.config.use_scaler,
                "use_pca": self.config.use_pca,
                "pca_components": self.config.pca_components,
            },
            "scaler": None,
            "pca": None,
        }
        if self._scaler is not None:
            payload["scaler"] = {
                "mean_": self._scaler.mean_.tolist(),
                "scale_": self._scaler.scale_.tolist(),
            }
        if self._pca is not None:
            payload["pca"] = {
                "components_": self._pca.components_.tolist(),
                "mean_": self._pca.mean_.tolist(),
                "explained_variance_": self._pca.explained_variance_.tolist(),
                "explained_variance_ratio_": self._pca.explained_variance_ratio_.tolist(),
                "singular_values_": self._pca.singular_values_.tolist(),
                "n_components_": int(self._pca.n_components_),
            }
        return payload

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> "TabularPreprocessor":
        config = PreprocessingConfig(**payload["config"])
        instance = cls(config)
        scaler_payload = payload.get("scaler")
        if scaler_payload:
            instance._scaler = StandardScaler()
            instance._scaler.mean_ = np.asarray(scaler_payload["mean_"], dtype=np.float64)
            instance._scaler.scale_ = np.asarray(scaler_payload["scale_"], dtype=np.float64)
            instance._scaler.var_ = instance._scaler.scale_ ** 2
            instance._scaler.n_features_in_ = len(instance._scaler.mean_)
        pca_payload = payload.get("pca")
        if pca_payload:
            instance._pca = PCA(n_components=pca_payload["n_components_"])
            instance._pca.components_ = np.asarray(pca_payload["components_"], dtype=np.float64)
            instance._pca.mean_ = np.asarray(pca_payload["mean_"], dtype=np.float64)
            instance._pca.explained_variance_ = np.asarray(pca_payload["explained_variance_"], dtype=np.float64)
            instance._pca.explained_variance_ratio_ = np.asarray(
                pca_payload["explained_variance_ratio_"], dtype=np.float64
            )
            instance._pca.singular_values_ = np.asarray(pca_payload["singular_values_"], dtype=np.float64)
            instance._pca.n_features_in_ = instance._pca.components_.shape[1]
            instance._pca.n_components_ = pca_payload["n_components_"]
        return instance
