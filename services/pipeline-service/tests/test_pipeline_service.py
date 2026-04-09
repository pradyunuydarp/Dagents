"""Unit tests for the pipeline service."""

from __future__ import annotations

import time
import unittest

from app.models import PipelineDefinition, PipelineRunRequest, PipelineStep
from app.pipeline.engine import PipelineExecutionEngine
from app.services.pipeline_service import (
    InMemoryPipelineDefinitionRepository,
    InMemoryPipelineRunRepository,
    PipelineService,
)


class PipelineServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = PipelineService(
            definitions=InMemoryPipelineDefinitionRepository(),
            runs=InMemoryPipelineRunRepository(),
            engine=PipelineExecutionEngine(),
        )

    def test_register_and_run_pipeline(self) -> None:
        pipeline = PipelineDefinition(
            pipeline_id="incident-triage",
            description="Filter and summarize incidents",
            steps=[
                PipelineStep(
                    step_id="enrich",
                    kind="enrich_context",
                    config={"target_field": "context", "values": {"tenant": "alpha"}},
                ),
                PipelineStep(
                    step_id="filter",
                    kind="filter_items",
                    depends_on=["enrich"],
                    config={
                        "items_field": "items",
                        "conditions": [{"field": "severity", "operator": "gte", "value": 4}],
                    },
                ),
                PipelineStep(
                    step_id="summarize",
                    kind="summarize_items",
                    depends_on=["filter"],
                    config={
                        "items_field": "items",
                        "target_field": "summary",
                        "aggregations": [
                            {"operation": "count", "alias": "incident_count"},
                            {"operation": "avg", "field": "score", "alias": "avg_score"},
                            {"operation": "max", "field": "score", "alias": "max_score"},
                        ],
                    },
                ),
                PipelineStep(
                    step_id="profile",
                    kind="profile_dataset",
                    depends_on=["summarize"],
                    config={
                        "items_field": "items",
                        "target_field": "dataset_profile",
                        "feature_fields": ["severity", "score"],
                    },
                ),
                PipelineStep(
                    step_id="model",
                    kind="run_model_job",
                    depends_on=["profile"],
                    config={
                        "items_field": "items",
                        "target_field": "model_run",
                        "feature_fields": ["severity", "score"],
                        "model_family": "autoencoder",
                        "task_type": "anomaly_detection",
                        "scope_kind": "source",
                        "artifact_prefix": "artifacts/runs",
                    },
                ),
            ],
        )

        self.service.register_pipeline(pipeline)
        result = self.service.run_pipeline(
            "incident-triage",
            PipelineRunRequest(
                payload={
                    "items": [
                        {"severity": 5, "score": 0.9, "id": "a"},
                        {"severity": 2, "score": 0.2, "id": "b"},
                        {"severity": 4, "score": 0.7, "id": "c"},
                    ]
                }
            ),
        )

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.final_payload["context"]["tenant"], "alpha")
        self.assertEqual(len(result.final_payload["items"]), 2)
        self.assertEqual(result.final_payload["summary"]["incident_count"], 2)
        self.assertAlmostEqual(result.final_payload["summary"]["avg_score"], 0.8)
        self.assertEqual(result.final_payload["dataset_profile"]["record_count"], 2)
        self.assertEqual(result.final_payload["model_run"]["model_family"], "autoencoder")

    def test_rejects_unknown_dependency(self) -> None:
        pipeline = PipelineDefinition(
            pipeline_id="broken",
            steps=[
                PipelineStep(
                    step_id="project",
                    kind="project_fields",
                    depends_on=["missing"],
                    config={"fields": ["foo"]},
                )
            ],
        )

        with self.assertRaises(ValueError):
            self.service.register_pipeline(pipeline)

    def test_submits_async_pipeline_run(self) -> None:
        self.service.register_pipeline(
            PipelineDefinition(
                pipeline_id="async-pipeline",
                steps=[
                    PipelineStep(
                        step_id="enrich",
                        kind="enrich_context",
                        config={"target_field": "context", "values": {"tenant": "alpha"}},
                    )
                ],
            )
        )

        queued = self.service.submit_pipeline_run(
            PipelineRunRequest(
                pipeline_id="async-pipeline",
                payload={"items": []},
            )
        )
        self.assertEqual(queued.status, "queued")

        completed = None
        for _ in range(20):
            time.sleep(0.01)
            completed = self.service.get_run(queued.run_id)
            if completed and completed.status == "completed":
                break

        self.assertIsNotNone(completed)
        assert completed is not None
        self.assertEqual(completed.status, "completed")
        self.assertEqual(completed.final_payload["context"]["tenant"], "alpha")


if __name__ == "__main__":
    unittest.main()
