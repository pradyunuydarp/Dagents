"""The in-memory audit log: counting records and checking the chain cheaply.

The audit endpoints of a public demo check the chain on every read. A full
check re-hashes every record, so its cost grows with every decision the Guard
records. By default ``verify_chain`` re-hashes only the records added since the
last successful check; ``verify_chain(full=True)`` re-hashes them all. The
Postgres sink has the same interface and its tests are in
``test_postgres_roles.py``.
"""

from __future__ import annotations

import time
import unittest
from unittest import mock
import uuid

from agents.common.application import ethical_guard
from agents.common.application.ethical_guard import InMemoryAuditLog
from agents.common.domain.governance import AuditRecord


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


if __name__ == "__main__":
    unittest.main()
