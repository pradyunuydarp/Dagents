from __future__ import annotations

import importlib.util
import time
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient

from agents.gma.main import app as gma_app
from agents.lma.main import app as lma_app

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
        "schema_hint": {
            "customer_id": "integer",
            "staff_id": "integer",
            "rental_id": "integer",
            "amount": "float",
            "is_high_value": "integer",
        },
        "batching": {"batch_size": 128, "max_records": 512},
        "checkpoint": {},
        "options": {},
    }


@unittest.skipUnless(PAGILA_AVAILABLE, "local pagila postgres dataset is required for postgres-backed API tests")
class PostgresBackedAgentApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.lma = TestClient(lma_app)
        self.gma = TestClient(gma_app)
        self.source_id = f"pagila-source-{uuid4().hex[:8]}"
        self.payload = pagila_source_payload(self.source_id)

    def test_lma_profiles_and_runs_model_against_pagila_source(self) -> None:
        register = self.lma.post("/api/v1/sources", json=self.payload)
        self.assertEqual(register.status_code, 200)

        validate = self.lma.post(f"/api/v1/sources/{self.source_id}:validate")
        self.assertEqual(validate.status_code, 200)
        self.assertTrue(validate.json()["valid"])

        profile = self.lma.post(
            "/api/v1/datasets:profile",
            json={
                "scope_id": "pagila-lma",
                "dataset": {"source_id": self.source_id},
                "feature_fields": ["customer_id", "staff_id", "rental_id", "amount"],
                "label_field": "is_high_value",
                "batch_size": 128,
            },
        )
        self.assertEqual(profile.status_code, 200)
        profile_payload = profile.json()
        self.assertEqual(profile_payload["record_count"], 512)
        self.assertEqual(profile_payload["partition_count"], 4)

        model = self.lma.post(
            "/api/v1/model-jobs",
            json={
                "scope_id": "pagila-lma",
                "dataset": {"source_id": self.source_id},
                "feature_fields": ["customer_id", "staff_id", "rental_id", "amount"],
                "label_field": "is_high_value",
                "task_type": "classification",
                "model_family": "random_forest",
                "hyperparameters": {"batch_size": 128},
                "artifact_prefix": "artifacts/tests",
                "model_version": "pagila",
            },
        )
        self.assertEqual(model.status_code, 202)
        run_payload = model.json()
        self.assertEqual(run_payload["run"]["record_count"], 512)
        self.assertEqual(run_payload["dataset_profile"]["record_count"], 512)

    def test_gma_profiles_and_runs_model_against_pagila_source(self) -> None:
        register = self.gma.post("/api/v1/sources", json=self.payload)
        self.assertEqual(register.status_code, 200)

        profile = self.gma.post(
            "/api/v1/datasets:profile",
            json={
                "scope_id": "pagila-gma",
                "scope_kind": "assimilated",
                "dataset": {"source_id": self.source_id},
                "feature_fields": ["customer_id", "staff_id", "rental_id", "amount"],
                "label_field": "is_high_value",
                "batch_size": 128,
            },
        )
        self.assertEqual(profile.status_code, 200)
        self.assertEqual(profile.json()["record_count"], 512)

        model = self.gma.post(
            "/api/v1/model-jobs",
            json={
                "scope_id": "pagila-gma",
                "scope_kind": "assimilated",
                "dataset": {"source_id": self.source_id},
                "feature_fields": ["customer_id", "staff_id", "rental_id", "amount"],
                "label_field": "is_high_value",
                "task_type": "classification",
                "model_family": "xgboost",
                "hyperparameters": {"batch_size": 128},
                "artifact_prefix": "artifacts/tests",
                "model_version": "pagila",
            },
        )
        self.assertEqual(model.status_code, 202)
        run_payload = model.json()
        self.assertEqual(run_payload["run"]["scope_kind"], "assimilated")
        self.assertEqual(run_payload["run"]["record_count"], 512)
