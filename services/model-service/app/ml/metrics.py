"""Evaluation metrics for anomaly detection."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from sklearn.metrics import (
    average_precision_score,
    f1_score,
    matthews_corrcoef,
    precision_score,
    recall_score,
    roc_auc_score,
)


@dataclass(frozen=True, slots=True)
class MetricBundle:
    roc_auc: float
    average_precision: float
    f1: float
    precision: float
    recall: float
    balanced_accuracy: float
    matthews_corrcoef: float
    precision_at_k: float
    recall_at_k: float
    threshold: float


def select_threshold(y_true: np.ndarray, scores: np.ndarray) -> float:
    candidate_thresholds = np.unique(np.quantile(scores, np.linspace(0.80, 0.995, 30)))
    best_threshold = float(candidate_thresholds[0])
    best_f1 = -1.0
    for threshold in candidate_thresholds:
        predictions = (scores >= threshold).astype(int)
        current_f1 = f1_score(y_true, predictions, zero_division=0)
        if current_f1 > best_f1:
            best_f1 = current_f1
            best_threshold = float(threshold)
    return best_threshold


def compute_metrics(y_true: np.ndarray, scores: np.ndarray, threshold: float | None = None) -> MetricBundle:
    if threshold is None:
        threshold = select_threshold(y_true, scores)

    predictions = (scores >= threshold).astype(int)
    positive_count = max(int(np.sum(y_true)), 1)
    top_indices = np.argsort(scores)[::-1][:positive_count]
    top_predictions = np.zeros_like(y_true)
    top_predictions[top_indices] = 1

    unique_labels = np.unique(y_true)
    if len(unique_labels) < 2:
        single_class = int(unique_labels[0])
        roc_auc = 0.0
        average_precision = precision_score(y_true, predictions, zero_division=0)
        if single_class == 0:
            balanced_accuracy = float(np.mean(predictions == 0))
        else:
            balanced_accuracy = float(np.mean(predictions == 1))
        matthews = 0.0
    else:
        roc_auc = float(roc_auc_score(y_true, scores))
        average_precision = float(average_precision_score(y_true, scores))
        true_positive = np.sum((y_true == 1) & (predictions == 1))
        true_negative = np.sum((y_true == 0) & (predictions == 0))
        false_positive = np.sum((y_true == 0) & (predictions == 1))
        false_negative = np.sum((y_true == 1) & (predictions == 0))
        sensitivity = true_positive / max(true_positive + false_negative, 1)
        specificity = true_negative / max(true_negative + false_positive, 1)
        balanced_accuracy = float((sensitivity + specificity) / 2.0)
        matthews = float(matthews_corrcoef(y_true, predictions))

    return MetricBundle(
        roc_auc=roc_auc,
        average_precision=average_precision,
        f1=float(f1_score(y_true, predictions, zero_division=0)),
        precision=float(precision_score(y_true, predictions, zero_division=0)),
        recall=float(recall_score(y_true, predictions, zero_division=0)),
        balanced_accuracy=balanced_accuracy,
        matthews_corrcoef=matthews,
        precision_at_k=float(precision_score(y_true, top_predictions, zero_division=0)),
        recall_at_k=float(recall_score(y_true, top_predictions, zero_division=0)),
        threshold=float(threshold),
    )
