"""The deployed service has to start before it can report anything.

An earlier version read every cohort while ``app.main`` was imported. A
deployment whose database was unreachable never finished importing, never bound
its port, and answered nothing, and the health endpoint that would have
explained it was never reachable.

So nothing at import may touch a store, and a store that cannot be reached is
reported as a 503 with a plain message. The message never names the database
host or user; the detail goes to the server log. None of these tests needs the
planner or a database, so none skips.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest
from unittest import mock

from tests.api_support import ApiTestCase, main

from app.domain.dataset import SITES, load_site_records
from app.services.consortium import Consortium
from app.services.site_store import StoreUnavailableError

APP_ROOT = Path(__file__).resolve().parents[1]
#: The Dagents checkout. Two levels up inside the Dagents repository; after
#: ``scripts/extract_repo.sh``, wherever ``DAGENTS_HOME`` points.
FRAMEWORK_ROOT = Path(os.environ.get("DAGENTS_HOME") or APP_ROOT.parents[1])

#: Nothing listens on port 1, so the connection is refused at once. The user
#: and password are distinctive, so a test can check they never reach a client.
UNREACHABLE = "postgresql://hidden_user:hidden_password@127.0.0.1:1/healthcare"


class ImportTimeTests(unittest.TestCase):
    """What may and may not happen while the app is being built."""

    def test_importing_the_app_touches_no_store(self) -> None:
        """Run in a fresh interpreter, so the import really happens."""
        script = textwrap.dedent(
            """
            from unittest import mock
            import psycopg
            with mock.patch("app.services.consortium.load_site_records") as loader, \\
                 mock.patch.object(psycopg, "connect", side_effect=AssertionError("connected at import")) as connect:
                import app.main
                assert len(app.main.consortium.hospitals) == 3
                assert not loader.called, "records were read at import"
                assert not connect.called, "a connection was opened at import"
            print("ok")
            """
        )
        for database_url in ("", UNREACHABLE):
            with self.subTest(database_url=bool(database_url)):
                paths = [str(FRAMEWORK_ROOT), str(APP_ROOT / "backend"), os.environ.get("PYTHONPATH", "")]
                env = {
                    **os.environ,
                    "PYTHONPATH": os.pathsep.join(path for path in paths if path),
                    "HEALTHCARE_DEMO_DATABASE_URL": database_url,
                }
                result = subprocess.run(
                    [sys.executable, "-c", script], cwd=APP_ROOT, env=env, capture_output=True, text=True, timeout=120
                )
                self.assertEqual(0, result.returncode, result.stderr[-2000:])
                self.assertEqual("ok", result.stdout.strip())

    def test_building_the_consortium_reads_no_records(self) -> None:
        with mock.patch("app.services.consortium.load_site_records") as loader:
            consortium = Consortium()
            self.assertEqual(3, len(consortium.hospitals))
            loader.assert_not_called()

    def test_each_hospitals_records_are_read_once_on_first_use(self) -> None:
        with mock.patch("app.services.consortium.load_site_records", side_effect=load_site_records) as loader:
            consortium = Consortium()
            consortium.overview()
            consortium.overview()
            consortium.hospital("uk-134").worklist(5)
        self.assertEqual(sorted(site.site_id for site in SITES), sorted(call.args[0] for call in loader.call_args_list))

    def test_an_unreachable_store_raises_and_names_the_hospital(self) -> None:
        consortium = Consortium(database_url=UNREACHABLE, environ={})
        with self.assertRaises(StoreUnavailableError) as caught:
            consortium.hospital("uk-134").counts()
        self.assertIn("hospital uk-134", str(caught.exception))
        consortium.close()


class ShutdownTests(unittest.TestCase):
    def test_shutting_down_closes_every_store(self) -> None:
        from fastapi.testclient import TestClient

        consortium = Consortium()
        with mock.patch.object(main, "consortium", consortium), mock.patch.object(consortium, "close") as close:
            with TestClient(main.app) as client:
                self.assertEqual(200, client.get("/api/v1/health").status_code)
                close.assert_not_called()
        close.assert_called_once_with()


class DeployedDependencyTests(unittest.TestCase):
    """What the image must contain. The Dockerfile installs requirements.txt and nothing else."""

    REQUIREMENTS = APP_ROOT / "requirements.txt"

    def test_a_postgres_driver_is_declared(self) -> None:
        self.assertRegex(self.REQUIREMENTS.read_text(encoding="utf-8"), r"(?m)^psycopg\b")

    def test_matplotlib_is_declared_for_the_charts(self) -> None:
        self.assertRegex(self.REQUIREMENTS.read_text(encoding="utf-8"), r"(?m)^matplotlib\b")

    def test_the_adapter_is_what_fails_without_the_driver(self) -> None:
        from agents.common.domain.sources import ConnectionRef, PostgresSelection, PostgresSourceSpec, SourceBatching
        from agents.common.infrastructure import sources

        spec = PostgresSourceSpec(
            source_id="stroke-encounters-test",
            connection_ref=ConnectionRef(connection_id="test"),
            selection=PostgresSelection(table="site_uk_134.encounters", columns=["encounter_id"]),
            batching=SourceBatching(batch_size=10, max_records=10),
        )
        with mock.patch.object(sources, "psycopg", None):
            with self.assertRaises(ValueError) as caught:
                sources.PostgresSourceAdapter().scan(spec, {"host": "db"})
        self.assertIn("requires psycopg", str(caught.exception))


class ServingWhileTheStoreIsDownTests(ApiTestCase):
    """The app answers, with a plain message, instead of disappearing."""

    @classmethod
    def make_consortium(cls) -> Consortium:
        return Consortium(database_url=UNREACHABLE, environ={})

    def assert_plain_503(self, response) -> None:
        self.assertEqual(503, response.status_code)
        self.assertEqual({"detail": main.STORE_UNAVAILABLE_MESSAGE}, response.json())
        # The frontend shows this 503 at once, because the app marked it.
        self.assertEqual("healthcare-demo", response.headers.get("x-dagents-app"))
        for secret in ("127.0.0.1", "hidden_user", "hidden_password", "psycopg", "port 1"):
            self.assertNotIn(secret, response.text)

    def test_health_does_not_depend_on_the_store(self) -> None:
        payload = self.ok(self.client.get("/api/v1/health"))
        self.assertEqual("ist-1-deidentified", payload["data"])

    def test_the_status_names_the_storage_without_connecting(self) -> None:
        payload = self.ok(self.client.get("/api/v1/framework/status"))
        self.assertEqual("postgres", payload["cohort_source"])
        self.assertEqual("schema-per-site", payload["store_layout"])

    def test_data_endpoints_answer_503_with_a_plain_message(self) -> None:
        with self.assertLogs("healthcare_demo", level="WARNING") as logged:
            self.assert_plain_503(self.client.get("/api/v1/overview"))
        # The detail is in the server log, for whoever runs the service.
        self.assertIn("could not connect", "\n".join(logged.output))
        paths = ("/api/v1/hospitals", "/api/v1/hospitals/uk-134", "/api/v1/charts", "/api/v1/hospitals/it-002/charts")
        for path in paths:
            with self.subTest(path=path), self.assertLogs("healthcare_demo", level="WARNING"):
                self.assert_plain_503(self.client.get(path))

    def test_an_entry_answers_503_and_is_not_kept_anywhere_else(self) -> None:
        from tests.support import HIGH_RISK_ENTRY

        with self.assertLogs("healthcare_demo", level="WARNING"):
            self.assert_plain_503(self.client.post("/api/v1/hospitals/uk-134/encounters", json=HIGH_RISK_ENTRY))


if __name__ == "__main__":
    unittest.main()
