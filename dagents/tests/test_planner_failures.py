"""Every way a planner call can fail, at the runner and at the Guard.

The runner raises :class:`PlannerUnavailableError` for each failure, with a
message that names the cause and repeats nothing from the request. The Guard
answers each one with a denial and an audit record of that denial.

The planners here are small shell scripts that stand in for ``dagentsc``: one
that sleeps, one that prints too much, one that prints half an answer. A real
process is the only way to test a timeout, an output cap and the killing of a
process group. The Guard's decisions on real plans are tested against the real
planner in ``test_governance.py``.
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from typing import Any
from unittest.mock import patch

import dagents
from dagents.common.application.ethical_guard import (
    STRATEGY_PARAMETERS,
    DagentscRestrictionPlanner,
    EthicalGuard,
    InMemoryAuditLog,
)
from dagents.common.domain.governance import RestrictionPlan
from dagents.common.infrastructure import dagents_runner
from dagents.common.infrastructure.dagents_runner import PlannerUnavailableError
from dagents.tests.test_governance import RECORDS, request_for

#: A value in every request these tests send. No error message may repeat it.
SECRET = "PHI-4417-do-not-log"

#: The settings the runner reads, cleared for each test so a developer's own
#: values cannot change the outcome.
LIMIT_SETTINGS = ("DAGENTSC_TIMEOUT_SECONDS", "DAGENTSC_MAX_INPUT_BYTES", "DAGENTSC_MAX_OUTPUT_BYTES", "DAGENTSC_SHA256")

#: What the Guard says, and records, when the planner fails.
UNREACHABLE = "The planner could not be reached, so the Guard denied the request."
UNUSABLE = "The planner's plan names a strategy the Guard cannot apply, so the Guard denied the request."
NOT_APPLIED = "The Guard could not apply the planner's plan to these records, so the Guard denied the request."

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

#: An answer nested deeper than Python's JSON decoder goes (about 1,000 levels).
NESTED_OBJECTS = b'{"a":' * 1200 + b"1" + b"}" * 1200

#: An answer the decoder reads, nested deeper than converting its keys can go.
#: Each list level costs the key conversion two Python frames.
NESTED_LISTS = b'{"a":' + b"[" * 700 + b"1" + b"]" * 700 + b"}"


def call_within(test: unittest.TestCase, seconds: float, call: Callable[[], Any]) -> Any:
    """Run ``call`` in a thread and return its result, or fail the test if it gives no answer in time.

    A call that hangs then fails its test instead of stopping the whole suite.
    An exception the call raises is raised here.
    """
    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            outcome["result"] = call()
        except BaseException as error:  # noqa: BLE001 - raised again in the test's thread
            outcome["error"] = error

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        test.fail(f"no answer within {seconds:g} seconds")
    if "error" in outcome:
        raise outcome["error"]
    return outcome["result"]


def release_fifo(fifo: pathlib.Path) -> None:
    """Open a FIFO for writing and close it, so a reader blocked on opening it gets an end of file."""
    try:
        descriptor = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return
    os.close(descriptor)


def process_gone(pid: int) -> bool:
    """Whether a process has exited. A zombie waiting for its parent counts as gone."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    try:
        state = pathlib.Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state in {"Z", "X"}


def wait_until_gone(pids: list[int], seconds: float = 3.0) -> list[int]:
    """The pids still running after up to ``seconds``."""
    deadline = time.monotonic() + seconds
    alive = list(pids)
    while alive and time.monotonic() < deadline:
        alive = [pid for pid in alive if not process_gone(pid)]
        if alive:
            time.sleep(0.05)
    return alive


@unittest.skipUnless(os.name == "posix", "the stand-in planners are shell scripts")
class FakePlannerCase(unittest.TestCase):
    """Runs each test with ``DAGENTSC_BIN`` pointing at a script the test writes."""

    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = pathlib.Path(folder.name)
        environ = {key: value for key, value in os.environ.items() if key not in LIMIT_SETTINGS}
        patcher = patch.dict(os.environ, environ, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def planner(self, body: str) -> pathlib.Path:
        """Write a stand-in planner with this shell body and point the runner at it."""
        path = self.folder / "dagentsc"
        path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
        path.chmod(0o755)
        os.environ["DAGENTSC_BIN"] = str(path)
        return path

    def planner_printing(self, output: bytes, *, exit_status: int = 0) -> pathlib.Path:
        """A planner that reads its input, prints ``output`` and exits with ``exit_status``."""
        answer = self.folder / "answer.out"
        answer.write_bytes(output)
        return self.planner(f'cat > /dev/null\ncat "{answer}"\nexit {exit_status}')

    def started_marker(self) -> pathlib.Path:
        return self.folder / "started"

    def planner_that_records_its_start(self) -> pathlib.Path:
        return self.planner(f'touch "{self.started_marker()}"\ncat > /dev/null\necho "{{}}"')

    def limits(self, **settings: str) -> None:
        os.environ.update(settings)

    def fifo_as_planner(self) -> pathlib.Path:
        """Point the runner at a FIFO with no writer, which blocks a plain ``open`` for reading."""
        fifo = self.folder / "dagentsc-fifo"
        os.mkfifo(fifo)
        # A test that hangs on the FIFO is released before its folder is removed.
        self.addCleanup(release_fifo, fifo)
        os.environ["DAGENTSC_BIN"] = str(fifo)
        return fifo

    def assert_clean_message(self, error: BaseException, *words: str) -> None:
        message = str(error)
        for word in words:
            self.assertIn(word, message)
        self.assertNotIn(SECRET, message, "a planner error must not repeat the request")
        self.assertLess(len(message), 1200, "a planner error must stay short enough for one log line")


def plan_request() -> dict[str, Any]:
    """A restriction request in the form the runner sends, holding the secret value."""
    return request_for(request_id=SECRET).model_dump(mode="json")


class RunnerFailureTests(FakePlannerCase):
    """Each failure raises PlannerUnavailableError and says why."""

    def test_a_missing_binary(self) -> None:
        os.environ["DAGENTSC_BIN"] = str(self.folder / "missing" / "dagentsc")
        with self.assertRaises(PlannerUnavailableError) as raised:
            dagents_runner.plan_restrictions(plan_request())
        self.assert_clean_message(raised.exception, "dagentsc could not be started", "No such file")
        self.assertIsInstance(raised.exception.__cause__, OSError)

    def test_a_non_zero_exit_names_the_status_and_the_planners_message(self) -> None:
        self.planner('cat > /dev/null\necho "Fatal error: exception Invalid_argument(\\"Missing JSON field: boundary\\")" >&2\nexit 2')
        with self.assertRaises(PlannerUnavailableError) as raised:
            dagents_runner.plan_restrictions(plan_request())
        self.assert_clean_message(raised.exception, "exit status 2", "Missing JSON field: boundary")
        # Started and then failed: core-service must not fall back to its Python renderer.
        self.assertNotIsInstance(raised.exception.__cause__, OSError)

    def test_a_planner_stopped_by_a_signal_names_the_signal(self) -> None:
        self.planner('cat > /dev/null\nprintf \'{"requestId": "x", "deci\'\nkill -9 $$')
        with self.assertRaises(PlannerUnavailableError) as raised:
            dagents_runner.plan_restrictions(plan_request())
        self.assert_clean_message(raised.exception, "stopped by signal 9")

    def test_a_long_error_output_is_cut_in_the_message(self) -> None:
        self.planner("cat > /dev/null\nhead -c 1000000 /dev/zero | tr '\\0' 'e' >&2\nexit 2")
        with self.assertRaises(PlannerUnavailableError) as raised:
            dagents_runner.plan_restrictions(plan_request())
        self.assert_clean_message(raised.exception, "exit status 2")

    def test_a_planner_that_does_not_finish_is_stopped_with_its_children(self) -> None:
        pids = self.folder / "pids"
        self.planner(f'echo $$ > "{pids}"\nsleep 30 &\necho $! >> "{pids}"\nwait')
        self.limits(DAGENTSC_TIMEOUT_SECONDS="1")
        started = time.monotonic()
        with self.assertRaises(PlannerUnavailableError) as raised:
            dagents_runner.plan_restrictions(plan_request())
        elapsed = time.monotonic() - started
        self.assert_clean_message(raised.exception, "did not finish within 1 second ", "DAGENTSC_TIMEOUT_SECONDS")
        self.assertNotIsInstance(raised.exception.__cause__, OSError)
        self.assertLess(elapsed, 5.0, "the runner waited far past its timeout")
        started_pids = [int(line) for line in pids.read_text().split()]
        self.assertEqual(2, len(started_pids), "the planner and its child should both have started")
        self.assertEqual([], wait_until_gone(started_pids), "the planner or its child was left running")

    def test_a_planner_that_never_reads_its_input_is_stopped(self) -> None:
        """Writing a large input to a planner that does not read it must not block past the timeout."""
        self.planner("sleep 30")
        self.limits(DAGENTSC_TIMEOUT_SECONDS="0.5")
        payload = plan_request() | {"padding": "x" * 2_000_000}
        started = time.monotonic()
        with self.assertRaises(PlannerUnavailableError) as raised:
            dagents_runner.run_dagentsc(["governance", "restrict", "--input", "-"], payload)
        self.assertLess(time.monotonic() - started, 5.0)
        self.assert_clean_message(raised.exception, "did not finish within 0.5 seconds")

    def test_output_over_the_cap_stops_the_planner_without_waiting_for_it(self) -> None:
        pids = self.folder / "pids"
        self.planner(f'echo $$ > "{pids}"\ncat > /dev/null\nhead -c 4096 /dev/zero | tr \'\\0\' \'x\'\nsleep 30')
        self.limits(DAGENTSC_MAX_OUTPUT_BYTES="1024")
        started = time.monotonic()
        with self.assertRaises(PlannerUnavailableError) as raised:
            dagents_runner.plan_restrictions(plan_request())
        self.assertLess(time.monotonic() - started, 5.0, "the runner waited for a planner it should have stopped")
        self.assert_clean_message(raised.exception, "more than 1024 bytes", "DAGENTSC_MAX_OUTPUT_BYTES")
        self.assertNotIsInstance(raised.exception.__cause__, OSError)
        self.assertEqual([], wait_until_gone([int(pids.read_text())]))

    def test_an_endless_output_stream_is_not_buffered(self) -> None:
        self.planner("cat > /dev/null\nexec yes")
        self.limits(DAGENTSC_MAX_OUTPUT_BYTES="65536")
        with self.assertRaises(PlannerUnavailableError) as raised:
            dagents_runner.plan_restrictions(plan_request())
        self.assert_clean_message(raised.exception, "more than 65536 bytes")

    def test_output_at_the_cap_is_read_in_full(self) -> None:
        answer = json.dumps({"padding": "y" * 200_000}).encode()
        self.planner_printing(answer)
        self.limits(DAGENTSC_MAX_OUTPUT_BYTES=str(len(answer)))
        self.assertEqual({"padding": "y" * 200_000}, dagents_runner.plan_restrictions(plan_request()))
        self.limits(DAGENTSC_MAX_OUTPUT_BYTES=str(len(answer) - 1))
        with self.assertRaises(PlannerUnavailableError):
            dagents_runner.plan_restrictions(plan_request())

    def test_truncated_output(self) -> None:
        self.planner_printing(b'{"requestId": "req-1", "boundary": "before_read", "assess')
        with self.assertRaises(PlannerUnavailableError) as raised:
            dagents_runner.plan_restrictions(plan_request())
        self.assert_clean_message(raised.exception, "ended before its JSON was complete")

    def test_no_output(self) -> None:
        self.planner_printing(b"")
        with self.assertRaises(PlannerUnavailableError) as raised:
            dagents_runner.plan_restrictions(plan_request())
        self.assert_clean_message(raised.exception, "printed no answer")

    def test_output_that_is_not_json(self) -> None:
        self.planner_printing(f"warning: {SECRET} looked odd\n".encode())
        with self.assertRaises(PlannerUnavailableError) as raised:
            dagents_runner.plan_restrictions(plan_request())
        self.assert_clean_message(raised.exception, "invalid json")

    def test_output_that_is_not_utf8(self) -> None:
        self.planner_printing(b'{"requestId": "\xff\xfe"}')
        with self.assertRaises(PlannerUnavailableError) as raised:
            dagents_runner.plan_restrictions(plan_request())
        self.assert_clean_message(raised.exception, "invalid json")

    def test_output_nested_too_deeply_to_decode(self) -> None:
        self.planner_printing(NESTED_OBJECTS)
        for call in (
            lambda: dagents_runner.plan_restrictions(plan_request()),
            lambda: dagents_runner.compile_round_plan({"round_id": SECRET}, []),
            lambda: dagents_runner.model_lifecycle(SECRET, {"kind": "registered"}),
        ):
            with self.subTest(call=call), self.assertRaises(PlannerUnavailableError) as raised:
                call()
            self.assert_clean_message(raised.exception, "nested too deeply")

    def test_output_nested_too_deeply_to_convert(self) -> None:
        """The decoder reads these lists; converting their keys is what goes too deep."""
        self.planner_printing(NESTED_LISTS)
        with self.assertRaises(PlannerUnavailableError) as raised:
            dagents_runner.compile_round_plan({"round_id": SECRET}, [])
        self.assert_clean_message(raised.exception, "nested too deeply")

    def test_a_special_file_is_not_read_to_check_a_pinned_hash(self) -> None:
        """A device or a FIFO named as the planner is refused at once, not read without end."""
        self.limits(DAGENTSC_SHA256="0" * 64, DAGENTSC_TIMEOUT_SECONDS="1")
        for name, select in (
            ("/dev/zero", lambda: os.environ.__setitem__("DAGENTSC_BIN", "/dev/zero")),
            ("a FIFO", self.fifo_as_planner),
        ):
            with self.subTest(planner=name):
                select()
                started = time.monotonic()
                with self.assertRaises(PlannerUnavailableError) as raised:
                    call_within(self, 10, lambda: dagents_runner.plan_restrictions(plan_request()))
                self.assertLess(time.monotonic() - started, 5.0)
                self.assert_clean_message(raised.exception, "DAGENTSC_SHA256", "not a regular file", "not started")
                self.assertNotIsInstance(raised.exception.__cause__, OSError)

    def test_a_special_file_has_no_hash(self) -> None:
        for name, select in (
            ("/dev/zero", lambda: os.environ.__setitem__("DAGENTSC_BIN", "/dev/zero")),
            ("a FIFO", self.fifo_as_planner),
        ):
            with self.subTest(planner=name):
                select()
                identity = call_within(self, 10, dagents_runner.planner_identity)
                self.assertEqual((dagents.__version__, None), (identity.version, identity.sha256))
                with self.assertRaises(OSError):
                    call_within(self, 10, lambda: dagents_runner.planner_sha256(os.environ["DAGENTSC_BIN"]))

    def test_an_input_over_the_cap_is_refused_before_the_planner_starts(self) -> None:
        self.planner_that_records_its_start()
        self.limits(DAGENTSC_MAX_INPUT_BYTES="256")
        with self.assertRaises(PlannerUnavailableError) as raised:
            dagents_runner.plan_restrictions(plan_request())
        self.assert_clean_message(raised.exception, "more than DAGENTSC_MAX_INPUT_BYTES (256)", "not started")
        self.assertRegex(str(raised.exception), r"the planner input is \d+ bytes")
        self.assertFalse(self.started_marker().exists(), "the planner was started with an input over the cap")

    def test_file_inputs_count_toward_the_input_cap(self) -> None:
        self.planner_that_records_its_start()
        self.limits(DAGENTSC_MAX_INPUT_BYTES="4096")
        records = [{"id": SECRET, "amount": index} for index in range(200)]
        scratch = self.folder / "scratch"
        scratch.mkdir()
        with patch.object(tempfile, "tempdir", str(scratch)):
            with self.assertRaises(PlannerUnavailableError) as raised:
                dagents_runner.evaluate_dataset_quality(records, [])
        self.assert_clean_message(raised.exception, "more than DAGENTSC_MAX_INPUT_BYTES (4096)")
        self.assertFalse(self.started_marker().exists())
        self.assertEqual([], list(scratch.iterdir()), "a temporary input file was left behind")

    def test_the_input_reaches_the_planner_unchanged(self) -> None:
        """The bounded reader and writer pass a large input and answer through intact."""
        self.planner("cat")
        payload = {"request_id": SECRET, "rows": [{"value": index} for index in range(20_000)]}
        self.assertEqual(payload, dagents_runner.run_dagentsc(["governance", "restrict", "--input", "-"], payload))

    def test_a_file_command_gets_no_standard_input(self) -> None:
        """A command that reads files must not wait on the caller's standard input."""
        # CI closes a step's standard input, so a planner that inherited it would
        # read an end of file at once and this test would pass either way. Hold
        # a pipe open on descriptor 0 instead: a planner that inherits it waits
        # until the timeout.
        read_end, write_end = os.pipe()
        try:
            saved = os.dup(0)
        except OSError:
            saved = None
        os.dup2(read_end, 0)

        def restore() -> None:
            if saved is None:
                os.close(0)
            else:
                os.dup2(saved, 0)
                os.close(saved)
            os.close(read_end)
            os.close(write_end)

        self.addCleanup(restore)
        self.planner('cat > /dev/null\necho \'{"blocking": false}\'')
        self.limits(DAGENTSC_TIMEOUT_SECONDS="5")
        self.assertEqual({"blocking": False}, dagents_runner.evaluate_dataset_quality([{"id": "a"}], []))


#: A round manifest the GMA accepts.
ROUND_MANIFEST = {
    "round_id": "round-1",
    "study_id": "study-a",
    "condition_id": "condition_a",
    "phase": "evaluation",
    "model_version": "model-1.0",
    "model_artifact_digest": "sha256:4f1c0e9a",
    "training_code_digest": "sha256:9b27d3e1",
    "feature_contract_version": "contract-v1",
    "privacy_profile": "guarded-rows",
    "aggregation": {"kind": "fedavg"},
    "minimum_participants": 2,
    "minimum_cohort_per_site": 20,
    "required_capabilities": ["evaluate"],
    "stop_conditions": ["schema_failure", "quorum_not_met"],
    "invited_sites": ["site-a", "site-b"],
}


class AgentAnswerTests(FakePlannerCase):
    """An LMA or GMA answers 503 planner_unavailable for an answer it cannot read, never a 500."""

    def test_an_answer_nested_too_deeply_is_a_503(self) -> None:
        from fastapi.testclient import TestClient

        from dagents.gma.app_factory import create_app as create_gma
        from dagents.gma.config import Settings as GmaSettings
        from dagents.lma.app_factory import create_app as create_lma
        from dagents.lma.config import Settings as LmaSettings

        gma_app = create_gma(GmaSettings.from_env({}))
        self.addCleanup(gma_app.state.gma.close)
        lma_app = create_lma(LmaSettings.from_env({}))
        self.addCleanup(lma_app.state.lma.close)
        calls = (
            ("GMA rounds:plan", gma_app, "/api/v1/federation/rounds:plan", ROUND_MANIFEST),
            ("GMA restrictions:plan", gma_app, "/api/v1/governance/restrictions:plan", plan_request()),
            ("LMA restrictions:plan", lma_app, "/api/v1/governance/restrictions:plan", plan_request()),
        )
        for answer in (NESTED_OBJECTS, NESTED_LISTS):
            self.planner_printing(answer)
            for name, app, path, body in calls:
                with self.subTest(call=name, answer=answer[:12]):
                    response = TestClient(app, raise_server_exceptions=False).post(path, json=body)
                    self.assertEqual(503, response.status_code, response.text[:200])
                    self.assertEqual("planner_unavailable", response.json()["code"])


class PlannerLimitSettingTests(unittest.TestCase):
    """The limits come from the process environment and refuse a value that makes no sense."""

    def test_the_defaults(self) -> None:
        limits = dagents_runner.planner_limits({})
        self.assertEqual(dagents_runner.DEFAULT_TIMEOUT_SECONDS, limits.timeout_seconds)
        self.assertEqual(dagents_runner.DEFAULT_MAX_INPUT_BYTES, limits.max_input_bytes)
        self.assertEqual(dagents_runner.DEFAULT_MAX_OUTPUT_BYTES, limits.max_output_bytes)
        self.assertEqual(limits, dagents_runner.planner_limits({name: " " for name in LIMIT_SETTINGS}))

    def test_values_are_read(self) -> None:
        limits = dagents_runner.planner_limits(
            {"DAGENTSC_TIMEOUT_SECONDS": "2.5", "DAGENTSC_MAX_INPUT_BYTES": "1000", "DAGENTSC_MAX_OUTPUT_BYTES": " 2000 "}
        )
        self.assertEqual((2.5, 1000, 2000), (limits.timeout_seconds, limits.max_input_bytes, limits.max_output_bytes))

    def test_a_value_that_makes_no_sense_is_refused_with_the_variable_named(self) -> None:
        cases = {
            "DAGENTSC_TIMEOUT_SECONDS": ("0", "-1", "ten", "nan", "inf", "-0.0"),
            "DAGENTSC_MAX_INPUT_BYTES": ("0", "-5", "1.5", "lots", "1e6", "²"),
            "DAGENTSC_MAX_OUTPUT_BYTES": ("0", "-5", "1.5", "lots"),
        }
        for name, values in cases.items():
            for value in values:
                with self.subTest(name=name, value=value):
                    with self.assertRaises(ValueError) as raised:
                        dagents_runner.planner_limits({name: value})
                    self.assertIn(name, str(raised.exception))
                    self.assertIn("above 0", str(raised.exception))

    def test_the_defaults_are_far_above_the_largest_real_call(self) -> None:
        """The defaults were measured from the repository's real planner calls; see the constants' comments."""
        self.assertGreaterEqual(dagents_runner.DEFAULT_TIMEOUT_SECONDS, 100 * 0.05)
        self.assertGreaterEqual(dagents_runner.DEFAULT_MAX_INPUT_BYTES, 100 * 12_000)
        self.assertGreaterEqual(dagents_runner.DEFAULT_MAX_OUTPUT_BYTES, 100 * 15_000)


class RunnerSettingFailureTests(FakePlannerCase):
    def test_a_setting_that_makes_no_sense_stops_every_call_before_the_planner_starts(self) -> None:
        self.planner_that_records_its_start()
        self.limits(DAGENTSC_TIMEOUT_SECONDS="0")
        with self.assertRaises(PlannerUnavailableError) as raised:
            dagents_runner.plan_restrictions(plan_request())
        self.assert_clean_message(raised.exception, "DAGENTSC_TIMEOUT_SECONDS must be")
        self.assertFalse(self.started_marker().exists())


#: A plan in the planner's camelCase form, for requests made by `request_for`.
def plan_output(strategy: str = "generalize:1", **changes: Any) -> bytes:
    plan = {
        "requestId": SECRET,
        "boundary": "before_read",
        "assessment": {"requesterId": "stroke-consortium-gma", "kyuScore": 0.97, "trust": "high", "rationale": []},
        "granularity": "row",
        "fieldRestrictions": [
            {"field": "nihss_total", "sensitivity": "high", "strategy": strategy, "reason": "test"},
            {"field": "age_band", "sensitivity": "medium", "strategy": "allow_full", "reason": "test"},
            {"field": "arrival_mode", "sensitivity": "low", "strategy": "allow_full", "reason": "test"},
        ],
        "decision": "narrow",
        "filteringScore": 0.12,
        "obligations": ["record the decision"],
        "violations": [],
    }
    plan.update(changes)
    return json.dumps(plan).encode()


class GuardDenialTests(FakePlannerCase):
    """Each planner failure gives a denial and an audit record of that denial."""

    def setUp(self) -> None:
        super().setUp()
        self.guard = EthicalGuard(DagentscRestrictionPlanner(), InMemoryAuditLog())

    def enforce_and_expect_denial(
        self,
        message: str = UNREACHABLE,
        cause: str | None = None,
        *,
        records: list[dict[str, Any]] | None = None,
        within: float = 30.0,
    ) -> None:
        records = RECORDS if records is None else records
        request = request_for(request_id=SECRET, cohort_size=len(records))
        with self.assertLogs("dagents.common.application.ethical_guard", level="ERROR") as logs:
            decision = call_within(
                self, within, lambda: self.guard.enforce(request, records, correlation_id="round-7")
            )
        self.assertFalse(decision.permitted)
        self.assertEqual([], decision.payload)
        self.assertEqual(list(request.requested_fields), decision.withheld_fields)
        self.assertEqual(message, decision.message)
        self.assertEqual("deny", decision.plan.decision)

        records = self.guard.audit.list_recent()
        self.assertEqual(1, len(records), "the denial must be audited")
        record = records[0]
        self.assertEqual(decision.audit_id, record.audit_id)
        self.assertEqual(
            (SECRET, "before_read", "deny", False, "round-7"),
            (record.request_id, record.boundary, record.decision, record.permitted, record.correlation_id),
        )
        self.assertEqual(list(request.requested_fields), record.withheld_fields)
        self.assertEqual([message], record.violations)
        # The record names the planner that failed: this package's version, and
        # the hash of the binary when there was one to read.
        binary = pathlib.Path(os.environ["DAGENTSC_BIN"])
        expected_sha256 = hashlib.sha256(binary.read_bytes()).hexdigest() if binary.is_file() else None
        self.assertEqual(
            (dagents.__version__, expected_sha256), (record.planner_version, record.planner_sha256)
        )
        self.assertTrue(self.guard.audit.verify_chain(full=True))
        if cause is not None:
            self.assertIn(cause, "\n".join(logs.output), "the log must say why the planner failed")

    def test_a_missing_binary(self) -> None:
        os.environ["DAGENTSC_BIN"] = str(self.folder / "missing" / "dagentsc")
        self.enforce_and_expect_denial(cause="could not be started")
        self.assertIsNone(self.guard.audit.list_recent()[0].planner_sha256, "there was no binary to hash")

    def test_a_device_named_as_the_planner(self) -> None:
        """/dev/zero never ends; hashing it to name the planner must not read it."""
        os.environ["DAGENTSC_BIN"] = "/dev/zero"
        self.limits(DAGENTSC_TIMEOUT_SECONDS="1")
        self.enforce_and_expect_denial(cause="could not be started", within=10)
        self.assertIsNone(self.guard.audit.list_recent()[0].planner_sha256, "a device has no hash")

    def test_a_fifo_named_as_the_planner(self) -> None:
        """Opening a FIFO with no writer blocks; hashing it to name the planner must not open it that way."""
        self.fifo_as_planner()
        self.limits(DAGENTSC_TIMEOUT_SECONDS="1")
        self.enforce_and_expect_denial(cause="could not be started", within=10)
        self.assertIsNone(self.guard.audit.list_recent()[0].planner_sha256, "a FIFO has no hash")

    def test_a_pinned_hash_that_differs(self) -> None:
        self.planner_that_records_its_start()
        self.limits(DAGENTSC_SHA256="0" * 64)
        self.enforce_and_expect_denial(cause="DAGENTSC_SHA256")
        self.assertFalse(self.started_marker().exists())

    def test_a_pinned_hash_that_is_not_a_sha256(self) -> None:
        self.planner_that_records_its_start()
        self.limits(DAGENTSC_SHA256="not-a-hash")
        self.enforce_and_expect_denial(cause="DAGENTSC_SHA256 must be")
        self.assertFalse(self.started_marker().exists())

    def test_a_non_zero_exit(self) -> None:
        self.planner('cat > /dev/null\necho "Fatal error" >&2\nexit 2')
        self.enforce_and_expect_denial(cause="exit status 2")

    def test_a_planner_that_does_not_finish(self) -> None:
        self.planner("sleep 30")
        self.limits(DAGENTSC_TIMEOUT_SECONDS="0.5")
        started = time.monotonic()
        self.enforce_and_expect_denial(cause="did not finish within 0.5 seconds")
        self.assertLess(time.monotonic() - started, 5.0)

    def test_output_over_the_cap(self) -> None:
        self.planner_printing(plan_output())
        self.limits(DAGENTSC_MAX_OUTPUT_BYTES="100")
        self.enforce_and_expect_denial(cause="more than 100 bytes")

    def test_truncated_output(self) -> None:
        whole = plan_output()
        self.planner_printing(whole[: len(whole) // 2])
        self.enforce_and_expect_denial(cause="ended before its JSON was complete")

    def test_output_that_is_not_json(self) -> None:
        self.planner_printing(b"Segmentation fault\n")
        self.enforce_and_expect_denial(cause="invalid json")

    def test_json_that_is_not_a_plan(self) -> None:
        self.planner_printing(b'{"decision": "permit"}')
        self.enforce_and_expect_denial(cause="validation error")

    def test_output_nested_too_deeply(self) -> None:
        for answer in (NESTED_OBJECTS, NESTED_LISTS):
            with self.subTest(answer=answer[:12]):
                self.guard = EthicalGuard(DagentscRestrictionPlanner(), InMemoryAuditLog())
                self.planner_printing(answer)
                self.enforce_and_expect_denial(cause="nested too deeply")

    def test_an_input_over_the_cap(self) -> None:
        self.planner_that_records_its_start()
        self.limits(DAGENTSC_MAX_INPUT_BYTES="256")
        self.enforce_and_expect_denial(cause="DAGENTSC_MAX_INPUT_BYTES")
        self.assertFalse(self.started_marker().exists())

    def test_a_setting_that_makes_no_sense(self) -> None:
        self.planner_that_records_its_start()
        self.limits(DAGENTSC_MAX_OUTPUT_BYTES="-1")
        self.enforce_and_expect_denial(cause="DAGENTSC_MAX_OUTPUT_BYTES must be")
        self.assertFalse(self.started_marker().exists())

    def test_a_strategy_the_guard_does_not_know(self) -> None:
        self.planner_printing(plan_output("blur:2"))
        self.enforce_and_expect_denial(UNUSABLE, cause="nihss_total: blur:2")

    def test_a_strategy_without_the_parameter_it_needs(self) -> None:
        for strategy in ("aggregate_only:many", "aggregate_only", "generalize:", "clip_contribution:nan", "redact:1"):
            with self.subTest(strategy=strategy):
                self.guard = EthicalGuard(DagentscRestrictionPlanner(), InMemoryAuditLog())
                self.planner_printing(plan_output(strategy))
                self.enforce_and_expect_denial(UNUSABLE, cause=f"nihss_total: {strategy}")

    def test_a_generalize_level_too_large_to_apply(self) -> None:
        """10 to the power of the level must fit in a float, so the largest level is 308."""
        for strategy in ("generalize:309", "generalize:400"):
            with self.subTest(strategy=strategy):
                self.guard = EthicalGuard(DagentscRestrictionPlanner(), InMemoryAuditLog())
                self.planner_printing(plan_output(strategy))
                self.enforce_and_expect_denial(UNUSABLE, cause=f"nihss_total: {strategy}")

    def test_a_known_strategy_still_permits(self) -> None:
        """The stand-in planner itself is not what denies: a plan it prints with known strategies is applied."""
        self.planner_printing(plan_output("generalize:1"))
        decision = self.guard.enforce(request_for(request_id=SECRET, cohort_size=len(RECORDS)), RECORDS)
        self.assertTrue(decision.permitted)
        self.assertEqual(["nihss_total"], decision.transformed_fields)
        self.assertEqual(1, len(self.guard.audit.list_recent()))

    def test_the_largest_generalize_level_and_an_aggregate_minimum_of_zero_still_apply(self) -> None:
        """The planner sends aggregate_only:0 when a classification sets no minimum cohort."""
        for strategy in ("generalize:308", "aggregate_only:0"):
            with self.subTest(strategy=strategy):
                self.guard = EthicalGuard(DagentscRestrictionPlanner(), InMemoryAuditLog())
                self.planner_printing(plan_output(strategy))
                decision = self.guard.enforce(request_for(request_id=SECRET, cohort_size=len(RECORDS)), RECORDS)
                self.assertTrue(decision.permitted, decision.message)
                self.assertEqual(["nihss_total"], decision.transformed_fields)
                self.assertEqual(1, len(self.guard.audit.list_recent()))


class GuardStrategyCheckTests(unittest.TestCase):
    """The Guard checks the strategies of any planner, not only dagentsc's."""

    def test_a_planner_object_with_an_unknown_strategy_is_denied(self) -> None:
        plan = RestrictionPlan.model_validate(
            dagents_runner.convert_keys(json.loads(plan_output("shuffle")), dagents_runner.to_snake_case)
        )

        class OddPlanner:
            def plan(self, request):  # noqa: ANN001, ANN201 - test double
                return plan

        guard = EthicalGuard(OddPlanner(), InMemoryAuditLog())
        with self.assertLogs("dagents.common.application.ethical_guard", level="ERROR"):
            decision = guard.enforce(request_for(request_id=SECRET), RECORDS)
        self.assertFalse(decision.permitted)
        self.assertEqual(UNUSABLE, decision.message)
        self.assertEqual([UNUSABLE], guard.audit.list_recent()[0].violations)

    def test_a_huge_generalize_level_is_refused_before_any_arithmetic(self) -> None:
        """A Guard that computed 10 ** 10**18 would never answer, and would hold the interpreter while it tried.

        A thread cannot bound that, so the decision runs in a child process
        that the test stops after 30 seconds.
        """
        child = (
            "import json\n"
            "from dagents.common.application.ethical_guard import EthicalGuard, InMemoryAuditLog\n"
            "from dagents.common.domain.governance import RestrictionPlan\n"
            "from dagents.common.infrastructure import dagents_runner\n"
            "from dagents.tests.test_governance import RECORDS, request_for\n"
            "from dagents.tests.test_planner_failures import plan_output\n"
            "plan = RestrictionPlan.model_validate(dagents_runner.convert_keys(\n"
            "    json.loads(plan_output('generalize:1e18')), dagents_runner.to_snake_case))\n"
            "class Planner:\n"
            "    def plan(self, request):\n"
            "        return plan\n"
            "guard = EthicalGuard(Planner(), InMemoryAuditLog())\n"
            "decision = guard.enforce(request_for(request_id=plan.request_id, cohort_size=len(RECORDS)), RECORDS)\n"
            "records = guard.audit.list_recent()\n"
            "print(json.dumps({'permitted': decision.permitted, 'message': decision.message,\n"
            "                  'records': len(records), 'chain': guard.audit.verify_chain(full=True)}))\n"
        )
        environ = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
        try:
            finished = subprocess.run(
                [sys.executable, "-c", child], cwd=REPO_ROOT, env=environ, capture_output=True, text=True, timeout=30
            )
        except subprocess.TimeoutExpired:
            self.fail("the Guard gave no answer within 30 seconds for generalize:1e18")
        self.assertEqual(0, finished.returncode, finished.stderr[-2000:])
        outcome = json.loads(finished.stdout.strip().splitlines()[-1])
        self.assertEqual({"permitted": False, "message": UNUSABLE, "records": 1, "chain": True}, outcome)

    def test_a_plan_the_guard_fails_to_apply_to_the_records_is_denied(self) -> None:
        """A usable strategy can still fail on a value, such as an integer too large for a float."""
        plan = RestrictionPlan.model_validate(
            dagents_runner.convert_keys(json.loads(plan_output("generalize:1")), dagents_runner.to_snake_case)
        )

        class PlainPlanner:
            def plan(self, request):  # noqa: ANN001, ANN201 - test double
                return plan

        records = [dict(RECORDS[0], nihss_total=10**400), *RECORDS[1:]]
        guard = EthicalGuard(PlainPlanner(), InMemoryAuditLog())
        with self.assertLogs("dagents.common.application.ethical_guard", level="ERROR") as logs:
            decision = guard.enforce(request_for(request_id=SECRET, cohort_size=len(records)), records)
        self.assertFalse(decision.permitted)
        self.assertEqual([], decision.payload)
        self.assertEqual(NOT_APPLIED, decision.message)
        self.assertEqual("deny", decision.plan.decision)
        audit = guard.audit.list_recent()
        self.assertEqual(1, len(audit), "the denial must be audited")
        self.assertEqual(("deny", False, [NOT_APPLIED]), (audit[0].decision, audit[0].permitted, audit[0].violations))
        self.assertTrue(guard.audit.verify_chain(full=True))
        self.assertIn("could not apply the plan", "\n".join(logs.output))
        self.assertNotIn(str(10**400)[:40], "\n".join(logs.output), "the log must not repeat a record value")

    def test_the_guard_knows_exactly_the_planners_strategies(self) -> None:
        """Read the planner's strategy names from its source, so the two lists cannot drift apart."""
        source = (REPO_ROOT / "bindings" / "ocaml" / "lib" / "common_ir" / "dagents_common_ir.ml").read_text(encoding="utf-8")
        body = re.search(r"let string_of_restriction_strategy = function(.*?)\n\n", source, re.DOTALL)
        self.assertIsNotNone(body, "string_of_restriction_strategy was not found in common_ir")
        planner = {}
        for name, parameter in re.findall(r'"([a-z_]+)(:%[a-z])?"', body.group(1)):
            planner[name] = bool(parameter)
        self.assertEqual(planner, dict(STRATEGY_PARAMETERS))


if __name__ == "__main__":
    unittest.main()
