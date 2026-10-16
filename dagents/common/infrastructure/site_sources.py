"""Site record sources that the framework ships.

A site record source (``dagents.common.application.ports.SiteRecordSource``) is
where an LMA started for a site reads that site's records. An application
usually hands the LMA a source built on its own store, or contributes a factory
through its extension's ``site_sources()``.

``StaticSiteSource`` holds records in memory. Examples and tests use it.
"""

from __future__ import annotations

import copy
import threading
from typing import Any, Iterable, Mapping


class StaticSiteSource:
    """A site's records, held in memory.

    Params:
    - `records`: the records, one dict each. They are copied, so a later change
      to the caller's list or dicts does not change the source.
    - `location`: names that place the records, such as ``{"name": "fixture"}``.
      Returned by :meth:`location`. Never a host, a password or a connection
      string.

    ``kind`` is ``"memory"`` and ``role`` is ``None``: no database role reads
    these records. :meth:`append` and :meth:`replace` change the records and the
    version, so a test can check that an LMA reads them again.
    """

    kind = "memory"
    role: str | None = None

    def __init__(self, records: Iterable[Mapping[str, Any]], *, location: Mapping[str, str] | None = None) -> None:
        self._lock = threading.Lock()
        self._records = self._copied(records)
        self._location = {str(key): str(value) for key, value in (location or {}).items()}
        self._generation = 0

    @staticmethod
    def _copied(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
        copied: list[dict[str, Any]] = []
        for record in records:
            if not isinstance(record, Mapping):
                raise TypeError(f"a site record must be a mapping, got {type(record).__name__}")
            copied.append(copy.deepcopy(dict(record)))
        return copied

    def records(self) -> list[dict[str, Any]]:
        """Return copies of every record, in the order they were given."""
        with self._lock:
            return copy.deepcopy(self._records)

    def count(self) -> int:
        """Return how many records the source holds."""
        with self._lock:
            return len(self._records)

    def version(self) -> str:
        """Return ``"<changes>:<count>"``. It changes with every :meth:`append` and :meth:`replace`."""
        with self._lock:
            return f"{self._generation}:{len(self._records)}"

    def location(self) -> dict[str, str]:
        """Return a copy of the names given as ``location``; empty by default."""
        return dict(self._location)

    def append(self, record: Mapping[str, Any]) -> None:
        """Add one record at the end and change the version."""
        [copied] = self._copied([record])
        with self._lock:
            self._records.append(copied)
            self._generation += 1

    def replace(self, records: Iterable[Mapping[str, Any]]) -> None:
        """Replace every record and change the version, even when the count stays the same."""
        copied = self._copied(records)
        with self._lock:
            self._records = copied
            self._generation += 1

    def close(self) -> None:
        """Nothing to close; the records stay in memory."""
