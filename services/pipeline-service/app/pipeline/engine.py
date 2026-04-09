"""Execution engine for Dagents pipelines."""

from __future__ import annotations

from copy import deepcopy
import math
import time
from typing import Any

from agents.common.application.ml_orchestration import build_dataset_profile, execute_model_run
from agents.common.application.ports import SourceResolver
from agents.common.domain.models import DatasetInput, DatasetProfileRequest, ModelExecutionRequest
from app.models import AggregationSpec, PipelineCondition, PipelineDefinition, PipelineRunResponse, StepRunResult


class StepHandler:
    kind: str

    def execute(
        self,
        payload: dict[str, Any],
        config: dict[str, Any],
        *,
        source_resolver: SourceResolver | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError


class EnrichContextHandler(StepHandler):
    kind = "enrich_context"

    def execute(self, payload: dict[str, Any], config: dict[str, Any], *, source_resolver: SourceResolver | None = None) -> dict[str, Any]:
        del source_resolver
        target_field = str(config.get("target_field", "context"))
        values = dict(config.get("values", {}))
        current = PipelineExecutionEngine._get_path(payload, target_field, default={})
        if not isinstance(current, dict):
            raise ValueError(f"Target field {target_field} must resolve to an object")
        merged = {**current, **values}
        PipelineExecutionEngine._set_path(payload, target_field, merged)
        return {"target_field": target_field, "merged_keys": sorted(values)}


class FilterItemsHandler(StepHandler):
    kind = "filter_items"

    def execute(self, payload: dict[str, Any], config: dict[str, Any], *, source_resolver: SourceResolver | None = None) -> dict[str, Any]:
        del source_resolver
        items_field = str(config.get("items_field", "items"))
        items = list(PipelineExecutionEngine._get_path(payload, items_field, default=[]))
        conditions = [PipelineCondition(**entry) for entry in config.get("conditions", [])]
        filtered = [item for item in items if PipelineExecutionEngine._matches_all(item, conditions)]
        PipelineExecutionEngine._set_path(payload, items_field, filtered)
        return {"items_field": items_field, "input_count": len(items), "output_count": len(filtered)}


class SummarizeItemsHandler(StepHandler):
    kind = "summarize_items"

    def execute(self, payload: dict[str, Any], config: dict[str, Any], *, source_resolver: SourceResolver | None = None) -> dict[str, Any]:
        del source_resolver
        items_field = str(config.get("items_field", "items"))
        target_field = str(config.get("target_field", "summary"))
        items = list(PipelineExecutionEngine._get_path(payload, items_field, default=[]))
        aggregations = [AggregationSpec(**entry) for entry in config.get("aggregations", [])]
        summary: dict[str, Any] = {}
        for aggregation in aggregations:
            key = aggregation.alias or aggregation.field or aggregation.operation
            summary[key] = PipelineExecutionEngine._apply_aggregation(items, aggregation)
        PipelineExecutionEngine._set_path(payload, target_field, summary)
        return {"target_field": target_field, "summary_keys": sorted(summary)}


class ProjectFieldsHandler(StepHandler):
    kind = "project_fields"

    def execute(self, payload: dict[str, Any], config: dict[str, Any], *, source_resolver: SourceResolver | None = None) -> dict[str, Any]:
        del source_resolver
        target_field = str(config.get("target_field", "projection"))
        fields = list(config.get("fields", []))
        projection = {}
        for field in fields:
            exists, value = PipelineExecutionEngine._maybe_get_path(payload, field)
            if exists:
                projection[field] = value
        PipelineExecutionEngine._set_path(payload, target_field, projection)
        return {"target_field": target_field, "projected_fields": sorted(projection)}


class ProfileDatasetHandler(StepHandler):
    kind = "profile_dataset"

    def execute(self, payload: dict[str, Any], config: dict[str, Any], *, source_resolver: SourceResolver | None = None) -> dict[str, Any]:
        items_field = str(config.get("items_field", "items"))
        target_field = str(config.get("target_field", "dataset_profile"))
        label_field = config.get("label_field")
        dataset_input = _dataset_input_from_config(config)
        records = list(PipelineExecutionEngine._get_path(payload, items_field, default=[])) if dataset_input is None else []
        profile = build_dataset_profile(
            DatasetProfileRequest(
                scope_id=str(config.get("scope_id", items_field)),
                scope_kind=str(config.get("scope_kind", "source")),
                extraction_strategy=str(config.get("extraction_strategy", "tabular")),
                dataset=dataset_input,
                records=records,
                feature_fields=list(config.get("feature_fields", [])),
                label_field=label_field,
                batch_size=max(1, int(config.get("batch_size", 1000))),
            ),
            source_resolver=source_resolver,
        )
        payload_value = profile.model_dump()
        PipelineExecutionEngine._set_path(payload, target_field, payload_value)
        return {"target_field": target_field, "record_count": profile.record_count, "feature_count": len(profile.feature_fields)}


class RunModelJobHandler(StepHandler):
    kind = "run_model_job"

    def execute(self, payload: dict[str, Any], config: dict[str, Any], *, source_resolver: SourceResolver | None = None) -> dict[str, Any]:
        items_field = str(config.get("items_field", "items"))
        target_field = str(config.get("target_field", "model_run"))
        dataset_input = _dataset_input_from_config(config)
        records = list(PipelineExecutionEngine._get_path(payload, items_field, default=[])) if dataset_input is None else []
        response = execute_model_run(
            ModelExecutionRequest(
                scope_id=str(config.get("scope_id", items_field)),
                scope_kind=str(config.get("scope_kind", "source")),
                task_type=str(config.get("task_type", "anomaly_detection")),
                model_family=str(config.get("model_family", "autoencoder")),
                dataset=dataset_input,
                records=records,
                feature_fields=list(config.get("feature_fields", [])),
                label_field=config.get("label_field"),
                hyperparameters=dict(config.get("hyperparameters", {"batch_size": int(config.get("batch_size", 1000))})),
                artifact_prefix=str(config.get("artifact_prefix", "artifacts")),
                model_version=str(config.get("model_version", "v1")),
            ),
            agent_role="SERVICE",
            source_resolver=source_resolver,
        )
        payload_value = response.run.model_dump()
        PipelineExecutionEngine._set_path(payload, target_field, payload_value)
        return {"target_field": target_field, "model_family": response.run.model_family, "record_count": response.run.record_count}


class PipelineExecutionEngine:
    """Executes registered pipeline definitions against JSON-like payloads."""

    def __init__(self, handlers: list[StepHandler] | None = None) -> None:
        registered = handlers or [
            EnrichContextHandler(),
            FilterItemsHandler(),
            SummarizeItemsHandler(),
            ProjectFieldsHandler(),
            ProfileDatasetHandler(),
            RunModelJobHandler(),
        ]
        self._handlers = {handler.kind: handler for handler in registered}

    def execute(
        self,
        definition: PipelineDefinition,
        payload: dict[str, Any],
        *,
        source_resolver: SourceResolver | None = None,
    ) -> PipelineRunResponse:
        started_at = int(time.time())
        working_payload = deepcopy(payload)
        step_results: list[StepRunResult] = []

        for step in self._resolve_step_order(definition):
            step_started_at = int(time.time())
            output = self._execute_step(step.kind, working_payload, step.config, source_resolver=source_resolver)
            step_results.append(
                StepRunResult(
                    step_id=step.step_id,
                    kind=step.kind,
                    status="completed",
                    output=output,
                    started_at=step_started_at,
                    completed_at=int(time.time()),
                )
            )

        completed_at = int(time.time())
        return PipelineRunResponse(
            run_id="",
            pipeline_id=definition.pipeline_id,
            status="completed",
            started_at=started_at,
            completed_at=completed_at,
            final_payload=working_payload,
            step_results=step_results,
        )

    def validate(self, definition: PipelineDefinition) -> None:
        self._resolve_step_order(definition)

    def _resolve_step_order(self, definition: PipelineDefinition):
        steps_by_id = {}
        for step in definition.steps:
            if step.step_id in steps_by_id:
                raise ValueError(f"Duplicate step id: {step.step_id}")
            steps_by_id[step.step_id] = step

        ordered = []
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(step_id: str) -> None:
            if step_id in visited:
                return
            if step_id in visiting:
                raise ValueError(f"Cyclic dependency detected at step {step_id}")
            if step_id not in steps_by_id:
                raise ValueError(f"Unknown dependency step id: {step_id}")
            visiting.add(step_id)
            step = steps_by_id[step_id]
            for dependency in step.depends_on:
                visit(dependency)
            visiting.remove(step_id)
            visited.add(step_id)
            ordered.append(step)

        for step in definition.steps:
            visit(step.step_id)
        return ordered

    def _execute_step(
        self,
        kind: str,
        payload: dict[str, Any],
        config: dict[str, Any],
        *,
        source_resolver: SourceResolver | None = None,
    ) -> dict[str, Any]:
        handler = self._handlers.get(kind)
        if handler is None:
            raise ValueError(f"Unsupported step kind: {kind}")
        return handler.execute(payload, config, source_resolver=source_resolver)

    @staticmethod
    def _matches_all(item: Any, conditions: list[PipelineCondition]) -> bool:
        return all(PipelineExecutionEngine._matches(item, condition) for condition in conditions)

    @staticmethod
    def _matches(item: Any, condition: PipelineCondition) -> bool:
        exists, actual = PipelineExecutionEngine._maybe_get_path(item, condition.field)
        if not exists:
            return False
        expected = condition.value
        if condition.operator == "eq":
            return actual == expected
        if condition.operator == "ne":
            return actual != expected
        if condition.operator == "gt":
            return actual > expected
        if condition.operator == "gte":
            return actual >= expected
        if condition.operator == "lt":
            return actual < expected
        if condition.operator == "lte":
            return actual <= expected
        if condition.operator == "contains":
            return expected in actual
        if condition.operator == "in":
            return actual in expected
        raise ValueError(f"Unsupported operator: {condition.operator}")

    @staticmethod
    def _apply_aggregation(items: list[Any], aggregation: AggregationSpec) -> Any:
        if aggregation.operation == "count":
            return len(items)

        if aggregation.field is None:
            raise ValueError(f"Aggregation {aggregation.operation} requires a field")

        values = []
        for item in items:
            exists, value = PipelineExecutionEngine._maybe_get_path(item, aggregation.field)
            if exists and isinstance(value, (int, float)):
                values.append(float(value))

        if not values:
            return 0.0
        if aggregation.operation == "sum":
            return sum(values)
        if aggregation.operation == "avg":
            return sum(values) / len(values)
        if aggregation.operation == "min":
            return min(values)
        if aggregation.operation == "max":
            return max(values)
        raise ValueError(f"Unsupported aggregation: {aggregation.operation}")

    @staticmethod
    def _get_path(payload: Any, path: str, default: Any = None) -> Any:
        exists, value = PipelineExecutionEngine._maybe_get_path(payload, path)
        return value if exists else default

    @staticmethod
    def _maybe_get_path(payload: Any, path: str) -> tuple[bool, Any]:
        current = payload
        for segment in path.split("."):
            if isinstance(current, dict) and segment in current:
                current = current[segment]
            else:
                return False, None
        return True, current

    @staticmethod
    def _set_path(payload: dict[str, Any], path: str, value: Any) -> None:
        current = payload
        parts = path.split(".")
        for segment in parts[:-1]:
            next_value = current.get(segment)
            if not isinstance(next_value, dict):
                next_value = {}
                current[segment] = next_value
            current = next_value
        current[parts[-1]] = value

    @staticmethod
    def _infer_feature_fields(records: list[dict[str, Any]], label_field: str | None) -> list[str]:
        if not records:
            return []
        return [field for field in records[0].keys() if field != label_field]


def _dataset_input_from_config(config: dict[str, Any]) -> DatasetInput | None:
    payload = config.get("dataset_input")
    if payload is None:
        return None
    return DatasetInput.model_validate(payload)
