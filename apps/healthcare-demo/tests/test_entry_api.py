"""Data entry over HTTP: the form, the entry, the outcome and the limits.

None of this needs the planner. The record is written to the hospital's own
store, its running profile is updated, and the hospital's next worklist and
the coordinator's overview count it.
"""

from __future__ import annotations

import unittest

from tests.api_support import ApiTestCase
from tests.support import HIGH_RISK_ENTRY

from app.domain.conditions import STROKE_ENTRY_FIELDS, STROKE_FIELD_DOMAINS
from app.domain.dataset import load_site_records
from app.models import ENTRY_REQUIRED_FIELDS
from app.services.consortium import Consortium

FORM_KEYS = {"name", "label", "type", "required", "options", "min", "max", "unit", "help"}


class EntryFormTests(ApiTestCase):
    def test_the_form_is_built_from_the_field_domains(self) -> None:
        payload = self.ok(self.client.get("/api/v1/hospitals/uk-134/entry-form"))
        fields = payload["fields"]
        self.assertEqual(list(STROKE_ENTRY_FIELDS), [field["name"] for field in fields])
        for field in fields:
            with self.subTest(field=field["name"]):
                domain = STROKE_FIELD_DOMAINS[field["name"]]
                self.assertEqual(FORM_KEYS, set(field))
                self.assertEqual(domain.label, field["label"])
                self.assertIn(field["type"], ("integer", "number", "choice", "boolean"))
                self.assertEqual(field["name"] in ENTRY_REQUIRED_FIELDS, field["required"])
                if field["type"] == "choice":
                    self.assertEqual(list(domain.options), field["options"])
                else:
                    self.assertIsNone(field["options"])
                self.assertEqual(domain.minimum, field["min"])
                self.assertEqual(domain.maximum, field["max"])
        by_name = {field["name"]: field for field in fields}
        age = tuple(by_name["age"][key] for key in ("type", "min", "max", "required"))
        self.assertEqual(("integer", 16, 110, True), age)
        self.assertEqual("boolean", by_name["face_weakness"]["type"])
        self.assertFalse(by_name["face_weakness"]["required"])
        self.assertNotIn("poor_outcome_6m", by_name)
        self.assertNotIn("age_band", by_name)

    def test_the_form_states_the_limits_and_the_warning(self) -> None:
        payload = self.ok(self.client.get("/api/v1/hospitals/uk-134/entry-form"))
        self.assertIn("Do not enter real patient information", payload["notice"])
        self.assertEqual(300, payload["max_entered"])
        self.assertEqual(payload["max_entered"] - payload["entered_count"], payload["remaining"])
        self.assertEqual("Idempotency-Key", payload["idempotency"]["header"])
        self.assertEqual("Hospital 134", payload["display_name"])


class EntryTests(ApiTestCase):
    def enter(self, site_id: str = "it-002", body: dict | None = None, key: str | None = None):
        headers = {"Idempotency-Key": key} if key else {}
        path = f"/api/v1/hospitals/{site_id}/encounters"
        return self.client.post(path, json=body or HIGH_RISK_ENTRY, headers=headers)

    def counts(self, site_id: str) -> dict:
        return self.ok(self.client.get(f"/api/v1/hospitals/{site_id}"))["counts"]

    def test_an_entry_is_stored_at_its_hospital_and_flows_through(self) -> None:
        before = {site: self.counts(site) for site in ("uk-134", "it-002", "ch-245")}
        payload = self.ok(self.enter(), 201)
        self.assertEqual({"record", "assessment", "counts", "profile"}, set(payload))
        record = payload["record"]
        self.assertRegex(record["encounter_id"], r"^it-002-e[0-9]+$")
        self.assertEqual("entered", record["origin"])
        self.assertEqual("90+", record["age_band"])
        self.assertNotIn("poor_outcome_6m", record)
        self.assertEqual(before["it-002"]["total"] + 1, payload["counts"]["total"])
        self.assertEqual(before["it-002"]["entered"] + 1, payload["counts"]["entered"])
        self.assertEqual(payload["counts"]["total"], payload["profile"]["record_count"])
        self.assertEqual("urgent", payload["assessment"]["priority"])

        # The hospital's own views include it.
        entered = self.ok(self.client.get("/api/v1/hospitals/it-002/encounters?origin=entered&limit=20"))
        self.assertEqual(record["encounter_id"], entered["encounters"][0]["encounter_id"])
        self.assertIn("assessment", entered["encounters"][0])
        worklist = self.ok(self.client.get("/api/v1/hospitals/it-002/worklist?limit=200"))["worklist"]
        self.assertIn(record["encounter_id"], [row["encounter_id"] for row in worklist])
        # The coordinator sees only the hospital's new count.
        hospitals = {h["site_id"]: h for h in self.ok(self.client.get("/api/v1/overview"))["hospitals"]}
        self.assertEqual(payload["counts"]["total"], hospitals["it-002"]["record_count"])
        self.assertEqual(payload["counts"]["entered"], hospitals["it-002"]["entered_count"])
        # The other hospitals are unchanged.
        self.assertEqual(before["uk-134"], self.counts("uk-134"))
        self.assertEqual(before["ch-245"], self.counts("ch-245"))

    def test_missing_optional_findings_are_accepted(self) -> None:
        body = {key: HIGH_RISK_ENTRY[key] for key in ENTRY_REQUIRED_FIELDS}
        body["face_weakness"] = None
        record = self.ok(self.enter("ch-245", body), 201)["record"]
        self.assertNotIn("face_weakness", record)
        self.assertNotIn("stroke_subtype", record)

    def test_validation_errors_name_each_field_and_store_nothing(self) -> None:
        before = self.counts("it-002")
        body = dict(HIGH_RISK_ENTRY)
        body.update({"age": 200, "sex": "unknown", "face_weakness": "yes", "systolic_bp": "120", "note": "free text"})
        del body["conscious_level"]
        response = self.enter(body=body)
        self.assertEqual(422, response.status_code)
        payload = response.json()
        self.assertEqual("Some fields are not valid.", payload["detail"])
        self.assertEqual(
            {
                "age": "must be between 16 and 110",
                "sex": "must be one of female, male",
                "face_weakness": "must be true or false",
                "systolic_bp": "must be a number",
                "note": "not a field of the record",
                "conscious_level": "is required",
            },
            payload["errors"],
        )
        self.assertEqual(before, self.counts("it-002"))

    def test_identifiers_and_derived_fields_cannot_be_sent(self) -> None:
        extras = ({"encounter_id": "it-002-e1"}, {"age_band": "90+"}, {"poor_outcome_6m": True}, {"origin": "dataset"})
        for extra in extras:
            with self.subTest(extra=extra):
                response = self.enter(body={**HIGH_RISK_ENTRY, **extra})
                self.assertEqual(422, response.status_code)
                self.assertEqual("not a field of the record", response.json()["errors"][next(iter(extra))])

    def test_an_idempotency_key_makes_a_retry_safe(self) -> None:
        first = self.ok(self.enter("uk-134", key="retry-1"), 201)
        again = self.ok(self.enter("uk-134", key="retry-1"), 201)
        self.assertEqual(first["record"]["encounter_id"], again["record"]["encounter_id"])
        self.assertEqual(first["counts"], again["counts"])

        other = self.enter("uk-134", body={**HIGH_RISK_ENTRY, "age": 51}, key="retry-1")
        self.assertEqual(409, other.status_code)
        self.assertIn("Idempotency-Key", other.json()["detail"])

    def test_a_bad_idempotency_key_is_a_field_error(self) -> None:
        for key in ("has spaces", "x" * 101):
            with self.subTest(key=key[:20]):
                response = self.enter(key=key)
                self.assertEqual(422, response.status_code)
                self.assertIn("Idempotency-Key", response.json()["errors"])

    def test_an_outcome_labels_the_entry(self) -> None:
        record = self.ok(self.enter("ch-245"), 201)["record"]
        before = self.counts("ch-245")
        path = f"/api/v1/hospitals/ch-245/encounters/{record['encounter_id']}/outcome"
        payload = self.ok(self.client.post(path, json={"poor_outcome_6m": True}))
        self.assertEqual({"record", "counts", "profile"}, set(payload))
        self.assertTrue(payload["record"]["poor_outcome_6m"])
        self.assertIn("outcome_recorded_at", payload["record"])
        self.assertEqual(before["labelled"] + 1, payload["counts"]["labelled"])
        listed = self.ok(self.client.get("/api/v1/hospitals/ch-245/encounters?origin=entered"))["encounters"]
        self.assertTrue(next(r for r in listed if r["encounter_id"] == record["encounter_id"])["poor_outcome_6m"])

    def test_an_outcome_is_recorded_once(self) -> None:
        record = self.ok(self.client.post("/api/v1/hospitals/ch-245/encounters", json=HIGH_RISK_ENTRY), 201)["record"]
        path = f"/api/v1/hospitals/ch-245/encounters/{record['encounter_id']}/outcome"
        first = self.ok(self.client.post(path, json={"poor_outcome_6m": False}))
        # The same value again is a retry and changes nothing.
        again = self.ok(self.client.post(path, json={"poor_outcome_6m": False}))
        self.assertEqual(first["record"], again["record"])
        response = self.client.post(path, json={"poor_outcome_6m": True})
        self.assertEqual(409, response.status_code)
        self.assertEqual({"detail": "This record already has an outcome. It cannot be changed."}, response.json())
        listed = self.ok(self.client.get("/api/v1/hospitals/ch-245/encounters?origin=entered&limit=50"))["encounters"]
        self.assertIs(False, next(r for r in listed if r["encounter_id"] == record["encounter_id"])["poor_outcome_6m"])

    def test_outcome_errors(self) -> None:
        dataset_id = load_site_records("ch-245")[0]["encounter_id"]
        base = "/api/v1/hospitals/ch-245/encounters"
        response = self.client.post(f"{base}/{dataset_id}/outcome", json={"poor_outcome_6m": True})
        self.assertEqual(422, response.status_code)
        self.assertEqual({"encounter_id": "only entered records take an outcome"}, response.json()["errors"])
        response = self.client.post(f"{base}/ch-245-e9999/outcome", json={"poor_outcome_6m": False})
        self.assertEqual(404, response.status_code)
        response = self.client.post(f"{base}/ch-245-e1/outcome", json={"poor_outcome_6m": "yes"})
        self.assertEqual(422, response.status_code)
        self.assertEqual({"poor_outcome_6m": "must be true or false"}, response.json()["errors"])

    def test_a_large_body_is_refused(self) -> None:
        response = self.client.post(
            "/api/v1/hospitals/it-002/encounters",
            content=b"{" + b" " * (70 * 1024) + b"}",
            headers={"content-type": "application/json"},
        )
        self.assertEqual(413, response.status_code)
        self.assertIn("larger than", response.json()["detail"])

    def test_invalid_json_is_a_body_error(self) -> None:
        response = self.client.post(
            "/api/v1/hospitals/it-002/encounters", content=b"{", headers={"content-type": "application/json"}
        )
        self.assertEqual(422, response.status_code)
        self.assertEqual({"body": "is not valid JSON"}, response.json()["errors"])


class EntryLimitTests(ApiTestCase):
    @classmethod
    def make_consortium(cls) -> Consortium:
        return Consortium(max_entered_per_site=2)

    def test_the_entry_cap_is_a_409_with_a_plain_message(self) -> None:
        for _ in range(2):
            self.ok(self.client.post("/api/v1/hospitals/uk-134/encounters", json=HIGH_RISK_ENTRY), 201)
        response = self.client.post("/api/v1/hospitals/uk-134/encounters", json=HIGH_RISK_ENTRY)
        self.assertEqual(409, response.status_code)
        self.assertEqual(
            "Hospital 134 already holds 2 entered records, the most this demo accepts. "
            "No more records can be entered here.",
            response.json()["detail"],
        )
        form = self.ok(self.client.get("/api/v1/hospitals/uk-134/entry-form"))
        self.assertEqual(0, form["remaining"])
        # Another hospital still accepts entries.
        self.ok(self.client.post("/api/v1/hospitals/it-002/encounters", json=HIGH_RISK_ENTRY), 201)


if __name__ == "__main__":
    unittest.main()
