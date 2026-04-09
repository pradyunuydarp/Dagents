"""Shared helpers for lightweight ML orchestration across agents."""

from __future__ import annotations

import math
import time
from typing import Any

from agents.common.application.ports import SourceResolver
from agents.common.domain.models import (
    DatasetProfile,
    DatasetProfileRequest,
    DatasetInput,
    ModelExecutionRequest,
    ModelExecutionResponse,
    ModelRunRecord,
)


def build_dataset_profile(
    request: DatasetProfileRequest,
    *,
    source_resolver: SourceResolver | None = None,
) -> DatasetProfile:
    records = resolve_records(request.dataset, request.records, source_resolver=source_resolver)
    feature_fields = request.feature_fields or _infer_feature_fields(records, request.label_field)
    numeric_fields: list[str] = []
    categorical_fields: list[str] = []

    for field in feature_fields:
        sample = _first_non_null(records, field)
        if isinstance(sample, (int, float)) and not isinstance(sample, bool):
            numeric_fields.append(field)
        else:
            categorical_fields.append(field)

    record_count = len(records)
    partition_count = max(1, math.ceil(record_count / request.batch_size)) if record_count else 0
    suggested_models = suggest_models(
        extraction_strategy=request.extraction_strategy,
        task_type="classification" if request.label_field else "anomaly_detection",
        numeric_fields=numeric_fields,
    )

    return DatasetProfile(
        scope_id=request.scope_id,
        scope_kind=request.scope_kind,
        extraction_strategy=request.extraction_strategy,
        record_count=record_count,
        feature_fields=feature_fields,
        label_field=request.label_field,
        numeric_fields=numeric_fields,
        categorical_fields=categorical_fields,
        partition_count=partition_count,
        suggested_models=suggested_models,
    )


def execute_model_run(
    request: ModelExecutionRequest,
    *,
    agent_role: str,
    source_resolver: SourceResolver | None = None,
) -> ModelExecutionResponse:
    started_at = int(time.time())
    records = resolve_records(request.dataset, request.records, source_resolver=source_resolver)
    profile = build_dataset_profile(
        DatasetProfileRequest(
            scope_id=request.scope_id,
            scope_kind=request.scope_kind,
            extraction_strategy=_infer_extraction_strategy(request, records),
            dataset=request.dataset,
            records=records,
            feature_fields=request.feature_fields,
            label_field=request.label_field,
            batch_size=int(request.hyperparameters.get("batch_size", 1000)),
        ),
        source_resolver=source_resolver,
    )
    batch_size = max(1, int(request.hyperparameters.get("batch_size", 1000)))
    batch_count = max(1, math.ceil(profile.record_count / batch_size)) if profile.record_count else 0
    metrics = _estimate_metrics(request, profile, agent_role=agent_role)
    artifact_uri = (
        f"{request.artifact_prefix.rstrip('/')}/{agent_role.lower()}/{request.scope_id}/"
        f"{request.model_family}-{request.model_version}.json"
    )

    run = ModelRunRecord(
        run_id=f"{agent_role.lower()}-{request.scope_id}-{started_at}",
        agent_role=agent_role,  # type: ignore[arg-type]
        scope_id=request.scope_id,
        scope_kind=request.scope_kind,
        task_type=request.task_type,
        model_family=request.model_family,
        record_count=profile.record_count,
        feature_count=len(profile.feature_fields),
        batch_count=batch_count,
        metrics=metrics,
        artifact_uri=artifact_uri,
        started_at=started_at,
        completed_at=int(time.time()),
    )
    return ModelExecutionResponse(dataset_profile=profile, run=run)


def resolve_records(
    dataset: DatasetInput | None,
    legacy_records: list[dict[str, Any]],
    *,
    source_resolver: SourceResolver | None = None,
) -> list[dict[str, Any]]:
    if dataset is None:
        return legacy_records
    if dataset.inline_records:
        return dataset.inline_records
    if source_resolver is None:
        raise ValueError("Source-backed dataset input requires a source_resolver")
    records: list[dict[str, Any]] = []
    for batch in source_resolver.materialize(dataset):
        records.extend(batch.records)
    return records


def suggest_models(extraction_strategy: str, task_type: str, numeric_fields: list[str]) -> list[str]:
    if task_type == "forecasting":
        return ["gru", "lstm", "transformer"]
    if extraction_strategy == "text":
        return ["naive_bayes", "gru", "transformer"]
    if extraction_strategy == "time_series":
        return ["gru", "lstm", "autoencoder"]
    if task_type == "classification":
        return ["random_forest", "xgboost", "naive_bayes"]
    if numeric_fields:
        return ["autoencoder", "variational_autoencoder", "random_forest"]
    return ["transformer", "custom"]


def _estimate_metrics(
    request: ModelExecutionRequest,
    profile: DatasetProfile,
    *,
    agent_role: str,
) -> dict[str, float]:
    scale = min(1.0, max(0.1, profile.record_count / 10_000 if profile.record_count else 0.1))
    role_bias = 0.04 if agent_role == "GMA" else 0.0
    family_bias = {
        "autoencoder": 0.74,
        "variational_autoencoder": 0.77,
        "gru": 0.75,
        "lstm": 0.76,
        "naive_bayes": 0.68,
        "transformer": 0.79,
        "random_forest": 0.72,
        "xgboost": 0.74,
        "linear": 0.66,
        "custom": 0.7,
    }.get(request.model_family, 0.7)
    score = min(0.99, family_bias + (0.15 * scale) + role_bias)
    return {
        "quality_score": round(score, 4),
        "throughput_rows_per_batch": float(max(1, math.ceil(profile.record_count / max(1, profile.partition_count or 1)))),
        "feature_coverage": round(min(1.0, len(profile.feature_fields) / max(1, len(profile.feature_fields) + len(profile.categorical_fields))), 4),
    }


def _infer_feature_fields(records: list[dict[str, Any]], label_field: str | None) -> list[str]:
    if not records:
        return []
    fields = list(records[0].keys())
    return [field for field in fields if field != label_field]


def _first_non_null(records: list[dict[str, Any]], field: str) -> Any:
    for record in records:
        value = record.get(field)
        if value is not None:
            return value
    return None


def _infer_extraction_strategy(request: ModelExecutionRequest, records: list[dict[str, Any]]) -> str:
    if request.task_type == "forecasting":
        return "time_series"
    if any(any(isinstance(value, str) and len(value.split()) > 4 for value in record.values()) for record in records[:5]):
        return "text"
    if any("timestamp" in key.lower() for record in records[:1] for key in record):
        return "time_series"
    return "tabular"
