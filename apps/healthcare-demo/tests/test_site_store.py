"""The in-memory and Postgres stores behave the same way.

Every assertion here runs against both implementations: ``MemorySiteStore``,
which a local run uses, and ``PostgresSiteStore``, which the deployed API uses.
The Postgres half needs ``HEALTHCARE_DEMO_TEST_DATABASE_URL`` (see
``test_site_isolation.py``) and skips without it.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sys
import time
from typing import Any
import unittest
import uuid

APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT / "backend"))
sys.path.insert(0, str(APP_ROOT.parents[1]))

from agents.common.domain.governance import AuditRecord  # noqa: E402
from agents.common.infrastructure.postgres_roles import acting_as  # noqa: E402
from app.domain.dataset import SITES, load_site_records, site_for  # noqa: E402
from app.services import site_store  # noqa: E402
from app.services.site_store import (  # noqa: E402
    IdempotencyKeyReused,
    MemoryCoordinatorStore,
    MemorySiteStore,
    ModelVersionConflict,
    OutcomeAlreadyRecorded,
    PostgresCoordinatorStore,
    PostgresSiteStore,
    RecordRejected,
    ROW_COLUMNS,
    UnknownEncounterError,
    UnknownModelError,
    site_database_url,
)
from tests.test_site_isolation import SupabaseLikeDatabase, admin_dsn_or_skip  # noqa: E402

UK = site_for("uk-134")
ITALY = site_for("it-002")

#: Three hand-written dataset rows in the extract's format: complete, with gaps,
#: and labelled false.
FIXTURE: list[dict[str, Any]] = [
    {
        "encounter_id": "uk-134-00007",
        "source_row": 7,
        "origin": "dataset",
        "age": 72,
        "age_band": "70-79",
        "sex": "female",
        "conscious_level": "drowsy",
        "face_weakness": True,
        "arm_weakness": True,
        "leg_weakness": False,
        "speech_difficulty": True,
        "hours_since_onset": 6.0,
        "systolic_bp": 150.0,
        "visual_field_loss": True,
        "atrial_fibrillation": True,
        "onset_on_waking": False,
        "stroke_subtype": "TACS",
        "poor_outcome_6m": True,
    },
    {
        "encounter_id": "uk-134-00003",
        "source_row": 3,
        "origin": "dataset",
        "age": 58,
        "age_band": "50-59",
        "sex": "male",
        "conscious_level": "alert",
        "arm_weakness": False,
        "hours_since_onset": 30.5,
        "systolic_bp": 182.0,
        "stroke_subtype": "LACS",
    },
    {
        "encounter_id": "uk-134-00012",
        "source_row": 12,
        "origin": "dataset",
        "age": 91,
        "age_band": "90+",
        "sex": "female",
        "conscious_level": "unconscious",
        "face_weakness": False,
        "hours_since_onset": 2.0,
        "systolic_bp": 95.0,
        "onset_on_waking": True,
        "stroke_subtype": "other",
        "poor_outcome_6m": False,
    },
]

ENTRY = {"age": 67, "sex": "male", "conscious_level": "alert", "arm_weakness": True, "systolic_bp": 171}

#: A release decision as the pilot stores it.
RELEASE_DECISION = {
    "action": "release",
    "guard_permitted": True,
    "gate_results": [
        {"gate_id": "discrimination", "metric": "auc", "observed": 0.79, "outcome": "pass", "blocking": True},
    ],
    "round_id": "pilot-20261011T000000Z-abc123-training-5",
}


def audit_record(request_id: str) -> AuditRecord:
    return AuditRecord(
        audit_id=str(uuid.uuid4()),
        recorded_at=int(time.time()),
        boundary="before_read",
        request_id=request_id,
        requester_id="tester",
        decision="permit",
        permitted=True,
        granularity="table",
        filtering_score=0.25,
        withheld_fields=["age"],
    )


class SiteStoreContract:
    """The behaviour both stores must have. Subclasses supply ``make_store``."""

    expected_kind = ""

    def make_store(self, site: Any, records: list[dict[str, Any]]) -> Any:
        raise NotImplementedError

    def setUp(self) -> None:
        self.store = self.make_store(UK, FIXTURE)

    # -- reading -------------------------------------------------------------

    def test_kind_and_location(self) -> None:
        self.assertEqual(self.expected_kind, self.store.kind)
        self.assertEqual("uk-134", self.store.site_id)
        if self.expected_kind == "postgres":
            self.assertEqual({"schema": "site_uk_134", "role": "site_uk_134"}, self.store.location)
        else:
            self.assertEqual({"memory": "process"}, self.store.location)

    def test_records_are_in_source_order_with_missing_values_absent(self) -> None:
        records = self.store.records()
        self.assertEqual(["uk-134-00003", "uk-134-00007", "uk-134-00012"], [r["encounter_id"] for r in records])
        by_id = {record["encounter_id"]: record for record in records}
        for expected in FIXTURE:
            self.assertEqual(expected, by_id[expected["encounter_id"]])
        self.assertNotIn("poor_outcome_6m", by_id["uk-134-00003"])
        self.assertNotIn(None, [value for record in records for value in record.values()])

    def test_counts(self) -> None:
        self.assertEqual({"total": 3, "entered": 0, "labelled": 2}, self.store.count())

    # -- entering records ------------------------------------------------------

    def test_append_numbers_entries_and_marks_them_entered(self) -> None:
        first = self.store.append(ENTRY, None)
        second = self.store.append({"age": 44, "sex": "female"}, None)
        self.assertEqual("uk-134-e1", first["encounter_id"])
        self.assertEqual("uk-134-e2", second["encounter_id"])
        self.assertEqual("entered", first["origin"])
        self.assertNotIn("source_row", first)
        self.assertEqual("60-69", first["age_band"], "the band is filled in from the age")
        self.assertEqual(171.0, first["systolic_bp"])
        self.assertIsInstance(first["systolic_bp"], float)
        self.assertIsInstance(datetime.fromisoformat(first["recorded_at"]), datetime)
        self.assertEqual({"total": 5, "entered": 2, "labelled": 2}, self.store.count())
        self.assertEqual(["uk-134-e1", "uk-134-e2"], [r["encounter_id"] for r in self.store.records()[-2:]])
        self.assertNotIn("recorded_at", self.store.records()[-1], "records() holds row columns only")

    def test_numbering_continues_past_nine(self) -> None:
        ids = [self.store.append({"age": 50}, None)["encounter_id"] for _ in range(11)]
        self.assertEqual(f"uk-134-e{11}", ids[-1])
        self.assertEqual(ids, [r["encounter_id"] for r in self.store.records()[3:]])

    def test_append_is_idempotent_on_the_key(self) -> None:
        first = self.store.append(ENTRY, "key-1")
        again = self.store.append(dict(ENTRY), "key-1")
        self.assertEqual(first, again)
        self.assertEqual(1, self.store.count()["entered"])

    def test_a_key_reused_for_a_different_record_is_refused(self) -> None:
        self.store.append(ENTRY, "key-2")
        with self.assertRaises(IdempotencyKeyReused):
            self.store.append({**ENTRY, "systolic_bp": 120}, "key-2")
        self.assertEqual(1, self.store.count()["entered"])

    def test_a_retry_after_the_outcome_was_recorded_returns_the_entry(self) -> None:
        entry = self.store.append(ENTRY, "key-3")
        self.store.set_outcome(entry["encounter_id"], True)
        again = self.store.append(ENTRY, "key-3")
        self.assertEqual(entry["encounter_id"], again["encounter_id"])
        self.assertTrue(again["poor_outcome_6m"])

    def test_append_refuses_fields_outside_the_schema(self) -> None:
        cases = {
            "identifier": {"age": 60, "patient_name": "x"},
            "row key": {"encounter_id": "uk-134-e99"},
            "origin": {"origin": "dataset"},
            "range": {"systolic_bp": 301},
            "category": {"sex": "unknown"},
            "type": {"arm_weakness": "yes"},
            "whole number": {"age": 60.5},
            "band": {"age": 60, "age_band": "70-79"},
        }
        for label, record in cases.items():
            with self.subTest(label):
                with self.assertRaises(RecordRejected):
                    self.store.append(record, None)
        with self.assertRaises(RecordRejected):
            self.store.append(ENTRY, "")
        with self.assertRaises(RecordRejected):
            self.store.append(ENTRY, "x" * 201)
        self.assertEqual(0, self.store.count()["entered"])

    def test_missing_values_are_accepted(self) -> None:
        entry = self.store.append({"age": 80, "face_weakness": None}, None)
        self.assertNotIn("face_weakness", entry)
        self.assertEqual("80-89", entry["age_band"])

    # -- outcomes --------------------------------------------------------------

    def test_set_outcome_labels_an_entry(self) -> None:
        entry = self.store.append(ENTRY, None)
        updated = self.store.set_outcome(entry["encounter_id"], False)
        self.assertIs(False, updated["poor_outcome_6m"])
        self.assertIsInstance(datetime.fromisoformat(updated["outcome_recorded_at"]), datetime)
        self.assertEqual({"total": 4, "entered": 1, "labelled": 3}, self.store.count())
        self.assertIs(False, self.store.records()[-1]["poor_outcome_6m"])

    def test_an_outcome_is_recorded_once(self) -> None:
        entry = self.store.append(ENTRY, None)
        first = self.store.set_outcome(entry["encounter_id"], True)
        # The same value again is a retry: the record comes back unchanged.
        self.assertEqual(first, self.store.set_outcome(entry["encounter_id"], True))
        with self.assertRaises(OutcomeAlreadyRecorded) as caught:
            self.store.set_outcome(entry["encounter_id"], False)
        self.assertEqual(entry["encounter_id"], caught.exception.args[0])
        self.assertIs(True, self.store.records()[-1]["poor_outcome_6m"])
        self.assertEqual(first, self.store.list_entered(1)[0])

    def test_set_outcome_refuses_unknown_ids_and_dataset_rows(self) -> None:
        with self.assertRaises(UnknownEncounterError):
            self.store.set_outcome("uk-134-e404", True)
        with self.assertRaises(RecordRejected):
            self.store.set_outcome("uk-134-00003", True)
        entry = self.store.append(ENTRY, None)
        with self.assertRaises(RecordRejected):
            self.store.set_outcome(entry["encounter_id"], "yes")  # type: ignore[arg-type]
        self.assertEqual(2, self.store.count()["labelled"])

    # -- listing ---------------------------------------------------------------

    def test_list_entered_is_newest_first_and_limited(self) -> None:
        for age in (40, 50, 60):
            self.store.append({"age": age}, None)
        listed = self.store.list_entered(2)
        self.assertEqual(["uk-134-e3", "uk-134-e2"], [row["encounter_id"] for row in listed])
        self.assertEqual(3, len(self.store.list_entered(50)))
        self.assertTrue(all(row["origin"] == "entered" for row in self.store.list_entered(50)))
        with self.assertRaises(ValueError):
            self.store.list_entered(0)

    # -- audit -----------------------------------------------------------------

    def test_the_audit_sink_is_the_sites_own_chain(self) -> None:
        sink = self.store.audit_sink()
        first = sink.append(audit_record("r1"))
        second = sink.append(audit_record("r2"))
        self.assertEqual("", first.previous_digest)
        self.assertEqual(first.digest, second.previous_digest)
        self.assertEqual(["r2", "r1"], [record.request_id for record in sink.list_recent(10)])
        self.assertEqual(["r2"], [record.request_id for record in sink.list_recent(1)])
        self.assertTrue(sink.verify_chain())
        self.assertTrue(sink.verify_chain(full=True))
        self.assertEqual(2, sink.count())
        self.assertEqual(second, sink.list_recent(1)[0])
        other = self.make_store(ITALY, [])
        self.assertEqual([], other.audit_sink().list_recent(10))
        self.assertEqual(0, other.audit_sink().count())


class MemorySiteStoreTests(SiteStoreContract, unittest.TestCase):
    expected_kind = "in-memory"

    def make_store(self, site: Any, records: list[dict[str, Any]]) -> MemorySiteStore:
        return MemorySiteStore(site, records)

    def test_duplicate_rows_are_refused(self) -> None:
        with self.assertRaises(RecordRejected):
            MemorySiteStore(UK, [FIXTURE[0], dict(FIXTURE[0])])


class PostgresSiteStoreTests(SiteStoreContract, unittest.TestCase):
    expected_kind = "postgres"
    db: SupabaseLikeDatabase

    @classmethod
    def setUpClass(cls) -> None:
        cls.db = SupabaseLikeDatabase(admin_dsn_or_skip())
        try:
            cls.db.start()
        except BaseException:
            cls.db.stop()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.stop()

    def make_store(self, site: Any, records: list[dict[str, Any]]) -> PostgresSiteStore:
        with self.db.connect() as conn, acting_as(conn, site.role):
            conn.execute(
                f"TRUNCATE {site.schema}.encounters, {site.schema}.guard_audit, {site.schema}.guard_audit_anchor "
                "RESTART IDENTITY"
            )
        store = PostgresSiteStore(site, self.db.dsn)
        store.seed_dataset(records)
        return store

    def test_seeding_replaces_rows_or_keeps_entries(self) -> None:
        self.store.append(ENTRY, "kept")
        self.assertEqual({"total": 4, "entered": 1, "labelled": 2}, self.store.seed_dataset(FIXTURE, keep_entries=True))
        self.assertEqual({"total": 2, "entered": 0, "labelled": 1}, self.store.seed_dataset(FIXTURE[:2]))
        with self.assertRaises(RecordRejected):
            self.store.seed_dataset([{**FIXTURE[0], "origin": "entered"}])

    def test_the_committed_extract_round_trips_for_every_site(self) -> None:
        """What the database returns is what the memory store holds, row for row."""
        for site in SITES:
            with self.subTest(site=site.site_id):
                dataset = load_site_records(site.site_id)
                self.assertEqual(site.expected_rows, len(dataset))
                postgres = self.make_store(site, dataset)
                memory = MemorySiteStore(site, dataset)
                self.assertEqual(memory.records(), postgres.records())
                self.assertEqual(memory.count(), postgres.count())
                self.assertEqual(sorted(dataset, key=lambda r: r["source_row"]), postgres.records())

    def test_the_seed_script_loads_every_site_as_that_site(self) -> None:
        sys.path.insert(0, str(APP_ROOT / "scripts"))
        import seed_supabase

        self.store.append(ENTRY, None)
        counts = seed_supabase.seed(self.db.dsn, keep_entries=True)
        expected = {site.site_id: site.expected_rows for site in SITES}
        expected["uk-134"] += 1
        self.assertEqual(expected, {site_id: count["total"] for site_id, count in counts.items()})
        self.assertEqual(1, counts["uk-134"]["entered"])
        counts = seed_supabase.seed(self.db.dsn)
        self.assertEqual(0, counts["uk-134"]["entered"])
        self.assertEqual(SITES[0].expected_rows, counts["uk-134"]["total"])

    def test_a_tampered_audit_record_breaks_the_chain(self) -> None:
        sink = self.store.audit_sink()
        sink.append(audit_record("r1"))
        sink.append(audit_record("r2"))
        with self.db.connect() as conn, acting_as(conn, UK.role):
            conn.execute(
                "UPDATE site_uk_134.guard_audit SET record = jsonb_set(record, '{requester_id}', '\"someone\"') "
                "WHERE seq = 1"
            )
        self.assertFalse(sink.verify_chain())

    def test_removing_an_old_audit_record_breaks_the_chain(self) -> None:
        sink = self.store.audit_sink()
        for number in range(4):
            sink.append(audit_record(f"r{number}"))
        self.assertTrue(sink.verify_chain())
        with self.db.connect() as conn, acting_as(conn, UK.role):
            conn.execute("DELETE FROM site_uk_134.guard_audit WHERE seq = 2")
        self.assertFalse(sink.verify_chain())

    def test_a_new_store_continues_the_audit_check_from_the_stored_anchor(self) -> None:
        """The hosted API restarts after every idle period; it does not hash the whole chain again."""
        sink = self.store.audit_sink()
        written = [sink.append(audit_record(f"r{number}")) for number in range(6)]
        self.assertTrue(sink.verify_chain())
        self.assertEqual(6, sink.rows_read)
        with self.db.connect() as conn, acting_as(conn, UK.role):
            stored = conn.execute("SELECT seq, digest, record_count FROM site_uk_134.guard_audit_anchor").fetchone()
        stored = tuple(stored.values()) if isinstance(stored, dict) else tuple(stored)
        self.assertEqual((6, written[-1].digest, 6), stored)
        self.store.close()
        restarted = PostgresSiteStore(UK, self.db.dsn)
        self.addCleanup(restarted.close)
        restarted.audit_sink().append(audit_record("r6"))
        self.assertTrue(restarted.audit_sink().verify_chain())
        self.assertEqual(2, restarted.audit_sink().rows_read, "the anchor and the new record")
        self.assertTrue(restarted.audit_sink().verify_chain(full=True))
        self.assertEqual(7, restarted.audit_sink().rows_read)

    def test_every_row_column_exists_in_the_table(self) -> None:
        with self.db.connect() as conn, acting_as(conn, UK.role):
            rows = conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'site_uk_134' AND table_name = 'encounters'"
            ).fetchall()
        columns = {row["column_name"] if isinstance(row, dict) else row[0] for row in rows}
        self.assertLessEqual(set(ROW_COLUMNS), columns)


class SiteDatabaseUrlTests(unittest.TestCase):
    def test_a_site_url_overrides_the_shared_one(self) -> None:
        environ = {"HEALTHCARE_DEMO_SITE_UK_134_DATABASE_URL": " postgresql://uk "}
        self.assertEqual("postgresql://uk", site_database_url(UK, "postgresql://shared", environ))
        self.assertEqual("postgresql://shared", site_database_url(ITALY, "postgresql://shared", environ))
        self.assertEqual("postgresql://shared", site_database_url(UK, "postgresql://shared", {}))

    def test_from_environment_uses_the_site_url(self) -> None:
        environ = {"HEALTHCARE_DEMO_SITE_IT_002_DATABASE_URL": "postgresql://it"}
        store = PostgresSiteStore.from_environment(ITALY, "postgresql://shared", environ)
        self.assertEqual("postgresql://it", store._dsn)

    def test_a_postgres_store_needs_a_connection_string(self) -> None:
        with self.assertRaises(ValueError):
            PostgresSiteStore(UK, " ")


class CoordinatorStoreContract:
    """The behaviour both coordinator stores must have."""

    def make_coordinator(self) -> Any:
        raise NotImplementedError

    def stored_pilot_reports(self) -> int:
        """How many pilot reports the store holds, read without the store's interface."""
        raise NotImplementedError

    def setUp(self) -> None:
        self.coordinator = self.make_coordinator()

    def test_models_are_stored_and_listed_oldest_first(self) -> None:
        seed = self.coordinator.save_model("rule-v1", "points_rule", {"weights": [1.5, 2]}, "d1", None, "released")
        candidate = self.coordinator.save_model("lr-r1", "logistic_regression", {"w": [0.25]}, "d2", "rule-v1")
        self.assertEqual("released", seed["status"])
        self.assertEqual("candidate", candidate["status"])
        self.assertEqual("rule-v1", candidate["parent_version"])
        self.assertIsNone(seed["parent_version"])
        self.assertEqual({"weights": [1.5, 2]}, self.coordinator.get_model("rule-v1")["params"])
        self.assertEqual(["rule-v1", "lr-r1"], [m["version"] for m in self.coordinator.list_models()])
        self.assertIsNone(self.coordinator.get_model("missing"))
        self.assertIsInstance(datetime.fromisoformat(seed["created_at"]), datetime)

    def test_a_limit_lists_only_the_newest_models_oldest_first(self) -> None:
        self.coordinator.save_model("v00", "k", {}, "d00", None, "released")
        for number in range(1, 8):
            self.coordinator.save_model(f"v{number:02d}", "k", {"n": number}, f"d{number:02d}", "v00")
        self.assertEqual(["v05", "v06", "v07"], [m["version"] for m in self.coordinator.list_models(limit=3)])
        self.assertEqual(8, len(self.coordinator.list_models()))
        self.assertEqual(8, len(self.coordinator.list_models(limit=100)))
        with self.assertRaises(ValueError):
            self.coordinator.list_models(limit=0)

    def test_saving_a_version_again(self) -> None:
        first = self.coordinator.save_model("v1", "k", {"a": 1}, "d1")
        self.assertEqual(first, self.coordinator.save_model("v1", "k", {"a": 1}, "d1"))
        with self.assertRaises(ModelVersionConflict):
            self.coordinator.save_model("v1", "k", {"a": 2}, "d2")
        with self.assertRaises(UnknownModelError):
            self.coordinator.save_model("v2", "k", {}, "d", "no-such-parent")
        with self.assertRaises(RecordRejected):
            self.coordinator.save_model("v3", "k", {}, "d", None, "deployed")

    def test_releasing_a_model_supersedes_the_previous_release(self) -> None:
        self.coordinator.save_model("v1", "k", {}, "d1", None, "released")
        self.coordinator.save_model("v2", "k", {}, "d2", "v1")
        self.assertEqual("v1", self.coordinator.released_model()["version"])
        released = self.coordinator.set_model_status("v2", "released")
        self.assertEqual("released", released["status"])
        self.assertEqual("superseded", self.coordinator.get_model("v1")["status"])
        self.assertEqual("v2", self.coordinator.released_model()["version"])
        self.assertEqual("rejected", self.coordinator.set_model_status("v1", "rejected")["status"])
        with self.assertRaises(UnknownModelError):
            self.coordinator.set_model_status("v9", "released")
        with self.assertRaises(RecordRejected):
            self.coordinator.set_model_status("v2", "deployed")

    def test_a_model_has_no_release_decision_until_one_is_stored(self) -> None:
        saved = self.coordinator.save_model("v1", "k", {}, "d1")
        self.assertIsNone(saved["release_decision"])
        self.assertIsNone(self.coordinator.get_model("v1")["release_decision"])

    def test_the_latest_release_decision_is_stored_with_the_model(self) -> None:
        self.coordinator.save_model("v1", "k", {"a": 1}, "d1")
        stored = self.coordinator.set_release_decision("v1", RELEASE_DECISION)
        decision = stored["release_decision"]
        self.assertEqual(set(RELEASE_DECISION) | {"decided_at"}, set(decision))
        self.assertEqual(RELEASE_DECISION, {key: decision[key] for key in RELEASE_DECISION})
        self.assertIsInstance(datetime.fromisoformat(decision["decided_at"]), datetime)
        self.assertEqual(stored, self.coordinator.get_model("v1"))
        self.assertEqual([stored], self.coordinator.list_models())
        # A later decision replaces it, and saving the version again keeps it.
        later = self.coordinator.set_release_decision("v1", {**RELEASE_DECISION, "action": "reject"})
        self.assertEqual("reject", later["release_decision"]["action"])
        self.assertEqual(later, self.coordinator.save_model("v1", "k", {"a": 1}, "d1"))
        self.assertEqual("candidate", later["status"], "storing a decision changes no status")

    def test_a_release_decision_needs_a_known_model_and_a_valid_shape(self) -> None:
        self.coordinator.save_model("v1", "k", {}, "d1")
        with self.assertRaises(UnknownModelError):
            self.coordinator.set_release_decision("v9", RELEASE_DECISION)
        bad = [
            "release",
            {**RELEASE_DECISION, "action": ""},
            {**RELEASE_DECISION, "guard_permitted": "yes"},
            {**RELEASE_DECISION, "gate_results": "pass"},
            {**RELEASE_DECISION, "gate_results": ["pass"]},
            {**RELEASE_DECISION, "round_id": ""},
            {**RELEASE_DECISION, "approved_by": "someone"},
            {key: value for key, value in RELEASE_DECISION.items() if key != "guard_permitted"},
        ]
        for decision in bad:
            with self.subTest(decision=decision):
                with self.assertRaises(RecordRejected):
                    self.coordinator.set_release_decision("v1", decision)  # type: ignore[arg-type]
        self.assertIsNone(self.coordinator.get_model("v1")["release_decision"])

    def test_a_release_decision_may_name_the_model_it_was_checked_against(self) -> None:
        self.coordinator.save_model("v1", "k", {}, "d1")
        stored = self.coordinator.set_release_decision("v1", {**RELEASE_DECISION, "baseline_version": "rule-v1"})
        self.assertEqual("rule-v1", stored["release_decision"]["baseline_version"])
        self.assertEqual(stored, self.coordinator.get_model("v1"))
        for baseline in ("", 5, "x" * 201, None):
            with self.subTest(baseline=baseline):
                with self.assertRaises(RecordRejected):
                    self.coordinator.set_release_decision("v1", {**RELEASE_DECISION, "baseline_version": baseline})
        self.assertEqual("rule-v1", self.coordinator.get_model("v1")["release_decision"]["baseline_version"])

    def test_there_is_no_pilot_report_until_one_is_saved(self) -> None:
        self.assertIsNone(self.coordinator.latest_pilot_report())

    def test_the_latest_pilot_report_is_kept(self) -> None:
        first = {"pilot_id": "ui-1", "rounds": {"training": [{"auc": 0.71}]}, "candidate": None}
        second = {"pilot_id": "ui-2", "rounds": {"training": [{"auc": 0.74}]}, "candidate": {"status": "candidate"}}
        self.coordinator.save_pilot_report("ui-1", first)
        self.assertEqual(first, self.coordinator.latest_pilot_report())
        self.coordinator.save_pilot_report("ui-2", second)
        self.assertEqual(second, self.coordinator.latest_pilot_report())
        # Saving a pilot again replaces its report and does not make it the latest.
        self.coordinator.save_pilot_report("ui-1", {**first, "candidate": {"status": "rejected"}})
        self.assertEqual(second, self.coordinator.latest_pilot_report())
        # The report is a copy: changing what was saved changes nothing stored.
        second["candidate"]["status"] = "changed"
        self.assertEqual("candidate", self.coordinator.latest_pilot_report()["candidate"]["status"])

    def test_only_the_newest_pilot_reports_are_kept(self) -> None:
        for number in range(site_store.PILOT_REPORTS_KEPT + 3):
            self.coordinator.save_pilot_report(f"ui-{number}", {"n": number})
        self.assertEqual({"n": site_store.PILOT_REPORTS_KEPT + 2}, self.coordinator.latest_pilot_report())
        self.assertEqual(site_store.PILOT_REPORTS_KEPT, self.stored_pilot_reports())

    def test_a_pilot_report_must_be_an_object_with_an_id(self) -> None:
        for pilot_id, report in (("", {}), ("x" * 201, {}), (5, {}), ("ui-1", ["not", "an", "object"]),
                                 ("ui-1", {"auc": float("nan")})):
            with self.subTest(pilot_id=pilot_id, report=report):
                with self.assertRaises(RecordRejected):
                    self.coordinator.save_pilot_report(pilot_id, report)  # type: ignore[arg-type]
        self.assertIsNone(self.coordinator.latest_pilot_report())

    def test_the_coordinator_has_its_own_audit_chain(self) -> None:
        sink = self.coordinator.audit_sink()
        self.assertIs(sink, self.coordinator.audit_sink(), "one sink per store")
        first = sink.append(audit_record("release-1"))
        second = sink.append(audit_record("release-2"))
        self.assertEqual(first.digest, second.previous_digest)
        self.assertEqual(["release-2", "release-1"], [record.request_id for record in sink.list_recent(10)])
        self.assertEqual(2, sink.count())
        self.assertTrue(sink.verify_chain(full=True))

    def test_contributions_are_shown_to_the_coordinator_and_to_their_own_site(self) -> None:
        a = self.coordinator.add_contribution(UK, "round-1", "analytics", {"n": 524, "norm": 0.5})
        self.coordinator.add_contribution(ITALY, "round-1", "analytics", {"n": 292})
        self.coordinator.add_contribution(UK, "round-2", "training", {"n": 500})
        self.assertEqual("uk-134", a["site_id"])
        self.assertEqual({"n": 524, "norm": 0.5}, a["payload"])
        self.assertIsInstance(datetime.fromisoformat(a["submitted_at"]), datetime)
        everyone = self.coordinator.contributions()
        self.assertEqual(["uk-134", "it-002", "uk-134"], [c["site_id"] for c in everyone])
        self.assertEqual(2, len(self.coordinator.contributions("round-1")))
        own = self.coordinator.site_contributions(UK)
        self.assertEqual(["round-1", "round-2"], [c["round_id"] for c in own])
        self.assertEqual(["it-002"], [c["site_id"] for c in self.coordinator.site_contributions(ITALY)])
        with self.assertRaises(RecordRejected):
            self.coordinator.add_contribution(UK, "", "analytics", {})


class MemoryCoordinatorStoreTests(CoordinatorStoreContract, unittest.TestCase):
    def make_coordinator(self) -> MemoryCoordinatorStore:
        return MemoryCoordinatorStore()

    def stored_pilot_reports(self) -> int:
        return len(self.coordinator._pilot_reports)  # noqa: SLF001 - counting what is kept

    def test_location(self) -> None:
        self.assertEqual(("in-memory", {"memory": "process"}), (self.coordinator.kind, self.coordinator.location))


class PostgresCoordinatorStoreTests(CoordinatorStoreContract, unittest.TestCase):
    db: SupabaseLikeDatabase

    @classmethod
    def setUpClass(cls) -> None:
        cls.db = SupabaseLikeDatabase(admin_dsn_or_skip())
        try:
            cls.db.start()
        except BaseException:
            cls.db.stop()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.stop()

    def make_coordinator(self) -> PostgresCoordinatorStore:
        with self.db.connect() as conn, acting_as(conn, "dagents_coordinator"):
            conn.execute(
                "TRUNCATE coordinator.site_contributions, coordinator.models, coordinator.pilot_reports, "
                "coordinator.guard_audit, coordinator.guard_audit_anchor RESTART IDENTITY"
            )
        store = PostgresCoordinatorStore(self.db.dsn, SITES)
        self.addCleanup(store.close)
        return store

    def stored_pilot_reports(self) -> int:
        with self.db.admin() as conn:
            return conn.execute("SELECT count(*) FROM coordinator.pilot_reports").fetchone()[0]

    def test_a_pilot_report_survives_a_new_store(self) -> None:
        """A restarted API builds a new store; the last pilot is still there."""
        self.coordinator.save_pilot_report("ui-1", {"pilot_id": "ui-1", "training_curve": [0.7, 0.72]})
        self.coordinator.close()
        restarted = PostgresCoordinatorStore(self.db.dsn, SITES)
        self.addCleanup(restarted.close)
        self.assertEqual({"pilot_id": "ui-1", "training_curve": [0.7, 0.72]}, restarted.latest_pilot_report())

    def test_the_coordinator_audit_chain_is_stored_in_its_schema(self) -> None:
        written = self.coordinator.audit_sink().append(audit_record("release-1"))
        with self.db.admin() as conn:
            rows = conn.execute("SELECT audit_id, digest FROM coordinator.guard_audit").fetchall()
        self.assertEqual([(written.audit_id, written.digest)], rows)
        restarted = PostgresCoordinatorStore(self.db.dsn, SITES)
        self.addCleanup(restarted.close)
        self.assertEqual([written], restarted.audit_sink().list_recent(10))
        self.assertTrue(restarted.audit_sink().verify_chain())

    def test_a_new_coordinator_store_continues_the_audit_check_from_the_stored_anchor(self) -> None:
        sink = self.coordinator.audit_sink()
        for number in range(5):
            sink.append(audit_record(f"release-{number}"))
        self.assertTrue(sink.verify_chain())
        self.coordinator.close()
        restarted = PostgresCoordinatorStore(self.db.dsn, SITES)
        self.addCleanup(restarted.close)
        self.assertTrue(restarted.audit_sink().verify_chain())
        self.assertEqual(1, restarted.audit_sink().rows_read)
        with self.db.connect() as conn, acting_as(conn, "dagents_coordinator"):
            conn.execute("DELETE FROM coordinator.guard_audit WHERE seq = 1")
        again = PostgresCoordinatorStore(self.db.dsn, SITES)
        self.addCleanup(again.close)
        self.assertFalse(again.audit_sink().verify_chain())

    def test_the_audit_sink_opens_no_connection_until_it_is_used(self) -> None:
        store = PostgresCoordinatorStore(self.db.dsn, SITES)
        self.addCleanup(store.close)
        store.audit_sink()
        self.assertEqual(0, store.connections_opened)
        store.audit_sink().count()
        self.assertEqual(1, store.connections_opened)

    def test_location(self) -> None:
        self.assertEqual(
            ("postgres", {"schema": "coordinator", "role": "dagents_coordinator"}),
            (self.coordinator.kind, self.coordinator.location),
        )

    def test_a_release_decision_survives_a_new_store(self) -> None:
        """A restarted API builds a new store; the decision is still there."""
        self.coordinator.save_model("v1", "k", {}, "d1")
        stored = self.coordinator.set_release_decision("v1", RELEASE_DECISION)
        self.coordinator.close()
        restarted = PostgresCoordinatorStore(self.db.dsn, SITES)
        self.addCleanup(restarted.close)
        self.assertEqual(stored, restarted.get_model("v1"))

    def test_the_table_refuses_a_malformed_decision(self) -> None:
        import psycopg

        self.coordinator.save_model("v1", "k", {}, "d1")
        for decision, decided_at in (
            ('{"action": 1, "guard_permitted": true, "gate_results": []}', "now()"),
            ('{"action": "release", "guard_permitted": "yes", "gate_results": []}', "now()"),
            ('{"action": "release", "guard_permitted": true, "gate_results": {}}', "now()"),
            ('{"action": "release", "guard_permitted": true, "gate_results": []}', "NULL"),
        ):
            with self.subTest(decision=decision, decided_at=decided_at):
                with self.assertRaises(psycopg.errors.CheckViolation):
                    with self.db.connect() as conn, acting_as(conn, "dagents_coordinator"):
                        conn.execute(
                            f"UPDATE coordinator.models SET release_decision = %s::jsonb, decided_at = {decided_at} "
                            "WHERE version = 'v1'",
                            (decision,),
                        )


if __name__ == "__main__":
    unittest.main()
