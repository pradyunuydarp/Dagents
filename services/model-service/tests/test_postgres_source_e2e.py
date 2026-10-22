"""A Postgres source read by a running model service.

The service runs as its own uvicorn process, as it does when deployed, and is
called over HTTP. The test registers a Postgres source with the login and
password of a scratch database on a real PostgreSQL 16 server. An asynchronous
model job trains on the table, and the classification, regression and
forecasting checks score models on it. It also checks that no answer about the
source shows the login, the password or the database name.

The table is built so that each check has a known result:

- ``is_high_value`` is 1 when ``amount`` is 8 or more, so a random forest
  that sees ``amount`` classifies every held-out row correctly.
- ``late_fee`` is ``1.5 * days_late + 0.25 * staff_id``, so a linear
  regression on those two fields predicts it exactly.
- ``demand`` is a sine wave with a period of 24 rows, so each value follows
  from the twelve before it, and a small recurrent model learns it in the
  check's 12 epochs.

It needs ``DAGENTS_TEST_DATABASE_URL``: a connection string for a superuser
login on a PostgreSQL 16 server, over TCP with a password. The test creates a
scratch database and a login role with ``ScratchDatabase`` from the agents'
role tests, and drops both afterwards. Without the variable the class skips
with a message that starts with ``database test skipped``. The CI job that runs
this suite has a PostgreSQL service and fails on that skip.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any
import unittest
import urllib.error
import urllib.request

from dagents.tests.test_postgres_roles import ScratchDatabase, admin_dsn_or_skip

try:
    import psycopg
    from psycopg.conninfo import conninfo_to_dict
except ModuleNotFoundError:  # pragma: no cover - the service's requirements install it
    psycopg = None

SERVICE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]

#: Rows in the table. The source reads at most ``MAX_RECORDS`` of them. The
#: requests below set no ``max_rows`` of their own, so the row counts they
#: report show that the source's limit was applied.
TABLE_ROWS = 600
MAX_RECORDS = 512
BATCH_SIZE = 128
SOURCE_ID = "scratch-payments"
ARTIFACT_NAME = "scratch-payments-autoencoder.pt"

#: How long the service may take to answer its health check (it imports torch
#: at start), a model job to finish, and one check to answer. Each has a margin
#: for a cold CI runner.
START_TIMEOUT_S = 120.0
JOB_TIMEOUT_S = 300.0
CHECK_TIMEOUT_S = 120.0

#: What a source answer shows of the connection options: every value hidden
#: except ``connect_timeout``, which says nothing about where the database is.
REDACTED_OPTIONS = {
    "host": "[redacted]",
    "port": "[redacted]",
    "dbname": "[redacted]",
    "user": "[redacted]",
    "password": "[redacted]",
    "connect_timeout": 10,
}

# Requests go straight to the service on loopback, never through a proxy that
# the environment configures.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def payment_rows() -> list[tuple[Any, ...]]:
    """The table's rows, in the column order of :func:`create_payments_table`."""
    rows = []
    for payment_id in range(1, TABLE_ROWS + 1):
        staff_id = 1 + payment_id % 2
        days_late = (payment_id * 3) % 8
        amount = (payment_id * 7) % 12 + 0.99
        rows.append(
            (
                payment_id,
                (payment_id * 37) % 599 + 1,
                staff_id,
                1000 + payment_id,
                days_late,
                amount,
                1.5 * days_late + 0.25 * staff_id,
                int(amount >= 8),
                math.sin(2 * math.pi * payment_id / 24),
            )
        )
    return rows


def create_payments_table(dsn: str) -> None:
    """Create ``source_data.payments`` as the scratch database's login role, which owns it."""
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute("CREATE SCHEMA source_data")
        connection.execute(
            "CREATE TABLE source_data.payments (payment_id int PRIMARY KEY, customer_id int NOT NULL, "
            "staff_id int NOT NULL, rental_id int NOT NULL, days_late int NOT NULL, "
            "amount double precision NOT NULL, late_fee double precision NOT NULL, is_high_value int NOT NULL, "
            "demand double precision NOT NULL)"
        )
        with connection.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO source_data.payments VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)", payment_rows()
            )


def postgres_source(dsn: str) -> dict[str, Any]:
    """A source definition whose connection options come from ``dsn``, password included."""
    parts = conninfo_to_dict(dsn)
    return {
        "source_id": SOURCE_ID,
        "kind": "postgres",
        "connection_ref": {
            "connection_id": "scratch-database",
            "options": {
                "host": parts.get("host", "127.0.0.1"),
                "port": int(parts.get("port", 5432)),
                "dbname": parts["dbname"],
                "user": parts["user"],
                "password": parts["password"],
                "connect_timeout": 10,
            },
        },
        "selection": {
            "sql": (
                "SELECT customer_id, staff_id, rental_id, days_late, amount, late_fee, is_high_value, demand "
                "FROM source_data.payments ORDER BY payment_id"
            )
        },
        "batching": {"batch_size": BATCH_SIZE, "max_records": MAX_RECORDS},
    }


def call(base_url: str, method: str, path: str, body: Any = None, timeout: float = 30.0) -> tuple[int, str]:
    """Send one request and return the status and the body as text."""
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {} if data is None else {"Content-Type": "application/json"}
    request = urllib.request.Request(base_url + path, data=data, method=method, headers=headers)
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode("utf-8", errors="replace")


class ServiceProcess:
    """The service in a uvicorn process of its own, in a process group of its own."""

    def __init__(self, work_dir: Path) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(64)
        self.base_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        self.log = work_dir / "model-service.log"
        self.artifact_dir = work_dir / "artifacts"
        environment = {
            key: value
            for key, value in os.environ.items()
            # libpq reads PG* variables; the source's own options must be all it uses.
            if not key.startswith("PG") and key.lower() not in ("http_proxy", "https_proxy", "all_proxy")
        }
        environment.update(
            PYTHONPATH=os.pathsep.join([str(REPO_ROOT), str(SERVICE_ROOT)]),
            MODEL_ARTIFACT_DIR=str(self.artifact_dir),
            NO_PROXY="127.0.0.1,localhost",
            no_proxy="127.0.0.1,localhost",
        )
        try:
            with self.log.open("wb") as handle:
                self.process = subprocess.Popen(
                    [sys.executable, "-m", "uvicorn", "app.main:app", "--fd", str(listener.fileno()),
                     "--log-level", "warning"],
                    cwd=work_dir,
                    env=environment,
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    pass_fds=(listener.fileno(),),
                    start_new_session=True,
                )
        finally:
            # The child holds its own copy of the listening socket.
            listener.close()

    def log_tail(self) -> str:
        return self.log.read_text(errors="replace")[-3000:]

    def wait_until_healthy(self, timeout_s: float) -> None:
        deadline = time.monotonic() + timeout_s
        while True:
            if self.process.poll() is not None:
                raise AssertionError(f"the service exited with {self.process.returncode}:\n{self.log_tail()}")
            try:
                status, _ = call(self.base_url, "GET", "/api/v1/health", timeout=5.0)
                if status == 200:
                    return
            except OSError:
                pass
            if time.monotonic() > deadline:
                raise AssertionError(f"the service did not answer /api/v1/health in {timeout_s} s:\n{self.log_tail()}")
            time.sleep(0.2)

    def stop(self) -> None:
        """Stop the whole process group, and kill it if it does not stop in 10 seconds."""
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
                self.process.wait(timeout=10)
            except ProcessLookupError:
                pass
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=10)
        # A child the service started would keep running after the service stopped.
        try:
            os.killpg(self.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def start_database() -> ScratchDatabase:
    """A scratch database with the payments table, or a skip without a test database."""
    database = ScratchDatabase(admin_dsn_or_skip())
    try:
        database.start()
        create_payments_table(database.dsn)
    except BaseException:
        database.stop()
        raise
    return database


class PostgresModelSourceTests(unittest.TestCase):
    """Register a Postgres source with the running service, then train and check models on it."""

    @classmethod
    def setUpClass(cls) -> None:
        database = start_database()
        cls.addClassCleanup(database.stop)
        cls.secrets = {
            "password": conninfo_to_dict(database.dsn)["password"],
            "login": database.owner,
            "database name": database.name,
        }
        folder = tempfile.TemporaryDirectory(prefix="dagents-model-pg-")
        cls.addClassCleanup(folder.cleanup)
        cls.service = ServiceProcess(Path(folder.name))
        cls.addClassCleanup(cls.service.stop)
        cls.service.wait_until_healthy(START_TIMEOUT_S)
        cls.registered = call(cls.service.base_url, "POST", "/api/v1/sources", postgres_source(database.dsn))

    def request(self, method: str, path: str, body: Any = None, timeout: float = 30.0) -> tuple[int, str]:
        return call(self.service.base_url, method, path, body, timeout)

    def check(self, task: str, body: dict[str, Any]) -> dict[str, Any]:
        """Run one check on the registered source and return its answer."""
        status, text = self.request(
            "POST",
            f"/api/v1/checks/{task}",
            {"dataset": {"source_id": SOURCE_ID}, **body},
            timeout=CHECK_TIMEOUT_S,
        )
        self.assertEqual(200, status, text)
        self.assert_shows_no_secret(text)
        answer = json.loads(text)
        self.assertEqual("source-backed", answer["dataset_name"])
        return answer

    def assert_shows_no_secret(self, text: str) -> None:
        for name, value in self.secrets.items():
            self.assertNotIn(value, text, f"the answer shows the {name}")

    def test_the_source_answers_hide_the_login(self) -> None:
        status, registered = self.registered
        self.assertEqual(200, status, registered)
        status, fetched = self.request("GET", f"/api/v1/sources/{SOURCE_ID}")
        self.assertEqual(200, status, fetched)
        status, listed = self.request("GET", "/api/v1/sources")
        self.assertEqual(200, status, listed)
        for answer, text in {"register": registered, "fetch": fetched, "list": listed}.items():
            with self.subTest(answer=answer):
                self.assert_shows_no_secret(text)
        self.assertEqual(REDACTED_OPTIONS, json.loads(registered)["connection_ref"]["options"])
        self.assertEqual(REDACTED_OPTIONS, json.loads(fetched)["connection_ref"]["options"])
        [source] = [item for item in json.loads(listed) if item["source_id"] == SOURCE_ID]
        self.assertEqual(REDACTED_OPTIONS, source["connection_ref"]["options"])

    def test_validation_accepts_the_source(self) -> None:
        status, text = self.request("POST", f"/api/v1/sources/{SOURCE_ID}:validate")
        self.assertEqual(200, status, text)
        self.assertEqual({"valid": True, "errors": [], "warnings": []}, json.loads(text))

    def test_an_async_model_job_trains_on_the_table(self) -> None:
        # A redacted answer must not replace the stored definition: after this
        # fetch, the job still has to log in with the real password.
        status, text = self.request("GET", f"/api/v1/sources/{SOURCE_ID}")
        self.assertEqual(200, status, text)
        features = ["customer_id", "staff_id", "rental_id", "amount"]
        status, text = self.request(
            "POST",
            "/api/v1/model-jobs",
            {
                "dataset": {"source_id": SOURCE_ID},
                "feature_fields": features,
                "label_field": "is_high_value",
                "model_family": "autoencoder",
                "n_splits": 2,
                "use_pca": False,
                "artifact_name": ARTIFACT_NAME,
                # One small candidate, so the job takes seconds.
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
        self.assertEqual(202, status, text)
        queued = json.loads(text)
        self.assertEqual("queued", queued["status"])

        job, text = self.wait_for_job(queued["job_id"])
        self.assertEqual("completed", job["status"], job.get("error"))
        self.assert_shows_no_secret(text)
        result = job["result"]
        self.assertEqual("source-backed", result["dataset_name"])
        self.assertEqual("autoencoder", result["model_family"])
        self.assertEqual(len(features), result["input_dim"])
        # The training and held-out rows together are the source's 512 rows.
        self.assertEqual(MAX_RECORDS, result["train_rows"] + result["test_rows"])
        artifact = self.service.artifact_dir / ARTIFACT_NAME
        self.assertTrue(artifact.is_file(), f"no model file at {artifact}")
        self.assertEqual(str(artifact), result["artifact_path"])

    def test_the_classification_check_learns_the_amount_threshold(self) -> None:
        answer = self.check(
            "classification",
            {
                "feature_fields": ["customer_id", "staff_id", "rental_id", "amount"],
                "label_field": "is_high_value",
                "model_family": "random_forest",
            },
        )
        self.assertEqual(MAX_RECORDS, answer["train_rows"] + answer["test_rows"])
        # The label is a threshold on a feature, so a random forest finds it.
        self.assertGreaterEqual(answer["metrics"]["accuracy"], 0.95, answer["metrics"])

    def test_the_regression_check_recovers_the_linear_fee(self) -> None:
        answer = self.check(
            "regression",
            {"feature_fields": ["staff_id", "days_late"], "label_field": "late_fee", "model_family": "linear"},
        )
        self.assertEqual(MAX_RECORDS, answer["train_rows"] + answer["test_rows"])
        # The label is a linear function of the two features.
        self.assertGreater(answer["metrics"]["r2"], 0.99, answer["metrics"])
        self.assertLess(answer["metrics"]["rmse"], 0.01, answer["metrics"])

    def test_the_forecasting_checks_follow_the_sine_wave(self) -> None:
        for family in ("gru", "lstm"):
            with self.subTest(model_family=family):
                answer = self.check(
                    "forecasting",
                    {
                        "feature_fields": ["demand"],
                        "label_field": "demand",
                        "model_family": family,
                        "sequence_length": 12,
                    },
                )
                self.assertEqual(family, answer["model_family"])
                # 512 rows in table order give 500 windows of 12; the last 20% are held out.
                self.assertEqual((400, 100), (answer["train_rows"], answer["test_rows"]))
                # Predicting the mean scores 0; the wave's shape makes the next value predictable.
                self.assertGreater(answer["metrics"]["r2"], 0.5, answer["metrics"])

    def wait_for_job(self, job_id: str) -> tuple[dict[str, Any], str]:
        """Read the job until it ends; return it and the answer's text."""
        deadline = time.monotonic() + JOB_TIMEOUT_S
        while True:
            status, text = self.request("GET", f"/api/v1/model-jobs/{job_id}")
            self.assertEqual(200, status, text)
            job = json.loads(text)
            if job["status"] in ("completed", "failed"):
                return job, text
            if time.monotonic() > deadline:
                self.fail(f"the job is still {job['status']} after {JOB_TIMEOUT_S} s:\n{self.service.log_tail()}")
            time.sleep(0.5)


if __name__ == "__main__":
    unittest.main()
