"""The GMA's model registry and its two stores.

The registry keeps every model version a study uses: the versions an
application registers, and the candidates the GMA's engine makes from training
rounds. Every status change comes from the planner's
``dagentsc federation model lifecycle`` command; the registry applies the
changes the planner returns, in one atomic step per event.

Two store implementations run the same assertions:

- ``InMemoryModelStore``, which never skips;
- ``PostgresModelStore``, on the scratch database from
  ``test_postgres_roles.py``. It skips with ``database test skipped`` when
  ``DAGENTS_TEST_DATABASE_URL`` is not set.

The registry tests use the real planner and skip without it.
"""

from __future__ import annotations

import copy
import os
import threading
from typing import Any
import unittest
from unittest.mock import patch

try:
    import psycopg
    from psycopg import sql
except ModuleNotFoundError:  # pragma: no cover - the agents' requirements install it
    psycopg = None

from agents.common.domain.errors import ModelVerificationError, StoreNotSetUpError
from agents.common.domain.federation import (
    GateResult,
    ModelReleaseDecision,
    ModelStatusChange,
    ModelVersion,
    ModelVersionRegistration,
)
from agents.common.extensions import ExtensionRegistry
from agents.common.infrastructure.dagents_runner import PlannerUnavailableError
from agents.common.infrastructure.postgres_lease import KeptConnection
from agents.gma.application.model_registry import (
    InvalidModelVersion,
    ModelLifecycleRefused,
    ModelRegistry,
    ModelStatusConflict,
    ModelVersionConflict,
    ModelVersionExists,
    UnknownModelKind,
    UnknownModelVersion,
)
from agents.gma.infrastructure.postgres_models import InMemoryModelStore, PostgresModelStore
from agents.tests.fixtures.toy_extension import ToyExtension, ToyLinearKind, ToyThresholdKind
from agents.tests.test_governance import dagentsc_available
from agents.tests.test_postgres_roles import ScratchDatabase, admin_dsn_or_skip

LINEAR = ToyLinearKind()
RULE = ToyThresholdKind()
RULE_PARAMS = {"threshold": 0.0}
INITIAL = {"bias": 0.0, "w.x1": 0.0, "w.x2": 0.0}
TRAINED = {"bias": 0.125, "w.x1": 1.0 / 3.0, "w.x2": -0.000123456789}


def model(
    version: str, status: str = "candidate", *, kind: Any = LINEAR, params: dict | None = None, parent: str | None = None
) -> ModelVersion:
    params = dict(INITIAL if params is None else params)
    return ModelVersion(
        version=version,
        kind=kind.kind,
        digest=kind.parameter_digest(params),
        status=status,
        parent_version=parent,
        params=params,
    )


def added(version: str, status: str) -> ModelStatusChange:
    return ModelStatusChange(version=version, from_status=None, to_status=status)


def change(version: str, before: str, after: str) -> ModelStatusChange:
    return ModelStatusChange(version=version, from_status=before, to_status=after)


def decision(**overrides: Any) -> ModelReleaseDecision:
    payload: dict[str, Any] = {
        "action": "release",
        "guard_permitted": True,
        "gate_results": [GateResult(gate_id="accuracy", outcome="passed", observed=0.8, threshold=0.6)],
        "round_id": "study-training-2",
        "baseline_version": "rule-v1",
        "metrics_source": "rounds",
    }
    payload.update(overrides)
    return ModelReleaseDecision(**payload)


class ModelStoreContract:
    """What every model store does. A subclass sets ``self.store``."""

    store: Any

    def insert(self, version: str, status: str = "candidate", **kwargs: Any) -> ModelVersion:
        new = model(version, status, **kwargs)
        self.store.apply([added(version, status)], new=new)
        return new

    def test_a_new_version_is_stored_with_its_parameters(self) -> None:
        new = self.insert("toy-linear-init", params=TRAINED)
        stored = self.store.get("toy-linear-init")
        self.assertEqual(new.model_copy(update={"created_at": stored.created_at}), stored)
        self.assertEqual(TRAINED, stored.params)
        self.assertIsInstance(stored.params["w.x1"], float)
        self.assertTrue(stored.created_at.endswith("+00:00"))
        self.assertIsNone(stored.release_decision)
        self.assertIsNone(self.store.get("missing"))

    def test_inserting_a_stored_version_again_is_refused(self) -> None:
        self.insert("toy-linear-init")
        with self.assertRaises(ModelVersionExists):
            self.insert("toy-linear-init", params=TRAINED)
        self.assertEqual(INITIAL, self.store.get("toy-linear-init").params)

    def test_a_version_whose_parent_is_unknown_is_refused(self) -> None:
        with self.assertRaises(UnknownModelVersion):
            self.insert("toy-linear+round-1", parent="missing")
        self.assertIsNone(self.store.get("toy-linear+round-1"))

    def test_versions_are_listed_newest_first(self) -> None:
        for version in ("v1", "v2", "v3"):
            self.insert(version)
        self.assertEqual(["v3", "v2", "v1"], [item.version for item in self.store.list(limit=10)])
        self.assertEqual(["v3", "v2"], [item.version for item in self.store.list(limit=2)])

    def test_the_released_version_is_found(self) -> None:
        self.assertIsNone(self.store.released())
        self.insert("rule-v1", "released", kind=RULE, params=RULE_PARAMS)
        self.insert("toy-linear-init")
        self.assertEqual("rule-v1", self.store.released().version)

    def test_status_changes_apply_together_with_a_new_version(self) -> None:
        self.insert("toy-linear-init")
        new = model("toy-linear+round-1", params=TRAINED, parent="toy-linear-init")
        self.store.apply(
            [change("toy-linear-init", "candidate", "superseded"), added("toy-linear+round-1", "candidate")], new=new
        )
        self.assertEqual("superseded", self.store.get("toy-linear-init").status)
        self.assertEqual("toy-linear-init", self.store.get("toy-linear+round-1").parent_version)

    def test_a_status_that_changed_meanwhile_refuses_every_change(self) -> None:
        """A change names the status it expects, so two requests cannot both win."""
        self.insert("toy-linear-init", "rejected")
        new = model("toy-linear+round-1", parent="toy-linear-init")
        with self.assertRaises(ModelStatusConflict):
            self.store.apply(
                [change("toy-linear-init", "candidate", "superseded"), added("toy-linear+round-1", "candidate")],
                new=new,
            )
        self.assertEqual("rejected", self.store.get("toy-linear-init").status)
        self.assertIsNone(self.store.get("toy-linear+round-1"))

    def test_an_approval_swaps_the_release_in_one_step(self) -> None:
        self.insert("rule-v1", "released", kind=RULE, params=RULE_PARAMS)
        self.insert("toy-linear+round-2")
        self.store.apply(
            [change("toy-linear+round-2", "candidate", "released"), change("rule-v1", "released", "superseded")]
        )
        self.assertEqual("toy-linear+round-2", self.store.released().version)
        self.assertEqual("superseded", self.store.get("rule-v1").status)

    def test_a_second_release_is_refused(self) -> None:
        self.insert("rule-v1", "released", kind=RULE, params=RULE_PARAMS)
        with self.assertRaises(ModelStatusConflict):
            self.insert("rule-v2", "released", kind=RULE, params={"threshold": 0.5})
        self.insert("toy-linear+round-2")
        with self.assertRaises(ModelStatusConflict):
            self.store.apply([change("toy-linear+round-2", "candidate", "released")])
        self.assertEqual("rule-v1", self.store.released().version)
        self.assertEqual("candidate", self.store.get("toy-linear+round-2").status)

    def test_a_change_to_an_unknown_version_is_refused(self) -> None:
        with self.assertRaises(ModelStatusConflict):
            self.store.apply([change("missing", "candidate", "rejected")])

    def test_a_release_decision_is_stored_with_its_time_and_replaced(self) -> None:
        self.insert("toy-linear+round-2")
        first = decision(action="another_round")
        self.store.apply([], decision=("toy-linear+round-2", first))
        stored = self.store.get("toy-linear+round-2").release_decision
        self.assertEqual(first.model_copy(update={"decided_at": stored.decided_at}), stored)
        self.assertTrue(stored.decided_at.endswith("+00:00"))
        self.store.apply(
            [change("toy-linear+round-2", "candidate", "rejected")],
            decision=("toy-linear+round-2", decision(action="reject")),
        )
        replaced = self.store.get("toy-linear+round-2")
        self.assertEqual("reject", replaced.release_decision.action)
        self.assertEqual("rejected", replaced.status)

    def test_a_decision_without_a_baseline_or_source_keeps_them_empty(self) -> None:
        self.insert("toy-linear+round-2")
        self.store.apply([], decision=("toy-linear+round-2", decision(baseline_version=None, metrics_source=None)))
        stored = self.store.get("toy-linear+round-2").release_decision
        self.assertIsNone(stored.baseline_version)
        self.assertIsNone(stored.metrics_source)

    def test_a_decision_for_an_unknown_version_is_refused(self) -> None:
        with self.assertRaises(UnknownModelVersion):
            self.store.apply([], decision=("missing", decision()))

    def test_invalid_names_are_refused(self) -> None:
        for version in ("", "v" * 201):
            with self.subTest(length=len(version)):
                with self.assertRaises(InvalidModelVersion):
                    invalid = model("v").model_copy(update={"version": version})
                    self.store.apply([added(version, "candidate")], new=invalid)

    def test_a_list_limit_below_one_is_refused(self) -> None:
        for limit in (0, -1, True):
            with self.subTest(limit=limit):
                with self.assertRaises(ValueError):
                    self.store.list(limit=limit)


class InMemoryModelStoreTests(ModelStoreContract, unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryModelStore()

    def tamper(self, version: str, params: dict) -> None:
        self.store._rows[version]["params"] = copy.deepcopy(params)  # noqa: SLF001 - simulates a changed row

    def test_stored_parameters_are_copies(self) -> None:
        self.insert("toy-linear-init")
        self.store.get("toy-linear-init").params["bias"] = 9.0
        self.assertEqual(INITIAL, self.store.get("toy-linear-init").params)


MODELS_TABLE = """
CREATE TABLE {table} (
    version        text PRIMARY KEY CHECK (length(version) BETWEEN 1 AND 200),
    kind           text NOT NULL CHECK (length(kind) BETWEEN 1 AND 64),
    params         jsonb NOT NULL,
    digest         text NOT NULL CHECK (length(digest) BETWEEN 1 AND 200),
    parent_version text REFERENCES {table} (version),
    created_at     timestamptz NOT NULL DEFAULT now(),
    status         text NOT NULL CHECK (status IN ('candidate', 'released', 'rejected', 'superseded')),
    release_decision jsonb,
    decided_at     timestamptz,
    CONSTRAINT models_release_decision_check CHECK (release_decision IS NULL OR (
        jsonb_typeof(release_decision) = 'object'
        AND jsonb_typeof(release_decision -> 'action') = 'string'
        AND jsonb_typeof(release_decision -> 'guard_permitted') = 'boolean'
        AND jsonb_typeof(release_decision -> 'gate_results') = 'array')),
    CONSTRAINT models_decision_has_time CHECK ((release_decision IS NULL) = (decided_at IS NULL))
)
"""


class PostgresModelsTestCase(unittest.TestCase):
    """A models table in the scratch database, owned by tenant alpha, emptied before each test.

    The table has the columns, checks and one-release index of the healthcare
    demo's ``coordinator.models`` (migration ``0002``).
    """

    db: ScratchDatabase

    @classmethod
    def setUpClass(cls) -> None:
        cls.db = ScratchDatabase(admin_dsn_or_skip())
        try:
            cls.db.start()
            cls.table = f"{cls.db.alpha}.models"
            identifier = sql.Identifier(cls.db.alpha, "models")
            with psycopg.connect(cls.db.dsn, autocommit=True) as conn, conn.transaction():
                conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(cls.db.alpha)))
                conn.execute(sql.SQL(MODELS_TABLE).format(table=identifier))
                conn.execute(
                    sql.SQL("CREATE UNIQUE INDEX models_one_release ON {} ((true)) WHERE status = 'released'").format(
                        identifier
                    )
                )
        except BaseException:
            cls.db.stop()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.stop()

    def setUp(self) -> None:
        self.lease = KeptConnection(self.db.dsn, name="coordinator")
        self.addCleanup(self.lease.close)
        with self.lease.acting_as(self.db.alpha) as conn:
            conn.execute(sql.SQL("TRUNCATE {}").format(sql.Identifier(self.db.alpha, "models")))
        self.store = PostgresModelStore(self.lease, self.db.alpha, self.table)

    def tamper(self, version: str, params: dict) -> None:
        from psycopg.types.json import Jsonb

        with self.lease.acting_as(self.db.alpha) as conn:
            conn.execute(
                sql.SQL("UPDATE {} SET params = %s WHERE version = %s").format(sql.Identifier(self.db.alpha, "models")),
                (Jsonb(params), version),
            )


class PostgresModelStoreTests(ModelStoreContract, PostgresModelsTestCase):
    def test_another_role_cannot_read_the_table(self) -> None:
        """Every operation acts as the store's role, so a role without a grant reads nothing."""
        self.insert("toy-linear-init")
        other = PostgresModelStore(self.lease, self.db.beta, self.table)
        with self.assertRaises(StoreNotSetUpError) as raised:
            other.get("toy-linear-init")
        self.assertNotIn(self.db.dsn, str(raised.exception))

    def test_a_table_name_that_is_not_a_plain_identifier_is_refused(self) -> None:
        for table in ("models; DROP TABLE x", "A.models", "a.b.c", ""):
            with self.subTest(table=table):
                with self.assertRaises(ValueError):
                    PostgresModelStore(self.lease, self.db.alpha, table)

    def test_rows_written_with_the_existing_decision_keys_are_read(self) -> None:
        """A decision stored before ``metrics_source`` existed reads with the source empty."""
        from psycopg.types.json import Jsonb

        self.insert("toy-linear+round-2")
        older = {
            "action": "release",
            "guard_permitted": True,
            "gate_results": [{"gate_id": "accuracy", "outcome": "passed", "observed": 0.8, "detail": "ok"}],
            "round_id": "older-training-2",
            "baseline_version": "rule-v1",
        }
        with self.lease.acting_as(self.db.alpha) as conn:
            conn.execute(
                sql.SQL("UPDATE {} SET release_decision = %s, decided_at = now() WHERE version = %s").format(
                    sql.Identifier(self.db.alpha, "models")
                ),
                (Jsonb(older), "toy-linear+round-2"),
            )
        stored = self.store.get("toy-linear+round-2").release_decision
        self.assertEqual("rule-v1", stored.baseline_version)
        self.assertIsNone(stored.metrics_source)


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class ModelRegistryContract:
    """The registry's behaviour, the same over both stores, with the real planner.

    A subclass sets ``self.store`` and defines ``reset`` and ``tamper(version, params)``.
    """

    store: Any

    def new_registry(self) -> ModelRegistry:
        kinds = ExtensionRegistry()
        kinds.register(ToyExtension())
        return ModelRegistry(self.store, kinds)

    def seeded(self) -> ModelRegistry:
        """A registry holding the released rule ``rule-v1`` and the candidate ``toy-linear-init``."""
        registry = self.new_registry()
        registry.register(
            "rule-v1", ModelVersionRegistration(kind="toy_threshold", params=RULE_PARAMS, requested_status="released")
        )
        registry.register("toy-linear-init", ModelVersionRegistration(kind="toy_linear", params=INITIAL))
        return registry

    def trained(self, registry: ModelRegistry, round_number: int = 1, parent: str = "toy-linear-init") -> ModelVersion:
        return registry.register_candidate(f"toy-linear+round-{round_number}", "toy_linear", TRAINED, parent)

    # Registration -------------------------------------------------------------

    def test_the_first_registration_may_be_released(self) -> None:
        registry = self.new_registry()
        result = registry.register(
            "rule-v1", ModelVersionRegistration(kind="toy_threshold", params=RULE_PARAMS, requested_status="released")
        )
        self.assertTrue(result.created)
        self.assertEqual("released", result.model.status)
        self.assertEqual(RULE.parameter_digest(RULE_PARAMS), result.model.digest)
        self.assertIsNone(result.model.params)
        self.assertEqual("rule-v1", registry.released().version)

    def test_registering_the_same_content_again_changes_nothing(self) -> None:
        """The application registers its versions at every start; a later status must stay."""
        registry = self.seeded()
        self.trained(registry)
        again = registry.register("toy-linear-init", ModelVersionRegistration(kind="toy_linear", params=INITIAL))
        self.assertFalse(again.created)
        self.assertEqual("superseded", again.model.status)
        released_again = registry.register(
            "rule-v1", ModelVersionRegistration(kind="toy_threshold", params=RULE_PARAMS, requested_status="released")
        )
        self.assertFalse(released_again.created)

    def test_registering_different_content_under_a_stored_name_is_refused(self) -> None:
        registry = self.seeded()
        with self.assertRaises(ModelVersionConflict):
            registry.register("toy-linear-init", ModelVersionRegistration(kind="toy_linear", params=TRAINED))
        with self.assertRaises(ModelVersionConflict):
            registry.register("toy-linear-init", ModelVersionRegistration(kind="toy_threshold", params=RULE_PARAMS))
        self.assertEqual(LINEAR.parameter_digest(INITIAL), registry.get("toy-linear-init").digest)

    def test_a_second_release_is_refused_by_the_lifecycle_rule(self) -> None:
        registry = self.seeded()
        with self.assertRaises(ModelLifecycleRefused) as raised:
            registry.register(
                "rule-v2",
                ModelVersionRegistration(kind="toy_threshold", params={"threshold": 0.5}, requested_status="released"),
            )
        self.assertEqual("already_released", raised.exception.refusal)
        self.assertIn("rule-v1", str(raised.exception))
        self.assertIsNone(registry.get("rule-v2"))

    def test_an_unknown_kind_or_invalid_parameters_are_refused(self) -> None:
        registry = self.new_registry()
        with self.assertRaises(UnknownModelKind):
            registry.register("x-v1", ModelVersionRegistration(kind="no_such_kind", params={}))
        with self.assertRaises(ModelVerificationError):
            registry.register("x-v1", ModelVersionRegistration(kind="toy_linear", params={"bias": 0.0}))
        with self.assertRaises(InvalidModelVersion):
            registry.register("", ModelVersionRegistration(kind="toy_linear", params=INITIAL))
        self.assertIsNone(registry.get("x-v1"))

    def test_without_the_planner_nothing_is_registered(self) -> None:
        """The lifecycle rule decides every status, so a missing planner changes nothing."""
        registry = self.new_registry()
        with patch.dict(os.environ, {"DAGENTSC_BIN": "/nonexistent/dagentsc"}):
            with self.assertRaises(PlannerUnavailableError):
                registry.register("toy-linear-init", ModelVersionRegistration(kind="toy_linear", params=INITIAL))
        self.assertIsNone(registry.get("toy-linear-init"))

    # Candidates ----------------------------------------------------------------

    def test_a_candidate_supersedes_its_candidate_parent(self) -> None:
        registry = self.seeded()
        first = self.trained(registry)
        self.assertEqual("candidate", first.status)
        self.assertEqual("toy-linear-init", first.parent_version)
        self.assertEqual(LINEAR.parameter_digest(TRAINED), first.digest)
        self.assertEqual("superseded", registry.get("toy-linear-init").status)
        self.trained(registry, 2, parent=first.version)
        self.assertEqual("superseded", registry.get(first.version).status)

    def test_a_candidate_leaves_a_released_parent_released(self) -> None:
        registry = self.new_registry()
        registry.register(
            "toy-linear-init",
            ModelVersionRegistration(kind="toy_linear", params=INITIAL, requested_status="released"),
        )
        self.trained(registry)
        self.assertEqual("released", registry.get("toy-linear-init").status)

    def test_registering_the_same_candidate_again_changes_nothing(self) -> None:
        registry = self.seeded()
        first = self.trained(registry)
        self.assertEqual(first, self.trained(registry))
        with self.assertRaises(ModelVersionConflict):
            registry.register_candidate(first.version, "toy_linear", INITIAL, "toy-linear-init")

    def test_a_candidate_with_an_unknown_parent_is_refused(self) -> None:
        registry = self.seeded()
        with self.assertRaises(UnknownModelVersion):
            registry.register_candidate("toy-linear+round-1", "toy_linear", TRAINED, "missing")

    # Release decisions -----------------------------------------------------------

    def test_a_decision_that_passes_keeps_the_candidate(self) -> None:
        registry = self.seeded()
        candidate = self.trained(registry)
        stored = registry.record_release_decision(candidate.version, decision())
        self.assertEqual("candidate", stored.status)
        self.assertEqual("rounds", stored.release_decision.metrics_source)
        self.assertEqual("rule-v1", stored.release_decision.baseline_version)
        self.assertIsNotNone(stored.release_decision.decided_at)

    def test_a_decision_that_fails_rejects_the_candidate(self) -> None:
        registry = self.seeded()
        for name, failing in {"gates": decision(action="reject"), "guard": decision(guard_permitted=False)}.items():
            with self.subTest(name):
                version = f"toy-linear+{name}"
                registry.register_candidate(version, "toy_linear", TRAINED, "toy-linear-init")
                self.assertEqual("rejected", registry.record_release_decision(version, failing).status)
                self.assertEqual(failing.action, registry.get(version).release_decision.action)

    def test_a_decision_for_a_version_that_is_not_a_candidate_is_refused(self) -> None:
        registry = self.seeded()
        with self.assertRaises(ModelLifecycleRefused) as raised:
            registry.record_release_decision("rule-v1", decision())
        self.assertEqual("not_candidate", raised.exception.refusal)
        self.assertIsNone(registry.get("rule-v1").release_decision)
        with self.assertRaises(UnknownModelVersion):
            registry.record_release_decision("missing", decision())

    # Approval ------------------------------------------------------------------------

    def test_an_approval_releases_the_candidate_and_supersedes_the_release(self) -> None:
        registry = self.seeded()
        candidate = self.trained(registry)
        registry.record_release_decision(candidate.version, decision())
        result = registry.approve(candidate.version)
        self.assertEqual(candidate.version, result.released.version)
        self.assertEqual("released", result.released.status)
        self.assertIsNone(result.released.params)
        self.assertEqual("rule-v1", result.previous)
        self.assertEqual("superseded", registry.get("rule-v1").status)
        self.assertEqual(candidate.version, registry.released().version)

    def test_each_approval_refusal_comes_from_the_planner_with_the_demo_wording(self) -> None:
        version = "toy-linear+round-1"
        cases = {
            "no_decision": (None, f"{version} has no release decision; only a pilot's candidate can be approved"),
            "gates_decided": (
                decision(action="another_round"),
                f"the release gates decided another_round for {version}",
            ),
            "guard_denied": (decision(guard_permitted=False), f"the release Guard did not permit {version}"),
            "metrics_not_from_rounds": (
                decision(metrics_source="request"),
                f"the release decision for {version} did not take its metrics from the rounds",
            ),
            "baseline_changed": (
                decision(baseline_version="rule-v0"),
                f"{version} was checked against rule-v0, but rule-v1 is now in use. Run the pilot again.",
            ),
        }
        for refusal, (stored, reason) in cases.items():
            with self.subTest(refusal):
                self.reset()
                registry = self.seeded()
                self.trained(registry)
                if stored is not None:
                    # Stored directly: record_release_decision would reject a failing candidate.
                    self.store.apply([], decision=(version, stored))
                with self.assertRaises(ModelLifecycleRefused) as raised:
                    registry.approve(version)
                self.assertEqual(refusal, raised.exception.refusal)
                self.assertEqual(reason, raised.exception.reason)
                self.assertEqual(reason, str(raised.exception))
                self.assertEqual("candidate", registry.get(version).status)
                self.assertEqual("rule-v1", registry.released().version)

    def test_a_rejected_candidate_cannot_be_approved(self) -> None:
        registry = self.seeded()
        candidate = self.trained(registry)
        registry.record_release_decision(candidate.version, decision(action="reject"))
        with self.assertRaises(ModelLifecycleRefused) as raised:
            registry.approve(candidate.version)
        self.assertEqual("gates_decided", raised.exception.refusal)
        self.store.apply([], decision=(candidate.version, decision()))
        with self.assertRaises(ModelLifecycleRefused) as raised:
            registry.approve(candidate.version)
        self.assertEqual("not_candidate", raised.exception.refusal)
        self.assertEqual(f"{candidate.version} is rejected, not a candidate", raised.exception.reason)

    def test_approving_an_unknown_version_is_refused(self) -> None:
        with self.assertRaises(UnknownModelVersion):
            self.seeded().approve("missing")

    def test_without_the_planner_an_approval_changes_nothing(self) -> None:
        registry = self.seeded()
        candidate = self.trained(registry)
        registry.record_release_decision(candidate.version, decision())
        with patch.dict(os.environ, {"DAGENTSC_BIN": "/nonexistent/dagentsc"}):
            with self.assertRaises(PlannerUnavailableError):
                registry.approve(candidate.version)
        self.assertEqual("rule-v1", registry.released().version)
        self.assertEqual("candidate", registry.get(candidate.version).status)

    def test_two_approvals_at_once_release_one_version(self) -> None:
        registry = self.seeded()
        versions = []
        for number in (1, 2):
            version = f"toy-linear+pair-{number}"
            registry.register_candidate(version, "toy_linear", TRAINED, "toy-linear-init")
            registry.record_release_decision(version, decision())
            versions.append(version)
        barrier = threading.Barrier(2)
        outcomes: dict[str, Any] = {}

        def approve(version: str) -> None:
            barrier.wait()
            try:
                outcomes[version] = registry.approve(version)
            except (ModelStatusConflict, ModelLifecycleRefused) as error:
                outcomes[version] = error

        threads = [threading.Thread(target=approve, args=(version,)) for version in versions]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        approved = [version for version, outcome in outcomes.items() if not isinstance(outcome, Exception)]
        self.assertEqual(1, len(approved), outcomes)
        self.assertEqual(approved[0], registry.released().version)
        self.assertEqual(1, sum(1 for item in registry.list(limit=10).versions if item.status == "released"))

    # Reads ----------------------------------------------------------------------------

    def test_the_list_shows_the_model_in_use_and_no_parameters(self) -> None:
        registry = self.seeded()
        self.trained(registry)
        listing = registry.list(limit=10)
        self.assertEqual("rule-v1", listing.model_in_use.version)
        self.assertEqual(
            ["toy-linear+round-1", "toy-linear-init", "rule-v1"], [item.version for item in listing.versions]
        )
        self.assertTrue(all(item.params is None for item in [listing.model_in_use, *listing.versions]))
        self.assertEqual(1, len(registry.list(limit=1).versions))

    def test_get_leaves_out_the_parameters(self) -> None:
        registry = self.seeded()
        self.assertIsNone(registry.get("toy-linear-init").params)
        self.assertIsNone(registry.get("missing"))
        with self.assertRaises(UnknownModelVersion):
            registry.require("missing")

    def test_the_released_version_comes_with_checked_parameters(self) -> None:
        registry = self.seeded()
        released = registry.released()
        self.assertEqual(RULE_PARAMS, released.params)
        self.assertEqual(RULE.parameter_digest(RULE_PARAMS), released.digest)
        self.assertIsNone(registry.released(with_params=False).params)

    def test_a_released_version_whose_parameters_changed_is_refused(self) -> None:
        registry = self.seeded()
        self.tamper("rule-v1", {"threshold": 0.9})
        with self.assertRaises(ModelVerificationError):
            registry.released()

    def test_the_released_version_is_read_from_the_store_each_time(self) -> None:
        """Another process can approve a version; the next read must see it."""
        registry = self.seeded()
        other = self.new_registry()
        candidate = self.trained(other)
        other.record_release_decision(candidate.version, decision())
        self.assertEqual("rule-v1", registry.released().version)
        other.approve(candidate.version)
        self.assertEqual(candidate.version, registry.released().version)

    def test_load_checks_the_digests(self) -> None:
        registry = self.seeded()
        loaded = registry.load("toy-linear-init", LINEAR.parameter_digest(INITIAL))
        self.assertEqual(("toy-linear-init", "toy_linear", INITIAL), (loaded.version, loaded.kind, loaded.params))
        self.assertEqual(LINEAR.parameter_digest(INITIAL), loaded.digest)
        self.assertEqual(loaded, registry.load("toy-linear-init"))
        with self.assertRaises(ModelVerificationError) as raised:
            registry.load("toy-linear-init", "sha256:other")
        self.assertIn("digest the round names", str(raised.exception))
        with self.assertRaises(ModelVerificationError):
            registry.load("missing")

    def test_load_refuses_parameters_that_changed_in_the_store(self) -> None:
        registry = self.seeded()
        self.tamper("toy-linear-init", TRAINED)
        with self.assertRaises(ModelVerificationError) as raised:
            registry.load("toy-linear-init")
        self.assertIn("do not match its stored digest", str(raised.exception))

    def test_load_reads_a_version_from_the_store_once_and_returns_copies(self) -> None:
        registry = self.seeded()
        reads: list[str] = []
        real_get = self.store.get

        def counting_get(version: str) -> Any:
            reads.append(version)
            return real_get(version)

        with patch.object(self.store, "get", side_effect=counting_get):
            first = registry.load("toy-linear-init")
            first.params["bias"] = 5.0
            second = registry.load("toy-linear-init")
        self.assertEqual(["toy-linear-init"], reads)
        self.assertEqual(INITIAL, second.params)

    def test_load_recomputes_the_digest_of_a_cached_copy(self) -> None:
        registry = self.seeded()
        registry.load("toy-linear-init")
        registry._params["toy-linear-init"][1]["bias"] = 3.0  # noqa: SLF001 - simulates a changed cache entry
        with self.assertRaises(ModelVerificationError):
            registry.load("toy-linear-init")


class InMemoryModelRegistryTests(ModelRegistryContract, unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryModelStore()

    def reset(self) -> None:
        self.store = InMemoryModelStore()

    def tamper(self, version: str, params: dict) -> None:
        self.store._rows[version]["params"] = copy.deepcopy(params)  # noqa: SLF001 - simulates a changed row


class PostgresModelRegistryTests(ModelRegistryContract, PostgresModelsTestCase):
    def reset(self) -> None:
        with self.lease.acting_as(self.db.alpha) as conn:
            conn.execute(sql.SQL("TRUNCATE {}").format(sql.Identifier(self.db.alpha, "models")))


if __name__ == "__main__":
    unittest.main()
