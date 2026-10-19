"""The in-memory audit log: counting records and checking the chain cheaply.

The audit endpoints of a public demo check the chain on every read. A full
check re-hashes every record, so its cost grows with every decision the Guard
records. By default ``verify_chain`` re-hashes only the records added since the
last successful check; ``verify_chain(full=True)`` re-hashes them all. The
Postgres sink has the same interface and its tests are in
``test_postgres_roles.py``.

The last tests load a chain that the code at commit 905ed28 wrote, before
audit records named their planner, and check that it still verifies, alone
and followed by records that name a planner.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time
import unittest
from unittest import mock
import uuid

from dagents.common.application import ethical_guard
from dagents.common.application.ethical_guard import InMemoryAuditLog, audit_digest
from dagents.common.domain.governance import AuditRecord


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
    )


def filled(count: int) -> InMemoryAuditLog:
    log = InMemoryAuditLog()
    for number in range(count):
        log.append(audit_record(f"r{number}"))
    return log


class InMemoryAuditLogTests(unittest.TestCase):
    def test_count_is_the_number_of_records(self) -> None:
        self.assertEqual(0, InMemoryAuditLog().count())
        self.assertEqual(3, filled(3).count())

    def test_an_empty_log_verifies(self) -> None:
        log = InMemoryAuditLog()
        self.assertTrue(log.verify_chain())
        self.assertTrue(log.verify_chain(full=True))

    def test_a_second_check_hashes_only_the_new_records(self) -> None:
        log = filled(4)
        with mock.patch.object(ethical_guard, "audit_digest", wraps=ethical_guard.audit_digest) as digest:
            self.assertTrue(log.verify_chain())
            self.assertEqual(4, digest.call_count)
            log.append(audit_record("r4"))  # one more hash for the append itself
            digest.reset_mock()
            self.assertTrue(log.verify_chain())
            self.assertEqual(1, digest.call_count)
            digest.reset_mock()
            self.assertTrue(log.verify_chain(full=True))
            self.assertEqual(5, digest.call_count)

    def test_a_record_edited_after_the_last_check_breaks_the_chain(self) -> None:
        log = filled(2)
        self.assertTrue(log.verify_chain())
        log.append(audit_record("r2"))
        stored = log._records  # noqa: SLF001 - reaching in is the point of the test
        stored[2] = stored[2].model_copy(update={"permitted": False})
        self.assertFalse(log.verify_chain())

    def test_a_record_replaced_before_the_last_check_is_still_caught(self) -> None:
        """The checked records must still be the same objects, so a swap is seen without re-hashing."""
        log = filled(3)
        self.assertTrue(log.verify_chain())
        stored = log._records  # noqa: SLF001
        stored[0] = stored[0].model_copy(update={"requester_id": "someone else"})
        self.assertFalse(log.verify_chain())
        self.assertFalse(log.verify_chain(full=True))

    def test_a_record_removed_before_the_last_check_is_caught_by_the_default_check(self) -> None:
        for removed in (0, 1, 2):
            with self.subTest(removed=removed):
                log = filled(4)
                self.assertTrue(log.verify_chain())
                del log._records[removed]  # noqa: SLF001
                self.assertFalse(log.verify_chain())

    def test_a_record_inserted_before_the_last_check_is_caught_by_the_default_check(self) -> None:
        log = filled(3)
        self.assertTrue(log.verify_chain())
        log._records.insert(1, audit_record("planted"))  # noqa: SLF001
        self.assertFalse(log.verify_chain())

    def test_after_a_failed_check_the_next_check_reads_everything_again(self) -> None:
        log = filled(3)
        stored = log._records  # noqa: SLF001
        original = stored[1]
        stored[1] = original.model_copy(update={"decision": "deny"})
        self.assertFalse(log.verify_chain())
        stored[1] = original
        self.assertTrue(log.verify_chain())


#: An audit chain written by the code at commit 905ed28, before audit records
#: named their planner: two records from the Guard with the real planner and
#: one bounded contribution. The Postgres version of these tests is in
#: ``test_postgres_roles.py``.
OLD_CHAIN = Path(__file__).resolve().parent / "fixtures" / "audit_chain_905ed28.json"


def old_chain_records() -> list[dict]:
    """The stored form of each record, as the old code dumped it."""
    return json.loads(OLD_CHAIN.read_text(encoding="utf-8"))["records"]


def new_record(request_id: str) -> AuditRecord:
    """A record as the Guard writes it now, naming its planner."""
    return audit_record(request_id).model_copy(
        update={"planner_version": "9.9.9", "planner_sha256": hashlib.sha256(request_id.encode()).hexdigest()}
    )


class ChainWrittenBeforePlannerIdentityTests(unittest.TestCase):
    """Chains stored before audit records named their planner still verify."""

    def old_log(self) -> InMemoryAuditLog:
        log = InMemoryAuditLog()
        # The in-memory log has no loader, so the stored records go in as they are.
        log._records.extend(AuditRecord.model_validate(stored) for stored in old_chain_records())  # noqa: SLF001
        return log

    def test_the_fixture_has_no_planner_fields(self) -> None:
        for stored in old_chain_records():
            self.assertNotIn("planner_version", stored)
            self.assertNotIn("planner_sha256", stored)

    def test_each_old_record_hashes_to_the_digest_it_was_stored_with(self) -> None:
        for stored in old_chain_records():
            record = AuditRecord.model_validate(stored)
            self.assertIsNone(record.planner_version)
            self.assertIsNone(record.planner_sha256)
            self.assertEqual(stored["digest"], audit_digest(record))

    def test_an_old_chain_verifies_in_full_and_by_default(self) -> None:
        log = self.old_log()
        self.assertTrue(log.verify_chain())
        self.assertTrue(log.verify_chain(full=True))

    def test_new_records_after_an_old_chain_verify_incrementally_and_in_full(self) -> None:
        log = self.old_log()
        self.assertTrue(log.verify_chain())
        last_old = log.list_recent(1)[0]
        first_new = log.append(new_record("after-1"))
        log.append(new_record("after-2"))
        self.assertEqual(last_old.digest, first_new.previous_digest)
        self.assertTrue(log.verify_chain())
        self.assertTrue(log.verify_chain(full=True))
        self.assertTrue(self.old_log().verify_chain(), "the old records alone still verify")

    def test_an_edit_to_an_old_or_new_record_is_still_caught(self) -> None:
        log = self.old_log()
        log.append(new_record("after-1"))
        log.append(new_record("after-2"))
        stored = log._records  # noqa: SLF001
        for index, changes in ((0, {"permitted": False}), (1, {"planner_sha256": "0" * 64}), (3, {"planner_sha256": None})):
            with self.subTest(index=index, changes=changes):
                original = stored[index]
                stored[index] = original.model_copy(update=changes)
                self.assertFalse(log.verify_chain(full=True))
                stored[index] = original
        self.assertTrue(log.verify_chain(full=True))

    def test_adding_planner_fields_to_an_old_record_breaks_the_chain(self) -> None:
        log = self.old_log()
        stored = log._records  # noqa: SLF001
        stored[1] = stored[1].model_copy(update={"planner_version": "0.1.0a1"})
        self.assertFalse(log.verify_chain(full=True))


if __name__ == "__main__":
    unittest.main()
