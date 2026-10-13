"""Text the backend declares for the stroke contract, which the pages show.

- The Guard's obligations name the law that applies to these hospitals. They
  are in the United Kingdom, Italy and Switzerland, so a US law such as HIPAA
  does not apply.
- The entry form's help text matches its controls, and a label does not repeat
  the unit shown next to it.
- The overview carries every field's label, so the pages use the backend's
  labels instead of keeping a second list.
"""

from __future__ import annotations

import unittest

from tests.support import dagentsc_available

from agents.common.domain.governance import KyuAttribute, Requester, RestrictionRequest

from app.domain.conditions import (
    STROKE_APPROVED_PURPOSES,
    STROKE_CLASSIFICATION,
    STROKE_ENTRY_FIELDS,
    STROKE_FIELD_DOMAINS,
)
from app.services.consortium import Consortium

NOT_ASSESSED_HELP = "Choose Not assessed if it could not be assessed."


class RegulationTests(unittest.TestCase):
    def test_no_us_law_is_named(self) -> None:
        for regulation in STROKE_CLASSIFICATION.regulations:
            self.assertNotIn("HIPAA", regulation)
        self.assertEqual(["each hospital's national data protection law"], STROKE_CLASSIFICATION.regulations)

    @unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
    def test_the_guards_obligations_name_the_national_law(self) -> None:
        hospital = Consortium().hospital("ch-245")
        decision = hospital.guard_probe(
            RestrictionRequest(
                request_id="contract-text-probe",
                boundary="before_read",
                requester=Requester(
                    requester_id="analyst",
                    requester_kind="human",
                    affiliation="stroke-consortium",
                    stated_purpose="stroke_triage_research",
                    compliance_history=0.9,
                    attributes=[KyuAttribute(attribute_id="verified_identity", verified=True)],
                ),
                classification=STROKE_CLASSIFICATION,
                requested_fields=["age_band"],
                granularity="table",
                cohort_size=100,
                declared_purpose="stroke_triage_research",
                approved_purposes=STROKE_APPROVED_PURPOSES,
            )
        )
        obligations = decision["plan"]["obligations"]
        self.assertFalse(any("HIPAA" in obligation for obligation in obligations), obligations)
        self.assertTrue(
            any("each hospital's national data protection law" in obligation for obligation in obligations),
            obligations,
        )


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


class FieldLabelTests(unittest.TestCase):
    def test_the_overview_carries_every_fields_label(self) -> None:
        labels = Consortium().overview()["field_labels"]
        self.assertEqual({name: domain.label for name, domain in STROKE_FIELD_DOMAINS.items()}, labels)
        self.assertEqual("Speech difficulty (dysphasia)", labels["speech_difficulty"])
        self.assertEqual("Time since symptom onset", labels["hours_since_onset"])


if __name__ == "__main__":
    unittest.main()
