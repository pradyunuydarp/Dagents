"""PyTorch provider adapters.

The in-repo families — ``autoencoder``, ``variational_autoencoder``, ``gru``,
``lstm`` — need no download and no extra install, but until now nothing loaded a
trained artifact back for inference: the pipeline saved one and the service
never read it. These adapters close that, and put the PyTorch families behind
the same provisioning contract as the Hugging Face ones, so a caller that has
resolved a capability does not have to know which provider it got.

``torch`` is imported inside `load`, not at module import, for the same reason
as in the Hugging Face provider: resolving or listing a capability must not cost
a framework import.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.ml.inventory import (
    MissingModelDependencyError,
    ModelCapability,
    UnsupportedModelError,
)


class TorchAnomalyAdapter:
    """Score rows with a trained anomaly artifact.

    Params:
    - `capability`: the inventory entry that selected this adapter.
    - `artifact_path`: the `.pt` file written by the training pipeline.
    - `device`: torch device string.

    What it does:
    - Holds only the path until `load` is called.
    - On `load`, restores the saved preprocessing, rebuilds the module from the
      artifact's own family and hyperparameters, and loads its weights — so
      inference cannot silently use a different architecture than training did.
    - On `score`, applies the stored preprocessing and returns reconstruction
      error per row, exactly as the training pipeline computed it.

    Returns:
    - Consumed through `load`, `score` and `flag`.
    """

    def __init__(
        self,
        capability: ModelCapability,
        *,
        artifact_path: str | Path | None = None,
        device: str = "cpu",
    ) -> None:
        self.capability = capability
        self.artifact_path = Path(artifact_path) if artifact_path else None
        self.device = device
        self._model: Any = None
        self._preprocessor: Any = None
        self._torch: Any = None
        self._threshold: float | None = None

    @property
    def is_loaded(self) -> bool:
        """Whether the module is resident and ready to score."""
        return self._model is not None

    @property
    def threshold(self) -> float | None:
        """The decision threshold chosen during training, once loaded."""
        return self._threshold

    def load(self) -> None:
        """Restore the artifact's preprocessing, architecture and weights.

        Raises:
        - `ValueError` when no artifact path was given, or the file is missing.
        - `MissingModelDependencyError` when the ML stack is not installed.
        - `UnsupportedModelError` when the artifact's family does not match the
          capability being loaded — a mismatch that would otherwise score rows
          with the wrong model and report the numbers as if they were right.
        """
        if self.artifact_path is None:
            raise ValueError(
                f"{self.capability.family}/{self.capability.task} needs an artifact_path to load"
            )
        if not self.artifact_path.is_file():
            raise ValueError(f"No model artifact at {self.artifact_path}")
        try:
            import torch

            from app.ml.artifacts import ArtifactStore
            from app.ml.modules import ModelConfig, build_model
            from app.ml.preprocessing import TabularPreprocessor
        except ImportError as exc:  # pragma: no cover - the service pins these
            raise MissingModelDependencyError(
                f"Could not import the PyTorch stack for {self.capability.family}: {exc}",
                family=self.capability.family,
                task=self.capability.task,
            ) from exc

        artifact = ArtifactStore.load(self.artifact_path)
        if artifact.metadata.model_family != self.capability.family:
            raise UnsupportedModelError(
                f"Artifact {self.artifact_path} holds a "
                f"{artifact.metadata.model_family!r} model, but this capability is "
                f"{self.capability.family!r}",
                family=self.capability.family,
                task=self.capability.task,
            )

        parameters = artifact.model_config
        model = build_model(
            artifact.metadata.input_dim,
            ModelConfig(
                family=artifact.metadata.model_family,
                hidden_dims=list(parameters["hidden_dims"]),
                latent_dim=int(parameters["latent_dim"]),
                dropout=float(parameters.get("dropout", 0.0)),
                beta=float(parameters.get("beta", 1.0)),
            ),
        )
        model.load_state_dict(artifact.state_dict)
        model.to(self.device)
        model.eval()

        self._torch = torch
        self._model = model
        self._preprocessor = TabularPreprocessor.from_payload(artifact.preprocessing)
        self._threshold = float(artifact.metadata.threshold)

    def score(self, rows: Any) -> Any:
        """Return one anomaly score per row; higher means more anomalous.

        Params:
        - `rows`: numeric matrix shaped `[rows, features]`, in the artifact's
          feature order.

        Returns:
        - A `numpy` array of reconstruction errors, computed the same way the
          training pipeline computed them.
        """
        if not self.is_loaded:
            raise RuntimeError("load() must be called before score()")
        import numpy as np

        transformed = self._preprocessor.transform(np.asarray(rows, dtype=np.float32))
        tensor = self._torch.tensor(transformed, dtype=self._torch.float32, device=self.device)
        with self._torch.no_grad():
            scores = self._model.score_samples(tensor).detach().cpu().numpy()
        return scores.astype(np.float64)

    def flag(self, rows: Any) -> Any:
        """Apply the artifact's own trained threshold to `score`.

        The threshold travels with the artifact, so a caller cannot accidentally
        evaluate a model against a boundary it was never tuned for.
        """
        import numpy as np

        return np.asarray(self.score(rows) >= self._threshold)


class TorchForecastingAdapter:
    """A small recurrent forecaster over ordered rows.

    Params:
    - `capability`: the inventory entry that selected this adapter, whose family
      (`gru` or `lstm`) picks the recurrent cell.
    - `input_dim`: features per time step.
    - `hidden_dim`: recurrent hidden width.
    - `device`: torch device string.

    What it does:
    - Builds the same `RecurrentRegressor` the forecasting check uses, so the
      inventory's forecasting entrypoint and the check cannot diverge into two
      architectures.

    Returns:
    - Consumed through `load` and `predict`.
    """

    def __init__(
        self,
        capability: ModelCapability,
        *,
        input_dim: int,
        hidden_dim: int = 32,
        device: str = "cpu",
    ) -> None:
        self.capability = capability
        self.input_dim = input_dim
        self.hidden_dim = hidden_dim
        self.device = device
        self._model: Any = None
        self._torch: Any = None

    @property
    def is_loaded(self) -> bool:
        """Whether the module is resident."""
        return self._model is not None

    def load(self) -> None:
        """Instantiate the recurrent regressor for this capability's family."""
        try:
            import torch

            from app.ml.checks import RecurrentRegressor
        except ImportError as exc:  # pragma: no cover - the service pins these
            raise MissingModelDependencyError(
                f"Could not import the PyTorch stack for {self.capability.family}: {exc}",
                family=self.capability.family,
                task=self.capability.task,
            ) from exc
        self._torch = torch
        self._model = RecurrentRegressor(
            input_dim=self.input_dim,
            hidden_dim=self.hidden_dim,
            family=self.capability.family,
        ).to(self.device)

    def predict(self, sequences: Any) -> Any:
        """Predict the next value for each sequence.

        Params:
        - `sequences`: tensor-like shaped `[batch, sequence_length, input_dim]`.

        Returns:
        - A `numpy` array of one prediction per sequence.
        """
        if not self.is_loaded:
            raise RuntimeError("load() must be called before predict()")
        import numpy as np

        tensor = self._torch.tensor(
            np.asarray(sequences, dtype=np.float32), device=self.device
        )
        with self._torch.no_grad():
            return self._model(tensor).squeeze(-1).cpu().numpy()


def build_anomaly_adapter(capability: ModelCapability, **options: Any) -> TorchAnomalyAdapter:
    """Inventory entrypoint for the PyTorch anomaly families."""
    return TorchAnomalyAdapter(capability, **options)


def build_forecasting_adapter(
    capability: ModelCapability, **options: Any
) -> TorchForecastingAdapter:
    """Inventory entrypoint for the PyTorch recurrent forecasting families."""
    return TorchForecastingAdapter(capability, **options)
