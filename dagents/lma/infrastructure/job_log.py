"""Where an LMA records each federated job it refused or ran: in memory, or in a Postgres table.

Both logs implement :class:`dagents.common.application.ports.JobLog` and pass
the same test suite (``dagents/tests/test_job_log.py``).

``PostgresJobLog`` runs every operation in one transaction on a kept
connection, acting as the site's role
(:meth:`dagents.common.infrastructure.postgres_lease.KeptConnection.acting_as`).
The table must have these columns, which are the columns of the healthcare
demo's ``coordinator.site_contributions`` table (migration ``0002``)::

    contribution_id bigint generated always as identity primary key
    round_id        text not null check (length(round_id) between 1 and 200)
    phase           text not null check (length(phase) between 1 and 64)
    payload         jsonb not null
    submitted_at    timestamptz not null default now()

The log inserts ``round_id``, ``phase`` and ``payload`` only. A table shared by
several sites adds ``site_role name not null default current_user`` and
row-level security that lets each site role insert and read only its own rows;
the log then needs no ``WHERE`` clause to read its own entries.

The payload holds ``participation``, ``contributed_examples``,
``update_norm``, ``update_digest``, ``metrics`` and ``job_digest``, and
``reason`` when the job was refused, which are the keys the healthcare demo
stored before the LMA had a job log.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import json
import logging
import threading
from typing import Any, Mapping

from pydantic import ValidationError

from dagents.common.domain.site import JobLogEntry
from dagents.common.infrastructure.postgres_lease import KeptConnection
from dagents.common.infrastructure.postgres_roles import table_identifier, validate_role_name

try:  # psycopg is optional for an LMA that keeps its job log in memory
    from psycopg import sql
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
except ModuleNotFoundError:  # pragma: no cover - exercised only without the driver
    sql = None  # type: ignore[assignment]
    dict_row = None  # type: ignore[assignment]
    Jsonb = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

#: The payload keys every entry stores. ``reason`` is added when there is one.
PAYLOAD_KEYS = ("participation", "contributed_examples", "update_norm", "update_digest", "metrics", "job_digest")

#: How many entries the in-memory log keeps before it drops the oldest.
DEFAULT_CAPACITY = 1000


def check_limit(limit: Any) -> int:
    """Return ``limit`` when it is a whole number of at least 1, or raise ``ValueError``."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError(f"limit must be a whole number of at least 1, got {limit!r}")
    return limit


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def payload_of(entry: JobLogEntry) -> dict[str, Any]:
    """The JSON payload stored for an entry: the demo's keys, and ``reason`` when there is one."""
    data = entry.model_dump(mode="json")
    payload = {key: data[key] for key in PAYLOAD_KEYS}
    if entry.reason:
        payload["reason"] = entry.reason
    # Through JSON, as the database stores it, so both logs return the same types.
    return json.loads(json.dumps(payload, allow_nan=False))


def entry_of(round_id: str, phase: str, payload: Mapping[str, Any], submitted_at: str | None) -> JobLogEntry:
    """Read one stored row back as an entry. Raises ``ValidationError`` when it cannot be read."""
    return JobLogEntry(
        round_id=round_id,
        phase=phase,
        participation=payload.get("participation"),
        contributed_examples=payload.get("contributed_examples") or 0,
        update_norm=payload.get("update_norm"),
        update_digest=payload.get("update_digest"),
        job_digest=payload.get("job_digest") or "",
        metrics=payload.get("metrics") or {},
        reason=payload.get("reason"),
        submitted_at=submitted_at,
    )


class InMemoryJobLog:
    """Job log entries in memory, newest last, at most ``capacity`` of them."""

    kind = "memory"

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        self._entries: deque[tuple[str, str, dict[str, Any], str]] = deque(maxlen=check_limit(capacity))
        self._lock = threading.Lock()

    def append(self, entry: JobLogEntry) -> JobLogEntry:
        """Store one entry and return it with ``submitted_at`` set."""
        stored = (entry.round_id, entry.phase, payload_of(entry), _now())
        with self._lock:
            self._entries.append(stored)
        return entry_of(*stored)

    def recent(self, limit: int = 20) -> list[JobLogEntry]:
        """The newest entries first, at most ``limit`` of them."""
        check_limit(limit)
        with self._lock:
            newest = list(self._entries)[-limit:][::-1]
        return [entry_of(round_id, phase, json.loads(json.dumps(payload)), at) for round_id, phase, payload, at in newest]


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


class PostgresJobLog:
    """Job log entries in one Postgres table, every operation acting as the site's role.

    Params:
    - `lease`: the kept connection, shared with the site's other stores.
    - `role`: the site's role. Take it from configuration, never from a
      request.
    - `table`: the job table, as ``schema.table``.

    Errors from the connection come from the lease: ``StoreUnavailableError``
    and ``StoreNotSetUpError``, with no connection details in their messages.
    A stored row that cannot be read as an entry is left out of
    :meth:`recent`, with a warning in the log.
    """

    kind = "postgres"

    def __init__(self, lease: KeptConnection, role: str, table: str) -> None:
        if sql is None:
            raise RuntimeError("PostgresJobLog needs psycopg 3")
        self._lease = lease
        self.role = validate_role_name(role)
        self.table = table
        self._table = table_identifier(table, "job log table")
        self._insert = sql.SQL(
            "INSERT INTO {table} (round_id, phase, payload) VALUES (%s, %s, %s) RETURNING submitted_at"
        ).format(table=self._table)
        self._select = sql.SQL(
            "SELECT round_id, phase, payload, submitted_at FROM {table} ORDER BY contribution_id DESC LIMIT %s"
        ).format(table=self._table)

    def append(self, entry: JobLogEntry) -> JobLogEntry:
        """Store one entry, acting as the site, and return it with ``submitted_at`` set."""
        payload = payload_of(entry)
        with self._lease.acting_as(self.role) as conn, conn.cursor(row_factory=dict_row) as cursor:
            row = cursor.execute(self._insert, (entry.round_id, entry.phase, Jsonb(payload))).fetchone()
        return entry_of(entry.round_id, entry.phase, payload, _timestamp(row["submitted_at"]))

    def recent(self, limit: int = 20) -> list[JobLogEntry]:
        """The site's newest entries first, at most ``limit`` of them."""
        check_limit(limit)
        with self._lease.acting_as(self.role) as conn, conn.cursor(row_factory=dict_row) as cursor:
            rows = cursor.execute(self._select, (limit,)).fetchall()
        entries: list[JobLogEntry] = []
        for row in rows:
            payload = row["payload"] if isinstance(row["payload"], Mapping) else {}
            try:
                entries.append(entry_of(row["round_id"], row["phase"], payload, _timestamp(row["submitted_at"])))
            except ValidationError:
                logger.warning("a job log entry for round %s cannot be read; it is left out", row["round_id"])
        return entries
