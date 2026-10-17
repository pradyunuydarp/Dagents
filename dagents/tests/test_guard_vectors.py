"""Tests for how the Ethical Guard bounds vector contributions.

A model update leaves a site as a vector of parameter deltas. ``clip_contribution``
and ``add_noise`` must apply to every coordinate. A value the Guard cannot bound
is withheld, never passed through. The audit record keeps the norm before and
after, so the bound that was applied can be checked later.

The unit tests call ``EthicalGuard._transform`` directly and need no planner. The
end-to-end tests run against the real ``dagentsc`` binary.
"""

from __future__ import annotations

import math
import random
import statistics
import unittest

from dagents.common.application.ethical_guard import (
    DagentscRestrictionPlanner,
    EthicalGuard,
    InMemoryAuditLog,
    contribution_norm,
)
from dagents.common.domain.governance import (
    DataClassification,
    KyuAttribute,
    Requester,
    RestrictionRequest,
)
from dagents.tests.test_governance import dagentsc_available

CLASSIFICATION = DataClassification(
    classification_id="vector-test",
    field_sensitivity={"model_update": "high", "reading": "high", "group": "low"},
    default_sensitivity="high",
    minimum_cohort=3,
)

#: Two verified attributes and a full compliance history: high trust, so the
#: planner selects ``clip_contribution:1`` for a model update.
HIGH_TRUST = Requester(
    requester_id="vector-coordinator",
    requester_kind="coordinator",
    compliance_history=1.0,
    attributes=[
        KyuAttribute(attribute_id="manifest_digest", verified=True),
        KyuAttribute(attribute_id="code_digest", verified=True),
    ],
)

#: One verified and one unverified attribute: moderate trust, so the planner
#: selects ``add_noise:0.5`` for a model update.
MODERATE_TRUST = Requester(
    requester_id="vector-coordinator",
    requester_kind="coordinator",
    compliance_history=0.5,
    attributes=[
        KyuAttribute(attribute_id="manifest_digest", verified=True),
        KyuAttribute(attribute_id="code_digest", verified=False),
    ],
)

#: Values that are not a numeric scalar or a vector of finite numbers.
UNBOUNDABLE = [
    "3.0",
    True,
    [1.0, "2.0"],
    [1.0, True],
    [[1.0, 2.0]],
    {"a": "1.0"},
    {"a": [1.0]},
    {1: 1.0},
    [1.0, float("nan")],
    {"a": float("inf")},
    (1.0, 2.0),
    object(),
]


def update_request(**overrides) -> RestrictionRequest:
    payload = {
        "request_id": "round-7:site-a:before_send",
        "boundary": "before_send",
        "requester": HIGH_TRUST,
        "classification": CLASSIFICATION,
        "requested_fields": ["model_update"],
        "granularity": "model_update",
        "cohort_size": 50,
    }
    payload.update(overrides)
    return RestrictionRequest(**payload)


class VectorClipTests(unittest.TestCase):
    """``clip_contribution:b`` scales a vector by min(1, b / ||v||2)."""

    def test_a_long_list_is_scaled_onto_the_bound(self) -> None:
        clipped = EthicalGuard._transform([3.0, 4.0], "clip_contribution", 1.0, seed="s")
        self.assertEqual(len(clipped), 2)
        self.assertAlmostEqual(clipped[0], 0.6)
        self.assertAlmostEqual(clipped[1], 0.8)
        self.assertLessEqual(contribution_norm(clipped), 1.0)

    def test_a_dict_is_scaled_and_keeps_its_keys(self) -> None:
        clipped = EthicalGuard._transform({"w.b": 3.0, "bias": 4.0}, "clip_contribution", 2.0, seed="s")
        self.assertEqual(list(clipped), ["w.b", "bias"])
        self.assertAlmostEqual(clipped["w.b"], 1.2)
        self.assertAlmostEqual(clipped["bias"], 1.6)

    def test_a_vector_inside_the_bound_is_unchanged(self) -> None:
        self.assertEqual(EthicalGuard._transform([0.1, -0.2], "clip_contribution", 1.0, seed="s"), [0.1, -0.2])
        self.assertEqual(
            EthicalGuard._transform({"a": 0.3, "b": -0.4}, "clip_contribution", 1.0, seed="s"),
            {"a": 0.3, "b": -0.4},
        )

    def test_integer_coordinates_are_accepted(self) -> None:
        clipped = EthicalGuard._transform([3, 4], "clip_contribution", 1.0, seed="s")
        self.assertTrue(all(isinstance(value, float) for value in clipped))
        self.assertAlmostEqual(clipped[0], 0.6)

    def test_the_bound_holds_exactly_for_many_vectors(self) -> None:
        """No tolerance: the clipped norm never exceeds the bound, and the direction is kept."""
        generator = random.Random(20261011)
        for trial in range(300):
            size = generator.randint(1, 60)
            scale = 10 ** generator.uniform(-3, 3)
            vector = [generator.gauss(0.0, scale) for _ in range(size)]
            bound = generator.choice([0.1, 1.0, 3.7])
            clipped = EthicalGuard._transform(vector, "clip_contribution", bound, seed=f"t{trial}")
            self.assertLessEqual(contribution_norm(clipped), bound, f"trial {trial}")
            factor = min(1.0, bound / contribution_norm(vector))
            for original, value in zip(vector, clipped):
                self.assertAlmostEqual(value, original * factor, delta=1e-9 * max(1.0, abs(original)))

    def test_zero_and_empty_vectors_pass_unchanged(self) -> None:
        self.assertEqual(EthicalGuard._transform([0.0, 0.0], "clip_contribution", 1.0, seed="s"), [0.0, 0.0])
        self.assertEqual(EthicalGuard._transform([], "clip_contribution", 1.0, seed="s"), [])
        self.assertEqual(EthicalGuard._transform({}, "clip_contribution", 1.0, seed="s"), {})

    def test_a_zero_bound_removes_the_contribution(self) -> None:
        self.assertEqual(EthicalGuard._transform([3.0, 4.0], "clip_contribution", 0.0, seed="s"), [0.0, 0.0])

    def test_a_scalar_is_still_clamped(self) -> None:
        self.assertEqual(EthicalGuard._transform(7.5, "clip_contribution", 1.0, seed="s"), 1.0)
        self.assertEqual(EthicalGuard._transform(-7.5, "clip_contribution", 1.0, seed="s"), -1.0)
        self.assertEqual(EthicalGuard._transform(0.3, "clip_contribution", 1.0, seed="s"), 0.3)


class VectorNoiseTests(unittest.TestCase):
    """``add_noise:s`` adds seeded Gaussian noise to each coordinate."""

    def test_noise_is_reproducible_from_the_seed(self) -> None:
        vector = {"w.a": 0.1, "w.b": -0.2, "bias": 0.0}
        first = EthicalGuard._transform(vector, "add_noise", 0.5, seed="req:model_update:0")
        second = EthicalGuard._transform(vector, "add_noise", 0.5, seed="req:model_update:0")
        other = EthicalGuard._transform(vector, "add_noise", 0.5, seed="req:model_update:1")
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertNotEqual(first, vector)

    def test_dict_noise_is_keyed_by_coordinate_name(self) -> None:
        """Reordering or adding coordinates does not change another coordinate's noise."""
        base = EthicalGuard._transform({"a": 0.0, "b": 0.0}, "add_noise", 0.5, seed="s")
        reordered = EthicalGuard._transform({"b": 0.0, "a": 0.0}, "add_noise", 0.5, seed="s")
        widened = EthicalGuard._transform({"a": 0.0, "c": 0.0, "b": 0.0}, "add_noise", 0.5, seed="s")
        self.assertEqual(base["a"], reordered["a"])
        self.assertEqual(base["b"], reordered["b"])
        self.assertEqual(base["a"], widened["a"])
        self.assertEqual(base["b"], widened["b"])
        self.assertNotEqual(base["a"], base["b"])

    def test_list_noise_is_keyed_by_position(self) -> None:
        short = EthicalGuard._transform([0.0, 0.0], "add_noise", 0.5, seed="s")
        longer = EthicalGuard._transform([0.0, 0.0, 0.0], "add_noise", 0.5, seed="s")
        self.assertEqual(short, longer[:2])

    def test_noise_scale_follows_the_parameter(self) -> None:
        noised = EthicalGuard._transform([0.0] * 4000, "add_noise", 0.5, seed="scale")
        self.assertAlmostEqual(statistics.fmean(noised), 0.0, delta=0.05)
        self.assertAlmostEqual(statistics.pstdev(noised), 0.5, delta=0.05)

    def test_scalar_noise_is_unchanged(self) -> None:
        expected = 1.0 + random.Random("s").gauss(0.0, 0.5)
        self.assertEqual(EthicalGuard._transform(1.0, "add_noise", 0.5, seed="s"), expected)


class UnboundableValueTests(unittest.TestCase):
    """A value these strategies cannot bound is withheld, never passed through."""

    def test_clip_withholds_what_it_cannot_bound(self) -> None:
        for value in UNBOUNDABLE:
            with self.subTest(value=repr(value)):
                self.assertIsNone(EthicalGuard._transform(value, "clip_contribution", 1.0, seed="s"))

    def test_noise_withholds_what_it_cannot_bound(self) -> None:
        for value in UNBOUNDABLE:
            with self.subTest(value=repr(value)):
                self.assertIsNone(EthicalGuard._transform(value, "add_noise", 0.5, seed="s"))

    def test_the_norm_helper_rejects_the_same_values(self) -> None:
        for value in UNBOUNDABLE:
            with self.subTest(value=repr(value)):
                self.assertIsNone(contribution_norm(value))
        self.assertEqual(contribution_norm(-2.5), 2.5)
        self.assertEqual(contribution_norm([3.0, 4.0]), 5.0)
        self.assertEqual(contribution_norm({"a": 3, "b": 4}), 5.0)
        self.assertEqual(contribution_norm([]), 0.0)


@unittest.skipUnless(dagentsc_available(), "dagentsc binary is not available")
class GuardedUpdateTests(unittest.TestCase):
    """Vector updates through the real planner, with privacy accounting in the audit log."""

    def setUp(self) -> None:
        self.guard = EthicalGuard(DagentscRestrictionPlanner(), InMemoryAuditLog())

    def test_a_vector_update_leaves_clipped_and_is_accounted(self) -> None:
        decision = self.guard.enforce(update_request(), [{"model_update": {"w.a": 3.0, "w.b": 4.0}}])
        self.assertTrue(decision.permitted)
        self.assertEqual(decision.transformed_fields, ["model_update"])
        self.assertEqual(decision.withheld_fields, [])
        sent = decision.payload[0]["model_update"]
        self.assertAlmostEqual(sent["w.a"], 0.6)
        self.assertAlmostEqual(sent["w.b"], 0.8)

        record = self.guard.audit.list_recent(1)[0]
        self.assertEqual(record.audit_id, decision.audit_id)
        self.assertEqual(record.contribution_norm_before, 5.0)
        self.assertLessEqual(record.contribution_norm_after, 1.0)
        self.assertAlmostEqual(record.contribution_norm_after, 1.0)
        self.assertEqual(record.contribution_strategies, ["clip_contribution:1"])

    def test_a_scalar_update_is_accounted_too(self) -> None:
        decision = self.guard.enforce(update_request(), [{"model_update": 7.5}])
        self.assertEqual(decision.payload, [{"model_update": 1.0}])
        record = self.guard.audit.list_recent(1)[0]
        self.assertEqual(record.contribution_norm_before, 7.5)
        self.assertEqual(record.contribution_norm_after, 1.0)

    def test_accounting_reports_the_largest_contribution_in_the_payload(self) -> None:
        self.guard.enforce(
            update_request(),
            [{"model_update": [0.3, 0.4]}, {"model_update": [3.0, 4.0]}, {"model_update": [0.6, 0.8]}],
        )
        record = self.guard.audit.list_recent(1)[0]
        self.assertEqual(record.contribution_norm_before, 5.0)
        self.assertAlmostEqual(record.contribution_norm_after, 1.0)

    def test_accounting_is_covered_by_the_digest_chain(self) -> None:
        self.guard.enforce(update_request(request_id="one"), [{"model_update": [3.0, 4.0]}])
        self.guard.enforce(update_request(request_id="two"), [{"model_update": [0.3, 0.4]}])
        audit = self.guard.audit
        self.assertTrue(audit.verify_chain())
        stored = audit._records  # noqa: SLF001 - reaching in is the point of the test
        original = stored[0]
        for field, forged in (
            ("contribution_norm_before", 0.9),
            ("contribution_norm_after", 5.0),
            ("contribution_strategies", ["allow_full"]),
        ):
            with self.subTest(field=field):
                stored[0] = original.model_copy(update={field: forged})
                self.assertFalse(audit.verify_chain())
        stored[0] = original
        self.assertTrue(audit.verify_chain())

    def test_a_non_numeric_update_is_withheld(self) -> None:
        decision = self.guard.enforce(update_request(), [{"model_update": {"w.a": "3.0"}}])
        self.assertEqual(decision.payload, [])
        self.assertEqual(decision.withheld_fields, ["model_update"])
        self.assertEqual(decision.transformed_fields, [])
        record = self.guard.audit.list_recent(1)[0]
        self.assertEqual(record.withheld_fields, ["model_update"])
        self.assertIsNone(record.contribution_norm_before)
        self.assertIsNone(record.contribution_norm_after)

    def test_one_unboundable_value_withholds_the_field_in_every_record(self) -> None:
        """Returning the field from some records would report it as both withheld and sent."""
        decision = self.guard.enforce(
            update_request(), [{"model_update": [3.0, 4.0]}, {"model_update": "opaque"}]
        )
        self.assertEqual(decision.payload, [])
        self.assertEqual(decision.withheld_fields, ["model_update"])
        self.assertEqual(decision.transformed_fields, [])

    def test_a_withheld_field_does_not_remove_the_others(self) -> None:
        request = update_request(
            requested_fields=["model_update", "group"],
            classification=CLASSIFICATION.model_copy(
                update={"field_sensitivity": {"model_update": "high", "group": "high"}}
            ),
        )
        decision = self.guard.enforce(request, [{"model_update": "opaque", "group": 0.25}])
        self.assertEqual(decision.withheld_fields, ["model_update"])
        self.assertEqual(decision.transformed_fields, ["group"])
        self.assertEqual(decision.payload, [{"group": 0.25}])

    def test_moderate_trust_adds_reproducible_noise(self) -> None:
        request = update_request(requester=MODERATE_TRUST)
        update = {"w.a": 0.3, "w.b": -0.4}
        first = self.guard.enforce(request, [{"model_update": dict(update)}])
        replay = EthicalGuard(DagentscRestrictionPlanner(), InMemoryAuditLog()).enforce(
            request, [{"model_update": dict(update)}]
        )
        self.assertEqual(first.payload, replay.payload)
        noised = first.payload[0]["model_update"]
        self.assertEqual(sorted(noised), ["w.a", "w.b"])
        self.assertNotEqual(noised, update)

        record = self.guard.audit.list_recent(1)[0]
        self.assertEqual(record.contribution_strategies, ["add_noise:0.5"])
        self.assertAlmostEqual(record.contribution_norm_before, 0.5)
        self.assertAlmostEqual(record.contribution_norm_after, math.hypot(*noised.values()))

    def test_a_request_without_a_contribution_has_no_accounting(self) -> None:
        self.guard.enforce(
            update_request(
                boundary="before_read", requested_fields=["reading", "group"], granularity="row", cohort_size=3
            ),
            [{"reading": 12.0, "group": "a"}] * 3,
        )
        record = self.guard.audit.list_recent(1)[0]
        self.assertIsNone(record.contribution_norm_before)
        self.assertIsNone(record.contribution_norm_after)
        self.assertEqual(record.contribution_strategies, [])


if __name__ == "__main__":
    unittest.main()
