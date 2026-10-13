"""A record entered at a hospital flows through that hospital's store, profile and rounds.

Every assertion runs against both store implementations: the in-memory stores
of a local run and the Postgres schemas of the deployed API. The Postgres half
needs ``HEALTHCARE_DEMO_TEST_DATABASE_URL`` (see ``test_site_isolation.py``).
The round tests also need the planner.
"""

from __future__ import annotations

from pathlib import Path
import sys
from typing import Any
import unittest
from unittest import mock

APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT / "backend"))
sys.path.insert(0, str(APP_ROOT.parents[1]))

from agents.common.application.running_profile import RunningProfile  # noqa: E402

from app.domain import stroke_rule  # noqa: E402
from app.domain.dataset import SITES, load_site_records  # noqa: E402
from app.services.consortium import Consortium  # noqa: E402
from app.services.hospital import PROFILE_FIELDS, EntryLimitReached  # noqa: E402
from app.services.site_store import (  # noqa: E402
    IdempotencyKeyReused,
    PostgresSiteStore,
    RecordRejected,
    StoreUnavailableError,
    UnknownEncounterError,
)
from tests.support import HIGH_RISK_ENTRY, dagentsc_available  # noqa: E402
from tests.test_site_isolation import SupabaseLikeDatabase, admin_dsn_or_skip  # noqa: E402

#: A record with gaps: the store accepts missing values.
PARTIAL_ENTRY = {"age": 58, "sex": "male", "conscious_level": "alert", "arm_weakness": True, "systolic_bp": 145.0}

#: A low-risk record.
LOW_RISK_ENTRY = {
    "age": 45,
    "sex": "male",
    "conscious_level": "alert",
    "face_weakness": False,
    "arm_weakness": True,
    "leg_weakness": False,
    "speech_difficulty": False,
    "hours_since_onset": 20.0,
    "systolic_bp": 140.0,
}

PLANNER = dagentsc_available()


def _by_site(round_report: dict[str, Any], site_id: str) -> dict[str, Any]:
    return next(result for result in round_report["results"] if result["site_id"] == site_id)


class DataEntryContract:
    """The data-entry flow. Subclasses supply the consortium."""

    def make_consortium(self, **kwargs: Any) -> Consortium:
        raise NotImplementedError

    def setUp(self) -> None:
        self.consortium = self.make_consortium()
        self.hospital = self.consortium.hospital("uk-134")

    def test_an_entry_is_stored_and_counted_once(self) -> None:
        before = self.hospital.counts()
        profile_before = self.hospital.profile_snapshot()

        result = self.hospital.enter(HIGH_RISK_ENTRY, "entry-1")
        record = result["record"]
        self.assertEqual("uk-134-e1", record["encounter_id"])
        self.assertEqual("entered", record["origin"])
        self.assertEqual("90+", record["age_band"])
        self.assertNotIn("poor_outcome_6m", record)
        self.assertEqual(before["total"] + 1, result["counts"]["total"])
        self.assertEqual(before["entered"] + 1, result["counts"]["entered"])
        self.assertEqual(before["labelled"], result["counts"]["labelled"])
        self.assertEqual(profile_before["record_count"] + 1, result["profile"]["record_count"])
        self.assertEqual(1, result["profile"]["fields"]["origin"]["categories"]["entered"])
        self.assertEqual("urgent", result["assessment"]["priority"])
        self.assertEqual(stroke_rule.RULE_VERSION, result["assessment"]["model_version"])

        again = self.hospital.enter(HIGH_RISK_ENTRY, "entry-1")
        self.assertEqual("uk-134-e1", again["record"]["encounter_id"])
        self.assertEqual(result["counts"], again["counts"])
        self.assertEqual(result["profile"], again["profile"])

    def test_the_running_profile_matches_one_built_from_the_store(self) -> None:
        """Updated one record at a time, the profile equals a profile recomputed from every row."""
        first = self.hospital.enter(HIGH_RISK_ENTRY, "p-1")["record"]["encounter_id"]
        second = self.hospital.enter(PARTIAL_ENTRY, "p-2")["record"]["encounter_id"]
        self.hospital.enter(LOW_RISK_ENTRY, "p-3")
        fourth = self.hospital.enter({**PARTIAL_ENTRY, "age": 71}, "p-4")["record"]["encounter_id"]
        # An outcome is recorded once, so each record is labelled once.
        self.hospital.set_outcome(first, True)
        self.hospital.set_outcome(second, False)
        self.hospital.set_outcome(fourth, True)

        rebuilt = RunningProfile(PROFILE_FIELDS)
        rebuilt.add_many(self.hospital.store.records())
        incremental = self.hospital.profile_snapshot()
        expected = rebuilt.snapshot()
        self.assertEqual(expected["record_count"], incremental["record_count"])
        for name, stats in expected["fields"].items():
            with self.subTest(field=name):
                got = incremental["fields"][name]
                for key in ("present", "missing", "kind", "categories", "booleans", "other"):
                    self.assertEqual(stats[key], got[key])
                if stats["numeric"] is not None:
                    for key in ("count", "min", "max"):
                        self.assertEqual(stats["numeric"][key], got["numeric"][key])
                    self.assertAlmostEqual(stats["numeric"]["mean"], got["numeric"]["mean"], places=9)
                    self.assertAlmostEqual(stats["numeric"]["variance"], got["numeric"]["variance"], places=9)

    def test_invalid_entries_raise_typed_errors_and_change_nothing(self) -> None:
        before = self.hospital.counts()
        profile = self.hospital.profile_snapshot()
        with self.assertRaises(RecordRejected) as caught:
            self.hospital.enter({**HIGH_RISK_ENTRY, "age": 140})
        self.assertIn("age", caught.exception.errors)
        with self.assertRaises(RecordRejected) as caught:
            self.hospital.enter({**HIGH_RISK_ENTRY, "patient_name": "someone"})
        self.assertIn("patient_name", caught.exception.errors)
        with self.assertRaises(RecordRejected):
            self.hospital.enter({**HIGH_RISK_ENTRY, "conscious_level": "confused"})
        self.hospital.enter(LOW_RISK_ENTRY, "key")
        with self.assertRaises(IdempotencyKeyReused):
            self.hospital.enter(HIGH_RISK_ENTRY, "key")
        self.assertEqual(before["entered"] + 1, self.hospital.counts()["entered"])
        self.assertEqual(profile["record_count"] + 1, self.hospital.profile_snapshot()["record_count"])

    def test_an_outcome_counts_the_record_as_labelled(self) -> None:
        site = self.consortium.hospital("ch-245")
        entry = site.enter(LOW_RISK_ENTRY, "outcome")["record"]["encounter_id"]
        before = site.summary()
        booleans = site.profile_snapshot()["fields"]["poor_outcome_6m"]["booleans"]

        result = site.set_outcome(entry, True)
        self.assertIs(True, result["record"]["poor_outcome_6m"])
        self.assertIn("outcome_recorded_at", result["record"])
        self.assertEqual(before["labelled_count"] + 1, result["counts"]["labelled"])
        after = result["profile"]["fields"]["poor_outcome_6m"]["booleans"]
        self.assertEqual(booleans["true"] + 1, after["true"])
        self.assertEqual(booleans["false"], after["false"])
        summary = site.summary()
        positives, labelled = booleans["true"] + 1, before["labelled_count"] + 1
        if PLANNER:
            self.assertAlmostEqual(positives / labelled, summary["outcome_rate"], places=12)
        else:
            # The planner decides whether the rate may leave; without it, it does not.
            self.assertIsNone(summary["outcome_rate"])

        dataset_row = load_site_records("ch-245")[0]["encounter_id"]
        with self.assertRaises(RecordRejected):
            site.set_outcome(dataset_row, False)
        with self.assertRaises(UnknownEncounterError):
            site.set_outcome("ch-245-e999", False)

    def test_the_worklist_entries_and_charts_include_a_new_record_at_once(self) -> None:
        bands_before = self.hospital.outcome_by_age_band()
        risk_before = self.hospital.risk_score_distribution()
        entry = self.hospital.enter(HIGH_RISK_ENTRY, "worklist")["record"]["encounter_id"]

        [top] = self.hospital.worklist(limit=1)
        self.assertEqual(entry, top["encounter_id"])
        [latest] = self.hospital.entered(limit=1)
        self.assertEqual(entry, latest["encounter_id"])
        self.assertEqual("urgent", latest["assessment"]["priority"])

        bands = self.hospital.outcome_by_age_band()
        index = bands["categories"].index("90+")
        self.assertEqual(bands_before["unlabelled"][index] + 1, bands["unlabelled"][index])
        risk = self.hospital.risk_score_distribution()
        self.assertEqual(
            risk_before["series"]["outcome not recorded"][-1] + 1, risk["series"]["outcome not recorded"][-1]
        )

    def test_chart_data_adds_up_to_the_hospital_counts(self) -> None:
        counts = self.hospital.counts()
        bands = self.hospital.outcome_by_age_band()
        self.assertEqual(counts["labelled"], sum(bands["labelled"]))
        self.assertEqual(counts["total"], sum(bands["labelled"]) + sum(bands["unlabelled"]))
        presentation = self.hospital.presentation_shares()
        self.assertEqual(6, len(presentation["share"]))
        for found, recorded in zip(presentation["with_finding"], presentation["recorded"], strict=True):
            self.assertLessEqual(found, recorded)
            self.assertLessEqual(recorded, counts["total"])
        histogram = self.hospital.systolic_bp_histogram()
        self.assertEqual(counts["total"], sum(histogram["counts"]) + histogram["missing"])
        self.assertEqual(60, histogram["bin_edges"][0])
        self.assertEqual(300, histogram["bin_edges"][-1])
        risk = self.hospital.risk_score_distribution()
        self.assertEqual(counts["total"], sum(map(sum, risk["series"].values())) + risk["not_scored"])
        labelled = sum(risk["series"]["poor outcome"]) + sum(risk["series"]["no poor outcome"])
        self.assertEqual(counts["labelled"], labelled)

    def test_the_dashboard_shows_the_hospital_its_own_numbers(self) -> None:
        dashboard = self.hospital.dashboard()
        counts = self.hospital.counts()
        self.assertEqual(counts, dashboard["counts"])
        self.assertEqual(counts["total"], dashboard["profile"]["record_count"])
        self.assertEqual(counts["labelled"], dashboard["local_metrics"]["labelled_records"])
        self.assertIn("auc", dashboard["local_metrics"]["metrics"])
        self.assertEqual(stroke_rule.RULE_VERSION, dashboard["current_model"]["version"])
        self.assertEqual(self.hospital.store.kind, dashboard["store"]["kind"])
        self.assertEqual([], dashboard["contributions"])

    def test_the_entry_limit_refuses_more_records(self) -> None:
        consortium = self.make_consortium(max_entered_per_site=1)
        site = consortium.hospital("it-002")
        site.enter(LOW_RISK_ENTRY, "limit-1")
        with self.assertRaises(EntryLimitReached):
            site.enter(HIGH_RISK_ENTRY, "limit-2")
        self.assertEqual(1, site.counts()["entered"])

    @unittest.skipUnless(PLANNER, "dagentsc binary is not available")
    def test_labelled_entries_reach_the_next_round(self) -> None:
        """Entries count in the next round: all of them for profiling, labelled ones for evaluation and training."""
        initial = self.consortium.registry.ensure_initial()
        analytics = self.consortium.run_round("entry-analytics-1", "analytics")
        evaluation = self.consortium.run_round("entry-evaluation-1", "evaluation")
        training = self.consortium.run_round("entry-training-1", "training", initial)
        before = {
            "analytics": _by_site(analytics, "uk-134")["contributed_examples"],
            "evaluation": _by_site(evaluation, "uk-134")["contributed_examples"],
            "training": _by_site(training, "uk-134")["contributed_examples"],
        }

        entries = [self.hospital.enter(entry, f"round-{index}")["record"]["encounter_id"]
                   for index, entry in enumerate((HIGH_RISK_ENTRY, LOW_RISK_ENTRY, PARTIAL_ENTRY))]
        self.hospital.set_outcome(entries[0], True)
        self.hospital.set_outcome(entries[1], False)

        analytics = self.consortium.run_round("entry-analytics-2", "analytics")
        evaluation = self.consortium.run_round("entry-evaluation-2", "evaluation")
        training = self.consortium.run_round("entry-training-2", "training", initial)
        self.assertEqual(before["analytics"] + 3, _by_site(analytics, "uk-134")["contributed_examples"])
        self.assertEqual(before["evaluation"] + 2, _by_site(evaluation, "uk-134")["contributed_examples"])
        self.assertEqual(before["training"] + 2, _by_site(training, "uk-134")["contributed_examples"])
        registration = {site.site_id: site for site in self.consortium.controller.list_sites()}["uk-134"]
        self.assertEqual(self.hospital.counts()["total"], registration.cohort_size)
        self.assertEqual(before["analytics"] + 3, registration.cohort_size)
        self.assertEqual(
            sum(result["contributed_examples"] for result in evaluation["results"]),
            evaluation["readiness"]["accepted_examples"],
        )


class ConstructionTests(unittest.TestCase):
    """Building a consortium reads nothing; a store that cannot be reached says which one."""

    UNREACHABLE = "postgresql://demo:demo@127.0.0.1:1/healthcare"

    def test_building_a_consortium_reads_no_records(self) -> None:
        with mock.patch("app.services.consortium.load_site_records") as loader:
            consortium = Consortium()
            self.assertEqual(["uk-134", "it-002", "ch-245"], list(consortium.hospitals))
            loader.assert_not_called()
            Consortium(database_url=self.UNREACHABLE, environ={})
            loader.assert_not_called()

    def test_an_unreachable_database_raises_and_names_the_store(self) -> None:
        consortium = Consortium(database_url=self.UNREACHABLE, environ={})
        self.assertEqual("postgres", consortium.data_source_status()["cohort_source"])
        with self.assertRaises(StoreUnavailableError) as caught:
            consortium.hospital("uk-134").summary()
        self.assertIn("hospital uk-134", str(caught.exception))
        with self.assertRaises(StoreUnavailableError) as caught:
            consortium.model_in_use()
        self.assertIn("coordinator store", str(caught.exception))

    def test_an_unknown_hospital_is_a_key_error(self) -> None:
        with self.assertRaises(KeyError):
            Consortium().hospital("nowhere")


class MemoryDataEntryTests(DataEntryContract, unittest.TestCase):
    def make_consortium(self, **kwargs: Any) -> Consortium:
        return Consortium(**kwargs)


class PostgresDataEntryTests(DataEntryContract, unittest.TestCase):
    db: SupabaseLikeDatabase

    @classmethod
    def setUpClass(cls) -> None:
        cls.db = SupabaseLikeDatabase(admin_dsn_or_skip())
        try:
            cls.db.start()
            for site in SITES:
                PostgresSiteStore(site, cls.db.dsn).seed_dataset(load_site_records(site.site_id))
        except BaseException:
            cls.db.stop()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.stop()

    def make_consortium(self, **kwargs: Any) -> Consortium:
        return Consortium(database_url=self.db.dsn, environ={}, **kwargs)

    def setUp(self) -> None:
        with self.db.admin() as conn:
            for site in SITES:
                conn.execute(f"DELETE FROM {site.schema}.encounters WHERE origin = 'entered'")
            conn.execute("DELETE FROM coordinator.site_contributions")
            conn.execute("UPDATE coordinator.models SET parent_version = NULL")
            conn.execute("DELETE FROM coordinator.models")
        super().setUp()

    def test_the_hospital_reports_its_own_schema(self) -> None:
        summary = self.hospital.summary()
        self.assertEqual({"kind": "postgres", "schema": "site_uk_134", "role": "site_uk_134"}, summary["store"])
        self.assertEqual(524, summary["record_count"])


if __name__ == "__main__":
    unittest.main()
