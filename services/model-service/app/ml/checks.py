"""Lightweight classification, regression, and forecasting evaluation checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import torch
from torch import nn
from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor
from sklearn.linear_model import LinearRegression, LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    average_precision_score,
    balanced_accuracy_score,
    f1_score,
    matthews_corrcoef,
    mean_absolute_error,
    mean_squared_error,
    precision_score,
    r2_score,
    recall_score,
    roc_auc_score,
)
from sklearn.model_selection import train_test_split
from sklearn.naive_bayes import GaussianNB


CLASSIFICATION_FAMILIES = {"random_forest", "naive_bayes", "linear"}
REGRESSION_FAMILIES = {"random_forest", "linear"}
FORECASTING_FAMILIES = {"gru", "lstm"}


@dataclass(frozen=True, slots=True)
class ClassificationCheckResult:
    train_rows: int
    test_rows: int
    metrics: dict[str, float]


@dataclass(frozen=True, slots=True)
class RegressionCheckResult:
    train_rows: int
    test_rows: int
    metrics: dict[str, float]


@dataclass(frozen=True, slots=True)
class ForecastingCheckResult:
    train_rows: int
    test_rows: int
    metrics: dict[str, float]


def run_classification_check(
    features: np.ndarray,
    labels: np.ndarray,
    *,
    model_family: str,
    test_size: float,
    random_seed: int,
) -> ClassificationCheckResult:
    """Run a lightweight supervised classification evaluation.

    Params:
    - `features`: numeric feature matrix shaped `[rows, features]`.
    - `labels`: class labels aligned to `features`.
    - `model_family`: one of the supported classification families.
    - `test_size`: holdout ratio used by `train_test_split`.
    - `random_seed`: deterministic seed for data splitting and seeded estimators.

    What it does:
    - Validates the requested model family and target cardinality.
    - Splits the data into train and test sets with class stratification.
    - Fits the requested classifier and computes standard classification metrics.

    Returns:
    - `ClassificationCheckResult` with train/test row counts and metric values.
    """
    if model_family not in CLASSIFICATION_FAMILIES:
        raise ValueError(f"Unsupported classification model family: {model_family}")
    if len(np.unique(labels)) < 2:
        raise ValueError("Classification checks require at least two target classes")

    x_train, x_test, y_train, y_test = train_test_split(
        features,
        labels.astype(np.int64),
        test_size=test_size,
        random_state=random_seed,
        stratify=labels,
    )
    model = _build_classifier(model_family, random_seed=random_seed)
    model.fit(x_train, y_train)
    # Predictions drive thresholded metrics, while probabilities or decision
    # scores are used for ranking-style metrics such as ROC AUC.
    predictions = model.predict(x_test)
    probabilities = _classification_scores(model, x_test)

    metrics = {
        "accuracy": float(accuracy_score(y_test, predictions)),
        "precision": float(precision_score(y_test, predictions, zero_division=0)),
        "recall": float(recall_score(y_test, predictions, zero_division=0)),
        "f1": float(f1_score(y_test, predictions, zero_division=0)),
        "balanced_accuracy": float(balanced_accuracy_score(y_test, predictions)),
        "matthews_corrcoef": float(matthews_corrcoef(y_test, predictions)),
    }
    if probabilities is not None:
        metrics["roc_auc"] = float(roc_auc_score(y_test, probabilities))
        metrics["average_precision"] = float(average_precision_score(y_test, probabilities))

    return ClassificationCheckResult(train_rows=len(x_train), test_rows=len(x_test), metrics=metrics)


def run_regression_check(
    features: np.ndarray,
    targets: np.ndarray,
    *,
    model_family: str,
    test_size: float,
    random_seed: int,
) -> RegressionCheckResult:
    """Run a lightweight supervised regression evaluation.

    Params:
    - `features`: numeric feature matrix.
    - `targets`: continuous target values aligned to `features`.
    - `model_family`: supported regression family.
    - `test_size`: holdout ratio used by `train_test_split`.
    - `random_seed`: deterministic seed for the data split and seeded estimators.

    What it does:
    - Splits the regression dataset into train/test partitions.
    - Fits the requested regressor.
    - Computes MAE, MSE, RMSE, and R² on the holdout set.

    Returns:
    - `RegressionCheckResult`.
    """
    if model_family not in REGRESSION_FAMILIES:
        raise ValueError(f"Unsupported regression model family: {model_family}")

    x_train, x_test, y_train, y_test = train_test_split(
        features,
        targets.astype(np.float64),
        test_size=test_size,
        random_state=random_seed,
    )
    model = _build_regressor(model_family, random_seed=random_seed)
    model.fit(x_train, y_train)
    predictions = model.predict(x_test)
    mse = float(mean_squared_error(y_test, predictions))

    return RegressionCheckResult(
        train_rows=len(x_train),
        test_rows=len(x_test),
        metrics={
            "mae": float(mean_absolute_error(y_test, predictions)),
            "mse": mse,
            "rmse": float(np.sqrt(mse)),
            "r2": float(r2_score(y_test, predictions)),
        },
    )


def run_forecasting_check(
    features: np.ndarray,
    targets: np.ndarray,
    *,
    model_family: str,
    test_size: float,
    random_seed: int,
    sequence_length: int,
    epochs: int = 12,
) -> ForecastingCheckResult:
    """Run a recurrent forecasting evaluation using GRU or LSTM.

    Params:
    - `features`: ordered numeric input rows.
    - `targets`: ordered numeric targets aligned to the input rows.
    - `model_family`: `gru` or `lstm`.
    - `test_size`: ratio reserved for sequence holdout evaluation.
    - `random_seed`: deterministic seed for the PyTorch model.
    - `sequence_length`: lookback window used to build sequence examples.
    - `epochs`: small fixed number of optimization epochs for the smoke-check model.

    What it does:
    - Converts the ordered tabular series into sliding-window sequence samples.
    - Splits those sequences into train and test ranges while preserving order.
    - Trains a small recurrent regressor and evaluates it on the holdout set.

    Returns:
    - `ForecastingCheckResult`.
    """
    if model_family not in FORECASTING_FAMILIES:
        raise ValueError(f"Unsupported forecasting model family: {model_family}")
    if len(features) <= sequence_length:
        raise ValueError("Forecasting checks require more rows than sequence_length")

    x_values, y_values = _build_sequences(features, targets, sequence_length)
    split_index = max(1, int(len(x_values) * (1.0 - test_size)))
    split_index = min(split_index, len(x_values) - 1)
    x_train = torch.tensor(x_values[:split_index], dtype=torch.float32)
    y_train = torch.tensor(y_values[:split_index], dtype=torch.float32).unsqueeze(-1)
    x_test = torch.tensor(x_values[split_index:], dtype=torch.float32)
    y_test = y_values[split_index:]

    torch.manual_seed(random_seed)
    # The recurrent model is intentionally small because this API is a fast
    # evaluation check, not the full production training pipeline.
    model = RecurrentRegressor(input_dim=x_train.shape[-1], hidden_dim=32, family=model_family)
    optimizer = torch.optim.Adam(model.parameters(), lr=0.01)
    loss_fn = nn.MSELoss()

    for _ in range(epochs):
        # Standard supervised training loop: forward pass, compute MSE, backprop,
        # and update parameters.
        optimizer.zero_grad()
        predictions = model(x_train)
        loss = loss_fn(predictions, y_train)
        loss.backward()
        optimizer.step()

    with torch.no_grad():
        predictions = model(x_test).squeeze(-1).cpu().numpy()
    mse = float(mean_squared_error(y_test, predictions))
    return ForecastingCheckResult(
        train_rows=len(x_train),
        test_rows=len(x_test),
        metrics={
            "mae": float(mean_absolute_error(y_test, predictions)),
            "mse": mse,
            "rmse": float(np.sqrt(mse)),
            "r2": float(r2_score(y_test, predictions)),
        },
    )


def _build_classifier(model_family: str, *, random_seed: int):
    """Construct the requested sklearn classifier for a classification check."""
    if model_family == "random_forest":
        return RandomForestClassifier(n_estimators=100, random_state=random_seed)
    if model_family == "naive_bayes":
        return GaussianNB()
    if model_family == "linear":
        return LogisticRegression(max_iter=1000, random_state=random_seed)
    raise ValueError(f"Unsupported classification model family: {model_family}")


def _build_regressor(model_family: str, *, random_seed: int):
    """Construct the requested sklearn regressor for a regression check."""
    if model_family == "random_forest":
        return RandomForestRegressor(n_estimators=100, random_state=random_seed)
    if model_family == "linear":
        return LinearRegression()
    raise ValueError(f"Unsupported regression model family: {model_family}")


def _classification_scores(model: Any, features: np.ndarray) -> np.ndarray | None:
    """Extract ranking scores from a classifier when the estimator exposes them.

    Params:
    - `model`: fitted sklearn-style classifier.
    - `features`: feature matrix used for scoring.

    What it does:
    - Prefers class probabilities when available.
    - Falls back to the decision function for linear models.

    Returns:
    - Score vector for the positive class, or `None` if unavailable.
    """
    if hasattr(model, "predict_proba"):
        probabilities = model.predict_proba(features)
        if probabilities.ndim == 2 and probabilities.shape[1] >= 2:
            return probabilities[:, 1]
    if hasattr(model, "decision_function"):
        decision = model.decision_function(features)
        return np.asarray(decision, dtype=np.float64)
    return None


class RecurrentRegressor(nn.Module):
    """Small recurrent regressor used by forecasting smoke checks.

    Params:
    - `input_dim`: number of features per time step.
    - `hidden_dim`: hidden-state width for the recurrent layer.
    - `family`: recurrent cell family, currently `gru` or `lstm`.

    What it does:
    - Builds a recurrent encoder plus a linear head that predicts the next value
      from the final hidden state of the sequence.
    """

    def __init__(self, *, input_dim: int, hidden_dim: int, family: str) -> None:
        super().__init__()
        if family == "gru":
            self.recurrent = nn.GRU(input_dim, hidden_dim, batch_first=True)
        elif family == "lstm":
            self.recurrent = nn.LSTM(input_dim, hidden_dim, batch_first=True)
        else:  # pragma: no cover - guarded by caller
            raise ValueError(f"Unsupported recurrent family: {family}")
        self.head = nn.Linear(hidden_dim, 1)

    def forward(self, values: torch.Tensor) -> torch.Tensor:
        """Run the sequence through the recurrent encoder and prediction head.

        Params:
        - `values`: tensor shaped `[batch, sequence_length, input_dim]`.

        What it does:
        - Computes recurrent hidden states across the full sequence.
        - Uses only the final step's hidden state for next-value prediction.

        Returns:
        - Tensor shaped `[batch, 1]`.
        """
        output, _ = self.recurrent(values)
        return self.head(output[:, -1, :])


def _build_sequences(features: np.ndarray, targets: np.ndarray, sequence_length: int) -> tuple[np.ndarray, np.ndarray]:
    """Convert ordered tabular data into sliding-window forecasting samples.

    Params:
    - `features`: full ordered feature matrix.
    - `targets`: full ordered target vector.
    - `sequence_length`: number of prior rows per forecasting example.

    What it does:
    - Builds a sequence from the `sequence_length` rows preceding each index.
    - Uses the current index target as the prediction label for that sequence.

    Returns:
    - Tuple of `(sequence_features, sequence_targets)`.
    """
    sequences: list[np.ndarray] = []
    labels: list[float] = []
    for index in range(sequence_length, len(features)):
        # Each sample predicts the current target from the immediately preceding
        # `sequence_length` rows.
        sequences.append(features[index - sequence_length : index])
        labels.append(float(targets[index]))
    return np.asarray(sequences, dtype=np.float32), np.asarray(labels, dtype=np.float32)
