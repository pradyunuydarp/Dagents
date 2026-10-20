"""The GMA's round state stores, and how a GMA is handed its own.

Three stores keep the state of federated rounds:

| Store | Protocol | Memory twin |
|---|---|---|
| Round records | ``RoundRepository`` | ``InMemoryRoundRepository`` |
| Site enrolments | ``SiteRegistry`` | ``InMemorySiteRegistry`` |
| Clipped updates of running training rounds | ``UpdateStore`` | ``InMemoryUpdateStore`` |

Each contract below is a mixin that a store's test class runs. The memory
classes never skip. Recovery after a restart reads ``RoundRepository.unfinished``
and closes rounds with ``RoundRepository.quarantine`` or the controller's
discard, so both methods are part of the round contract.

The last classes check that ``create_app`` and ``build_round_controller`` use
the stores a caller passes, and build new memory stores when it passes none.
They run the real planner and skip without it.
"""

from __future__ import annotations

import threading
from typing import Any, Callable
import unittest

from fastapi.testclient import TestClient

from dagents.common.application.federation import (
    FederatedRoundController,
    InMemoryRoundRepository,
    InMemorySiteRegistry,
    InProcessFederationEngine,
    RoundRepository,
    UnfinishedRound,
)
from dagents.common.domain.errors import NotSelected, RoundConflict
from dagents.common.domain.federation import RoundPlan, RoundRecord, UpdateUpload
from dagents.gma.app_factory import GmaDeps, create_app
from dagents.gma.config import Settings
from dagents.gma.di import build_round_controller
from dagents.gma.infrastructure.update_store import InMemoryUpdateStore, UpdateStore
from dagents.tests.fixtures.gma_fixtures import (
    INITIAL_VERSION,
    SITES as TOY_SITES,
    build_study,
    register_models,
    toy_manifest,
    toy_registry,
)
from dagents.tests.fixtures.toy_extension import ToyLinearKind
from dagents.tests.test_federation import SITES, build_manifest, build_registration
from dagents.tests.test_governance import dagentsc_available


def training_record(
    status: str = "dispatched", phase: str = "training", round_id: str = "round-0042", **overrides: Any
) -> RoundRecord:
    """A round as the controller stores it after planning, without running the planner."""
    manifest = build_manifest(phase=phase, round_id=round_id)
    plan = RoundPlan(
        round_id=manifest.round_id,
        study_id=manifest.study_id,
        phase=manifest.phase,
        selected_sites=list(SITES),
        quorum_met=True,
        required_participants=2,
        aggregation=manifest.aggregation,
        round_digest="fnv1a:0123456789abcdef",
    )
    payload = {"manifest": manifest, "plan": plan, "created_at": 0, "updated_at": 0, "status": status}
    payload.update(overrides)
    return RoundRecord(**payload)


UPDATE = {"bias": 0.01, "w.x1": -0.02, "w.x2": 0.03}


class Clock:
    """A clock a test sets by hand."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


# --------------------------------------------------------------------------- #
# Round records
# --------------------------------------------------------------------------- #


class RoundRepositoryContract:
    """What every round repository does. A subclass implements ``new_repository``."""

    def new_repository(self) -> Any:
        raise NotImplementedError

    def setUp(self) -> None:
        self.repo = self.new_repository()

    def create(self, round_id: str, status: str = "dispatched", phase: str = "training") -> RoundRecord:
        return self.repo.create(training_record(status=status, phase=phase, round_id=round_id))

    def test_it_is_a_round_repository(self) -> None:
        self.assertIsInstance(self.repo, RoundRepository)

    def test_create_refuses_a_round_id_that_is_stored(self) -> None:
        self.create("round-1")
        with self.assertRaises(RoundConflict):
            self.create("round-1", status="collecting")
        self.assertEqual("dispatched", self.repo.get("round-1").status)

    def test_update_of_an_unknown_round_raises_key_error(self) -> None:
        with self.assertRaises(KeyError):
            self.repo.update("missing", lambda record: record)

    def test_a_change_that_raises_leaves_the_record_as_it_was(self) -> None:
        before = self.create("round-1")

        def change(record: RoundRecord) -> RoundRecord:
            raise RoundConflict("refused")

        with self.assertRaises(RoundConflict):
            self.repo.update("round-1", change)
        self.assertEqual(before, self.repo.get("round-1"))

    def test_a_change_that_returns_another_round_is_refused(self) -> None:
        self.create("round-1")
        other = training_record(round_id="round-2")
        with self.assertRaises(ValueError):
            self.repo.update("round-1", lambda record: other)
        self.assertIsNone(self.repo.get("round-2"))

    def test_list_returns_the_newest_rounds_first(self) -> None:
        for round_id in ("round-1", "round-2", "round-3"):
            self.create(round_id)
        self.assertEqual(["round-3", "round-2"], [r.manifest.round_id for r in self.repo.list(limit=2)])

    # --- unfinished() ------------------------------------------------------------

    def test_unfinished_of_an_empty_store_is_empty(self) -> None:
        self.assertEqual([], self.repo.unfinished())

    def test_unfinished_lists_planned_dispatched_and_collecting_rounds_oldest_first(self) -> None:
        """Those are the statuses the planner's recovery rule can close; finished rounds are left out."""
        self.create("round-1", status="planned")
        self.create("round-2", status="dispatched")
        self.create("round-3", status="aggregated")
        self.create("round-4", status="collecting", phase="evaluation")
        self.create("round-5", status="closed")
        self.assertEqual(
            [
                UnfinishedRound(round_id="round-1", status="planned", phase="training", readable=True),
                UnfinishedRound(round_id="round-2", status="dispatched", phase="training", readable=True),
                UnfinishedRound(round_id="round-4", status="collecting", phase="evaluation", readable=True),
            ],
            self.repo.unfinished(),
        )

    def test_unfinished_keeps_the_order_rounds_were_created_in(self) -> None:
        """A later write to an old round does not move it behind newer ones."""
        self.create("round-1")
        self.create("round-2")
        self.repo.update("round-1", lambda record: record.model_copy(update={"status": "collecting", "updated_at": 9}))
        self.assertEqual(["round-1", "round-2"], [item.round_id for item in self.repo.unfinished()])
        self.assertEqual("collecting", self.repo.unfinished()[0].status)

    def test_a_round_that_finishes_leaves_the_unfinished_list(self) -> None:
        self.create("round-1", status="collecting")
        self.create("round-2", status="collecting")
        self.repo.update("round-1", lambda record: record.model_copy(update={"status": "aggregated"}))
        self.assertEqual(["round-2"], [item.round_id for item in self.repo.unfinished()])

    # --- quarantine() ------------------------------------------------------------

    def test_quarantine_closes_an_unfinished_round_with_its_reason(self) -> None:
        for status in ("planned", "dispatched", "collecting"):
            with self.subTest(status=status):
                round_id = f"round-{status}"
                self.create(round_id, status=status)
                self.assertTrue(self.repo.quarantine(round_id, "the stored record could not be read"))
                stored = self.repo.get(round_id)
                self.assertEqual("closed", stored.status)
                self.assertEqual("the stored record could not be read", stored.abandoned_reason)
        self.assertEqual([], self.repo.unfinished())

    def test_quarantine_keeps_the_round_id_reserved(self) -> None:
        """A closed round's id is never used again, so a new round with it is refused."""
        self.create("round-1")
        self.repo.quarantine("round-1", "the stored record could not be read")
        with self.assertRaises(RoundConflict):
            self.create("round-1")
        self.assertEqual("closed", self.repo.get("round-1").status)
        self.assertEqual(["round-1"], [record.manifest.round_id for record in self.repo.list()])

    def test_quarantine_of_a_finished_round_changes_nothing(self) -> None:
        for status in ("aggregated", "closed"):
            with self.subTest(status=status):
                round_id = f"round-{status}"
                before = self.create(round_id, status=status)
                self.assertFalse(self.repo.quarantine(round_id, "the stored record could not be read"))
                self.assertEqual(before, self.repo.get(round_id))

    def test_quarantine_of_an_unknown_round_raises_key_error(self) -> None:
        with self.assertRaises(KeyError):
            self.repo.quarantine("missing", "the stored record could not be read")

    def test_quarantine_needs_a_reason(self) -> None:
        self.create("round-1")
        for reason in ("", "   "):
            with self.subTest(reason=reason), self.assertRaises(ValueError):
                self.repo.quarantine("round-1", reason)
        self.assertEqual("dispatched", self.repo.get("round-1").status)


class InMemoryRoundRepositoryTests(RoundRepositoryContract, unittest.TestCase):
    def new_repository(self) -> InMemoryRoundRepository:
        return InMemoryRoundRepository()

    def test_save_of_a_stored_round_keeps_its_place(self) -> None:
        self.create("round-1")
        self.create("round-2")
        self.repo.save(training_record(status="collecting", round_id="round-1"))
        self.assertEqual(["round-1", "round-2"], [item.round_id for item in self.repo.unfinished()])


# --------------------------------------------------------------------------- #
# Clipped updates
# --------------------------------------------------------------------------- #


class UpdateStoreContract:
    """What every update store does: it checks each upload against the round's plan.

    A subclass implements ``new_store(clock)``; ``clock`` gives the time a put
    records, and ``None`` means the store's own clock.
    """

    def new_store(self, clock: Callable[[], float] | None = None) -> Any:
        raise NotImplementedError

    def setUp(self) -> None:
        self.store = self.new_store()
        self.kind = ToyLinearKind()
        self.record = training_record()

    def upload(self, update: dict | None = None, job_digest: str = "fnv1a:0123456789abcdef") -> UpdateUpload:
        return UpdateUpload(job_digest=job_digest, update=dict(UPDATE if update is None else update))

    def test_it_is_an_update_store(self) -> None:
        self.assertIsInstance(self.store, UpdateStore)
        self.assertIsInstance(self.store.kind, str)
        self.assertTrue(self.store.kind)

    def test_an_upload_is_stored_and_its_digest_comes_from_the_model_kind(self) -> None:
        receipt = self.store.put(self.record, "hospital-a", self.upload(), self.kind)
        self.assertEqual(self.kind.update_digest(UPDATE), receipt.digest)
        stored = self.store.get(self.record.manifest.round_id, "hospital-a")
        self.assertEqual(UPDATE, stored.update)
        self.assertEqual(receipt.digest, stored.digest)
        self.assertEqual("fnv1a:0123456789abcdef", stored.job_digest)
        self.assertEqual([self.record.manifest.round_id], self.store.pending_rounds())

    def test_a_collecting_round_takes_uploads_too(self) -> None:
        self.store.put(training_record(status="collecting"), "hospital-b", self.upload(), self.kind)
        self.assertIsNotNone(self.store.get(self.record.manifest.round_id, "hospital-b"))

    def test_a_site_the_plan_did_not_select_is_refused(self) -> None:
        with self.assertRaises(NotSelected):
            self.store.put(self.record, "intruder", self.upload(), self.kind)
        self.assertEqual([], self.store.pending_rounds())

    def test_a_round_that_is_not_a_training_round_takes_no_update(self) -> None:
        with self.assertRaises(RoundConflict) as raised:
            self.store.put(training_record(phase="evaluation"), "hospital-a", self.upload(), self.kind)
        self.assertIn("is in the evaluation phase; only a training round takes updates", str(raised.exception))

    def test_a_round_that_is_aggregated_closed_or_discarded_takes_no_update(self) -> None:
        records = {
            "aggregated": training_record(status="aggregated"),
            "closed": training_record(status="closed"),
            "discarded": training_record(status="collecting", abandoned_reason="stopped"),
        }
        for name, record in records.items():
            with self.subTest(name):
                with self.assertRaises(RoundConflict):
                    self.store.put(record, "hospital-a", self.upload(), self.kind)
        self.assertEqual([], self.store.pending_rounds())

    def test_a_job_digest_that_is_not_the_plan_round_digest_is_refused(self) -> None:
        with self.assertRaises(RoundConflict) as raised:
            self.store.put(self.record, "hospital-a", self.upload(job_digest="fnv1a:ffffffffffffffff"), self.kind)
        self.assertIn("round digest", str(raised.exception))

    def test_a_second_upload_from_one_site_is_refused_and_the_first_is_kept(self) -> None:
        first = self.store.put(self.record, "hospital-a", self.upload(), self.kind)
        with self.assertRaises(RoundConflict):
            self.store.put(self.record, "hospital-a", self.upload({"bias": 9.0, "w.x1": 9.0, "w.x2": 9.0}), self.kind)
        self.assertEqual(first.digest, self.store.get(self.record.manifest.round_id, "hospital-a").digest)

    def test_an_update_the_model_kind_refuses_is_refused(self) -> None:
        with self.assertRaises(RoundConflict) as raised:
            self.store.put(self.record, "hospital-a", self.upload({"bias": 0.1, "secret": 1.0}), self.kind)
        self.assertIn("unknown names", str(raised.exception))
        self.assertIsNone(self.store.get(self.record.manifest.round_id, "hospital-a"))

    def test_a_round_whose_model_kind_cannot_be_trained_takes_no_update(self) -> None:
        from dagents.tests.fixtures.toy_extension import ToyThresholdKind

        with self.assertRaises(RoundConflict) as raised:
            self.store.put(self.record, "hospital-a", self.upload(), ToyThresholdKind())
        self.assertIn("toy_threshold", str(raised.exception))
        self.assertEqual([], self.store.pending_rounds())

    def test_a_read_returns_a_copy(self) -> None:
        self.store.put(self.record, "hospital-a", self.upload(), self.kind)
        copy = self.store.get(self.record.manifest.round_id, "hospital-a")
        copy.update["bias"] = 100.0
        self.assertEqual(UPDATE, self.store.get(self.record.manifest.round_id, "hospital-a").update)
        everything = self.store.updates(self.record.manifest.round_id)
        self.assertEqual(["hospital-a"], list(everything))

    def test_discard_deletes_the_round_updates_and_refuses_later_uploads(self) -> None:
        """An upload that arrives after the round was discarded must not stay in memory."""
        self.store.put(self.record, "hospital-a", self.upload(), self.kind)
        self.store.put(self.record, "hospital-b", self.upload(), self.kind)
        self.assertEqual(2, self.store.discard(self.record.manifest.round_id))
        self.assertEqual({}, self.store.updates(self.record.manifest.round_id))
        self.assertEqual([], self.store.pending_rounds())
        with self.assertRaises(RoundConflict):
            self.store.put(self.record, "hospital-c", self.upload(), self.kind)
        self.assertEqual(0, self.store.discard(self.record.manifest.round_id))

    def test_a_discard_before_any_upload_refuses_every_later_upload(self) -> None:
        """Recovery discards a closed round's updates even when none are stored; a late upload stays out."""
        self.assertEqual(0, self.store.discard(self.record.manifest.round_id))
        for site_id in SITES:
            with self.subTest(site=site_id), self.assertRaises(RoundConflict) as raised:
                self.store.put(self.record, site_id, self.upload(), self.kind)
            self.assertIn("was discarded", str(raised.exception))
        self.assertEqual({}, self.store.updates(self.record.manifest.round_id))
        self.assertEqual([], self.store.pending_rounds())

    def test_a_discard_refuses_uploads_for_that_round_only(self) -> None:
        other = training_record(round_id="round-0043")
        self.store.discard(self.record.manifest.round_id)
        self.store.put(other, "hospital-a", self.upload(), self.kind)
        self.assertEqual(["round-0043"], self.store.pending_rounds())

    def test_concurrent_uploads_from_one_site_store_one_update(self) -> None:
        barrier = threading.Barrier(8)
        outcomes: list[str] = []

        def put() -> None:
            barrier.wait()
            try:
                self.store.put(self.record, "hospital-a", self.upload(), self.kind)
                outcomes.append("stored")
            except RoundConflict:
                outcomes.append("refused")

        threads = [threading.Thread(target=put) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(["refused"] * 7 + ["stored"], sorted(outcomes))

    # --- pending_ages() ----------------------------------------------------------

    def test_pending_ages_of_an_empty_store_is_empty(self) -> None:
        self.assertEqual({}, self.store.pending_ages(1000.0))

    def test_pending_ages_gives_the_age_of_each_round_oldest_update(self) -> None:
        clock = Clock(100.0)
        store = self.new_store(clock)
        later = training_record(round_id="round-0043")
        store.put(self.record, "hospital-a", self.upload(), self.kind)
        clock.now = 130.0
        store.put(self.record, "hospital-b", self.upload(), self.kind)
        clock.now = 160.0
        store.put(later, "hospital-a", self.upload(), self.kind)
        ages = store.pending_ages(200.0)
        self.assertEqual({"round-0042": 100.0, "round-0043": 40.0}, ages)
        self.assertEqual(["round-0042", "round-0043"], list(ages))
        self.assertEqual(store.pending_rounds(), list(ages))

    def test_pending_ages_leaves_out_a_discarded_round_and_a_refused_upload(self) -> None:
        clock = Clock(100.0)
        store = self.new_store(clock)
        later = training_record(round_id="round-0043")
        store.put(self.record, "hospital-a", self.upload(), self.kind)
        with self.assertRaises(NotSelected):
            store.put(later, "intruder", self.upload(), self.kind)
        self.assertEqual({"round-0042": 50.0}, store.pending_ages(150.0))
        store.discard("round-0042")
        self.assertEqual({}, store.pending_ages(150.0))

    def test_an_age_is_never_negative(self) -> None:
        """A clock that moved back gives an age of 0, not a negative one the planner would refuse."""
        clock = Clock(500.0)
        store = self.new_store(clock)
        store.put(self.record, "hospital-a", self.upload(), self.kind)
        self.assertEqual({"round-0042": 0.0}, store.pending_ages(400.0))


class InMemoryUpdateStoreTests(UpdateStoreContract, unittest.TestCase):
    def new_store(self, clock: Callable[[], float] | None = None) -> InMemoryUpdateStore:
        return InMemoryUpdateStore() if clock is None else InMemoryUpdateStore(clock=clock)

    def test_the_kind_names_memory(self) -> None:
        self.assertEqual("in-memory", self.store.kind)


# --------------------------------------------------------------------------- #
# Handing stores to a controller and to a GMA
# --------------------------------------------------------------------------- #


class RecordingUpdateStore(InMemoryUpdateStore):
    """The memory update store, recording each put and discard."""

    def __init__(self) -> None:
        super().__init__()
        self.puts: list[tuple[str, str]] = []
        self.discards: list[str] = []

    def put(self, record, site_id, upload, kind):  # noqa: ANN001, ANN201 - same signature as the parent
        receipt = super().put(record, site_id, upload, kind)
        self.puts.append((record.manifest.round_id, site_id))
        return receipt

    def discard(self, round_id: str) -> int:
        self.discards.append(round_id)
        return super().discard(round_id)


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class BuildRoundControllerTests(unittest.TestCase):
    """``build_round_controller`` uses the stores it is given, and new memory stores otherwise."""

    def test_the_given_stores_hold_the_enrolments_and_the_rounds(self) -> None:
        sites, rounds = InMemorySiteRegistry(), InMemoryRoundRepository()
        controller = build_round_controller(InProcessFederationEngine(), sites=sites, rounds=rounds)
        for site_id in SITES:
            controller.register_site(build_registration(site_id))
        self.assertEqual(list(SITES), [site.site_id for site in sites.list()])
        record = controller.dispatch_round(build_manifest())
        self.assertEqual(record, rounds.get("round-0042"))

    def test_without_stores_each_controller_has_its_own(self) -> None:
        first, second = build_round_controller(), build_round_controller()
        self.assertIsInstance(first, FederatedRoundController)
        first.register_site(build_registration("hospital-a"))
        self.assertEqual([], second.list_sites())
        self.assertEqual("in-process-simulator", first.engine_name)


class GmaDefaultStoresTests(unittest.TestCase):
    """A GMA built without stores keeps its round state in memory of its own."""

    def test_the_update_store_is_in_memory(self) -> None:
        app = create_app(Settings(), GmaDeps())
        self.assertIsInstance(app.state.gma.updates, InMemoryUpdateStore)

    def test_two_gmas_do_not_share_enrolments_rounds_or_updates(self) -> None:
        first, second = create_app(Settings(), GmaDeps()), create_app(Settings(), GmaDeps())
        self.assertIsNot(first.state.gma.updates, second.state.gma.updates)
        registration = build_registration("hospital-a").model_dump(mode="json")
        with TestClient(first) as client:
            self.assertEqual(200, client.put("/api/v1/federation/sites/hospital-a", json=registration).status_code)
        with TestClient(second) as client:
            self.assertEqual([], client.get("/api/v1/federation/sites").json())


class GmaGivenStoresTests(unittest.TestCase):
    """``GmaDeps.enrolments`` and ``GmaDeps.rounds`` reach the controller of a simulator GMA."""

    def test_an_enrolment_goes_into_the_given_registry(self) -> None:
        enrolments = InMemorySiteRegistry()
        app = create_app(Settings(), GmaDeps(enrolments=enrolments))
        registration = build_registration("hospital-a").model_dump(mode="json")
        with TestClient(app) as client:
            self.assertEqual(200, client.put("/api/v1/federation/sites/hospital-a", json=registration).status_code)
        self.assertEqual(["hospital-a"], [site.site_id for site in enrolments.list()])

    def test_the_given_update_store_is_the_one_the_gma_holds(self) -> None:
        updates = InMemoryUpdateStore()
        app = create_app(Settings(), GmaDeps(updates=updates))
        self.assertIs(updates, app.state.gma.updates)

    @unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
    def test_a_dispatched_round_goes_into_the_given_repository(self) -> None:
        rounds, enrolments = InMemoryRoundRepository(), InMemorySiteRegistry()
        for site_id in SITES:
            enrolments.save(build_registration(site_id))
        app = create_app(Settings(), GmaDeps(rounds=rounds, enrolments=enrolments))
        with TestClient(app) as client:
            answer = client.post("/api/v1/federation/rounds", json=build_manifest().model_dump(mode="json"))
            self.assertEqual(202, answer.status_code, answer.text)
            self.assertEqual("round-0042", client.get("/api/v1/federation/rounds").json()[0]["manifest"]["round_id"])
        self.assertIsNotNone(rounds.get("round-0042"))
        self.assertEqual(rounds.get("round-0042").plan.selected_sites, list(SITES))


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class GmaGivenStoresHttpEngineTests(unittest.TestCase):
    """With the HTTP engine, the GMA's routes, controller and engine all use the stores it was given."""

    def setUp(self) -> None:
        self.rounds = InMemoryRoundRepository()
        self.enrolments = InMemorySiteRegistry()
        self.updates = RecordingUpdateStore()
        self.study = build_study(
            registry=toy_registry(),
            stores={"rounds": self.rounds, "enrolments": self.enrolments, "updates": self.updates},
        )
        self.addCleanup(self.study.close)
        self.gma = self.study.gma()
        self.digests = register_models(self.gma)

    def test_a_training_round_uses_the_given_stores_from_refresh_to_aggregate(self) -> None:
        self.gma.refresh_sites()
        self.assertEqual(list(TOY_SITES), [site.site_id for site in self.enrolments.list()])

        manifest = toy_manifest("round-train-1", "training", INITIAL_VERSION, self.digests[INITIAL_VERSION])
        record = self.gma.dispatch_round(manifest)
        self.assertEqual("collecting", record.status)
        self.assertEqual("collecting", self.rounds.get("round-train-1").status)
        # Each LMA uploaded its update through the GMA's route into the given store.
        self.assertEqual([("round-train-1", site) for site in TOY_SITES], sorted(self.updates.puts))
        self.assertEqual(list(TOY_SITES), sorted(self.updates.updates("round-train-1")))

        candidate = self.gma.aggregate("round-train-1")
        self.assertTrue(candidate.artifact_digest)
        # The engine combined the given store's updates and deleted them.
        self.assertIn("round-train-1", self.updates.discards)
        self.assertEqual([], self.updates.pending_rounds())
        self.assertEqual("aggregated", self.rounds.get("round-train-1").status)
        self.assertIs(self.updates, self.study.state.updates)


class SurvivingRoundRepository(InMemoryRoundRepository):
    """A memory round store that says it keeps its records after a restart, as a database store would."""

    survives_restart = True


class GmaRoundIdsSurviveRestartTests(unittest.TestCase):
    """The HTTP engine learns from the given round store whether round ids stay used after a restart.

    Only then may it return a candidate an earlier attempt registered
    (``test_round_recovery.ReusedRoundIdTests`` shows why).
    """

    def engine_trusts_registered_candidates(self, stores: dict[str, Any]) -> bool:
        study = build_study(registry=toy_registry(), stores=stores)
        self.addCleanup(study.close)
        return study.state.engine._round_ids_survive_restart  # noqa: SLF001 - the wiring under test

    def test_the_default_memory_store_does_not_survive_a_restart(self) -> None:
        self.assertFalse(InMemoryRoundRepository.survives_restart)
        self.assertFalse(self.engine_trusts_registered_candidates({}))
        self.assertFalse(self.engine_trusts_registered_candidates({"rounds": InMemoryRoundRepository()}))

    def test_a_store_that_says_it_survives_a_restart_is_trusted(self) -> None:
        self.assertTrue(self.engine_trusts_registered_candidates({"rounds": SurvivingRoundRepository()}))

    def test_a_store_without_the_attribute_or_with_a_value_that_is_not_true_is_not_trusted(self) -> None:
        class NotABool(InMemoryRoundRepository):
            survives_restart = "yes"  # type: ignore[assignment]

        class WithoutTheAttribute:
            """A memory round store seen through a wrapper that has no ``survives_restart``."""

            def __init__(self) -> None:
                self._inner = InMemoryRoundRepository()

            def __getattr__(self, name: str) -> Any:
                if name == "survives_restart":
                    raise AttributeError(name)
                return getattr(self._inner, name)

        self.assertFalse(self.engine_trusts_registered_candidates({"rounds": NotABool()}))
        self.assertFalse(hasattr(WithoutTheAttribute(), "survives_restart"))
        self.assertFalse(self.engine_trusts_registered_candidates({"rounds": WithoutTheAttribute()}))


if __name__ == "__main__":
    unittest.main()
