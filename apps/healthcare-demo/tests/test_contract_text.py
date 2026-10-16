"""Text the backend declares for the stroke contract, which the pages show.

- The Guard's obligations name the law that applies to these hospitals. They
  are in the United Kingdom, Italy and Switzerland, so a US law such as HIPAA
  does not apply. The check asks a Dagents LMA that serves one hospital with
  this app's extension, so it reads the classification and the condition pack
  the way the demo server's agents do.
- The condition pack's approved purposes reach that Guard: a purpose outside
  them is denied.
- The entry form's help text matches its controls, and a label does not repeat
  the unit shown next to it.
- The overview carries every field's label, so the pages use the backend's
  labels instead of keeping a second list.
"""

from __future__ import annotations

import secrets
from typing import Any
import unittest

from fastapi.testclient import TestClient

from tests.api_support import ApiTestCase
from tests.support import dagentsc_available

from agents.common.domain.governance import KyuAttribute, Requester
from agents.common.extensions import ExtensionRegistry
from agents.common.infrastructure.site_sources import StaticSiteSource
from agents.lma.app_factory import LmaDeps, create_app
from agents.lma.config import Settings
from agents.lma.domain.site_config import SiteConfig

from app.domain.conditions import (
    STROKE_CLASSIFICATION,
    STROKE_CONDITION,
    STROKE_ENTRY_FIELDS,
    STROKE_FIELD_DOMAINS,
    STROKE_STUDY_ID,
)
from app.domain.dataset import load_site_records
from app.extension import HealthcareStrokeExtension

NOT_ASSESSED_HELP = "Choose Not assessed if it could not be assessed."
NATIONAL_LAW = "each hospital's national data protection law"


def analyst() -> Requester:
    return Requester(
        requester_id="analyst",
        requester_kind="human",
        affiliation="stroke-consortium",
        stated_purpose="stroke_triage_research",
        compliance_history=0.9,
        attributes=[KyuAttribute(attribute_id="verified_identity", verified=True)],
    )


class HospitalLma:
    """One Dagents LMA serving a hospital's committed records with this app's extension, in a fresh registry."""

    def __init__(self, site_id: str) -> None:
        registry = ExtensionRegistry()
        registry.register(HealthcareStrokeExtension())
        token = secrets.token_urlsafe(24)
        self.headers = {"Authorization": f"Bearer {token}"}
        app = create_app(
            Settings(site_token=token, coordinator_token=secrets.token_urlsafe(24)),
            LmaDeps(
                registry=registry,
                site=SiteConfig(
                    site_id=site_id,
                    condition_id=STROKE_CONDITION.condition_id,
                    study_id=STROKE_STUDY_ID,
                    reporting_purpose="stroke_triage_research",
                ),
                source=StaticSiteSource(load_site_records(site_id), location={"extract": "ist"}),
            ),
        )
        self.client = TestClient(app)

    def probe(self, **overrides: Any) -> dict[str, Any]:
        body: dict[str, Any] = {
            "boundary": "before_read",
            "granularity": "table",
            "requested_fields": ["age_band"],
            "cohort_size": 100,
            "requester": analyst().model_dump(mode="json"),
            "declared_purpose": "stroke_triage_research",
        }
        body.update(overrides)
        response = self.client.post("/api/v1/governance/restrictions:probe", json=body, headers=self.headers)
        if response.status_code != 200:
            raise AssertionError(f"{response.status_code}: {response.text[:500]}")
        return response.json()


class RegulationTests(unittest.TestCase):
    def test_no_us_law_is_named(self) -> None:
        for regulation in STROKE_CLASSIFICATION.regulations:
            self.assertNotIn("HIPAA", regulation)
        self.assertEqual([NATIONAL_LAW], STROKE_CLASSIFICATION.regulations)

    @unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
    def test_the_guards_obligations_name_the_national_law(self) -> None:
        decision = HospitalLma("ch-245").probe()
        self.assertTrue(decision["permitted"], decision)
        obligations = decision["plan"]["obligations"]
        self.assertFalse(any("HIPAA" in obligation for obligation in obligations), obligations)
        self.assertTrue(any(NATIONAL_LAW in obligation for obligation in obligations), obligations)


class PurposeTests(unittest.TestCase):
    @unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
    def test_a_purpose_outside_the_condition_pack_is_denied(self) -> None:
        lma = HospitalLma("ch-245")
        for purpose in STROKE_CONDITION.approved_purposes:
            with self.subTest(purpose=purpose):
                self.assertTrue(lma.probe(declared_purpose=purpose)["permitted"])
        refused = lma.probe(declared_purpose="marketing")
        self.assertFalse(refused["permitted"], refused)
        self.assertEqual(0, refused["released_count"])


class EntryFormTextTests(unittest.TestCase):
    def test_help_for_an_assessed_finding_names_the_not_assessed_choice(self) -> None:
        for name in STROKE_ENTRY_FIELDS:
            domain = STROKE_FIELD_DOMAINS[name]
            with self.subTest(field=name):
                self.assertNotIn("Leave empty", domain.help or "")
        for name in ("face_weakness", "arm_weakness", "leg_weakness", "speech_difficulty", "visual_field_loss"):
            self.assertEqual(NOT_ASSESSED_HELP, STROKE_FIELD_DOMAINS[name].help, name)

    def test_no_label_repeats_its_unit(self) -> None:
        for name, domain in STROKE_FIELD_DOMAINS.items():
            if domain.unit:
                with self.subTest(field=name):
                    self.assertNotIn(domain.unit.lower(), domain.label.lower())


class FieldLabelTests(ApiTestCase):
    # The overview shows the model in use, which the GMA checks with the planner.
    @unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
    def test_the_overview_carries_every_fields_label(self) -> None:
        labels = self.ok(self.client.get("/api/v1/overview"))["field_labels"]
        self.assertEqual({name: domain.label for name, domain in STROKE_FIELD_DOMAINS.items()}, labels)
        self.assertEqual("Speech difficulty (dysphasia)", labels["speech_difficulty"])
        self.assertEqual("Time since symptom onset", labels["hours_since_onset"])


if __name__ == "__main__":
    unittest.main()
