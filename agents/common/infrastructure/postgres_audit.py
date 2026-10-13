"""A digest-chained audit log stored in a PostgreSQL table.

``PostgresAuditSink`` implements the framework's ``AuditSink`` protocol. It
keeps the same chain as ``InMemoryAuditLog``: each record's digest covers every
field and the previous record's digest, so editing or removing a stored record
breaks every digest after it. Both sinks compute the digest with
:func:`agents.common.application.ethical_guard.audit_digest`, so a chain one
writes verifies with the other.

Every operation runs as one role, through
:func:`agents.common.infrastructure.postgres_roles.acting_as`, on a connection
from the factory. A data source's audit chain then lives in that source's own
schema, and only its role can read or write it.

The table must have these columns (``seq`` orders the chain)::

    seq             bigint generated always as identity primary key
    audit_id        text not null unique
    recorded_at     bigint not null
    boundary        text not null
    decision        text not null
    permitted       boolean not null
    record          jsonb not null
    previous_digest text not null unique
    digest          text not null unique

``previous_digest`` is unique, so two writers cannot both extend the same
record.

How an append finds the record it extends:

- The first append of a sink locks the table, reads the last digest and
  inserts, as three statements. Other writers wait for the lock.
- Later appends assume the chain still ends with the record this sink wrote
  last, and send one statement: an INSERT that runs only if the table's last
  digest is still that one. If another writer has appended since, or a record
  was removed, the INSERT adds nothing, or fails on the unique
  ``previous_digest``, and the sink appends again the first way. The check is in
  the database, so a wrong guess costs a retry, never a fork.

The second way saves two round trips per append, which matters when the
database is a pooler 20 ms away and a federated round appends a record per
boundary.

How a check stays cheap: a public demo checks the chain on every audit read,
and the chain grows with every decision. ``verify_chain`` keeps an anchor: the
seq and digest of the last record its previous successful check covered, and
how many records the table held up to it. The next check reads the anchor
record and the ones after it, hashes the anchor record again, and counts the
records up to the anchor in the same statement. A record removed or inserted
before the anchor changes that count, so the check falls back to reading every
record. An edit to a record before the anchor is not seen;
``verify_chain(full=True)`` reads and hashes every record.

The anchor can be stored in a table of its own (``anchor_table``), read and
written as the chain's role, so a new process continues from it instead of
reading the whole chain again. The table holds one row::

    id           boolean primary key default true check (id)
    seq          bigint not null
    digest       text not null
    record_count bigint not null
    verified_at  timestamptz not null default now()

A stored anchor is checked like any other: the record it names must still be
there with the same digest, its content must hash to that digest, and the count
must match. If any of this fails, the check reads every record. A failed check
deletes the stored anchor, so a later check cannot pass by starting after the
break. The anchor saves work and adds no protection: whoever can change the
chain can change its anchor.
"""

from __future__ import annotations

import re
import threading
from typing import Any, Callable, Mapping

from pydantic import ValidationError

from agents.common.application.ethical_guard import audit_digest
from agents.common.domain.governance import AuditRecord
from agents.common.infrastructure.postgres_roles import acting_as, validate_role_name, validate_setting_name

try:
    from psycopg import errors, sql
except ModuleNotFoundError:  # pragma: no cover - exercised only without the driver
    errors = None  # type: ignore[assignment]
    sql = None  # type: ignore[assignment]


#: The columns the sink reads and writes, in table order.
COLUMNS: tuple[str, ...] = (
    "seq",
    "audit_id",
    "recorded_at",
    "boundary",
    "decision",
    "permitted",
    "record",
    "previous_digest",
    "digest",
)

#: The columns of an anchor table, in table order (see the module docstring).
ANCHOR_COLUMNS: tuple[str, ...] = ("id", "seq", "digest", "record_count", "verified_at")

_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

#: An anchor: the seq and digest of the last record a successful check
#: covered, and how many records the table held up to and including it.
Anchor = tuple[int, str, int]

#: The anchor of an empty chain. A check from it reads every record.
NO_ANCHOR: Anchor = (0, "", 0)


def _table_identifier(table: str) -> "sql.Composable":
    """Quote ``schema.table`` (or ``table``) after checking each part."""
    parts = table.split(".")
    if not 1 <= len(parts) <= 2 or not all(_IDENTIFIER.fullmatch(part) for part in parts):
        raise ValueError(f"audit table must be 'schema.table' in lower case, got {table!r}")
    return sql.Identifier(*parts)


class PostgresAuditSink:
    """Store guard decisions in one table, acting as one role.

    Params:
    - `connection_factory`: returns a context manager that gives an autocommit
      connection, for example ``lambda: postgres_roles.connect(dsn)`` (a new
      connection each time, closed when the block ends).
    - `role`: the role every read and write runs as.
    - `table`: the audit table, as ``schema.table``.
    - `settings`: ``SET LOCAL`` settings for every operation's transaction, for
      example ``{"statement_timeout": "15000ms"}``.
    - `anchor_table`: optional, as ``schema.table``. Where the anchor of the
      last successful check is stored (see the module docstring), so a new
      sink on the same table continues from it. Without it, each new sink's
      first check reads every record.

    ``statements`` counts the statements the sink has sent, not counting the
    two :func:`acting_as` adds to each operation. Tests use it to check how
    many round trips an append costs.
    """

    def __init__(
        self,
        connection_factory: Callable[[], Any],
        role: str,
        table: str,
        *,
        settings: Mapping[str, Any] | None = None,
        anchor_table: str | None = None,
    ) -> None:
        if sql is None:
            raise RuntimeError("PostgresAuditSink needs psycopg 3")
        self._connect = connection_factory
        self.role = validate_role_name(role)
        self.table = table
        self._table = _table_identifier(table)
        self.anchor_table = anchor_table
        self._anchor_table = _table_identifier(anchor_table) if anchor_table is not None else None
        self._settings = {validate_setting_name(name): value for name, value in (settings or {}).items()}
        # The digest of the record this sink appended last, or None before
        # the first append and after a failed one.
        self._last_digest: str | None = None
        # The anchor of the last successful check in this sink, or None before
        # the first check and after a failed one.
        self._verified: Anchor | None = None
        self._lock = threading.Lock()
        self.statements = 0
        #: Rows the last ``verify_chain`` call read. Tests use it to check that
        #: a repeated check reads only new rows.
        self.rows_read = 0

    def _scope(self, conn: Any) -> Any:
        return acting_as(conn, self.role, self._settings)

    def _execute(self, conn: Any, query: Any, params: Any = None) -> Any:
        with self._lock:
            self.statements += 1
        return conn.execute(query, params)

    def append(self, record: AuditRecord) -> AuditRecord:
        """Chain the record to the last stored one, store it and return it."""
        with self._lock:
            assumed = self._last_digest
            self._last_digest = None
        if assumed is not None:
            try:
                chained = self._append_after(record, assumed)
            except errors.UniqueViolation:
                # Another writer extended the same record first; the
                # transaction was rolled back. Append the first way.
                chained = None
            if chained is not None:
                self._remember(chained)
                return chained
        chained = self._append_locked(record)
        self._remember(chained)
        return chained

    def _remember(self, chained: AuditRecord) -> None:
        with self._lock:
            self._last_digest = chained.digest

    @staticmethod
    def _chain(record: AuditRecord, previous: str) -> AuditRecord:
        chained = record.model_copy(update={"previous_digest": previous})
        return chained.model_copy(update={"digest": audit_digest(chained)})

    @staticmethod
    def _values(chained: AuditRecord) -> tuple:
        return (
            chained.audit_id,
            chained.recorded_at,
            chained.boundary,
            chained.decision,
            chained.permitted,
            _jsonb(chained),
            chained.previous_digest,
            chained.digest,
        )

    def _append_locked(self, record: AuditRecord) -> AuditRecord:
        """Lock the table, read the last digest and insert. Three statements."""
        with self._connect() as conn, self._scope(conn):
            # Writers queue here, so each one reads the digest it extends after
            # the previous writer has committed. Readers are not blocked. The
            # two statements bind no parameters, so they go as one message.
            cursor = self._execute(
                conn,
                sql.SQL("LOCK TABLE {table} IN SHARE ROW EXCLUSIVE MODE; "
                        "SELECT digest FROM {table} ORDER BY seq DESC LIMIT 1").format(table=self._table),
            )
            cursor.nextset()
            row = cursor.fetchone()
            chained = self._chain(record, _column(row, 0, "digest") if row else "")
            self._execute(
                conn,
                sql.SQL(
                    "INSERT INTO {} (audit_id, recorded_at, boundary, decision, permitted, "
                    "record, previous_digest, digest) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
                ).format(self._table),
                self._values(chained),
            )
        return chained

    def _append_after(self, record: AuditRecord, assumed: str) -> AuditRecord | None:
        """Insert after ``assumed`` if the chain still ends there. One statement.

        Returns ``None`` when the chain ends somewhere else. Raises
        ``UniqueViolation`` when another writer extended ``assumed`` at the same
        moment.
        """
        chained = self._chain(record, assumed)
        with self._connect() as conn, self._scope(conn):
            row = self._execute(
                conn,
                sql.SQL(
                    "INSERT INTO {table} (audit_id, recorded_at, boundary, decision, permitted, "
                    "record, previous_digest, digest) "
                    "SELECT %s, %s, %s, %s, %s, %s, %s, %s "
                    "WHERE coalesce((SELECT digest FROM {table} ORDER BY seq DESC LIMIT 1), '') = %s "
                    "RETURNING seq"
                ).format(table=self._table),
                (*self._values(chained), assumed),
            ).fetchone()
        return chained if row is not None else None

    def list_recent(self, limit: int = 50) -> list[AuditRecord]:
        """Return the most recent records, newest first."""
        if limit < 1:
            return []
        with self._connect() as conn, self._scope(conn):
            rows = self._execute(
                conn,
                sql.SQL("SELECT record FROM {} ORDER BY seq DESC LIMIT %s").format(self._table),
                (int(limit),),
            ).fetchall()
        return [AuditRecord.model_validate(_record_of(row)) for row in rows]

    def count(self) -> int:
        """The number of records in the table."""
        with self._connect() as conn, self._scope(conn):
            row = self._execute(conn, sql.SQL("SELECT count(*) FROM {}").format(self._table)).fetchone()
        return int(_column(row, 0, "count"))

    def verify_chain(self, full: bool = False) -> bool:
        """Recompute the digests of the chain and report whether it is intact.

        Params:
        - `full`: read and hash every record. By default the check starts at
          an anchor: the last record the previous successful check in this
          sink covered, or else the anchor stored in ``anchor_table``. It reads
          that record and the ones after it, and counts the records up to it.
          With no anchor, it reads every record.

        The default check catches a record edited after the anchor, an edit to
        the anchor record, and a record removed or inserted anywhere. It does
        not see an edit to a record before the anchor; ``full=True`` does. If
        the default check fails, a full check decides, so it never reports a
        break that a full check would accept (for example, after the last
        record was removed). A failed check is not remembered and deletes the
        stored anchor, so the next check reads everything again.

        The stored anchor is read, the records are checked and the new anchor
        is stored in one transaction, as the chain's role.
        """
        with self._lock:
            anchor = None if full or self._verified == NO_ANCHOR else self._verified
        with self._connect() as conn, self._scope(conn):
            # What the stored anchor is taken to be: this sink stored its own
            # anchor after its last check, so it reads the table only when it
            # has none.
            stored = anchor
            if anchor is None and not full and self._anchor_table is not None:
                anchor = stored = self._read_anchor(conn)
            result = self._check(conn, anchor) if anchor is not None else None
            if result is None:
                result = self._check(conn, None)
            if self._anchor_table is not None and (full or result != stored):
                self._store_anchor(conn, result)
        with self._lock:
            self._verified = result
        return result is not None

    def _read_anchor(self, conn: Any) -> Anchor | None:
        row = self._execute(
            conn, sql.SQL("SELECT seq, digest, record_count FROM {} WHERE id").format(self._anchor_table)
        ).fetchone()
        if row is None or int(_column(row, 0, "seq")) < 1:
            return None
        return (int(_column(row, 0, "seq")), str(_column(row, 1, "digest")), int(_column(row, 2, "record_count")))

    def _store_anchor(self, conn: Any, anchor: Anchor | None) -> None:
        """Store the anchor, or delete the stored one after a failed check or for an empty chain."""
        if anchor is None or anchor == NO_ANCHOR:
            self._execute(conn, sql.SQL("DELETE FROM {}").format(self._anchor_table))
            return
        self._execute(
            conn,
            sql.SQL(
                "INSERT INTO {} (id, seq, digest, record_count, verified_at) VALUES (true, %s, %s, %s, now()) "
                "ON CONFLICT (id) DO UPDATE SET seq = excluded.seq, digest = excluded.digest, "
                "record_count = excluded.record_count, verified_at = excluded.verified_at"
            ).format(self._anchor_table),
            anchor,
        )

    def _check(self, conn: Any, anchor: Anchor | None) -> Anchor | None:
        """Check the chain from ``anchor``, or from the start.

        Returns the anchor of the last record, :data:`NO_ANCHOR` for an empty
        table, or ``None`` if the chain does not verify.
        """
        if anchor is None:
            rows = self._execute(
                conn, sql.SQL("SELECT seq, record, previous_digest, digest FROM {} ORDER BY seq").format(self._table)
            ).fetchall()
        else:
            # The count rides on every row, so the check is still one statement.
            rows = self._execute(
                conn,
                sql.SQL(
                    "SELECT seq, record, previous_digest, digest, "
                    "(SELECT count(*) FROM {table} WHERE seq <= %s) AS checked "
                    "FROM {table} WHERE seq >= %s ORDER BY seq"
                ).format(table=self._table),
                (anchor[0], anchor[0]),
            ).fetchall()
        self.rows_read = len(rows)
        try:
            return self._walk(rows, anchor)
        except ValidationError:
            # A stored record that is not an audit record breaks the chain.
            return None

    @staticmethod
    def _walk(rows: list[Any], anchor: Anchor | None) -> Anchor | None:
        last = NO_ANCHOR
        if anchor is not None:
            # The anchor must still be there, unchanged, as the first row read,
            # with as many records up to it as when it was checked.
            if not rows or _column(rows[0], 0, "seq") != anchor[0] or _column(rows[0], 3, "digest") != anchor[1]:
                return None
            if int(_column(rows[0], 4, "checked")) != anchor[2]:
                return None
            # Re-hash the anchor too, so an edit to its content is caught.
            first = AuditRecord.model_validate(_column(rows[0], 1, "record"))
            if first.digest != anchor[1] or first.digest != audit_digest(first.model_copy(update={"digest": ""})):
                return None
            last, rows = anchor, rows[1:]
        previous = last[1]
        count = last[2]
        for row in rows:
            record = AuditRecord.model_validate(_column(row, 1, "record"))
            stored_previous, stored_digest = _column(row, 2, "previous_digest"), _column(row, 3, "digest")
            if record.previous_digest != previous or stored_previous != previous:
                return None
            if stored_digest != record.digest:
                return None
            if record.digest != audit_digest(record.model_copy(update={"digest": ""})):
                return None
            previous = record.digest
            count += 1
            last = (int(_column(row, 0, "seq")), record.digest, count)
        return last


def _jsonb(record: AuditRecord) -> Any:
    from psycopg.types.json import Jsonb

    return Jsonb(record.model_dump(mode="json"))


def _column(row: Any, index: int, name: str) -> Any:
    """Read a column from a tuple row or a dict row."""
    if isinstance(row, dict):
        return row[name]
    return row[index]


def _record_of(row: Any) -> dict[str, Any]:
    return _column(row, 0, "record")
