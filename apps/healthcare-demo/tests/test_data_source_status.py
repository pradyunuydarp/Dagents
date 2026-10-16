"""What /api/v1/framework/status reports about the hospital stores, and the charts of an empty store.

A deployment can be reachable while its stores hold nothing: the database is
not migrated yet, or it is migrated but not seeded. The status endpoint says
which, through ``ready`` and per-hospital counts that each hospital's LMA
reports through the GMA, and never answers 503. A hospital chart over an empty
store is listed as unavailable instead of drawn empty.

The in-memory tests need nothing, except those that list a hospital's charts or
score its records with the model in use, which need the planner. The Postgres tests need
``HEALTHCARE_DEMO_TEST_DATABASE_URL`` (see ``test_site_isolation.py``).
"""

from __future__ import annotations

import contextlib
import io
import unittest
from unittest import mock

from tests.api_support import ApiTestCase
from tests.support import dagentsc_available, tiny_site

from app.domain.dataset import SITES, load_site_records
from app.services.agents import HealthcareDemo
from app.services.charts_service import NO_STORE_HAS_RECORDS, ChartService, EMPTY_STORE
from app.services.site_store import MemorySiteStore, StoreUnavailableError

HOSPITAL_KEYS = {"site_id", "record_count", "labelled_count"}
EMPTY_REASON = "This hospital's store holds no records yet."
ALL_EMPTY_REASON = "No hospital's store holds records yet."


def empty_consortium() -> HealthcareDemo:
    stores = {site.site_id: MemorySiteStore(site, []) for site in SITES}
    return HealthcareDemo(site_stores=stores)


class DemoCase(unittest.TestCase):
    def built(self, demo: HealthcareDemo) -> HealthcareDemo:
        """``demo``, stopped when the test ends."""
        self.addCleanup(demo.close)
        return demo


class FailingStore(MemorySiteStore):
    """A store whose database cannot be read."""

    def count(self) -> dict[str, int]:
        raise StoreUnavailableError("The hospital stores cannot be reached right now.")

    def records(self) -> list[dict]:
        raise StoreUnavailableError("The hospital stores cannot be reached right now.")


class MemoryStatusTests(DemoCase):
    def test_the_reason_text(self) -> None:
        self.assertEqual(EMPTY_REASON, EMPTY_STORE)
        self.assertEqual(ALL_EMPTY_REASON, NO_STORE_HAS_RECORDS)

    def test_seeded_stores_are_ready(self) -> None:
        status = self.built(HealthcareDemo()).status()
        self.assertIs(True, status["ready"])
        self.assertEqual([site.site_id for site in SITES], [row["site_id"] for row in status["hospitals"]])
        for row, site in zip(status["hospitals"], SITES, strict=True):
            self.assertEqual(HOSPITAL_KEYS, set(row))
            self.assertEqual(len(load_site_records(site.site_id)), row["record_count"])
            self.assertGreater(row["labelled_count"], 0)

    def test_empty_stores_are_not_ready(self) -> None:
        status = self.built(empty_consortium()).status()
        self.assertIs(False, status["ready"])
        self.assertEqual([0, 0, 0], [row["record_count"] for row in status["hospitals"]])

    def test_one_empty_store_is_enough_to_be_not_ready(self) -> None:
        site, store = tiny_site(0)
        status = self.built(HealthcareDemo(sites=[*SITES, site], site_stores={site.site_id: store})).status()
        self.assertIs(False, status["ready"])

    def test_an_unreadable_store_gives_not_ready_and_no_counts(self) -> None:
        stores = {site.site_id: FailingStore(site, []) for site in SITES}
        with self.assertLogs(level="WARNING"):
            status = self.built(HealthcareDemo(site_stores=stores)).status()
        self.assertIs(False, status["ready"])
        self.assertIsNone(status["hospitals"])
        self.assertEqual("in-memory", status["cohort_source"])
        # Each LMA answered; it could not read its store.
        self.assertEqual([True, True, True], [lma["reachable"] for lma in status["agents"]["lmas"]])


class EmptyStoreChartTests(DemoCase):
    # A hospital's chart list names the model in use, which the GMA checks with the planner.
    @unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
    def test_every_hospital_chart_is_unavailable(self) -> None:
        consortium = self.built(empty_consortium())
        for site in SITES:
            charts = ChartService().site_list(consortium.hospital(site.site_id))
            for chart in charts:
                with self.subTest(site=site.site_id, chart=chart["chart_id"]):
                    self.assertIs(False, chart["available"])
                    self.assertEqual(EMPTY_REASON, chart["reason"])

    def test_the_consortium_record_charts_are_unavailable_when_every_store_is_empty(self) -> None:
        charts = {c["chart_id"]: c for c in ChartService().consortium_list(self.built(empty_consortium()).overview)}
        for chart_id in ("hospital-records", "hospital-outcome-rates"):
            self.assertIs(False, charts[chart_id]["available"])
            # The chart covers every hospital, so its reason names them all.
            self.assertEqual(ALL_EMPTY_REASON, charts[chart_id]["reason"])

    def test_the_consortium_record_charts_draw_when_one_store_holds_records(self) -> None:
        stores = {site.site_id: MemorySiteStore(site, []) for site in SITES[1:]}
        demo = self.built(HealthcareDemo(site_stores=stores))
        charts = {c["chart_id"]: c for c in ChartService().consortium_list(demo.overview)}
        self.assertIs(True, charts["hospital-records"]["available"])
        self.assertIs(True, charts["hospital-outcome-rates"]["available"])

    @unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
    def test_a_hospital_with_records_still_draws(self) -> None:
        charts = ChartService().site_list(self.built(HealthcareDemo()).hospital(SITES[0].site_id))
        self.assertTrue(all(chart["available"] for chart in charts))


class StatusEndpointTests(ApiTestCase):
    def test_the_endpoint_reports_ready_and_counts(self) -> None:
        status = self.ok(self.client.get("/api/v1/framework/status"))
        self.assertIs(True, status["ready"])
        self.assertEqual(len(SITES), len(status["hospitals"]))

    def test_an_unreadable_store_is_a_200_with_ready_false(self) -> None:
        stores = {site.site_id: FailingStore(site, []) for site in SITES}
        failing = HealthcareDemo(site_stores=stores)
        self.addCleanup(failing.close)
        import app.main as main

        with mock.patch.object(main, "demo", failing), contextlib.redirect_stderr(io.StringIO()):
            with self.assertLogs(level="WARNING"):
                response = self.client.get("/api/v1/framework/status")
        status = self.ok(response)
        self.assertIs(False, status["ready"])
        self.assertIsNone(status["hospitals"])


class FrozenGmaStatusTests(ApiTestCase):
    """The status report waits a bounded time for the GMA's site summaries.

    The page asks for the status on every load, and each request holds one of
    the app's in-flight slots. A GMA that waits on a frozen LMA must not hold
    the report for the agent network's whole dispatch timeout.
    """

    def test_a_gma_that_does_not_answer_in_time_gives_a_prompt_200(self) -> None:
        import time

        from dagents.gma.application.site_directory import SiteDirectory
        import app.services.overview as overview

        original = SiteDirectory.summaries

        def frozen(directory, include_audit=False):  # noqa: ANN001, ANN202 - same signature as the method
            time.sleep(3.0)
            return original(directory, include_audit)

        self.client.get("/api/v1/framework/status")  # the agents are built on first use
        with mock.patch.object(overview, "STATUS_REPORT_TIMEOUT_SECONDS", 0.5), mock.patch.object(
            SiteDirectory, "summaries", frozen
        ), self.assertLogs(level="WARNING") as logged:
            started = time.monotonic()
            response = self.client.get("/api/v1/framework/status")
            elapsed = time.monotonic() - started
        status = self.ok(response)
        self.assertLess(elapsed, 2.5, "the status report waited for the GMA")
        self.assertIs(False, status["ready"])
        self.assertIsNone(status["hospitals"])
        self.assertIsNone(status["agents"]["lmas"])
        self.assertIn("status report", "\n".join(logged.output))

    def test_a_frozen_gma_on_a_fresh_start_gives_a_prompt_200(self) -> None:
        """No call of the report waits longer than its bound: the site summaries, the engine name or a health check.

        The demo is new, so the GMA's engine name is not cached yet, and the
        GMA's health route is as slow as its site summaries.
        """
        import dataclasses
        import time

        from dagents.gma.application.aggregation_service import AggregationService
        from dagents.gma.application.site_directory import SiteDirectory
        import app.main as main
        import app.services.overview as overview

        fresh = HealthcareDemo(settings=dataclasses.replace(main.settings, request_timeout_seconds=0.5))
        self.addCleanup(fresh.close)
        fresh.agents.gma()  # the agents are built, but nothing has asked the GMA for its engine yet
        original_summaries = SiteDirectory.summaries
        original_health = AggregationService.health_payload

        def frozen_summaries(directory, include_audit=False):  # noqa: ANN001, ANN202 - same signature as the method
            time.sleep(3.0)
            return original_summaries(directory, include_audit)

        def frozen_health():  # noqa: ANN202 - same signature as the method
            time.sleep(3.0)
            return original_health()

        with mock.patch.object(main, "demo", fresh), mock.patch.object(
            overview, "STATUS_REPORT_TIMEOUT_SECONDS", 0.5
        ), mock.patch.object(SiteDirectory, "summaries", frozen_summaries), mock.patch.object(
            AggregationService, "health_payload", staticmethod(frozen_health)
        ), self.assertLogs(level="WARNING"):
            started = time.monotonic()
            response = self.client.get("/api/v1/framework/status")
            elapsed = time.monotonic() - started
        status = self.ok(response)
        # 0.5 s for the summaries, 0.5 s for the engine name and 0.5 s for the
        # health checks, which run at the same time, plus a margin.
        self.assertLess(elapsed, 2.5, "the status report waited for the GMA")
        self.assertIs(False, status["ready"])
        self.assertIsNone(status["agents"]["engine"])
        gma = next(service for service in status["services"] if service["name"] == "gma")
        self.assertEqual("unavailable", gma["status"])


def _postgres_imports():
    from tests.test_site_isolation import SupabaseLikeDatabase, admin_dsn_or_skip

    return SupabaseLikeDatabase, admin_dsn_or_skip


class PostgresStatusTests(DemoCase):
    """Each state a real deployment passes through: unmigrated, migrated and empty, seeded."""

    def database(self, *, migrate: bool):
        SupabaseLikeDatabase, admin_dsn_or_skip = _postgres_imports()
        db = SupabaseLikeDatabase(admin_dsn_or_skip())
        db.start(migrate=migrate)
        self.addCleanup(db.stop)
        return db

    def test_an_unmigrated_database_is_not_ready(self) -> None:
        db = self.database(migrate=False)
        consortium = self.built(HealthcareDemo(database_url=db.dsn, environ={}))
        with self.assertLogs(level="WARNING") as logs:
            status = consortium.status()
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
        consortium = self.built(HealthcareDemo(database_url=db.dsn, environ={}))
        status = consortium.status()
        self.assertIs(False, status["ready"])
        self.assertEqual([0, 0, 0], [row["record_count"] for row in status["hospitals"]])
        charts = ChartService().site_list(consortium.hospital(SITES[0].site_id))
        self.assertEqual({EMPTY_REASON}, {chart["reason"] for chart in charts})

    def test_a_seeded_database_is_ready(self) -> None:
        from app.services.site_store import PostgresSiteStore

        db = self.database(migrate=True)
        for site in SITES:
            PostgresSiteStore(site, db.dsn).seed_dataset(load_site_records(site.site_id))
        consortium = self.built(HealthcareDemo(database_url=db.dsn, environ={}))
        status = consortium.status()
        self.assertIs(True, status["ready"])
        self.assertEqual(
            [len(load_site_records(site.site_id)) for site in SITES],
            [row["record_count"] for row in status["hospitals"]],
        )


if __name__ == "__main__":
    unittest.main()
