"""The in-memory and Postgres stores behave the same way.

Every assertion in the contract classes runs against both implementations:
``MemorySiteStore``, which a local run uses, and ``PostgresSiteStore``, which
the deployed API uses. The same holds for the source each hospital's LMA reads
the store through (``StoreSiteSource``). The Postgres half needs
``HEALTHCARE_DEMO_TEST_DATABASE_URL`` (see ``test_site_isolation.py``) and
skips without it.

The Postgres stores run on the framework's kept connection. The coordinator's
model versions and each hospital's job log are kept by the framework's
``PostgresModelStore`` and ``PostgresJobLog``, on the tables of migration
``0002``; the Postgres coordinator class checks that those tables take them
unchanged.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sys
import threading
import time
from typing import Any
import unittest
from unittest import mock
import uuid

APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT / "backend"))
sys.path.insert(0, str(APP_ROOT.parents[1]))

from agents.common.domain import errors as framework_errors  # noqa: E402
from agents.common.domain.federation import (  # noqa: E402
    GateResult,
    ModelReleaseDecision,
    ModelStatusChange,
    ModelVersion,
)
from agents.common.domain.governance import AuditRecord  # noqa: E402
from agents.common.domain.site import JobLogEntry  # noqa: E402
from agents.common.infrastructure import postgres_lease  # noqa: E402
from agents.common.infrastructure.postgres_lease import KeptConnection  # noqa: E402
from agents.common.infrastructure.postgres_roles import acting_as  # noqa: E402
from agents.gma.infrastructure.postgres_models import PostgresModelStore  # noqa: E402
from agents.lma.infrastructure.job_log import PostgresJobLog  # noqa: E402
from app.domain.dataset import JOINING_SITES, SITES, load_site_records, site_for  # noqa: E402
from app.services import site_store  # noqa: E402
from app.services.db_hints import connection_hint  # noqa: E402
from app.services.site_source import SITE_SOURCE_NAME, StoreSiteSource, site_store_source  # noqa: E402
from app.services.site_store import (  # noqa: E402
    COORDINATOR_ROLE,
    EntryLimitReached,
    IdempotencyKeyReused,
    MemoryCoordinatorStore,
    MemorySiteStore,
    OutcomeAlreadyRecorded,
    PostgresCoordinatorStore,
    PostgresSiteStore,
    RecordRejected,
    ROW_COLUMNS,
    StoreUnavailableError,
    UnknownEncounterError,
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

#: Nothing listens on port 1. The password must never appear in an error.
UNREACHABLE = "postgresql://demo:pw-never-shown@127.0.0.1:1/healthcare"

#: The members of the framework's ``SiteRecordSource`` port.
SITE_SOURCE_MEMBERS = ("records", "count", "version", "location", "close")


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


class FakeClock:
    """Monotonic seconds that move only when a test moves them."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class SiteStoreContract:
    """The behaviour both stores must have. Subclasses supply ``make_store``."""

    expected_kind = ""

    def make_store(self, site: Any, records: list[dict[str, Any]], **kwargs: Any) -> Any:
        raise NotImplementedError

    def setUp(self) -> None:
        self.store = self.make_store(UK, FIXTURE)

    # -- reading -------------------------------------------------------------

    def test_kind_and_location(self) -> None:
        self.assertEqual(self.expected_kind, self.store.kind)
        self.assertEqual("uk-134", self.store.site_id)
        if self.expected_kind == "postgres":
            self.assertEqual({"schema": "site_uk_134", "role": "site_uk_134"}, self.store.location)
            self.assertEqual("site_uk_134", self.store.role)
        else:
            self.assertEqual({"memory": "process"}, self.store.location)
            self.assertIsNone(self.store.role)
            self.assertIsNone(self.store.lease)

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
        self.assertIs(sink, self.store.audit_sink(), "one sink per store")
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

    # -- the write count ---------------------------------------------------------

    def test_every_change_through_the_store_is_counted(self) -> None:
        start = self.store.write_count
        entry = self.store.append(ENTRY, "count-1")
        self.assertEqual(start + 1, self.store.write_count)
        self.store.set_outcome(entry["encounter_id"], True)
        self.assertEqual(start + 2, self.store.write_count)
        self.store.append({"age": 70, "poor_outcome_6m": False}, None)
        self.assertEqual(start + 3, self.store.write_count)

    def test_retries_and_refused_writes_change_nothing_and_are_not_counted(self) -> None:
        entry = self.store.append(ENTRY, "count-2")
        self.store.set_outcome(entry["encounter_id"], True)
        counted = self.store.write_count
        self.store.append(ENTRY, "count-2")
        self.store.set_outcome(entry["encounter_id"], True)
        refused = (
            lambda: self.store.append({"systolic_bp": 999}, None),
            lambda: self.store.append({**ENTRY, "age": 30}, "count-2"),
            lambda: self.store.set_outcome(entry["encounter_id"], False),
            lambda: self.store.set_outcome("uk-134-00003", True),
            lambda: self.store.set_outcome("uk-134-e404", True),
        )
        for write in refused:
            with self.assertRaises((RecordRejected, IdempotencyKeyReused, OutcomeAlreadyRecorded, UnknownEncounterError)):
                write()
        self.assertEqual(counted, self.store.write_count)

    # -- the entry cap -------------------------------------------------------------

    def test_the_default_cap_is_the_demos(self) -> None:
        self.assertEqual(site_store.DEFAULT_MAX_ENTERED, self.store.max_entered)

    def test_the_entry_cap_refuses_more_entries_and_names_the_hospital(self) -> None:
        store = self.make_store(UK, FIXTURE, max_entered=2)
        store.append(ENTRY, "cap-1")
        store.append({"age": 50}, "cap-2")
        writes = store.write_count
        with self.assertRaises(EntryLimitReached) as raised:
            store.append({"age": 60}, "cap-3")
        self.assertEqual(
            "Hospital 134 already holds 2 entered records, the most this demo accepts", str(raised.exception)
        )
        # A retry of an earlier entry is refused too once the cap is reached.
        with self.assertRaises(EntryLimitReached):
            store.append(ENTRY, "cap-1")
        self.assertEqual({"total": 5, "entered": 2, "labelled": 2}, store.count())
        self.assertEqual(writes, store.write_count, "a refused entry is not a write")
        # Outcomes still record at the cap: they change no row count.
        store.set_outcome("uk-134-e1", True)
        self.assertEqual(3, store.count()["labelled"])

    def test_the_cap_counts_entries_only(self) -> None:
        store = self.make_store(UK, FIXTURE, max_entered=1)
        self.assertEqual(3, store.count()["total"])
        store.append(ENTRY, None)
        with self.assertRaises(EntryLimitReached):
            store.append(ENTRY, None)

    def second_writer(self, store: Any) -> Any:
        """Another writer to the same hospital's rows: the same object in memory, another connection in Postgres."""
        return store

    def test_entries_at_the_same_moment_cannot_pass_the_cap_together(self) -> None:
        store = self.make_store(ITALY, [], max_entered=3)
        writers = [store, self.second_writer(store)]
        outcomes: list[str] = []
        failures: list[BaseException] = []

        def enter(number: int) -> None:
            try:
                writers[number % 2].append({"age": 40 + number}, f"race-{number}")
                outcomes.append("stored")
            except EntryLimitReached:
                outcomes.append("refused")
            except BaseException as error:  # noqa: BLE001 - reported below
                failures.append(error)

        threads = [threading.Thread(target=enter, args=(number,)) for number in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertEqual([], failures)
        self.assertEqual(3, outcomes.count("stored"))
        self.assertEqual(5, outcomes.count("refused"))
        self.assertEqual(3, store.count()["entered"])

    def test_a_cap_below_one_is_refused(self) -> None:
        for cap in (0, -1, 1.5, True, "300"):
            with self.subTest(cap=cap):
                with self.assertRaises(ValueError):
                    self.make_store(ITALY, [], max_entered=cap)

    # -- the site source: how the hospital's LMA reads this store ------------------

    def source(self, **kwargs: Any) -> StoreSiteSource:
        source = StoreSiteSource(self.store, **kwargs)
        self.addCleanup(source.close)
        return source

    def test_the_site_source_has_every_member_of_the_framework_port(self) -> None:
        source = self.source()
        for member in SITE_SOURCE_MEMBERS:
            with self.subTest(member=member):
                self.assertTrue(callable(getattr(source, member, None)))

    def test_the_site_source_reads_the_store(self) -> None:
        source = self.source()
        self.assertEqual(self.store.records(), source.records())
        self.assertEqual(3, source.count())
        self.assertEqual(self.expected_kind, source.kind)
        self.assertEqual(self.store.role, source.role)
        self.assertEqual(self.store.location, source.location())
        source.location()["schema"] = "changed"
        self.assertEqual(self.store.location, source.location(), "location() returns a copy")

    def test_the_site_source_version_changes_on_every_write(self) -> None:
        source = self.source()
        seen = [source.version()]
        self.assertEqual(seen[0], source.version(), "no write, no change")
        entry = self.store.append(ENTRY, None)
        seen.append(source.version())
        self.store.set_outcome(entry["encounter_id"], False)
        seen.append(source.version())
        self.store.append({"age": 81, "poor_outcome_6m": True}, None)
        seen.append(source.version())
        self.assertEqual(len(seen), len(set(seen)), f"a write left the version unchanged: {seen}")
        self.assertEqual(seen[-1], source.version())

    def test_the_site_source_version_ignores_refused_writes(self) -> None:
        source = self.source()
        before = source.version()
        with self.assertRaises(RecordRejected):
            self.store.append({"sex": "unknown"}, None)
        with self.assertRaises(UnknownEncounterError):
            self.store.set_outcome("uk-134-e404", True)
        self.assertEqual(before, source.version())

    def test_the_site_source_counts_at_most_once_per_interval(self) -> None:
        clock = FakeClock()
        source = self.source(recount_seconds=30, clock=clock)
        with mock.patch.object(self.store, "count", wraps=self.store.count) as counted:
            first = source.version()
            for _ in range(5):
                self.assertEqual(first, source.version())
            self.assertEqual(1, counted.call_count)
            # A write through the store changes the version without a count.
            self.store.append(ENTRY, None)
            self.assertNotEqual(first, source.version())
            self.assertEqual(1, counted.call_count)
            clock.now += 29.9
            source.version()
            self.assertEqual(1, counted.call_count)
            clock.now += 0.1
            source.version()
            self.assertEqual(2, counted.call_count)

    def test_a_negative_count_interval_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            StoreSiteSource(self.store, recount_seconds=-1)

    def test_closing_the_site_source_leaves_the_store_open(self) -> None:
        source = StoreSiteSource(self.store)
        self.store.count()
        opened = getattr(self.store, "connections_opened", 0)
        source.close()
        self.assertEqual(3, self.store.count()["total"])
        self.assertEqual(opened, getattr(self.store, "connections_opened", 0))


class MemorySiteStoreTests(SiteStoreContract, unittest.TestCase):
    expected_kind = "in-memory"

    def make_store(self, site: Any, records: list[dict[str, Any]], **kwargs: Any) -> MemorySiteStore:
        return MemorySiteStore(site, records, **kwargs)

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

    def make_store(self, site: Any, records: list[dict[str, Any]], **kwargs: Any) -> PostgresSiteStore:
        with self.db.connect() as conn, acting_as(conn, site.role):
            conn.execute(
                f"TRUNCATE {site.schema}.encounters, {site.schema}.guard_audit, {site.schema}.guard_audit_anchor "
                "RESTART IDENTITY"
            )
        store = PostgresSiteStore(site, self.db.dsn, **kwargs)
        self.addCleanup(store.close)
        store.seed_dataset(records)
        return store

    def second_writer(self, store: PostgresSiteStore) -> PostgresSiteStore:
        other = PostgresSiteStore(store.site, self.db.dsn, max_entered=store.max_entered)
        self.addCleanup(other.close)
        return other

    def test_seeding_replaces_rows_or_keeps_entries(self) -> None:
        self.store.append(ENTRY, "kept")
        self.assertEqual({"total": 4, "entered": 1, "labelled": 2}, self.store.seed_dataset(FIXTURE, keep_entries=True))
        self.assertEqual({"total": 2, "entered": 0, "labelled": 1}, self.store.seed_dataset(FIXTURE[:2]))
        with self.assertRaises(RecordRejected):
            self.store.seed_dataset([{**FIXTURE[0], "origin": "entered"}])

    def test_seeding_changes_the_site_source_version(self) -> None:
        source = self.source()
        before = source.version()
        self.store.seed_dataset(FIXTURE, keep_entries=True)
        self.assertNotEqual(before, source.version())

    def test_the_committed_extract_round_trips_for_every_site(self) -> None:
        """What the database returns is what the memory store holds, row for row."""
        for site in (*SITES, *JOINING_SITES):
            with self.subTest(site=site.site_id):
                dataset = load_site_records(site.site_id)
                self.assertEqual(site.expected_rows, len(dataset))
                postgres = self.make_store(site, dataset)
                memory = MemorySiteStore(site, dataset)
                self.assertEqual(memory.records(), postgres.records())
                self.assertEqual(memory.count(), postgres.count())
                self.assertEqual(sorted(dataset, key=lambda r: r["source_row"]), postgres.records())
                self.assertEqual(StoreSiteSource(memory).records(), StoreSiteSource(postgres).records())

    def test_the_seed_script_loads_every_site_as_that_site(self) -> None:
        sys.path.insert(0, str(APP_ROOT / "scripts"))
        import seed_supabase

        self.store.append(ENTRY, None)
        counts = seed_supabase.seed(self.db.dsn, keep_entries=True)
        expected = {site.site_id: site.expected_rows for site in (*SITES, *JOINING_SITES)}
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

    # -- the framework lease -------------------------------------------------------

    def test_the_store_runs_on_a_framework_lease_named_for_the_hospital(self) -> None:
        self.assertIsInstance(self.store.lease, KeptConnection)
        self.assertEqual("hospital uk-134", self.store.lease.name)
        self.store.count()
        self.store.audit_sink().count()
        self.assertEqual(1, self.store.lease.connections_opened)
        self.assertEqual(self.store.lease.connections_opened, self.store.connections_opened)

    def test_a_store_given_a_lease_shares_it_and_leaves_it_open(self) -> None:
        lease = KeptConnection(self.db.dsn, name="site uk-134")
        self.addCleanup(lease.close)
        shared = PostgresSiteStore(UK, lease=lease)
        self.assertIs(lease, shared.lease)
        self.assertEqual(self.store.records(), shared.records())
        shared.audit_sink().count()
        with lease.acting_as(UK.role) as conn:
            pid = conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
        shared.close()
        with lease.acting_as(UK.role) as conn:
            self.assertEqual(pid, conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"])
        self.assertEqual(1, lease.connections_opened)

    # -- the site source on Postgres -------------------------------------------------

    def test_writes_by_another_process_reach_the_version_after_the_count_interval(self) -> None:
        """Another process (the seed script, or the API when the LMA runs on its own) writes the same schema."""
        clock = FakeClock()
        source = self.source(recount_seconds=30, clock=clock)
        other_process = PostgresSiteStore(UK, self.db.dsn)
        self.addCleanup(other_process.close)
        before = source.version()
        other_process.append(ENTRY, None)
        self.assertEqual(before, source.version(), "the counts are read at most once per interval")
        clock.now += 30
        after = source.version()
        self.assertNotEqual(before, after)
        other_process.append({"age": 55}, None)
        at_once = self.source(recount_seconds=0)
        first = at_once.version()
        other_process.append({"age": 56}, None)
        self.assertNotEqual(first, at_once.version(), "with no interval the counts are read every time")

    def test_closing_a_source_that_owns_its_store_closes_the_store(self) -> None:
        store = PostgresSiteStore(UK, self.db.dsn)
        self.addCleanup(store.close)
        source = StoreSiteSource(store, close_store=True)
        source.records()
        self.assertEqual(1, store.connections_opened)
        source.close()
        source.records()
        self.assertEqual(2, store.connections_opened, "the store's connection was closed, so a new one opened")

    def test_the_site_source_factory_reads_the_hospital_through_the_given_lease(self) -> None:
        """An LMA started on its own builds its source this way, on its own lease."""
        lease = KeptConnection(self.db.dsn, name="site uk-134")
        self.addCleanup(lease.close)
        source = site_store_source(lease, UK.role, "uk-134")
        self.assertEqual("postgres", source.kind)
        self.assertEqual(UK.role, source.role)
        self.assertEqual({"schema": "site_uk_134", "role": "site_uk_134"}, source.location())
        self.assertEqual(self.store.records(), source.records())
        self.assertEqual(3, source.count())
        before = source.version()
        # The API writes through its own store; the LMA's source sees it at
        # once, because a refresh or a federated job asks for the version.
        self.store.append(ENTRY, None)
        self.assertNotEqual(before, source.version())
        self.assertEqual(4, len(source.records()))
        self.assertEqual(1, lease.connections_opened)
        source.close()
        source.records()
        self.assertEqual(1, lease.connections_opened, "closing the source leaves the LMA's lease open")

    def test_the_store_lease_serves_the_framework_job_log_as_the_hospital(self) -> None:
        with self.db.admin() as admin:
            admin.execute("TRUNCATE coordinator.site_contributions RESTART IDENTITY")
        italy = self.make_store(ITALY, [])
        uk_log = PostgresJobLog(self.store.lease, UK.role, "coordinator.site_contributions")
        italy_log = PostgresJobLog(italy.lease, ITALY.role, "coordinator.site_contributions")
        entry = JobLogEntry(
            round_id="pilot-x-training-1",
            phase="training",
            participation="completed",
            contributed_examples=3,
            update_norm=0.5,
            update_digest="sha256:abc",
            job_digest="sha256:def",
            metrics={"auc": 0.75},
        )
        uk_log.append(entry)
        self.assertEqual(["pilot-x-training-1"], [row.round_id for row in uk_log.recent(5)])
        self.assertEqual([], italy_log.recent(5), "row-level security keeps each hospital to its own rows")
        with self.db.admin() as admin:
            roles = [row[0] for row in admin.execute("SELECT site_role FROM coordinator.site_contributions")]
        self.assertEqual([UK.role], roles, "the row was inserted as the hospital's role")
        self.assertEqual(1, self.store.connections_opened)


class SiteSourceFactoryTests(unittest.TestCase):
    """What the factory refuses before it opens anything."""

    def lease(self) -> KeptConnection:
        lease = KeptConnection(UNREACHABLE, name="site uk-134")
        self.addCleanup(lease.close)
        return lease

    def test_a_source_needs_a_database(self) -> None:
        with self.assertRaises(ValueError) as raised:
            site_store_source(None, UK.role, "uk-134")
        self.assertIn("LMA_DATABASE_URL", str(raised.exception))

    def test_a_source_acts_only_as_the_hospitals_own_role(self) -> None:
        lease = self.lease()
        for role in ("site_it_002", "dagents_coordinator", "postgres", ""):
            with self.subTest(role=role):
                with self.assertRaises(ValueError) as raised:
                    site_store_source(lease, role, "uk-134")
                self.assertIn("site_uk_134", str(raised.exception))
        self.assertEqual(0, lease.connections_opened)

    def test_an_unknown_hospital_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            site_store_source(self.lease(), "site_xx_001", "xx-001")

    def test_a_joining_hospital_has_a_source_read_as_its_own_role(self) -> None:
        """Hospital 290's LMA runs from settings, like the others, once it joins."""
        from agents.common.extensions import ExtensionRegistry
        from app.extension import install

        joining = JOINING_SITES[0]
        lease = self.lease()
        source = site_store_source(lease, joining.role, joining.site_id)
        self.assertEqual(({"schema": joining.schema, "role": joining.role}, joining.role), (source.location(), source.role))
        with self.assertRaises(ValueError):
            site_store_source(lease, UK.role, joining.site_id)
        registry = ExtensionRegistry()
        install(registry)
        self.assertIsInstance(registry.site_source(SITE_SOURCE_NAME)(lease, joining.role, joining.site_id), StoreSiteSource)
        self.assertEqual(0, lease.connections_opened)

    def test_the_factory_opens_no_connection_until_it_is_read(self) -> None:
        lease = self.lease()
        source = site_store_source(lease, UK.role, "uk-134")
        self.assertEqual(("postgres", UK.role), (source.kind, source.role))
        self.assertEqual(0, lease.connections_opened)

    def test_the_apps_extension_contributes_this_source(self) -> None:
        """An LMA started with ``LMA_SITE_SOURCE=healthcare_site_store`` gets this source from the registry."""
        from agents.common.extensions import ExtensionRegistry
        from app.extension import install

        registry = ExtensionRegistry()
        install(registry)
        self.assertIn(SITE_SOURCE_NAME, registry.list_site_sources())
        lease = self.lease()
        source = registry.site_source(SITE_SOURCE_NAME)(lease, UK.role, "uk-134")
        self.assertIsInstance(source, StoreSiteSource)
        self.assertEqual(({"schema": "site_uk_134", "role": "site_uk_134"}, UK.role), (source.location(), source.role))
        self.assertEqual(0, lease.connections_opened)
        with self.assertRaises(ValueError):
            registry.site_source(SITE_SOURCE_NAME)(None, UK.role, "uk-134")


class StoreErrorTests(unittest.TestCase):
    """The stores raise the framework's store errors, with no connection detail."""

    def test_the_store_errors_are_the_framework_errors(self) -> None:
        self.assertIs(framework_errors.StoreUnavailableError, site_store.StoreUnavailableError)
        self.assertIs(framework_errors.StoreNotSetUpError, site_store.StoreNotSetUpError)
        self.assertIs(postgres_lease.not_set_up_errors, site_store.not_set_up_errors)

    def test_an_unreachable_database_names_the_store_and_hides_the_connection_string(self) -> None:
        for store, name in (
            (PostgresSiteStore(UK, UNREACHABLE), "hospital uk-134"),
            (PostgresCoordinatorStore(UNREACHABLE), "coordinator store"),
        ):
            with self.subTest(name=name):
                self.addCleanup(store.close)
                with self.assertRaises(StoreUnavailableError) as raised:
                    store.latest_pilot_report() if name == "coordinator store" else store.count()
                message = str(raised.exception)
                self.assertIn(name, message)
                self.assertIn("could not connect to the database", message)
                for secret in ("pw-never-shown", UNREACHABLE, "127.0.0.1"):
                    self.assertNotIn(secret, message)


class StoreLeaseSettingsTests(unittest.TestCase):
    """How each Postgres store builds its lease. No connection is opened."""

    def built(self, make: Any) -> mock.MagicMock:
        with mock.patch.object(site_store, "KeptConnection") as kept:
            make()
        kept.assert_called_once()
        return kept

    def test_a_site_store_lease_carries_the_name_timeouts_and_hint(self) -> None:
        kept = self.built(
            lambda: PostgresSiteStore(UK, "postgresql://shared", statement_timeout_ms=60_000, liveness_idle_seconds=5)
        )
        self.assertEqual(("postgresql://shared",), kept.call_args.args)
        self.assertEqual(
            {"name": "hospital uk-134", "statement_timeout_ms": 60_000, "liveness_idle_seconds": 5,
             "hint": connection_hint},
            kept.call_args.kwargs,
        )

    def test_the_coordinator_store_lease_is_named_for_the_coordinator(self) -> None:
        kept = self.built(lambda: PostgresCoordinatorStore("postgresql://shared"))
        self.assertEqual("coordinator store", kept.call_args.kwargs["name"])
        self.assertEqual(site_store.DEFAULT_STATEMENT_TIMEOUT_MS, kept.call_args.kwargs["statement_timeout_ms"])

    def test_from_environment_uses_the_site_url(self) -> None:
        environ = {"HEALTHCARE_DEMO_SITE_IT_002_DATABASE_URL": "postgresql://it"}
        kept = self.built(lambda: PostgresSiteStore.from_environment(ITALY, "postgresql://shared", environ))
        self.assertEqual(("postgresql://it",), kept.call_args.args)

    def test_a_store_given_a_lease_builds_none(self) -> None:
        lease = object()
        with mock.patch.object(site_store, "KeptConnection") as kept:
            store = PostgresSiteStore(UK, lease=lease)
            coordinator = PostgresCoordinatorStore(lease=lease)
        kept.assert_not_called()
        self.assertIs(lease, store.lease)
        self.assertIs(lease, coordinator.lease)

    def test_a_store_needs_one_connection_string_or_one_lease(self) -> None:
        for make in (
            lambda: PostgresSiteStore(UK, " "),
            lambda: PostgresSiteStore(UK),
            lambda: PostgresSiteStore(UK, "postgresql://shared", lease=object()),
            lambda: PostgresCoordinatorStore(),
            lambda: PostgresCoordinatorStore("postgresql://shared", lease=object()),
        ):
            with self.assertRaises(ValueError):
                make()


class SiteDatabaseUrlTests(unittest.TestCase):
    def test_a_site_url_overrides_the_shared_one(self) -> None:
        environ = {"HEALTHCARE_DEMO_SITE_UK_134_DATABASE_URL": " postgresql://uk "}
        self.assertEqual("postgresql://uk", site_database_url(UK, "postgresql://shared", environ))
        self.assertEqual("postgresql://shared", site_database_url(ITALY, "postgresql://shared", environ))
        self.assertEqual("postgresql://shared", site_database_url(UK, "postgresql://shared", {}))


class CoordinatorStoreContract:
    """The behaviour both coordinator stores must have.

    The coordinator store keeps the pilot reports and the release Guard's audit
    chain. The model registry is the GMA's (``PostgresModelStore`` on this
    store's lease, tested at the end of this file and in
    ``agents/tests/test_model_registry.py``).
    """

    def make_coordinator(self) -> Any:
        raise NotImplementedError

    def stored_pilot_reports(self) -> int:
        """How many pilot reports the store holds, read without the store's interface."""
        raise NotImplementedError

    def setUp(self) -> None:
        self.coordinator = self.make_coordinator()

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

    def test_the_coordinator_store_keeps_no_model_registry_and_no_site_rows(self) -> None:
        """Models live in the GMA's registry; what sites report lives in each LMA's job log."""
        for name in ("save_model", "get_model", "list_models", "set_model_status", "released_model",
                     "set_release_decision", "add_contribution", "contributions", "site_contributions"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(self.coordinator, name))


class MemoryCoordinatorStoreTests(CoordinatorStoreContract, unittest.TestCase):
    def make_coordinator(self) -> MemoryCoordinatorStore:
        return MemoryCoordinatorStore()

    def stored_pilot_reports(self) -> int:
        return len(self.coordinator._pilot_reports)  # noqa: SLF001 - counting what is kept

    def test_location(self) -> None:
        self.assertEqual(("in-memory", {"memory": "process"}), (self.coordinator.kind, self.coordinator.location))
        self.assertIsNone(self.coordinator.lease)


#: A release decision as the GMA stores it, with every key it writes.
def release_decision() -> ModelReleaseDecision:
    return ModelReleaseDecision(
        action="release",
        guard_permitted=True,
        gate_results=[GateResult(gate_id="discrimination", outcome="passed", observed=0.79, threshold=0.7)],
        round_id="pilot-20261011T000000Z-abc123-training-5",
        baseline_version="stroke-outcome-rule-v1",
        metrics_source="rounds",
    )


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
        with self.db.connect() as conn, acting_as(conn, COORDINATOR_ROLE):
            conn.execute(
                "TRUNCATE coordinator.site_contributions, coordinator.models, coordinator.pilot_reports, "
                "coordinator.guard_audit, coordinator.guard_audit_anchor RESTART IDENTITY"
            )
        store = PostgresCoordinatorStore(self.db.dsn)
        self.addCleanup(store.close)
        return store

    def restarted(self) -> PostgresCoordinatorStore:
        """A new store, as a restarted API builds it."""
        store = PostgresCoordinatorStore(self.db.dsn)
        self.addCleanup(store.close)
        return store

    def stored_pilot_reports(self) -> int:
        with self.db.admin() as conn:
            return conn.execute("SELECT count(*) FROM coordinator.pilot_reports").fetchone()[0]

    def test_location(self) -> None:
        self.assertEqual(
            ("postgres", {"schema": "coordinator", "role": "dagents_coordinator"}),
            (self.coordinator.kind, self.coordinator.location),
        )
        self.assertEqual(COORDINATOR_ROLE, self.coordinator.role)

    def test_a_pilot_report_survives_a_new_store(self) -> None:
        """A restarted API builds a new store; the last pilot is still there."""
        self.coordinator.save_pilot_report("ui-1", {"pilot_id": "ui-1", "training_curve": [0.7, 0.72]})
        self.coordinator.close()
        self.assertEqual({"pilot_id": "ui-1", "training_curve": [0.7, 0.72]}, self.restarted().latest_pilot_report())

    def test_the_coordinator_audit_chain_is_stored_in_its_schema(self) -> None:
        written = self.coordinator.audit_sink().append(audit_record("release-1"))
        with self.db.admin() as conn:
            rows = conn.execute("SELECT audit_id, digest FROM coordinator.guard_audit").fetchall()
        self.assertEqual([(written.audit_id, written.digest)], rows)
        restarted = self.restarted()
        self.assertEqual([written], restarted.audit_sink().list_recent(10))
        self.assertTrue(restarted.audit_sink().verify_chain())

    def test_a_new_coordinator_store_continues_the_audit_check_from_the_stored_anchor(self) -> None:
        sink = self.coordinator.audit_sink()
        for number in range(5):
            sink.append(audit_record(f"release-{number}"))
        self.assertTrue(sink.verify_chain())
        self.coordinator.close()
        restarted = self.restarted()
        self.assertTrue(restarted.audit_sink().verify_chain())
        self.assertEqual(1, restarted.audit_sink().rows_read)
        with self.db.connect() as conn, acting_as(conn, COORDINATOR_ROLE):
            conn.execute("DELETE FROM coordinator.guard_audit WHERE seq = 1")
        self.assertFalse(self.restarted().audit_sink().verify_chain())

    def test_the_audit_sink_opens_no_connection_until_it_is_used(self) -> None:
        store = self.restarted()
        store.audit_sink()
        self.assertEqual(0, store.connections_opened)
        store.audit_sink().count()
        self.assertEqual(1, store.connections_opened)

    def test_the_store_runs_on_a_framework_lease_named_for_the_coordinator(self) -> None:
        self.assertIsInstance(self.coordinator.lease, KeptConnection)
        self.assertEqual("coordinator store", self.coordinator.lease.name)

    # -- the GMA's model registry on this store's lease and table ------------------

    def model_store(self, coordinator: PostgresCoordinatorStore) -> PostgresModelStore:
        return PostgresModelStore(coordinator.lease, COORDINATOR_ROLE, "coordinator.models")

    def test_the_coordinator_lease_serves_the_gmas_model_store(self) -> None:
        models = self.model_store(self.coordinator)
        seed = ModelVersion(
            version="stroke-outcome-rule-v1", kind="points_rule", digest="sha256:seed", status="released",
            params={"points": {"age_80_plus": 2}},
        )
        models.apply([ModelStatusChange(version=seed.version, from_status=None, to_status="released")], new=seed)
        candidate = ModelVersion(
            version="stroke-outcome-logreg+pilot-x-training-5", kind="logistic_regression", digest="sha256:lr",
            status="candidate", parent_version=seed.version, params={"bias": 0.25, "w.age": -0.125},
        )
        models.apply(
            [ModelStatusChange(version=candidate.version, from_status=None, to_status="candidate")], new=candidate
        )
        models.apply([], decision=(candidate.version, release_decision()))
        self.coordinator.save_pilot_report("ui-1", {"pilot_id": "ui-1"})
        self.coordinator.audit_sink().append(audit_record("release-1"))
        self.assertEqual(1, self.coordinator.connections_opened, "one kept connection for every coordinator object")
        # The decision, with the keys the GMA adds, survives a restart.
        self.coordinator.close()
        stored = self.model_store(self.restarted()).get(candidate.version)
        self.assertIsInstance(datetime.fromisoformat(stored.release_decision.decided_at), datetime)
        self.assertEqual(release_decision(), stored.release_decision.model_copy(update={"decided_at": None}))
        self.assertEqual({"bias": 0.25, "w.age": -0.125}, stored.params)
        self.assertEqual(seed.version, self.model_store(self.restarted()).released().version)

    def test_the_table_refuses_a_malformed_decision(self) -> None:
        import psycopg

        with self.db.connect() as conn, acting_as(conn, COORDINATOR_ROLE):
            conn.execute(
                "INSERT INTO coordinator.models (version, kind, params, digest, status) "
                "VALUES ('v1', 'k', '{}'::jsonb, 'd1', 'candidate')"
            )
        for decision, decided_at in (
            ('{"action": 1, "guard_permitted": true, "gate_results": []}', "now()"),
            ('{"action": "release", "guard_permitted": "yes", "gate_results": []}', "now()"),
            ('{"action": "release", "guard_permitted": true, "gate_results": {}}', "now()"),
            ('{"action": "release", "guard_permitted": true, "gate_results": []}', "NULL"),
        ):
            with self.subTest(decision=decision, decided_at=decided_at):
                with self.assertRaises(psycopg.errors.CheckViolation):
                    with self.db.connect() as conn, acting_as(conn, COORDINATOR_ROLE):
                        conn.execute(
                            f"UPDATE coordinator.models SET release_decision = %s::jsonb, decided_at = {decided_at} "
                            "WHERE version = 'v1'",
                            (decision,),
                        )


if __name__ == "__main__":
    unittest.main()
