from __future__ import annotations

import importlib.util
import time
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient

from app.main import app

TORCH_AVAILABLE = importlib.util.find_spec("torch") is not None
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
class PostgresBackedModelApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)
        self.source_id = f"pagila-model-source-{uuid4().hex[:8]}"

    def test_model_job_reports_status_for_pagila_source(self) -> None:
        register = self.client.post("/api/v1/sources", json=pagila_source_payload(self.source_id))
        self.assertEqual(register.status_code, 200)

        validate = self.client.post(f"/api/v1/sources/{self.source_id}:validate")
        self.assertEqual(validate.status_code, 200)
        self.assertTrue(validate.json()["valid"])

        queued = self.client.post(
            "/api/v1/model-jobs",
            json={
                "dataset": {"source_id": self.source_id},
                "feature_fields": ["customer_id", "staff_id", "rental_id", "amount"],
                "label_field": "is_high_value",
                "model_family": "autoencoder",
                "max_rows": 512,
                "n_splits": 2,
                "use_pca": False,
                "search": {
                    "values": {
                        "hidden_dims": [[16, 8]],
                        "latent_dim": [4],
                        "dropout": [0.0],
                        "learning_rate": [0.001],
                        "weight_decay": [0.0],
                        "batch_size": [32],
                        "epochs": [2],
                        "patience": [1],
                        "input_noise_std": [0.0],
                        "pca_components": [None],
                        "beta": [1.0],
                    }
                },
            },
        )
        self.assertEqual(queued.status_code, 202)
        job_id = queued.json()["job_id"]

        terminal = None
        for _ in range(120):
            time.sleep(0.05)
            current = self.client.get(f"/api/v1/model-jobs/{job_id}")
            self.assertEqual(current.status_code, 200)
            payload = current.json()
            if payload["status"] in {"completed", "failed"}:
                terminal = payload
                break

        self.assertIsNotNone(terminal)
        assert terminal is not None
        if TORCH_AVAILABLE:
            self.assertEqual(terminal["status"], "completed")
            self.assertEqual(terminal["result"]["dataset_name"], "source-backed")
            self.assertGreaterEqual(terminal["result"]["train_rows"], 1)
        else:
            self.assertEqual(terminal["status"], "failed")
            self.assertIn("torch", (terminal.get("error") or "").lower())
