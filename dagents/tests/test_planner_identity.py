"""Which planner decided: the planner identity in each audit record, and the pinned SHA-256.

Every audit record the Guard writes names the planner that decided, or tried
to: ``planner_version`` is the version of this package, whose runner started
the planner, and ``planner_sha256`` is the SHA-256 of the binary it started.
When an operator sets ``DAGENTSC_SHA256``, the runner starts only a binary with
that hash; any other makes the planner unavailable, so the Guard denies.

No test here pins a hash value. Each one hashes the file it uses while it runs,
because the binary differs between machines and between CI runs.

The runner tests use small shell scripts that stand in for ``dagentsc``. The
Guard tests use the real planner, as ``test_governance.py`` does.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import pathlib
import tempfile
import time
import unittest
from unittest.mock import patch

import dagents
from dagents.common.application.ethical_guard import (
    PLANNER_UNREACHABLE,
    DagentscRestrictionPlanner,
    EthicalGuard,
    InMemoryAuditLog,
    audit_digest,
)
from dagents.common.domain.governance import RestrictionPlan
from dagents.common.infrastructure import dagents_runner
from dagents.common.infrastructure.dagents_runner import PlannerUnavailableError
from dagents.tests.test_governance import RECORDS, dagentsc_available, request_for

#: Every setting the runner reads. Each test starts without them, so a
#: developer's own values cannot change the outcome.
PLANNER_SETTINGS = (
    "DAGENTSC_BIN",
    "DAGENTSC_SHA256",
    "DAGENTSC_TIMEOUT_SECONDS",
    "DAGENTSC_MAX_INPUT_BYTES",
    "DAGENTSC_MAX_OUTPUT_BYTES",
)

#: A well-formed SHA-256 that no planner has.
OTHER_SHA256 = "0" * 64


def sha256_of(path: str | os.PathLike[str]) -> str:
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


class StandInPlannerCase(unittest.TestCase):
    """Runs each test with a clean environment and a folder for stand-in planners."""

    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = pathlib.Path(folder.name)
        self.empty_path = self.folder / "empty-path"
        self.empty_path.mkdir()
        environ = {key: value for key, value in os.environ.items() if key not in PLANNER_SETTINGS}
        environ["PATH"] = str(self.empty_path) + os.pathsep + "/usr/bin:/bin"
        patcher = patch.dict(os.environ, environ, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        bundled = patch.object(dagents_runner, "BUNDLED_PLANNER", self.folder / "_bin" / "dagentsc")
        bundled.start()
        self.addCleanup(bundled.stop)

    def stand_in(self, name: str = "dagentsc", answer: str = '{"roundDigest": "sha256:abc"}') -> pathlib.Path:
        """Write a planner that records its start, reads its input and prints ``answer``, and select it."""
        path = self.folder / name
        marker = self.folder / f"{name}.started"
        path.write_text(f'#!/bin/sh\ntouch "{marker}"\ncat > /dev/null\necho \'{answer}\'\n', encoding="utf-8")
        path.chmod(0o755)
        os.environ["DAGENTSC_BIN"] = str(path)
        return path

    def started(self, name: str = "dagentsc") -> bool:
        return (self.folder / f"{name}.started").exists()


@unittest.skipUnless(os.name == "posix", "the stand-in planners are shell scripts")
class PlannerIdentityTests(StandInPlannerCase):
    """The runner says which planner it starts."""

    def test_the_identity_is_this_packages_version_and_the_binarys_sha256(self) -> None:
        path = self.stand_in()
        identity = dagents_runner.planner_identity()
        self.assertEqual(dagents.__version__, identity.version)
        self.assertEqual(sha256_of(path), identity.sha256)

    def test_a_planner_on_path_is_hashed_at_the_file_the_system_starts(self) -> None:
        on_path = self.empty_path / "dagentsc"
        on_path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        on_path.chmod(0o755)
        self.assertEqual("dagentsc", dagents_runner.dagentsc_binary())
        self.assertEqual(sha256_of(on_path), dagents_runner.planner_identity().sha256)

    def test_a_missing_planner_has_a_version_and_no_hash(self) -> None:
        os.environ["DAGENTSC_BIN"] = str(self.folder / "missing" / "dagentsc")
        identity = dagents_runner.planner_identity()
        self.assertEqual(dagents.__version__, identity.version)
        self.assertIsNone(identity.sha256)

    def test_the_hash_is_computed_once_per_path_size_and_modification_time(self) -> None:
        first = self.stand_in("first")
        with patch.object(dagents_runner, "_hash_file", wraps=dagents_runner._hash_file) as hashed:  # noqa: SLF001
            for _ in range(3):
                dagents_runner.planner_identity()
            self.assertEqual(1, hashed.call_count, "an unchanged file is hashed once")

            later = time.time() + 5
            os.utime(first, (later, later))
            self.assertEqual(sha256_of(first), dagents_runner.planner_identity().sha256)
            self.assertEqual(2, hashed.call_count, "a new modification time is hashed again")

            first.write_text(first.read_text(encoding="utf-8") + "# a longer file\n", encoding="utf-8")
            os.utime(first, (later, later))
            self.assertEqual(sha256_of(first), dagents_runner.planner_identity().sha256)
            self.assertEqual(3, hashed.call_count, "a new size is hashed again")

            second = self.stand_in("second")
            self.assertEqual(sha256_of(second), dagents_runner.planner_identity().sha256)
            self.assertEqual(4, hashed.call_count, "another path is hashed on its own")

            os.environ["DAGENTSC_BIN"] = str(first)
            dagents_runner.planner_identity()
            self.assertEqual(4, hashed.call_count, "each path keeps its own hash")

    def test_a_planner_call_does_not_hash_the_binary_when_no_hash_is_pinned(self) -> None:
        self.stand_in()
        with patch.object(dagents_runner, "_hash_file", wraps=dagents_runner._hash_file) as hashed:  # noqa: SLF001
            dagents_runner.round_digest({"round_id": "r"})
        self.assertEqual(0, hashed.call_count)


@unittest.skipUnless(os.name == "posix", "the stand-in planners are shell scripts")
class PinnedHashTests(StandInPlannerCase):
    """With DAGENTSC_SHA256 set, the runner starts only a binary with that hash."""

    def test_a_pinned_hash_that_matches_runs_the_planner(self) -> None:
        path = self.stand_in()
        for written in (sha256_of(path), sha256_of(path).upper(), f"  {sha256_of(path)}\n"):
            with self.subTest(written=written):
                os.environ["DAGENTSC_SHA256"] = written
                self.assertEqual({"round_digest": "sha256:abc"}, dagents_runner.round_digest({"round_id": "r"}))
        self.assertIsNone(dagents_runner.planner_problem())

    def test_a_pinned_hash_that_differs_stops_every_call_before_the_planner_starts(self) -> None:
        path = self.stand_in()
        os.environ["DAGENTSC_SHA256"] = OTHER_SHA256
        with self.assertRaises(PlannerUnavailableError) as raised:
            dagents_runner.round_digest({"round_id": "r"})
        message = str(raised.exception)
        for words in ("DAGENTSC_SHA256", sha256_of(path), OTHER_SHA256, "not started"):
            self.assertIn(words, message)
        self.assertFalse(self.started(), "a binary with another hash must not run")
        self.assertNotIsInstance(
            raised.exception.__cause__, OSError, "core-service falls back to Python only when no planner could start"
        )

    def test_calls_with_a_pinned_hash_read_an_unchanged_binary_once(self) -> None:
        path = self.stand_in()
        os.environ["DAGENTSC_SHA256"] = sha256_of(path)
        guard = EthicalGuard(DagentscRestrictionPlanner(), InMemoryAuditLog())
        with patch.object(dagents_runner, "_hash_file", wraps=dagents_runner._hash_file) as hashed:  # noqa: SLF001
            for _ in range(3):
                dagents_runner.round_digest({"round_id": "r"})
                guard._planner_identity()  # noqa: SLF001 - what the Guard writes into each record
        self.assertEqual(1, hashed.call_count, "the pin check and the audit record share one hash of the file")

    def test_the_file_commands_are_checked_too(self) -> None:
        self.stand_in(answer="{}")
        os.environ["DAGENTSC_SHA256"] = OTHER_SHA256
        with self.assertRaises(PlannerUnavailableError):
            dagents_runner.validate_dataset_schema([{"a": 1}], {"fields": []})
        self.assertFalse(self.started())

    def test_a_binary_replaced_after_a_check_is_checked_again(self) -> None:
        path = self.stand_in()
        os.environ["DAGENTSC_SHA256"] = sha256_of(path)
        dagents_runner.round_digest({"round_id": "r"})
        path.write_text(path.read_text(encoding="utf-8") + "# replaced\n", encoding="utf-8")
        (self.folder / "dagentsc.started").unlink()
        with self.assertRaises(PlannerUnavailableError):
            dagents_runner.round_digest({"round_id": "r"})
        self.assertFalse(self.started())

    def test_a_binary_rewritten_in_place_with_its_old_times_is_hashed_again(self) -> None:
        """``cp -p``, ``rsync --inplace`` or ``tar x`` can keep the size, the modification time and the inode.

        Only the change time moves, and user space cannot set it back.
        """
        path = self.stand_in()
        old_sha256 = sha256_of(path)
        os.environ["DAGENTSC_SHA256"] = old_sha256
        dagents_runner.round_digest({"round_id": "r"})
        (self.folder / "dagentsc.started").unlink()
        before = path.stat()

        content = path.read_bytes()
        rewritten = content.replace(b"sha256:abc", b"sha256:xyz")
        self.assertEqual(len(content), len(rewritten))
        self.assertNotEqual(content, rewritten)
        with open(path, "r+b") as handle:
            handle.write(rewritten)
        # Restore the old times. A kernel with coarse timestamps can leave the
        # change time on the same tick, so repeat until it has moved.
        deadline = time.monotonic() + 5
        while True:
            os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
            after = path.stat()
            if after.st_ctime_ns != before.st_ctime_ns or time.monotonic() > deadline:
                break
            time.sleep(0.01)
        self.assertEqual(
            (before.st_size, before.st_mtime_ns, before.st_ino, before.st_dev),
            (after.st_size, after.st_mtime_ns, after.st_ino, after.st_dev),
        )
        self.assertNotEqual(before.st_ctime_ns, after.st_ctime_ns, "the change time did not move")

        new_sha256 = sha256_of(path)
        self.assertNotEqual(old_sha256, new_sha256)
        self.assertEqual(new_sha256, dagents_runner.planner_identity().sha256)
        with self.assertRaises(PlannerUnavailableError) as raised:
            dagents_runner.round_digest({"round_id": "r"})
        self.assertIn(new_sha256, str(raised.exception))
        self.assertFalse(self.started(), "a binary with another hash must not run")

        guard = EthicalGuard(DagentscRestrictionPlanner(), InMemoryAuditLog())
        with self.assertLogs("dagents.common.application.ethical_guard", level="ERROR"):
            decision = guard.enforce(request_for(), RECORDS)
        self.assertFalse(decision.permitted)
        records = guard.audit.list_recent()
        self.assertEqual(1, len(records), "the denial must be audited")
        self.assertEqual(new_sha256, records[0].planner_sha256, "the record names the binary on disk")
        self.assertFalse(self.started())

    def test_a_pinned_hash_that_is_not_a_sha256_is_refused(self) -> None:
        self.stand_in()
        for written in ("abc123", "g" * 64, "0" * 63, "0" * 65, "sha256:" + "0" * 64):
            with self.subTest(written=written):
                os.environ["DAGENTSC_SHA256"] = written
                with self.assertRaises(ValueError) as raised:
                    dagents_runner.pinned_sha256()
                self.assertIn("DAGENTSC_SHA256 must be", str(raised.exception))
                self.assertNotIn(written, str(raised.exception))
                with self.assertRaises(ValueError):
                    dagents_runner.check_planner_settings()
                with self.assertRaises(PlannerUnavailableError) as call:
                    dagents_runner.round_digest({"round_id": "r"})
                self.assertIn("DAGENTSC_SHA256 must be", str(call.exception))
                self.assertFalse(self.started())

    def test_an_empty_setting_pins_nothing(self) -> None:
        self.stand_in()
        os.environ["DAGENTSC_SHA256"] = "  "
        self.assertIsNone(dagents_runner.pinned_sha256())
        dagents_runner.round_digest({"round_id": "r"})
        self.assertTrue(self.started())

    def test_the_problem_names_a_pinned_hash_that_differs(self) -> None:
        path = self.stand_in()
        os.environ["DAGENTSC_SHA256"] = OTHER_SHA256
        problem = dagents_runner.planner_problem()
        self.assertIsNotNone(problem)
        for words in (str(path), sha256_of(path), "DAGENTSC_SHA256"):
            self.assertIn(words, problem)

    def test_the_problem_names_a_pinned_hash_that_is_not_a_sha256(self) -> None:
        self.stand_in()
        os.environ["DAGENTSC_SHA256"] = "abc"
        self.assertIn("DAGENTSC_SHA256 must be", dagents_runner.planner_problem())

    def test_the_agents_warning_names_the_pinned_hash(self) -> None:
        self.stand_in()
        os.environ["DAGENTSC_SHA256"] = OTHER_SHA256
        log = logging.getLogger("dagents.tests.pinned-planner")
        with self.assertLogs(log, level="WARNING") as captured:
            self.assertIsNotNone(dagents_runner.warn_if_no_planner(log, "dagents-lma"))
        self.assertEqual(1, len(captured.records))
        message = captured.records[0].getMessage()
        for words in ("dagents-lma has no planner", "DAGENTSC_SHA256", "denies every request", "python -m dagents"):
            self.assertIn(words, message)

    def test_a_planner_that_cannot_be_read_cannot_be_checked(self) -> None:
        """A binary the process may run but not read, such as one with mode 0111."""
        self.stand_in()
        os.environ["DAGENTSC_SHA256"] = OTHER_SHA256
        unreadable = PermissionError(13, "Permission denied")
        with patch.object(dagents_runner, "_hash_file", side_effect=unreadable):
            self.assertIn("could not be read", dagents_runner.planner_problem())
            with self.assertRaises(PlannerUnavailableError) as raised:
                dagents_runner.round_digest({"round_id": "r"})
            self.assertIn("DAGENTSC_SHA256", str(raised.exception))
            self.assertIsNone(dagents_runner.planner_identity().sha256)
        self.assertFalse(self.started())


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class GuardRecordsThePlannerTests(unittest.TestCase):
    """Every audit record the Guard writes names the planner, with the real planner."""

    def setUp(self) -> None:
        environ = {key: value for key, value in os.environ.items() if key != "DAGENTSC_SHA256"}
        patcher = patch.dict(os.environ, environ, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.planner_path, _ = dagents_runner.find_planner()
        self.planner_sha256 = sha256_of(self.planner_path)
        self.guard = EthicalGuard(DagentscRestrictionPlanner(), InMemoryAuditLog())

    def assert_names_the_planner(self, record) -> None:  # noqa: ANN001 - an AuditRecord
        self.assertEqual(dagents.__version__, record.planner_version)
        self.assertEqual(self.planner_sha256, record.planner_sha256)
        self.assertNotIn(self.planner_path, json.dumps(record.model_dump(mode="json")), "no server path in the chain")

    def test_permitted_and_denied_decisions_name_the_planner(self) -> None:
        permitted = self.guard.enforce(request_for(request_id="read", cohort_size=len(RECORDS)), RECORDS)
        denied = self.guard.enforce(request_for(request_id="purpose", declared_purpose="marketing"), RECORDS)
        self.assertTrue(permitted.permitted)
        self.assertFalse(denied.permitted)
        records = self.guard.audit.list_recent()
        self.assertEqual(2, len(records))
        for record in records:
            self.assert_names_the_planner(record)
        self.assertTrue(self.guard.audit.verify_chain(full=True))

    def test_the_digest_covers_the_planner(self) -> None:
        self.guard.enforce(request_for(request_id="read", cohort_size=len(RECORDS)), RECORDS)
        self.guard.enforce(request_for(request_id="read-2", cohort_size=len(RECORDS)), RECORDS)
        audit = self.guard.audit
        stored = audit._records  # noqa: SLF001 - reaching in is the point of the test
        original = stored[0]
        for changes in ({"planner_sha256": OTHER_SHA256}, {"planner_sha256": None}, {"planner_version": None}):
            with self.subTest(changes=changes):
                stored[0] = original.model_copy(update=changes)
                self.assertFalse(audit.verify_chain(full=True))
        stored[0] = original
        self.assertTrue(audit.verify_chain(full=True))

    def test_a_pinned_hash_that_matches_decides_as_before(self) -> None:
        os.environ["DAGENTSC_SHA256"] = self.planner_sha256
        decision = self.guard.enforce(request_for(request_id="read", cohort_size=len(RECORDS)), RECORDS)
        self.assertTrue(decision.permitted)
        self.assert_names_the_planner(self.guard.audit.list_recent()[0])

    def test_a_pinned_hash_that_differs_makes_the_guard_deny_and_record_the_binary(self) -> None:
        os.environ["DAGENTSC_SHA256"] = OTHER_SHA256
        with self.assertLogs("dagents.common.application.ethical_guard", level="ERROR") as logs:
            decision = self.guard.enforce(request_for(request_id="read", cohort_size=len(RECORDS)), RECORDS)
        self.assertFalse(decision.permitted)
        self.assertEqual([], decision.payload)
        self.assertEqual(PLANNER_UNREACHABLE, decision.message)
        records = self.guard.audit.list_recent()
        self.assertEqual(1, len(records), "the denial must be audited")
        self.assertEqual(("deny", False), (records[0].decision, records[0].permitted))
        self.assert_names_the_planner(records[0])
        self.assertTrue(self.guard.audit.verify_chain(full=True))
        self.assertIn("DAGENTSC_SHA256", "\n".join(logs.output))


class GuardWithoutPlannerIdentityTests(unittest.TestCase):
    """A planner object without ``identity()`` still works; its records name no planner."""

    def test_the_fields_stay_empty_and_the_record_hashes_without_them(self) -> None:
        from dagents.tests.test_planner_failures import plan_output

        plan = RestrictionPlan.model_validate(
            dagents_runner.convert_keys(json.loads(plan_output("generalize:1")), dagents_runner.to_snake_case)
        )

        class PlainPlanner:
            def plan(self, request):  # noqa: ANN001, ANN201 - test double
                return plan

        guard = EthicalGuard(PlainPlanner(), InMemoryAuditLog())
        guard.enforce(request_for(request_id=plan.request_id, cohort_size=len(RECORDS)), RECORDS)
        record = guard.audit.list_recent()[0]
        self.assertIsNone(record.planner_version)
        self.assertIsNone(record.planner_sha256)
        material = record.model_dump(mode="json", exclude={"digest", "planner_version", "planner_sha256"})
        expected = hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        self.assertEqual(expected, audit_digest(record))
        self.assertTrue(guard.audit.verify_chain(full=True))

    def test_a_planner_whose_identity_fails_still_gets_a_decision(self) -> None:
        class FailingIdentity:
            def identity(self):  # noqa: ANN201 - test double
                raise OSError("the binary vanished")

            def plan(self, request):  # noqa: ANN001, ANN201 - test double
                raise PlannerUnavailableError("dagentsc could not be started")

        guard = EthicalGuard(FailingIdentity(), InMemoryAuditLog())
        with self.assertLogs("dagents.common.application.ethical_guard", level="ERROR"):
            decision = guard.enforce(request_for(), RECORDS)
        self.assertFalse(decision.permitted)
        record = guard.audit.list_recent()[0]
        self.assertIsNone(record.planner_sha256)
        self.assertTrue(guard.audit.verify_chain(full=True))


class PlannerSettingsTests(StandInPlannerCase):
    def test_the_settings_check_reads_the_limits_and_the_pin(self) -> None:
        self.assertEqual(dagents_runner.planner_limits(), dagents_runner.check_planner_settings())
        os.environ["DAGENTSC_TIMEOUT_SECONDS"] = "-1"
        with self.assertRaises(ValueError):
            dagents_runner.check_planner_settings()


if __name__ == "__main__":
    unittest.main()
