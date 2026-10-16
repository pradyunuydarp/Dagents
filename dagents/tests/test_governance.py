"""Tests for the GRAILS governance layer.

These run against the real ``dagentsc`` binary, because the whole point of the
split is that the decision comes from the typed planner. A test that stubbed the
planner would prove the Guard calls something, not that the framework's
governance works.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import sys
import threading
import unittest
import warnings

from dagents.common.application.ethical_guard import (
    DagentscRestrictionPlanner,
    EthicalGuard,
    InMemoryAuditLog,
)
from dagents.common.application.governance_service import GovernanceService
from dagents.common.domain.errors import ClassificationConflict
from dagents.common.domain.governance import (
    AuditRecord,
    DataClassification,
    KyuAttribute,
    Requester,
    RestrictionRequest,
)
from dagents.common.extensions import ExtensionRegistry
from dagents.common.infrastructure.dagents_runner import dagentsc_binary


#: Where ``dune build`` puts the planner, relative to the repository root.
DUNE_BUILT_BINARY = (
    pathlib.Path(__file__).resolve().parents[2]
    / "bindings"
    / "ocaml"
    / "_build"
    / "default"
    / "bin"
    / "dagentsc.exe"
)


def dagentsc_available() -> bool:
    """Whether the OCaml planner can actually be reached from this environment.

    Checks ``DAGENTSC_BIN``, then ``PATH``, then the dune build output. The last
    one matters: without it a developer who has built the OCaml layer, as the
    contributor guide instructs, would still see these tests skip, and a suite
    that skips its governance tests is green without having proved anything.
    """
    binary = dagentsc_binary()
    if os.path.isfile(binary) or shutil.which(binary) is not None:
        return True
    if DUNE_BUILT_BINARY.is_file():
        # Point the runner at it for the rest of the process, so the tests
        # exercise the same resolution path a service would.
        os.environ.setdefault("DAGENTSC_BIN", str(DUNE_BUILT_BINARY))
        return True
    # Say so rather than skipping in silence. These are the governance tests;
    # a suite that drops them quietly reports OK without having checked the
    # thing most worth checking.
    warnings.warn(
        "dagentsc was not found, so the governance and federation tests will skip. "
        "Build it with `opam exec -- dune build ./bin/dagentsc.exe` in bindings/ocaml.",
        RuntimeWarning,
        stacklevel=2,
    )
    return False


TRUSTED = Requester(
    requester_id="stroke-consortium-gma",
    requester_kind="coordinator",
    affiliation="stroke-consortium",
    stated_purpose="stroke_triage_research",
    compliance_history=0.9,
    attributes=[
        KyuAttribute(attribute_id="verified_identity", verified=True),
        KyuAttribute(attribute_id="signed_dua", verified=True),
    ],
)

UNTRUSTED = Requester(requester_id="unknown-caller", requester_kind="service")

#: Identified but unverified: enough for moderate trust, not enough for high.
UNTRUSTED_BUT_KNOWN = Requester(
    requester_id="claimed-caller",
    requester_kind="human",
    affiliation="unchecked",
    compliance_history=0.9,
    attributes=[KyuAttribute(attribute_id="claimed_identity", verified=False)],
)

CLASSIFICATION = DataClassification(
    classification_id="stroke-triage-v1",
    field_sensitivity={"nihss_total": "high", "age_band": "medium", "arrival_mode": "low"},
    default_sensitivity="high",
    regulations=["HIPAA"],
    minimum_cohort=3,
)

RECORDS = [
    {"nihss_total": 14.0, "age_band": "70-79", "arrival_mode": "ambulance", "mrn": "PHI-1"},
    {"nihss_total": 6.0, "age_band": "50-59", "arrival_mode": "walk_in", "mrn": "PHI-2"},
    {"nihss_total": 21.0, "age_band": "80-89", "arrival_mode": "ambulance", "mrn": "PHI-3"},
]


def request_for(**overrides) -> RestrictionRequest:
    """Build a restriction request, overriding one field at a time."""
    payload = {
        "request_id": "req-1",
        "boundary": "before_read",
        "requester": TRUSTED,
        "classification": CLASSIFICATION,
        "requested_fields": ["nihss_total", "age_band", "arrival_mode"],
        "granularity": "row",
        "cohort_size": len(RECORDS),
        "declared_purpose": "stroke_triage_research",
        "approved_purposes": ["stroke_triage_research"],
    }
    payload.update(overrides)
    return RestrictionRequest(**payload)


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class EthicalGuardTests(unittest.TestCase):
    """The Guard applied to real payloads through the real planner."""

    def setUp(self) -> None:
        self.guard = EthicalGuard(DagentscRestrictionPlanner(), InMemoryAuditLog())

    def test_guard_projects_only_requested_fields(self) -> None:
        """A field the request never asked for must not survive enforcement.

        The payload carries an MRN. Nothing asked for it, so nothing should get
        it: minimum-necessary is enforced by projection, not by trusting the
        caller to have filtered its own payload.
        """
        decision = self.guard.enforce(request_for(), RECORDS)
        self.assertTrue(decision.permitted)
        for record in decision.payload:
            self.assertNotIn("mrn", record)

    def test_guard_denies_when_cohort_is_below_the_floor(self) -> None:
        """A group too small to describe safely is refused, not shrunk."""
        decision = self.guard.enforce(
            request_for(request_id="small", granularity="column", cohort_size=2), RECORDS[:2]
        )
        self.assertFalse(decision.permitted)
        self.assertEqual(decision.payload, [])
        self.assertTrue(any("minimum_cohort" in v for v in decision.plan.violations))

    def test_guard_denies_an_unapproved_purpose(self) -> None:
        """Trust does not substitute for an approved purpose."""
        decision = self.guard.enforce(request_for(request_id="purpose", declared_purpose="marketing"), RECORDS)
        self.assertFalse(decision.permitted)
        self.assertIn("marketing", decision.message)

    def test_column_requests_return_an_aggregate_not_rows(self) -> None:
        """A column-level ask yields a group summary, and accounts for every field."""
        decision = self.guard.enforce(request_for(request_id="col", granularity="column"), RECORDS)
        self.assertTrue(decision.permitted)
        self.assertEqual(len(decision.payload), 1)
        summary = decision.payload[0]
        self.assertEqual(summary["record_count"], 3)
        for field in ("nihss_total", "age_band", "arrival_mode"):
            accounted = any(key.startswith(field) for key in summary) or field in decision.withheld_fields
            self.assertTrue(accounted, f"{field} was neither summarized nor withheld")

    def test_untrusted_requester_is_refused_high_sensitivity_data(self) -> None:
        """An unidentified caller gets nothing from a high-sensitivity field."""
        decision = self.guard.enforce(request_for(request_id="anon", requester=UNTRUSTED), RECORDS)
        self.assertFalse(decision.permitted)

    def test_model_update_egress_is_bounded(self) -> None:
        """An outbound contribution is clipped, never released as reported.

        The runner reports a norm of 7.5; the Guard must not let that leave
        unchanged, because an unbounded update is the federated equivalent of
        shipping the rows.
        """
        decision = self.guard.enforce(
            request_for(
                request_id="egress",
                boundary="before_send",
                requested_fields=["model_update"],
                granularity="model_update",
                cohort_size=140,
            ),
            [{"model_update": 7.5}],
        )
        self.assertTrue(decision.permitted)
        self.assertIn("model_update", decision.transformed_fields)
        self.assertLess(decision.payload[0]["model_update"], 7.5)

    def test_every_decision_is_audited_and_the_chain_verifies(self) -> None:
        """Permitted and denied alike leave a record, chained against tampering."""
        self.guard.enforce(request_for(request_id="a"), RECORDS, correlation_id="round-1")
        self.guard.enforce(request_for(request_id="b", declared_purpose="marketing"), RECORDS)
        records = self.guard.audit.list_recent()
        self.assertEqual(len(records), 2)
        self.assertTrue(self.guard.audit.verify_chain())
        self.assertEqual(records[0].previous_digest, records[1].digest)
        self.assertTrue(all(record.digest for record in records))

    def test_audit_chain_detects_tampering(self) -> None:
        """Editing a stored decision must invalidate the chain."""
        self.guard.enforce(request_for(request_id="a"), RECORDS)
        self.guard.enforce(request_for(request_id="b"), RECORDS)
        audit = self.guard.audit
        stored = audit._records  # noqa: SLF001 - reaching in is the point of the test
        stored[0] = stored[0].model_copy(update={"permitted": not stored[0].permitted})
        self.assertFalse(audit.verify_chain())


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class GuardRegressionTests(unittest.TestCase):
    """Defects found by review. Each one released data it should have protected."""

    def setUp(self) -> None:
        self.guard = EthicalGuard(DagentscRestrictionPlanner(), InMemoryAuditLog())
        self.records = [{"age": 71, "zip": "02139"}, {"age": 32, "zip": "02142"}, {"age": 55, "zip": "02138"}]

    def _request(self, **overrides) -> RestrictionRequest:
        payload = {
            "request_id": "regression",
            "boundary": "before_read",
            "requester": TRUSTED,
            "classification": DataClassification(
                classification_id="regression", default_sensitivity="medium", minimum_cohort=2
            ),
            "requested_fields": ["age", "zip"],
            "granularity": "row",
            "cohort_size": 3,
        }
        payload.update(overrides)
        return RestrictionRequest(**payload)

    def test_generalize_actually_coarsens_a_number(self) -> None:
        """The strategy parameter is a coarsening level, not a decimal precision.

        Read as decimal places it made level 1 a no-op on any value already
        recorded to one decimal, so exact ages were returned while the field was
        reported as transformed.
        """
        decision = self.guard.enforce(
            self._request(requester=UNTRUSTED_BUT_KNOWN, requested_fields=["age"]), self.records
        )
        strategies = {r.field: r.strategy for r in decision.plan.field_restrictions}
        self.assertTrue(strategies["age"].startswith("generalize"))
        returned = [record["age"] for record in decision.payload]
        self.assertNotIn(71, returned, "the exact age survived generalization")
        self.assertEqual(returned, [70, 30, 60])

    def test_an_aggregate_never_publishes_a_protected_field_s_extremes(self) -> None:
        """A min or a max over a group is one specific subject's exact value.

        Publishing both alongside record_count made two of three subjects'
        values directly recoverable from a summary that was supposed to
        describe only the group.
        """
        decision = self.guard.enforce(
            self._request(
                request_id="agg",
                granularity="column",
                classification=DataClassification(
                    classification_id="regression", default_sensitivity="high", minimum_cohort=2
                ),
            ),
            self.records,
        )
        summary = decision.payload[0]
        self.assertEqual(summary["record_count"], 3)
        self.assertNotIn("age_min", summary)
        self.assertNotIn("age_max", summary)
        self.assertIn("age_mean", summary)
        self.assertIsNotNone(summary["age_mean"], "an aggregated field must still report its mean")

    def test_a_suppressed_group_reports_no_field_as_both_withheld_and_transformed(self) -> None:
        """Those are two different claims, and a caller cannot act on both."""
        decision = self.guard.enforce(
            self._request(
                request_id="suppressed",
                granularity="column",
                cohort_size=10,
                classification=DataClassification(
                    classification_id="regression", default_sensitivity="high", minimum_cohort=10
                ),
            ),
            self.records[:2],
        )
        self.assertEqual(decision.payload, [])
        self.assertEqual(set(decision.withheld_fields), {"age", "zip"})
        self.assertEqual(set(decision.withheld_fields) & set(decision.transformed_fields), set())

    def test_an_all_withheld_request_does_not_return_the_record_count(self) -> None:
        """One empty dict per record hands back a cohort size nobody granted."""
        decision = self.guard.enforce(
            self._request(
                request_id="redacted",
                requester=UNTRUSTED_BUT_KNOWN,
                classification=DataClassification(
                    classification_id="regression", default_sensitivity="high", minimum_cohort=0
                ),
            ),
            self.records,
        )
        self.assertEqual(decision.payload, [])
        self.assertEqual(set(decision.withheld_fields), {"age", "zip"})

    def test_an_unknown_strategy_withholds_rather_than_passing_through(self) -> None:
        """An unrecognized strategy is not permission."""
        self.assertIsNone(EthicalGuard._transform(42.0, "not_a_strategy", 1.0, seed="s"))
        self.assertIsNone(EthicalGuard._transform(42.0, "aggregate_only", 20.0, seed="s"))

    def test_the_audit_digest_covers_obligations(self) -> None:
        """The duties the Guard committed to must not be rewritable in place."""
        request = self._request(request_id="audited")
        self.guard.enforce(request, self.records)
        self.guard.enforce(request.model_copy(update={"request_id": "audited-2"}), self.records)
        audit = self.guard.audit
        self.assertTrue(audit.verify_chain())
        stored = audit._records  # noqa: SLF001 - reaching in is the point of the test
        stored[0] = stored[0].model_copy(update={"obligations": ["nothing required"]})
        self.assertFalse(audit.verify_chain())

    def test_the_audit_digest_is_unambiguous_across_list_encodings(self) -> None:
        """Joining a list on a comma makes ["a,b"] and ["a","b"] hash alike."""
        from dagents.common.domain.governance import AuditRecord

        base = AuditRecord(
            audit_id="a", recorded_at=1, boundary="before_read", request_id="r",
            requester_id="x", decision="permit", permitted=True, granularity="row",
            filtering_score=0.0,
        )
        one = base.model_copy(update={"withheld_fields": ["a,b"]})
        two = base.model_copy(update={"withheld_fields": ["a", "b"]})
        self.assertNotEqual(InMemoryAuditLog._digest(one), InMemoryAuditLog._digest(two))


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class GovernanceServiceWiringTests(unittest.TestCase):
    """An injected guard must not leave the service reporting on an empty log."""

    def test_an_injected_guard_shares_its_audit_sink(self) -> None:
        guard = EthicalGuard(DagentscRestrictionPlanner(), InMemoryAuditLog())
        service = GovernanceService(guard=guard)
        service.register_classification(CLASSIFICATION)
        service.enforce(request_for(), RECORDS)
        self.assertEqual(len(service.recent_audit()), 1)
        self.assertEqual(len(guard.audit.list_recent()), 1)
        self.assertTrue(service.audit_chain_intact())


class FailClosedTests(unittest.TestCase):
    """The Guard's behaviour when the planner cannot be reached."""

    def test_unreachable_planner_denies_rather_than_permits(self) -> None:
        """A missing planner is not permission to proceed.

        This is the failure mode that matters: an enforcement layer whose
        absence silently grants access is not an enforcement layer.
        """

        class BrokenPlanner:
            def plan(self, request):  # noqa: ANN001, ANN201 - test double
                raise RuntimeError("dagentsc is not installed")

        guard = EthicalGuard(BrokenPlanner(), InMemoryAuditLog())
        decision = guard.enforce(request_for(), RECORDS)
        self.assertFalse(decision.permitted)
        self.assertEqual(decision.payload, [])
        self.assertEqual(decision.withheld_fields, list(request_for().requested_fields))
        self.assertEqual(len(guard.audit.list_recent()), 1, "the failure must still be audited")

    def test_the_denial_reason_names_no_server_detail(self) -> None:
        """The reason is shown to callers and kept in the audit chain, so the
        planner's error, which can name a server path, goes to the log only."""

        class BrokenPlanner:
            def plan(self, request):  # noqa: ANN001, ANN201 - test double
                raise FileNotFoundError("[Errno 2] No such file or directory: '/srv/bin/dagentsc'")

        guard = EthicalGuard(BrokenPlanner(), InMemoryAuditLog())
        with self.assertLogs("dagents.common.application.ethical_guard", level="ERROR") as logs:
            decision = guard.enforce(request_for(), RECORDS)
        self.assertEqual("The planner could not be reached, so the Guard denied the request.", decision.message)
        stored = json.dumps(guard.audit.list_recent()[0], default=str)
        self.assertNotIn("/srv/bin/dagentsc", decision.message)
        self.assertNotIn("/srv/bin/dagentsc", stored)
        self.assertIn("/srv/bin/dagentsc", "\n".join(logs.output))


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class GovernanceServiceTests(unittest.TestCase):
    """The shared application service both agents compose."""

    def test_runtime_classifications_are_resolvable(self) -> None:
        service = GovernanceService()
        service.register_classification(CLASSIFICATION)
        self.assertEqual(
            service.get_classification("stroke-triage-v1").minimum_cohort, CLASSIFICATION.minimum_cohort
        )
        self.assertIn(CLASSIFICATION, service.list_classifications())

    def test_planning_does_not_write_an_audit_record(self) -> None:
        """Planning is a question, not an act, so it leaves no trace."""
        service = GovernanceService()
        service.register_classification(CLASSIFICATION)
        service.plan(request_for())
        self.assertEqual(service.recent_audit(), [])
        service.enforce(request_for(), RECORDS)
        self.assertEqual(len(service.recent_audit()), 1)


class _PolicyExtension:
    """An extension that contributes one classification and nothing else."""

    def __init__(self, classification: DataClassification, extension_id: str = "policy") -> None:
        self.extension_id = extension_id
        self.version = "1.0.0"
        self._classification = classification

    def classifications(self) -> list[DataClassification]:
        return [self._classification]


#: A caller's own classification with the registered id: every field low.
ALL_LOW = CLASSIFICATION.model_copy(
    update={
        "field_sensitivity": {"nihss_total": "low", "age_band": "low", "arrival_mode": "low"},
        "default_sensitivity": "low",
        "minimum_cohort": 0,
    }
)


def registry_with(classification: DataClassification) -> ExtensionRegistry:
    registry = ExtensionRegistry()
    registry.register(_PolicyExtension(classification))
    return registry


class RuntimeClassificationTests(unittest.TestCase):
    """A classification registered at runtime cannot replace one an extension contributes."""

    def setUp(self) -> None:
        self.registry = registry_with(CLASSIFICATION)
        self.service = GovernanceService(registry=self.registry)

    def test_a_different_classification_with_an_extension_id_is_refused(self) -> None:
        with self.assertRaises(ClassificationConflict) as caught:
            self.service.register_classification(ALL_LOW)
        self.assertEqual(caught.exception.code, "conflict")
        self.assertEqual(caught.exception.http_status, 409)
        self.assertIn("stroke-triage-v1", str(caught.exception))
        self.assertEqual(self.service.get_classification("stroke-triage-v1"), CLASSIFICATION)

    def test_the_same_classification_registers_again_without_change(self) -> None:
        """An app that registers its own extension's classification keeps working."""
        self.assertEqual(self.service.register_classification(CLASSIFICATION.model_copy(deep=True)), CLASSIFICATION)
        self.assertEqual(
            [c.classification_id for c in self.service.list_classifications()], ["stroke-triage-v1"]
        )

    def test_a_new_id_registers_as_before(self) -> None:
        other = ALL_LOW.model_copy(update={"classification_id": "runtime-only"})
        self.service.register_classification(other)
        self.assertEqual(self.service.get_classification("runtime-only"), other)
        replaced = other.model_copy(update={"minimum_cohort": 7})
        self.service.register_classification(replaced)
        self.assertEqual(self.service.get_classification("runtime-only").minimum_cohort, 7)

    def test_an_extension_registered_later_wins_over_a_runtime_classification(self) -> None:
        """The extension's policy applies even when the runtime entry came first."""
        registry = ExtensionRegistry()
        service = GovernanceService(registry=registry)
        service.register_classification(ALL_LOW)
        registry.register(_PolicyExtension(CLASSIFICATION))
        self.assertEqual(service.get_classification("stroke-triage-v1"), CLASSIFICATION)
        self.assertEqual(service.list_classifications(), [CLASSIFICATION])


class StrictClassificationRefusalTests(unittest.TestCase):
    """Strict mode refuses a request before the planner runs, so these need no planner."""

    def setUp(self) -> None:
        self.service = GovernanceService(registry=registry_with(CLASSIFICATION), strict_classifications=True)

    def test_a_differing_inline_classification_is_refused(self) -> None:
        for call in (
            lambda: self.service.plan(request_for(classification=ALL_LOW)),
            lambda: self.service.enforce(request_for(classification=ALL_LOW), RECORDS),
        ):
            with self.assertRaises(ClassificationConflict) as caught:
                call()
            self.assertIn("stroke-triage-v1", str(caught.exception))
        self.assertEqual(self.service.audit_record_count(), 0)

    def test_an_unknown_classification_id_is_refused(self) -> None:
        """Otherwise a caller could choose a new id and send any policy it likes."""
        unknown = ALL_LOW.model_copy(update={"classification_id": "made-up"})
        with self.assertRaises(ClassificationConflict) as caught:
            self.service.enforce(request_for(classification=unknown), RECORDS)
        self.assertIn("made-up", str(caught.exception))
        self.assertEqual(self.service.audit_record_count(), 0)

    def test_strict_mode_is_off_by_default(self) -> None:
        self.assertFalse(GovernanceService(registry=registry_with(CLASSIFICATION)).strict_classifications)
        self.assertTrue(self.service.strict_classifications)


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class StrictClassificationTests(unittest.TestCase):
    """Strict mode against the real planner."""

    def test_the_registered_classification_is_enforced(self) -> None:
        service = GovernanceService(registry=registry_with(CLASSIFICATION), strict_classifications=True)
        decision = service.enforce(request_for(), RECORDS)
        self.assertTrue(decision.permitted)
        self.assertEqual(service.audit_record_count(), 1)
        self.assertEqual(service.plan(request_for()).decision, decision.plan.decision)

    def test_a_runtime_classification_with_a_new_id_is_enforced(self) -> None:
        service = GovernanceService(registry=ExtensionRegistry(), strict_classifications=True)
        service.register_classification(CLASSIFICATION)
        self.assertTrue(service.enforce(request_for(), RECORDS).permitted)

    def test_without_strict_mode_the_inline_classification_is_used(self) -> None:
        """Today's behaviour, kept for agents with no site or condition pack."""
        service = GovernanceService(registry=registry_with(CLASSIFICATION))
        decision = service.enforce(request_for(request_id="low", classification=ALL_LOW), RECORDS)
        self.assertEqual(decision.plan.decision, "permit")


class AuditLogConcurrencyTests(unittest.TestCase):
    """Agent handlers run in a thread pool, so appends can arrive together."""

    def test_concurrent_appends_keep_one_chain(self) -> None:
        log = InMemoryAuditLog()
        threads = 8
        per_thread = 150
        record = AuditRecord(
            audit_id="a",
            recorded_at=1,
            boundary="before_read",
            request_id="r",
            requester_id="x",
            decision="permit",
            permitted=True,
            granularity="row",
            filtering_score=0.0,
        )
        start = threading.Barrier(threads)

        def append_many(worker: int) -> None:
            start.wait()
            for index in range(per_thread):
                log.append(record.model_copy(update={"audit_id": f"{worker}-{index}"}))

        previous_interval = sys.getswitchinterval()
        # Switch threads as often as possible, so an unlocked append would be
        # interrupted between reading the last digest and storing its record.
        sys.setswitchinterval(1e-6)
        try:
            workers = [threading.Thread(target=append_many, args=(n,)) for n in range(threads)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
        finally:
            sys.setswitchinterval(previous_interval)

        stored = list(reversed(log.list_recent(limit=threads * per_thread)))
        self.assertEqual(len(stored), threads * per_thread)
        previous = [entry.previous_digest for entry in stored]
        self.assertEqual(len(previous), len(set(previous)), "two records name the same previous record")
        self.assertTrue(log.verify_chain(full=True))


class MissingValueTests(unittest.TestCase):
    """A missing value stays missing under every strategy."""

    def test_generalize_keeps_none(self) -> None:
        for level in (1.0, 2.0, 3.0):
            with self.subTest(level=level):
                self.assertIsNone(EthicalGuard._transform(None, "generalize", level, seed="s"))
        self.assertEqual(EthicalGuard._transform("ambulance", "generalize", 1.0, seed="s"), "amb*")

    @unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
    def test_a_generalized_field_returns_none_for_a_missing_value(self) -> None:
        guard = EthicalGuard(DagentscRestrictionPlanner(), InMemoryAuditLog())
        records = [{"age": None, "zip": "02139"}, {"age": 32, "zip": None}, {"age": 55, "zip": "02138"}]
        decision = guard.enforce(
            RestrictionRequest(
                request_id="missing",
                boundary="before_read",
                requester=UNTRUSTED_BUT_KNOWN,
                classification=DataClassification(
                    classification_id="missing", default_sensitivity="medium", minimum_cohort=0
                ),
                requested_fields=["age", "zip"],
                granularity="row",
                cohort_size=3,
            ),
            records,
        )
        strategies = {r.field: r.strategy for r in decision.plan.field_restrictions}
        self.assertTrue(strategies["zip"].startswith("generalize"), strategies)
        self.assertEqual([row["age"] for row in decision.payload], [None, 30, 60])
        self.assertEqual([row["zip"] for row in decision.payload], ["021*", None, "021*"])
        self.assertEqual(decision.withheld_fields, [])


if __name__ == "__main__":
    unittest.main()
