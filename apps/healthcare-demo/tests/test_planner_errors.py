"""A missing or broken planner gives the pilot a plain 503, and the Guard still fails closed.

These tests replace the planner on purpose: they check what the app does when
the planner cannot be used, which the real planner never shows. The Guard's
decisions themselves are tested against the real planner elsewhere.
"""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from unittest import mock

from tests.api_support import ApiTestCase

PLANNER_MESSAGE = "The planner is not available right now, so the pilot cannot run. Try again in a minute."


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
        hospital = self.consortium.hospital("uk-134")
        before = hospital.governance.recent_audit(1)
        from agents.common.domain.governance import KyuAttribute, Requester, RestrictionRequest
        from app.domain.conditions import STROKE_APPROVED_PURPOSES, STROKE_CLASSIFICATION

        request = RestrictionRequest(
            request_id="planner-missing-probe",
            boundary="before_read",
            requester=Requester(
                requester_id="test",
                requester_kind="human",
                affiliation="test",
                stated_purpose="stroke_triage_research",
                attributes=[KyuAttribute(attribute_id="verified_identity", verified=True)],
            ),
            classification=STROKE_CLASSIFICATION,
            requested_fields=["age_band"],
            granularity="table",
            cohort_size=100,
            declared_purpose="stroke_triage_research",
            approved_purposes=STROKE_APPROVED_PURPOSES,
        )
        with mock.patch.dict(os.environ, {"DAGENTSC_BIN": os.path.join(self._dir.name, "missing")}):
            decision = hospital.guard_probe(request)
        self.assertFalse(decision["permitted"])
        after = hospital.governance.recent_audit(1)
        self.assertNotEqual(before, after, "the denial must be recorded")
        self.assertTrue(hospital.governance.audit_chain_intact())


if __name__ == "__main__":
    unittest.main()
