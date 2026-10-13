"""Where each hospital's records, audit chain and published results are stored.

Each hospital has its own store. Two implementations behave the same way and
share one test suite:

- ``MemorySiteStore`` keeps the rows in this process. A local run with no
  database uses it, loaded from the committed IST extract.
- ``PostgresSiteStore`` keeps the rows in the hospital's own schema
  (``site_uk_134.encounters`` and so on, created by
  ``supabase/migrations/0002_site_isolation.sql``). Every operation runs in one
  transaction that acts as the hospital's role (``SET LOCAL ROLE``), so a
  hospital's code can reach only its own schema.

Each Postgres store keeps one autocommit connection and runs one operation at a
time on it, behind a lock. Opening a connection through a pooler takes much
longer than a query, and a pilot runs about 145 store operations. The role and
the statement timeout are set with ``SET LOCAL``, so both end with each
operation's transaction and nothing carries over to the next one. A connection
that is closed, broken or not idle is replaced, and one that has been idle for
a while is checked with ``SELECT 1`` first. ``close()`` closes it; the next
operation opens a new one.

``CoordinatorStore`` holds what the coordinator keeps: the model registry, the
results each hospital chose to publish, the newest pilot reports and the audit
chain of the release Guard. Its Postgres version acts as
``dagents_coordinator``, which cannot read any hospital schema. A hospital's
contribution is inserted while acting as that hospital. The pilot reports and
the audit chain are in ``coordinator.pilot_reports`` and
``coordinator.guard_audit``, created by
``supabase/migrations/0003_coordinator_records.sql``.

Each Postgres audit chain stores the point its last successful check reached in
a ``guard_audit_anchor`` table next to it (also from 0003), written by the
chain's own role. A restarted API continues the check from there instead of
hashing the whole chain again.

A database error that means a schema, table, role or grant is missing is raised
as :class:`StoreNotSetUpError`, so the API can say that the database is not set
up for this version instead of failing with a 500.

Records follow the app's convention: a missing value is an absent key, never
``None``. A database NULL becomes an absent key.

The demo has one connection string, ``HEALTHCARE_DEMO_DATABASE_URL``. A
deployment that gives each hospital its own login can set
``HEALTHCARE_DEMO_SITE_<SITE>_DATABASE_URL`` (for example
``HEALTHCARE_DEMO_SITE_UK_134_DATABASE_URL``) and
:meth:`PostgresSiteStore.from_environment` uses it for that hospital.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import os
import re
import threading
import time
import weakref
from typing import Any, Iterable, Iterator, Mapping, Protocol

from agents.common.application.ethical_guard import AuditSink, InMemoryAuditLog
from agents.common.infrastructure.postgres_audit import PostgresAuditSink
from agents.common.infrastructure.postgres_roles import acting_as, connect
from app.domain.dataset import AGE_BANDS, AGE_MAX, AGE_MIN, Site, age_band_for
from app.services.db_hints import connection_hint


# --- The record schema --------------------------------------------------------
# These rules match the check constraints in 0002_site_isolation.sql. The
# in-memory store applies them itself; the Postgres store applies them before
# writing, and the database checks them again. test_supabase_schema.py fails if
# the two copies disagree.

#: Fields a hospital records for an encounter, in table order.
FIELD_COLUMNS: tuple[str, ...] = (
    "age",
    "age_band",
    "sex",
    "conscious_level",
    "face_weakness",
    "arm_weakness",
    "leg_weakness",
    "speech_difficulty",
    "hours_since_onset",
    "systolic_bp",
    "visual_field_loss",
    "atrial_fibrillation",
    "onset_on_waking",
    "stroke_subtype",
    "poor_outcome_6m",
)

#: The row key and provenance, then the fields. ``records()`` returns these.
ROW_COLUMNS: tuple[str, ...] = ("encounter_id", "source_row", "origin") + FIELD_COLUMNS

#: Bookkeeping columns. They are never part of a record the framework sees.
BOOKKEEPING_COLUMNS: tuple[str, ...] = ("recorded_at", "outcome_recorded_at", "client_request_id")

#: Every column of ``<schema>.encounters``.
TABLE_COLUMNS: tuple[str, ...] = ROW_COLUMNS + BOOKKEEPING_COLUMNS

#: Text fields and the values each may take.
CATEGORIES: dict[str, tuple[str, ...]] = {
    "age_band": AGE_BANDS,
    "sex": ("female", "male"),
    "conscious_level": ("alert", "drowsy", "unconscious"),
    "stroke_subtype": ("TACS", "PACS", "LACS", "POCS", "other"),
}

#: Numeric fields and their inclusive ranges.
RANGES: dict[str, tuple[float, float]] = {
    "age": (AGE_MIN, AGE_MAX),
    "hours_since_onset": (0, 48),
    "systolic_bp": (60, 300),
}

INTEGER_FIELDS = frozenset({"age"})
REAL_FIELDS = frozenset({"hours_since_onset", "systolic_bp"})
BOOLEAN_FIELDS = frozenset(
    {
        "face_weakness",
        "arm_weakness",
        "leg_weakness",
        "speech_difficulty",
        "visual_field_loss",
        "atrial_fibrillation",
        "onset_on_waking",
        "poor_outcome_6m",
    }
)

ORIGINS = ("dataset", "entered")

#: Longest idempotency key the schema accepts.
MAX_CLIENT_REQUEST_ID = 200

_ENTRY_ID = re.compile(r"-e([1-9][0-9]*)$")
_DATASET_ID = re.compile(r"^[a-z0-9-]+-[0-9]{5,}$")


class RecordRejected(ValueError):
    """A record breaks the schema's rules. ``errors`` maps field to reason."""

    def __init__(self, errors: Mapping[str, str]) -> None:
        self.errors = dict(errors)
        super().__init__("; ".join(f"{field}: {reason}" for field, reason in sorted(self.errors.items())))


class IdempotencyKeyReused(ValueError):
    """The idempotency key was already used for a different record."""


class UnknownEncounterError(KeyError):
    """No encounter with this id is stored at this hospital."""


class OutcomeAlreadyRecorded(ValueError):
    """The record already has a different six-month outcome. ``args[0]`` is its id.

    An outcome is recorded once. The demo has no accounts, so allowing a change
    would let any visitor flip another visitor's training label between pilots,
    and nothing would record the earlier value.
    """


class StoreUnavailableError(RuntimeError):
    """The database could not be reached, or an operation on it failed."""


class StoreNotSetUpError(StoreUnavailableError):
    """The database lacks a schema, table, column, role or grant this version needs.

    The migrations for this version have not been applied, or not all of them.
    """


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate_fields(record: Mapping[str, Any]) -> dict[str, Any]:
    """Check a record's fields and return them in stored form.

    Params:
    - `record`: field values. ``None`` means missing. Keys outside
      :data:`FIELD_COLUMNS` are refused, so ids and free text cannot be stored.

    What it does:
    - Checks each type, range and allowed value.
    - Fills ``age_band`` from ``age`` when only the age is given, and refuses a
      band that does not match the age.

    Returns:
    - A new dict without missing values. Integers for ``age``, floats for the
      measurements.
    """
    errors: dict[str, str] = {}
    fields: dict[str, Any] = {}
    for key, value in record.items():
        if key not in FIELD_COLUMNS:
            errors[str(key)] = "not a field of the record"
            continue
        if value is None:
            continue
        if key in INTEGER_FIELDS:
            if not _is_number(value) or float(value) != int(value):
                errors[key] = "must be a whole number"
                continue
            value = int(value)
        elif key in REAL_FIELDS:
            if not _is_number(value):
                errors[key] = "must be a number"
                continue
            value = float(value)
        elif key in BOOLEAN_FIELDS:
            if not isinstance(value, bool):
                errors[key] = "must be true or false"
                continue
        elif key in CATEGORIES:
            if value not in CATEGORIES[key]:
                errors[key] = f"must be one of {', '.join(CATEGORIES[key])}"
                continue
        if key in RANGES:
            low, high = RANGES[key]
            if not low <= value <= high:
                errors[key] = f"must be between {low:g} and {high:g}"
                continue
        fields[key] = value
    if "age" in fields:
        band = age_band_for(fields["age"])
        if "age_band" not in fields and "age_band" not in errors:
            fields["age_band"] = band
        elif fields.get("age_band", band) != band:
            errors["age_band"] = f"does not match age {fields['age']} (expected {band})"
    if errors:
        raise RecordRejected(errors)
    return fields


def validate_stored_row(row: Mapping[str, Any], site_id: str) -> dict[str, Any]:
    """Check a whole row (key, provenance and fields), as loaded or seeded.

    Dataset rows need ``source_row`` and an id of the form
    ``{site_id}-{source_row:05d}``. Entered rows need an id ending ``-e<n>`` and
    no source row.
    """
    errors: dict[str, str] = {}
    encounter_id = row.get("encounter_id")
    origin = row.get("origin", "dataset")
    source_row = row.get("source_row")
    if origin not in ORIGINS:
        errors["origin"] = f"must be one of {', '.join(ORIGINS)}"
    if not isinstance(encounter_id, str) or not encounter_id.startswith(f"{site_id}-"):
        errors["encounter_id"] = f"must start with {site_id}-"
    elif origin == "dataset":
        if not isinstance(source_row, int) or isinstance(source_row, bool) or source_row < 1:
            errors["source_row"] = "a dataset row needs a source row of 1 or more"
        elif not _DATASET_ID.fullmatch(encounter_id) or int(encounter_id.rsplit("-", 1)[1]) != source_row:
            errors["encounter_id"] = "a dataset row's id ends with its zero-padded source row"
    elif origin == "entered":
        if source_row is not None:
            errors["source_row"] = "an entered row has no source row"
        if not _ENTRY_ID.search(encounter_id):
            errors["encounter_id"] = "an entered row's id ends with -e<n>"
    fields_only = {key: value for key, value in row.items() if key not in ("encounter_id", "origin", "source_row")}
    try:
        fields = validate_fields(fields_only)
    except RecordRejected as rejected:
        errors.update(rejected.errors)
        fields = {}
    if errors:
        raise RecordRejected(errors)
    stored = {"encounter_id": encounter_id, "origin": origin, **fields}
    if source_row is not None:
        stored["source_row"] = source_row
    return stored


def _check_unique(rows: list[Mapping[str, Any]]) -> None:
    """Encounter ids and source rows are unique, as the table's constraints require."""
    for column in ("encounter_id", "source_row"):
        values = [row[column] for row in rows if row.get(column) is not None]
        if len(set(values)) != len(values):
            raise RecordRejected({column: "the same value appears in more than one row"})


def _check_request_id(client_request_id: str | None) -> str | None:
    if client_request_id is None:
        return None
    if (
        not isinstance(client_request_id, str)
        or not 1 <= len(client_request_id) <= MAX_CLIENT_REQUEST_ID
        or not client_request_id.isprintable()
    ):
        raise RecordRejected(
            {"client_request_id": f"must be 1-{MAX_CLIENT_REQUEST_ID} printable characters"}
        )
    return client_request_id


def _check_limit(limit: int) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError(f"limit must be a positive whole number, got {limit!r}")
    return limit


def _entry_number(encounter_id: str) -> int:
    match = _ENTRY_ID.search(encounter_id)
    return int(match.group(1)) if match else 0


def _sort_key(row: Mapping[str, Any]) -> tuple:
    """Dataset rows in source order, then entries in the order they were made.

    The Postgres store orders by the same key:
    ``source_row NULLS LAST, length(encounter_id), encounter_id``.
    """
    source_row = row.get("source_row")
    encounter_id = row["encounter_id"]
    return (source_row is None, source_row or 0, len(encounter_id), encounter_id)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _record_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """The record as the framework sees it: row columns only, no missing values."""
    return {key: row[key] for key in ROW_COLUMNS if row.get(key) is not None}


def _entry_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """The record plus when it was entered and when its outcome was recorded."""
    view = _record_view(row)
    for key in ("recorded_at", "outcome_recorded_at"):
        value = row.get(key)
        if value is not None:
            view[key] = _timestamp(value) if isinstance(value, datetime) else value
    return view


def _same_entry(stored: Mapping[str, Any], fields: Mapping[str, Any]) -> bool:
    """Whether a retried entry matches the stored one.

    An outcome recorded after the entry does not count as a difference.
    """
    compared = [key for key in FIELD_COLUMNS if not (key == "poor_outcome_6m" and stored.get("outcome_recorded_at"))]
    return all(stored.get(key) == fields.get(key) for key in compared)


def _counts(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    total = entered = labelled = 0
    for row in rows:
        total += 1
        entered += row.get("origin") == "entered"
        labelled += row.get("poor_outcome_6m") is not None
    return {"total": total, "entered": entered, "labelled": labelled}


# --- The interface -------------------------------------------------------------


class SiteStore(Protocol):
    """One hospital's store. Both implementations behave the same way."""

    kind: str
    location: dict[str, str]
    site_id: str

    def records(self) -> list[dict[str, Any]]:
        """Every row: the row columns, with missing values as absent keys."""

    def count(self) -> dict[str, int]:
        """``{"total", "entered", "labelled"}``."""

    def append(self, record: Mapping[str, Any], client_request_id: str | None = None) -> dict[str, Any]:
        """Store a new entry and return it. A repeated key returns the first entry."""

    def set_outcome(self, encounter_id: str, poor_outcome_6m: bool) -> dict[str, Any]:
        """Record the six-month outcome of an entered record and return it.

        An outcome is recorded once. The same value again returns the record
        unchanged; a different value raises :class:`OutcomeAlreadyRecorded`.
        """

    def list_entered(self, limit: int) -> list[dict[str, Any]]:
        """Entered records, newest first."""

    def audit_sink(self) -> AuditSink:
        """The hospital's own audit chain."""

    def close(self) -> None:
        """Release the store's database connection, if it has one."""


# --- In memory -------------------------------------------------------------------


class MemorySiteStore:
    """A hospital's rows in this process. Nothing survives a restart."""

    kind = "in-memory"

    def __init__(self, site: Site, records: Iterable[Mapping[str, Any]]) -> None:
        self.site = site
        self.site_id = site.site_id
        self.location = {"memory": "process"}
        self._lock = threading.Lock()
        loaded_at = datetime.now(timezone.utc)
        rows = []
        for record in records:
            row = validate_stored_row(record, site.site_id)
            row["recorded_at"] = loaded_at
            rows.append(row)
        _check_unique(rows)
        self._rows: list[dict[str, Any]] = sorted(rows, key=_sort_key)
        self._audit = InMemoryAuditLog()

    def records(self) -> list[dict[str, Any]]:
        with self._lock:
            return [_record_view(row) for row in self._rows]

    def count(self) -> dict[str, int]:
        with self._lock:
            return _counts(self._rows)

    def append(self, record: Mapping[str, Any], client_request_id: str | None = None) -> dict[str, Any]:
        fields = validate_fields(record)
        key = _check_request_id(client_request_id)
        with self._lock:
            if key is not None:
                for row in self._rows:
                    if row.get("client_request_id") == key:
                        if not _same_entry(row, fields):
                            raise IdempotencyKeyReused(
                                "this idempotency key was already used for a different record"
                            )
                        return _entry_view(row)
            entered = (_entry_number(r["encounter_id"]) for r in self._rows if r["origin"] == "entered")
            number = 1 + max(entered, default=0)
            row = {
                "encounter_id": f"{self.site_id}-e{number}",
                "origin": "entered",
                **fields,
                "recorded_at": datetime.now(timezone.utc),
                "client_request_id": key,
            }
            self._rows.append(row)
            return _entry_view(row)

    def set_outcome(self, encounter_id: str, poor_outcome_6m: bool) -> dict[str, Any]:
        if not isinstance(poor_outcome_6m, bool):
            raise RecordRejected({"poor_outcome_6m": "must be true or false"})
        with self._lock:
            for row in self._rows:
                if row["encounter_id"] == encounter_id:
                    if row["origin"] != "entered":
                        raise RecordRejected({"encounter_id": "only entered records take an outcome"})
                    if row.get("outcome_recorded_at") is not None:
                        if row.get("poor_outcome_6m") != poor_outcome_6m:
                            raise OutcomeAlreadyRecorded(encounter_id)
                        return _entry_view(row)
                    row["poor_outcome_6m"] = poor_outcome_6m
                    row["outcome_recorded_at"] = datetime.now(timezone.utc)
                    return _entry_view(row)
        raise UnknownEncounterError(encounter_id)

    def list_entered(self, limit: int) -> list[dict[str, Any]]:
        _check_limit(limit)
        with self._lock:
            entered = [row for row in self._rows if row["origin"] == "entered"]
        entered.sort(key=_sort_key, reverse=True)
        return [_entry_view(row) for row in entered[:limit]]

    def audit_sink(self) -> AuditSink:
        return self._audit

    def close(self) -> None:
        """Nothing to close; the rows stay in memory."""


# --- PostgreSQL ------------------------------------------------------------------

#: Rows per INSERT statement when seeding. 18 columns x 500 rows stays well
#: under PostgreSQL's limit of 65,535 parameters per statement.
SEED_BATCH_ROWS = 500

#: Statement timeout for one store operation, in milliseconds. Role settings do
#: not apply after SET ROLE, so the store sets it with SET LOCAL in each
#: transaction, audit appends included.
DEFAULT_STATEMENT_TIMEOUT_MS = 15_000

#: Seconds a store's connection may sit idle before the next operation checks
#: it with ``SELECT 1``. Poolers and proxies close idle connections; the check
#: replaces such a connection instead of failing the operation.
DEFAULT_LIVENESS_IDLE_SECONDS = 30.0


def site_database_url(site: Site, default: str, environ: Mapping[str, str] | None = None) -> str:
    """The connection string for one hospital.

    ``HEALTHCARE_DEMO_SITE_<SITE>_DATABASE_URL`` (``uk-134`` becomes
    ``UK_134``) when it is set, otherwise ``default``.
    """
    env = os.environ if environ is None else environ
    name = f"HEALTHCARE_DEMO_SITE_{site.site_id.upper().replace('-', '_')}_DATABASE_URL"
    return (env.get(name) or "").strip() or default


def not_set_up_errors() -> tuple[type[Exception], ...]:
    """The psycopg errors that mean a schema, table, column, role or grant is missing.

    A role that does not exist makes ``SET LOCAL ROLE`` fail with
    ``InvalidParameterValue``; a membership that was never granted makes it
    fail with ``InsufficientPrivilege``.
    """
    from psycopg import errors

    return (
        errors.UndefinedTable,
        errors.UndefinedColumn,
        errors.UndefinedObject,
        errors.UndefinedFunction,
        errors.InvalidSchemaName,
        errors.InvalidParameterValue,
        errors.InsufficientPrivilege,
    )


class _ConnectionSlot:
    """Holds a store's connection, so it can be closed when the store is dropped."""

    def __init__(self) -> None:
        self.conn: Any = None

    def close(self) -> None:
        conn, self.conn = self.conn, None
        if conn is not None and not conn.closed:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - the connection is being thrown away
                pass


class _PostgresBase:
    """Keeps one autocommit connection and runs each operation as one role.

    Params:
    - `dsn`: the connection string.
    - `statement_timeout_ms`: the statement timeout inside each operation.
    - `liveness_idle_seconds`: how long the connection may be idle before the
      next operation checks it. ``0`` checks before every operation.

    Operations take the connection one at a time, behind a lock. An operation
    that starts while the same thread already holds the connection (it does
    not happen in this module, but a caller could nest one) gets a separate
    connection for that operation, because the shared one is inside a
    transaction.

    ``connections_opened`` counts the connections this store has opened.
    """

    def __init__(
        self,
        dsn: str,
        statement_timeout_ms: int,
        liveness_idle_seconds: float = DEFAULT_LIVENESS_IDLE_SECONDS,
    ) -> None:
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("a Postgres store needs a connection string")
        self._dsn = dsn
        self._statement_timeout_ms = int(statement_timeout_ms)
        self._liveness_idle_seconds = float(liveness_idle_seconds)
        self._slot = _ConnectionSlot()
        # A store that is dropped without close() still closes its connection.
        weakref.finalize(self, self._slot.close)
        self._conn_lock = threading.RLock()
        self._depth = 0
        self._last_used = 0.0
        self.connections_opened = 0

    def _open(self) -> Any:
        import psycopg
        from psycopg.rows import dict_row

        try:
            conn = connect(self._dsn, row_factory=dict_row)
        except psycopg.OperationalError as error:
            raise StoreUnavailableError(
                f"{self._unavailable_prefix()}could not connect to the database: {error}{connection_hint(self._dsn)}"
            ) from error
        self.connections_opened += 1
        return conn

    def _usable(self, conn: Any) -> bool:
        """Whether the kept connection can run the next operation."""
        import psycopg
        from psycopg.pq import TransactionStatus

        if conn.closed or conn.broken or conn.info.transaction_status != TransactionStatus.IDLE:
            return False
        if time.monotonic() - self._last_used < self._liveness_idle_seconds:
            return True
        try:
            conn.execute("SELECT 1")
        except psycopg.Error:
            return False
        return conn.info.transaction_status == TransactionStatus.IDLE

    def _discard(self) -> None:
        self._slot.close()

    def close(self) -> None:
        """Close the kept connection. The next operation opens a new one."""
        with self._conn_lock:
            self._discard()

    @contextmanager
    def _lease(self, row_factory: Any = None) -> Iterator[Any]:
        """The store's connection, held by one operation until the block ends.

        Params:
        - `row_factory`: rows as this factory makes them during the block
          (the audit sink reads tuples). Restored afterwards.

        A database error that means the connection failed (``OperationalError``
        or ``InterfaceError``, which include a statement timeout) discards the
        connection and is raised as :class:`StoreUnavailableError`.

        An error that means the database lacks a schema, table, column, role
        or grant this version needs is raised as :class:`StoreNotSetUpError`.
        It keeps the connection: :func:`acting_as` has already rolled the
        transaction back.
        """
        import psycopg
        from psycopg.pq import TransactionStatus

        not_set_up = not_set_up_errors()
        with self._conn_lock:
            if self._depth:
                conn = self._open()
                try:
                    if row_factory is not None:
                        conn.row_factory = row_factory
                    with conn:
                        yield conn
                except (psycopg.OperationalError, psycopg.InterfaceError) as error:
                    raise self._failed(error) from error
                except not_set_up as error:
                    raise self._not_set_up(error) from error
                return

            if self._slot.conn is None or not self._usable(self._slot.conn):
                self._discard()
                self._slot.conn = self._open()
            conn = self._slot.conn
            previous_factory = conn.row_factory
            if row_factory is not None:
                conn.row_factory = row_factory
            self._depth += 1
            try:
                yield conn
            except (psycopg.OperationalError, psycopg.InterfaceError) as error:
                self._discard()
                raise self._failed(error) from error
            except not_set_up as error:
                raise self._not_set_up(error) from error
            finally:
                self._depth -= 1
                self._last_used = time.monotonic()
                if self._slot.conn is conn:
                    if conn.closed or conn.broken or conn.info.transaction_status != TransactionStatus.IDLE:
                        self._discard()
                    else:
                        conn.row_factory = previous_factory

    def _failed(self, error: Exception) -> StoreUnavailableError:
        return StoreUnavailableError(f"{self._unavailable_prefix()}the database operation failed: {error}")

    def _not_set_up(self, error: Exception) -> StoreNotSetUpError:
        return StoreNotSetUpError(
            f"{self._unavailable_prefix()}the database is not set up for this version of the app "
            f"({type(error).__name__}: {error}). Apply the migrations, then seed."
        )

    def _lease_tuples(self) -> Any:
        """The connection with tuple rows, as the audit sink reads them."""
        from psycopg.rows import tuple_row

        return self._lease(row_factory=tuple_row)

    def _unavailable_prefix(self) -> str:
        """Names the store in a connection error, so a 503 says which store failed."""
        return ""

    @property
    def _settings(self) -> dict[str, str]:
        """``SET LOCAL`` settings for every operation: the statement timeout."""
        return {"statement_timeout": f"{self._statement_timeout_ms}ms"}

    @contextmanager
    def _acting_as(self, role: str) -> Iterator[Any]:
        # acting_as sends BEGIN, the role, the timeout and the current_user
        # check as one message, so an operation costs its own queries plus two
        # round trips (that message and COMMIT).
        with self._lease() as conn, acting_as(conn, role, self._settings):
            yield conn


class PostgresSiteStore(_PostgresBase):
    """A hospital's rows in its own schema, read and written as its own role."""

    kind = "postgres"

    def __init__(
        self,
        site: Site,
        dsn: str,
        *,
        statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
        liveness_idle_seconds: float = DEFAULT_LIVENESS_IDLE_SECONDS,
    ) -> None:
        super().__init__(dsn, statement_timeout_ms, liveness_idle_seconds)
        from psycopg import sql

        self.site = site
        self.site_id = site.site_id
        self.role = site.role
        self.location = {"schema": site.schema, "role": site.role}
        self._sql = sql
        self._table = sql.Identifier(site.schema, "encounters")
        self._audit: PostgresAuditSink | None = None

    def _unavailable_prefix(self) -> str:
        return f"hospital {self.site_id}: "

    @classmethod
    def from_environment(
        cls, site: Site, default_dsn: str, environ: Mapping[str, str] | None = None, **kwargs: Any
    ) -> "PostgresSiteStore":
        """Use the hospital's own connection string if one is set."""
        return cls(site, site_database_url(site, default_dsn, environ), **kwargs)

    def _q(self, template: str) -> Any:
        return self._sql.SQL(template).format(table=self._table)

    def _select(self, columns: Iterable[str]) -> Any:
        return self._sql.SQL(", ").join(self._sql.Identifier(column) for column in columns)

    def records(self) -> list[dict[str, Any]]:
        query = self._sql.SQL(
            "SELECT {columns} FROM {table} ORDER BY source_row NULLS LAST, length(encounter_id), encounter_id"
        ).format(columns=self._select(ROW_COLUMNS), table=self._table)
        with self._acting_as(self.role) as conn:
            rows = conn.execute(query).fetchall()
        return [_record_view(row) for row in rows]

    def count(self) -> dict[str, int]:
        query = self._q(
            "SELECT count(*) AS total, count(*) FILTER (WHERE origin = 'entered') AS entered, "
            "count(poor_outcome_6m) AS labelled FROM {table}"
        )
        with self._acting_as(self.role) as conn:
            row = conn.execute(query).fetchone()
        return {key: int(row[key]) for key in ("total", "entered", "labelled")}

    def append(self, record: Mapping[str, Any], client_request_id: str | None = None) -> dict[str, Any]:
        fields = validate_fields(record)
        key = _check_request_id(client_request_id)
        with self._acting_as(self.role) as conn:
            # One writer at a time, so two entries cannot take the same number.
            conn.execute(self._q("LOCK TABLE {table} IN SHARE ROW EXCLUSIVE MODE"))
            if key is not None:
                existing = conn.execute(
                    self._q("SELECT * FROM {table} WHERE client_request_id = %s"), (key,)
                ).fetchone()
                if existing is not None:
                    if not _same_entry(existing, fields):
                        raise IdempotencyKeyReused("this idempotency key was already used for a different record")
                    return _entry_view(existing)
            number = conn.execute(
                self._q(
                    "SELECT coalesce(max(substring(encounter_id from '-e([0-9]+)$')::int), 0) + 1 AS n "
                    "FROM {table} WHERE origin = 'entered'"
                )
            ).fetchone()["n"]
            row = {"encounter_id": f"{self.site_id}-e{number}", "origin": "entered", **fields, "client_request_id": key}
            columns = list(row)
            inserted = conn.execute(
                self._sql.SQL("INSERT INTO {table} ({columns}) VALUES ({values}) RETURNING *").format(
                    table=self._table,
                    columns=self._select(columns),
                    values=self._sql.SQL(", ").join(self._sql.Placeholder() * len(columns)),
                ),
                [row[column] for column in columns],
            ).fetchone()
        return _entry_view(inserted)

    def set_outcome(self, encounter_id: str, poor_outcome_6m: bool) -> dict[str, Any]:
        if not isinstance(poor_outcome_6m, bool):
            raise RecordRejected({"poor_outcome_6m": "must be true or false"})
        with self._acting_as(self.role) as conn:
            # FOR UPDATE: two requests for the same record wait for each other,
            # so only the first one records an outcome.
            found = conn.execute(
                self._q("SELECT * FROM {table} WHERE encounter_id = %s FOR UPDATE"), (encounter_id,)
            ).fetchone()
            if found is None:
                raise UnknownEncounterError(encounter_id)
            if found["origin"] != "entered":
                raise RecordRejected({"encounter_id": "only entered records take an outcome"})
            if found["outcome_recorded_at"] is not None:
                if found["poor_outcome_6m"] != poor_outcome_6m:
                    raise OutcomeAlreadyRecorded(encounter_id)
                return _entry_view(found)
            updated = conn.execute(
                self._q(
                    "UPDATE {table} SET poor_outcome_6m = %s, outcome_recorded_at = now() "
                    "WHERE encounter_id = %s RETURNING *"
                ),
                (poor_outcome_6m, encounter_id),
            ).fetchone()
        return _entry_view(updated)

    def list_entered(self, limit: int) -> list[dict[str, Any]]:
        _check_limit(limit)
        with self._acting_as(self.role) as conn:
            rows = conn.execute(
                self._q(
                    "SELECT * FROM {table} WHERE origin = 'entered' "
                    "ORDER BY length(encounter_id) DESC, encounter_id DESC LIMIT %s"
                ),
                (limit,),
            ).fetchall()
        return [_entry_view(row) for row in rows]

    def audit_sink(self) -> AuditSink:
        if self._audit is None:
            # The sink takes the store's own connection for each operation. The
            # lease releases it when the sink's ``with`` block ends; it does not
            # close it.
            # The anchor table (0003) lets a restarted API continue the chain
            # check where the last process left it.
            self._audit = PostgresAuditSink(
                self._lease_tuples,
                self.site.role,
                f"{self.site.schema}.guard_audit",
                settings=self._settings,
                anchor_table=f"{self.site.schema}.guard_audit_anchor",
            )
        return self._audit

    def seed_dataset(self, records: Iterable[Mapping[str, Any]], *, keep_entries: bool = False) -> dict[str, int]:
        """Replace this hospital's dataset rows, acting as the hospital.

        Params:
        - `records`: dataset rows, as ``app.domain.dataset.load_site_records``
          returns them.
        - `keep_entries`: keep the records people entered in the demo. By
          default every row is deleted first.

        The audit chain is not touched. Returns :meth:`count`.
        """
        rows = [validate_stored_row(record, self.site_id) for record in records]
        if any(row["origin"] != "dataset" for row in rows):
            raise RecordRejected({"origin": "only dataset rows can be seeded"})
        _check_unique(rows)
        delete = "DELETE FROM {table} WHERE origin = 'dataset'" if keep_entries else "DELETE FROM {table}"
        one_row = self._sql.SQL("({})").format(
            self._sql.SQL(", ").join(self._sql.Placeholder() * len(ROW_COLUMNS))
        )
        with self._acting_as(self.role) as conn:
            conn.execute(self._q(delete))
            # Multi-row INSERT statements, one after another. executemany would
            # pipeline them, and Supabase's transaction pooler does not support
            # pipelining.
            for start in range(0, len(rows), SEED_BATCH_ROWS):
                batch = rows[start : start + SEED_BATCH_ROWS]
                conn.execute(
                    self._sql.SQL("INSERT INTO {table} ({columns}) VALUES {values}").format(
                        table=self._table,
                        columns=self._select(ROW_COLUMNS),
                        values=self._sql.SQL(", ").join([one_row] * len(batch)),
                    ),
                    [row.get(column) for row in batch for column in ROW_COLUMNS],
                )
        return self.count()


# --- The coordinator ---------------------------------------------------------------

COORDINATOR_ROLE = "dagents_coordinator"
COORDINATOR_SCHEMA = "coordinator"

#: A model's lifecycle. Exactly one model at a time may be ``released``.
MODEL_STATUSES = ("candidate", "released", "rejected", "superseded")


class ModelVersionConflict(ValueError):
    """The version is already stored with different contents."""


class UnknownModelError(KeyError):
    """No model with this version is stored."""


def _check_model_limit(limit: int) -> int:
    """A limit on a model listing: a whole number of 1 or more."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError(f"a model listing limit must be a whole number of 1 or more, got {limit!r}")
    return limit


def _check_model(version: str, kind: str, digest: str, parent_version: str | None, status: str) -> None:
    errors: dict[str, str] = {}
    if not isinstance(version, str) or not 1 <= len(version) <= 200:
        errors["version"] = "must be 1-200 characters"
    if not isinstance(kind, str) or not 1 <= len(kind) <= 64:
        errors["kind"] = "must be 1-64 characters"
    if not isinstance(digest, str) or not 1 <= len(digest) <= 200:
        errors["digest"] = "must be 1-200 characters"
    if parent_version is not None and not isinstance(parent_version, str):
        errors["parent_version"] = "must be a version or None"
    if status not in MODEL_STATUSES:
        errors["status"] = f"must be one of {', '.join(MODEL_STATUSES)}"
    if errors:
        raise RecordRejected(errors)


def _check_contribution(round_id: str, phase: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    errors: dict[str, str] = {}
    if not isinstance(round_id, str) or not 1 <= len(round_id) <= 200:
        errors["round_id"] = "must be 1-200 characters"
    if not isinstance(phase, str) or not 1 <= len(phase) <= 64:
        errors["phase"] = "must be 1-64 characters"
    if not isinstance(payload, Mapping):
        errors["payload"] = "must be an object"
    if errors:
        raise RecordRejected(errors)
    return _json_copy(payload)


def _json_copy(value: Any) -> Any:
    """Copy through JSON, as the database does, so both stores return the same types."""
    return json.loads(json.dumps(value, allow_nan=False))


#: The keys a stored release decision must have, besides ``decided_at``, which
#: the store sets.
RELEASE_DECISION_KEYS = ("action", "guard_permitted", "gate_results", "round_id")

#: Keys a stored release decision may have. ``baseline_version`` names the
#: model the gates compared the candidate with. Decisions stored before it
#: existed lack it.
OPTIONAL_RELEASE_DECISION_KEYS = ("baseline_version",)


def _check_release_decision(decision: Mapping[str, Any]) -> dict[str, Any]:
    """Check a release decision and return it in stored form (a JSON copy).

    It needs ``action`` (the gates' decision, such as ``release`` or
    ``reject``), ``guard_permitted`` (whether the release Guard permitted the
    request), ``gate_results`` (a list of objects) and ``round_id``. It may
    have ``baseline_version``, 1-200 characters.
    """
    if not isinstance(decision, Mapping):
        raise RecordRejected({"release_decision": "must be an object"})
    errors: dict[str, str] = {}
    unknown = set(decision) - set(RELEASE_DECISION_KEYS) - set(OPTIONAL_RELEASE_DECISION_KEYS)
    if unknown:
        errors["release_decision"] = f"unknown keys: {', '.join(sorted(map(str, unknown)))}"
    action = decision.get("action")
    if not isinstance(action, str) or not 1 <= len(action) <= 64:
        errors["action"] = "must be 1-64 characters"
    if not isinstance(decision.get("guard_permitted"), bool):
        errors["guard_permitted"] = "must be true or false"
    gates = decision.get("gate_results")
    if not isinstance(gates, list) or not all(isinstance(gate, Mapping) for gate in gates):
        errors["gate_results"] = "must be a list of objects"
    round_id = decision.get("round_id")
    if not isinstance(round_id, str) or not 1 <= len(round_id) <= 200:
        errors["round_id"] = "must be 1-200 characters"
    if "baseline_version" in decision:
        baseline = decision["baseline_version"]
        if not isinstance(baseline, str) or not 1 <= len(baseline) <= 200:
            errors["baseline_version"] = "must be 1-200 characters"
    if errors:
        raise RecordRejected(errors)
    keys = RELEASE_DECISION_KEYS + tuple(key for key in OPTIONAL_RELEASE_DECISION_KEYS if key in decision)
    return _json_copy({key: decision[key] for key in keys})


#: Pilot reports the coordinator keeps. Only the latest is read; a few more are
#: kept so a report can be compared with the ones before it. The rest are
#: deleted, so pilots run by visitors cannot fill the database.
PILOT_REPORTS_KEPT = 20


def _check_pilot_report(pilot_id: str, report: Mapping[str, Any]) -> dict[str, Any]:
    """Check a pilot report and return it in stored form (a JSON copy)."""
    errors: dict[str, str] = {}
    if not isinstance(pilot_id, str) or not 1 <= len(pilot_id) <= 200:
        errors["pilot_id"] = "must be 1-200 characters"
    if not isinstance(report, Mapping):
        errors["report"] = "must be an object"
    else:
        try:
            copy = _json_copy(report)
        except (TypeError, ValueError):
            errors["report"] = "must hold only JSON values, with finite numbers"
    if errors:
        raise RecordRejected(errors)
    return copy


def _same_model(stored: Mapping[str, Any], kind: str, params: Any, digest: str, parent: str | None) -> bool:
    return (
        stored["kind"] == kind
        and stored["params"] == params
        and stored["digest"] == digest
        and stored["parent_version"] == parent
    )


class CoordinatorStore(Protocol):
    """The coordinator's records: models and what hospitals published."""

    kind: str
    location: dict[str, str]

    def save_model(
        self,
        version: str,
        kind: str,
        params: Mapping[str, Any],
        digest: str,
        parent_version: str | None = None,
        status: str = "candidate",
    ) -> dict[str, Any]:
        """Store a model. Saving the same version again with the same contents returns it."""

    def get_model(self, version: str) -> dict[str, Any] | None:
        """One model, or None."""

    def list_models(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Every model, oldest first, or only the newest ``limit`` of them (still oldest first)."""

    def set_model_status(self, version: str, status: str) -> dict[str, Any]:
        """Change a model's status. Releasing one supersedes the previous release."""

    def released_model(self) -> dict[str, Any] | None:
        """The model the hospitals use, if one is released."""

    def set_release_decision(self, version: str, decision: Mapping[str, Any]) -> dict[str, Any]:
        """Store a model's latest release decision, replacing any earlier one.

        ``decision`` has the keys in :data:`RELEASE_DECISION_KEYS`. The store
        adds ``decided_at``. Returns the model, whose ``release_decision`` is
        the stored decision (``None`` until one is stored).
        """

    def add_contribution(self, site: Site, round_id: str, phase: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Record what a hospital published in a round, as that hospital."""

    def contributions(self, round_id: str | None = None) -> list[dict[str, Any]]:
        """Every hospital's contributions, oldest first, as the coordinator."""

    def site_contributions(self, site: Site) -> list[dict[str, Any]]:
        """One hospital's own contributions, oldest first, read as that hospital."""

    def save_pilot_report(self, pilot_id: str, report: Mapping[str, Any]) -> None:
        """Store a pilot's report. Saving the same pilot again replaces its report.

        Only the newest :data:`PILOT_REPORTS_KEPT` reports are kept.
        """

    def latest_pilot_report(self) -> dict[str, Any] | None:
        """The report of the pilot saved last, or None."""

    def audit_sink(self) -> AuditSink:
        """The audit chain of the coordinator's release Guard. The same sink each time."""

    def close(self) -> None:
        """Release the store's database connection, if it has one."""


class MemoryCoordinatorStore:
    """The coordinator's records in this process."""

    kind = "in-memory"

    def __init__(self) -> None:
        self.location = {"memory": "process"}
        self._lock = threading.Lock()
        self._models: dict[str, dict[str, Any]] = {}
        self._contributions: list[dict[str, Any]] = []
        # Oldest first. A dict keeps the order in which each pilot was first saved.
        self._pilot_reports: dict[str, dict[str, Any]] = {}
        self._audit = InMemoryAuditLog()

    def save_model(
        self,
        version: str,
        kind: str,
        params: Mapping[str, Any],
        digest: str,
        parent_version: str | None = None,
        status: str = "candidate",
    ) -> dict[str, Any]:
        _check_model(version, kind, digest, parent_version, status)
        params_copy = _json_copy(params)
        with self._lock:
            stored = self._models.get(version)
            if stored is not None:
                if not _same_model(stored, kind, params_copy, digest, parent_version):
                    raise ModelVersionConflict(f"model {version} is already stored with different contents")
                return _json_copy(stored)
            if parent_version is not None and parent_version not in self._models:
                raise UnknownModelError(parent_version)
            if status == "released":
                self._supersede_release()
            self._models[version] = {
                "version": version,
                "kind": kind,
                "params": params_copy,
                "digest": digest,
                "parent_version": parent_version,
                "status": status,
                "created_at": _timestamp(datetime.now(timezone.utc)),
                "release_decision": None,
            }
            return _json_copy(self._models[version])

    def _supersede_release(self) -> None:
        for model in self._models.values():
            if model["status"] == "released":
                model["status"] = "superseded"

    def get_model(self, version: str) -> dict[str, Any] | None:
        with self._lock:
            model = self._models.get(version)
            return _json_copy(model) if model is not None else None

    def list_models(self, limit: int | None = None) -> list[dict[str, Any]]:
        start = -_check_model_limit(limit) if limit is not None else 0
        with self._lock:
            # A dict keeps insertion order, which is creation order.
            return [_json_copy(model) for model in list(self._models.values())[start:]]

    def set_model_status(self, version: str, status: str) -> dict[str, Any]:
        if status not in MODEL_STATUSES:
            raise RecordRejected({"status": f"must be one of {', '.join(MODEL_STATUSES)}"})
        with self._lock:
            model = self._models.get(version)
            if model is None:
                raise UnknownModelError(version)
            if status == "released" and model["status"] != "released":
                self._supersede_release()
            model["status"] = status
            return _json_copy(model)

    def released_model(self) -> dict[str, Any] | None:
        with self._lock:
            for model in self._models.values():
                if model["status"] == "released":
                    return _json_copy(model)
        return None

    def set_release_decision(self, version: str, decision: Mapping[str, Any]) -> dict[str, Any]:
        stored = _check_release_decision(decision)
        with self._lock:
            model = self._models.get(version)
            if model is None:
                raise UnknownModelError(version)
            model["release_decision"] = {**stored, "decided_at": _timestamp(datetime.now(timezone.utc))}
            return _json_copy(model)

    def add_contribution(self, site: Site, round_id: str, phase: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        payload_copy = _check_contribution(round_id, phase, payload)
        with self._lock:
            entry = {
                "contribution_id": len(self._contributions) + 1,
                "round_id": round_id,
                "phase": phase,
                "site_id": site.site_id,
                "payload": payload_copy,
                "submitted_at": _timestamp(datetime.now(timezone.utc)),
            }
            self._contributions.append(entry)
            return _json_copy(entry)

    def contributions(self, round_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            return [
                _json_copy(entry)
                for entry in self._contributions
                if round_id is None or entry["round_id"] == round_id
            ]

    def site_contributions(self, site: Site) -> list[dict[str, Any]]:
        with self._lock:
            return [_json_copy(entry) for entry in self._contributions if entry["site_id"] == site.site_id]

    def save_pilot_report(self, pilot_id: str, report: Mapping[str, Any]) -> None:
        stored = _check_pilot_report(pilot_id, report)
        with self._lock:
            self._pilot_reports[pilot_id] = stored
            for oldest in list(self._pilot_reports)[: max(len(self._pilot_reports) - PILOT_REPORTS_KEPT, 0)]:
                del self._pilot_reports[oldest]

    def latest_pilot_report(self) -> dict[str, Any] | None:
        with self._lock:
            if not self._pilot_reports:
                return None
            return _json_copy(next(reversed(self._pilot_reports.values())))

    def audit_sink(self) -> AuditSink:
        return self._audit

    def close(self) -> None:
        """Nothing to close; the records stay in memory."""


class PostgresCoordinatorStore(_PostgresBase):
    """The coordinator's records in the ``coordinator`` schema."""

    kind = "postgres"

    def __init__(
        self,
        dsn: str,
        sites: Iterable[Site],
        *,
        statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
        liveness_idle_seconds: float = DEFAULT_LIVENESS_IDLE_SECONDS,
    ) -> None:
        super().__init__(dsn, statement_timeout_ms, liveness_idle_seconds)
        self.location = {"schema": COORDINATOR_SCHEMA, "role": COORDINATOR_ROLE}
        self._site_by_role = {site.role: site for site in sites}
        self._audit: PostgresAuditSink | None = None
        self._audit_lock = threading.Lock()

    def _unavailable_prefix(self) -> str:
        return "coordinator store: "

    @staticmethod
    def _model(row: Mapping[str, Any]) -> dict[str, Any]:
        decision = row["release_decision"]
        return {
            "version": row["version"],
            "kind": row["kind"],
            "params": row["params"],
            "digest": row["digest"],
            "parent_version": row["parent_version"],
            "status": row["status"],
            "created_at": _timestamp(row["created_at"]),
            "release_decision": (
                {**decision, "decided_at": _timestamp(row["decided_at"])} if decision is not None else None
            ),
        }

    def _contribution(self, row: Mapping[str, Any]) -> dict[str, Any]:
        site = self._site_by_role.get(row["site_role"])
        return {
            "contribution_id": int(row["contribution_id"]),
            "round_id": row["round_id"],
            "phase": row["phase"],
            "site_id": site.site_id if site is not None else row["site_role"],
            "payload": row["payload"],
            "submitted_at": _timestamp(row["submitted_at"]),
        }

    def save_model(
        self,
        version: str,
        kind: str,
        params: Mapping[str, Any],
        digest: str,
        parent_version: str | None = None,
        status: str = "candidate",
    ) -> dict[str, Any]:
        from psycopg import errors
        from psycopg.types.json import Jsonb

        _check_model(version, kind, digest, parent_version, status)
        params_copy = _json_copy(params)
        values = (version, kind, Jsonb(params_copy), digest, parent_version, status)
        if status != "released":
            # One statement in the usual case. ON CONFLICT makes "insert unless
            # stored" atomic, and the foreign key refuses an unknown parent, so
            # no table lock is needed.
            try:
                with self._acting_as(COORDINATOR_ROLE) as conn:
                    row = conn.execute(
                        "INSERT INTO coordinator.models (version, kind, params, digest, parent_version, status) "
                        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (version) DO NOTHING RETURNING *",
                        values,
                    ).fetchone()
                    if row is None:
                        row = conn.execute(
                            "SELECT * FROM coordinator.models WHERE version = %s", (version,)
                        ).fetchone()
                        model = self._model(row)
                        if not _same_model(model, kind, params_copy, digest, parent_version):
                            raise ModelVersionConflict(f"model {version} is already stored with different contents")
                        return model
            except errors.ForeignKeyViolation as error:
                raise UnknownModelError(parent_version) from error
            return self._model(row)
        # A release supersedes the current one. The lock keeps two releases
        # from racing; the unique index models_one_release would refuse the
        # second anyway.
        with self._acting_as(COORDINATOR_ROLE) as conn:
            conn.execute("LOCK TABLE coordinator.models IN SHARE ROW EXCLUSIVE MODE")
            stored = conn.execute("SELECT * FROM coordinator.models WHERE version = %s", (version,)).fetchone()
            if stored is not None:
                model = self._model(stored)
                if not _same_model(model, kind, params_copy, digest, parent_version):
                    raise ModelVersionConflict(f"model {version} is already stored with different contents")
                return model
            if parent_version is not None and conn.execute(
                "SELECT 1 FROM coordinator.models WHERE version = %s", (parent_version,)
            ).fetchone() is None:
                raise UnknownModelError(parent_version)
            conn.execute("UPDATE coordinator.models SET status = 'superseded' WHERE status = 'released'")
            row = conn.execute(
                "INSERT INTO coordinator.models (version, kind, params, digest, parent_version, status) "
                "VALUES (%s, %s, %s, %s, %s, %s) RETURNING *",
                values,
            ).fetchone()
        return self._model(row)

    def get_model(self, version: str) -> dict[str, Any] | None:
        with self._acting_as(COORDINATOR_ROLE) as conn:
            row = conn.execute("SELECT * FROM coordinator.models WHERE version = %s", (version,)).fetchone()
        return self._model(row) if row is not None else None

    def list_models(self, limit: int | None = None) -> list[dict[str, Any]]:
        with self._acting_as(COORDINATOR_ROLE) as conn:
            if limit is None:
                rows = conn.execute("SELECT * FROM coordinator.models ORDER BY created_at, version").fetchall()
            else:
                # The database picks the newest rows, so a long registry is not read.
                rows = conn.execute(
                    "SELECT * FROM (SELECT * FROM coordinator.models ORDER BY created_at DESC, version DESC "
                    "LIMIT %s) AS newest ORDER BY created_at, version",
                    (_check_model_limit(limit),),
                ).fetchall()
        return [self._model(row) for row in rows]

    def set_model_status(self, version: str, status: str) -> dict[str, Any]:
        if status not in MODEL_STATUSES:
            raise RecordRejected({"status": f"must be one of {', '.join(MODEL_STATUSES)}"})
        if status != "released":
            # One statement: no other row changes.
            with self._acting_as(COORDINATOR_ROLE) as conn:
                row = conn.execute(
                    "UPDATE coordinator.models SET status = %s WHERE version = %s RETURNING *", (status, version)
                ).fetchone()
            if row is None:
                raise UnknownModelError(version)
            return self._model(row)
        with self._acting_as(COORDINATOR_ROLE) as conn:
            conn.execute("LOCK TABLE coordinator.models IN SHARE ROW EXCLUSIVE MODE")
            if conn.execute("SELECT 1 FROM coordinator.models WHERE version = %s", (version,)).fetchone() is None:
                raise UnknownModelError(version)
            conn.execute(
                "UPDATE coordinator.models SET status = 'superseded' WHERE status = 'released' AND version <> %s",
                (version,),
            )
            row = conn.execute(
                "UPDATE coordinator.models SET status = 'released' WHERE version = %s RETURNING *", (version,)
            ).fetchone()
        return self._model(row)

    def released_model(self) -> dict[str, Any] | None:
        with self._acting_as(COORDINATOR_ROLE) as conn:
            row = conn.execute("SELECT * FROM coordinator.models WHERE status = 'released'").fetchone()
        return self._model(row) if row is not None else None

    def set_release_decision(self, version: str, decision: Mapping[str, Any]) -> dict[str, Any]:
        from psycopg.types.json import Jsonb

        stored = _check_release_decision(decision)
        with self._acting_as(COORDINATOR_ROLE) as conn:
            row = conn.execute(
                "UPDATE coordinator.models SET release_decision = %s, decided_at = now() "
                "WHERE version = %s RETURNING *",
                (Jsonb(stored), version),
            ).fetchone()
        if row is None:
            raise UnknownModelError(version)
        return self._model(row)

    def add_contribution(self, site: Site, round_id: str, phase: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        from psycopg.types.json import Jsonb

        payload_copy = _check_contribution(round_id, phase, payload)
        # Inserted as the hospital: the row's site_role defaults to current_user,
        # and the hospital has no grant to set it.
        with self._acting_as(site.role) as conn:
            row = conn.execute(
                "INSERT INTO coordinator.site_contributions (round_id, phase, payload) "
                "VALUES (%s, %s, %s) RETURNING *",
                (round_id, phase, Jsonb(payload_copy)),
            ).fetchone()
        return self._contribution(row)

    def contributions(self, round_id: str | None = None) -> list[dict[str, Any]]:
        with self._acting_as(COORDINATOR_ROLE) as conn:
            if round_id is None:
                rows = conn.execute(
                    "SELECT * FROM coordinator.site_contributions ORDER BY contribution_id"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM coordinator.site_contributions WHERE round_id = %s ORDER BY contribution_id",
                    (round_id,),
                ).fetchall()
        return [self._contribution(row) for row in rows]

    def site_contributions(self, site: Site) -> list[dict[str, Any]]:
        # No WHERE clause: row level security returns only the hospital's rows.
        with self._acting_as(site.role) as conn:
            rows = conn.execute("SELECT * FROM coordinator.site_contributions ORDER BY contribution_id").fetchall()
        return [self._contribution(row) for row in rows]

    def save_pilot_report(self, pilot_id: str, report: Mapping[str, Any]) -> None:
        from psycopg.types.json import Jsonb

        stored = _check_pilot_report(pilot_id, report)
        # coordinator.pilot_reports is created by 0003_coordinator_records.sql.
        # seq orders the reports; a pilot saved again keeps its place.
        with self._acting_as(COORDINATOR_ROLE) as conn:
            conn.execute(
                "INSERT INTO coordinator.pilot_reports (pilot_id, report) VALUES (%s, %s) "
                "ON CONFLICT (pilot_id) DO UPDATE SET report = excluded.report",
                (pilot_id, Jsonb(stored)),
            )
            conn.execute(
                "DELETE FROM coordinator.pilot_reports WHERE seq NOT IN "
                "(SELECT seq FROM coordinator.pilot_reports ORDER BY seq DESC LIMIT %s)",
                (PILOT_REPORTS_KEPT,),
            )

    def latest_pilot_report(self) -> dict[str, Any] | None:
        with self._acting_as(COORDINATOR_ROLE) as conn:
            row = conn.execute("SELECT report FROM coordinator.pilot_reports ORDER BY seq DESC LIMIT 1").fetchone()
        return row["report"] if row is not None else None

    def audit_sink(self) -> AuditSink:
        # Built on first use, and it opens no connection until it is used, so
        # building the app still opens none.
        with self._audit_lock:
            if self._audit is None:
                self._audit = PostgresAuditSink(
                    self._lease_tuples,
                    COORDINATOR_ROLE,
                    "coordinator.guard_audit",
                    settings=self._settings,
                    anchor_table="coordinator.guard_audit_anchor",
                )
            return self._audit
