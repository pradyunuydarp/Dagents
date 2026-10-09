"""The deployed service has to start before it can report anything.

These tests exist because of a real outage. `StrokeDataSource` used to read its
cohort in `__init__`, and `Consortium.__init__` registered every site — which
reads each site's record count. Both run while `app.main` is being imported, so
a deployment whose database was unreachable never finished importing, never
bound its port, and answered nothing at all: the platform's edge accepted each
connection and the caller timed out with zero bytes received. "No response" is
the one failure a reader cannot interpret, and the health endpoint that would
have explained it was never reachable.

So: nothing at import may touch the encounter store, and a store that cannot be
read is reported rather than replaced. Neither test needs the planner — the
process starting is not a governance question — so neither skips.
"""

from __future__ import annotations

import unittest
from unittest import mock

from app.services.consortium import Consortium
from app.services.encounters import CohortUnavailableError


#: A host that is listening to nothing: connection refused immediately, so the
#: test is fast and deterministic rather than waiting out a timeout.
UNREACHABLE = "postgresql://demo:demo@127.0.0.1:1/healthcare"


class ImportTimeTests(unittest.TestCase):
    """What may and may not happen while the app is being constructed."""

    def test_building_the_consortium_reads_no_cohorts(self) -> None:
        with mock.patch("app.services.hospital.load_cohort") as loader:
            consortium = Consortium(cohort_size=10, database_url=UNREACHABLE)
            self.assertEqual(3, len(consortium.hospitals))
            loader.assert_not_called()

    def test_a_cohort_is_read_once_on_first_use(self) -> None:
        with mock.patch("app.services.hospital.load_cohort", return_value=([], "supabase")) as loader:
            consortium = Consortium(cohort_size=10, database_url=UNREACHABLE)
            source = consortium.hospitals["riverside-general"].data_source
            self.assertEqual("supabase", source.provenance)
            self.assertEqual([], source.records_held)
            self.assertEqual(1, loader.call_count, "the cohort was loaded more than once")

    def test_an_unreachable_store_raises_rather_than_generating(self) -> None:
        """The failure the whole module exists to keep visible."""
        consortium = Consortium(cohort_size=10, database_url=UNREACHABLE)
        source = consortium.hospitals["riverside-general"].data_source
        with self.assertRaises(CohortUnavailableError) as caught:
            _ = source.records_held
        self.assertIn("riverside-general", str(caught.exception))


class ServingWhileTheStoreIsDownTests(unittest.TestCase):
    """The app answers, and says what is wrong, instead of disappearing."""

    @classmethod
    def setUpClass(cls) -> None:
        from fastapi.testclient import TestClient

        from app.main import app, consortium

        cls.client = TestClient(app, raise_server_exceptions=False)
        cls.consortium = consortium

    def test_health_does_not_depend_on_the_store(self) -> None:
        """Render's health check hits this, and keeps the service alive by it."""
        with mock.patch.object(
            type(self.consortium.hospitals["riverside-general"].data_source),
            "_materialize",
            side_effect=CohortUnavailableError("the store is unreachable"),
        ):
            response = self.client.get("/api/v1/health")
        self.assertEqual(200, response.status_code)
        self.assertEqual("synthetic-only", response.json()["data"])

    def test_a_data_endpoint_answers_503_with_the_reason(self) -> None:
        with mock.patch.object(
            type(self.consortium.hospitals["riverside-general"].data_source),
            "_materialize",
            side_effect=CohortUnavailableError("could not read riverside-general: boom"),
        ):
            response = self.client.get("/api/v1/overview")
        self.assertEqual(503, response.status_code)
        body = response.json()
        self.assertIn("riverside-general", body["detail"])
        self.assertEqual("configured but unreadable", body["cohort_source"])
        self.assertTrue(body["records_are_synthetic"])


if __name__ == "__main__":
    unittest.main()
