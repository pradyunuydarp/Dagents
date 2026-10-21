"""A planner that fails inside a running agent, and an LMA process that dies, over loopback HTTP.

The GMA and three LMAs run as separate uvicorn processes, started from their
settings by ``start_over_http`` (``test_agent_network_flow.py``), and the test
calls them only through their HTTP APIs. Every agent uses the real
``dagentsc``, except where a case gives one agent a stand-in planner:

(a) Site A's planner hangs on every call. Its LMA stops each call after
    ``DAGENTSC_TIMEOUT_SECONDS=1``, refuses the round because it cannot
    compute the round digest, and the round goes on with the other two sites.
    The site's Guard probe is denied, ``/health`` still answers, and no
    stand-in process is left running.
(b) Site A's planner works for every command except ``governance restrict``,
    where it hangs. The digest check passes, the ``before_train`` Guard
    denies the job, and the site's audit chain holds the denial, naming the
    stand-in by its SHA-256.
(c) The GMA registers its models with a planner, then loses it
    (``DAGENTSC_BIN`` names a file that has been removed). Planning and
    dispatch answer ``503`` ``planner_unavailable``, no LMA receives a job,
    and ``/health`` answers ``200``.
(e) Site C's LMA process is killed with SIGKILL before a dispatch. Its
    dispatch row has no status code, the round still meets quorum with the
    other two sites, and the GMA's directory reports the site unreachable.

A stand-in writes its own process id to a file and then replaces itself with
``sleep`` (``exec``), so the id in the file is the process the runner starts,
stops and reaps. A process in state ``Z`` (a zombie) counts as gone: the
container's first process may not reap orphans.

A gap in the LMA: when it refuses a job because its planner could not compute
the round digest, it writes a job-log entry and no audit record, so the audit
chain does not show that refusal. Case (a) does not assert either way about
the audit record.

These tests skip without ``dagentsc``. ``AgentProcessStopTests`` checks how
the helper stops the agent processes and needs neither the planner nor a
database.
"""

from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from dagents import __version__
from dagents.common.application.ethical_guard import PLANNER_UNREACHABLE
from dagents.common.domain.site import ProbeRequest
from dagents.common.infrastructure.agent_client import AgentError
from dagents.tests.fixtures.gma_fixtures import (
    ADMIN_TOKEN,
    SITES,
    THRESHOLD_VERSION,
    register_models,
    toy_manifest,
)
from dagents.tests.fixtures.toy_extension import TOY_CONDITION_ID, TOY_LABEL_FIELD, toy_records
from dagents.tests.test_agent_network_flow import (
    PROBE_REQUESTER,
    STAFF_TOKENS,
    AgentLaunch,
    AgentProcesses,
    start_over_http,
)
from dagents.tests.test_governance import dagentsc_available, dagentsc_binary

#: The words of the reason a site gives when its planner cannot compute a round digest.
DIGEST_REFUSAL = "could not compute the round digest with its planner"

#: How long one planner call may run in a site with a stand-in planner.
PLANNER_TIMEOUT_SECONDS = "1"

#: The bound on a probe while the planner hangs: five planner timeouts.
PROBE_BOUND_SECONDS = 5.0

#: The bound on ``/health`` while nothing else is queued.
HEALTH_BOUND_SECONDS = 1.0

FAILING_SITE, OTHER_SITES = SITES[0], SITES[1:]


def probe_request() -> ProbeRequest:
    """The probe a site's staff send: five rows of every field, for the study's coordinator."""
    return ProbeRequest(
        boundary="before_read",
        granularity="row",
        requested_fields=["x1", "x2", "group", TOY_LABEL_FIELD],
        record_limit=5,
        requester=PROBE_REQUESTER,
        declared_purpose=TOY_CONDITION_ID,
    )


def write_stand_in(folder: Path, name: str, *, hang_only_on_restrict: bool) -> tuple[Path, Path]:
    """Write a stand-in planner and return its path and the file it writes its process ids to.

    Without ``hang_only_on_restrict`` it hangs on every command. With it, it
    hands every command but ``governance restrict`` to the real planner, so
    the version, the digest and every other call get real answers.
    """
    pids = folder / f"{name}.pids"
    script = folder / name
    hang = [f'echo $$ >> "{pids}"', "exec sleep 30"]
    if hang_only_on_restrict:
        lines = [
            'if [ "$1" = governance ] && [ "$2" = restrict ]; then',
            *(f"  {line}" for line in hang),
            "fi",
            f'exec "{dagentsc_binary()}" "$@"',
        ]
    else:
        lines = hang
    script.write_text("\n".join(["#!/bin/sh", *lines, ""]), encoding="utf-8")
    script.chmod(0o755)
    return script, pids


def stand_in_ids(pids: Path) -> list[int]:
    """The process ids a stand-in wrote, one per call."""
    if not pids.exists():
        return []
    return [int(line) for line in pids.read_text(encoding="utf-8").split()]


def running(pid: int) -> bool:
    """Whether the stand-in with this id still runs.

    Gone means: no such process, a zombie (``Z``) or a dead one (``X``), or a
    process that is no longer ``sleep``, because its id was given to another
    process after the stand-in was reaped.
    """
    proc = Path("/proc")
    if not proc.is_dir():
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    try:
        state = (proc / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()[0]
        command = (proc / str(pid) / "cmdline").read_bytes()
    except (FileNotFoundError, ProcessLookupError, IndexError):
        return False
    return state not in ("Z", "X") and command.startswith(b"sleep")


def kill_stand_ins(pids: Path) -> None:
    """Stop every stand-in that still runs, with its process group.

    The runner starts the planner in a session of its own, so stopping an LMA
    does not stop it. A working runner stops it at the timeout; this cleanup
    runs when a test fails because one did not.
    """
    for pid in stand_in_ids(pids):
        if running(pid):
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class PlannerFailureOverHttpTests(unittest.TestCase):
    """The Guard and a federated round while a planner fails, with each agent in its own process."""

    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory(prefix="dagents-failures-")
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def stand_in(self, name: str, *, hang_only_on_restrict: bool) -> tuple[Path, Path]:
        """Write a stand-in planner (:func:`write_stand_in`) whose leftover processes are stopped after the test."""
        script, pids = write_stand_in(self.folder, name, hang_only_on_restrict=hang_only_on_restrict)
        self.addCleanup(kill_stand_ins, pids)
        return script, pids

    def start(self, **settings: object) -> AgentProcesses:
        agents = start_over_http(self.folder, **settings)  # type: ignore[arg-type]
        self.addCleanup(agents.stop)
        return agents

    def assert_no_stand_in_left(self, pids: Path, calls: int) -> None:
        """At least ``calls`` stand-ins started, and none of them still runs after five seconds."""
        started = stand_in_ids(pids)
        self.assertGreaterEqual(len(started), calls, "the stand-in planner was not called")
        deadline = time.monotonic() + PROBE_BOUND_SECONDS
        left = [pid for pid in started if running(pid)]
        while left and time.monotonic() < deadline:
            time.sleep(0.1)
            left = [pid for pid in left if running(pid)]
        self.assertEqual([], left, "a stand-in planner is still running after its call ended")

    # --- (a) ------------------------------------------------------------------------

    def test_a_site_whose_planner_hangs_refuses_the_job_and_the_round_goes_on(self) -> None:
        stand_in, pids = self.stand_in("dagentsc-hangs", hang_only_on_restrict=False)
        agents = self.start(
            site_settings={FAILING_SITE: {"DAGENTSC_BIN": str(stand_in), "DAGENTSC_TIMEOUT_SECONDS": PLANNER_TIMEOUT_SECONDS}}
        )
        gma = agents.network.gma(ADMIN_TOKEN)
        registered = register_models(gma)
        self.assertEqual([e.site_id for e in gma.refresh_sites()], list(SITES))

        round_id = "hanging-planner-evaluation"
        record = gma.dispatch_round(
            toy_manifest(round_id, "evaluation", THRESHOLD_VERSION, registered[THRESHOLD_VERSION], minimum_participants=2)
        )

        acceptance = {a.site_id: a for a in record.acceptances}
        self.assertFalse(acceptance[FAILING_SITE].accepted)
        self.assertFalse(acceptance[FAILING_SITE].verified_digest)
        self.assertIn(DIGEST_REFUSAL, acceptance[FAILING_SITE].reason)
        self.assertEqual(
            {r.site_id: r.participation for r in record.results},
            {FAILING_SITE: "rejected", **{site: "completed" for site in OTHER_SITES}},
        )
        rows = {call.site_id: (call.step, call.status_code, call.outcome) for call in record.dispatch}
        self.assertEqual(rows[FAILING_SITE], ("run", 200, "refused"))

        readiness = gma.readiness(round_id)
        self.assertTrue(readiness.aggregation_permitted)
        self.assertEqual(sorted(readiness.site_weights), list(OTHER_SITES))
        candidate = gma.aggregate(round_id)
        self.assertEqual(candidate.contributed_examples, sum(len(toy_records(site)) for site in OTHER_SITES))

        staff = agents.network.lma(FAILING_SITE, STAFF_TOKENS[FAILING_SITE])
        [entry] = staff.job_log(limit=10)
        self.assertEqual((entry.round_id, entry.participation), (round_id, "rejected"))
        self.assertIn(DIGEST_REFUSAL, entry.reason or "")

        started = time.monotonic()
        probe = staff.probe(probe_request())
        self.assertLess(time.monotonic() - started, PROBE_BOUND_SECONDS)
        self.assertFalse(probe.permitted)
        self.assertEqual(probe.message, PLANNER_UNREACHABLE)
        self.assertEqual(probe.released_count, 0)

        started = time.monotonic()
        self.assertEqual(staff.health()["status"], "ok")
        self.assertLess(time.monotonic() - started, HEALTH_BOUND_SECONDS)

        # One call for the round digest and one for the probe.
        self.assert_no_stand_in_left(pids, calls=2)

    # --- (b) ------------------------------------------------------------------------

    def test_a_guard_planner_that_hangs_denies_training_and_the_audit_names_the_stand_in(self) -> None:
        stand_in, pids = self.stand_in("dagentsc-restrict-hangs", hang_only_on_restrict=True)
        agents = self.start(
            site_settings={FAILING_SITE: {"DAGENTSC_BIN": str(stand_in), "DAGENTSC_TIMEOUT_SECONDS": PLANNER_TIMEOUT_SECONDS}}
        )
        gma = agents.network.gma(ADMIN_TOKEN)
        registered = register_models(gma)
        gma.refresh_sites()

        round_id = "hanging-guard-evaluation"
        record = gma.dispatch_round(
            toy_manifest(round_id, "evaluation", THRESHOLD_VERSION, registered[THRESHOLD_VERSION], minimum_participants=2)
        )
        acceptance = {a.site_id: a for a in record.acceptances}
        # The digest came from the real planner, so the site checked it; the Guard then failed closed.
        self.assertTrue(acceptance[FAILING_SITE].verified_digest)
        self.assertFalse(acceptance[FAILING_SITE].accepted)
        self.assertEqual(acceptance[FAILING_SITE].reason, PLANNER_UNREACHABLE)
        self.assertEqual(
            {r.site_id: r.participation for r in record.results},
            {FAILING_SITE: "rejected", **{site: "completed" for site in OTHER_SITES}},
        )

        staff = agents.network.lma(FAILING_SITE, STAFF_TOKENS[FAILING_SITE])
        audit = staff.request("GET", "/api/v1/governance/audit", params={"limit": 50}).body
        self.assertTrue(audit["chain_intact"])
        [denial] = [r for r in audit["records"] if r["boundary"] == "before_train" and r["correlation_id"] == round_id]
        self.assertEqual((denial["decision"], denial["permitted"]), ("deny", False))
        self.assertEqual(denial["violations"], [PLANNER_UNREACHABLE])
        self.assertEqual(denial["planner_sha256"], hashlib.sha256(stand_in.read_bytes()).hexdigest())
        self.assertEqual(denial["planner_version"], __version__)
        self.assertNotEqual(denial["planner_sha256"], hashlib.sha256(Path(dagentsc_binary()).read_bytes()).hexdigest())

        self.assert_no_stand_in_left(pids, calls=1)

    # --- (c) ------------------------------------------------------------------------

    def test_a_gma_without_a_planner_answers_503_and_dispatches_nothing(self) -> None:
        # The GMA registers its models while it has a planner, so a dispatch
        # that went ahead without a plan would find the model and reach the
        # LMAs. Then the planner file is removed, and DAGENTSC_BIN names a
        # missing file.
        planner = self.folder / "dagentsc-gma"
        planner.write_text(f'#!/bin/sh\nexec "{dagentsc_binary()}" "$@"\n', encoding="utf-8")
        planner.chmod(0o755)
        agents = self.start(gma_settings={"DAGENTSC_BIN": str(planner)})
        gma = agents.network.gma(ADMIN_TOKEN)
        registered = register_models(gma)
        self.assertEqual([e.site_id for e in gma.refresh_sites()], list(SITES))
        planner.unlink()
        self.assertEqual(gma.health()["status"], "ok")

        manifest = toy_manifest("no-planner-evaluation", "evaluation", THRESHOLD_VERSION, registered[THRESHOLD_VERSION])
        for name, call in (("plan", lambda: gma.plan_round(manifest)), ("dispatch", lambda: gma.dispatch_round(manifest))):
            with self.subTest(call=name):
                with self.assertRaises(AgentError) as caught:
                    call()
                self.assertEqual((caught.exception.status, caught.exception.code), (503, "planner_unavailable"))

        self.assertEqual(gma.list_rounds(limit=10), [])
        for site in SITES:
            with self.subTest(site=site):
                self.assertEqual(agents.network.lma(site, STAFF_TOKENS[site]).job_log(limit=10), [])
        self.assertEqual(gma.health()["status"], "ok")

    # --- (e) ------------------------------------------------------------------------

    def test_a_killed_lma_is_unreachable_and_the_round_meets_quorum_without_it(self) -> None:
        killed, alive = SITES[-1], SITES[:-1]
        agents = self.start()
        gma = agents.network.gma(ADMIN_TOKEN)
        registered = register_models(gma)
        self.assertEqual([e.site_id for e in gma.refresh_sites()], list(SITES))

        agents.kill_lma(killed)
        round_id = "killed-site-evaluation"
        record = gma.dispatch_round(
            toy_manifest(round_id, "evaluation", THRESHOLD_VERSION, registered[THRESHOLD_VERSION], minimum_participants=2)
        )
        # The plan still selects the site: its enrolment is stored.
        self.assertEqual(record.plan.selected_sites, list(SITES))
        self.assertTrue(record.plan.quorum_met)
        rows = {call.site_id: (call.step, call.status_code, call.outcome) for call in record.dispatch}
        self.assertEqual(rows[killed], ("run", None, "failed"))
        self.assertEqual(
            {r.site_id: r.participation for r in record.results},
            {killed: "failed", **{site: "completed" for site in alive}},
        )
        readiness = gma.readiness(round_id)
        self.assertTrue(readiness.aggregation_permitted)
        self.assertEqual(sorted(readiness.site_weights), list(alive))
        gma.aggregate(round_id)

        directory = {entry.site_id: entry.reachable for entry in gma.site_summaries()}
        self.assertEqual(directory, {killed: False, **{site: True for site in alive}})
        # A refresh keeps only the sites that answered.
        self.assertEqual([e.site_id for e in gma.refresh_sites()], list(alive))


class AgentProcessStopTests(unittest.TestCase):
    """``AgentProcesses.stop`` signals only the process groups whose leader still runs.

    A test that kills an LMA and starts it again leaves processes that have
    exited and been reaped. The system can give their ids to new process
    groups, such as another job's planner call, so ``stop`` must not signal
    them. Real ``sleep`` and ``true`` processes stand in for the agents.
    """

    def start(self, command: list[str]) -> subprocess.Popen:
        process = subprocess.Popen(command, start_new_session=True)

        def stop() -> None:
            if process.poll() is None:
                process.kill()
            process.wait()

        self.addCleanup(stop)
        return process

    def test_stop_signals_no_group_whose_leader_was_reaped(self) -> None:
        running = [self.start(["sleep", "30"]) for _ in range(2)]
        reaped = [self.start(["true"]) for _ in range(2)]
        for process in reaped:
            process.wait()
        # site-b's LMA was killed and started again: its launch lists both processes.
        launch = AgentLaunch(
            name="lma",
            command=["sleep", "30"],
            environment={},
            port=0,
            log_dir=Path("."),
            takes_socket=True,
            started=[reaped[1], running[1]],
        )
        agents = AgentProcesses(
            network=mock.Mock(),
            processes=[running[0], reaped[0], running[1]],
            logs=[],
            sites=("site-a", "site-b"),
            launches={"site-b": launch},
        )
        ours = {process.pid: process for process in running}
        signalled: list[tuple[int, int]] = []
        send = os.killpg

        def record(pid: int, number: int) -> None:
            signalled.append((pid, number))
            # Signal only a process this test started and has not reaped, even when stop() is wrong.
            if pid in ours and ours[pid].returncode is None:
                send(pid, number)

        with mock.patch("os.killpg", side_effect=record):
            agents.stop()
        self.assertEqual(sorted(signalled), sorted((pid, signal.SIGTERM) for pid in ours))
        self.assertEqual([process.returncode for process in running], [-signal.SIGTERM] * 2)


if __name__ == "__main__":
    unittest.main()
