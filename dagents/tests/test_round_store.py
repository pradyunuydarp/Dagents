"""The GMA's round state stores, and how a GMA is handed its own.

Three stores keep the state of federated rounds:

| Store | Protocol | Memory twin | Postgres twin |
|---|---|---|---|
| Round records | ``RoundRepository`` | ``InMemoryRoundRepository`` | ``PostgresRoundRepository`` |
| Site enrolments | ``SiteRegistry`` | ``InMemorySiteRegistry`` | ``PostgresEnrolmentStore`` |
| Clipped updates of running training rounds | ``UpdateStore`` | ``InMemoryUpdateStore`` | ``PostgresUpdateStore`` |

Each contract below is a mixin that a store's test class runs. The memory
classes never skip. Recovery after a restart reads ``RoundRepository.unfinished``
and closes rounds with ``RoundRepository.quarantine`` or the controller's
discard, so both methods are part of the round contract.

The Postgres classes run the same contracts on the scratch database from
``test_postgres_roles.py``, with each table created from its DDL constant in
``dagents/gma/infrastructure/postgres_rounds.py``. Tenant alpha plays the
coordinator role and owns the rounds and enrolments tables; tenant beta plays
the update role and owns the updates table. They skip with ``database test
skipped`` when ``DAGENTS_TEST_DATABASE_URL`` is not set. Further Postgres
classes run the controller tests of ``test_federation.py`` on Postgres stores;
they also need the planner.

``ReadmeDdlTests`` checks that the tables the GMA README tells operators to
create are the ones these tests create. It needs no database.

The last classes check that ``create_app`` and ``build_round_controller`` use
the stores a caller passes, and build new memory stores when it passes none,
and that a GMA answers 422 to a request that holds NaN or an infinite number
with either round store (``NonFiniteRequestContract``; its Postgres class
needs the database too). They run the real planner and skip without it.
"""

from __future__ import annotations

import itertools
import json
import math
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable
import unittest

from fastapi.testclient import TestClient

try:
    import psycopg
    from psycopg import sql
    from psycopg.types.json import Json
except ModuleNotFoundError:  # pragma: no cover - the agents' requirements install it
    psycopg = None

from dagents.common.application.federation import (
    FederatedRoundController,
    InMemoryRoundRepository,
    InMemorySiteRegistry,
    InProcessFederationEngine,
    RoundRepository,
    UnfinishedRound,
)
from dagents.common.domain.errors import InvalidRoundRecord, NotSelected, RoundConflict, StoreNotSetUpError
from dagents.common.domain.federation import (
    AggregationMethod,
    AggregationReadiness,
    CandidateModel,
    ContributionRejection,
    DispatchCall,
    GateResult,
    JobAcceptance,
    ReleaseDecision,
    RoundPlan,
    RoundRecord,
    SiteExclusion,
    SiteResult,
    UpdateReceipt,
    UpdateUpload,
)
from dagents.common.infrastructure.postgres_lease import KeptConnection
from dagents.common.infrastructure.postgres_roles import RoleScopeError
from dagents.gma.app_factory import GmaDeps, create_app
from dagents.gma.config import Settings
from dagents.gma.di import build_round_controller
from dagents.gma.infrastructure.postgres_models import PARAMS_TO_JSON_SQL
from dagents.gma.infrastructure.postgres_rounds import (
    ENROLMENT_TABLE_DDL,
    RECORD_VERSION,
    ROUND_TABLE_DDL,
    UPDATE_TABLE_DDL,
    PostgresEnrolmentStore,
    PostgresRoundRepository,
    PostgresUpdateStore,
    create_table_sql,
)
from dagents.gma.infrastructure.update_store import InMemoryUpdateStore, UpdateStore
from dagents.tests import test_federation as federation_tests
from dagents.tests.fixtures.gma_fixtures import (
    INITIAL_VERSION,
    SITES as TOY_SITES,
    build_study,
    register_models,
    toy_manifest,
    toy_registry,
)
from dagents.tests.fixtures.toy_extension import ToyLinearKind
from dagents.tests.test_federation import SITES, build_manifest, build_registration, build_worker
from dagents.tests.test_governance import dagentsc_available
from dagents.tests.test_postgres_roles import ScratchDatabase, admin_dsn_or_skip

#: The logger the Postgres stores warn on when they leave out a row they cannot read.
STORE_LOGGER = "dagents.gma.infrastructure.postgres_rounds"


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


#: A whole number above 2**53, the largest a double holds exactly.
BIG_COUNT = 2**53 + 1


def full_record(round_id: str = "round-full") -> RoundRecord:
    """A round with every field set, holding numbers a ``jsonb`` column changes.

    ``jsonb`` returns ``-0.0`` as ``0.0``, and ``1e16`` and ``1e300`` as whole
    numbers. A store must return each one as it was sent.
    """
    manifest = build_manifest(
        round_id=round_id,
        aggregation=AggregationMethod(kind="fedprox", mu=-0.0),
        stop_conditions=["quorum_not_met", "unsafe_metric"],
    )
    plan = RoundPlan(
        round_id=round_id,
        study_id=manifest.study_id,
        phase=manifest.phase,
        selected_sites=["hospital-a", "hospital-b"],
        excluded_sites=[SiteExclusion(site_id="hospital-c", reason="site is not enrolled")],
        quorum_met=True,
        required_participants=2,
        aggregation=manifest.aggregation,
        round_digest="fnv1a:0123456789abcdef",
    )
    results = [
        SiteResult(
            round_id=round_id,
            site_id="hospital-a",
            job_digest=plan.round_digest,
            participation="completed",
            code_verified=True,
            privacy_checks_passed=True,
            contributed_examples=BIG_COUNT,
            update_norm=-0.0,
            update_digest="fnv1a:aaaaaaaaaaaaaaaa",
            metrics={"auc": 1e16, "brier": -0.0, "huge": 1e300, "tiny": 5e-324},
            local_evidence_pointer="site-a/evidence/1",
        ),
        SiteResult(
            round_id=round_id,
            site_id="hospital-b",
            job_digest=plan.round_digest,
            participation="rejected",
            refusal_reason="the guard withheld the outbound model update",
        ),
    ]
    candidate = CandidateModel(
        round_id=round_id,
        candidate_version=f"toy-linear+{round_id}",
        parent_version=manifest.model_version,
        aggregation_method="fedprox",
        contributing_sites=["hospital-a"],
        site_weights={"hospital-a": 1e16},
        contributed_examples=BIG_COUNT,
        metrics={"auc": -0.0, "loss": 1e16},
        artifact_uri="registry://toy-linear",
        artifact_digest="sha256:feed",
        produced_at=1_700_000_000,
    )
    return RoundRecord(
        manifest=manifest,
        plan=plan,
        results=results,
        readiness=AggregationReadiness(
            round_id=round_id,
            accepted_sites=["hospital-a"],
            rejected_contributions=[ContributionRejection(site_id="hospital-b", reason="site reported a failed run")],
            accepted_examples=BIG_COUNT,
            aggregation_permitted=True,
            site_weights={"hospital-a": 1.0, "hospital-b": -0.0},
        ),
        decision=ReleaseDecision(
            round_id=round_id,
            candidate_version=candidate.candidate_version,
            gate_results=[GateResult(gate_id="auc", outcome="failed", observed=-0.0, threshold=1e16, detail="below")],
            action="reject",
            blocking_failures=["auc"],
            rollback_version="toy-linear-init",
        ),
        created_at=1_700_000_000,
        updated_at=1_700_000_001,
        status="closed",
        acceptances=[
            JobAcceptance(
                round_id=round_id,
                site_id="hospital-a",
                accepted=True,
                reason="verified",
                verified_digest=True,
                feature_contract_version=manifest.feature_contract_version,
            ),
            JobAcceptance(round_id=round_id, site_id="hospital-b", accepted=False, reason="the site is busy"),
        ],
        candidate=candidate,
        dispatch=[
            DispatchCall(site_id="hospital-a", step="run", status_code=200, ms=-0.0, outcome="completed"),
            DispatchCall(site_id="hospital-b", step="run", status_code=None, ms=1e16, outcome="failed"),
        ],
        abandoned_reason="kept for the test",
    )


def record_json(record: RoundRecord) -> str:
    """The record as JSON text. Unlike ``==``, it tells ``-0.0`` from ``0.0`` and ``1e16`` from ``10**16``."""
    return json.dumps(record.model_dump(mode="json"), sort_keys=True)


#: JSON nested 10,000 deep. PostgreSQL's ``json`` type accepts it, and Python's
#: decoder raises ``RecursionError`` on it on Python 3.11 to 3.13. (3,000 deep
#: decodes on Python 3.12 and later, so a test with it would pass there
#: without the code it checks.)
DEEP_JSON = "[" * 10_000 + "]" * 10_000

#: A whole number of 5,000 digits. PostgreSQL's ``json`` type accepts it, and
#: Python's decoder refuses more than 4,300 digits with ``ValueError``.
LONG_INTEGER = "1" * 5_000


def raw_json(text: str) -> Any:
    """``text`` as a ``Json`` parameter, sent as it is, so a test can store JSON that Python would not write."""
    return Json(None, dumps=lambda _value: text)


def nan_records(record: RoundRecord) -> dict[str, RoundRecord]:
    """``record`` changed in three ways no round store can keep: NaN in a metric, NaN as a norm, an infinite metric.

    ``model_copy`` skips validation, as an engine or an extension that builds
    a result that way does, so each record reaches the store.
    """
    result = with_result(record, "hospital-a").results[-1]
    candidate = full_record(record.manifest.round_id).candidate
    return {
        "NaN metric": record.model_copy(update={"results": [result.model_copy(update={"metrics": {"auc": math.nan}})]}),
        "NaN norm": record.model_copy(update={"results": [result.model_copy(update={"update_norm": math.nan})]}),
        "infinite metric": record.model_copy(
            update={"candidate": candidate.model_copy(update={"metrics": {"auc": math.inf}})}
        ),
    }


def undecodable_records(round_id: str) -> dict[str, str]:
    """Record texts PostgreSQL's ``json`` type stores but this version cannot read, for the round ``round_id``.

    Each is otherwise a valid record of that round, so only the named value
    makes it unreadable.
    """
    plain = json.dumps(training_record(status="collecting", round_id=round_id).model_dump(mode="json"))
    scored = training_record(status="collecting", round_id=round_id)
    scored = scored.model_copy(
        update={
            "results": [with_result(scored, "hospital-a").results[-1].model_copy(update={"metrics": {"auc": 0.5}})],
            "candidate": full_record(round_id).candidate.model_copy(update={"metrics": {"auc": 0.25}}),
        }
    )
    scored_text = json.dumps(scored.model_dump(mode="json"))
    texts = {
        "nested 10,000 deep": plain[:-1] + ', "extra": ' + DEEP_JSON + "}",
        "with a whole number of 5,000 digits": plain.replace('"created_at": 0', '"created_at": ' + LONG_INTEGER),
        "with 1e400 in a result's metric": scored_text.replace('"auc": 0.5', '"auc": 1e400'),
        "with 1e400 in the candidate's metric": scored_text.replace('"auc": 0.25', '"auc": 1e400'),
    }
    for name, text in texts.items():
        assert text != plain and text != scored_text, name
    return texts


def with_result(record: RoundRecord, site_id: str) -> RoundRecord:
    """The record with one more completed result, from ``site_id``."""
    result = SiteResult(
        round_id=record.manifest.round_id,
        site_id=site_id,
        job_digest=record.plan.round_digest,
        participation="completed",
    )
    return record.model_copy(update={"results": [*record.results, result]})


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

    def test_list_follows_python_slice_rules(self) -> None:
        """0 returns none, a negative limit leaves out that many of the oldest rounds, and no limit is too large."""
        round_ids = [f"round-{number}" for number in range(1, 6)]
        for round_id in round_ids:
            self.create(round_id)
        newest_first = list(reversed(round_ids))
        for limit in (0, 1, 2, 5, 6, 10**30, -1, -2, -5, -6, -(10**30)):
            with self.subTest(limit=limit):
                listed = [record.manifest.round_id for record in self.repo.list(limit=limit)]
                self.assertEqual(newest_first[:limit], listed)
        self.assertEqual(newest_first, [record.manifest.round_id for record in self.repo.list()])

    def test_get_of_an_unknown_round_is_none(self) -> None:
        self.assertIsNone(self.repo.get("missing"))
        self.assertEqual([], self.repo.list())

    def test_update_returns_what_a_later_get_returns(self) -> None:
        self.create("round-1")
        changed = self.repo.update(
            "round-1", lambda record: record.model_copy(update={"status": "collecting", "updated_at": 7})
        )
        self.assertEqual("collecting", changed.status)
        self.assertEqual(7, changed.updated_at)
        self.assertEqual(changed, self.repo.get("round-1"))

    def test_update_is_atomic_under_20_threads(self) -> None:
        """Twenty changes to one round at the same time each keep their result."""
        self.create("round-1")
        site_ids = [f"site-{number:02d}" for number in range(20)]
        barrier = threading.Barrier(len(site_ids))
        errors: list[BaseException] = []

        def add(site_id: str) -> None:
            try:
                barrier.wait()
                self.repo.update("round-1", lambda record: with_result(record, site_id))
            except BaseException as error:  # noqa: BLE001 - reported below
                errors.append(error)

        threads = [threading.Thread(target=add, args=(site_id,)) for site_id in site_ids]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual([], errors)
        self.assertEqual(site_ids, sorted(result.site_id for result in self.repo.get("round-1").results))

    def test_a_fully_populated_record_comes_back_as_it_was_stored(self) -> None:
        """Every number comes back as sent: -0.0 keeps its sign, 1e16 and 1e300 stay floats, 2**53 + 1 stays exact."""
        record = full_record("round-full")
        self.assertIs(record, self.repo.create(record))
        self.assert_same_record(record, self.repo.get("round-full"))
        self.assert_same_record(record, self.repo.list(limit=1)[0])
        self.assert_same_record(record, self.repo.update("round-full", lambda current: current))
        self.assert_same_record(record, self.repo.get("round-full"))
        saved = full_record("round-saved")
        self.assertIs(saved, self.repo.save(saved))
        self.assert_same_record(saved, self.repo.get("round-saved"))

    def assert_same_record(self, expected: RoundRecord, actual: RoundRecord | None) -> None:
        self.assertIsNotNone(actual)
        self.assertEqual(expected, actual)
        self.assertEqual(record_json(expected), record_json(actual))

    def test_a_record_with_nan_or_an_infinite_number_is_refused_and_nothing_changes(self) -> None:
        """JSON has no form for NaN or infinity, and pydantic writes them as null, so no store could read them back."""
        record = training_record(round_id="round-nan")
        self.repo.create(record)
        for name, broken in nan_records(record).items():
            with self.subTest(name):
                fresh = broken.model_copy(update={"manifest": build_manifest(round_id="round-new")})
                with self.assertRaises(InvalidRoundRecord) as raised:
                    self.repo.create(fresh)
                self.assertIn("round-new", str(raised.exception))
                self.assertIsInstance(raised.exception, ValueError)
                with self.assertRaises(InvalidRoundRecord):
                    self.repo.update("round-nan", lambda current, broken=broken: broken)
                with self.assertRaises(InvalidRoundRecord):
                    self.repo.save(broken)
                with self.assertRaises(InvalidRoundRecord):
                    self.repo.save(fresh)
        self.assertIsNone(self.repo.get("round-new"))
        self.assertEqual(record, self.repo.get("round-nan"))
        self.assertEqual(["round-nan"], [stored.manifest.round_id for stored in self.repo.list()])
        self.assertEqual(["round-nan"], [item.round_id for item in self.repo.unfinished()])

    # --- save() ------------------------------------------------------------------

    def test_save_stores_a_new_round_and_replaces_a_stored_one(self) -> None:
        self.repo.save(training_record(round_id="round-1"))
        self.assertEqual("dispatched", self.repo.get("round-1").status)
        self.repo.save(training_record(status="collecting", round_id="round-1"))
        self.assertEqual("collecting", self.repo.get("round-1").status)
        self.assertEqual(["round-1"], [record.manifest.round_id for record in self.repo.list()])

    def test_save_of_a_stored_round_keeps_its_place(self) -> None:
        self.create("round-1")
        self.create("round-2")
        self.repo.save(training_record(status="collecting", round_id="round-1"))
        self.assertEqual(["round-1", "round-2"], [item.round_id for item in self.repo.unfinished()])

    def test_save_of_a_quarantined_round_replaces_it_as_given(self) -> None:
        self.create("round-1")
        self.repo.quarantine("round-1", "the stored record could not be read")
        replacement = training_record(status="collecting", round_id="round-1")
        self.repo.save(replacement)
        self.assertEqual(replacement, self.repo.get("round-1"))

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

    def test_quarantine_keeps_a_reason_the_round_already_had(self) -> None:
        self.repo.create(training_record(round_id="round-1", abandoned_reason="the first reason"))
        self.assertTrue(self.repo.quarantine("round-1", "the stored record could not be read"))
        stored = self.repo.get("round-1")
        self.assertEqual("closed", stored.status)
        self.assertEqual("the first reason", stored.abandoned_reason)

    def test_a_change_after_quarantine_sees_the_round_closed(self) -> None:
        """A later write, such as the controller's discard, starts from the closed round."""
        self.create("round-1", status="collecting")
        self.repo.quarantine("round-1", "the stored record could not be read")
        seen: list[RoundRecord] = []

        def change(record: RoundRecord) -> RoundRecord:
            seen.append(record)
            return record.model_copy(update={"updated_at": record.updated_at + 1})

        changed = self.repo.update("round-1", change)
        [before] = seen
        self.assertEqual(("closed", "the stored record could not be read"), (before.status, before.abandoned_reason))
        self.assertEqual(("closed", "the stored record could not be read"), (changed.status, changed.abandoned_reason))
        self.assertEqual(changed, self.repo.get("round-1"))
        self.assertEqual([], self.repo.unfinished())


class InMemoryRoundRepositoryTests(RoundRepositoryContract, unittest.TestCase):
    def new_repository(self) -> InMemoryRoundRepository:
        return InMemoryRoundRepository()


# --------------------------------------------------------------------------- #
# Site enrolments
# --------------------------------------------------------------------------- #


class SiteRegistryContract:
    """What every enrolment store does. A subclass implements ``new_registry``."""

    def new_registry(self) -> Any:
        raise NotImplementedError

    def setUp(self) -> None:
        self.registry = self.new_registry()

    def test_an_unknown_site_has_no_enrolment(self) -> None:
        self.assertIsNone(self.registry.get("hospital-a"))
        self.assertEqual([], self.registry.list())

    def test_an_enrolment_comes_back_with_every_field(self) -> None:
        registration = build_registration(
            "hospital-a",
            capabilities=["local_training", "evaluate"],
            approved_conditions=["suspected_stroke", "sepsis"],
            policy_version="policy-7",
            cohort_size=BIG_COUNT,
        )
        self.assertEqual(registration, self.registry.save(registration))
        self.assertEqual(registration, self.registry.get("hospital-a"))
        self.assertEqual([registration], self.registry.list())

    def test_saving_a_site_again_replaces_its_enrolment(self) -> None:
        """The last writer wins, and the site is listed once."""
        self.registry.save(build_registration("hospital-a", cohort_size=10))
        self.registry.save(build_registration("hospital-b"))
        latest = build_registration("hospital-a", cohort_size=20, enrolled=False)
        self.registry.save(latest)
        self.assertEqual(latest, self.registry.get("hospital-a"))
        self.assertEqual(["hospital-a", "hospital-b"], [site.site_id for site in self.registry.list()])

    def test_the_list_is_in_python_sort_order(self) -> None:
        """Code point order, as ``sorted()`` gives it: upper case first, then punctuation by its code."""
        site_ids = ["hospital-b", "Hospital-A", "hospital-a", "hospital_c", "hospital-10", "hospital-9",
                    "zebra", "Zürich", "a b", "a-b", "a_b", "é-site", "hospital-a2"]
        for site_id in site_ids:
            self.registry.save(build_registration(site_id))
        self.assertEqual(sorted(site_ids), [site.site_id for site in self.registry.list()])


class InMemorySiteRegistryTests(SiteRegistryContract, unittest.TestCase):
    def new_registry(self) -> InMemorySiteRegistry:
        return InMemorySiteRegistry()


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

    # --- what a reload returns ---------------------------------------------------

    def reloaded(self) -> Any:
        """The store as a restarted GMA reads it. A Postgres class returns a new store on a new connection."""
        return self.store

    def test_a_negative_zero_coordinate_survives_a_reload_and_passes_the_digest_check(self) -> None:
        """-0.0 keeps its sign and 1e16 stays a float, so the model kind computes the receipt's digest again."""
        update = {"bias": -0.0, "w.x1": 1e16, "w.x2": 5e-324}
        receipt = self.store.put(self.record, "hospital-a", self.upload(update), self.kind)
        self.assertNotEqual(self.kind.update_digest({**update, "bias": 0.0}), receipt.digest)
        reloaded = self.reloaded()
        round_id = self.record.manifest.round_id
        for name, stored in (("get", reloaded.get(round_id, "hospital-a")),
                             ("updates", reloaded.updates(round_id)["hospital-a"])):
            with self.subTest(read=name):
                self.assertEqual(-1.0, math.copysign(1.0, stored.update["bias"]))
                self.assertIs(float, type(stored.update["w.x1"]))
                self.assertEqual(json.dumps(update, sort_keys=True), json.dumps(stored.update, sort_keys=True))
                self.assertEqual(receipt.digest, stored.digest)
                self.assertEqual(receipt.digest, self.kind.update_digest(stored.update))

    def test_a_stored_update_comes_back_with_every_field(self) -> None:
        store = self.new_store(Clock(1_700_000_000.25))
        receipt = store.put(self.record, "hospital-b", self.upload(), self.kind)
        expected = ("round-0042", "hospital-b", UPDATE, receipt.digest, "fnv1a:0123456789abcdef", 1_700_000_000.25)
        self.assertEqual(expected, astuple_of(store.get("round-0042", "hospital-b")))

    def test_updates_pending_rounds_and_ages_are_in_python_sort_order(self) -> None:
        """Code point order, as ``sorted()`` gives it, for site ids and round ids alike."""
        site_ids = ["hospital-b", "Hospital-A", "hospital_c", "a b", "a-b", "Zürich", "é-site"]
        round_ids = ["round-b", "Round-A", "round_c", "round b", "Round-é"]
        for round_id in round_ids:
            record = selecting(training_record(round_id=round_id), site_ids)
            for site_id in site_ids:
                self.store.put(record, site_id, self.upload(), self.kind)
        self.assertEqual(sorted(site_ids), list(self.store.updates("Round-A")))
        self.assertEqual(sorted(round_ids), self.store.pending_rounds())
        self.assertEqual(sorted(round_ids), list(self.store.pending_ages(0.0)))


def astuple_of(stored: Any) -> tuple[Any, ...]:
    """A stored update's fields, in the order ``StoredUpdate`` declares them."""
    return (stored.round_id, stored.site_id, stored.update, stored.digest, stored.job_digest, stored.received_at)


def selecting(record: RoundRecord, site_ids: list[str]) -> RoundRecord:
    """The record with a plan that selected ``site_ids``."""
    return record.model_copy(update={"plan": record.plan.model_copy(update={"selected_sites": list(site_ids)})})


class InMemoryUpdateStoreTests(UpdateStoreContract, unittest.TestCase):
    def new_store(self, clock: Callable[[], float] | None = None) -> InMemoryUpdateStore:
        return InMemoryUpdateStore() if clock is None else InMemoryUpdateStore(clock=clock)

    def test_the_kind_names_memory(self) -> None:
        self.assertEqual("in-memory", self.store.kind)

    def test_its_updates_do_not_survive_a_restart(self) -> None:
        # Recovery tells the planner the updates were not kept.
        self.assertIs(False, self.store.survives_restart)


# --------------------------------------------------------------------------- #
# The Postgres twins
# --------------------------------------------------------------------------- #


def started_scratch_database() -> ScratchDatabase:
    """A started scratch database. Skips with ``database test skipped`` when none is configured."""
    db = ScratchDatabase(admin_dsn_or_skip())
    try:
        db.start()
    except BaseException:
        db.stop()
        raise
    return db


def create_table(lease: KeptConnection, role: str, ddl: str, table: str) -> str:
    """Create ``table`` from a DDL constant, acting as ``role``, so the role owns it. Returns the table name."""
    with lease.acting_as(role) as conn:
        conn.execute(create_table_sql(ddl, table))
    return table


class PostgresTables:
    """A scratch database with the tables in ``TABLES``, emptied before each test.

    Tenant alpha plays the coordinator role and tenant beta the update role.
    Each table is created from its DDL constant as its owner, in the owner's
    schema. Alpha may use beta's schema, so a read of beta's updates table by
    alpha fails on the table's own privileges, as it would in a schema both
    roles share. ``self.lease`` is a kept connection, ``self.role`` is alpha
    and ``self.update_role`` is beta.
    """

    #: ``(name, DDL constant, owner)`` for each table. The owner is
    #: ``"coordinator"`` (alpha) or ``"update"`` (beta).
    TABLES: tuple[tuple[str, str, str], ...] = (
        ("rounds", ROUND_TABLE_DDL, "coordinator"),
        ("enrolments", ENROLMENT_TABLE_DDL, "coordinator"),
        ("updates", UPDATE_TABLE_DDL, "update"),
    )

    db: ScratchDatabase

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.db = started_scratch_database()
        cls.role, cls.update_role = cls.db.alpha, cls.db.beta
        try:
            lease = KeptConnection(cls.db.dsn, name="setup")
            try:
                for name, ddl, _ in cls.TABLES:
                    create_table(lease, cls.owner(name), ddl, cls.table(name))
                with lease.acting_as(cls.update_role) as conn:
                    conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                        sql.Identifier(cls.update_role), sql.Identifier(cls.role)))
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
        self.lease = KeptConnection(self.db.dsn, name="coordinator")
        self.addCleanup(self.lease.close)
        for name, _, _ in self.TABLES:
            with self.lease.acting_as(self.owner(name)) as conn:
                conn.execute(sql.SQL("TRUNCATE {} RESTART IDENTITY").format(self.identifier(name)))
        super().setUp()

    @classmethod
    def owner(cls, name: str) -> str:
        """The role that owns the table, in whose schema it is."""
        [owner] = [owner for table, _, owner in cls.TABLES if table == name]
        return cls.update_role if owner == "update" else cls.role

    @classmethod
    def table(cls, name: str) -> str:
        return f"{cls.owner(name)}.{name}"

    @classmethod
    def identifier(cls, name: str) -> Any:
        return sql.Identifier(cls.owner(name), name)

    def second_lease(self) -> KeptConnection:
        """Another kept connection, as a second GMA process would hold."""
        lease = KeptConnection(self.db.dsn, name="coordinator two")
        self.addCleanup(lease.close)
        return lease

    def assert_no_connection_details(self, message: str) -> None:
        details = psycopg.conninfo.conninfo_to_dict(self.db.dsn)
        for secret in (self.db.dsn, details.get("password"), details.get("host")):
            self.assertNotIn(str(secret), message)


def run_together(calls: list[Callable[[], Any]]) -> list[Any]:
    """Run each call on its own thread, all starting at once. Returns each result or the error it raised."""
    barrier = threading.Barrier(len(calls))
    outcomes: list[Any] = [None] * len(calls)

    def run(index: int, call: Callable[[], Any]) -> None:
        barrier.wait()
        try:
            outcomes[index] = call()
        except BaseException as error:  # noqa: BLE001 - returned to the test
            outcomes[index] = error

    threads = [threading.Thread(target=run, args=(index, call)) for index, call in enumerate(calls)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    return outcomes


class PostgresRoundRepositoryTests(PostgresTables, RoundRepositoryContract, unittest.TestCase):
    def new_repository(self) -> PostgresRoundRepository:
        return PostgresRoundRepository(self.lease, self.role, self.table("rounds"))

    def raw_row(self, round_id: str) -> dict[str, Any] | None:
        """The round's row as stored, with the record as text."""
        query = sql.SQL(
            "SELECT status, phase, record_version, record::text AS record, pg_typeof(record)::text AS record_type, "
            "closed_reason, created_at, updated_at FROM {} WHERE round_id = %s"
        ).format(self.identifier("rounds"))
        with self.lease.acting_as(self.role) as conn:
            return conn.execute(query, (round_id,)).fetchone()

    def write_columns(self, round_id: str, **columns: Any) -> None:
        """Change columns of a stored round directly, as a damaged or newer row would hold them."""
        assignments = sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(name)) for name in columns)
        query = sql.SQL("UPDATE {} SET {} WHERE round_id = %s").format(self.identifier("rounds"), assignments)
        with self.lease.acting_as(self.role) as conn:
            conn.execute(query, (*columns.values(), round_id))

    # --- the store and its table -------------------------------------------------

    def test_the_store_keeps_rounds_across_restarts(self) -> None:
        self.assertEqual("postgres", self.repo.kind)
        self.assertIs(True, self.repo.survives_restart)
        self.assertEqual({"table": self.table("rounds"), "role": self.role}, self.repo.location)

    def test_building_a_store_opens_no_connection(self) -> None:
        lease = KeptConnection(self.db.dsn, name="unused")
        self.addCleanup(lease.close)
        PostgresRoundRepository(lease, self.role, self.table("rounds"))
        PostgresEnrolmentStore(lease, self.role, self.table("enrolments"))
        self.assertEqual(0, lease.connections_opened)

    def test_the_tables_use_json_columns(self) -> None:
        """``jsonb`` changes -0.0, 1e16 and larger numbers, so no column the stores write is ``jsonb``."""
        for ddl in (ROUND_TABLE_DDL, ENROLMENT_TABLE_DDL):
            self.assertNotIn("jsonb", ddl)
        query = (
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name IN ('rounds', 'enrolments') AND data_type IN ('json', 'jsonb') "
            "ORDER BY table_name, column_name"
        )
        with self.lease.acting_as(self.role) as conn:
            columns = [tuple(row.values()) for row in conn.execute(query, (self.role,)).fetchall()]
        self.assertEqual([("enrolments", "registration", "json"), ("rounds", "record", "json")], columns)

    def test_a_record_is_stored_as_json_text_with_its_columns(self) -> None:
        record = full_record("round-full")
        self.repo.create(record)
        row = self.raw_row("round-full")
        self.assertEqual("json", row["record_type"])
        self.assertEqual(RECORD_VERSION, row["record_version"])
        self.assertEqual(("closed", "training"), (row["status"], row["phase"]))
        self.assertEqual((record.created_at, record.updated_at), (row["created_at"], row["updated_at"]))
        self.assertIsNone(row["closed_reason"])
        for text in ('"brier": -0.0', '"auc": 1e+16', '"huge": 1e+300', str(BIG_COUNT)):
            self.assertIn(text, row["record"])
        self.assertEqual(record_json(record), json.dumps(json.loads(row["record"]), sort_keys=True))

    def test_a_refused_record_writes_nothing_to_the_row(self) -> None:
        """The contract's NaN test reads through the store; here the row itself is unchanged."""
        record = training_record(round_id="round-nan")
        self.repo.create(record)
        before = self.raw_row("round-nan")
        for name, broken in nan_records(record).items():
            with self.subTest(name), self.assertRaises(InvalidRoundRecord):
                self.repo.update("round-nan", lambda current, broken=broken: broken)
        self.assertEqual(before, self.raw_row("round-nan"))

    def test_a_role_without_a_grant_gets_store_not_set_up_with_no_connection_details(self) -> None:
        self.create("round-1")
        other = PostgresRoundRepository(self.lease, self.db.beta, self.table("rounds"))
        calls: dict[str, Callable[[], Any]] = {
            "create": lambda: other.create(training_record(round_id="round-2")),
            "save": lambda: other.save(training_record(round_id="round-2")),
            "update": lambda: other.update("round-1", lambda record: record),
            "get": lambda: other.get("round-1"),
            "list": lambda: other.list(),
            "list with a negative limit": lambda: other.list(limit=-1),
            "unfinished": other.unfinished,
            "quarantine": lambda: other.quarantine("round-1", "the stored record could not be read"),
        }
        for name, call in calls.items():
            with self.subTest(call=name):
                with self.assertRaises(StoreNotSetUpError) as raised:
                    call()
                self.assert_no_connection_details(str(raised.exception))
        self.assertEqual("dispatched", self.repo.get("round-1").status)
        self.assertIsNone(self.repo.get("round-2"))

    def test_bad_table_and_role_names_are_refused(self) -> None:
        for table in ("rounds; DROP TABLE x", "A.rounds", "a.b.c", "", "1x.rounds", 'a."b"', None):
            with self.subTest(table=table), self.assertRaises(ValueError):
                PostgresRoundRepository(self.lease, self.role, table)
        for role in ("Coordinator", "postgres", "pg_monitor", "a-b", "", "x; DROP ROLE y", None):
            with self.subTest(role=role), self.assertRaises(RoleScopeError):
                PostgresRoundRepository(self.lease, role, self.table("rounds"))

    # --- two GMA processes on one table -----------------------------------------

    def test_two_leases_creating_one_round_id_store_it_once(self) -> None:
        other = PostgresRoundRepository(self.second_lease(), self.role, self.table("rounds"))
        stores = [self.repo, other, self.repo, other]
        outcomes = run_together(
            [lambda store=store, n=n: store.create(training_record(round_id="round-race", updated_at=n))
             for n, store in enumerate(stores)]
        )
        created = [outcome for outcome in outcomes if isinstance(outcome, RoundRecord)]
        refused = [outcome for outcome in outcomes if isinstance(outcome, RoundConflict)]
        self.assertEqual((1, 3), (len(created), len(refused)), outcomes)
        self.assertEqual(created[0], self.repo.get("round-race"))
        self.assertEqual(["round-race"], [record.manifest.round_id for record in self.repo.list()])

    def test_two_leases_changing_one_round_lose_no_change(self) -> None:
        """Each change holds the row's lock from its read to its write, across processes."""
        self.create("round-1")
        other = PostgresRoundRepository(self.second_lease(), self.role, self.table("rounds"))
        site_ids = [f"site-{number:02d}" for number in range(20)]

        def slow_add(site_id: str) -> Callable[[RoundRecord], RoundRecord]:
            def change(record: RoundRecord) -> RoundRecord:
                time.sleep(0.005)  # widen the gap between the read and the write
                return with_result(record, site_id)

            return change

        outcomes = run_together(
            [lambda store=(self.repo if n % 2 else other), site_id=site_id: store.update("round-1", slow_add(site_id))
             for n, site_id in enumerate(site_ids)]
        )
        self.assertEqual([], [outcome for outcome in outcomes if isinstance(outcome, BaseException)])
        self.assertEqual(site_ids, sorted(result.site_id for result in self.repo.get("round-1").results))

    # --- a stored record this version cannot read --------------------------------

    def damaged_rows(self, round_id: str) -> dict[str, dict[str, Any]]:
        """Ways the stored record of round ``round_id`` can be unreadable, as column values.

        The record texts that Python cannot decode, or that hold a number that
        is not finite, come from :func:`undecodable_records`.
        """
        rows: dict[str, dict[str, Any]] = {
            "does not parse": {"record": Json({"manifest": "broken"})},
            "has an unknown record version": {"record_version": RECORD_VERSION + 1},
            "names another round": {"record": Json(training_record(round_id="round-99").model_dump(mode="json"))},
        }
        for damage, text in undecodable_records(round_id).items():
            rows[f"is JSON {damage}"] = {"record": raw_json(text)}
        return rows

    def damage(self) -> list[tuple[str, str, dict[str, Any]]]:
        """``(damage, round_id, columns)`` for each way in :meth:`damaged_rows`, each with a round of its own."""
        names = list(self.damaged_rows("round-0"))
        return [
            (name, f"round-{number}", self.damaged_rows(f"round-{number}")[name])
            for number, name in enumerate(names, start=1)
        ]

    def test_get_and_update_of_an_unreadable_record_raise_store_not_set_up_naming_the_round(self) -> None:
        for damage, round_id, columns in self.damage():
            with self.subTest(damage=damage):
                self.create(round_id, status="collecting")
                self.write_columns(round_id, **columns)
                before = self.raw_row(round_id)
                with self.assertRaises(StoreNotSetUpError) as raised:
                    self.repo.get(round_id)
                self.assertIn(round_id, str(raised.exception))
                self.assert_no_connection_details(str(raised.exception))
                called: list[RoundRecord] = []
                with self.assertRaises(StoreNotSetUpError) as raised:
                    self.repo.update(round_id, lambda record: called.append(record) or record)
                self.assertIn(round_id, str(raised.exception))
                self.assertEqual([], called)
                self.assertEqual(before, self.raw_row(round_id))

    def test_every_unreadable_record_is_marked_in_unfinished_left_out_of_the_list_and_can_be_quarantined(self) -> None:
        """One bad row hides no other round, and recovery can close each from its columns."""
        damage = self.damage()
        for _, round_id, columns in damage:
            self.create(round_id, status="collecting")
            self.write_columns(round_id, **columns)
        self.create("round-good")
        with self.assertLogs(STORE_LOGGER, "WARNING") as logs:
            self.assertEqual(["round-good"], [record.manifest.round_id for record in self.repo.list()])
        for name, round_id, _ in damage:
            with self.subTest(damage=name):
                self.assertTrue(any(f"round {round_id} " in line for line in logs.output), logs.output)
        self.assertEqual(
            {**{round_id: False for _, round_id, _ in damage}, "round-good": True},
            {item.round_id: item.readable for item in self.repo.unfinished()},
        )
        for name, round_id, _ in damage:
            with self.subTest(damage=name):
                self.assertTrue(self.repo.quarantine(round_id, "the stored record could not be read"))
        self.assertEqual(["round-good"], [item.round_id for item in self.repo.unfinished()])

    def test_list_leaves_out_an_unreadable_record_with_a_warning(self) -> None:
        self.create("round-1")
        self.create("round-2")
        self.create("round-3")
        self.write_columns("round-2", record=Json({"manifest": "broken"}))
        with self.assertLogs(STORE_LOGGER, "WARNING") as logs:
            listed = [record.manifest.round_id for record in self.repo.list()]
        self.assertEqual(["round-3", "round-1"], listed)
        self.assertTrue(any("round-2" in line for line in logs.output), logs.output)

    def test_unfinished_reads_status_and_phase_from_the_columns_and_marks_unreadable_records(self) -> None:
        self.create("round-1", status="collecting")
        self.create("round-2", status="dispatched", phase="evaluation")
        self.create("round-3", status="planned")
        self.write_columns("round-1", record=Json({"manifest": "broken"}))
        self.write_columns("round-3", record_version=RECORD_VERSION + 1)
        self.assertEqual(
            [
                UnfinishedRound(round_id="round-1", status="collecting", phase="training", readable=False),
                UnfinishedRound(round_id="round-2", status="dispatched", phase="evaluation", readable=True),
                UnfinishedRound(round_id="round-3", status="planned", phase="training", readable=False),
            ],
            self.repo.unfinished(),
        )

    def test_quarantine_closes_an_unreadable_round_without_reading_its_record(self) -> None:
        self.create("round-1", status="collecting")
        self.create("round-2", status="dispatched")
        self.write_columns("round-1", record=Json({"manifest": "broken"}))
        before = self.raw_row("round-1")
        self.assertTrue(self.repo.quarantine("round-1", "the stored record could not be read"))
        after = self.raw_row("round-1")
        self.assertEqual(("closed", "the stored record could not be read"), (after["status"], after["closed_reason"]))
        self.assertEqual(before["record"], after["record"])
        self.assertEqual(before["record_version"], after["record_version"])
        self.assertEqual(["round-2"], [item.round_id for item in self.repo.unfinished()])
        self.assertFalse(self.repo.quarantine("round-1", "again"))
        with self.assertRaises(RoundConflict):
            self.create("round-1")
        self.assertEqual("the stored record could not be read", self.raw_row("round-1")["closed_reason"])

    def test_quarantine_writes_only_the_status_the_reason_and_the_time(self) -> None:
        record = training_record(status="collecting", round_id="round-1")
        self.repo.create(record)
        before = self.raw_row("round-1")
        self.repo.quarantine("round-1", "the stored record could not be read")
        after = self.raw_row("round-1")
        self.assertEqual(before["record"], after["record"])
        self.assertGreaterEqual(after["updated_at"], int(time.time()) - 60)
        stored = self.repo.get("round-1")
        self.assertEqual(after["updated_at"], stored.updated_at)
        self.assertEqual(record.model_copy(update={"status": "closed", "abandoned_reason": "the stored record could not be read",
                                                   "updated_at": after["updated_at"]}), stored)

    def test_save_of_a_quarantined_round_clears_the_quarantine_reason(self) -> None:
        self.create("round-1")
        self.repo.quarantine("round-1", "the stored record could not be read")
        self.repo.save(training_record(status="collecting", round_id="round-1"))
        self.assertIsNone(self.raw_row("round-1")["closed_reason"])
        self.assertEqual(["round-1"], [item.round_id for item in self.repo.unfinished()])


class SlowPostgresRoundRepository(PostgresRoundRepository):
    """The Postgres round store with reads that take 50 ms, as ``SlowReadRepository`` does for memory."""

    def get(self, round_id: str) -> RoundRecord | None:
        record = super().get(round_id)
        time.sleep(0.05)
        return record


class PostgresEnrolmentStoreTests(PostgresTables, SiteRegistryContract, unittest.TestCase):
    def new_registry(self) -> PostgresEnrolmentStore:
        return PostgresEnrolmentStore(self.lease, self.role, self.table("enrolments"))

    def write_registration(self, site_id: str, registration: Any) -> None:
        query = sql.SQL("UPDATE {} SET registration = %s WHERE site_id = %s").format(self.identifier("enrolments"))
        with self.lease.acting_as(self.role) as conn:
            conn.execute(query, (Json(registration), site_id))

    def test_the_kind_names_postgres(self) -> None:
        self.assertEqual("postgres", self.registry.kind)
        self.assertEqual({"table": self.table("enrolments"), "role": self.role}, self.registry.location)

    def test_the_list_keeps_python_order_under_another_column_collation(self) -> None:
        """The store sorts with ``COLLATE "C"``, so a server or column with a language collation lists the same order."""
        column = sql.SQL("ALTER TABLE {} ALTER COLUMN site_id TYPE text COLLATE {}")
        with self.lease.acting_as(self.role) as conn:
            conn.execute(column.format(self.identifier("enrolments"), sql.Identifier("und-x-icu")))
        self.addCleanup(self.reset_collation)
        site_ids = ["hospital-b", "Hospital-A", "hospital-a", "hospital_c", "a b", "a-b", "Zürich", "é-site"]
        for site_id in site_ids:
            self.registry.save(build_registration(site_id))
        with self.lease.acting_as(self.role) as conn:
            language_order = [row["site_id"] for row in conn.execute(
                sql.SQL("SELECT site_id FROM {} ORDER BY site_id").format(self.identifier("enrolments"))
            ).fetchall()]
        self.assertNotEqual(sorted(site_ids), language_order, "the column collation sorts as Python does")
        self.assertEqual(sorted(site_ids), [site.site_id for site in self.registry.list()])

    def reset_collation(self) -> None:
        column = sql.SQL('ALTER TABLE {} ALTER COLUMN site_id TYPE text COLLATE "default"')
        with self.lease.acting_as(self.role) as conn:
            conn.execute(column.format(self.identifier("enrolments")))

    def test_two_leases_enrolling_one_site_store_one_row(self) -> None:
        other = PostgresEnrolmentStore(self.second_lease(), self.role, self.table("enrolments"))
        registrations = [build_registration("hospital-a", cohort_size=size) for size in range(1, 11)]
        outcomes = run_together(
            [lambda store=(self.registry if n % 2 else other), registration=registration: store.save(registration)
             for n, registration in enumerate(registrations)]
        )
        self.assertEqual(registrations, outcomes)
        [stored] = self.registry.list()
        self.assertIn(stored, registrations)
        self.assertEqual(stored, self.registry.get("hospital-a"))

    def test_a_role_without_a_grant_gets_store_not_set_up_with_no_connection_details(self) -> None:
        self.registry.save(build_registration("hospital-a"))
        other = PostgresEnrolmentStore(self.lease, self.db.beta, self.table("enrolments"))
        calls: dict[str, Callable[[], Any]] = {
            "save": lambda: other.save(build_registration("hospital-b")),
            "get": lambda: other.get("hospital-a"),
            "list": other.list,
        }
        for name, call in calls.items():
            with self.subTest(call=name):
                with self.assertRaises(StoreNotSetUpError) as raised:
                    call()
                self.assert_no_connection_details(str(raised.exception))
        self.assertEqual(["hospital-a"], [site.site_id for site in self.registry.list()])

    def test_bad_table_and_role_names_are_refused(self) -> None:
        for table in ("enrolments; DROP TABLE x", "A.enrolments", "a.b.c", "", None):
            with self.subTest(table=table), self.assertRaises(ValueError):
                PostgresEnrolmentStore(self.lease, self.role, table)
        for role in ("Coordinator", "postgres", "pg_monitor", "a-b", "", None):
            with self.subTest(role=role), self.assertRaises(RoleScopeError):
                PostgresEnrolmentStore(self.lease, role, self.table("enrolments"))

    def test_an_unreadable_enrolment_raises_on_get_and_is_left_out_of_the_list(self) -> None:
        for site_id in ("hospital-a", "hospital-b", "hospital-c"):
            self.registry.save(build_registration(site_id))
        self.write_registration("hospital-a", {"site_id": "hospital-a", "cohort_size": "many"})
        self.write_registration("hospital-b", build_registration("hospital-z").model_dump(mode="json"))
        for site_id in ("hospital-a", "hospital-b"):
            with self.subTest(site=site_id):
                with self.assertRaises(StoreNotSetUpError) as raised:
                    self.registry.get(site_id)
                self.assertIn(site_id, str(raised.exception))
        with self.assertLogs(STORE_LOGGER, "WARNING") as logs:
            self.assertEqual(["hospital-c"], [site.site_id for site in self.registry.list()])
        self.assertTrue(any("hospital-a" in line for line in logs.output), logs.output)
        self.assertTrue(any("hospital-b" in line for line in logs.output), logs.output)

    def test_an_enrolment_python_cannot_decode_raises_on_get_and_hides_no_other_site(self) -> None:
        """PostgreSQL's json type stores both texts; one such row must not hide every site from round planning."""
        for site_id in ("hospital-a", "hospital-b", "hospital-c"):
            self.registry.save(build_registration(site_id))
        registration = build_registration("hospital-b")
        plain = json.dumps(registration.model_dump(mode="json"))
        texts = {
            "hospital-a": '{"site_id": "hospital-a", "capabilities": ' + DEEP_JSON + "}",
            "hospital-b": plain.replace(f'"cohort_size": {registration.cohort_size}', '"cohort_size": ' + LONG_INTEGER),
        }
        self.assertNotEqual(plain, texts["hospital-b"])
        query = sql.SQL("UPDATE {} SET registration = %s WHERE site_id = %s").format(self.identifier("enrolments"))
        with self.lease.acting_as(self.role) as conn:
            for site_id, text in texts.items():
                conn.execute(query, (raw_json(text), site_id))
        for site_id in texts:
            with self.subTest(site=site_id):
                with self.assertRaises(StoreNotSetUpError) as raised:
                    self.registry.get(site_id)
                self.assertIn(site_id, str(raised.exception))
                self.assert_no_connection_details(str(raised.exception))
        with self.assertLogs(STORE_LOGGER, "WARNING") as logs:
            self.assertEqual(["hospital-c"], [site.site_id for site in self.registry.list()])
        for site_id in texts:
            self.assertTrue(any(site_id in line for line in logs.output), logs.output)


class PostgresUpdateStoreTests(PostgresTables, UpdateStoreContract, unittest.TestCase):
    """The update store on a table the update role (beta) owns, with alpha as the coordinator role."""

    def new_store(self, clock: Callable[[], float] | None = None, lease: KeptConnection | None = None) -> Any:
        extra = {} if clock is None else {"clock": clock}
        return PostgresUpdateStore(
            lease or self.lease, self.update_role, self.table("updates"), coordinator_role=self.role, **extra
        )

    def reloaded(self) -> PostgresUpdateStore:
        return self.new_store(lease=self.second_lease())

    def test_its_updates_survive_a_restart(self) -> None:
        # Recovery tells the planner the updates were kept.
        self.assertIs(True, self.store.survives_restart)
        self.store.put(self.record, "hospital-a", self.upload(), self.kind)
        self.assertEqual(["hospital-a"], list(self.reloaded().updates(self.record.manifest.round_id)))

    def raw_rows(self, round_id: str) -> list[dict[str, Any]]:
        """The round's rows as stored, gate row first, with the update as text."""
        query = sql.SQL(
            "SELECT site_id, update_values::text AS update_values, pg_typeof(update_values)::text AS value_type, "
            'digest, job_digest, received_at, discarded FROM {} WHERE round_id = %s ORDER BY site_id COLLATE "C"'
        ).format(self.identifier("updates"))
        with self.lease.acting_as(self.update_role) as conn:
            return list(conn.execute(query, (round_id,)).fetchall())

    def write_row(self, round_id: str, site_id: str, **columns: Any) -> None:
        """Change columns of a stored row directly, as a damaged row would hold them."""
        assignments = sql.SQL(", ").join(sql.SQL("{} = %s").format(sql.Identifier(name)) for name in columns)
        query = sql.SQL("UPDATE {} SET {} WHERE round_id = %s AND site_id = %s").format(
            self.identifier("updates"), assignments
        )
        with self.lease.acting_as(self.update_role) as conn:
            conn.execute(query, (*columns.values(), round_id, site_id))

    # --- the store and its table -------------------------------------------------

    def test_the_kind_names_postgres(self) -> None:
        self.assertEqual("postgres", self.store.kind)
        self.assertEqual({"table": self.table("updates"), "role": self.update_role}, self.store.location)

    def test_building_a_store_opens_no_connection(self) -> None:
        lease = KeptConnection(self.db.dsn, name="unused")
        self.addCleanup(lease.close)
        PostgresUpdateStore(lease, self.update_role, self.table("updates"), coordinator_role=self.role)
        self.assertEqual(0, lease.connections_opened)

    def test_the_table_uses_a_json_column(self) -> None:
        """``jsonb`` turns -0.0 into 0.0, which changes an update's digest."""
        self.assertNotIn("jsonb", UPDATE_TABLE_DDL)
        query = (
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = 'updates' AND data_type IN ('json', 'jsonb')"
        )
        with self.lease.acting_as(self.update_role) as conn:
            columns = [tuple(row.values()) for row in conn.execute(query, (self.update_role,)).fetchall()]
        self.assertEqual([("update_values", "json")], columns)

    def test_an_update_is_stored_as_json_text_beside_its_round_gate_row(self) -> None:
        store = self.new_store(Clock(100.0))
        receipt = store.put(self.record, "hospital-a", self.upload({"bias": -0.0, "w.x1": 1e16, "w.x2": 0.5}), self.kind)
        gate, row = self.raw_rows("round-0042")
        self.assertEqual(
            {"site_id": "", "update_values": None, "value_type": "json", "digest": None, "job_digest": None,
             "received_at": 100.0, "discarded": False},
            gate,
        )
        self.assertEqual(("hospital-a", "json", receipt.digest, "fnv1a:0123456789abcdef", 100.0, False),
                         (row["site_id"], row["value_type"], row["digest"], row["job_digest"], row["received_at"],
                          row["discarded"]))
        for text in ('"bias": -0.0', '"w.x1": 1e+16', '"w.x2": 0.5'):
            self.assertIn(text, row["update_values"])

    # --- roles ---------------------------------------------------------------------

    def test_the_update_role_must_differ_from_the_coordinator_role(self) -> None:
        """Otherwise anything acting as the coordinator role could read a vector."""
        lease = KeptConnection(self.db.dsn, name="unused")
        self.addCleanup(lease.close)
        with self.assertRaises(RoleScopeError) as raised:
            PostgresUpdateStore(lease, self.role, self.table("updates"), coordinator_role=self.role)
        self.assertIn(self.role, str(raised.exception))
        self.assertEqual(0, lease.connections_opened)

    def test_the_coordinator_role_cannot_select_from_the_updates_table(self) -> None:
        """The server refuses the table itself: alpha may use beta's schema but holds no grant on the table."""
        self.store.put(self.record, "hospital-a", self.upload(), self.kind)
        statements = {
            "read the vectors": "SELECT update_values FROM {}",
            "count the rows": "SELECT count(*) FROM {}",
            "delete": "DELETE FROM {}",
        }
        with psycopg.connect(self.db.dsn, autocommit=True) as conn:
            for name, statement in statements.items():
                with self.subTest(statement=name):
                    with self.assertRaises(psycopg.errors.InsufficientPrivilege) as raised, conn.transaction():
                        conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(self.role)))
                        conn.execute(sql.SQL(statement).format(self.identifier("updates")))
                    self.assertIn("permission denied for table updates", str(raised.exception))
            with conn.transaction():  # the owner resolves the table's name; the check is about alpha
                conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(self.update_role)))
                privileges = conn.execute(
                    "SELECT has_table_privilege(%s, %s, 'SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER')",
                    (self.role, self.table("updates")),
                ).fetchone()
        self.assertEqual((False,), privileges)

        # Through the kept connection, as coordinator code would try it: store_not_set_up, with no connection details.
        with self.assertRaises(StoreNotSetUpError) as raised, self.lease.acting_as(self.role) as conn:
            conn.execute(sql.SQL("SELECT update_values FROM {}").format(self.identifier("updates")))
        self.assert_no_connection_details(str(raised.exception))
        self.assertEqual(["round-0042"], self.store.pending_rounds())

    def test_a_role_without_a_grant_gets_store_not_set_up_with_no_connection_details(self) -> None:
        self.store.put(self.record, "hospital-a", self.upload(), self.kind)
        other = PostgresUpdateStore(self.lease, self.role, self.table("updates"), coordinator_role=self.update_role)
        calls: dict[str, Callable[[], Any]] = {
            "put": lambda: other.put(self.record, "hospital-b", self.upload(), self.kind),
            "get": lambda: other.get("round-0042", "hospital-a"),
            "updates": lambda: other.updates("round-0042"),
            "discard": lambda: other.discard("round-0042"),
            "pending_rounds": other.pending_rounds,
            "pending_ages": lambda: other.pending_ages(0.0),
        }
        for name, call in calls.items():
            with self.subTest(call=name):
                with self.assertRaises(StoreNotSetUpError) as raised:
                    call()
                self.assert_no_connection_details(str(raised.exception))
        self.assertEqual(["hospital-a"], list(self.store.updates("round-0042")))

    def test_bad_table_and_role_names_are_refused(self) -> None:
        for table in ("updates; DROP TABLE x", "A.updates", "a.b.c", "", "1x.updates", 'a."b"', None):
            with self.subTest(table=table), self.assertRaises(ValueError):
                PostgresUpdateStore(self.lease, self.update_role, table, coordinator_role=self.role)
        for role in ("Coordinator", "postgres", "pg_monitor", "a-b", "", "x; DROP ROLE y", None):
            with self.subTest(role=role), self.assertRaises(RoleScopeError):
                PostgresUpdateStore(self.lease, role, self.table("updates"), coordinator_role=self.role)
            with self.subTest(coordinator_role=role), self.assertRaises(RoleScopeError):
                PostgresUpdateStore(self.lease, self.update_role, self.table("updates"), coordinator_role=role)

    # --- the gate row ----------------------------------------------------------------

    def test_a_discard_in_one_process_refuses_an_upload_in_another(self) -> None:
        """The gate row keeps the discard in the table, so a restarted GMA refuses a late upload too."""
        self.assertEqual(0, self.store.discard("round-0042"))
        later = self.reloaded()
        with self.assertRaises(RoundConflict) as raised:
            later.put(self.record, "hospital-a", self.upload(), self.kind)
        self.assertIn("was discarded", str(raised.exception))
        self.assertEqual([("", None, True)],
                         [(row["site_id"], row["update_values"], row["discarded"]) for row in self.raw_rows("round-0042")])
        self.assertIsNone(later.get("round-0042", ""))
        self.assertEqual(({}, [], {}), (later.updates("round-0042"), later.pending_rounds(), later.pending_ages(0.0)))

    def test_a_refused_upload_leaves_no_row(self) -> None:
        """Checks against the round run before the store writes anything."""
        with self.assertRaises(NotSelected):
            self.store.put(self.record, "intruder", self.upload(), self.kind)
        with self.assertRaises(RoundConflict):
            self.store.put(self.record, "hospital-a", self.upload(job_digest="fnv1a:ffffffffffffffff"), self.kind)
        self.assertEqual([], self.raw_rows("round-0042"))

    def test_an_update_row_the_store_cannot_read_raises_store_not_set_up_naming_the_round_and_site(self) -> None:
        damages: dict[str, dict[str, Any]] = {
            "values that are not a mapping": {"update_values": Json([0.1, 0.2, 0.3])},
            "a value that is text": {"update_values": Json({"bias": "high", "w.x1": 0.0, "w.x2": 0.0})},
            "a value that is a boolean": {"update_values": Json({"bias": True, "w.x1": 0.0, "w.x2": 0.0})},
            "a value that is null": {"update_values": Json({"bias": None, "w.x1": 0.0, "w.x2": 0.0})},
            "no digest": {"digest": None},
            "no job digest": {"job_digest": None},
            # PostgreSQL's json type stores these three; Python cannot decode the
            # first two, and reads the third as infinity.
            "values nested 10,000 deep": {"update_values": raw_json('{"bias": ' + DEEP_JSON + "}")},
            "a value of 5,000 digits": {
                "update_values": raw_json('{"bias": ' + LONG_INTEGER + ', "w.x1": 0.0, "w.x2": 0.0}')
            },
            "a value too large for a float": {"update_values": raw_json('{"bias": 1e400, "w.x1": 0.0, "w.x2": 0.0}')},
        }
        for number, (damage, columns) in enumerate(damages.items(), start=1):
            with self.subTest(damage=damage):
                record = training_record(round_id=f"round-{number}")
                self.store.put(record, "hospital-a", self.upload(), self.kind)
                self.store.put(record, "hospital-b", self.upload(), self.kind)
                self.write_row(f"round-{number}", "hospital-a", **columns)
                for read in (lambda: self.store.get(f"round-{number}", "hospital-a"),
                             lambda: self.store.updates(f"round-{number}")):
                    with self.assertRaises(StoreNotSetUpError) as raised:
                        read()
                    self.assertIn(f"round-{number}", str(raised.exception))
                    self.assertIn("hospital-a", str(raised.exception))
                    self.assert_no_connection_details(str(raised.exception))
                self.assertEqual(UPDATE, self.store.get(f"round-{number}", "hospital-b").update)
                self.assertIn(f"round-{number}", self.store.pending_rounds())

    def test_updates_and_rounds_keep_python_order_under_another_column_collation(self) -> None:
        """The store sorts with ``COLLATE "C"``, so a column with a language collation lists the same order."""
        column = sql.SQL("ALTER TABLE {} ALTER COLUMN round_id TYPE text COLLATE {}, ALTER COLUMN site_id TYPE text COLLATE {}")
        with self.lease.acting_as(self.update_role) as conn:
            conn.execute(column.format(self.identifier("updates"), sql.Identifier("und-x-icu"), sql.Identifier("und-x-icu")))
        self.addCleanup(self.reset_collation)
        site_ids = ["hospital-b", "Hospital-A", "hospital_c", "a b", "a-b", "Zürich", "é-site"]
        round_ids = ["round-b", "Round-A", "round_c", "round b"]
        for round_id in round_ids:
            record = selecting(training_record(round_id=round_id), site_ids)
            for site_id in site_ids:
                self.store.put(record, site_id, self.upload(), self.kind)
        with self.lease.acting_as(self.update_role) as conn:
            language_order = [row["round_id"] for row in conn.execute(
                sql.SQL("SELECT DISTINCT round_id FROM {} ORDER BY round_id").format(self.identifier("updates"))
            ).fetchall()]
        self.assertNotEqual(sorted(round_ids), language_order, "the column collation sorts as Python does")
        self.assertEqual(sorted(site_ids), list(self.store.updates("Round-A")))
        self.assertEqual(sorted(round_ids), self.store.pending_rounds())
        self.assertEqual(sorted(round_ids), list(self.store.pending_ages(0.0)))

    def reset_collation(self) -> None:
        column = sql.SQL(
            'ALTER TABLE {} ALTER COLUMN round_id TYPE text COLLATE "default", ALTER COLUMN site_id TYPE text COLLATE "default"'
        )
        with self.lease.acting_as(self.update_role) as conn:
            conn.execute(column.format(self.identifier("updates")))

    # --- two GMA processes on one table -----------------------------------------------

    def test_two_leases_putting_and_discarding_lose_no_update(self) -> None:
        """In 30 races, every update a put stored is deleted and counted by the discard, and none is left behind."""
        other = self.reloaded()
        for number in range(30):
            record = training_record(round_id=f"round-race-{number:02d}")
            round_id = record.manifest.round_id
            putter, discarder = (self.store, other) if number % 2 else (other, self.store)
            outcomes = run_together([
                lambda putter=putter, record=record: putter.put(record, "hospital-a", self.upload(), self.kind),
                lambda discarder=discarder, round_id=round_id: discarder.discard(round_id),
                lambda putter=putter, record=record: putter.put(record, "hospital-b", self.upload(), self.kind),
            ])
            with self.subTest(race=number, outcomes=outcomes):
                first, deleted, second = outcomes
                self.assertIsInstance(deleted, int)
                stored = [outcome for outcome in (first, second) if isinstance(outcome, UpdateReceipt)]
                refused = [outcome for outcome in (first, second) if not isinstance(outcome, UpdateReceipt)]
                for refusal in refused:
                    self.assertIsInstance(refusal, RoundConflict)
                    self.assertIn("was discarded", str(refusal))
                self.assertEqual(len(stored), deleted)
                self.assertEqual({}, self.store.updates(round_id))
                with self.assertRaises(RoundConflict):
                    other.put(record, "hospital-c", self.upload(), self.kind)
        self.assertEqual([], self.store.pending_rounds())
        self.assertEqual([], other.pending_rounds())

    def test_two_leases_putting_one_site_update_store_one(self) -> None:
        other = self.reloaded()
        updates = [{"bias": float(number), "w.x1": 0.0, "w.x2": 0.0} for number in range(8)]
        outcomes = run_together(
            [lambda store=(self.store if number % 2 else other), update=update:
             store.put(self.record, "hospital-a", self.upload(update), self.kind)
             for number, update in enumerate(updates)]
        )
        stored = [outcome for outcome in outcomes if isinstance(outcome, UpdateReceipt)]
        refused = [outcome for outcome in outcomes if isinstance(outcome, RoundConflict)]
        self.assertEqual((1, 7), (len(stored), len(refused)), outcomes)
        for refusal in refused:
            self.assertIn("already stored", str(refusal))
        kept = self.store.get(self.record.manifest.round_id, "hospital-a")
        self.assertEqual(stored[0].digest, kept.digest)
        self.assertEqual(kept.digest, self.kind.update_digest(kept.update))


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class PostgresRoundRecoveryTests(PostgresTables, unittest.TestCase):
    """Recovery after a restart, with the round and enrolment stores in Postgres and the real planner."""

    def controller(self) -> tuple[FederatedRoundController, PostgresRoundRepository]:
        rounds = PostgresRoundRepository(self.lease, self.role, self.table("rounds"))
        engine = InProcessFederationEngine()
        controller = FederatedRoundController(
            engine=engine, sites=PostgresEnrolmentStore(self.lease, self.role, self.table("enrolments")), rounds=rounds
        )
        for site_id in SITES:
            controller.register_site(build_registration(site_id))
        digest = controller.digest(build_manifest())
        for site_id in SITES:
            engine.register_worker(build_worker(site_id, expected_digest=digest))
        return controller, rounds

    def test_a_collecting_round_with_a_malformed_record_is_quarantined_and_a_new_round_still_dispatches(self) -> None:
        controller, rounds = self.controller()
        self.assertEqual("collecting", controller.dispatch_round(build_manifest()).status)
        query = sql.SQL("UPDATE {} SET record = %s WHERE round_id = %s").format(self.identifier("rounds"))
        with self.lease.acting_as(self.role) as conn:
            conn.execute(query, (Json({"manifest": "broken"}), "round-0042"))

        # A restarted GMA builds a new controller on the same tables.
        restarted, rounds = self.controller()
        report = restarted.recover(updates_kept=False, now=time.time())
        [outcome] = report.rounds
        self.assertEqual(
            ("round-0042", "collecting", "training", False, "close", "unreadable_record"),
            (outcome.round_id, outcome.status, outcome.phase, outcome.readable, outcome.verdict, outcome.reason),
        )
        self.assertEqual([], rounds.unfinished())
        with self.lease.acting_as(self.role) as conn:
            row = conn.execute(
                sql.SQL("SELECT status, closed_reason FROM {} WHERE round_id = %s").format(self.identifier("rounds")),
                ("round-0042",),
            ).fetchone()
        self.assertEqual(("closed", outcome.message), (row["status"], row["closed_reason"]))

        record = restarted.dispatch_round(build_manifest(round_id="round-0043"))
        self.assertEqual("collecting", record.status)
        self.assertEqual(list(SITES), [result.site_id for result in record.results])
        with self.assertLogs(STORE_LOGGER, "WARNING"):
            self.assertEqual(["round-0043"], [item.manifest.round_id for item in restarted.list_rounds()])
        with self.assertRaises(RoundConflict):
            rounds.create(training_record(round_id="round-0042"))

    def test_rounds_whose_records_python_cannot_decode_are_quarantined_and_a_new_round_still_dispatches(self) -> None:
        """A record too deep, with too many digits, or holding 1e400 must not stop recovery, collecting or dispatched."""
        controller, _ = self.controller()
        damaged: dict[str, tuple[str, str]] = {}
        for status in ("collecting", "dispatched"):
            for number, damage in enumerate(undecodable_records("round-x"), start=1):
                round_id = f"round-{status}-{number}"
                self.assertEqual("collecting", controller.dispatch_round(build_manifest(round_id=round_id)).status)
                damaged[round_id] = (status, damage)
        query = sql.SQL("UPDATE {} SET record = %s, status = %s WHERE round_id = %s").format(self.identifier("rounds"))
        with self.lease.acting_as(self.role) as conn:
            for round_id, (status, damage) in damaged.items():
                conn.execute(query, (raw_json(undecodable_records(round_id)[damage]), status, round_id))

        # A restarted GMA builds a new controller on the same tables.
        restarted, rounds = self.controller()
        self.assertEqual({round_id: False for round_id in damaged}, {item.round_id: item.readable for item in rounds.unfinished()})
        report = restarted.recover(updates_kept=False, now=time.time())
        self.assertEqual(
            {round_id: (status, False, "close", "unreadable_record") for round_id, (status, _) in damaged.items()},
            {outcome.round_id: (outcome.status, outcome.readable, outcome.verdict, outcome.reason) for outcome in report.rounds},
        )
        self.assertEqual([], rounds.unfinished())
        for round_id in damaged:
            with self.subTest(round=round_id, damage=damaged[round_id][1]):
                with self.assertRaises(StoreNotSetUpError) as raised:
                    rounds.get(round_id)
                self.assertIn(round_id, str(raised.exception))
                with self.assertRaises(RoundConflict):
                    rounds.create(training_record(round_id=round_id))

        record = restarted.dispatch_round(build_manifest(round_id="round-0043"))
        self.assertEqual("collecting", record.status)
        with self.assertLogs(STORE_LOGGER, "WARNING"):
            self.assertEqual(["round-0043"], [item.manifest.round_id for item in restarted.list_rounds(limit=50)])

    def study(self) -> tuple[Any, Any, PostgresUpdateStore]:
        """A GMA with the HTTP engine and fixture LMAs, with every round store in this database.

        Each call is a new process's view: a new connection, new stores, a new
        GMA and new LMAs on the same tables.
        """
        lease = self.second_lease()
        updates = PostgresUpdateStore(lease, self.update_role, self.table("updates"), coordinator_role=self.role)
        study = build_study(
            registry=toy_registry(),
            stores={
                "rounds": PostgresRoundRepository(lease, self.role, self.table("rounds")),
                "enrolments": PostgresEnrolmentStore(lease, self.role, self.table("enrolments")),
                "updates": updates,
            },
        )
        self.addCleanup(study.close)
        gma = study.gma()
        digests = register_models(gma)
        gma.refresh_sites()
        return study, digests, updates

    def test_a_training_round_with_a_malformed_record_is_quarantined_its_updates_deleted_and_a_new_round_runs(
        self,
    ) -> None:
        """With the update store in Postgres too: recovery closes the round from its columns and deletes its updates."""
        study, digests, updates = self.study()
        manifest = toy_manifest("round-train-1", "training", INITIAL_VERSION, digests[INITIAL_VERSION])
        stored = study.gma().dispatch_round(manifest)
        self.assertEqual("collecting", stored.status)
        self.assertEqual(list(TOY_SITES), list(updates.updates("round-train-1")))
        query = sql.SQL("UPDATE {} SET record = %s WHERE round_id = %s").format(self.identifier("rounds"))
        with self.lease.acting_as(self.role) as conn:
            conn.execute(query, (Json({"manifest": "broken"}), "round-train-1"))
        study.close()

        # A restarted GMA: new connection, new stores, same tables.
        restarted, digests, updates = self.study()
        self.assertEqual(["round-train-1"], updates.pending_rounds())
        report = restarted.state.rounds.recover(updates_kept=True, now=time.time())
        [outcome] = report.rounds
        self.assertEqual(
            ("round-train-1", "collecting", False, "close", "unreadable_record"),
            (outcome.round_id, outcome.status, outcome.readable, outcome.verdict, outcome.reason),
        )
        self.assertEqual(([], {}), (updates.pending_rounds(), updates.updates("round-train-1")))
        with self.assertRaises(RoundConflict) as raised:
            updates.put(stored, TOY_SITES[0], UpdateUpload(job_digest=stored.plan.round_digest, update=dict(UPDATE)),
                        ToyLinearKind())
        self.assertIn("was discarded", str(raised.exception))

        gma = restarted.gma()
        manifest = toy_manifest("round-train-2", "training", INITIAL_VERSION, digests[INITIAL_VERSION])
        self.assertEqual("collecting", gma.dispatch_round(manifest).status)
        self.assertEqual(["round-train-2"], updates.pending_rounds())
        self.assertEqual(list(TOY_SITES), list(updates.updates("round-train-2")))
        self.assertTrue(gma.aggregate("round-train-2").artifact_digest)
        self.assertEqual([], updates.pending_rounds())


class PostgresControllerFactory:
    """Builds each controller on a rounds table and an enrolments table of its own.

    The tables are created from the DDL constants in a scratch database, as
    tenant alpha, which owns them, and the stores act as alpha. A test that
    builds two controllers gets two pairs of tables, as it gets two pairs of
    memory stores in ``test_federation.py``.
    """

    db: ScratchDatabase

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.db = started_scratch_database()
        cls.numbers = itertools.count(1)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.stop()
        super().tearDownClass()

    def setUp(self) -> None:
        self.lease = KeptConnection(self.db.dsn, name="coordinator")
        self.addCleanup(self.lease.close)
        super().setUp()

    def new_controller(self, engine: Any, *, slow_reads: bool = False) -> FederatedRoundController:
        role, number = self.db.alpha, next(self.numbers)
        rounds = create_table(self.lease, role, ROUND_TABLE_DDL, f"{role}.rounds_{number}")
        enrolments = create_table(self.lease, role, ENROLMENT_TABLE_DDL, f"{role}.enrolments_{number}")
        repository = SlowPostgresRoundRepository if slow_reads else PostgresRoundRepository
        return FederatedRoundController(
            engine=engine,
            sites=PostgresEnrolmentStore(self.lease, role, enrolments),
            rounds=repository(self.lease, role, rounds),
        )

    def assert_on_postgres(self, controller: FederatedRoundController) -> None:
        """The controller's stores are the Postgres twins, so the inherited tests do not run on memory."""
        slow = self.new_controller(InProcessFederationEngine(), slow_reads=True)
        # Private attributes: the wiring under test.
        self.assertIsInstance(controller._rounds, PostgresRoundRepository)  # noqa: SLF001
        self.assertIsInstance(controller._sites, PostgresEnrolmentStore)  # noqa: SLF001
        self.assertIsInstance(slow._rounds, SlowPostgresRoundRepository)  # noqa: SLF001


class PostgresRoundLifecycleTests(PostgresControllerFactory, federation_tests.RoundLifecycleTests):
    """``RoundLifecycleTests`` with every controller on Postgres stores."""

    def test_the_controller_keeps_its_state_in_postgres(self) -> None:
        self.assert_on_postgres(self.controller)


class PostgresRoundStateTests(PostgresControllerFactory, federation_tests.RoundStateTests):
    """``RoundStateTests`` with every controller on Postgres stores."""

    def test_the_controller_keeps_its_state_in_postgres(self) -> None:
        self.assert_on_postgres(self.controller)


class PostgresSiteRefusalTests(PostgresControllerFactory, federation_tests.SiteRefusalTests):
    """``SiteRefusalTests`` with every controller on Postgres stores."""

    def test_the_controller_keeps_its_state_in_postgres(self) -> None:
        self.assert_on_postgres(self._controller_with([]))


# --------------------------------------------------------------------------- #
# The tables the GMA README tells operators to create
# --------------------------------------------------------------------------- #

GMA_README = Path(__file__).resolve().parents[1] / "gma" / "README.md"


class ReadmeDdlTests(unittest.TestCase):
    """The README's SQL creates the tables these tests create, so an operator who copies it gets the same tables.

    The package has no migrations, so operators copy the README's SQL. The
    README names its tables ``coord.rounds``, ``coord.site_enrolments``,
    ``coord_updates.updates`` and ``coord.models``.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.sql = "\n".join(re.findall(r"```sql\n(.*?)```", GMA_README.read_text(encoding="utf-8"), flags=re.S))

    def test_each_round_state_table_is_the_one_its_constant_creates(self) -> None:
        for name, ddl, table in (
            ("ROUND_TABLE_DDL", ROUND_TABLE_DDL, "coord.rounds"),
            ("ENROLMENT_TABLE_DDL", ENROLMENT_TABLE_DDL, "coord.site_enrolments"),
            ("UPDATE_TABLE_DDL", UPDATE_TABLE_DDL, "coord_updates.updates"),
        ):
            with self.subTest(constant=name):
                self.assertIn(ddl.format(table=table), self.sql)

    def test_the_models_table_keeps_its_parameters_in_a_json_column(self) -> None:
        """``jsonb`` turns -0.0 into 0.0 and 1e16 into a whole number, so a stored digest would no longer match."""
        [models] = re.findall(r"create table coord\.models \((.*?)\n\);", self.sql, flags=re.S)
        self.assertIn("\n  params json not null,", models)
        self.assertEqual(1, self.sql.count(PARAMS_TO_JSON_SQL.format(table="coord.models")))

    def test_no_round_state_table_uses_jsonb(self) -> None:
        for table in ("coord.rounds", "coord.site_enrolments", "coord_updates.updates"):
            with self.subTest(table=table):
                [columns] = re.findall(rf"create table {re.escape(table)} \((.*?)\n\);", self.sql, flags=re.S)
                self.assertNotIn("jsonb", columns)


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


class NonFiniteRequestContract:
    """A GMA answers 422 to a request that holds NaN or an infinite number, and changes no round.

    Python reads ``NaN`` and ``Infinity`` in a JSON body, but a JSON answer
    cannot hold them, so FastAPI's own 422 answer would fail with a 500. A
    subclass implements ``stores()``: the round and enrolment stores the GMA
    gets.
    """

    def stores(self) -> dict[str, Any]:
        raise NotImplementedError

    @staticmethod
    def post(client: TestClient, path: str, body: dict[str, Any]) -> Any:
        """POST ``body`` as JSON text that may hold ``NaN`` and ``Infinity``, as ``json.dumps`` writes them."""
        return client.post(path, content=json.dumps(body), headers={"content-type": "application/json"})

    def assert_refused(self, answer: Any) -> None:
        self.assertEqual(422, answer.status_code, answer.text)
        self.assertTrue(any("finite number" in error["msg"] for error in answer.json()["detail"]), answer.text)
        for token in ("NaN", "Infinity"):
            self.assertNotIn(token, answer.text)

    def test_nan_in_a_manifest_or_a_result_answers_422_and_changes_no_round(self) -> None:
        stores = self.stores()
        for site_id in SITES:
            stores["enrolments"].save(build_registration(site_id))
        manifest = build_manifest().model_dump(mode="json")
        with TestClient(create_app(Settings(), GmaDeps(**stores))) as client:
            for name, mu in (("NaN", math.nan), ("infinity", math.inf)):
                with self.subTest(manifest=name):
                    body = {**manifest, "round_id": "round-nan", "aggregation": {"kind": "fedprox", "mu": mu}}
                    self.assert_refused(self.post(client, "/api/v1/federation/rounds", body))
                    self.assertEqual(404, client.get("/api/v1/federation/rounds/round-nan").status_code)

            self.assertEqual(202, client.post("/api/v1/federation/rounds", json=manifest).status_code)
            before = client.get("/api/v1/federation/rounds/round-0042").json()
            result = {"round_id": "round-0042", "site_id": "hospital-a", "participation": "completed"}
            for name, values in (
                ("NaN metric", {"metrics": {"auc": math.nan}}),
                ("infinite metric", {"metrics": {"auc": -math.inf}}),
                ("NaN norm", {"update_norm": math.nan}),
            ):
                with self.subTest(result=name):
                    path = "/api/v1/federation/rounds/round-0042/results"
                    self.assert_refused(self.post(client, path, {**result, **values}))
            self.assertEqual(before, client.get("/api/v1/federation/rounds/round-0042").json())
            self.assertEqual(["round-0042"], [item["manifest"]["round_id"] for item in client.get("/api/v1/federation/rounds").json()])


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class InMemoryNonFiniteRequestTests(NonFiniteRequestContract, unittest.TestCase):
    def stores(self) -> dict[str, Any]:
        return {"rounds": InMemoryRoundRepository(), "enrolments": InMemorySiteRegistry()}


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class PostgresNonFiniteRequestTests(PostgresTables, NonFiniteRequestContract, unittest.TestCase):
    def stores(self) -> dict[str, Any]:
        return {
            "rounds": PostgresRoundRepository(self.lease, self.role, self.table("rounds")),
            "enrolments": PostgresEnrolmentStore(self.lease, self.role, self.table("enrolments")),
        }


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
