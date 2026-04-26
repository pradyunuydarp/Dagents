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
    """Abstract execution contract for one pipeline step kind.

    Each handler owns the runtime behavior for a single `kind` value from the
    pipeline definition, which keeps the main engine focused on orchestration
    instead of embedding step-specific branching.
    """

    kind: str

    def execute(
        self,
        payload: dict[str, Any],
        config: dict[str, Any],
        *,
        source_resolver: SourceResolver | None = None,
    ) -> dict[str, Any]:
        """Execute one step against the mutable pipeline payload.

        Params:
        - `payload`: shared JSON-like pipeline payload that handlers may mutate.
        - `config`: per-step configuration dictionary.
        - `source_resolver`: optional shared resolver for source-backed steps.

        Returns:
        - Dict describing the step output that is stored in run history.
        """
        raise NotImplementedError


class EnrichContextHandler(StepHandler):
    kind = "enrich_context"

    def execute(self, payload: dict[str, Any], config: dict[str, Any], *, source_resolver: SourceResolver | None = None) -> dict[str, Any]:
        """Merge static context values into a target payload object."""
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
        """Filter an item collection using declarative conditions."""
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
        """Aggregate numeric fields from an item collection into a summary object."""
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
        """Project selected payload paths into a smaller target object."""
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
        """Profile inline or source-backed data inside a pipeline step.

        Params:
        - `payload`: pipeline payload containing inline items or contextual fields.
        - `config`: step configuration, including dataset input, feature fields,
          label field, and profile output target.
        - `source_resolver`: optional shared source resolver used for source-backed
          dataset materialization.

        What it does:
        - Builds a `DatasetProfileRequest` from the step config.
        - Calls the shared profiling utility used elsewhere in the framework.
        - Stores the resulting profile back into the pipeline payload.

        Returns:
        - Dict summarizing profile target and resulting dimensions.
        """
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
        """Execute a shared model run from within a pipeline step.

        Params:
        - `payload`: mutable pipeline payload.
        - `config`: model-run configuration, including dataset source, model family,
          task type, and output target.
        - `source_resolver`: optional shared source resolver used for source-backed
          datasets.

        What it does:
        - Builds a `ModelExecutionRequest`.
        - Delegates to the shared orchestration utility used by other services.
        - Writes the resulting run metadata back into the pipeline payload.

        Returns:
        - Dict summarizing the produced model run.
        """
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
        """Create an engine with the default or injected step-handler registry.

        Params:
        - `handlers`: optional handler instances. When omitted, the engine
          registers the built-in Dagents step kinds.
        """
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
        """Execute a pipeline definition from start to finish.

        Params:
        - `definition`: validated pipeline definition containing ordered steps and
          dependencies.
        - `payload`: initial JSON-like payload provided by the caller.
        - `source_resolver`: optional shared source resolver for source-backed steps.

        What it does:
        - Deep-copies the input payload to keep caller-owned data untouched.
        - Resolves dependency order.
        - Executes each step in sequence while collecting step-level history.

        Returns:
        - `PipelineRunResponse` with final payload and per-step results.
        """
        started_at = int(time.time())
        # Handlers mutate the payload in place, so execution starts from a deep
        # copy to preserve request immutability at the API boundary.
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
        """Validate structural correctness using the OCaml binding layer and resolve dependency order."""
        from agents.common.infrastructure.dagents_runner import run_dagentsc
        try:
            run_dagentsc(["pipeline", "compile", "--input", "-", "--output", "json"], definition.model_dump(by_alias=False))
        except (RuntimeError, FileNotFoundError):
            pass # Fallback to builtin validaton layer
        self._resolve_step_order(definition)

    def _resolve_step_order(self, definition: PipelineDefinition):
        """Topologically sort the step graph and detect invalid dependencies.

        Params:
        - `definition`: pipeline definition to validate and order.

        What it does:
        - Rejects duplicate step ids.
        - Rejects missing dependencies.
        - Rejects cycles through a depth-first traversal.

        Returns:
        - Ordered list of steps ready for execution.
        """
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
            # Dependencies are visited first so the resulting list is safe for
            # single-pass execution.
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
        """Dispatch execution to the registered handler for one step kind."""
        handler = self._handlers.get(kind)
        if handler is None:
            raise ValueError(f"Unsupported step kind: {kind}")
        return handler.execute(payload, config, source_resolver=source_resolver)

    @staticmethod
    def _matches_all(item: Any, conditions: list[PipelineCondition]) -> bool:
        """Return `True` only when every declarative condition matches."""
        return all(PipelineExecutionEngine._matches(item, condition) for condition in conditions)

    @staticmethod
    def _matches(item: Any, condition: PipelineCondition) -> bool:
        """Evaluate one declarative filter condition against one payload item."""
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
        """Apply a simple aggregate function to a collection of payload items.

        Supported operations are intentionally small and deterministic so the
        pipeline layer remains easy to reason about and test.
        """
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
        """Resolve a dotted path and fall back to `default` when missing."""
        exists, value = PipelineExecutionEngine._maybe_get_path(payload, path)
        return value if exists else default

    @staticmethod
    def _maybe_get_path(payload: Any, path: str) -> tuple[bool, Any]:
        """Resolve a dotted path without raising when any segment is absent."""
        current = payload
        for segment in path.split("."):
            if isinstance(current, dict) and segment in current:
                current = current[segment]
            else:
                return False, None
        return True, current

    @staticmethod
    def _set_path(payload: dict[str, Any], path: str, value: Any) -> None:
        """Write a value into a dotted payload path, creating parent objects."""
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
        """Infer feature fields from the first record while excluding the label."""
        if not records:
            return []
        return [field for field in records[0].keys() if field != label_field]


def _dataset_input_from_config(config: dict[str, Any]) -> DatasetInput | None:
    """Parse optional `dataset_input` config into the shared Dagents model."""
    payload = config.get("dataset_input")
    if payload is None:
        return None
    return DatasetInput.model_validate(payload)
