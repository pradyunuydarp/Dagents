"""A Postgres source read by a running pipeline service.

The service runs as its own uvicorn process, as it does when deployed, and is
called over HTTP. The test registers a Postgres source with the login and
password of a scratch database on a real PostgreSQL 16 server, runs an
asynchronous pipeline that profiles the table and runs a model step on it, and
reads the finished run. It also checks that no answer about the source shows
the login, the password or the database name: not the source endpoints, and
not the pipeline endpoints when a step carries the source itself in
``dataset_input.source``.

It needs ``DAGENTS_TEST_DATABASE_URL``: a connection string for a superuser
login on a PostgreSQL 16 server, over TCP with a password. The test creates a
scratch database and a login role with ``ScratchDatabase`` from the agents'
role tests, and drops both afterwards. Without the variable the class skips
with a message that starts with ``database test skipped``. The CI job that runs
this suite has a PostgreSQL service and fails on that skip.
"""

from __future__ import annotations

import json
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

#: Rows in the table. The source reads at most ``MAX_RECORDS`` of them, so a
#: count of ``MAX_RECORDS`` shows that the source's limit was applied.
TABLE_ROWS = 600
MAX_RECORDS = 512
BATCH_SIZE = 128
FEATURES = ["customer_id", "staff_id", "rental_id", "amount"]
SOURCE_ID = "scratch-payments"
PIPELINE_ID = "scratch-payments-profile-and-score"
INLINE_PIPELINE_ID = "scratch-payments-inline-source"

#: How long the service may take to answer its health check, and a run to
#: finish. Both have a margin for a cold CI runner.
START_TIMEOUT_S = 60.0
RUN_TIMEOUT_S = 120.0

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


def payment_rows() -> list[tuple[int, int, int, int, float, int]]:
    """The table's rows: ``(payment_id, customer_id, staff_id, rental_id, amount, is_high_value)``.

    ``amount`` takes the twelve values 0.99 to 11.99 in turn, and
    ``is_high_value`` is 1 when ``amount`` is 8 or more.
    """
    rows = []
    for payment_id in range(1, TABLE_ROWS + 1):
        amount = (payment_id * 7) % 12 + 0.99
        rows.append(
            (payment_id, (payment_id * 37) % 599 + 1, 1 + payment_id % 2, 1000 + payment_id, amount, int(amount >= 8))
        )
    return rows


def create_payments_table(dsn: str) -> None:
    """Create ``source_data.payments`` as the scratch database's login role, which owns it."""
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute("CREATE SCHEMA source_data")
        connection.execute(
            "CREATE TABLE source_data.payments (payment_id int PRIMARY KEY, customer_id int NOT NULL, "
            "staff_id int NOT NULL, rental_id int NOT NULL, amount double precision NOT NULL, "
            "is_high_value int NOT NULL)"
        )
        with connection.cursor() as cursor:
            cursor.executemany("INSERT INTO source_data.payments VALUES (%s, %s, %s, %s, %s, %s)", payment_rows())


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
                "SELECT customer_id, staff_id, rental_id, amount, is_high_value "
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

    def __init__(self, log_dir: Path) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(64)
        self.base_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        self.log = log_dir / "pipeline-service.log"
        environment = {
            key: value
            for key, value in os.environ.items()
            # libpq reads PG* variables; the source's own options must be all it uses.
            if not key.startswith("PG") and key.lower() not in ("http_proxy", "https_proxy", "all_proxy")
        }
        environment.update(
            PYTHONPATH=os.pathsep.join([str(REPO_ROOT), str(SERVICE_ROOT)]),
            NO_PROXY="127.0.0.1,localhost",
            no_proxy="127.0.0.1,localhost",
        )
        try:
            with self.log.open("wb") as handle:
                self.process = subprocess.Popen(
                    [sys.executable, "-m", "uvicorn", "app.main:app", "--fd", str(listener.fileno()),
                     "--log-level", "warning"],
                    cwd=log_dir,
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


class PostgresPipelineSourceTests(unittest.TestCase):
    """Register a Postgres source with the running service, run a pipeline on it and read the run."""

    @classmethod
    def setUpClass(cls) -> None:
        database = start_database()
        cls.addClassCleanup(database.stop)
        cls.dsn = database.dsn
        cls.secrets = {
            "password": conninfo_to_dict(database.dsn)["password"],
            "login": database.owner,
            "database name": database.name,
        }
        folder = tempfile.TemporaryDirectory(prefix="dagents-pipeline-pg-")
        cls.addClassCleanup(folder.cleanup)
        cls.service = ServiceProcess(Path(folder.name))
        cls.addClassCleanup(cls.service.stop)
        cls.service.wait_until_healthy(START_TIMEOUT_S)
        cls.registered = call(cls.service.base_url, "POST", "/api/v1/sources", postgres_source(cls.dsn))

    def request(self, method: str, path: str, body: Any = None) -> tuple[int, str]:
        return call(self.service.base_url, method, path, body)

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

    def test_a_definition_hides_an_inline_source_login_and_runs_with_it(self) -> None:
        # A step can carry its own source in `dataset_input.source` instead of
        # naming a registered one. Every pipeline answer echoes the step.
        definition = {
            "pipeline_id": INLINE_PIPELINE_ID,
            "description": "Profile the scratch payments table through a source sent with the pipeline",
            "steps": [
                {
                    "step_id": "profile",
                    "kind": "profile_dataset",
                    "config": {
                        "target_field": "dataset_profile",
                        "dataset_input": {"source": postgres_source(self.dsn)},
                        "feature_fields": FEATURES,
                        "batch_size": BATCH_SIZE,
                    },
                }
            ],
        }
        answers = {
            "register": self.request("POST", "/api/v1/pipeline-definitions", definition),
            "register, short path": self.request("POST", "/api/v1/pipelines", definition),
            "list": self.request("GET", "/api/v1/pipeline-definitions"),
            "list, short path": self.request("GET", "/api/v1/pipelines"),
            "fetch": self.request("GET", f"/api/v1/pipeline-definitions/{INLINE_PIPELINE_ID}"),
            "validate": self.request("POST", f"/api/v1/pipeline-definitions/{INLINE_PIPELINE_ID}:validate"),
        }
        for answer, (status, text) in answers.items():
            with self.subTest(answer=answer):
                self.assertEqual(200, status, text)
                self.assert_shows_no_secret(text)
                body = json.loads(text)
                if "pipelines" in body:
                    [body] = [item for item in body["pipelines"] if item["pipeline_id"] == INLINE_PIPELINE_ID]
                source = body["steps"][0]["config"]["dataset_input"]["source"]
                self.assertEqual(REDACTED_OPTIONS, source["connection_ref"]["options"])

        # The answers are redacted copies. The stored step keeps the real
        # password, so the run still logs in.
        status, text = self.request("POST", "/api/v1/pipeline-runs", {"pipeline_id": INLINE_PIPELINE_ID, "payload": {}})
        self.assertEqual(202, status, text)
        run, text = self.wait_for_run(json.loads(text)["run_id"])
        self.assertEqual("completed", run["status"], run.get("error"))
        self.assert_shows_no_secret(text)
        self.assertEqual(MAX_RECORDS, run["final_payload"]["dataset_profile"]["record_count"])

    def test_an_async_run_profiles_and_scores_the_table(self) -> None:
        # A redacted answer must not replace the stored definition: after this
        # fetch, the run still has to log in with the real password.
        status, text = self.request("GET", f"/api/v1/sources/{SOURCE_ID}")
        self.assertEqual(200, status, text)
        definition = {
            "pipeline_id": PIPELINE_ID,
            "description": "Profile and score the scratch payments table",
            "steps": [
                {
                    "step_id": "profile",
                    "kind": "profile_dataset",
                    "config": {
                        "target_field": "dataset_profile",
                        "dataset_input": {"source_id": SOURCE_ID},
                        "feature_fields": FEATURES,
                        "label_field": "is_high_value",
                        "batch_size": BATCH_SIZE,
                    },
                },
                {
                    "step_id": "model",
                    "kind": "run_model_job",
                    "depends_on": ["profile"],
                    "config": {
                        "target_field": "model_run",
                        "dataset_input": {"source_id": SOURCE_ID},
                        "feature_fields": FEATURES,
                        "label_field": "is_high_value",
                        "task_type": "classification",
                        "model_family": "random_forest",
                        "artifact_prefix": "artifacts/tests",
                    },
                },
            ],
        }
        status, text = self.request("POST", "/api/v1/pipeline-definitions", definition)
        self.assertEqual(200, status, text)

        status, text = self.request("POST", "/api/v1/pipeline-runs", {"pipeline_id": PIPELINE_ID, "payload": {}})
        self.assertEqual(202, status, text)
        queued = json.loads(text)
        self.assertEqual("queued", queued["status"])

        run, text = self.wait_for_run(queued["run_id"])
        self.assertEqual("completed", run["status"], run.get("error"))
        self.assert_shows_no_secret(text)
        self.assertEqual(["profile", "model"], [step["step_id"] for step in run["step_results"]])

        profile = run["final_payload"]["dataset_profile"]
        self.assertEqual(MAX_RECORDS, profile["record_count"])
        self.assertEqual(MAX_RECORDS // BATCH_SIZE, profile["partition_count"])
        # Every feature arrived as a number, so the rows came from the table's
        # int and double precision columns.
        self.assertEqual(FEATURES, profile["numeric_fields"])
        self.assertEqual("is_high_value", profile["label_field"])

        model_run = run["final_payload"]["model_run"]
        self.assertEqual(MAX_RECORDS, model_run["record_count"])
        self.assertEqual("random_forest", model_run["model_family"])
        self.assertEqual("classification", model_run["task_type"])
        self.assertEqual(len(FEATURES), model_run["feature_count"])

        status, text = self.request("GET", "/api/v1/pipeline-runs")
        self.assertEqual(200, status, text)
        self.assertIn(queued["run_id"], [listed["run_id"] for listed in json.loads(text)["runs"]])

    def wait_for_run(self, run_id: str) -> tuple[dict[str, Any], str]:
        """Read the run until it ends; return it and the answer's text."""
        deadline = time.monotonic() + RUN_TIMEOUT_S
        while True:
            status, text = self.request("GET", f"/api/v1/pipeline-runs/{run_id}")
            self.assertEqual(200, status, text)
            run = json.loads(text)
            if run["status"] in ("completed", "failed"):
                return run, text
            if time.monotonic() > deadline:
                self.fail(f"the run is still {run['status']} after {RUN_TIMEOUT_S} s:\n{self.service.log_tail()}")
            time.sleep(0.2)


if __name__ == "__main__":
    unittest.main()
