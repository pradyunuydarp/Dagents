"""Where the GMA keeps round records, site enrolments and pending updates in Postgres.

| Store | Implements | Table owner |
|---|---|---|
| ``PostgresRoundRepository`` | :class:`dagents.common.application.federation.RoundRepository` | the coordinator role |
| ``PostgresEnrolmentStore`` | :class:`dagents.common.application.federation.SiteRegistry` | the coordinator role |
| ``PostgresUpdateStore`` | :class:`dagents.gma.infrastructure.update_store.UpdateStore` | the update role |

Each passes the same test suite as its memory twin
(``dagents/tests/test_round_store.py``). A GMA builds them from
``GMA_ROUND_TABLE``, ``GMA_ENROLMENT_TABLE`` and ``GMA_UPDATE_TABLE`` (with
``GMA_UPDATE_ROLE``), on its one coordinator connection, or an application
hands them to its GMA through ``GmaDeps(rounds=..., enrolments=...,
updates=...)``.

Each store takes a kept connection, a role and a table, and runs every
method in one transaction on that connection, acting as the role
(:meth:`dagents.common.infrastructure.postgres_lease.KeptConnection.acting_as`).
Create each table as the role that owns it, from the DDL constants below:
``ROUND_TABLE_DDL`` and ``ENROLMENT_TABLE_DDL`` as the coordinator role, and
``UPDATE_TABLE_DDL`` as the update role. ``create_table_sql`` fills in a table
name. The GMA creates no table itself. The update role must differ from the
coordinator role, and the coordinator role holds no grant on the updates
table, so nothing acting as the coordinator role can read an update vector.

Rules every store follows:

- **json columns, bound with psycopg's ``Json``, never ``jsonb``.** A
  ``jsonb`` column returns ``-0.0`` as ``0.0``, and ``1e16`` and larger floats
  as whole numbers. A ``Jsonb`` parameter does the same even when the column
  is ``json``, because the server converts it to ``jsonb`` first. A ``json``
  column keeps the text that was sent, so a record comes back as it was
  stored. ``json`` cannot hold NaN or an infinite number, so a round record
  that holds one, or any record that would not read back equal, is refused
  with :class:`InvalidRoundRecord` (a ``ValueError``) and nothing is written.
  The memory store refuses the same records
  (:func:`dagents.common.application.federation.round_record_json`).
- **JSON is read as text and decoded in Python**, row by row, so a value the
  decoder refuses makes that one row unreadable instead of failing the whole
  read.
- **Errors come from the lease:** ``StoreUnavailableError`` when the database
  cannot be reached or an operation fails, and ``StoreNotSetUpError`` when a
  table, column, role or grant is missing. No message holds the connection
  string, its password or its host.
- **A stored row this version cannot read** makes ``get`` and ``update``
  raise ``StoreNotSetUpError`` naming the round or site. ``list`` leaves it
  out and logs a warning, so one bad row does not hide the others. An update
  row is never left out: ``get`` and ``updates`` raise, so an aggregation does
  not run without it. A row cannot be read when:

  - its JSON is text ``json`` accepts but Python cannot decode: nested deeper
    than Python's decoder goes, or a whole number with more digits than
    Python reads (4,300);
  - its JSON does not match the contract, or holds a number that is not
    finite, such as ``1e400``, which Python reads as infinity;
  - its ``record_version`` is unknown, or it names another id.

The round table copies ``phase`` and ``status`` out of the record, so
:meth:`PostgresRoundRepository.unfinished` and
:meth:`PostgresRoundRepository.quarantine` work from the columns. Recovery
after a restart can then close a round whose record cannot be read.
"""

from __future__ import annotations

import json
import logging
import math
import operator
import time
from typing import Any, Callable, Mapping

from dagents.common.application.federation import UNFINISHED_STATUSES, UnfinishedRound, round_record_json
from dagents.common.domain.errors import RoundConflict, StoreNotSetUpError
from dagents.common.domain.federation import RoundRecord, SiteRegistration, UpdateReceipt, UpdateUpload
from dagents.common.extensions.registry import ModelKind
from dagents.common.infrastructure.postgres_lease import KeptConnection
from dagents.common.infrastructure.postgres_roles import RoleScopeError, table_identifier, validate_role_name
from dagents.gma.infrastructure.update_store import StoredUpdate, checked_update

try:  # psycopg is optional for callers that keep round state in memory
    from psycopg import sql
    from psycopg.rows import dict_row
    from psycopg.types.json import Json
except ModuleNotFoundError:  # pragma: no cover - exercised only without the driver
    sql = None  # type: ignore[assignment]
    dict_row = None  # type: ignore[assignment]
    Json = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

#: The ``record_version`` this release writes and reads. A later release that
#: changes how a record is stored writes a new number, so it can read old rows
#: on purpose or refuse them.
RECORD_VERSION = 1

#: The rounds table. ``{table}`` is the table's name, such as ``coord.rounds``.
#:
#: - ``seq`` gives the order rounds were created in; ``created_at`` is in
#:   whole seconds, so two rounds can share it.
#: - ``phase`` and ``status`` copy the record, so recovery can work from the
#:   columns. ``closed_reason`` is set only by ``quarantine``.
#: - The partial index covers the rounds ``unfinished`` reads.
ROUND_TABLE_DDL = """\
create table {table} (
  seq bigint generated always as identity unique,
  round_id text primary key,
  phase text not null,
  status text not null check (status in ('planned', 'dispatched', 'collecting', 'aggregated', 'closed')),
  record_version integer not null,
  record json not null,
  closed_reason text,
  created_at bigint not null,
  updated_at bigint not null
);

create index on {table} (seq) where status in ('planned', 'dispatched', 'collecting');
"""

#: The site enrolments table. ``{table}`` is the table's name, such as
#: ``coord.site_enrolments``.
ENROLMENT_TABLE_DDL = """\
create table {table} (
  site_id text primary key,
  registration json not null,
  saved_at timestamptz not null default now()
);
"""

#: The updates table. ``{table}`` is the table's name, such as
#: ``coord_updates.updates``. Create it as the update role, never as the
#: coordinator role.
#:
#: - Each site's update for a round is one row. The row whose ``site_id`` is
#:   ``''`` is the round's gate row: it holds no update, and ``discarded``
#:   on it refuses every later upload for the round.
UPDATE_TABLE_DDL = """\
create table {table} (
  round_id text not null,
  site_id text not null,
  update_values json,
  digest text,
  job_digest text,
  received_at double precision not null,
  discarded boolean not null default false,
  primary key (round_id, site_id),
  check ((site_id = '') = (update_values is null))
);
"""

#: The largest number a ``bigint`` holds, the most rows ``list`` can return.
_BIGINT_MAX = 2**63 - 1

_UNFINISHED = sql.SQL(", ").join(sql.Literal(status) for status in UNFINISHED_STATUSES) if sql is not None else None


def create_table_sql(ddl: str, table: str) -> Any:
    """The statements of a DDL constant for ``table``, with the name checked and quoted.

    Params:
    - `ddl`: ``ROUND_TABLE_DDL`` or ``ENROLMENT_TABLE_DDL``.
    - `table`: ``schema.table`` (or ``table``) in lower case; see
      :func:`dagents.common.infrastructure.postgres_roles.table_identifier`.

    Returns a ``psycopg.sql.Composed`` to run, as the role that will own the
    table, with ``connection.execute``. Raises ``ValueError`` for any other
    table name.
    """
    return sql.SQL(ddl).format(table=table_identifier(table, "table"))


def json_parameter(value: Any, what: str) -> Any:
    """``value`` as a ``Json`` parameter, written once as JSON text.

    Raises ``ValueError`` naming `what` when the value holds NaN or an
    infinite number, which a ``json`` column cannot store, before any
    statement runs.
    """
    try:
        text = json.dumps(value, allow_nan=False)
    except ValueError as error:
        raise ValueError(f"{what} holds NaN or an infinite number, which a json column cannot store") from error
    return Json(value, dumps=lambda _value: text)


def _record_parameter(record: RoundRecord) -> Any:
    """The record as a ``Json`` parameter. Raises :class:`InvalidRoundRecord` when it would not read back equal.

    The check is :func:`round_record_json`, the one the memory store runs, so
    both stores refuse the same records.
    """
    return json_parameter(round_record_json(record), f"round {record.manifest.round_id}")


def _decoded(text: Any) -> Any:
    """``text`` decoded as JSON, or ``None`` when it is not text Python can decode.

    PostgreSQL's ``json`` type accepts text that Python's decoder refuses:
    nesting deeper than the decoder goes (``RecursionError``) and whole
    numbers of more than 4,300 digits (``ValueError``). Decoding here, row by
    row, keeps such a row from failing a whole read. ``None`` is safe as the
    answer, because no stored value this module reads is ``null``.
    """
    if not isinstance(text, str):
        return None
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        return None


def _require_driver(store: str) -> None:
    if sql is None:
        raise RuntimeError(f"{store} needs psycopg 3")


class PostgresRoundRepository:
    """Round records in one Postgres table, every method acting as one role.

    Params:
    - `lease`: the kept connection, shared with the GMA's other stores.
    - `role`: the role every read and write acts as, the coordinator role
      that owns the table. Take it from configuration, never from a request.
    - `table`: the rounds table, as ``schema.table``, created from
      :data:`ROUND_TABLE_DDL`.

    Building the store opens no connection. Each method runs in one
    transaction:

    | Method | Statements |
    |---|---|
    | ``create`` | ``INSERT ... ON CONFLICT (round_id) DO NOTHING RETURNING seq``; no row back raises :class:`RoundConflict` |
    | ``update`` | ``SELECT ... FOR UPDATE``, then ``change(record)`` in Python, then ``UPDATE`` of the record and its columns |
    | ``save`` | an upsert that replaces the record and clears ``closed_reason`` |
    | ``get``, ``list`` | one ``SELECT``; ``list`` orders by ``seq``, newest first |
    | ``unfinished`` | one ``SELECT`` of the unfinished rounds, oldest first, through the partial index |
    | ``quarantine`` | one ``UPDATE`` of ``status``, ``closed_reason`` and ``updated_at`` that leaves the record as it is |

    ``update`` holds the row's lock while ``change`` runs, so changes from two
    threads or two processes to one round never overwrite each other. The
    ``change`` callback must not use a store: on the same thread that opens a
    second connection, which could wait for the lock its own caller holds.

    A quarantined round reads as ``closed``, with the quarantine reason as its
    ``abandoned_reason`` unless it already had one, and with the time of the
    quarantine as its ``updated_at``.
    """

    kind = "postgres"

    #: The rows outlive the process, so a round id stays used after a restart.
    survives_restart = True

    def __init__(self, lease: KeptConnection, role: str, table: str) -> None:
        _require_driver("PostgresRoundRepository")
        self._lease = lease
        self.role = validate_role_name(role)
        self.table = table
        self._table = table_identifier(table, "rounds table")
        self.location = {"table": table, "role": self.role}
        # The record comes as text and is decoded in ``_parse``; see ``_decoded``.
        columns = "round_id, status, phase, record_version, record::text AS record, closed_reason, updated_at"
        self._select = sql.SQL("SELECT " + columns + " FROM {table}").format(table=self._table)
        self._insert = sql.SQL(
            "INSERT INTO {table} (round_id, phase, status, record_version, record, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)"
        ).format(table=self._table)

    # --- reading rows ----------------------------------------------------------

    def _parse(self, row: Mapping[str, Any]) -> RoundRecord | None:
        """The row's record, or ``None`` when this version cannot read it.

        A record that decodes but that the store would refuse to write, such
        as one holding ``1e400`` (infinity once decoded), counts as
        unreadable too: no change to it could be written back, so recovery
        must be able to close it from its columns.
        """
        if row["record_version"] != RECORD_VERSION:
            return None
        try:
            record = RoundRecord.model_validate(_decoded(row["record"]))
            if record.manifest.round_id != row["round_id"]:
                return None
            round_record_json(record)
        except (ValueError, RecursionError):  # ValidationError and InvalidRoundRecord are ValueErrors
            return None
        return record

    @staticmethod
    def _as_stored(record: RoundRecord, row: Mapping[str, Any]) -> RoundRecord:
        """The record as the store returns it: closed, when ``quarantine`` closed its row."""
        if row["status"] != "closed" or row["closed_reason"] is None:
            return record
        update: dict[str, object] = {"status": "closed", "updated_at": row["updated_at"]}
        if record.abandoned_reason is None:
            update["abandoned_reason"] = row["closed_reason"]
        return record.model_copy(update=update)

    def _unreadable(self, row: Mapping[str, Any]) -> StoreNotSetUpError:
        if row["record_version"] != RECORD_VERSION:
            why = f"its record version {row['record_version']} is not one this version reads"
        else:
            why = (
                "its record is not JSON this version can decode, does not match the round record this version "
                "reads, holds a number that is not finite, or names another round"
            )
        return StoreNotSetUpError(f"the stored round {row['round_id']} in {self.table} cannot be read: {why}")

    def _readable(self, row: Mapping[str, Any]) -> RoundRecord:
        record = self._parse(row)
        if record is None:
            raise self._unreadable(row)
        return self._as_stored(record, row)

    def _rows(self, query: Any, values: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._lease.acting_as(self.role) as conn, conn.cursor(row_factory=dict_row) as cursor:
            return list(cursor.execute(query, values).fetchall())

    @staticmethod
    def _values(record: RoundRecord) -> tuple[Any, ...]:
        """``phase, status, record_version, record, created_at, updated_at`` for the record."""
        return (
            record.manifest.phase,
            record.status,
            RECORD_VERSION,
            _record_parameter(record),
            record.created_at,
            record.updated_at,
        )

    # --- RoundRepository ---------------------------------------------------------

    def save(self, record: RoundRecord) -> RoundRecord:
        """Store the record, replacing any record with its round id. A replaced round keeps its place in the order.

        Raises :class:`InvalidRoundRecord` for a record the store cannot keep, and writes nothing.
        """
        query = sql.SQL(
            "{insert} ON CONFLICT (round_id) DO UPDATE SET phase = excluded.phase, status = excluded.status, "
            "record_version = excluded.record_version, record = excluded.record, closed_reason = NULL, "
            "created_at = excluded.created_at, updated_at = excluded.updated_at"
        ).format(insert=self._insert)
        values = (record.manifest.round_id, *self._values(record))
        with self._lease.acting_as(self.role) as conn:
            conn.execute(query, values)
        return record

    def create(self, record: RoundRecord) -> RoundRecord:
        """Store a new round.

        Raises :class:`RoundConflict` when its round id is stored, quarantined
        rounds included, and :class:`InvalidRoundRecord` for a record the
        store cannot keep. Either way it writes nothing.
        """
        round_id = record.manifest.round_id
        query = sql.SQL("{insert} ON CONFLICT (round_id) DO NOTHING RETURNING seq").format(insert=self._insert)
        values = (round_id, *self._values(record))
        with self._lease.acting_as(self.role) as conn:
            created = conn.execute(query, values).fetchone()
        if created is None:
            raise RoundConflict(f"round {round_id} already exists; a new round needs a new round id")
        return record

    def update(self, round_id: str, change: Callable[[RoundRecord], RoundRecord]) -> RoundRecord:
        """Replace the round with ``change(record)`` while holding its row's lock.

        Raises ``KeyError`` for an unknown round, ``ValueError`` when the
        change returns another round, :class:`InvalidRoundRecord` when the
        changed record holds a value the store cannot keep, and
        ``StoreNotSetUpError`` when the stored record cannot be read;
        ``change`` is then not called. Any error leaves the row as it was.
        """
        lock = sql.SQL("{select} WHERE round_id = %s FOR UPDATE").format(select=self._select)
        write = sql.SQL(
            "UPDATE {table} SET phase = %s, status = %s, record_version = %s, record = %s, created_at = %s, "
            "updated_at = %s WHERE round_id = %s"
        ).format(table=self._table)
        with self._lease.acting_as(self.role) as conn, conn.cursor(row_factory=dict_row) as cursor:
            row = cursor.execute(lock, (round_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown round: {round_id}")
            changed = change(self._readable(row))
            if changed.manifest.round_id != round_id:
                raise ValueError(f"a change to round {round_id} returned round {changed.manifest.round_id}")
            cursor.execute(write, (*self._values(changed), round_id))
        written = {**row, "status": changed.status, "updated_at": changed.updated_at}
        return self._as_stored(changed, written)

    def get(self, round_id: str) -> RoundRecord | None:
        """The round, or ``None``. Raises ``StoreNotSetUpError`` when its record cannot be read."""
        rows = self._rows(sql.SQL("{select} WHERE round_id = %s").format(select=self._select), (round_id,))
        return self._readable(rows[0]) if rows else None

    def list(self, limit: int = 20) -> list[RoundRecord]:
        """The newest rounds first, cut as Python's ``rounds[:limit]`` would cut them.

        ``0`` returns none, and a negative ``limit`` leaves out that many of
        the oldest rounds. A record that cannot be read is left out, with a
        warning in the log.
        """
        count = None if limit is None else operator.index(limit)
        newest = sql.SQL("{select} ORDER BY seq DESC").format(select=self._select)
        if count is None:
            rows = self._rows(newest)
        elif count >= 0:
            rows = self._rows(sql.SQL("{newest} LIMIT %s").format(newest=newest), (min(count, _BIGINT_MAX),))
        else:
            query = sql.SQL("{newest} LIMIT (SELECT greatest(count(*) + %s, 0) FROM {table})").format(
                newest=newest, table=self._table
            )
            rows = self._rows(query, (max(count, -_BIGINT_MAX),))
        records: list[RoundRecord] = []
        for row in rows:
            record = self._parse(row)
            if record is None:
                logger.warning("%s; it is left out of the list of rounds", self._unreadable(row))
                continue
            records.append(self._as_stored(record, row))
        return records

    def unfinished(self) -> list[UnfinishedRound]:
        """Every planned, dispatched or collecting round, oldest first.

        The status and phase come from the columns. ``readable`` is false when
        the record does not match the contract or its ``record_version`` is
        unknown, so recovery can close the round without reading it.
        """
        query = sql.SQL("{select} WHERE status IN ({unfinished}) ORDER BY seq").format(
            select=self._select, unfinished=_UNFINISHED
        )
        return [
            UnfinishedRound(
                round_id=row["round_id"],
                status=row["status"],
                phase=row["phase"],
                readable=self._parse(row) is not None,
            )
            for row in self._rows(query)
        ]

    def quarantine(self, round_id: str, reason: str) -> bool:
        """Close an unfinished round from its columns, leaving its record as it is.

        Sets ``status`` to ``closed``, ``closed_reason`` to ``reason`` and
        ``updated_at`` to now. The round id stays used. Returns ``False`` for
        an ``aggregated`` or ``closed`` round, which it leaves as it was.
        Raises ``KeyError`` for an unknown round and ``ValueError`` for an
        empty reason.
        """
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("a quarantine needs a reason")
        close = sql.SQL(
            "UPDATE {table} SET status = 'closed', closed_reason = %s, updated_at = %s "
            "WHERE round_id = %s AND status IN ({unfinished}) RETURNING round_id"
        ).format(table=self._table, unfinished=_UNFINISHED)
        exists = sql.SQL("SELECT 1 FROM {table} WHERE round_id = %s").format(table=self._table)
        with self._lease.acting_as(self.role) as conn:
            if conn.execute(close, (reason, int(time.time()), round_id)).fetchone() is not None:
                return True
            if conn.execute(exists, (round_id,)).fetchone() is None:
                raise KeyError(f"unknown round: {round_id}")
        return False


class PostgresEnrolmentStore:
    """Site enrolments in one Postgres table, every method acting as one role.

    Params:
    - `lease`: the kept connection, shared with the GMA's other stores.
    - `role`: the role every read and write acts as, the coordinator role
      that owns the table. Take it from configuration, never from a request.
    - `table`: the enrolments table, as ``schema.table``, created from
      :data:`ENROLMENT_TABLE_DDL`.

    ``save`` is an upsert, so the last writer wins. ``list`` orders by
    ``site_id COLLATE "C"``, which is the order of Python's ``sorted()``. An
    enrolment that cannot be read makes ``get`` raise ``StoreNotSetUpError``,
    and ``list`` leaves it out with a warning in the log, so the site is not
    selected for a round.
    """

    kind = "postgres"

    def __init__(self, lease: KeptConnection, role: str, table: str) -> None:
        _require_driver("PostgresEnrolmentStore")
        self._lease = lease
        self.role = validate_role_name(role)
        self.table = table
        self._table = table_identifier(table, "enrolments table")
        self.location = {"table": table, "role": self.role}
        # The enrolment comes as text and is decoded in ``_parse``; see ``_decoded``.
        self._select = sql.SQL("SELECT site_id, registration::text AS registration FROM {table}").format(
            table=self._table
        )

    def _parse(self, row: Mapping[str, Any]) -> SiteRegistration | None:
        try:
            registration = SiteRegistration.model_validate(_decoded(row["registration"]))
        except (ValueError, RecursionError):  # a pydantic ValidationError is a ValueError
            return None
        return registration if registration.site_id == row["site_id"] else None

    def _rows(self, query: Any, values: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._lease.acting_as(self.role) as conn, conn.cursor(row_factory=dict_row) as cursor:
            return list(cursor.execute(query, values).fetchall())

    def _unreadable(self, site_id: str) -> str:
        return (
            f"the stored enrolment of site {site_id} in {self.table} is not JSON this version can decode, "
            "or does not match the enrolment this version reads"
        )

    def save(self, registration: SiteRegistration) -> SiteRegistration:
        """Store the enrolment, replacing any enrolment of the same site."""
        query = sql.SQL(
            "INSERT INTO {table} (site_id, registration) VALUES (%s, %s) "
            "ON CONFLICT (site_id) DO UPDATE SET registration = excluded.registration, saved_at = now()"
        ).format(table=self._table)
        value = json_parameter(registration.model_dump(mode="json"), f"the enrolment of site {registration.site_id}")
        with self._lease.acting_as(self.role) as conn:
            conn.execute(query, (registration.site_id, value))
        return registration

    def get(self, site_id: str) -> SiteRegistration | None:
        """The site's enrolment, or ``None``. Raises ``StoreNotSetUpError`` when it cannot be read."""
        rows = self._rows(sql.SQL("{select} WHERE site_id = %s").format(select=self._select), (site_id,))
        if not rows:
            return None
        registration = self._parse(rows[0])
        if registration is None:
            raise StoreNotSetUpError(self._unreadable(site_id))
        return registration

    def list(self) -> list[SiteRegistration]:
        """Every enrolment, in the order of Python's ``sorted()`` on the site ids."""
        rows = self._rows(sql.SQL('{select} ORDER BY site_id COLLATE "C"').format(select=self._select))
        registrations: list[SiteRegistration] = []
        for row in rows:
            registration = self._parse(row)
            if registration is None:
                logger.warning("%s; it is left out of the list of enrolments", self._unreadable(row["site_id"]))
                continue
            registrations.append(registration)
        return registrations


#: The ``site_id`` of a round's gate row.
_GATE = ""


def _stored_update(row: Mapping[str, Any]) -> StoredUpdate | None:
    """The row as a stored update, or ``None`` when this version cannot read it.

    ``update_values`` comes as text; see :func:`_decoded`. A value Python
    reads as infinity, such as ``1e400``, makes the row unreadable.
    """
    values = _decoded(row["update_values"])
    if not isinstance(values, dict) or not isinstance(row["digest"], str) or not isinstance(row["job_digest"], str):
        return None
    update: dict[str, float] = {}
    for name, value in values.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            return None
        update[name] = float(value)
    return StoredUpdate(
        round_id=row["round_id"],
        site_id=row["site_id"],
        update=update,
        digest=row["digest"],
        job_digest=row["job_digest"],
        received_at=float(row["received_at"]),
    )


class PostgresUpdateStore:
    """Clipped updates in one Postgres table, every method acting as the update role.

    Params:
    - `lease`: the kept connection, shared with the GMA's other stores. Its
      login must be able to act as the update role.
    - `role`: the update role, which owns the table. Take it from
      configuration, never from a request.
    - `table`: the updates table, as ``schema.table``, created from
      :data:`UPDATE_TABLE_DDL` as the update role.
    - `coordinator_role`: the role the round and enrolment stores act as.
      It must differ from `role`, so that nothing acting as the coordinator
      role (a route, the release flow, a reporting login) can read an update.
      The coordinator role needs no grant on this table, and must get none.
    - `clock`: gives the Unix time a ``put`` records as ``received_at``;
      ``time.time`` when left out.

    Raises ``RoleScopeError`` for a bad role name or when the two roles are
    the same, and ``ValueError`` for a bad table name. Building the store
    opens no connection. Each method runs in one transaction:

    | Method | Statements |
    |---|---|
    | ``put`` | the checks of :func:`checked_update` in Python; then an upsert of the round's gate row that locks it (a discarded gate raises :class:`RoundConflict`); then ``INSERT ... ON CONFLICT DO NOTHING RETURNING``, where no row back raises :class:`RoundConflict` |
    | ``discard`` | an upsert that marks the gate row discarded, then a ``DELETE`` of the round's updates |
    | ``get``, ``updates``, ``pending_rounds``, ``pending_ages`` | one ``SELECT`` of the rows that are not gate rows |

    A ``put`` and a ``discard`` of one round both lock its gate row first, so
    they run one after the other, in any process: an update stored before
    the discard is deleted by it, and a ``put`` after it is refused. Gate
    rows are kept, so a round id that was discarded stays refused after a
    restart.

    A row this version cannot read (JSON Python cannot decode, values that
    are not finite numbers, or no digest) makes ``get`` and ``updates`` raise
    ``StoreNotSetUpError`` naming the round and the site, so an aggregation
    never runs without it.
    """

    kind = "postgres"
    #: The updates stay in the table after the process stops.
    survives_restart = True

    def __init__(
        self,
        lease: KeptConnection,
        role: str,
        table: str,
        *,
        coordinator_role: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        _require_driver("PostgresUpdateStore")
        self.role = validate_role_name(role)
        if validate_role_name(coordinator_role) == self.role:
            raise RoleScopeError(
                f"the update store's role {role} is also the coordinator role; give the updates table a role of "
                "its own, so that nothing acting as the coordinator role can read an update"
            )
        self._lease = lease
        self.table = table
        self._table = table_identifier(table, "updates table")
        self.location = {"table": table, "role": self.role}
        self._clock = clock
        # The values come as text and are decoded in ``_stored_update``; see ``_decoded``.
        self._select = sql.SQL(
            "SELECT round_id, site_id, update_values::text AS update_values, digest, job_digest, received_at "
            "FROM {table}"
        ).format(table=self._table)

    def _rows(self, query: Any, values: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._lease.acting_as(self.role) as conn, conn.cursor(row_factory=dict_row) as cursor:
            return list(cursor.execute(query, values).fetchall())

    def _readable(self, row: Mapping[str, Any]) -> StoredUpdate:
        stored = _stored_update(row)
        if stored is None:
            raise StoreNotSetUpError(
                f"the stored update of site {row['site_id']} for round {row['round_id']} in {self.table} cannot be "
                "read: its values must be JSON this version can decode, finite numbers by name, with a digest and "
                "a job digest"
            )
        return stored

    def put(self, record: RoundRecord, site_id: str, upload: UpdateUpload, kind: ModelKind) -> UpdateReceipt:
        """Check one site's upload against the round, store it, and return its digest.

        Params as :meth:`InMemoryUpdateStore.put`. Raises :class:`NotSelected`
        and :class:`RoundConflict` for the checks in
        ``dagents/gma/infrastructure/update_store.py``, before anything is
        written.
        """
        vector, digest = checked_update(record, site_id, upload, kind)
        round_id = record.manifest.round_id
        value = json_parameter(vector, f"the update of site {site_id} for round {round_id}")
        received_at = float(self._clock())
        gate = sql.SQL(
            "INSERT INTO {table} AS stored (round_id, site_id, received_at) VALUES (%s, %s, %s) "
            "ON CONFLICT (round_id, site_id) DO UPDATE SET discarded = stored.discarded RETURNING discarded"
        ).format(table=self._table)
        insert = sql.SQL(
            "INSERT INTO {table} (round_id, site_id, update_values, digest, job_digest, received_at) "
            "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (round_id, site_id) DO NOTHING RETURNING site_id"
        ).format(table=self._table)
        with self._lease.acting_as(self.role) as conn, conn.cursor(row_factory=dict_row) as cursor:
            if cursor.execute(gate, (round_id, _GATE, received_at)).fetchone()["discarded"]:
                raise RoundConflict(f"round {round_id} was discarded; it takes no more updates")
            stored = cursor.execute(
                insert, (round_id, site_id, value, digest, upload.job_digest, received_at)
            ).fetchone()
            if stored is None:
                raise RoundConflict(f"an update from {site_id} is already stored for {round_id}")
        return UpdateReceipt(digest=digest)

    def get(self, round_id: str, site_id: str) -> StoredUpdate | None:
        """One site's stored update for a round, or ``None``. Raises ``StoreNotSetUpError`` when it cannot be read."""
        query = sql.SQL("{select} WHERE round_id = %s AND site_id = %s AND site_id <> %s").format(select=self._select)
        rows = self._rows(query, (round_id, site_id, _GATE))
        return self._readable(rows[0]) if rows else None

    def updates(self, round_id: str) -> dict[str, StoredUpdate]:
        """Every stored update for a round, by site id in the order of Python's ``sorted()``.

        Raises ``StoreNotSetUpError`` when one of them cannot be read.
        """
        query = sql.SQL('{select} WHERE round_id = %s AND site_id <> %s ORDER BY site_id COLLATE "C"').format(
            select=self._select
        )
        return {row["site_id"]: self._readable(row) for row in self._rows(query, (round_id, _GATE))}

    def discard(self, round_id: str) -> int:
        """Delete every update stored for a round, refuse later uploads for it, and return how many were deleted."""
        gate = sql.SQL(
            "INSERT INTO {table} (round_id, site_id, received_at, discarded) VALUES (%s, %s, %s, true) "
            "ON CONFLICT (round_id, site_id) DO UPDATE SET discarded = true"
        ).format(table=self._table)
        delete = sql.SQL("DELETE FROM {table} WHERE round_id = %s AND site_id <> %s").format(table=self._table)
        with self._lease.acting_as(self.role) as conn:
            conn.execute(gate, (round_id, _GATE, float(self._clock())))
            return conn.execute(delete, (round_id, _GATE)).rowcount

    def pending_rounds(self) -> list[str]:
        """Rounds that still have stored updates, in the order of Python's ``sorted()``."""
        query = sql.SQL(
            'SELECT round_id FROM {table} WHERE site_id <> %s GROUP BY round_id ORDER BY round_id COLLATE "C"'
        ).format(table=self._table)
        return [row["round_id"] for row in self._rows(query, (_GATE,))]

    def pending_ages(self, now: float) -> dict[str, float]:
        """For each round with stored updates, the age in seconds of its oldest update.

        Rounds come in the order of Python's ``sorted()``. An update that
        arrived after ``now`` counts as 0 seconds old.
        """
        query = sql.SQL(
            "SELECT round_id, min(received_at) AS oldest FROM {table} WHERE site_id <> %s "
            'GROUP BY round_id ORDER BY round_id COLLATE "C"'
        ).format(table=self._table)
        return {row["round_id"]: max(0.0, float(now) - float(row["oldest"])) for row in self._rows(query, (_GATE,))}


__all__ = [
    "ENROLMENT_TABLE_DDL",
    "RECORD_VERSION",
    "ROUND_TABLE_DDL",
    "UPDATE_TABLE_DDL",
    "PostgresEnrolmentStore",
    "PostgresRoundRepository",
    "PostgresUpdateStore",
    "create_table_sql",
    "json_parameter",
]

