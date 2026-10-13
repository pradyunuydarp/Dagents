"""What /api/v1/framework/status reports about the hospital stores, and the charts of an empty store.

A deployment can be reachable while its stores hold nothing: the database is
not migrated yet, or it is migrated but not seeded. The status endpoint says
which, through ``ready`` and per-hospital counts, and never answers 503. A
hospital chart over an empty store is listed as unavailable instead of drawn
empty.

The in-memory tests need nothing. The Postgres tests need
``HEALTHCARE_DEMO_TEST_DATABASE_URL`` (see ``test_site_isolation.py``).
"""

from __future__ import annotations

import contextlib
import io
import unittest
from unittest import mock

from tests.api_support import ApiTestCase
from tests.support import tiny_site

from app.domain.dataset import SITES, load_site_records
from app.services.charts_service import NO_STORE_HAS_RECORDS, ChartService, EMPTY_STORE
from app.services.consortium import Consortium
from app.services.site_store import MemorySiteStore, StoreUnavailableError

HOSPITAL_KEYS = {"site_id", "record_count", "labelled_count"}
EMPTY_REASON = "This hospital's store holds no records yet."
ALL_EMPTY_REASON = "No hospital's store holds records yet."


def empty_consortium() -> Consortium:
    stores = {site.site_id: MemorySiteStore(site, []) for site in SITES}
    return Consortium(site_stores=stores)


class FailingStore(MemorySiteStore):
    """A store whose database cannot be read."""

    def count(self) -> dict[str, int]:
        raise StoreUnavailableError("The hospital stores cannot be reached right now.")

    def records(self) -> list[dict]:
        raise StoreUnavailableError("The hospital stores cannot be reached right now.")


class MemoryStatusTests(unittest.TestCase):
    def test_the_reason_text(self) -> None:
        self.assertEqual(EMPTY_REASON, EMPTY_STORE)
        self.assertEqual(ALL_EMPTY_REASON, NO_STORE_HAS_RECORDS)

    def test_seeded_stores_are_ready(self) -> None:
        status = Consortium().data_source_status()
        self.assertIs(True, status["ready"])
        self.assertEqual([site.site_id for site in SITES], [row["site_id"] for row in status["hospitals"]])
        for row, site in zip(status["hospitals"], SITES, strict=True):
            self.assertEqual(HOSPITAL_KEYS, set(row))
            self.assertEqual(len(load_site_records(site.site_id)), row["record_count"])
            self.assertGreater(row["labelled_count"], 0)

    def test_empty_stores_are_not_ready(self) -> None:
        status = empty_consortium().data_source_status()
        self.assertIs(False, status["ready"])
        self.assertEqual([0, 0, 0], [row["record_count"] for row in status["hospitals"]])

    def test_one_empty_store_is_enough_to_be_not_ready(self) -> None:
        site, store = tiny_site(0)
        status = Consortium(sites=[*SITES, site], site_stores={site.site_id: store}).data_source_status()
        self.assertIs(False, status["ready"])

    def test_an_unreadable_store_gives_not_ready_and_no_counts(self) -> None:
        stores = {site.site_id: FailingStore(site, []) for site in SITES}
        with self.assertLogs("app.services.consortium", level="WARNING"):
            status = Consortium(site_stores=stores).data_source_status()
        self.assertIs(False, status["ready"])
        self.assertIsNone(status["hospitals"])
        self.assertEqual("in-memory", status["cohort_source"])


class EmptyStoreChartTests(unittest.TestCase):
    def test_every_hospital_chart_is_unavailable(self) -> None:
        consortium = empty_consortium()
        for site in SITES:
            charts = ChartService().site_list(consortium.hospital(site.site_id))
            for chart in charts:
                with self.subTest(site=site.site_id, chart=chart["chart_id"]):
                    self.assertIs(False, chart["available"])
                    self.assertEqual(EMPTY_REASON, chart["reason"])

    def test_the_consortium_record_charts_are_unavailable_when_every_store_is_empty(self) -> None:
        charts = {c["chart_id"]: c for c in ChartService().consortium_list(empty_consortium())}
        for chart_id in ("hospital-records", "hospital-outcome-rates"):
            self.assertIs(False, charts[chart_id]["available"])
            # The chart covers every hospital, so its reason names them all.
            self.assertEqual(ALL_EMPTY_REASON, charts[chart_id]["reason"])

    def test_the_consortium_record_charts_draw_when_one_store_holds_records(self) -> None:
        stores = {site.site_id: MemorySiteStore(site, []) for site in SITES[1:]}
        charts = {c["chart_id"]: c for c in ChartService().consortium_list(Consortium(site_stores=stores))}
        self.assertIs(True, charts["hospital-records"]["available"])
        self.assertIs(True, charts["hospital-outcome-rates"]["available"])

    def test_a_hospital_with_records_still_draws(self) -> None:
        charts = ChartService().site_list(Consortium().hospital(SITES[0].site_id))
        self.assertTrue(all(chart["available"] for chart in charts))


class StatusEndpointTests(ApiTestCase):
    def test_the_endpoint_reports_ready_and_counts(self) -> None:
        status = self.ok(self.client.get("/api/v1/framework/status"))
        self.assertIs(True, status["ready"])
        self.assertEqual(len(SITES), len(status["hospitals"]))

    def test_an_unreadable_store_is_a_200_with_ready_false(self) -> None:
        stores = {site.site_id: FailingStore(site, []) for site in SITES}
        failing = Consortium(site_stores=stores)
        import app.main as main

        with mock.patch.object(main, "consortium", failing), contextlib.redirect_stderr(io.StringIO()):
            with self.assertLogs("app.services.consortium", level="WARNING"):
                response = self.client.get("/api/v1/framework/status")
        status = self.ok(response)
        self.assertIs(False, status["ready"])
        self.assertIsNone(status["hospitals"])


def _postgres_imports():
    from tests.test_site_isolation import SupabaseLikeDatabase, admin_dsn_or_skip

    return SupabaseLikeDatabase, admin_dsn_or_skip


class PostgresStatusTests(unittest.TestCase):
    """Each state a real deployment passes through: unmigrated, migrated and empty, seeded."""

    def database(self, *, migrate: bool):
        SupabaseLikeDatabase, admin_dsn_or_skip = _postgres_imports()
        db = SupabaseLikeDatabase(admin_dsn_or_skip())
        db.start(migrate=migrate)
        self.addCleanup(db.stop)
        return db

    def test_an_unmigrated_database_is_not_ready(self) -> None:
        db = self.database(migrate=False)
        consortium = Consortium(database_url=db.dsn, environ={})
        self.addCleanup(consortium.close)
        with self.assertLogs("app.services.consortium", level="WARNING") as logs:
            status = consortium.data_source_status()
        self.assertIs(False, status["ready"])
        self.assertIsNone(status["hospitals"])
        self.assertEqual("postgres", status["cohort_source"])
        # The response names no host and no user; the log may.
        text = repr(status)
        self.assertNotIn("supa_like", text)
        self.assertNotIn("127.0.0.1", text)
        self.assertTrue(logs.output)

    def test_a_migrated_empty_database_is_not_ready_and_its_charts_say_why(self) -> None:
        db = self.database(migrate=True)
        consortium = Consortium(database_url=db.dsn, environ={})
        self.addCleanup(consortium.close)
        status = consortium.data_source_status()
        self.assertIs(False, status["ready"])
        self.assertEqual([0, 0, 0], [row["record_count"] for row in status["hospitals"]])
        charts = ChartService().site_list(consortium.hospital(SITES[0].site_id))
        self.assertEqual({EMPTY_REASON}, {chart["reason"] for chart in charts})

    def test_a_seeded_database_is_ready(self) -> None:
        from app.services.site_store import PostgresSiteStore

        db = self.database(migrate=True)
        for site in SITES:
            PostgresSiteStore(site, db.dsn).seed_dataset(load_site_records(site.site_id))
        consortium = Consortium(database_url=db.dsn, environ={})
        self.addCleanup(consortium.close)
        status = consortium.data_source_status()
        self.assertIs(True, status["ready"])
        self.assertEqual(
            [len(load_site_records(site.site_id)) for site in SITES],
            [row["record_count"] for row in status["hospitals"]],
        )


if __name__ == "__main__":
    unittest.main()
