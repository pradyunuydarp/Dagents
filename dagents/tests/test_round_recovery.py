"""Crash windows inside one GMA call, and recovery of the rounds a restart leaves unfinished.

Three GMA calls write to more than one store. A store error, or a restart,
between two of those writes must not leave a round that no retry can finish:

- **Aggregate.** The engine registers the candidate in the model store, then
  the controller stores the round as ``aggregated``. The engine deletes the
  round's updates only when it refuses the aggregation; otherwise the
  controller deletes them after the ``aggregated`` write. A retry with the
  updates combines them again, and the model store returns the candidate it
  holds. A retry after a restart that lost the updates returns the registered
  candidate, when the round store keeps round ids across the restart.
- **Evaluate-release.** The model store keeps the decision, then the round
  stores it. A retry sends the model's stored decision to the planner's
  ``release_evaluated`` rule: the same decision for the same round changes
  nothing and is saved on the round; a different one is refused
  (``decision_differs``).
- **Recovery.** :meth:`FederatedRoundController.recover` asks the planner
  (``dagentsc federation round recover``) about each unfinished round and
  closes the rounds it says to close, then deletes the stored updates of rounds
  that are no longer unfinished.

A GMA with the HTTP engine and three fixture LMAs runs on one in-process
network, as in ``test_gma_http_engine.py``. The stores are memory stores
wrapped to fail one chosen write once. Every plan, readiness check, lifecycle
event and recovery verdict comes from the real ``dagentsc``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
import unittest
from typing import Any, Callable
from unittest import mock

from fastapi.testclient import TestClient

from dagents.common.application.federation import (
    FederatedRoundController,
    InMemoryRoundRepository,
    InMemorySiteRegistry,
    InProcessFederationEngine,
    RecoveryReport,
    RoundRecovery,
    UnfinishedRound,
)
from dagents.common.domain.errors import AggregationRefused, RoundConflict, StoreNotSetUpError, StoreUnavailableError
from dagents.common.domain.federation import CandidateModel, ModelVersionRegistration, RoundRecord, UpdateUpload
from dagents.common.infrastructure import dagents_runner
from dagents.common.infrastructure.agent_client import AgentError
from dagents.gma.infrastructure.postgres_models import InMemoryModelStore
from dagents.gma.infrastructure.update_store import InMemoryUpdateStore
from dagents.tests.fixtures.gma_fixtures import (
    ADMIN_TOKEN,
    INITIAL_VERSION,
    SITE_TOKENS,
    THRESHOLD_VERSION,
    build_study,
    register_models,
    toy_manifest,
    toy_registry,
)
from dagents.tests.fixtures.toy_extension import ToyLinearKind
from dagents.tests.test_federation import SITES as SIMULATED_SITES
from dagents.tests.test_federation import build_manifest, build_registration, build_worker
from dagents.tests.test_gma_http_engine import SpyExtension
from dagents.tests.test_gma_release import Pilot, ReleaseToyExtension, round_flow, run_pilot
from dagents.tests.test_governance import dagentsc_available

UPDATE = {"bias": 0.01, "w.x1": -0.02, "w.x2": 0.03}


def became_aggregated(before: RoundRecord, after: RoundRecord) -> bool:
    """The write that stores a round as ``aggregated``."""
    return before.status != "aggregated" and after.status == "aggregated"


def became_decided(before: RoundRecord, after: RoundRecord) -> bool:
    """The write that stores a round's release decision."""
    return before.decision is None and after.decision is not None


class Clock:
    """A clock a test sets by hand."""

    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class ScriptedRoundRepository(InMemoryRoundRepository):
    """A memory round store that can fail one chosen write once, and can make records unreadable.

    - ``fail_next(matches)``: the next ``update`` whose change makes
      ``matches(before, after)`` true raises :class:`StoreUnavailableError`,
      as a store that lost its connection would, and leaves the record as it
      was.
    - ``unreadable``: round ids whose stored record cannot be read. ``get``
      and ``update`` raise :class:`StoreNotSetUpError` for them, and
      ``unfinished`` lists them with ``readable`` false, as a store that keeps
      records outside the process does for a record it cannot parse.
    - ``survives_restart``: true by default, because :meth:`StudyTestCase.restart`
      keeps this store, as a store outside the process would be kept. A test
      that models the default memory store, which a restart empties, passes
      false.
    """

    def __init__(self, *, survives_restart: bool = True) -> None:
        super().__init__()
        self.survives_restart = survives_restart
        self._matches: Callable[[RoundRecord, RoundRecord], bool] | None = None
        self.failures = 0
        self.unreadable: set[str] = set()

    def fail_next(self, matches: Callable[[RoundRecord, RoundRecord], bool]) -> None:
        self._matches = matches

    def _readable(self, round_id: str) -> None:
        if round_id in self.unreadable:
            raise StoreNotSetUpError(f"the stored record of round {round_id} cannot be read")

    def get(self, round_id: str) -> RoundRecord | None:
        self._readable(round_id)
        return super().get(round_id)

    def stored(self, round_id: str) -> RoundRecord | None:
        """The record as it is kept, read past ``unreadable``: what a test checks."""
        return super().get(round_id)

    def update(self, round_id: str, change: Callable[[RoundRecord], RoundRecord]) -> RoundRecord:
        self._readable(round_id)

        def failing(current: RoundRecord) -> RoundRecord:
            changed = change(current)
            matches = self._matches
            if matches is not None and matches(current, changed):
                self._matches = None
                self.failures += 1
                raise StoreUnavailableError(f"the round store lost its connection while it saved round {round_id}")
            return changed

        return super().update(round_id, failing)

    def unfinished(self) -> list[UnfinishedRound]:
        return [
            dataclasses.replace(summary, readable=summary.round_id not in self.unreadable)
            for summary in super().unfinished()
        ]


class FailOnceModelStore(InMemoryModelStore):
    """A memory model store that refuses the next candidate it is asked to add, once."""

    def __init__(self) -> None:
        super().__init__()
        self.fail_next_candidate = False
        self.failures = 0

    def apply(self, changes, *, new=None, decision=None) -> None:  # noqa: ANN001 - same signature as the parent
        if self.fail_next_candidate and new is not None and new.parent_version is not None:
            self.fail_next_candidate = False
            self.failures += 1
            raise StoreUnavailableError("the model store lost its connection")
        super().apply(changes, new=new, decision=decision)


class WatchedUpdateStore(InMemoryUpdateStore):
    """A memory update store that records each discard with the round's stored status at that moment.

    It also counts the reads of a round's updates, and ``fail_next_discard``
    makes the next discard raise before it deletes anything.
    """

    def __init__(self, rounds: ScriptedRoundRepository, clock: Callable[[], float] = time.time) -> None:
        super().__init__(clock=clock)
        self._rounds = rounds
        self.discards: list[tuple[str, str | None]] = []
        self.reads = 0
        self.fail_next_discard = False

    def updates(self, round_id: str):  # noqa: ANN201 - same signature as the parent
        self.reads += 1
        return super().updates(round_id)

    def discard(self, round_id: str) -> int:
        if self.fail_next_discard:
            self.fail_next_discard = False
            raise StoreUnavailableError("the update store lost its connection")
        record = self._rounds.stored(round_id)
        self.discards.append((round_id, record.status if record is not None else None))
        return super().discard(round_id)


@dataclasses.dataclass
class Stores:
    """The stores one GMA is given. A restart keeps all of them but the update store."""

    clock: Callable[[], float] = time.time
    rounds: ScriptedRoundRepository = dataclasses.field(default_factory=ScriptedRoundRepository)
    enrolments: InMemorySiteRegistry = dataclasses.field(default_factory=InMemorySiteRegistry)
    models: FailOnceModelStore = dataclasses.field(default_factory=FailOnceModelStore)
    updates: WatchedUpdateStore | None = None

    def __post_init__(self) -> None:
        self.updates = WatchedUpdateStore(self.rounds, self.clock)

    def deps(self) -> dict[str, Any]:
        return {"rounds": self.rounds, "enrolments": self.enrolments, "model_store": self.models, "updates": self.updates}

    def forget_updates(self) -> None:
        """A new update store in memory, as a restart without durable updates leaves."""
        self.updates = WatchedUpdateStore(self.rounds, self.clock)

    def forget_rounds(self) -> None:
        """New round, enrolment and update stores in memory, as a restart of the default GMA leaves.

        Only the model store is kept, as ``GMA_MODEL_TABLE`` or
        ``GmaDeps.model_store`` keeps it. Round ids are free again.
        """
        self.rounds = ScriptedRoundRepository(survives_restart=False)
        self.enrolments = InMemorySiteRegistry()
        self.forget_updates()


def uninterrupted_candidate(round_id: str) -> CandidateModel:
    """The candidate a training round with this id gives on a GMA that nothing interrupts."""
    study = build_study(registry=toy_registry())
    try:
        gma = study.gma()
        digests = register_models(gma)
        gma.refresh_sites()
        gma.dispatch_round(toy_manifest(round_id, "training", INITIAL_VERSION, digests[INITIAL_VERSION]))
        return gma.aggregate(round_id)
    finally:
        study.close()


class StudyTestCase(unittest.TestCase):
    """A GMA with the HTTP engine over :class:`Stores`, and a restart that keeps the stores."""

    extension: Any = None

    def start(self, stores: Stores) -> None:
        self.stores = stores
        registry = toy_registry(self.extension) if self.extension is not None else toy_registry()
        self.study = build_study(registry=registry, stores=stores.deps())
        self.addCleanup(self.study.close)
        self.gma = self.study.gma()

    def restart(self) -> None:
        """Stop the GMA and start a new one on the same round, enrolment and model stores, without the updates."""
        self.study.close()
        self.stores.forget_updates()
        self.start(self.stores)

    def restart_keeping_only_models(self) -> None:
        """Stop the GMA and start a new one that keeps only the model store, and new LMAs."""
        self.study.close()
        self.stores.forget_rounds()
        self.start(self.stores)

    @property
    def controller(self) -> FederatedRoundController:
        return self.study.state.rounds

    def assert_agent_error(self, call: Callable[[], Any], status: int, code: str) -> AgentError:
        with self.assertRaises(AgentError) as caught:
            call()
        self.assertEqual((status, code), (caught.exception.status, caught.exception.code), caught.exception.detail)
        return caught.exception


# --------------------------------------------------------------------------- #
# Aggregate
# --------------------------------------------------------------------------- #


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class AggregateCrashWindowTests(StudyTestCase):
    """A store error between registering the candidate and storing the aggregated round."""

    def setUp(self) -> None:
        self.extension = SpyExtension()
        self.start(Stores())
        self.digests = register_models(self.gma)
        self.gma.refresh_sites()
        self.round_id = "round-train-1"
        self.version = f"toy-linear+{self.round_id}"
        self.gma.dispatch_round(
            toy_manifest(self.round_id, "training", INITIAL_VERSION, self.digests[INITIAL_VERSION])
        )

    def combined(self) -> int:
        """How many times the model kind combined updates."""
        return len(self.extension.linear.combined)

    def test_an_aggregate_deletes_the_updates_after_it_stores_the_round(self) -> None:
        candidate = self.gma.aggregate(self.round_id)
        self.assertEqual([(self.round_id, "aggregated")], self.stores.updates.discards)
        self.assertEqual([], self.stores.updates.pending_rounds())
        self.assertEqual(candidate, self.gma.get_round(self.round_id).candidate)

    def test_a_failed_aggregated_write_keeps_the_updates_and_a_retry_gives_the_same_candidate(self) -> None:
        self.stores.rounds.fail_next(became_aggregated)
        self.assert_agent_error(lambda: self.gma.aggregate(self.round_id), 503, "store_unavailable")
        self.assertEqual(1, self.stores.rounds.failures)

        # The candidate is registered and its parent superseded, but the round is not aggregated.
        registered = self.gma.get_model_version(self.version)
        self.assertEqual(("candidate", INITIAL_VERSION), (registered.status, registered.parent_version))
        self.assertEqual("superseded", self.gma.get_model_version(INITIAL_VERSION).status)
        stored = self.gma.get_round(self.round_id)
        self.assertEqual(("collecting", None), (stored.status, stored.candidate))
        # The updates are kept, so a retry could combine them again.
        self.assertEqual([self.round_id], self.stores.updates.pending_rounds())
        self.assertEqual([], self.stores.updates.discards)

        candidate = self.gma.aggregate(self.round_id)
        self.assertEqual((self.version, registered.digest), (candidate.candidate_version, candidate.artifact_digest))
        self.assertEqual(uninterrupted_candidate(self.round_id).artifact_digest, candidate.artifact_digest)
        # The updates were kept, so the retry combined them again, and registering the same
        # candidate a second time changed nothing in the model store.
        self.assertEqual(2, self.combined())
        self.assertEqual(registered, self.gma.get_model_version(self.version))
        # The updates were deleted once, after the round was stored as aggregated.
        self.assertEqual([(self.round_id, "aggregated")], self.stores.updates.discards)
        self.assertEqual([], self.stores.updates.pending_rounds())
        saved = self.gma.get_round(self.round_id)
        self.assertEqual(("aggregated", candidate), (saved.status, saved.candidate))

    def test_after_a_restart_without_the_updates_a_retry_returns_the_registered_candidate(self) -> None:
        self.stores.rounds.fail_next(became_aggregated)
        self.assert_agent_error(lambda: self.gma.aggregate(self.round_id), 503, "store_unavailable")
        registered = self.gma.get_model_version(self.version)

        self.restart()
        self.assertEqual([], self.stores.updates.pending_rounds())
        candidate = self.gma.aggregate(self.round_id)
        self.assertEqual((self.version, registered.digest), (candidate.candidate_version, candidate.artifact_digest))
        self.assertEqual(f"registry://models/{self.version}", candidate.artifact_uri)
        self.assertEqual(uninterrupted_candidate(self.round_id).artifact_digest, candidate.artifact_digest)
        # The update store was asked once and held nothing, so the registered version is the candidate.
        self.assertEqual(1, self.stores.updates.reads)
        self.assertEqual(1, self.combined())
        self.assertEqual("aggregated", self.gma.get_round(self.round_id).status)
        self.assertEqual(registered, self.gma.get_model_version(self.version))

    def test_a_candidate_the_model_store_failed_to_add_is_combined_again_on_a_retry(self) -> None:
        self.stores.models.fail_next_candidate = True
        self.assert_agent_error(lambda: self.gma.aggregate(self.round_id), 503, "store_unavailable")
        self.assertEqual(1, self.stores.models.failures)

        # Nothing changed in the model store, and the updates are kept.
        self.assert_agent_error(lambda: self.gma.get_model_version(self.version), 404, "not_found")
        self.assertEqual("candidate", self.gma.get_model_version(INITIAL_VERSION).status)
        self.assertEqual([self.round_id], self.stores.updates.pending_rounds())
        self.assertEqual([], self.stores.updates.discards)
        self.assertEqual("collecting", self.gma.get_round(self.round_id).status)

        candidate = self.gma.aggregate(self.round_id)
        self.assertEqual(self.version, candidate.candidate_version)
        self.assertEqual(uninterrupted_candidate(self.round_id).artifact_digest, candidate.artifact_digest)
        self.assertEqual(2, self.combined())
        self.assertEqual([(self.round_id, "aggregated")], self.stores.updates.discards)
        self.assertEqual("superseded", self.gma.get_model_version(INITIAL_VERSION).status)

    def test_a_refused_aggregation_still_deletes_the_updates(self) -> None:
        # The update store loses site-b's update, so the engine refuses the aggregation.
        self.stores.updates._updates.pop((self.round_id, "site-b"))  # noqa: SLF001 - simulating a lost update
        error = self.assert_agent_error(lambda: self.gma.aggregate(self.round_id), 409, "aggregation_refused")
        self.assertIn("site-b", error.detail)
        self.assertEqual([(self.round_id, "collecting")], self.stores.updates.discards)
        self.assertEqual([], self.stores.updates.pending_rounds())
        stored = self.gma.get_round(self.round_id)
        self.assertEqual(("collecting", None), (stored.status, stored.candidate))

    def change_the_registered_parameters(self) -> None:
        row = self.stores.models._rows[self.version]  # noqa: SLF001 - simulating a changed row
        row["params"] = {**row["params"], "bias": row["params"]["bias"] + 1.0}

    def test_a_registered_candidate_whose_parameters_changed_is_refused_after_a_restart(self) -> None:
        self.stores.rounds.fail_next(became_aggregated)
        self.assert_agent_error(lambda: self.gma.aggregate(self.round_id), 503, "store_unavailable")
        self.change_the_registered_parameters()

        # Without the updates, the engine loads the registered version and checks its digest.
        self.restart()
        error = self.assert_agent_error(lambda: self.gma.aggregate(self.round_id), 409, "aggregation_refused")
        self.assertIn(self.version, error.detail)
        self.assertEqual([(self.round_id, "collecting")], self.stores.updates.discards)
        self.assertEqual([], self.stores.updates.pending_rounds())
        self.assertIsNone(self.gma.get_round(self.round_id).candidate)

    def test_a_registered_candidate_whose_parameters_changed_is_refused_when_the_updates_are_combined_again(
        self,
    ) -> None:
        self.stores.rounds.fail_next(became_aggregated)
        self.assert_agent_error(lambda: self.gma.aggregate(self.round_id), 503, "store_unavailable")
        self.change_the_registered_parameters()

        # The updates are kept, so the retry combines them again and the model store
        # refuses a second version under the name. No retry can register it, so the
        # engine deletes the updates.
        error = self.assert_agent_error(lambda: self.gma.aggregate(self.round_id), 409, "conflict")
        self.assertIn(self.version, error.detail)
        self.assertEqual(2, self.combined())
        self.assertEqual([(self.round_id, "collecting")], self.stores.updates.discards)
        self.assertEqual([], self.stores.updates.pending_rounds())
        self.assertIsNone(self.gma.get_round(self.round_id).candidate)

    def test_a_planner_refusal_keeps_the_updates_until_the_round_is_discarded(self) -> None:
        # site-a fails while it runs its job, so two of the three sites the round needs report.
        self.study.lmas["site-a"].crash_on_execute = True
        self.gma.dispatch_round(
            toy_manifest("round-quorum", "training", INITIAL_VERSION, self.digests[INITIAL_VERSION], minimum_participants=3)
        )
        self.study.lmas["site-a"].crash_on_execute = False
        self.assertIn("round-quorum", self.stores.updates.pending_rounds())

        error = self.assert_agent_error(lambda: self.gma.aggregate("round-quorum"), 409, "aggregation_refused")
        self.assertIn("quorum_not_met", error.detail)
        # The planner refused before the engine ran, so the engine deleted nothing.
        self.assertIn("round-quorum", self.stores.updates.pending_rounds())
        self.assertEqual([], self.stores.updates.discards)
        self.assertEqual("collecting", self.gma.get_round("round-quorum").status)

        self.gma.discard_round("round-quorum", "the round cannot meet its quorum")
        self.assertNotIn("round-quorum", self.stores.updates.pending_rounds())
        self.assertEqual([("round-quorum", "closed")], self.stores.updates.discards)


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class ReusedRoundIdTests(StudyTestCase):
    """A round id used again after a restart that kept only the model store.

    The default GMA keeps rounds in memory, while ``GMA_MODEL_TABLE`` or
    ``GmaDeps.model_store`` can keep model versions across a restart. A new
    round with an earlier round's id must not get that round's candidate,
    which other contributions produced.
    """

    def setUp(self) -> None:
        self.extension = SpyExtension()
        stores = Stores()
        stores.forget_rounds()
        self.start(stores)
        self.digests = register_models(self.gma)
        self.gma.refresh_sites()
        self.round_id = "round-1"
        self.version = f"toy-linear+{self.round_id}"
        self.gma.dispatch_round(self.manifest())
        self.first = self.gma.aggregate(self.round_id)

        self.restart_keeping_only_models()
        self.gma.refresh_sites()
        # The same id again, with other contributions: site-c refuses the job.
        self.study.lmas["site-c"].refuse_with = "site-c is closed for maintenance"
        record = self.gma.dispatch_round(self.manifest())
        self.assertEqual(["site-a", "site-b"], record.readiness.accepted_sites)

    def manifest(self):  # noqa: ANN201 - a RoundManifest
        return toy_manifest(self.round_id, "training", INITIAL_VERSION, self.digests[INITIAL_VERSION])

    def assert_first_candidate_unchanged(self) -> None:
        kept = self.gma.get_model_version(self.version)
        self.assertEqual((self.first.artifact_digest, INITIAL_VERSION), (kept.digest, kept.parent_version))

    def test_the_new_round_does_not_count_the_earlier_candidate_as_registered(self) -> None:
        manifest = self.gma.get_round(self.round_id).manifest
        self.assertFalse(self.study.state.engine.candidate_registered(manifest))

    def test_the_new_round_is_refused_and_its_updates_deleted(self) -> None:
        error = self.assert_agent_error(lambda: self.gma.aggregate(self.round_id), 409, "conflict")
        self.assertIn(self.version, error.detail)
        # The new round's updates were combined, and the model store refused a second model under the name.
        self.assertEqual(2, len(self.extension.linear.combined))
        self.assertEqual([(self.round_id, "collecting")], self.stores.updates.discards)
        self.assertEqual([], self.stores.updates.pending_rounds())
        stored = self.gma.get_round(self.round_id)
        self.assertEqual(("collecting", None), (stored.status, stored.candidate))
        self.assert_first_candidate_unchanged()

    def test_a_retry_after_a_refused_aggregation_does_not_get_the_earlier_candidate(self) -> None:
        # The update store loses site-b's update, so the engine refuses and deletes the rest.
        self.stores.updates._updates.pop((self.round_id, "site-b"))  # noqa: SLF001 - simulating a lost update
        self.assert_agent_error(lambda: self.gma.aggregate(self.round_id), 409, "aggregation_refused")
        self.assertEqual([], self.stores.updates.pending_rounds())

        # No update is stored now, but this round store does not keep ids across a
        # restart, so the registered version cannot be this round's candidate.
        error = self.assert_agent_error(lambda: self.gma.aggregate(self.round_id), 409, "aggregation_refused")
        self.assertIn("site-a", error.detail)
        self.assertIsNone(self.gma.get_round(self.round_id).candidate)
        self.assertEqual(1, len(self.extension.linear.combined))
        self.assert_first_candidate_unchanged()


#: Parameters of a toy linear model that no round in these tests produces.
FOREIGN_PARAMS = {"bias": 5.0, "w.x1": 5.0, "w.x2": 5.0}


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class ForeignCandidateTests(StudyTestCase):
    """A model registered under a round's candidate name before the round, with another parent.

    Only a version whose parent is the round's starting version can be the
    candidate an earlier attempt of the round registered. A registration
    request has no parent field, and a candidate of another starting version
    has another parent.
    """

    def setUp(self) -> None:
        self.extension = SpyExtension()
        self.clock = Clock(1_800_000_000.0)
        self.start(Stores(clock=self.clock))
        self.digests = register_models(self.gma)
        self.gma.refresh_sites()
        self.round_id = "round-x"
        self.version = f"toy-linear+{self.round_id}"

    def register_without_parent(self) -> None:
        stored = self.gma.register_model_version(
            self.version, ModelVersionRegistration(kind="toy_linear", params=dict(FOREIGN_PARAMS))
        )
        self.assertIsNone(stored.parent_version)

    def register_with_another_parent(self) -> None:
        other = self.gma.register_model_version(
            "toy-linear-other",
            ModelVersionRegistration(kind="toy_linear", params={"bias": 0.1, "w.x1": 0.0, "w.x2": 0.0}),
        )
        stored = self.study.state.models.register_candidate(self.version, "toy_linear", FOREIGN_PARAMS, other.version)
        self.assertEqual(other.version, stored.parent_version)

    def dispatch(self) -> None:
        self.gma.dispatch_round(toy_manifest(self.round_id, "training", INITIAL_VERSION, self.digests[INITIAL_VERSION]))

    def check_recovery_closes_the_round(self) -> None:
        self.dispatch()
        manifest = self.gma.get_round(self.round_id).manifest
        self.assertFalse(self.study.state.engine.candidate_registered(manifest))
        report = self.controller.recover(updates_kept=False, now=self.clock.now)
        [item] = [entry for entry in report.rounds if entry.round_id == self.round_id]
        self.assertEqual(("close", "updates_not_kept"), (item.verdict, item.reason))
        self.assertEqual("closed", self.stores.rounds.stored(self.round_id).status)

    def check_aggregate_is_refused(self) -> None:
        foreign = self.gma.get_model_version(self.version)
        self.dispatch()
        error = self.assert_agent_error(lambda: self.gma.aggregate(self.round_id), 409, "conflict")
        self.assertIn(self.version, error.detail)
        # The updates were combined once, the name is taken, and the round has no candidate.
        self.assertEqual(1, len(self.extension.linear.combined))
        self.assertIsNone(self.gma.get_round(self.round_id).candidate)
        self.assertEqual([], self.stores.updates.pending_rounds())
        self.assertEqual(foreign, self.gma.get_model_version(self.version))

    def check_aggregate_after_a_restart_is_refused(self) -> None:
        self.dispatch()
        self.restart()
        # The engine does not take the other model for this round's candidate: an aggregate
        # on the controller finds no update and is refused.
        with self.assertRaises(AggregationRefused) as refused:
            self.controller.aggregate(self.round_id)
        self.assertIn("no update", str(refused.exception))
        self.assertEqual(0, len(self.extension.linear.combined))
        self.assertIsNone(self.gma.get_round(self.round_id).candidate)
        # Through the GMA's route, recovery runs first. The updates were not kept and no
        # candidate is registered for the round, so the planner closes it.
        error = self.assert_agent_error(lambda: self.gma.aggregate(self.round_id), 409, "conflict")
        self.assertIn("not kept across the restart", error.detail)
        self.assertEqual(0, len(self.extension.linear.combined))
        stored = self.gma.get_round(self.round_id)
        self.assertEqual(("closed", None), (stored.status, stored.candidate))

    def test_without_a_parent_it_is_not_registered_for_the_round_and_recovery_closes_the_round(self) -> None:
        self.register_without_parent()
        self.check_recovery_closes_the_round()

    def test_with_another_parent_it_is_not_registered_for_the_round_and_recovery_closes_the_round(self) -> None:
        self.register_with_another_parent()
        self.check_recovery_closes_the_round()

    def test_without_a_parent_an_aggregate_does_not_return_it(self) -> None:
        self.register_without_parent()
        self.check_aggregate_is_refused()

    def test_with_another_parent_an_aggregate_does_not_return_it(self) -> None:
        self.register_with_another_parent()
        self.check_aggregate_is_refused()

    def test_without_a_parent_an_aggregate_after_a_restart_does_not_return_it(self) -> None:
        self.register_without_parent()
        self.check_aggregate_after_a_restart_is_refused()

    def test_with_another_parent_an_aggregate_after_a_restart_does_not_return_it(self) -> None:
        self.register_with_another_parent()
        self.check_aggregate_after_a_restart_is_refused()


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class ControllerAggregateOrderTests(unittest.TestCase):
    """When the controller asks the engine to delete what it holds for an aggregated round."""

    def setUp(self) -> None:
        self.rounds = ScriptedRoundRepository()
        self.engine = RecordingEngine(self.rounds)
        self.controller = FederatedRoundController(engine=self.engine, rounds=self.rounds)
        for site_id in SIMULATED_SITES:
            self.controller.register_site(build_registration(site_id))
        self.manifest = build_manifest()
        digest = self.controller.digest(self.manifest)
        for site_id in SIMULATED_SITES:
            self.engine.register_worker(build_worker(site_id, expected_digest=digest))
        self.round_id = self.manifest.round_id
        self.controller.dispatch_round(self.manifest)

    def test_the_engine_deletes_after_the_aggregated_write(self) -> None:
        candidate = self.controller.aggregate(self.round_id)
        self.assertEqual([(self.round_id, "aggregated")], self.engine.discards)
        self.assertEqual(candidate, self.rounds.get(self.round_id).candidate)

    def test_a_store_error_on_the_aggregated_write_keeps_what_the_engine_holds(self) -> None:
        self.rounds.fail_next(became_aggregated)
        with self.assertRaises(StoreUnavailableError):
            self.controller.aggregate(self.round_id)
        self.assertEqual([], self.engine.discards)
        self.assertEqual("collecting", self.rounds.get(self.round_id).status)

    def test_a_round_that_finished_while_the_engine_combined_is_refused_and_the_engine_deletes(self) -> None:
        def discard_meanwhile() -> None:
            self.controller.discard(self.round_id, "the study stopped")

        self.engine.while_aggregating = discard_meanwhile
        with self.assertRaises(RoundConflict):
            self.controller.aggregate(self.round_id)
        # Once from the discard, once after the refused aggregated write.
        self.assertEqual([(self.round_id, "closed"), (self.round_id, "closed")], self.engine.discards)
        stored = self.rounds.get(self.round_id)
        self.assertEqual(("closed", None), (stored.status, stored.candidate))

    def test_an_engine_that_cannot_delete_after_the_write_still_returns_the_candidate(self) -> None:
        self.engine.fail_discard = True
        with self.assertLogs("dagents.common.application.federation", "WARNING") as logs:
            candidate = self.controller.aggregate(self.round_id)
        self.assertEqual(candidate, self.rounds.get(self.round_id).candidate)
        self.assertEqual("aggregated", self.rounds.get(self.round_id).status)
        self.assertIn(self.round_id, "\n".join(logs.output))


class RecordingEngine(InProcessFederationEngine):
    """The simulator, recording each ``discard`` with the round's stored status at that moment.

    - ``while_aggregating``: called inside ``aggregate``, as another request
      that finishes the round while the engine combines would.
    - ``fail_discard``: ``discard`` raises.
    """

    def __init__(self, rounds: ScriptedRoundRepository) -> None:
        super().__init__()
        self._rounds = rounds
        self.discards: list[tuple[str, str | None]] = []
        self.while_aggregating: Callable[[], None] | None = None
        self.fail_discard = False

    def aggregate(self, manifest, readiness, results):  # noqa: ANN001, ANN201 - same signature as the parent
        if self.while_aggregating is not None:
            self.while_aggregating()
        return super().aggregate(manifest, readiness, results)

    def discard(self, round_id: str) -> None:
        if self.fail_discard:
            raise StoreUnavailableError("the engine's store lost its connection")
        record = self._rounds.stored(round_id)
        self.discards.append((round_id, record.status if record is not None else None))


# --------------------------------------------------------------------------- #
# Evaluate-release
# --------------------------------------------------------------------------- #


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class ReleaseCrashWindowTests(StudyTestCase):
    """A store error between the model's decision and the round's decision."""

    def setUp(self) -> None:
        self.extension = ReleaseToyExtension()
        self.start(Stores())
        self.digests = register_models(self.gma)
        self.gma.refresh_sites()

    def evaluate(self, pilot: Pilot, **overrides: Any):  # noqa: ANN201 - a ReleaseEvaluation
        return self.gma.evaluate_release(pilot.last_training_id, round_flow(pilot, **overrides))

    def retry_after_a_failed_decided_write(self, pilot: Pilot, **overrides: Any):  # noqa: ANN201
        round_id = pilot.last_training_id
        audit_before = self.gma.audit_summary().record_count
        self.stores.rounds.fail_next(became_decided)
        self.assert_agent_error(lambda: self.evaluate(pilot, **overrides), 503, "store_unavailable")
        self.assertEqual(1, self.stores.rounds.failures)

        # The model holds the decision; the round does not.
        after_failure = self.gma.get_model_version(pilot.candidate_version)
        self.assertEqual(round_id, after_failure.release_decision.round_id)
        stored = self.gma.get_round(round_id)
        self.assertEqual(("aggregated", None), (stored.status, stored.decision))
        self.assertEqual(audit_before + 1, self.gma.audit_summary().record_count)

        evaluation = self.evaluate(pilot, **overrides)
        # One decision on the round, the one the planner computed again.
        saved = self.gma.get_round(round_id)
        self.assertEqual(("closed", evaluation.decision), (saved.status, saved.decision))
        # The model keeps the decision and status the first attempt stored: the retry changed nothing there.
        self.assertEqual(after_failure, self.gma.get_model_version(pilot.candidate_version))
        self.assertEqual(after_failure.status, evaluation.candidate_status)
        kept = after_failure.release_decision
        self.assertEqual(
            (kept.action, kept.guard_permitted, kept.gate_results, kept.baseline_version, kept.metrics_source),
            (
                evaluation.decision.action,
                evaluation.guard.permitted,
                evaluation.decision.gate_results,
                evaluation.baseline_version,
                evaluation.metrics_source,
            ),
        )
        # The retry ran the Guard again: one more record, and the chain verifies.
        summary = self.gma.audit_summary()
        self.assertEqual(audit_before + 2, summary.record_count)
        self.assertTrue(summary.chain_intact)
        self.assertTrue(self.study.state.governance.audit_chain_intact())
        # The round is decided now, so a third request is refused before the Guard.
        self.assert_agent_error(lambda: self.evaluate(pilot, **overrides), 409, "conflict")
        self.assertEqual(audit_before + 2, self.gma.audit_summary().record_count)
        return evaluation

    def test_a_release_retried_after_a_failed_round_write_is_saved_once(self) -> None:
        pilot = run_pilot(self.gma, self.digests, "pilot-release")
        evaluation = self.retry_after_a_failed_decided_write(pilot)
        self.assertEqual(("release", True, "candidate"), (evaluation.decision.action, evaluation.guard.permitted, evaluation.candidate_status))
        self.assertEqual(pilot.candidate_version, self.gma.approve(pilot.candidate_version).released.version)

    def test_a_rejection_retried_after_a_failed_round_write_is_saved_once(self) -> None:
        # Without a validation aggregate every gate is not evaluated, and the gates decide reject.
        pilot = run_pilot(self.gma, self.digests, "pilot-reject", validate=False)
        evaluation = self.retry_after_a_failed_decided_write(pilot)
        self.assertEqual(("reject", "rejected"), (evaluation.decision.action, evaluation.candidate_status))

    def test_a_guard_denial_retried_after_a_failed_round_write_is_saved_once(self) -> None:
        pilot = run_pilot(self.gma, self.digests, "pilot-denied")
        evaluation = self.retry_after_a_failed_decided_write(pilot, declared_purpose="marketing")
        self.assertEqual(("release", False, "rejected"), (evaluation.decision.action, evaluation.guard.permitted, evaluation.candidate_status))

    def test_a_retry_whose_decision_differs_is_refused_with_decision_differs(self) -> None:
        first = run_pilot(self.gma, self.digests, "pilot-1")
        second = run_pilot(self.gma, self.digests, "pilot-2")
        # Without a baseline round, the baseline is the version released at the time: the rule.
        self.stores.rounds.fail_next(became_decided)
        self.assert_agent_error(lambda: self.evaluate(first, baseline_from_round=None), 503, "store_unavailable")
        kept = self.gma.get_model_version(first.candidate_version)
        self.assertEqual(THRESHOLD_VERSION, kept.release_decision.baseline_version)

        # Another candidate is released before the retry, so the retry compares with that one.
        self.assertEqual("release", self.evaluate(second).decision.action)
        self.gma.approve(second.candidate_version)
        audit_before = self.gma.audit_summary().record_count

        client = TestClient(self.study.gma_app, headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})
        response = client.post(
            f"/api/v1/federation/rounds/{first.last_training_id}:evaluate-release",
            json=round_flow(first, baseline_from_round=None).model_dump(mode="json"),
        )
        self.assertEqual(409, response.status_code, response.text)
        body = response.json()
        self.assertEqual(("conflict", "decision_differs"), (body["code"], body["refusal"]))
        self.assertIn(first.last_training_id, body["detail"])
        self.assertIn("baseline_version", body["detail"])
        # Nothing changed: the round has no decision and the model keeps the first one.
        self.assertIsNone(self.gma.get_round(first.last_training_id).decision)
        self.assertEqual(kept, self.gma.get_model_version(first.candidate_version))
        # The Guard ran before the planner refused the retry, so its record stays and the chain verifies.
        summary = self.gma.audit_summary()
        self.assertEqual(audit_before + 1, summary.record_count)
        self.assertTrue(summary.chain_intact)


# --------------------------------------------------------------------------- #
# Recovery
# --------------------------------------------------------------------------- #


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class RoundRecoveryTests(StudyTestCase):
    """``FederatedRoundController.recover`` on a GMA with the HTTP engine."""

    def setUp(self) -> None:
        self.clock = Clock(1_800_000_000.0)
        self.start(Stores(clock=self.clock))
        self.digests = register_models(self.gma)
        self.gma.refresh_sites()
        # The GMA recovers once, as its lifespan does when uvicorn starts it, so its
        # first round write does not recover the rounds a test stores directly.
        asyncio.run(self.study.state.ensure_recovered())
        self.assertTrue(self.study.state.recovered)

    def recover(self, **arguments: Any) -> RecoveryReport:
        arguments.setdefault("now", self.clock.now)
        return self.controller.recover(**arguments)

    def training(self, round_id: str) -> RoundRecord:
        return self.gma.dispatch_round(
            toy_manifest(round_id, "training", INITIAL_VERSION, self.digests[INITIAL_VERSION])
        )

    def measuring(self, round_id: str, phase: str) -> RoundRecord:
        return self.gma.dispatch_round(toy_manifest(round_id, phase, THRESHOLD_VERSION, self.digests[THRESHOLD_VERSION]))

    def planned(self, round_id: str) -> RoundRecord:
        """A training round stored as ``planned``, which the GMA itself never stores."""
        manifest = toy_manifest(round_id, "training", INITIAL_VERSION, self.digests[INITIAL_VERSION])
        now = int(self.clock.now)
        return self.stores.rounds.create(
            RoundRecord(
                manifest=manifest,
                plan=self.controller.plan_round(manifest),
                status="planned",
                created_at=now,
                updated_at=now,
            )
        )

    def cut_during_dispatch(self, round_id: str) -> None:
        """Store a dispatched round as a restart during its dispatch leaves it: no results yet, its updates stored."""
        self.stores.rounds.update(
            round_id,
            lambda record: record.model_copy(update={"status": "dispatched", "results": [], "readiness": None}),
        )

    def entry(self, report: RecoveryReport, round_id: str) -> RoundRecovery:
        found = [item for item in report.rounds if item.round_id == round_id]
        self.assertEqual(1, len(found), report)
        return found[0]

    def assert_closed(self, report: RecoveryReport, round_id: str, reason: str) -> RoundRecovery:
        item = self.entry(report, round_id)
        self.assertEqual(("close", reason), (item.verdict, item.reason))
        self.assertTrue(item.message)
        stored = self.stores.rounds.stored(round_id)
        self.assertEqual(("closed", item.message), (stored.status, stored.abandoned_reason))
        self.assertNotIn(round_id, self.stores.updates.pending_rounds())
        return item

    def assert_kept(self, report: RecoveryReport, round_id: str) -> RoundRecovery:
        item = self.entry(report, round_id)
        self.assertEqual(("keep", None), (item.verdict, item.reason))
        self.assertTrue(item.message)
        self.assertIsNone(self.stores.rounds.stored(round_id).abandoned_reason)
        return item

    def test_without_unfinished_rounds_the_planner_is_not_asked(self) -> None:
        with mock.patch.object(dagents_runner, "run_dagentsc", wraps=dagents_runner.run_dagentsc) as planner:
            report = self.recover(updates_kept=False)
        self.assertEqual((), report.rounds)
        self.assertEqual((), report.deleted_updates)
        self.assertEqual(0, planner.call_count)

    def test_planned_and_dispatched_rounds_are_closed_and_their_updates_deleted(self) -> None:
        # site-c refuses the job, so it uploads nothing and could still upload while the round is open.
        self.study.lmas["site-c"].refuse_with = "site-c is closed for maintenance"
        dispatched = self.training("round-dispatched")
        self.study.lmas["site-c"].refuse_with = None
        self.cut_during_dispatch("round-dispatched")
        self.planned("round-planned")
        self.assertEqual(["round-dispatched"], self.stores.updates.pending_rounds())

        report = self.recover(updates_kept=True)
        for round_id, status in (("round-dispatched", "dispatched"), ("round-planned", "planned")):
            with self.subTest(round_id=round_id):
                item = self.assert_closed(report, round_id, "dispatch_interrupted")
                self.assertEqual((status, "training", True), (item.status, item.phase, item.readable))
        self.assertIn(("round-dispatched", "closed"), self.stores.updates.discards)
        self.assertEqual([], self.stores.updates.pending_rounds())

        # A late upload for the closed round is refused.
        error = self.assert_agent_error(
            lambda: self.study.gma(SITE_TOKENS["site-c"]).upload_update(
                "round-dispatched", "site-c", UpdateUpload(job_digest=dispatched.plan.round_digest, update=dict(UPDATE))
            ),
            409,
            "conflict",
        )
        self.assertIn("discarded", error.detail)
        # The operator runs the round again under a new id.
        self.assertEqual("collecting", self.training("round-dispatched-again").status)

    def test_a_collecting_training_round_is_closed_when_its_updates_were_not_kept(self) -> None:
        self.training("round-train")
        self.restart()
        report = self.recover(updates_kept=False)
        self.assert_closed(report, "round-train", "updates_not_kept")
        self.assert_agent_error(lambda: self.gma.aggregate("round-train"), 409, "conflict")

    def test_a_collecting_training_round_is_kept_when_its_updates_are_kept(self) -> None:
        self.training("round-train")
        report = self.recover(updates_kept=True)
        self.assert_kept(report, "round-train")
        self.assertEqual(["round-train"], self.stores.updates.pending_rounds())
        self.gma.aggregate("round-train")
        self.assertEqual("aggregated", self.gma.get_round("round-train").status)

    def test_a_training_round_whose_oldest_update_is_older_than_the_limit_is_closed(self) -> None:
        start = self.clock.now
        self.training("round-old")
        self.clock.now = start + 100.0
        self.training("round-new")
        self.clock.now = start + 120.0

        report = self.recover(updates_kept=True, update_max_age_seconds=60.0)
        old = self.assert_closed(report, "round-old", "updates_expired")
        self.assertIn("120 seconds", old.message)
        self.assertIn("60 seconds", old.message)
        self.assert_kept(report, "round-new")
        self.assertEqual(["round-new"], self.stores.updates.pending_rounds())

    def test_a_training_round_whose_candidate_is_registered_is_kept_and_a_retried_aggregate_finishes_it(self) -> None:
        self.training("round-train")
        self.stores.rounds.fail_next(became_aggregated)
        self.assert_agent_error(lambda: self.gma.aggregate("round-train"), 503, "store_unavailable")
        registered = self.gma.get_model_version("toy-linear+round-train")

        self.restart()
        report = self.recover(updates_kept=False)
        self.assert_kept(report, "round-train")
        self.assertEqual("collecting", self.gma.get_round("round-train").status)

        candidate = self.gma.aggregate("round-train")
        self.assertEqual((registered.version, registered.digest), (candidate.candidate_version, candidate.artifact_digest))
        self.assertEqual("aggregated", self.gma.get_round("round-train").status)

    def test_analytics_and_evaluation_rounds_are_kept(self) -> None:
        self.measuring("round-analytics", "analytics")
        self.measuring("round-evaluation", "evaluation")
        self.restart()
        report = self.recover(updates_kept=False)
        for round_id in ("round-analytics", "round-evaluation"):
            with self.subTest(round_id=round_id):
                self.assert_kept(report, round_id)
                self.assertTrue(self.gma.aggregate(round_id).metrics)

    def test_an_unreadable_round_is_quarantined_without_reading_its_record(self) -> None:
        record = self.training("round-unreadable")
        self.stores.rounds.unreadable.add("round-unreadable")

        report = self.recover(updates_kept=True)
        item = self.assert_closed(report, "round-unreadable", "unreadable_record")
        self.assertEqual(("collecting", "training", False), (item.status, item.phase, item.readable))
        # The update store refuses later uploads for the round.
        with self.assertRaises(RoundConflict) as caught:
            self.stores.updates.put(
                record, "site-a", UpdateUpload(job_digest=record.plan.round_digest, update=dict(UPDATE)), ToyLinearKind()
            )
        self.assertIn("discarded", str(caught.exception))
        # Other rounds still dispatch.
        self.assertEqual("collecting", self.training("round-next").status)

    def test_leftover_updates_of_finished_or_missing_rounds_are_deleted(self) -> None:
        # Aggregated: the update store failed to delete after the aggregated write.
        self.training("round-aggregated")
        self.stores.updates.fail_next_discard = True
        with self.assertLogs("dagents.common.application.federation", "WARNING"):
            self.gma.aggregate("round-aggregated")
        self.assertEqual("aggregated", self.gma.get_round("round-aggregated").status)
        # Closed: a restart between the closing write and the deletion.
        self.training("round-closed")
        self.stores.rounds.update("round-closed", lambda record: record.model_copy(update={"status": "closed"}))
        # Missing: updates for a round the round store does not hold.
        source = self.training("round-open")
        missing = source.model_copy(update={"manifest": source.manifest.model_copy(update={"round_id": "round-missing"})})
        self.stores.updates.put(
            missing, "site-a", UpdateUpload(job_digest=missing.plan.round_digest, update=dict(UPDATE)), ToyLinearKind()
        )
        self.assertEqual(
            ["round-aggregated", "round-closed", "round-missing", "round-open"], self.stores.updates.pending_rounds()
        )

        report = self.recover(updates_kept=True)
        self.assertEqual(("round-aggregated", "round-closed", "round-missing"), report.deleted_updates)
        self.assertEqual(["round-open"], self.stores.updates.pending_rounds())
        self.assertEqual(["round-open"], [item.round_id for item in report.rounds])
        self.assert_kept(report, "round-open")
        self.assertEqual("aggregated", self.stores.rounds.stored("round-aggregated").status)
        self.assertIsNone(self.stores.rounds.stored("round-closed").abandoned_reason)

    def test_recover_asks_the_planner_once_per_unfinished_round_oldest_first(self) -> None:
        self.planned("round-1-planned")
        self.training("round-2-dispatched")
        self.cut_during_dispatch("round-2-dispatched")
        self.training("round-3-training")
        self.measuring("round-4-evaluation", "evaluation")
        self.training("round-5-unreadable")
        self.stores.rounds.unreadable.add("round-5-unreadable")
        self.training("round-6-aggregated")
        self.gma.aggregate("round-6-aggregated")

        with mock.patch.object(dagents_runner, "run_dagentsc", wraps=dagents_runner.run_dagentsc) as planner:
            report = self.recover(updates_kept=False)
        commands = [call.args[0][:3] for call in planner.call_args_list]
        self.assertEqual([["federation", "round", "recover"]] * 5, commands)
        self.assertEqual(
            ["round-1-planned", "round-2-dispatched", "round-3-training", "round-4-evaluation", "round-5-unreadable"],
            [item.round_id for item in report.rounds],
        )
        self.assertEqual(
            [
                ("close", "dispatch_interrupted"),
                ("close", "dispatch_interrupted"),
                ("close", "updates_not_kept"),
                ("keep", None),
                ("close", "unreadable_record"),
            ],
            [(item.verdict, item.reason) for item in report.rounds],
        )
        # A second recovery finds only the round it kept.
        self.assertEqual(["round-4-evaluation"], [item.round_id for item in self.recover(updates_kept=False).rounds])

    def test_recover_needs_a_yes_or_no_for_kept_updates(self) -> None:
        with self.assertRaises(TypeError):
            self.recover(updates_kept="yes")


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class SimulatorRecoveryTests(unittest.TestCase):
    """Recovery with an engine that has none of the optional members: no candidates and no stored updates."""

    def test_a_collecting_training_round_is_closed_when_its_updates_were_not_kept(self) -> None:
        engine = InProcessFederationEngine()
        controller = FederatedRoundController(engine=engine)
        for site_id in SIMULATED_SITES:
            controller.register_site(build_registration(site_id))
        manifest = build_manifest()
        digest = controller.digest(manifest)
        for site_id in SIMULATED_SITES:
            engine.register_worker(build_worker(site_id, expected_digest=digest))
        controller.dispatch_round(manifest)

        report = controller.recover(updates_kept=False, now=time.time())
        self.assertEqual([("close", "updates_not_kept")], [(item.verdict, item.reason) for item in report.rounds])
        self.assertEqual((), report.deleted_updates)
        self.assertEqual("closed", controller.get_round(manifest.round_id).status)


if __name__ == "__main__":
    unittest.main()
