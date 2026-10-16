"""Tests for the incremental per-field profile a site keeps as records arrive.

A site updates its profile once per inserted record instead of recomputing it
from every row. These tests check that the incremental result equals a batch
computation, that merging two profiles equals profiling the combined records,
and that a snapshot restores the exact state.
"""

from __future__ import annotations

import json
import math
import random
import statistics
import unittest

from dagents.common.application.running_profile import RunningProfile


def sample_records(count: int = 200, seed: int = 7) -> list[dict]:
    """Mixed records: numbers, categories, booleans and missing values."""
    generator = random.Random(seed)
    records: list[dict] = []
    for index in range(count):
        record: dict = {"reading": round(generator.gauss(140.0, 25.0), 1), "level": generator.choice(["x", "y", "z"])}
        if index % 5:
            record["flag"] = generator.random() < 0.3
        if index % 7 == 0:
            record["reading"] = None
        if index % 3 == 0:
            record["count"] = generator.randint(0, 48)
        records.append(record)
    return records


def assert_snapshots_close(test: unittest.TestCase, left: dict, right: dict, path: str = "") -> None:
    """Compare two snapshots: integers and strings exactly, floats to 1e-9 relative."""
    test.assertEqual(type(left), type(right), path)
    if isinstance(left, dict):
        test.assertEqual(list(left), list(right), path)
        for key in left:
            assert_snapshots_close(test, left[key], right[key], f"{path}.{key}")
    elif isinstance(left, list):
        test.assertEqual(len(left), len(right), path)
        for index, (one, two) in enumerate(zip(left, right)):
            assert_snapshots_close(test, one, two, f"{path}[{index}]")
    elif isinstance(left, float):
        test.assertTrue(math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-9), f"{path}: {left} != {right}")
    else:
        test.assertEqual(left, right, path)


class IncrementalTests(unittest.TestCase):
    """Adding records one at a time gives the batch statistics."""

    def test_incremental_equals_batch(self) -> None:
        records = sample_records()
        one_by_one = RunningProfile()
        for record in records:
            one_by_one.add(record)
        batch = RunningProfile()
        batch.add_many(records)
        self.assertEqual(one_by_one.snapshot(), batch.snapshot())

        readings = [record["reading"] for record in records if record.get("reading") is not None]
        snapshot = batch.snapshot()
        self.assertEqual(snapshot["record_count"], len(records))
        numeric = snapshot["fields"]["reading"]["numeric"]
        self.assertEqual(numeric["count"], len(readings))
        self.assertAlmostEqual(numeric["mean"], statistics.fmean(readings), places=9)
        self.assertAlmostEqual(numeric["variance"], statistics.variance(readings), places=6)
        self.assertEqual(numeric["min"], min(readings))
        self.assertEqual(numeric["max"], max(readings))
        self.assertEqual(snapshot["fields"]["reading"]["missing"], len(records) - len(readings))

        levels = snapshot["fields"]["level"]
        self.assertEqual(levels["kind"], "categorical")
        for value in ("x", "y", "z"):
            self.assertEqual(levels["categories"][value], sum(1 for record in records if record["level"] == value))

    def test_booleans_are_counted_not_averaged(self) -> None:
        profile = RunningProfile()
        profile.add_many([{"flag": True}, {"flag": False}, {"flag": True}, {}])
        flag = profile.snapshot()["fields"]["flag"]
        self.assertEqual(flag["kind"], "boolean")
        self.assertEqual(flag["booleans"], {"false": 1, "true": 2})
        self.assertIsNone(flag["numeric"])
        self.assertEqual((flag["present"], flag["missing"]), (3, 1))

    def test_missing_counts_include_records_before_a_field_first_appears(self) -> None:
        profile = RunningProfile()
        profile.add_many([{"a": 1}, {"a": 2}, {"a": 3, "b": "late"}])
        field = profile.snapshot()["fields"]["b"]
        self.assertEqual((field["present"], field["missing"]), (1, 2))

    def test_none_and_non_finite_numbers_count_as_missing(self) -> None:
        profile = RunningProfile()
        profile.add_many([{"a": None}, {"a": float("nan")}, {"a": float("inf")}, {"a": 2.0}])
        field = profile.snapshot()["fields"]["a"]
        self.assertEqual((field["present"], field["missing"]), (1, 3))
        self.assertEqual(field["numeric"]["mean"], 2.0)

    def test_variance_needs_two_values(self) -> None:
        profile = RunningProfile()
        profile.add({"a": 5})
        numeric = profile.snapshot()["fields"]["a"]["numeric"]
        self.assertEqual((numeric["count"], numeric["mean"], numeric["min"], numeric["max"]), (1, 5.0, 5, 5))
        self.assertIsNone(numeric["variance"])

    def test_unsupported_values_are_counted_separately(self) -> None:
        profile = RunningProfile()
        profile.add_many([{"a": [1, 2]}, {"a": {"b": 1}}, {"a": 3}])
        field = profile.snapshot()["fields"]["a"]
        self.assertEqual(field["other"], 2)
        self.assertEqual(field["present"], 3)
        self.assertEqual(field["kind"], "mixed")

    def test_tracked_fields_ignore_others_and_list_unseen_fields(self) -> None:
        profile = RunningProfile(fields=["reading", "never"])
        profile.add_many(sample_records(20))
        snapshot = profile.snapshot()
        self.assertEqual(snapshot["tracked_fields"], ["never", "reading"])
        self.assertEqual(list(snapshot["fields"]), ["never", "reading"])
        never = snapshot["fields"]["never"]
        self.assertEqual((never["present"], never["missing"], never["kind"]), (0, 20, "empty"))

    def test_snapshot_ordering_does_not_depend_on_insertion_order(self) -> None:
        forward = RunningProfile()
        forward.add_many([{"b": "q", "a": 1}, {"a": 2, "b": "p"}])
        backward = RunningProfile()
        backward.add_many([{"a": 2, "b": "p"}, {"b": "q", "a": 1}])
        self.assertEqual(json.dumps(forward.snapshot()), json.dumps(backward.snapshot()))
        self.assertEqual(list(forward.snapshot()["fields"]["b"]["categories"]), ["p", "q"])


class MergeTests(unittest.TestCase):
    """Merging profiles of parts gives the profile of the whole."""

    def test_merge_equals_combined(self) -> None:
        records = sample_records(301)
        combined = RunningProfile()
        combined.add_many(records)
        for cuts in ((150,), (1, 300), (50, 51, 200), (0,)):
            with self.subTest(cuts=cuts):
                bounds = [0, *cuts, len(records)]
                merged = RunningProfile()
                for start, end in zip(bounds, bounds[1:]):
                    part = RunningProfile()
                    part.add_many(records[start:end])
                    merged.merge(part)
                assert_snapshots_close(self, merged.snapshot(), combined.snapshot())

    def test_merge_returns_the_merged_profile_and_leaves_the_other_alone(self) -> None:
        left = RunningProfile()
        left.add({"a": 1})
        right = RunningProfile()
        right.add({"a": 3})
        before = right.snapshot()
        self.assertIs(left.merge(right), left)
        self.assertEqual(right.snapshot(), before)
        self.assertEqual(left.snapshot()["fields"]["a"]["numeric"]["mean"], 2.0)

    def test_merging_an_empty_profile_changes_nothing(self) -> None:
        profile = RunningProfile()
        profile.add_many(sample_records(30))
        before = profile.snapshot()
        profile.merge(RunningProfile())
        self.assertEqual(profile.snapshot(), before)

    def test_merge_keeps_the_receiving_profile_s_tracked_fields(self) -> None:
        tracked = RunningProfile(fields=["a"])
        tracked.add({"a": 1, "b": "x"})
        everything = RunningProfile()
        everything.add({"a": 2, "b": "y"})
        tracked.merge(everything)
        snapshot = tracked.snapshot()
        self.assertEqual(list(snapshot["fields"]), ["a"])
        self.assertEqual(snapshot["record_count"], 2)

    def test_a_field_one_side_never_saw_counts_as_missing_there(self) -> None:
        left = RunningProfile()
        left.add_many([{"a": 1}, {"a": 2}])
        right = RunningProfile()
        right.add_many([{"b": "x"}])
        field = left.merge(right).snapshot()["fields"]
        self.assertEqual((field["a"]["present"], field["a"]["missing"]), (2, 1))
        self.assertEqual((field["b"]["present"], field["b"]["missing"]), (1, 2))


class SnapshotTests(unittest.TestCase):
    """A snapshot is plain JSON and restores the exact state."""

    def test_snapshot_round_trip(self) -> None:
        records = sample_records(120)
        profile = RunningProfile()
        profile.add_many(records[:80])
        stored = json.loads(json.dumps(profile.snapshot()))
        restored = RunningProfile.from_snapshot(stored)
        self.assertEqual(restored.snapshot(), profile.snapshot())

        # Continuing after a restore matches never having stored it.
        restored.add_many(records[80:])
        profile.add_many(records[80:])
        self.assertEqual(restored.snapshot(), profile.snapshot())

    def test_tracked_fields_survive_a_round_trip(self) -> None:
        profile = RunningProfile(fields=["a"])
        profile.add({"a": 1, "b": 2})
        restored = RunningProfile.from_snapshot(json.loads(json.dumps(profile.snapshot())))
        restored.add({"a": 2, "b": 3})
        self.assertEqual(list(restored.snapshot()["fields"]), ["a"])
        self.assertEqual(restored.fields, ["a"])

    def test_an_empty_profile_round_trips(self) -> None:
        empty = RunningProfile().snapshot()
        self.assertEqual(empty, {"record_count": 0, "tracked_fields": None, "fields": {}})
        self.assertEqual(RunningProfile.from_snapshot(empty).snapshot(), empty)

    def test_from_snapshot_rejects_inconsistent_input(self) -> None:
        profile = RunningProfile()
        profile.add_many([{"a": 1.0}, {"a": 2.0}, {"a": "x"}])
        good = profile.snapshot()
        broken = [
            {**good, "record_count": -1},
            {**good, "record_count": 1},
            {key: value for key, value in good.items() if key != "fields"},
            json.loads(json.dumps(good).replace('"present": 3', '"present": 9')),
            json.loads(json.dumps(good).replace('"count": 2', '"count": 5')),
            "not a snapshot",
        ]
        for snapshot in broken:
            with self.subTest(snapshot=str(snapshot)[:60]):
                with self.assertRaises(ValueError):
                    RunningProfile.from_snapshot(snapshot)


if __name__ == "__main__":
    unittest.main()
