"""Tests for the GRAILS governance layer.

These run against the real ``dagentsc`` binary, because the whole point of the
split is that the decision comes from the typed planner. A test that stubbed the
planner would prove the Guard calls something, not that the framework's
governance works.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import unittest
import warnings

from agents.common.application.ethical_guard import (
    DagentscRestrictionPlanner,
    EthicalGuard,
    InMemoryAuditLog,
)
from agents.common.application.governance_service import GovernanceService
from agents.common.domain.governance import (
    DataClassification,
    KyuAttribute,
    Requester,
    RestrictionRequest,
)
from agents.common.infrastructure.dagents_runner import dagentsc_binary


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
        from agents.common.domain.governance import AuditRecord

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


if __name__ == "__main__":
    unittest.main()
