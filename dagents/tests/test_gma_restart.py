"""A GMA that keeps its round state in Postgres, restarted during a study, against PostgreSQL 16 and the real planner.

Every class here needs ``DAGENTS_TEST_DATABASE_URL`` and skips with
``database test skipped`` without it. Each creates a scratch database with the
GMA's tables, created from the package's DDL constants: the coordinator's
tables (models, audit chain and anchor, rounds, site enrolments) owned by
tenant alpha, the coordinator role, and the updates table owned by tenant
beta, the update role. Every test starts with empty tables and new LMAs. The
GMA is built from its settings, as ``GMA_*`` variables would set them, with
``GMA_ENGINE=http``.

**A restart** stops the GMA app (its connection, threads and memory go) and
builds a new app from the same settings and the same database. The LMAs keep
running. The agent network holds a :class:`Restartable` under the GMA's name,
and a restart hands it the new app, as a new GMA process takes over the
address of the one that stopped. Each test checks that the restart happened:
the new app and every store it runs on are new objects, its connection has
not opened yet, and its memory holds no recovery, cached parameters, dispatch
buffers or site summaries.

**The reference** is ``run_study`` from ``test_agent_network_flow.py`` on the
default GMA in memory, with no restart. A run is compared with it through that
module's ``comparable()``: every call, the round results and digests, the
dispatch rows, the gates, the model versions, the job logs, the probes, the
site descriptions, the listed rounds, the late result and the coordinator
audit count, without times or audit ids.

- ``StudyRestartTests``: the study on Postgres without a restart, then with a
  restart before the second training dispatch, before the first training
  aggregate (with ``GMA_UPDATE_TABLE``), before the candidate validation
  aggregate, before the release evaluation and before the approval. Each run
  equals the reference, and ``PostgresAuditSink.verify_chain(full=True)``
  passes with the reference's record count.
- ``CrashWindowRestartTests``: a write that fails inside a call, then a
  restart, then a retry. A failed ``aggregated`` write, with and without
  ``GMA_UPDATE_TABLE``, and a failed ``decided`` write for a release (in the
  study) and for a rejection (in a shorter flow). The retried decision adds
  exactly one Guard record, and the chain verifies.
- ``InMemoryStateRestartTests``: what a restart loses when a store stays in
  memory. Without ``GMA_UPDATE_TABLE`` a training aggregate after a restart
  answers 409, because recovery closed the round. Without
  ``GMA_ENROLMENT_TABLE`` the next round closes for lack of quorum.
- ``RecoveryRestartTests``: a GMA stopped during a dispatch leaves a
  ``dispatched`` round with a stored update. The new GMA closes the round,
  deletes the update, refuses a late upload with 409 and dispatches a new
  round id. Stored updates older than ``GMA_UPDATE_MAX_AGE_SECONDS`` are
  deleted and their round closed, at a restart and before a dispatch.
- ``PostgresUploadThreadTests``: ``UploadThreadTests`` from
  ``test_gma_http_engine.py`` with every table set. More training dispatches
  than the GMA has server threads all finish within the same 5 s bound,
  although every dispatch, upload and recovery now goes through the one kept
  connection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import functools
import threading
import time
from typing import Any, Callable
import unittest

from dagents.common.domain.errors import StoreUnavailableError
from dagents.common.domain.federation import (
    FederatedJob,
    ReleaseEvaluation,
    ReleaseEvaluationRequest,
    RoundRecord,
    UpdateUpload,
)
from dagents.common.infrastructure.agent_client import GMA_AGENT, AgentError, AgentNetwork, CallLog, lma_agent
from dagents.common.infrastructure.postgres_lease import KeptConnection
from dagents.gma.app_factory import GmaDeps, GmaState, create_app
from dagents.gma.config import Settings
from dagents.tests import test_gma_http_engine as http_engine_tests
from dagents.tests.fixtures.gma_fixtures import (
    ADMIN_TOKEN,
    COORDINATOR_TOKENS,
    INITIAL_VERSION,
    SITE_TOKENS,
    SITES,
    THRESHOLD_VERSION,
    FixtureLma,
    build_fixture_lma,
    register_models,
    toy_manifest,
    toy_registry,
)
from dagents.tests.fixtures.toy_extension import TOY_CONDITION_ID
from dagents.tests.test_agent_network_flow import (
    STAFF_TOKENS,
    Outcome,
    build_in_process,
    comparable,
    release_request,
    run_study,
    study_gma_settings,
)
from dagents.tests.test_gma_release import COMMITTEE
from dagents.tests.test_governance import dagentsc_available
from dagents.tests.test_round_recovery import became_aggregated, became_decided

#: The prefix of every round id in the study. The reference uses the same one.
PREFIX = "restart"


def candidate_of(round_id: str) -> str:
    """The candidate version a training round registers."""
    return f"toy-linear+{round_id}"


#: The planner's sentences for the rounds a recovery or an age check closes.
DISPATCH_INTERRUPTED = (
    "The round is dispatched, and a dispatch cannot resume after a restart, so it is closed; run it again under a "
    "new round id."
)
UPDATES_NOT_KEPT = (
    "The round's training updates were not kept across the restart, so it is closed; run it again under a new "
    "round id."
)

#: How old the age tests make a round's stored updates, against the limit in :meth:`DurableTables.table_settings`.
AGED_SECONDS = 7200.0


@functools.lru_cache(maxsize=None)
def reference() -> dict[str, Any]:
    """``comparable()`` of the study on the default GMA in memory, with no restart."""
    network, _, _, _ = build_in_process()
    try:
        return comparable(run_study(network, PREFIX))
    finally:
        network.close()


class DurableTables:
    """A scratch database with every table a GMA can keep its state in, emptied before each test.

    ``table_settings()`` gives the GMA's database settings with every table
    set, or with the updates or enrolments left in memory; ``settings()``
    gives whole GMA settings with every table set.
    """

    #: The coordinator's tables, owned by alpha. ``audit`` and ``audit_anchor``
    #: come with the scratch database.
    COORDINATOR_TABLES = ("models", "audit", "audit_anchor", "rounds", "site_enrolments")

    #: The age limit for stored updates in these tests: 1 hour.
    UPDATE_MAX_AGE_SECONDS = 3600.0

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        from psycopg import sql

        from dagents.gma.infrastructure.postgres_rounds import ENROLMENT_TABLE_DDL, ROUND_TABLE_DDL, UPDATE_TABLE_DDL
        from dagents.tests.test_model_registry import MODELS_TABLE
        from dagents.tests.test_round_store import create_table, started_scratch_database

        cls.sql = sql
        cls.db = started_scratch_database()
        cls.role, cls.update_role = cls.db.alpha, cls.db.beta
        try:
            lease = KeptConnection(cls.db.dsn, name="setup")
            try:
                create_table(lease, cls.role, ROUND_TABLE_DDL, f"{cls.role}.rounds")
                create_table(lease, cls.role, ENROLMENT_TABLE_DDL, f"{cls.role}.site_enrolments")
                create_table(lease, cls.update_role, UPDATE_TABLE_DDL, f"{cls.update_role}.updates")
                with lease.acting_as(cls.role) as conn:
                    conn.execute(sql.SQL(MODELS_TABLE).format(table=sql.Identifier(cls.role, "models")))
            finally:
                lease.close()
        except BaseException:
            cls.db.stop()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.stop()
        super().tearDownClass()

    def setUp(self) -> None:
        self.empty_tables()
        super().setUp()

    def empty_tables(self) -> None:
        """Empty every table, as each test starts."""
        sql = self.sql
        lease = KeptConnection(self.db.dsn, name="truncate")
        try:
            for owner, tables in ((self.role, self.COORDINATOR_TABLES), (self.update_role, ("updates",))):
                with lease.acting_as(owner) as conn:
                    for table in tables:
                        conn.execute(sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(sql.Identifier(owner, table)))
        finally:
            lease.close()

    def table_settings(self, *, updates: bool = True, enrolments: bool = True) -> dict[str, Any]:
        """The database settings: every table, or every table but the updates or the enrolments."""
        role = self.role
        tables: dict[str, Any] = {
            "database_url": self.db.dsn,
            "database_role": role,
            "model_table": f"{role}.models",
            "audit_table": f"{role}.audit",
            "audit_anchor_table": f"{role}.audit_anchor",
            "round_table": f"{role}.rounds",
        }
        if enrolments:
            tables["enrolment_table"] = f"{role}.site_enrolments"
        if updates:
            tables.update(
                update_table=f"{self.update_role}.updates",
                update_role=self.update_role,
                update_max_age_seconds=self.UPDATE_MAX_AGE_SECONDS,
            )
        return tables

    def settings(self, **values: Any) -> Settings:
        return Settings(**{**values, **self.table_settings()})

    def count_rows(self, owner: str, table: str, where: str = "true", params: tuple[Any, ...] = ()) -> int:
        """How many rows ``owner``'s ``table`` holds, read as the owner."""
        sql = self.sql
        lease = KeptConnection(self.db.dsn, name="read")
        try:
            with lease.acting_as(owner) as conn:
                query = sql.SQL("SELECT count(*) AS n FROM {} WHERE " + where).format(sql.Identifier(owner, table))
                return conn.execute(query, params).fetchone()["n"]
        finally:
            lease.close()

    def update_rows(self, round_id: str) -> int:
        """How many updates the updates table holds for the round (its gate row left out)."""
        return self.count_rows(self.update_role, "updates", "round_id = %s AND site_id <> ''", (round_id,))

    def gate_discarded(self, round_id: str) -> bool:
        """Whether the round's gate row in the updates table refuses new updates."""
        where = "round_id = %s AND site_id = '' AND discarded"
        return 1 == self.count_rows(self.update_role, "updates", where, (round_id,))

    def age_updates(self, round_id: str, seconds: float) -> None:
        """Make the round's stored updates ``seconds`` older, as if they had been received that much earlier."""
        sql = self.sql
        lease = KeptConnection(self.db.dsn, name="age")
        try:
            with lease.acting_as(self.update_role) as conn:
                conn.execute(
                    sql.SQL("UPDATE {} SET received_at = received_at - %s WHERE round_id = %s AND site_id <> ''").format(
                        sql.Identifier(self.update_role, "updates")
                    ),
                    (seconds, round_id),
                )
        finally:
            lease.close()

    def audit_chain(self) -> tuple[bool, int]:
        """``verify_chain(full=True)`` and the record count of the coordinator's audit table, from a sink of its own."""
        from dagents.common.infrastructure.postgres_audit import PostgresAuditSink

        lease = KeptConnection(self.db.dsn, name="audit check")
        try:
            sink = PostgresAuditSink(
                lease.lease,
                self.role,
                f"{self.role}.audit",
                settings=lease.settings,
                anchor_table=f"{self.role}.audit_anchor",
            )
            return sink.verify_chain(full=True), sink.count()
        finally:
            lease.close()


class Restartable:
    """An ASGI app that hands each request to the GMA app it holds now.

    The agent network keeps one app per agent name. The test adds this one
    under the GMA's name, and a restart hands it the new app.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        await self.app(scope, receive, send)


@dataclass
class RestartableGma:
    """A GMA built from its settings behind a :class:`Restartable`, and the LMAs on its network."""

    network: AgentNetwork
    settings: Settings
    slot: Restartable
    #: The fixture LMAs, when the network has fixture LMAs rather than real ones.
    lmas: dict[str, FixtureLma] = field(default_factory=dict)
    #: Each restart's stopped app and new app, in order.
    restarts: list[tuple[Any, Any]] = field(default_factory=list)

    @property
    def state(self) -> GmaState:
        """The running GMA's state."""
        return self.slot.app.state.gma

    def client(self) -> Any:
        return self.network.gma(ADMIN_TOKEN)

    def kill(self) -> None:
        """Stop the GMA as a killed process stops: nothing it still runs reaches the database again.

        A dispatch the stopped GMA still runs keeps running in this process,
        but every store operation it starts fails, because its one kept
        connection can no longer connect.
        """
        state = self.state
        lease = state.coordinator_lease
        assert lease is not None

        def stopped(*_: Any, **__: Any) -> Any:
            raise StoreUnavailableError("the GMA process was stopped")

        lease._open = stopped  # type: ignore[method-assign]  # noqa: SLF001 - the stopped process cannot connect
        state.close()

    def restart(self) -> tuple[Any, Any]:
        """Stop the GMA and start a new one from the same settings and database. The LMAs keep running."""
        stopped = self.slot.app
        stopped.state.gma.close()
        started = create_app(self.settings, GmaDeps(registry=toy_registry(), network=self.network))
        self.slot.app = started
        self.restarts.append((stopped, started))
        return stopped, started

    def close(self) -> None:
        self.state.close()
        self.network.close()


def fail_once(store: Any, matches: Callable[[RoundRecord, RoundRecord], bool]) -> list[str]:
    """Make the next ``store.update`` whose change makes ``matches(before, after)`` true fail, once.

    The update raises :class:`StoreUnavailableError` before it commits, as a
    store that lost its connection would, and the record stays as it was. The
    patch is on this store object only; a GMA built after a restart has a new
    store. Returns a list that gets the failed round's id.
    """
    original = store.update
    failed: list[str] = []

    def update(round_id: str, change: Callable[[RoundRecord], RoundRecord]) -> RoundRecord:
        def failing(current: RoundRecord) -> RoundRecord:
            changed = change(current)
            if not failed and matches(current, changed):
                failed.append(round_id)
                raise StoreUnavailableError(f"the round store lost its connection while it saved round {round_id}")
            return changed

        return original(round_id, failing)

    store.update = update
    return failed


class RestartTestCase(DurableTables, unittest.TestCase):
    """Starts a study network with a restartable GMA, restarts it, and compares runs with the reference."""

    def start(self, *, updates: bool = True, enrolments: bool = True) -> RestartableGma:
        """Real LMAs with the toy extension and a GMA from its settings and the scratch tables."""
        settings = study_gma_settings(**self.table_settings(updates=updates, enrolments=enrolments))
        network, _, slot, _ = build_in_process(Restartable, gma_settings=settings)
        gma = RestartableGma(network=network, settings=settings, slot=slot)
        self.addCleanup(gma.close)
        return gma

    def start_with_fixture_lmas(
        self, uploads: dict[str, Callable[[FederatedJob, dict[str, float], Callable[[], str]], str]] | None = None
    ) -> RestartableGma:
        """Fixture LMAs (``gma_fixtures``) and a GMA from its settings with every table.

        ``uploads`` replaces a site's update upload with a function of the
        job, the update and ``send``, which uploads to the GMA as the fixture
        would and returns the receipt's digest.
        """
        settings = self.settings(
            engine="http",
            admin_token=ADMIN_TOKEN,
            site_tokens=dict(SITE_TOKENS),
            lma_tokens=dict(COORDINATOR_TOKENS),
            dispatch_concurrency=3,
            summary_ttl_seconds=0.0,
            allow_source_reads=False,
        )
        registry = toy_registry()
        network = AgentNetwork.in_process(thread_tokens=64, timeout_s=30.0, dispatch_timeout_s=60.0)
        try:
            slot = Restartable(create_app(settings, GmaDeps(registry=registry, network=network)))
            network.add_app(GMA_AGENT, slot)
            gma = RestartableGma(network=network, settings=settings, slot=slot)

            def upload_for(site_id: str) -> Callable[[FederatedJob, dict[str, float]], str] | None:
                hook = (uploads or {}).get(site_id)
                if hook is None:
                    return None

                def upload(job: FederatedJob, update: dict[str, float]) -> str:
                    def send() -> str:
                        body = UpdateUpload(job_digest=job.manifest_digest, update=update)
                        return network.gma(SITE_TOKENS[site_id]).upload_update(job.round_id, site_id, body).digest

                    return hook(job, update, send)

                return upload

            for site_id in SITES:
                lma = build_fixture_lma(site_id, registry=registry, network=network, upload=upload_for(site_id))
                network.add_app(lma_agent(site_id), lma.app)
                gma.lmas[site_id] = lma
        except BaseException:
            network.close()
            raise
        self.addCleanup(gma.close)
        return gma

    def restart(self, gma: RestartableGma, *, kill: bool = False) -> GmaState:
        """Restart the GMA (after stopping it as a killed process stops, with ``kill``) and check that it restarted."""
        if kill:
            gma.kill()
        stopped, started = gma.restart()
        self.assert_restarted(stopped, started)
        return started.state.gma

    def restart_before(
        self, gma: RestartableGma, step: str, then: Callable[[GmaState], None] | None = None
    ) -> Callable[[str, Outcome], None]:
        """A ``run_study`` hook that restarts the GMA just before ``step``, then calls ``then`` with the new state."""

        def before(name: str, outcome: Outcome) -> None:
            if name == step:
                state = self.restart(gma)
                if then is not None:
                    then(state)

        return before

    def assert_restarted(self, stopped: Any, started: Any) -> None:
        """The new GMA shares no object with the stopped one, has not connected, and holds nothing in memory."""
        self.assertIsNot(stopped, started)
        old, new = stopped.state.gma, started.state.gma
        self.assertIsNot(old, new)
        for part in ("rounds", "round_store", "enrolments", "updates", "models", "engine", "governance", "releases"):
            with self.subTest(part=part):
                self.assertIsNot(getattr(old, part), getattr(new, part))
        self.assertIsNot(old.coordinator_lease, new.coordinator_lease)
        # The stopped GMA's connection is closed, and the new one has opened none yet.
        self.assertIsNone(old.coordinator_lease._slot.conn)  # noqa: SLF001
        self.assertEqual(0, new.coordinator_lease.connections_opened)
        # Nothing in memory came across.
        self.assertFalse(new.recovered)
        self.assertEqual({}, new.models._params)  # noqa: SLF001 - the parameter cache
        self.assertEqual(0, len(new.engine._rounds))  # noqa: SLF001 - the dispatch buffers
        self.assertEqual({}, new.directory._cache)  # noqa: SLF001 - the site summaries
        if not new.updates_survive_restart:
            self.assertEqual([], new.updates.pending_rounds())

    def assert_matches_reference(
        self,
        outcome: Outcome,
        *,
        failed_calls: dict[int, tuple[Any, ...]] | None = None,
        audit_records: int | None = None,
    ) -> None:
        """``comparable(outcome)`` equals the reference's, part by part.

        - `failed_calls`: calls this run made that the reference did not, by
          their index in this run's call log: the failed attempts of a crash
          window.
        - `audit_records`: the coordinator chain's record count, when it
          differs from the reference's.
        """
        expected = dict(reference())
        calls = list(expected["calls"])
        for index, call in sorted((failed_calls or {}).items()):
            calls.insert(index, call)
        expected["calls"] = calls
        if audit_records is not None:
            expected["coordinator_audit"] = (True, audit_records)
        actual = comparable(outcome)
        self.assertEqual(sorted(expected), sorted(actual))
        for part in expected:
            with self.subTest(part=part):
                self.assertEqual(expected[part], actual[part])

    def assert_chain(self, records: int | None = None) -> None:
        """The coordinator's audit chain verifies in full and holds ``records`` records (the reference's by default)."""
        expected = reference()["coordinator_audit"][1] if records is None else records
        self.assertEqual((True, expected), self.audit_chain())

    def assert_recovered_on_one_connection(self, gma: RestartableGma) -> None:
        state = gma.state
        self.assertTrue(state.recovered)
        self.assertEqual(1, state.coordinator_lease.connections_opened)


# --------------------------------------------------------------------------- #
# Restart points
# --------------------------------------------------------------------------- #


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class StudyRestartTests(RestartTestCase):
    """A restart at each point of the study gives the same study as a run that nothing interrupts."""

    def run_with_restart(self, step: str, *, updates: bool, then: Callable[[GmaState], None] | None = None) -> None:
        gma = self.start(updates=updates)
        outcome = run_study(gma.network, PREFIX, before=self.restart_before(gma, step, then))
        self.assertEqual(1, len(gma.restarts), f"the GMA did not restart before {step}")
        self.assert_matches_reference(outcome)
        self.assert_chain()
        self.assert_recovered_on_one_connection(gma)
        if updates:
            # Every stored update was deleted once its round was aggregated or discarded.
            self.assertEqual(0, self.count_rows(self.update_role, "updates", "site_id <> ''"))

    def check_restart_point(self, step: str, *, then: Callable[[GmaState], None] | None = None) -> None:
        """The study with a restart before ``step``, without ``GMA_UPDATE_TABLE`` and with every table."""
        for updates in (False, True):
            with self.subTest(update_table=updates):
                self.empty_tables()
                self.run_with_restart(step, updates=updates, then=then)

    def test_the_study_on_postgres_without_a_restart_equals_the_reference(self) -> None:
        for updates in (False, True):
            with self.subTest(update_table=updates):
                self.empty_tables()
                gma = self.start(updates=updates)
                outcome = run_study(gma.network, PREFIX)
                self.assertEqual([], gma.restarts)
                self.assert_matches_reference(outcome)
                self.assert_chain()
                self.assertEqual(1, gma.state.coordinator_lease.connections_opened)
                self.assertEqual(
                    ("postgres", "postgres", "postgres"),
                    (gma.state.round_store.kind, gma.state.enrolments.kind, gma.state.models._store.kind),  # noqa: SLF001
                )
                self.assertEqual("postgres" if updates else "in-memory", gma.state.updates.kind)

    def test_a_restart_before_the_second_training_dispatch(self) -> None:
        def nothing_unfinished(state: GmaState) -> None:
            self.assertEqual([], state.round_store.unfinished())

        self.check_restart_point(f"dispatch {PREFIX}-training-2", then=nothing_unfinished)

    def test_a_restart_before_the_first_training_aggregate_with_the_update_table(self) -> None:
        round_id = f"{PREFIX}-training-1"

        def updates_kept(state: GmaState) -> None:
            # The round is collecting, and its three updates are in the table, not in the new GMA's memory.
            self.assertEqual("collecting", state.round_store.get(round_id).status)
            self.assertEqual(3, self.update_rows(round_id))

        self.run_with_restart(f"aggregate {round_id}", updates=True, then=updates_kept)

    def test_a_restart_before_the_candidate_validation_aggregate(self) -> None:
        round_id = f"{PREFIX}-candidate-validation"

        def collecting(state: GmaState) -> None:
            # An evaluation round aggregates from the results its record holds.
            record = state.round_store.get(round_id)
            self.assertEqual(("collecting", len(SITES)), (record.status, len(record.results)))

        self.check_restart_point(f"aggregate {round_id}", then=collecting)

    def test_a_restart_before_the_release_evaluation(self) -> None:
        def aggregated(state: GmaState) -> None:
            record = state.round_store.get(f"{PREFIX}-training-3")
            self.assertEqual("aggregated", record.status)
            self.assertIsNone(record.decision)
            self.assertEqual(0, self.audit_chain()[1])

        self.check_restart_point("evaluate-release", then=aggregated)

    def test_a_restart_before_the_approval(self) -> None:
        def decided(state: GmaState) -> None:
            candidate = candidate_of(f"{PREFIX}-training-3")
            self.assertEqual("candidate", state.models.get(candidate).status)
            self.assertEqual("release", state.models.get(candidate).release_decision.action)
            self.assertEqual("closed", state.round_store.get(f"{PREFIX}-training-3").status)

        self.check_restart_point("approve", then=decided)


# --------------------------------------------------------------------------- #
# Crash windows across a restart
# --------------------------------------------------------------------------- #


def run_reject_flow(
    network: AgentNetwork, prefix: str, *, before: Callable[[str, ReleaseEvaluationRequest], None] | None = None
) -> ReleaseEvaluation:
    """A baseline round, one training round and a release evaluation without round metrics, which the gates reject.

    ``before`` is called with the training round's id and the request just
    before the release evaluation.
    """
    gma = network.gma(ADMIN_TOKEN)
    digests = register_models(gma)
    gma.refresh_sites()
    evaluation = f"{prefix}-evaluation"
    gma.dispatch_round(toy_manifest(evaluation, "evaluation", THRESHOLD_VERSION, digests[THRESHOLD_VERSION]))
    gma.aggregate(evaluation)
    training = f"{prefix}-training-1"
    gma.dispatch_round(toy_manifest(training, "training", INITIAL_VERSION, digests[INITIAL_VERSION]))
    candidate = gma.aggregate(training)
    gma.discard_round(training, "the round is finished")
    request = ReleaseEvaluationRequest(
        candidate_version=candidate.candidate_version,
        condition_id=TOY_CONDITION_ID,
        metrics_from_round=None,
        baseline_from_round=evaluation,
        change_from_version=INITIAL_VERSION,
        rollback_version=THRESHOLD_VERSION,
        release_requester=COMMITTEE,
        declared_purpose="toy_research",
    )
    if before is not None:
        before(training, request)
    return gma.evaluate_release(training, request)


def evaluation_parts(evaluation: ReleaseEvaluation) -> tuple[Any, ...]:
    """What a release evaluation decided, without times or audit ids."""
    assert evaluation.guard is not None
    return (
        evaluation.decision.model_dump(mode="json"),
        evaluation.guard.permitted,
        evaluation.candidate_status,
        evaluation.metrics_source,
        evaluation.baseline_version,
    )


@functools.lru_cache(maxsize=None)
def reject_reference() -> tuple[Any, ...]:
    """The rejection flow on the default GMA in memory, with no restart."""
    network, _, _, _ = build_in_process()
    try:
        return evaluation_parts(run_reject_flow(network, PREFIX))
    finally:
        network.close()


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class CrashWindowRestartTests(RestartTestCase):
    """A write fails inside a call, the GMA restarts, and the retry finishes the call as an uninterrupted run does."""

    def failed_call(self, call: Callable[[], Any], path: str) -> tuple[int, tuple[Any, ...]]:
        """Make ``call`` inside ``run_study``, expect 503 ``store_unavailable``, and return its place in the call log.

        Returns the index of the failed call in the study's call log and the
        call as ``comparable()`` lists it.
        """
        index = len(CallLog.current().calls)
        with self.assertRaises(AgentError) as caught:
            call()
        self.assertEqual((503, "store_unavailable"), (caught.exception.status, caught.exception.code))
        return index, ("gma", None, "POST", path, 503)

    def check_aggregate_crash_window(self, *, updates: bool) -> None:
        round_id = f"{PREFIX}-training-1"
        version = candidate_of(round_id)
        gma = self.start(updates=updates)
        failed_calls: dict[int, tuple[Any, ...]] = {}

        def before(name: str, outcome: Outcome) -> None:
            if name != f"aggregate {round_id}":
                return
            failed = fail_once(gma.state.round_store, became_aggregated)
            index, call = self.failed_call(
                lambda: gma.client().aggregate(round_id), "/api/v1/federation/rounds/{round_id}:aggregate"
            )
            failed_calls[index] = call
            self.assertEqual([round_id], failed)
            # The candidate is registered and its parent superseded, but the round is not aggregated.
            state = gma.state
            registered = state.models.get(version)
            self.assertEqual(("candidate", INITIAL_VERSION), (registered.status, registered.parent_version))
            self.assertEqual("superseded", state.models.get(INITIAL_VERSION).status)
            stored = state.round_store.get(round_id)
            self.assertEqual(("collecting", None), (stored.status, stored.candidate))
            # The updates are kept, so a retry could combine them again.
            self.assertEqual([round_id], state.updates.pending_rounds())
            self.assertEqual(3 if updates else 0, self.update_rows(round_id))

            new = self.restart(gma)
            if updates:
                self.assertEqual([round_id], new.updates.pending_rounds())
                self.assertEqual(3, self.update_rows(round_id))

        outcome = run_study(gma.network, PREFIX, before=before)
        self.assertEqual(1, len(gma.restarts))
        self.assertEqual(1, len(failed_calls))
        # The retry gave the candidate of an uninterrupted run, so every later round and decision is the same.
        self.assert_matches_reference(outcome, failed_calls=failed_calls)
        self.assert_chain()
        self.assert_recovered_on_one_connection(gma)
        self.assertEqual(0, self.count_rows(self.update_role, "updates", "site_id <> ''"))

    def test_a_failed_aggregated_write_then_a_restart_then_a_retry_with_the_update_table(self) -> None:
        # The updates survive the restart, so the retry combines them again.
        self.check_aggregate_crash_window(updates=True)

    def test_a_failed_aggregated_write_then_a_restart_then_a_retry_without_the_update_table(self) -> None:
        # The updates are lost, so the retry returns the candidate the first attempt registered.
        self.check_aggregate_crash_window(updates=False)

    def test_a_failed_decided_write_then_a_restart_then_a_retry_saves_the_release_once(self) -> None:
        gma = self.start(updates=False)
        failed_calls: dict[int, tuple[Any, ...]] = {}
        kept: dict[str, Any] = {}

        def before(name: str, outcome: Outcome) -> None:
            if name != "evaluate-release":
                return
            round_id = outcome.training_ids[-1]
            request = release_request(PREFIX, outcome)
            failed = fail_once(gma.state.round_store, became_decided)
            index, call = self.failed_call(
                lambda: gma.client().evaluate_release(round_id, request),
                "/api/v1/federation/rounds/{round_id}:evaluate-release",
            )
            failed_calls[index] = call
            self.assertEqual([round_id], failed)
            # The model holds the decision; the round does not. The Guard wrote one record.
            model = gma.state.models.get(request.candidate_version)
            self.assertEqual((round_id, "release"), (model.release_decision.round_id, model.release_decision.action))
            record = gma.state.round_store.get(round_id)
            self.assertEqual(("aggregated", None), (record.status, record.decision))
            self.assertEqual((True, 1), self.audit_chain())
            kept["model"] = model
            self.restart(gma)

        outcome = run_study(gma.network, PREFIX, before=before)
        self.assertEqual(1, len(gma.restarts))
        # The retry ran the Guard again: exactly one more record than the reference, and the chain verifies.
        reference_records = reference()["coordinator_audit"][1]
        self.assert_matches_reference(outcome, failed_calls=failed_calls, audit_records=reference_records + 1)
        self.assert_chain(reference_records + 1)
        self.assert_recovered_on_one_connection(gma)
        # One decision on the round, the one the retry returned; the model kept the decision the first attempt stored.
        last = outcome.training_ids[-1]
        self.assertEqual(outcome.release.decision, outcome.final_rounds[last].decision)
        self.assertEqual(kept["model"].release_decision, gma.state.models.get(kept["model"].version).release_decision)

    def test_a_failed_decided_write_then_a_restart_then_a_retry_saves_the_rejection_once(self) -> None:
        gma = self.start(updates=False)
        kept: dict[str, Any] = {}

        def before(round_id: str, request: ReleaseEvaluationRequest) -> None:
            failed = fail_once(gma.state.round_store, became_decided)
            with self.assertRaises(AgentError) as caught:
                gma.client().evaluate_release(round_id, request)
            self.assertEqual((503, "store_unavailable"), (caught.exception.status, caught.exception.code))
            self.assertEqual([round_id], failed)
            model = gma.state.models.get(request.candidate_version)
            self.assertEqual(("rejected", "reject"), (model.status, model.release_decision.action))
            self.assertIsNone(gma.state.round_store.get(round_id).decision)
            self.assertEqual((True, 1), self.audit_chain())
            kept.update(model=model, round_id=round_id, request=request)
            self.restart(gma)

        evaluation = run_reject_flow(gma.network, PREFIX, before=before)
        self.assertEqual(1, len(gma.restarts))
        self.assertEqual(reject_reference(), evaluation_parts(evaluation))
        self.assertEqual(("reject", "rejected"), (evaluation.decision.action, evaluation.candidate_status))
        # Exactly one more Guard record, and the chain verifies.
        self.assert_chain(2)
        state = gma.state
        self.assertTrue(state.recovered)
        record = state.round_store.get(kept["round_id"])
        self.assertEqual(("closed", evaluation.decision), (record.status, record.decision))
        self.assertEqual(kept["model"], state.models.get(kept["model"].version))
        # The round is decided now, so a third request is refused before the Guard.
        with self.assertRaises(AgentError) as caught:
            gma.client().evaluate_release(kept["round_id"], kept["request"])
        self.assertEqual((409, "conflict"), (caught.exception.status, caught.exception.code))
        self.assert_chain(2)


# --------------------------------------------------------------------------- #
# What a restart loses when a store stays in memory
# --------------------------------------------------------------------------- #


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class InMemoryStateRestartTests(RestartTestCase):
    """The round table alone does not carry a round across a restart: its updates and enrolments must be kept too."""

    def test_with_the_updates_in_memory_a_training_aggregate_after_a_restart_answers_409(self) -> None:
        round_id = f"{PREFIX}-training-1"
        gma = self.start(updates=False)
        with self.assertRaises(AgentError) as caught:
            run_study(gma.network, PREFIX, before=self.restart_before(gma, f"aggregate {round_id}"))
        self.assertEqual(1, len(gma.restarts))
        self.assertEqual((409, "conflict"), (caught.exception.status, caught.exception.code))
        self.assertIn(UPDATES_NOT_KEPT, caught.exception.detail)
        # Recovery closed the round with the planner's sentence, before the aggregate looked at it.
        state = gma.state
        self.assertTrue(state.recovered)
        record = state.round_store.get(round_id)
        self.assertEqual(("closed", UPDATES_NOT_KEPT, None), (record.status, record.abandoned_reason, record.candidate))
        # No candidate was made, and the starting version is still the candidate.
        self.assertIsNone(state.models.get(candidate_of(round_id)))
        self.assertEqual("candidate", state.models.get(INITIAL_VERSION).status)
        self.assertEqual([], state.updates.pending_rounds())

    def test_with_the_enrolments_in_memory_the_next_round_closes_for_lack_of_quorum(self) -> None:
        round_id = f"{PREFIX}-training-2"
        gma = self.start(enrolments=False)
        with self.assertRaises(AgentError) as caught:
            run_study(gma.network, PREFIX, before=self.restart_before(gma, f"dispatch {round_id}"))
        self.assertEqual(1, len(gma.restarts))
        self.assertEqual((409, "conflict"), (caught.exception.status, caught.exception.code))
        client = gma.client()
        self.assertEqual([], client.list_sites())
        record = client.get_round(round_id)
        self.assertEqual("closed", record.status)
        self.assertFalse(record.plan.quorum_met)
        self.assertEqual(([], [], []), (record.plan.selected_sites, record.acceptances, record.results))
        # No LMA was offered the round.
        for site in SITES:
            with self.subTest(site=site):
                entries = gma.network.lma(site, STAFF_TOKENS[site]).job_log(limit=50)
                self.assertNotIn(round_id, [entry.round_id for entry in entries])
        # The operator refreshes the sites, and the round runs again under a new id.
        self.assertEqual(list(SITES), [registration.site_id for registration in client.refresh_sites()])
        start = client.get_model_version(candidate_of(f"{PREFIX}-training-1"))
        again = client.dispatch_round(toy_manifest(f"{round_id}-again", "training", start.version, start.digest))
        self.assertEqual("collecting", again.status)
        self.assertEqual(["completed"] * len(SITES), [result.participation for result in again.results])


# --------------------------------------------------------------------------- #
# Recovery of a stopped dispatch, and the age limit
# --------------------------------------------------------------------------- #


def wait_for(condition: Callable[[], bool], timeout_s: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return condition()


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class RecoveryRestartTests(RestartTestCase):
    """The new GMA closes what the stopped one could not finish, as the planner decides."""

    def test_a_dispatched_round_with_a_stored_update_is_closed_and_a_new_round_id_dispatches(self) -> None:
        round_id, again = "stopped-dispatch", "stopped-dispatch-again"
        uploaded = threading.Event()
        late_waiting = threading.Event()
        late_go = threading.Event()
        release = threading.Event()
        late_errors: list[AgentError] = []

        def site_a(job: FederatedJob, update: dict[str, float], send: Callable[[], str]) -> str:
            # site-a's update reaches the store, then its job is still running when the GMA stops.
            digest = send()
            if job.round_id == round_id:
                uploaded.set()
                release.wait(60)
            return digest

        def site_c(job: FederatedJob, update: dict[str, float], send: Callable[[], str]) -> str:
            # site-c uploads only after the GMA has restarted: a late upload.
            if job.round_id == round_id:
                late_waiting.set()
                late_go.wait(60)
                try:
                    return send()
                except AgentError as error:
                    late_errors.append(error)
                    raise
            return send()

        gma = self.start_with_fixture_lmas({"site-a": site_a, "site-c": site_c})
        client = gma.client()
        digests = register_models(client)
        client.refresh_sites()
        manifest = toy_manifest(round_id, "training", INITIAL_VERSION, digests[INITIAL_VERSION])
        dispatched: dict[str, Any] = {}

        def dispatch() -> None:
            try:
                dispatched["record"] = client.dispatch_round(manifest)
            except AgentError as error:
                dispatched["error"] = error

        thread = threading.Thread(target=dispatch, name="stopped-dispatch")
        thread.start()
        # Cleanups run last first: let the held jobs go, then wait for the stopped GMA's dispatch.
        self.addCleanup(thread.join, 60)
        self.addCleanup(release.set)
        self.addCleanup(late_go.set)
        self.assertTrue(uploaded.wait(30) and late_waiting.wait(30), "the dispatch did not reach the sites")
        self.assertEqual(1, self.count_rows(self.role, "rounds", "round_id = %s AND status = 'dispatched'", (round_id,)))
        self.assertTrue(wait_for(lambda: self.update_rows(round_id) == 2), "site-a and site-b did not upload")

        # The GMA stops during the dispatch, and a new one starts on the same tables.
        state = self.restart(gma, kill=True)
        # A read does not recover, so it still shows the round as the stopped GMA stored it.
        self.assertEqual("dispatched", client.get_round(round_id).status)
        self.assertFalse(state.recovered)

        # site-c's late upload is the new GMA's first round write: it recovers, then refuses the upload.
        late_go.set()
        self.assertTrue(wait_for(lambda: bool(late_errors)), "site-c's late upload got no answer")
        self.assertEqual((409, "conflict"), (late_errors[0].status, late_errors[0].code))
        self.assertTrue(state.recovered)
        closed = state.round_store.get(round_id)
        self.assertEqual(("closed", DISPATCH_INTERRUPTED), (closed.status, closed.abandoned_reason))
        self.assertEqual(([], [], None), (closed.acceptances, closed.results, closed.candidate))
        # The stored updates are deleted, and the round's gate refuses any other upload.
        self.assertEqual(0, self.update_rows(round_id))
        self.assertTrue(self.gate_discarded(round_id))
        self.assertEqual([], state.updates.pending_rounds())

        # A new round id dispatches and aggregates on the new GMA.
        record = client.dispatch_round(toy_manifest(again, "training", INITIAL_VERSION, digests[INITIAL_VERSION]))
        self.assertEqual("collecting", record.status)
        self.assertEqual(["completed"] * len(SITES), [result.participation for result in record.results])
        self.assertEqual(3, self.update_rows(again))
        candidate = client.aggregate(again)
        self.assertEqual(candidate_of(again), candidate.candidate_version)
        self.assertEqual(0, self.update_rows(again))

        # The stopped GMA's dispatch ends when site-a's job ends. It cannot write any more, so the
        # round stays as the new GMA's recovery left it.
        release.set()
        thread.join(60)
        self.assertFalse(thread.is_alive())
        self.assertEqual((503, "store_unavailable"), (dispatched["error"].status, dispatched["error"].code))
        self.assertEqual(closed, state.round_store.get(round_id))
        self.assertEqual(1, state.coordinator_lease.connections_opened)

    def training_round(self, gma: RestartableGma, round_id: str) -> RoundRecord:
        client = gma.client()
        start = client.get_model_version(INITIAL_VERSION)
        record = client.dispatch_round(toy_manifest(round_id, "training", start.version, start.digest))
        self.assertEqual("collecting", record.status)
        self.assertEqual(3, self.update_rows(round_id))
        return record

    def evaluation_round(self, gma: RestartableGma, round_id: str) -> RoundRecord:
        client = gma.client()
        rule = client.get_model_version(THRESHOLD_VERSION)
        return client.dispatch_round(toy_manifest(round_id, "evaluation", rule.version, rule.digest))

    def assert_expired(self, gma: RestartableGma, round_id: str) -> None:
        """The round is closed with the planner's age sentence, its updates are deleted, and it cannot be aggregated."""
        record = gma.state.round_store.get(round_id)
        self.assertEqual("closed", record.status)
        self.assertIn("more than the limit of 3600 seconds", record.abandoned_reason)
        self.assertTrue(record.abandoned_reason.startswith("The round's oldest stored update is "))
        self.assertEqual(0, self.update_rows(round_id))
        self.assertTrue(self.gate_discarded(round_id))
        with self.assertRaises(AgentError) as caught:
            gma.client().aggregate(round_id)
        self.assertEqual((409, "conflict"), (caught.exception.status, caught.exception.code))

    def test_updates_older_than_the_limit_are_deleted_and_their_round_closed_before_the_next_dispatch(self) -> None:
        gma = self.start()
        client = gma.client()
        register_models(client)
        client.refresh_sites()
        self.training_round(gma, "aging")
        state = self.restart(gma)
        # The first dispatch after the restart recovers: the updates are kept and young, so the round stays open.
        self.evaluation_round(gma, "first")
        self.assertTrue(state.recovered)
        self.assertEqual("collecting", state.round_store.get("aging").status)
        self.assertEqual(3, self.update_rows("aging"))

        # The updates grow older than the limit, and the next dispatch closes the round first.
        self.age_updates("aging", AGED_SECONDS)
        record = self.evaluation_round(gma, "second")
        self.assertEqual("collecting", record.status)
        self.assert_expired(gma, "aging")

    def test_updates_older_than_the_limit_at_a_restart_are_deleted_and_their_round_closed(self) -> None:
        gma = self.start()
        client = gma.client()
        register_models(client)
        client.refresh_sites()
        self.training_round(gma, "aging")
        young = self.training_round(gma, "young")
        self.age_updates("aging", AGED_SECONDS)
        state = self.restart(gma)
        # The first round write recovers: the planner closes the round with old updates and keeps the other.
        client.aggregate(young.manifest.round_id)
        self.assertTrue(state.recovered)
        self.assert_expired(gma, "aging")
        self.assertEqual("aggregated", state.round_store.get("young").status)


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class PostgresUploadThreadTests(DurableTables, http_engine_tests.UploadThreadTests):
    """An upload never waits for the thread tokens the dispatch handlers hold, with every table in Postgres."""

    def gma_settings(self, **values: Any) -> Settings:
        return self.settings(**values)

    def test_more_training_dispatches_than_thread_tokens_all_complete(self) -> None:
        super().test_more_training_dispatches_than_thread_tokens_all_complete()
        state = self.gma_app.state.gma
        self.assertEqual(
            ("postgres", "postgres", "postgres", "postgres"),
            (state.round_store.kind, state.enrolments.kind, state.updates.kind, state.models._store.kind),  # noqa: SLF001
        )
        self.assertTrue(state.recovered)
        # Every round, enrolment and update went into the tables, through the one kept connection.
        self.assertEqual(1, state.coordinator_lease.connections_opened)
        self.assertEqual(self.ROUNDS, self.count_rows(self.role, "rounds", "status = 'collecting'"))
        self.assertEqual(3, self.count_rows(self.role, "site_enrolments"))
        self.assertEqual(self.ROUNDS * 3, self.count_rows(self.update_role, "updates", "site_id <> ''"))


if __name__ == "__main__":
    unittest.main()
