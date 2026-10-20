"""A GMA process killed with SIGKILL during a study and started again on the same port, against PostgreSQL 16 and the real planner.

``test_gma_restart.py`` restarts the GMA app inside the test process. This
file stops a real GMA process as a container runtime or the kernel does, with
SIGKILL to its process group, so nothing in it runs again: no ``finally``
block, no lifespan shutdown, no last database write.

Each test starts the GMA and three LMAs as separate processes with
``start_over_http`` from ``test_agent_network_flow.py``, on loopback ports in
9800-9899. The LMAs run under uvicorn as that module starts them, with the toy
extension. The GMA starts as an operator starts it: ``python -m dagents.gma``
with ``GMA_*`` variables only, among them ``GMA_ENGINE=http``, the database,
the coordinator role and every table (models, audit chain and anchor, rounds,
enrolments, and updates with their own role and age limit). Every test starts
new processes on empty tables. A restart starts the same command with the
same variables on the same port and waits for ``/health``; under uvicorn the
GMA's lifespan recovers before it serves.

- **Killed while a training round is collecting.** Before the first training
  round's aggregate, its three updates are in the updates table. The GMA is
  killed and started again, the planner keeps the round, and the study
  finishes. ``comparable()`` equals the reference.
- **Killed during a dispatch, with slow LMAs.** Each LMA holds its training
  job (``TOY_TRAIN_HOLD_FILE``, a setting of the toy extension) while the GMA
  waits for the LMAs' answers. The GMA is killed there and started again.
  Its lifespan closes the round with the planner's ``dispatch_interrupted``
  reason before any request reaches it. The LMAs' late uploads get 409, and
  each site logs the job as rejected. The study then runs its next training
  round, under its own new id, as an operator runs a cut round again, and
  finishes. ``comparable()`` equals the reference plus the cut round's
  traces: one rejected entry in each site's job log, three more Guard records
  in each site's audit chain, and the round in the GMA's round list.

The reference is ``test_gma_restart.reference()``: ``run_study`` on the
default GMA in memory, in the test process, with no restart. Each test also
checks the coordinator chain with ``PostgresAuditSink.verify_chain(full=True)``
and its record count.

``TrainHoldTests`` checks the toy extension's hold setting and needs neither
a database nor the planner.
"""

from __future__ import annotations

import contextvars
import os
from pathlib import Path
import signal
import tempfile
import threading
import time
from typing import Any, Callable
import unittest
from unittest import mock

from dagents.common.infrastructure.agent_client import AgentError
from dagents.tests.fixtures.gma_fixtures import ADMIN_TOKEN, SITES, toy_manifest
from dagents.tests.fixtures.toy_extension import (
    TOY_LABEL_FIELD,
    TRAIN_HOLD_VARIABLE,
    ToyLinearKind,
    hold_markers,
    toy_records,
)
from dagents.tests.test_agent_network_flow import (
    STAFF_TOKENS,
    AgentProcesses,
    Outcome,
    comparable,
    run_study,
    start_over_http,
)
from dagents.tests.test_gma_restart import (
    DISPATCH_INTERRUPTED,
    PREFIX,
    DurableTables,
    reference,
    wait_for,
)
from dagents.tests.test_governance import dagentsc_available

#: The extra training round a test cuts by killing the GMA during its dispatch.
CUT_ROUND = f"{PREFIX}-cut"

#: How many Guard decisions an LMA records for a training job: ``before_train``, ``before_read`` and ``before_send``.
GUARD_RECORDS_PER_JOB = 3


def quietly(call: Callable[..., Any], *args: Any) -> Any:
    """Run ``call`` outside the study's call log, so ``comparable()`` sees only the study's own calls."""
    return contextvars.Context().run(call, *args)


class TrainHoldTests(unittest.TestCase):
    """``TOY_TRAIN_HOLD_FILE`` holds a toy training job while its file exists, and only then."""

    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory(prefix="dagents-hold-")
        self.addCleanup(folder.cleanup)
        self.hold = Path(folder.name) / "hold"
        self.records = toy_records("site-a")

    def train(self) -> Any:
        return ToyLinearKind().train(ToyLinearKind().initial_params(), self.records, TOY_LABEL_FIELD)

    def test_without_the_variable_or_the_file_nothing_waits(self) -> None:
        expected = self.train()
        with mock.patch.dict(os.environ, {TRAIN_HOLD_VARIABLE: str(self.hold)}):
            started = time.monotonic()
            self.assertEqual(expected, self.train())
            self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual([], hold_markers(self.hold))

    def test_a_job_waits_while_the_file_exists_and_leaves_a_marker(self) -> None:
        expected = self.train()
        self.hold.touch()
        outcome: list[Any] = []
        with mock.patch.dict(os.environ, {TRAIN_HOLD_VARIABLE: str(self.hold)}):
            job = threading.Thread(target=lambda: outcome.append(self.train()))
            job.start()
            self.addCleanup(job.join, 10)
            self.addCleanup(self.hold.unlink, missing_ok=True)
            self.assertTrue(wait_for(lambda: len(hold_markers(self.hold)) == 1, 10), "the job did not reach the hold")
            time.sleep(0.3)
            self.assertTrue(job.is_alive(), "the job did not wait for the hold file")
            self.assertEqual([], outcome)
            self.hold.unlink()
            job.join(10)
        self.assertFalse(job.is_alive())
        # The hold changes nothing in the result, and the marker is gone.
        self.assertEqual([expected], outcome)
        self.assertEqual([], hold_markers(self.hold))


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class GmaProcessRestartTests(DurableTables, unittest.TestCase):
    """The study finishes with the same results after its GMA process was killed and started again on the same port."""

    def start(self, **lma_settings: str) -> AgentProcesses:
        """The GMA (``python -m dagents.gma``, every table set) and three LMAs, as processes."""
        folder = tempfile.TemporaryDirectory(prefix="dagents-gma-kill-")
        self.addCleanup(folder.cleanup)
        self.log_dir = Path(folder.name)
        settings = {f"GMA_{name.upper()}": str(value) for name, value in self.table_settings().items()}
        agents = start_over_http(self.log_dir, gma_settings=settings, lma_settings=lma_settings, serve_gma=True)
        self.addCleanup(agents.stop)
        return agents

    def kill(self, agents: AgentProcesses) -> Any:
        """Kill the GMA's process group with SIGKILL and check that the GMA died of it."""
        stopped = agents.kill_gma()
        self.assertEqual(-signal.SIGKILL, stopped.returncode)
        return stopped

    def start_again(self, agents: AgentProcesses, stopped: Any) -> None:
        """Start the GMA again with the same command and variables, on the same port, and wait for ``/health``."""
        started = agents.start_gma()
        self.assertNotEqual(stopped.pid, started.pid)
        self.assertIsNone(started.poll())
        self.assertEqual([stopped, started], agents.gma_launch.started)

    def assert_matches(self, outcome: Outcome, expected: dict[str, Any]) -> None:
        actual = comparable(outcome)
        self.assertEqual(sorted(expected), sorted(actual))
        for part in expected:
            with self.subTest(part=part):
                self.assertEqual(expected[part], actual[part])

    def assert_chain(self) -> None:
        """The coordinator's audit chain verifies in full and holds the reference's record count."""
        self.assertEqual((True, reference()["coordinator_audit"][1]), self.audit_chain())

    def round_rows(self, round_id: str, status: str) -> int:
        return self.count_rows(self.role, "rounds", "round_id = %s AND status = %s", (round_id, status))

    def test_a_gma_killed_while_a_training_round_is_collecting(self) -> None:
        agents = self.start()
        round_id = f"{PREFIX}-training-1"
        restarts: list[str] = []

        def before(step: str, outcome: Outcome) -> None:
            if step != f"aggregate {round_id}":
                return
            # The round is collecting, and its three updates are in the table.
            self.assertEqual(1, self.round_rows(round_id, "collecting"))
            self.assertEqual(len(SITES), self.update_rows(round_id))
            self.start_again(agents, self.kill(agents))
            restarts.append(step)
            # The new GMA recovered before it served, and the planner kept the round and its updates.
            self.assertEqual(1, self.round_rows(round_id, "collecting"))
            self.assertEqual(len(SITES), self.update_rows(round_id))

        outcome = run_study(agents.network, PREFIX, before=before)
        self.assertEqual([f"aggregate {round_id}"], restarts)
        self.assertEqual(2, len(agents.gma_launch.started))
        self.assert_matches(outcome, reference())
        self.assert_chain()
        # Every stored update was deleted once its round was aggregated or discarded.
        self.assertEqual(0, self.count_rows(self.update_role, "updates", "site_id <> ''"))

    def test_a_gma_killed_during_a_dispatch_with_slow_lmas(self) -> None:
        hold = self.hold_file()
        agents = self.start(**{TRAIN_HOLD_VARIABLE: str(hold)})
        # Cleanups run last first: let any held job go before the processes stop.
        self.addCleanup(hold.unlink, missing_ok=True)
        rerun = f"{PREFIX}-training-2"
        cut: dict[str, Any] = {}

        def dispatch_cut_round(manifest: Any) -> None:
            try:
                cut["record"] = agents.network.gma(ADMIN_TOKEN).dispatch_round(manifest)
            except AgentError as error:
                cut["error"] = error

        def logged_cut_job(site: str) -> Any:
            entries = agents.network.lma(site, STAFF_TOKENS[site]).job_log(limit=50)
            return next((entry for entry in entries if entry.round_id == CUT_ROUND), None)

        def before(step: str, outcome: Outcome) -> None:
            if step != f"dispatch {rerun}":
                return
            start = outcome.candidates[outcome.training_ids[-1]]
            manifest = toy_manifest(CUT_ROUND, "training", start.candidate_version, start.artifact_digest)
            hold.touch()
            # A thread starts with an empty context, so this dispatch is not in the study's call log.
            thread = threading.Thread(target=dispatch_cut_round, args=(manifest,), name="cut-dispatch")
            thread.start()
            # On a failure, let the held jobs go first, then wait for the thread.
            self.addCleanup(thread.join, 60)
            self.addCleanup(hold.unlink, missing_ok=True)
            # Every LMA holds its job, so the GMA waits for their answers.
            self.assertTrue(
                wait_for(lambda: len(hold_markers(hold)) == len(SITES)), "the LMAs did not all reach the hold"
            )
            self.assertEqual(1, self.round_rows(CUT_ROUND, "dispatched"))

            stopped = self.kill(agents)
            thread.join(30)
            self.assertFalse(thread.is_alive())
            # The killed GMA never answered the dispatch.
            self.assertNotIn("record", cut)
            self.assertIsNone(cut["error"].status)
            self.assertEqual(1, self.round_rows(CUT_ROUND, "dispatched"))

            self.start_again(agents, stopped)
            # Recovery ran in the lifespan, before the GMA served: the round is closed before any request.
            self.assertEqual(1, self.round_rows(CUT_ROUND, "closed"))
            self.assertTrue(self.gate_discarded(CUT_ROUND))

            # The LMAs finish their jobs: each upload gets 409, and each site logs the job as rejected.
            hold.unlink()
            self.assertTrue(
                wait_for(lambda: all(quietly(logged_cut_job, site) for site in SITES)),
                "the sites did not log the cut round's job",
            )
            self.assertEqual([], hold_markers(hold))

        outcome = run_study(agents.network, PREFIX, before=before)
        self.assertIn("error", cut)
        self.assertEqual(2, len(agents.gma_launch.started))

        # The cut round is closed with the planner's reason, holds nothing, and kept no update.
        record = agents.network.gma(ADMIN_TOKEN).get_round(CUT_ROUND)
        self.assertEqual(("closed", DISPATCH_INTERRUPTED), (record.status, record.abandoned_reason))
        self.assertEqual(([], [], None), (record.acceptances, record.results, record.candidate))
        self.assertEqual(0, self.update_rows(CUT_ROUND))
        for site in SITES:
            with self.subTest(site=site):
                entry = logged_cut_job(site)
                self.assertEqual(("training", "rejected", 0, None), (entry.phase, entry.participation, entry.contributed_examples, entry.update_digest))
                self.assertIn("the update channel did not accept the update", entry.reason)

        # Every round of the study, the re-run among them, equals the reference. The cut round adds its traces.
        expected = dict(reference())
        newest_first = list(expected["listed_rounds"])
        place = newest_first.index(f"{PREFIX}-training-1")
        expected["listed_rounds"] = newest_first[:place] + [CUT_ROUND] + newest_first[place:]
        expected["job_logs"] = {
            site: entries[:place] + [("training", "rejected", 0, None, {})] + entries[place:]
            for site, entries in expected["job_logs"].items()
        }
        expected["directory"] = [
            (site, reachable, status, count + GUARD_RECORDS_PER_JOB) for site, reachable, status, count in expected["directory"]
        ]
        self.assert_matches(outcome, expected)
        self.assert_chain()
        self.assertEqual(0, self.count_rows(self.update_role, "updates", "site_id <> ''"))

    def hold_file(self) -> Path:
        """A hold file path in a folder of its own, removed after the test."""
        folder = tempfile.TemporaryDirectory(prefix="dagents-hold-")
        self.addCleanup(folder.cleanup)
        return Path(folder.name) / "hold"


if __name__ == "__main__":
    unittest.main()
