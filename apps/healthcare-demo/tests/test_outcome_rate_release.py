"""A hospital reports its outcome rate only when its Guard's planner permits it.

The rate is the share of labelled records with a poor outcome. It leaves the
hospital in its summary and in an analytics round's profile. The hospital's
Dagents LMA asks its planner, and the planner's cohort rule decides whether it
may leave: below the classification's minimum cohort the request is denied.
Neither the app nor the LMA compares the count with the minimum itself. These
tests use the real planner and skip without it.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from tests.support import built_demo, dagentsc_available

from app.domain.conditions import STROKE_CLASSIFICATION, STROKE_LABEL_FIELD
from app.domain.dataset import Site, load_site_records
from app.services.agents import HealthcareDemo
from app.services.site_store import MemorySiteStore


def labelled_site(labelled: int, total: int = 30, site_id: str = "rate-001") -> tuple[Site, MemorySiteStore]:
    """A hospital with ``total`` records, the first ``labelled`` of them with an outcome."""
    site = Site(site_id, "Rate clinic", "Nowhere", 0, "site_" + site_id.replace("-", "_"), total)
    records = []
    for number, record in enumerate(load_site_records("uk-134")[:total], start=1):
        copy = dict(record)
        copy["encounter_id"] = f"{site_id}-{number:05d}"
        copy["source_row"] = number
        copy[STROKE_LABEL_FIELD] = (number % 3 == 0) if number <= labelled else None
        records.append(copy)
    return site, MemorySiteStore(site, records)


def demo_with(labelled: int) -> tuple[HealthcareDemo, str]:
    site, store = labelled_site(labelled)
    return HealthcareDemo(sites=[site], site_stores={site.site_id: store}), site.site_id


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class OutcomeRateReleaseTests(unittest.TestCase):
    def consortium_with(self, labelled: int) -> tuple[HealthcareDemo, str]:
        """App services with one hospital holding ``labelled`` labelled records, stopped when the test ends."""
        demo, site_id = demo_with(labelled)
        self.addCleanup(demo.close)
        return demo, site_id

    def test_the_minimum_cohort_is_twenty(self) -> None:
        # The thresholds below are written for this classification.
        self.assertEqual(20, STROKE_CLASSIFICATION.minimum_cohort)

    def test_nineteen_labelled_records_report_no_rate(self) -> None:
        consortium, site_id = self.consortium_with(19)
        summary = consortium.hospital(site_id).summary()
        self.assertEqual(19, summary["labelled_count"])
        self.assertIsNone(summary["outcome_rate"])
        self.assertEqual("below_minimum_cohort", summary["outcome_rate_status"])

    def test_twenty_labelled_records_report_a_rate(self) -> None:
        consortium, site_id = self.consortium_with(20)
        summary = consortium.hospital(site_id).summary()
        self.assertEqual(20, summary["labelled_count"])
        self.assertAlmostEqual(6 / 20, summary["outcome_rate"])
        self.assertEqual("released", summary["outcome_rate_status"])

    def test_no_labelled_record_reports_that_there_are_no_outcomes(self) -> None:
        consortium, site_id = self.consortium_with(0)
        summary = consortium.hospital(site_id).summary()
        self.assertIsNone(summary["outcome_rate"])
        self.assertEqual("no_outcomes", summary["outcome_rate_status"])

    def test_the_dashboard_reports_the_same_status(self) -> None:
        for labelled, status in ((19, "below_minimum_cohort"), (20, "released")):
            with self.subTest(labelled=labelled):
                consortium, site_id = self.consortium_with(labelled)
                self.assertEqual(status, consortium.hospital(site_id).dashboard()["outcome_rate_status"])

    def test_a_denial_for_another_reason_is_not_reported_as_a_small_cohort(self) -> None:
        """The status comes from the planner's own reason, not from comparing the count."""
        from dagents.common.domain.governance import RestrictionPlan

        consortium, site_id = self.consortium_with(25)
        governance = built_demo(consortium)[f"lma:{site_id}"].state.site.governance
        hospital = consortium.hospital(site_id)
        real = governance.plan

        def denied(request):
            plan = real(request)
            return RestrictionPlan.model_validate(
                {**plan.model_dump(mode="json"), "decision": "deny", "violations": ["declared purpose is not approved"]}
            )

        with mock.patch.object(governance, "plan", side_effect=denied):
            summary = hospital.summary()
        self.assertIsNone(summary["outcome_rate"])
        self.assertEqual("denied", summary["outcome_rate_status"])

    def test_the_profile_round_follows_the_same_decision(self) -> None:
        for labelled, expected in ((19, False), (20, True)):
            with self.subTest(labelled=labelled):
                consortium, site_id = self.consortium_with(labelled)
                # What the hospital's LMA reports for a profile job: the
                # pilot's report calls the LMA's label rate the outcome rate.
                site = built_demo(consortium)[f"lma:{site_id}"].state.site
                hospital = consortium.hospital(site_id)
                metrics = site.profile_metrics(hospital.records())
                self.assertEqual(expected, "label_rate" in metrics, metrics)

    def test_asking_the_planner_writes_no_audit_record(self) -> None:
        # Page views call summary() often; they must not grow the audit chain.
        consortium, site_id = self.consortium_with(25)
        hospital = consortium.hospital(site_id)
        before = len(hospital.audit_trail(500)["records"])
        for _ in range(3):
            hospital.summary()
        self.assertEqual(before, len(hospital.audit_trail(500)["records"]))

    def test_without_a_planner_no_rate_is_reported(self) -> None:
        consortium, site_id = self.consortium_with(25)
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {"DAGENTSC_BIN": os.path.join(directory, "missing")}):
                summary = consortium.hospital(site_id).summary()
        self.assertEqual(25, summary["labelled_count"])
        self.assertIsNone(summary["outcome_rate"])
        # Withheld because the planner could not be asked, not because the cohort is small.
        self.assertEqual("withheld", summary["outcome_rate_status"])

    def test_a_planner_failure_is_not_remembered(self) -> None:
        consortium, site_id = self.consortium_with(25)
        hospital = consortium.hospital(site_id)
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {"DAGENTSC_BIN": os.path.join(directory, "missing")}):
                self.assertIsNone(hospital.summary()["outcome_rate"])
        self.assertIsNotNone(hospital.summary()["outcome_rate"])


if __name__ == "__main__":
    unittest.main()
