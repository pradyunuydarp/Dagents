"""Artifact persistence for trained anomaly models."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import torch


@dataclass(frozen=True, slots=True)
class ArtifactMetadata:
    model_version: str
    model_family: str
    dataset_name: str
    input_dim: int
    feature_names: list[str]
    threshold: float
    best_params: dict[str, Any]
    test_metrics: dict[str, float]


@dataclass(frozen=True, slots=True)
class ModelArtifact:
    metadata: ArtifactMetadata
    preprocessing: dict[str, Any]
    model_config: dict[str, Any]
    state_dict: dict[str, Any]


class ArtifactStore:
    @staticmethod
    def save(path: Path, artifact: ModelArtifact) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        torch.save(
            {
                "metadata": asdict(artifact.metadata),
                "preprocessing": artifact.preprocessing,
                "model_config": artifact.model_config,
                "state_dict": artifact.state_dict,
            },
            path,
        )

    @staticmethod
    def load(path: Path) -> ModelArtifact:
        payload = torch.load(path, map_location="cpu", weights_only=False)
        return ModelArtifact(
            metadata=ArtifactMetadata(**payload["metadata"]),
            preprocessing=payload["preprocessing"],
            model_config=payload["model_config"],
            state_dict=payload["state_dict"],
        )
