"""Incremental per-field statistics for the records a site holds.

A site updates its profile once for each record it stores, instead of reading
every row again to recompute it. Two profiles can be merged, and a snapshot is
plain JSON that restores the exact state, so a site can persist its profile and
continue from it.

For each field the profile keeps:

- how many records hold a value (``present``) and how many do not
  (``missing``). An absent key, ``None``, NaN and infinity count as missing;
- for numbers: count, mean, sample variance, minimum and maximum. The mean and
  variance use Welford's update for single records and Chan's formula for
  merges, which stay accurate where a running sum of squares would not;
- for strings and booleans: how many records hold each value;
- a count of values of any other type (lists, dicts, dates), which are not
  profiled further.

This is runtime state that changes with every stored record, so it lives in
Python. It makes no decisions. Note that ``min`` and ``max`` are the values of
single records: a snapshot that leaves a site goes through the Ethical Guard
like any other payload.

A profile is not thread-safe. A caller that shares one between threads holds a
lock around ``add``, ``merge`` and ``snapshot``.
"""

from __future__ import annotations

import decimal
import math
import numbers
from collections.abc import Iterable, Mapping
from typing import Any

#: The value kinds a field can hold, in the order a snapshot reports them.
_KINDS = ("numeric", "categorical", "boolean", "other")


def _classify(value: Any) -> tuple[str, Any]:
    """Return a value's kind and the form the profile stores.

    Numbers are returned as ``(float_value, exact_value)``: the float feeds the
    mean and variance, and the exact value (an ``int`` for integers) is kept
    for the minimum and maximum, so a snapshot shows ``48`` rather than
    ``48.0``.
    """
    if value is None:
        return "missing", None
    if isinstance(value, bool):
        return "boolean", value
    if isinstance(value, (numbers.Real, decimal.Decimal)):
        try:
            number = float(value)
        except (OverflowError, ValueError):
            return "missing", None
        if not math.isfinite(number):
            return "missing", None
        exact = int(value) if isinstance(value, numbers.Integral) else number
        return "numeric", (number, exact)
    if isinstance(value, str):
        return "categorical", value
    return "other", None


def _require_count(value: Any, name: str, minimum: int = 0) -> int:
    """Validate a count read from a snapshot."""
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer of at least {minimum}, got {value!r}")
    return value


def _require_number(value: Any, name: str) -> float | int:
    """Validate a finite number read from a snapshot."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number, got {value!r}")
    return value


class _FieldStats:
    """The accumulators for one field."""

    __slots__ = ("count", "mean", "m2", "minimum", "maximum", "categories", "true_count", "false_count", "other")

    def __init__(self) -> None:
        self.count = 0
        self.mean = 0.0
        self.m2 = 0.0
        self.minimum: float | int | None = None
        self.maximum: float | int | None = None
        self.categories: dict[str, int] = {}
        self.true_count = 0
        self.false_count = 0
        self.other = 0

    @property
    def present(self) -> int:
        return self.count + sum(self.categories.values()) + self.true_count + self.false_count + self.other

    def add(self, kind: str, value: Any) -> None:
        """Add one non-missing value of a known kind."""
        if kind == "numeric":
            number, exact = value
            self.count += 1
            delta = number - self.mean
            self.mean += delta / self.count
            self.m2 += delta * (number - self.mean)
            if self.minimum is None or exact < self.minimum:
                self.minimum = exact
            if self.maximum is None or exact > self.maximum:
                self.maximum = exact
        elif kind == "categorical":
            self.categories[value] = self.categories.get(value, 0) + 1
        elif kind == "boolean":
            if value:
                self.true_count += 1
            else:
                self.false_count += 1
        else:
            self.other += 1

    def merge(self, other: _FieldStats) -> None:
        """Add another field's accumulators to this one."""
        if other.count:
            if not self.count:
                self.count, self.mean, self.m2 = other.count, other.mean, other.m2
                self.minimum, self.maximum = other.minimum, other.maximum
            else:
                total = self.count + other.count
                delta = other.mean - self.mean
                self.mean += delta * other.count / total
                self.m2 += other.m2 + delta * delta * self.count * other.count / total
                self.count = total
                self.minimum = min(self.minimum, other.minimum)  # type: ignore[type-var]
                self.maximum = max(self.maximum, other.maximum)  # type: ignore[type-var]
        for category, count in list(other.categories.items()):
            self.categories[category] = self.categories.get(category, 0) + count
        self.true_count += other.true_count
        self.false_count += other.false_count
        self.other += other.other

    def kind(self) -> str:
        seen = [
            name
            for name, held in zip(
                _KINDS, (self.count, self.categories, self.true_count + self.false_count, self.other)
            )
            if held
        ]
        if not seen:
            return "empty"
        return seen[0] if len(seen) == 1 else "mixed"

    def snapshot(self, record_count: int) -> dict[str, Any]:
        present = self.present
        numeric = None
        if self.count:
            numeric = {
                "count": self.count,
                "mean": self.mean,
                # Sample variance: divides by count - 1.
                "variance": self.m2 / (self.count - 1) if self.count > 1 else None,
                "min": self.minimum,
                "max": self.maximum,
                # The sum of squared deviations. Kept so a restored or merged
                # profile continues exactly where this one stopped.
                "m2": self.m2,
            }
        booleans = {"false": self.false_count, "true": self.true_count} if self.true_count + self.false_count else {}
        return {
            "present": present,
            "missing": record_count - present,
            "kind": self.kind(),
            "numeric": numeric,
            "categories": {category: self.categories[category] for category in sorted(self.categories)},
            "booleans": booleans,
            "other": self.other,
        }

    @classmethod
    def from_snapshot(cls, name: str, data: Mapping[str, Any]) -> _FieldStats:
        stats = cls()
        numeric = data.get("numeric")
        if numeric is not None:
            stats.count = _require_count(numeric["count"], f"{name}.numeric.count", minimum=1)
            stats.mean = float(_require_number(numeric["mean"], f"{name}.numeric.mean"))
            stats.m2 = float(_require_number(numeric["m2"], f"{name}.numeric.m2"))
            if stats.m2 < 0:
                raise ValueError(f"{name}.numeric.m2 must not be negative")
            stats.minimum = _require_number(numeric["min"], f"{name}.numeric.min")
            stats.maximum = _require_number(numeric["max"], f"{name}.numeric.max")
            if stats.minimum > stats.maximum:
                raise ValueError(f"{name}.numeric.min is larger than its max")
        for category, count in dict(data.get("categories") or {}).items():
            if not isinstance(category, str):
                raise ValueError(f"{name}.categories keys must be strings")
            stats.categories[category] = _require_count(count, f"{name}.categories.{category}", minimum=1)
        booleans = dict(data.get("booleans") or {})
        if set(booleans) - {"false", "true"}:
            raise ValueError(f"{name}.booleans may only hold 'false' and 'true'")
        stats.false_count = _require_count(booleans.get("false", 0), f"{name}.booleans.false")
        stats.true_count = _require_count(booleans.get("true", 0), f"{name}.booleans.true")
        stats.other = _require_count(data.get("other", 0), f"{name}.other")
        return stats


class RunningProfile:
    """Per-field statistics that grow one record at a time.

    Inputs:
    - ``fields``: the fields to track. Keys a record holds outside this list
      are ignored, and a tracked field that no record holds is still reported.
      With ``None`` the profile tracks every key it sees, and a field that first
      appears in a later record counts as missing in the earlier ones.
    """

    def __init__(self, fields: Iterable[str] | None = None) -> None:
        self._fields: list[str] | None = None if fields is None else sorted(set(fields))
        self._record_count = 0
        self._stats: dict[str, _FieldStats] = {}
        for name in self._fields or []:
            self._stats[name] = _FieldStats()

    @property
    def fields(self) -> list[str] | None:
        """The tracked fields in sorted order, or ``None`` when tracking every key."""
        return None if self._fields is None else list(self._fields)

    @property
    def record_count(self) -> int:
        """How many records the profile has seen."""
        return self._record_count

    def add(self, record: Mapping[str, Any]) -> None:
        """Add one record."""
        if not isinstance(record, Mapping):
            raise TypeError(f"a record must be a mapping, got {type(record).__name__}")
        names = self._fields if self._fields is not None else list(record)
        for name in names:
            if not isinstance(name, str):
                raise TypeError(f"record keys must be strings, got {name!r}")
        self._record_count += 1
        for name in names:
            if name not in record:
                continue
            stats = self._stats.setdefault(name, _FieldStats())
            kind, value = _classify(record[name])
            if kind != "missing":
                stats.add(kind, value)

    def add_many(self, records: Iterable[Mapping[str, Any]]) -> None:
        """Add records in order. The result equals calling ``add`` for each."""
        for record in records:
            self.add(record)

    def merge(self, other: RunningProfile) -> RunningProfile:
        """Add another profile's records to this one and return this profile.

        The result equals one profile that saw both sets of records, up to
        floating-point rounding in the mean and variance. This profile's
        tracked fields decide which of the other profile's fields are kept.
        ``other`` is not changed.
        """
        if not isinstance(other, RunningProfile):
            raise TypeError(f"can only merge another RunningProfile, got {type(other).__name__}")
        incoming = list(other._stats.items())
        self._record_count += other._record_count
        for name, stats in incoming:
            if self._fields is not None and name not in self._fields:
                continue
            self._stats.setdefault(name, _FieldStats()).merge(stats)
        return self

    def snapshot(self) -> dict[str, Any]:
        """Return the profile as JSON-serialisable data, with fields in sorted order."""
        return {
            "record_count": self._record_count,
            "tracked_fields": self.fields,
            "fields": {name: self._stats[name].snapshot(self._record_count) for name in sorted(self._stats)},
        }

    @classmethod
    def from_snapshot(cls, snapshot: Mapping[str, Any]) -> RunningProfile:
        """Restore a profile from :meth:`snapshot` output.

        Raises ``ValueError`` when the snapshot is malformed or its counts do
        not agree with each other, so a damaged stored profile is not used.
        """
        if not isinstance(snapshot, Mapping):
            raise ValueError("a profile snapshot must be a mapping")
        try:
            record_count = _require_count(snapshot["record_count"], "record_count")
            tracked = snapshot.get("tracked_fields")
            if tracked is not None and (
                not isinstance(tracked, list) or not all(isinstance(name, str) for name in tracked)
            ):
                raise ValueError("tracked_fields must be a list of strings or null")
            fields = snapshot["fields"]
            if not isinstance(fields, Mapping):
                raise ValueError("fields must be a mapping")
            profile = cls(fields=tracked)
            profile._record_count = record_count
            for name, data in fields.items():
                if not isinstance(name, str) or not isinstance(data, Mapping):
                    raise ValueError(f"field {name!r} is malformed")
                if tracked is not None and name not in tracked:
                    raise ValueError(f"field {name!r} is not in tracked_fields")
                stats = _FieldStats.from_snapshot(name, data)
                present = stats.present
                if present > record_count:
                    raise ValueError(f"field {name!r} has {present} values but the profile has {record_count} records")
                if data.get("present") != present or data.get("missing") != record_count - present:
                    raise ValueError(f"field {name!r} has present and missing counts that do not match its values")
                profile._stats[name] = stats
        except (KeyError, TypeError) as exc:
            raise ValueError(f"malformed profile snapshot: {exc!r}") from exc
        return profile


__all__ = ["RunningProfile"]
