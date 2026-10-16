"""A missing or broken planner gives a plain 503, and the Guard still fails closed.

These tests replace the planner on purpose: they check what the app does when
the planner cannot be used, which the real planner never shows. The Guard's
decisions themselves are tested against the real planner elsewhere.

The agents run the planner, not the app: each hospital's LMA runs its Guard,
and the GMA checks every model version and plans every round. A page that
shows the model in use therefore needs the planner too. Its 503 has its own
message; the pilot keeps the pilot's message.
"""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from unittest import mock

from tests.api_support import ApiTestCase
from tests.support import dagentsc_available

PLANNER_MESSAGE = "The planner is not available right now, so the pilot cannot run. Try again in a minute."
PLANNER_NEEDED_MESSAGE = "The planner is not available right now. Try again in a minute."


class PlannerUnavailableApiTests(ApiTestCase):
    """POST /api/v1/pilot:run when the planner is missing or answers with the wrong shape."""

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)

    def _stub(self, output: str) -> str:
        path = os.path.join(self._dir.name, "dagentsc")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(f"#!/bin/sh\ncat >/dev/null\nprintf '%s' '{output}'\n")
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
        return path

    def _assert_planner_503(self, binary: str) -> None:
        with mock.patch.dict(os.environ, {"DAGENTSC_BIN": binary}):
            response = self.client.post(
                "/api/v1/pilot:run", json={"round_prefix": "api"}, headers={"Origin": "http://localhost:5174"}
            )
        self.assertEqual(503, response.status_code, response.text[:300])
        self.assertEqual({"detail": PLANNER_MESSAGE}, response.json())
        self.assertIn("x-dagents-app", {key.lower() for key in response.headers})

    def test_a_missing_planner_gives_a_plain_503(self) -> None:
        self._assert_planner_503(os.path.join(self._dir.name, "missing"))

    def test_a_planner_that_exits_with_an_error_gives_a_plain_503(self) -> None:
        self._assert_planner_503("/bin/false")

    def test_a_planner_answer_with_the_wrong_shape_gives_a_plain_503(self) -> None:
        self._assert_planner_503(self._stub('{"decision":"permit"}'))

    def test_the_guard_still_denies_when_the_planner_is_missing(self) -> None:
        """The hospital's LMA runs the Guard. Without the planner it denies the probe and records the denial."""
        def audit() -> dict:
            return self.ok(self.client.get("/api/v1/hospitals/uk-134/audit?limit=500"))

        before = audit()["records"]
        body = {"site_id": "uk-134", "verified": True, "granularity": "table", "cohort_size": 100,
                "requested_fields": ["age_band"]}
        with mock.patch.dict(os.environ, {"DAGENTSC_BIN": os.path.join(self._dir.name, "missing")}):
            decision = self.ok(self.client.post("/api/v1/governance:probe", json=body))
        self.assertFalse(decision["permitted"])
        self.assertEqual(0, decision["released_count"])
        self.assertEqual([], decision["released_fields"])
        after = audit()
        self.assertEqual(len(before) + 1, len(after["records"]), "the denial must be recorded")
        self.assertEqual(decision["audit_id"], after["records"][0]["audit_id"])
        self.assertFalse(after["records"][0]["permitted"])
        self.assertTrue(after["chain_intact"])


class PlannerMissingPageTests(ApiTestCase):
    """Without the planner, the pages that need no model version still answer, and no rate leaves a hospital.

    A fresh app: the model versions are registered with the GMA on first use,
    and that needs the planner, so the pages that show the model in use get a
    plain 503 with the app's header.
    """

    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        patcher = mock.patch.dict(os.environ, {"DAGENTSC_BIN": os.path.join(folder.name, "missing")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_hospital_summaries_answer_without_a_rate(self) -> None:
        hospitals = self.ok(self.client.get("/api/v1/hospitals"))
        self.assertEqual(3, len(hospitals))
        for hospital in hospitals:
            with self.subTest(site=hospital["site_id"]):
                self.assertGreater(hospital["labelled_count"], 20)
                self.assertIsNone(hospital["outcome_rate"])
                # Withheld because the planner could not be asked, not because the cohort is small.
                self.assertEqual("withheld", hospital["outcome_rate_status"])

    def test_the_status_report_and_the_audit_trail_answer(self) -> None:
        status = self.ok(self.client.get("/api/v1/framework/status"))
        self.assertEqual("in-process", status["agents"]["mode"])
        self.assertTrue(status["ready"])
        self.assertTrue(self.ok(self.client.get("/api/v1/hospitals/it-002/audit"))["chain_intact"])

    def test_a_page_that_shows_the_model_in_use_is_a_plain_503(self) -> None:
        for path in ("/api/v1/hospitals/it-002", "/api/v1/hospitals/it-002/worklist", "/api/v1/models",
                     "/api/v1/overview"):
            with self.subTest(path=path):
                response = self.client.get(path, headers={"Origin": "http://localhost:5174"})
                self.assertEqual(503, response.status_code, response.text[:300])
                self.assertEqual({"detail": PLANNER_NEEDED_MESSAGE}, response.json())
                self.assertIn("x-dagents-app", {key.lower() for key in response.headers})
                # Inside the CORS middleware, so the published page can read it.
                self.assertIn("access-control-allow-origin", {key.lower() for key in response.headers})


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class PlannerMissingAfterStartTests(ApiTestCase):
    """The planner goes missing after the model versions were registered: the pages that need it still say so."""

    def test_the_pilot_needs_the_planner_and_the_probe_still_denies(self) -> None:
        self.ok(self.client.get("/api/v1/models"))  # the model versions are now registered
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        with mock.patch.dict(os.environ, {"DAGENTSC_BIN": os.path.join(folder.name, "missing")}):
            pilot = self.client.post("/api/v1/pilot:run", json={"round_prefix": "api"})
            probe = self.client.post("/api/v1/governance:probe", json={"site_id": "ch-245"})
        self.assertEqual(503, pilot.status_code, pilot.text[:300])
        self.assertEqual({"detail": PLANNER_MESSAGE}, pilot.json())
        self.assertEqual(200, probe.status_code)
        self.assertFalse(probe.json()["permitted"])


if __name__ == "__main__":
    unittest.main()
