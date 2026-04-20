from __future__ import annotations

import importlib.util
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


@unittest.skipUnless(PAGILA_AVAILABLE, "local pagila postgres dataset is required for postgres-backed check API tests")
class PostgresBackedCheckApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(app)
        self.source_id = f"pagila-checks-{uuid4().hex[:8]}"
        register = self.client.post("/api/v1/sources", json=pagila_source_payload(self.source_id))
        assert register.status_code == 200

    def test_classification_check_against_pagila_source(self) -> None:
        response = self.client.post(
            "/api/v1/checks/classification",
            json={
                "dataset": {"source_id": self.source_id},
                "feature_fields": ["customer_id", "staff_id", "rental_id", "amount"],
                "label_field": "is_high_value",
                "model_family": "random_forest",
                "max_rows": 512,
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["dataset_name"], "source-backed")
        self.assertGreaterEqual(payload["metrics"]["accuracy"], 0.5)

    def test_regression_check_against_pagila_source(self) -> None:
        response = self.client.post(
            "/api/v1/checks/regression",
            json={
                "dataset": {"source_id": self.source_id},
                "feature_fields": ["customer_id", "staff_id", "rental_id"],
                "label_field": "amount",
                "model_family": "linear",
                "max_rows": 512,
            },
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["dataset_name"], "source-backed")
        self.assertIn("rmse", payload["metrics"])

    def test_forecasting_checks_against_pagila_source(self) -> None:
        forecast_source_id = f"pagila-forecast-{uuid4().hex[:8]}"
        register = self.client.post(
            "/api/v1/sources",
            json={
                "source_id": forecast_source_id,
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
                        "select extract(epoch from payment_date)::float8 as payment_epoch, amount::float8 "
                        "from payment order by payment_date limit 512"
                    )
                },
                "format": "rows",
                "schema_hint": {},
                "batching": {"batch_size": 128, "max_records": 512},
                "checkpoint": {},
                "options": {},
            },
        )
        self.assertEqual(register.status_code, 200)

        for family in ("gru", "lstm"):
            response = self.client.post(
                "/api/v1/checks/forecasting",
                json={
                    "dataset": {"source_id": forecast_source_id},
                    "feature_fields": ["amount"],
                    "label_field": "amount",
                    "model_family": family,
                    "max_rows": 512,
                    "sequence_length": 12,
                },
            )
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload["dataset_name"], "source-backed")
            self.assertEqual(payload["model_family"], family)
            self.assertIn("rmse", payload["metrics"])


if __name__ == "__main__":
    unittest.main()
