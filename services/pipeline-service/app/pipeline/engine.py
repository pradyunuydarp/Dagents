"""Execution engine for Dagents pipelines."""

from __future__ import annotations

from copy import deepcopy
import time
from typing import Any

from app.models import AggregationSpec, PipelineCondition, PipelineDefinition, PipelineRunResponse, StepRunResult


class PipelineExecutionEngine:
    """Executes registered pipeline definitions against JSON-like payloads."""

    def execute(self, definition: PipelineDefinition, payload: dict[str, Any]) -> PipelineRunResponse:
        started_at = int(time.time())
        working_payload = deepcopy(payload)
        step_results: list[StepRunResult] = []

        for step in self._resolve_step_order(definition):
            step_started_at = int(time.time())
            output = self._execute_step(step.kind, working_payload, step.config)
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

    def _execute_step(self, kind: str, payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        if kind == "enrich_context":
            return self._enrich_context(payload, config)
        if kind == "filter_items":
            return self._filter_items(payload, config)
        if kind == "summarize_items":
            return self._summarize_items(payload, config)
        if kind == "project_fields":
            return self._project_fields(payload, config)
        raise ValueError(f"Unsupported step kind: {kind}")

    def _enrich_context(self, payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        target_field = str(config.get("target_field", "context"))
        values = dict(config.get("values", {}))
        current = self._get_path(payload, target_field, default={})
        if not isinstance(current, dict):
            raise ValueError(f"Target field {target_field} must resolve to an object")
        merged = {**current, **values}
        self._set_path(payload, target_field, merged)
        return {"target_field": target_field, "merged_keys": sorted(values)}

    def _filter_items(self, payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        items_field = str(config.get("items_field", "items"))
        items = list(self._get_path(payload, items_field, default=[]))
        conditions = [PipelineCondition(**entry) for entry in config.get("conditions", [])]
        filtered = [item for item in items if self._matches_all(item, conditions)]
        self._set_path(payload, items_field, filtered)
        return {"items_field": items_field, "input_count": len(items), "output_count": len(filtered)}

    def _summarize_items(self, payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        items_field = str(config.get("items_field", "items"))
        target_field = str(config.get("target_field", "summary"))
        items = list(self._get_path(payload, items_field, default=[]))
        aggregations = [AggregationSpec(**entry) for entry in config.get("aggregations", [])]
        summary: dict[str, Any] = {}
        for aggregation in aggregations:
            key = aggregation.alias or aggregation.field or aggregation.operation
            summary[key] = self._apply_aggregation(items, aggregation)
        self._set_path(payload, target_field, summary)
        return {"target_field": target_field, "summary_keys": sorted(summary)}

    def _project_fields(self, payload: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        target_field = str(config.get("target_field", "projection"))
        fields = list(config.get("fields", []))
        projection = {}
        for field in fields:
            exists, value = self._maybe_get_path(payload, field)
            if exists:
                projection[field] = value
        self._set_path(payload, target_field, projection)
        return {"target_field": target_field, "projected_fields": sorted(projection)}

    def _matches_all(self, item: Any, conditions: list[PipelineCondition]) -> bool:
        return all(self._matches(item, condition) for condition in conditions)

    def _matches(self, item: Any, condition: PipelineCondition) -> bool:
        exists, actual = self._maybe_get_path(item, condition.field)
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

    def _apply_aggregation(self, items: list[Any], aggregation: AggregationSpec) -> Any:
        if aggregation.operation == "count":
            return len(items)

        if aggregation.field is None:
            raise ValueError(f"Aggregation {aggregation.operation} requires a field")

        values = []
        for item in items:
            exists, value = self._maybe_get_path(item, aggregation.field)
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

    def _get_path(self, payload: Any, path: str, default: Any = None) -> Any:
        exists, value = self._maybe_get_path(payload, path)
        return value if exists else default

    def _maybe_get_path(self, payload: Any, path: str) -> tuple[bool, Any]:
        current = payload
        for segment in path.split("."):
            if isinstance(current, dict) and segment in current:
                current = current[segment]
            else:
                return False, None
        return True, current

    def _set_path(self, payload: dict[str, Any], path: str, value: Any) -> None:
        current = payload
        parts = path.split(".")
        for segment in parts[:-1]:
            next_value = current.get(segment)
            if not isinstance(next_value, dict):
                next_value = {}
                current[segment] = next_value
            current = next_value
        current[parts[-1]] = value
