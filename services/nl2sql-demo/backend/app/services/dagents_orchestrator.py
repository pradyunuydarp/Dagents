"""Dagents service and functional-planner orchestration for NL2SQL."""

from __future__ import annotations

from typing import Any

import httpx

from agents.common.infrastructure.dagents_runner import (
    compile_dataset_extraction,
    evaluate_dataset_quality,
    run_dagentsc,
    validate_dataset_schema,
    validate_dataset_source,
)
from app.core.config import Settings
from app.models import DagentsBackendStatus, DagentsServiceStatus, DagentsTraceStep, SchemaTable
from app.services.schema import schema_contract, schema_records


class DagentsOrchestrator:
    """Coordinates Dagents planners and services for the NL2SQL demo."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def backend_status(self) -> DagentsBackendStatus:
        services = [
            ("core-service", f"{self.settings.core_service_url}/api/v1/health"),
            ("pipeline-service", f"{self.settings.pipeline_service_url}/api/v1/health"),
            ("model-service", f"{self.settings.model_service_url}/api/v1/health"),
            ("lma", f"{self.settings.lma_url}/health"),
            ("gma", f"{self.settings.gma_url}/health"),
        ]
        statuses: list[DagentsServiceStatus] = []
        with httpx.Client(timeout=self.settings.request_timeout_seconds) as client:
            for name, url in services:
                try:
                    response = client.get(url)
                    response.raise_for_status()
                    statuses.append(DagentsServiceStatus(name=name, url=url, status="ok", detail="reachable"))
                except Exception as exc:
                    statuses.append(DagentsServiceStatus(name=name, url=url, status="unavailable", detail=str(exc)))
        return DagentsBackendStatus(services=statuses)

    def functional_trace(self, tables: list[SchemaTable], question: str) -> list[DagentsTraceStep]:
        records = schema_records(tables)
        trace: list[DagentsTraceStep] = []

        inline_source = {
            "source_id": "nl2sql-schema-source",
            "kind": "inline",
            "selection": {"records": records},
            "format": "rows",
            "batching": {"batch_size": 1000, "max_records": len(records) or 1},
        }

        self._append_trace(trace, "Dagents SourceSpec validation", lambda: validate_dataset_source(inline_source))
        self._append_trace(trace, "Dagents extraction planning", lambda: compile_dataset_extraction(inline_source))
        self._append_trace(trace, "Dagents schema contract validation", lambda: validate_dataset_schema(records, schema_contract()))
        self._append_trace(
            trace,
            "Dagents quality rules",
            lambda: evaluate_dataset_quality(
                records,
                [
                    {"rule_id": "table_present", "field": "table_name", "operator": "non_null", "severity": "error"},
                    {"rule_id": "column_present", "field": "column_name", "operator": "non_null", "severity": "error"},
                    {"rule_id": "dtype_present", "field": "dtype", "operator": "non_null", "severity": "warning"},
                ],
            ),
        )
        self._append_trace(trace, "Dagents pipeline DAG planning", lambda: self._pipeline_plan_payload())
        self._append_trace(trace, "Dagents model route planning", lambda: run_dagentsc(["model", "route", "--task", "embedding", "--output", "json"], {}))
        self._append_service_trace(trace, records, question)
        self._append_trace(trace, "Dagents service status", lambda: self.backend_status().model_dump())
        return trace

    def _append_trace(self, trace: list[DagentsTraceStep], name: str, action) -> None:
        try:
            payload = action()
            trace.append(DagentsTraceStep(name=name, status="ok", detail="completed", payload=payload))
        except Exception as exc:
            trace.append(DagentsTraceStep(name=name, status="warning", detail=str(exc), payload=None))

    def _append_service_trace(self, trace: list[DagentsTraceStep], records: list[dict[str, Any]], question: str) -> None:
        """Touch the Dagents service stack with lightweight orchestration requests."""
        self._append_trace(trace, "core-service catalog and topology", lambda: self._core_catalog_and_topology())
        self._append_trace(trace, "core-service workload compile", lambda: self._compile_demo_workload())
        self._append_trace(trace, "pipeline-service register and run", lambda: self._register_and_run_pipeline(records, question))
        self._append_trace(trace, "model-service catalog", lambda: self._model_service_catalog())
        self._append_trace(trace, "LMA source profile", lambda: self._agent_profile(self.settings.lma_url, "source", records))
        self._append_trace(trace, "GMA assimilated profile", lambda: self._agent_profile(self.settings.gma_url, "assimilated", records))

    def _pipeline_plan_payload(self) -> dict[str, Any]:
        return run_dagentsc(
            ["pipeline", "compile", "--input", "-", "--output", "json"],
            {
                "pipeline_id": "nl2sql-generation",
                "steps": [
                    {"step_id": "validate_schema", "kind": "profile_dataset", "depends_on": []},
                    {"step_id": "generate_sql", "kind": "run_model_job", "depends_on": ["validate_schema"]},
                    {"step_id": "render_result", "kind": "project_fields", "depends_on": ["generate_sql"]},
                ],
            },
        )

    def _core_catalog_and_topology(self) -> dict[str, Any]:
        return {
            "services": self._get_json(f"{self.settings.core_service_url}/api/v1/services"),
            "topology": self._get_json(f"{self.settings.core_service_url}/api/v1/topology"),
        }

    def _compile_demo_workload(self) -> dict[str, Any]:
        return self._post_json(
            f"{self.settings.core_service_url}/api/v1/workloads:compile",
            {
                "plan_id": "nl2sql-demo",
                "namespace": "dagents-demo",
                "include_services": True,
                "include_config_maps": True,
                "components": [
                    {
                        "name": "nl2sql-demo-backend",
                        "image": "dagents/nl2sql-demo-backend:local",
                        "kind": "Deployment",
                        "replicas": 1,
                        "ports": [{"name": "http", "container_port": self.settings.api_port}],
                        "env": [{"name": "NL2SQL_DEFAULT_MODEL_ID", "value": self.settings.default_model_id}],
                        "generated_resources": ["Service", "ConfigMap"],
                        "config_map_data": {"dagents.integration": "core,pipeline,model,lma,gma,ocaml"},
                    }
                ],
            },
        )

    def _register_and_run_pipeline(self, records: list[dict[str, Any]], question: str) -> dict[str, Any]:
        pipeline_id = "nl2sql-demo-schema-flow"
        definition = {
            "pipeline_id": pipeline_id,
            "description": "Profiles schema records before NL2SQL model selection.",
            "steps": [
                {
                    "step_id": "attach_question",
                    "kind": "enrich_context",
                    "depends_on": [],
                    "config": {"target_field": "context", "values": {"question": question}},
                },
                {
                    "step_id": "profile_schema_records",
                    "kind": "profile_dataset",
                    "depends_on": ["attach_question"],
                    "config": {
                        "items_field": "schema_records",
                        "target_field": "schema_profile",
                        "scope_id": "nl2sql-schema",
                        "scope_kind": "source",
                        "feature_fields": ["table_name", "column_name", "dtype"],
                    },
                },
                {
                    "step_id": "summarize_columns",
                    "kind": "summarize_items",
                    "depends_on": ["profile_schema_records"],
                    "config": {
                        "items_field": "schema_records",
                        "target_field": "schema_summary",
                        "aggregations": [{"operation": "count", "alias": "column_count"}],
                    },
                },
                {
                    "step_id": "project_generation_context",
                    "kind": "project_fields",
                    "depends_on": ["summarize_columns"],
                    "config": {
                        "target_field": "dagents_projection",
                        "fields": ["context.question", "schema_profile.record_count", "schema_summary.column_count"],
                    },
                },
            ],
        }
        registered = self._post_json(f"{self.settings.pipeline_service_url}/api/v1/pipelines", definition)
        run = self._post_json(
            f"{self.settings.pipeline_service_url}/api/v1/pipelines/{pipeline_id}/runs",
            {"payload": {"schema_records": records, "context": {}}},
        )
        return {"registered": registered, "run": run}

    def _model_service_catalog(self) -> dict[str, Any]:
        return {
            "datasets": self._get_json(f"{self.settings.model_service_url}/api/v1/datasets"),
            "recent_jobs": self._get_json(f"{self.settings.model_service_url}/api/v1/model-jobs?limit=5"),
        }

    def _agent_profile(self, base_url: str, scope_kind: str, records: list[dict[str, Any]]) -> dict[str, Any]:
        return self._post_json(
            f"{base_url}/api/v1/datasets:profile",
            {
                "scope_id": f"nl2sql-schema-{scope_kind}",
                "scope_kind": scope_kind,
                "extraction_strategy": "tabular",
                "records": records,
                "feature_fields": ["table_name", "column_name", "dtype"],
                "batch_size": 1000,
            },
        )

    def _get_json(self, url: str) -> dict[str, Any]:
        with httpx.Client(timeout=self.settings.request_timeout_seconds) as client:
            response = client.get(url)
            response.raise_for_status()
            return response.json()

    def _post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        with httpx.Client(timeout=self.settings.request_timeout_seconds) as client:
            response = client.post(url, json=payload)
            response.raise_for_status()
            return response.json()
