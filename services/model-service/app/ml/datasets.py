"""Benchmark anomaly dataset loaders."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.datasets import fetch_kddcup99, fetch_openml
from sklearn.model_selection import train_test_split


@dataclass(frozen=True, slots=True)
class DatasetDescriptor:
    name: str
    source: str
    source_url: str
    description: str
    anomaly_label: str


@dataclass(frozen=True, slots=True)
class TabularDataset:
    descriptor: DatasetDescriptor
    features: pd.DataFrame
    labels: np.ndarray


DATASET_REGISTRY: dict[str, DatasetDescriptor] = {
    "kddcup99_http": DatasetDescriptor(
        name="kddcup99_http",
        source="scikit-learn",
        source_url="https://scikit-learn.org/stable/modules/generated/sklearn.datasets.fetch_kddcup99.html",
        description="KDD Cup 1999 HTTP subset for network intrusion anomaly detection.",
        anomaly_label="not normal.",
    ),
    "creditcard_openml": DatasetDescriptor(
        name="creditcard_openml",
        source="OpenML",
        source_url="https://www.openml.org/d/42397",
        description="Credit card fraud anomaly detection benchmark hosted on OpenML.",
        anomaly_label="True",
    ),
    "mammography_openml": DatasetDescriptor(
        name="mammography_openml",
        source="OpenML",
        source_url="https://www.openml.org/d/310",
        description="Mammography anomaly detection benchmark hosted on OpenML.",
        anomaly_label="1",
    ),
}


def list_datasets() -> list[DatasetDescriptor]:
    return [DATASET_REGISTRY[key] for key in sorted(DATASET_REGISTRY)]


def load_dataset(name: str, max_rows: int | None = None, random_seed: int = 42) -> TabularDataset:
    if name not in DATASET_REGISTRY:
        raise ValueError(f"Unsupported dataset: {name}")

    if name == "kddcup99_http":
        dataset = fetch_kddcup99(subset="http", percent10=True, as_frame=True)
        features = dataset.data.copy()
        labels = (dataset.target.astype(str) != "normal.").astype(int).to_numpy()
    elif name == "creditcard_openml":
        dataset = fetch_openml(data_id=42397, as_frame=True)
        features = dataset.data.copy()
        labels = (dataset.target.astype(str) == "True").astype(int).to_numpy()
    elif name == "mammography_openml":
        dataset = fetch_openml(name="mammography", version=1, as_frame=True)
        features = dataset.data.copy()
        labels = (dataset.target.astype(str) == "1").astype(int).to_numpy()
    else:  # pragma: no cover
        raise ValueError(f"Unsupported dataset: {name}")

    features = _numeric_frame(features)
    if max_rows is not None and len(features) > max_rows:
        sampled_indices, _ = train_test_split(
            np.arange(len(features)),
            train_size=max_rows,
            random_state=random_seed,
            stratify=labels,
        )
        sampled_indices = np.sort(sampled_indices)
        features = features.iloc[sampled_indices].reset_index(drop=True)
        labels = labels[sampled_indices]

    return TabularDataset(
        descriptor=DATASET_REGISTRY[name],
        features=features.reset_index(drop=True),
        labels=np.asarray(labels, dtype=np.int64),
    )


def _numeric_frame(frame: pd.DataFrame) -> pd.DataFrame:
    converted = frame.copy()
    for column in converted.columns:
        converted[column] = pd.to_numeric(converted[column], errors="coerce")
    converted = converted.fillna(0.0)
    return converted.astype("float32")
