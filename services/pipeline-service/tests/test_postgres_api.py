from __future__ import annotations

import importlib.util
import time
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import app

PSYCOPG_AVAILABLE = importlib.util.find_spec("psycopg") is not None

if PSYCOPG_AVAILABLE:
    import psycopg


def pagila_available() -> bool:
    if not PSYCOPG_AVAILABLE:
        return False
    try:
        with psycopg.connect(dbname="pagila", user="pradyundevarakonda", host="127.0.0.1", port=5432) as connection:
            with connection.cursor() as cursor:
                cursor.execute("select 1")
                cursor.fetchone()
        return True
    except Exception:
        return False


PAGILA_AVAILABLE = pagila_available()


def pagila_source_payload(source_id: str) -> dict[str, object]:
    return {
        "source_id": source_id,
        "kind": "postgres",
        "connection_ref": {
            "connection_id": "local-pagila",
            "options": {
                "dbname": "pagila",
                "user": "pradyundevarakonda",
                "host": "127.0.0.1",
                "port": 5432,
            },
        },
        "selection": {
            "sql": (
                "select customer_id::int, staff_id::int, rental_id::int, amount::float8, "
                "case when amount >= 8 then 1 else 0 end as is_high_value "
                "from payment order by payment_id limit 512"
            )
        },
        "format": "rows",
        "schema_hint": {},
        "batching": {"batch_size": 128, "max_records": 512},
        "checkpoint": {},
        "options": {},
    }


@unittest.skipUnless(PAGILA_AVAILABLE, "local pagila postgres dataset is required for postgres-backed API tests")
class PostgresBackedPipelineApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)
        self.source_id = f"pagila-pipeline-source-{uuid4().hex[:8]}"
        self.pipeline_id = f"pagila-pipeline-{uuid4().hex[:8]}"

    def test_pipeline_runs_profile_and_model_steps_against_pagila_source(self) -> None:
        register = self.client.post("/api/v1/sources", json=pagila_source_payload(self.source_id))
        self.assertEqual(register.status_code, 200)

        definition = {
            "pipeline_id": self.pipeline_id,
            "description": "Profile and score pagila payment data",
            "steps": [
                {
                    "step_id": "profile",
                    "kind": "profile_dataset",
                    "config": {
                        "target_field": "dataset_profile",
                        "dataset_input": {"source_id": self.source_id},
                        "feature_fields": ["customer_id", "staff_id", "rental_id", "amount"],
                        "label_field": "is_high_value",
                        "batch_size": 128,
                    },
                },
                {
                    "step_id": "model",
                    "kind": "run_model_job",
                    "depends_on": ["profile"],
                    "config": {
                        "target_field": "model_run",
                        "dataset_input": {"source_id": self.source_id},
                        "feature_fields": ["customer_id", "staff_id", "rental_id", "amount"],
                        "label_field": "is_high_value",
                        "task_type": "classification",
                        "model_family": "random_forest",
                        "artifact_prefix": "artifacts/tests",
                    },
                },
            ],
        }
        create_pipeline = self.client.post("/api/v1/pipeline-definitions", json=definition)
        self.assertEqual(create_pipeline.status_code, 200)

        queued = self.client.post("/api/v1/pipeline-runs", json={"pipeline_id": self.pipeline_id, "payload": {}})
        self.assertEqual(queued.status_code, 202)
        run_id = queued.json()["run_id"]

        completed = None
        for _ in range(60):
            time.sleep(0.05)
            current = self.client.get(f"/api/v1/pipeline-runs/{run_id}")
            self.assertEqual(current.status_code, 200)
            if current.json()["status"] == "completed":
                completed = current.json()
                break

        self.assertIsNotNone(completed)
        assert completed is not None
        self.assertEqual(completed["final_payload"]["dataset_profile"]["record_count"], 512)
        self.assertEqual(completed["final_payload"]["model_run"]["record_count"], 512)
        self.assertEqual(completed["final_payload"]["model_run"]["model_family"], "random_forest")
