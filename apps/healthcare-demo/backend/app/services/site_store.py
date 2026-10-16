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

The app writes entries and outcomes through the store. The hospital's LMA reads
the same store through :class:`app.services.site_source.StoreSiteSource`, and
keeps its audit chain (:meth:`PostgresSiteStore.audit_sink`) and its job log
(the framework's ``PostgresJobLog`` on :attr:`PostgresSiteStore.lease` and
:data:`SITE_JOB_LOG_TABLE`) on the store's connection.

Each Postgres store runs on the framework's kept connection
(:class:`agents.common.infrastructure.postgres_lease.KeptConnection`): one
autocommit connection, lent to one operation at a time. Opening a connection
through a pooler takes much longer than a query. The role and the statement
timeout are set with ``SET LOCAL``, so both end with each operation's
transaction and nothing carries over to the next one. ``close()`` closes the
connection; the next operation opens a new one. A store can also run on a
lease it is given, for example the lease of an LMA started on its own; it then
leaves that lease open when it is closed.

Every write that changes a store adds one to its :attr:`write_count`. The site
source puts that count in its version, so the LMA reads the records again
after each write through the same store object.

The demo accepts at most :data:`DEFAULT_MAX_ENTERED` entered records per
hospital. The store checks the cap in the same transaction as the insert, so
two entries at the same moment cannot both pass it.

``CoordinatorStore`` holds what the coordinator keeps itself: the newest pilot
reports and the audit chain of the release Guard. Its Postgres version acts as
``dagents_coordinator``, which cannot read any hospital schema. The pilot
reports and the audit chain are in ``coordinator.pilot_reports`` and
``coordinator.guard_audit``, created by
``supabase/migrations/0003_coordinator_records.sql``. The model registry
belongs to the GMA: the framework's ``PostgresModelStore`` keeps the model
versions in :data:`COORDINATOR_MODELS_TABLE` on the coordinator store's lease.

Each Postgres audit chain stores the point its last successful check reached in
a ``guard_audit_anchor`` table next to it (also from 0003), written by the
chain's own role. A restarted API continues the check from there instead of
hashing the whole chain again.

Database errors are raised as the framework's store errors, whose messages
never hold the connection string, its password or its host:
:class:`StoreUnavailableError` when the database cannot be reached or an
operation fails, and :class:`StoreNotSetUpError` when a schema, table, role or
grant is missing, so the API can say that the database is not set up for this
version instead of failing with a 500.

Records follow the app's convention: a missing value is an absent key, never
``None``. A database NULL becomes an absent key.

The demo has one connection string, ``HEALTHCARE_DEMO_DATABASE_URL``. A
deployment that gives each hospital its own login can set
``HEALTHCARE_DEMO_SITE_<SITE>_DATABASE_URL`` (for example
``HEALTHCARE_DEMO_SITE_UK_134_DATABASE_URL``) and
:meth:`PostgresSiteStore.from_environment` uses it for that hospital.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
import re
import threading
from typing import Any, Iterable, Mapping, Protocol

from agents.common.application.ethical_guard import AuditSink, InMemoryAuditLog
from agents.common.domain.errors import StoreNotSetUpError, StoreUnavailableError
from agents.common.infrastructure.postgres_audit import PostgresAuditSink
from agents.common.infrastructure.postgres_lease import (
    DEFAULT_LIVENESS_IDLE_SECONDS,
    DEFAULT_STATEMENT_TIMEOUT_MS,
    KeptConnection,
    not_set_up_errors,
)
from app.domain.dataset import AGE_BANDS, AGE_MAX, AGE_MIN, Site, age_band_for
from app.services.db_hints import connection_hint

__all__ = [
    "BOOKKEEPING_COLUMNS",
    "CATEGORIES",
    "COORDINATOR_MODELS_TABLE",
    "COORDINATOR_ROLE",
    "COORDINATOR_SCHEMA",
    "DEFAULT_LIVENESS_IDLE_SECONDS",
    "DEFAULT_MAX_ENTERED",
    "DEFAULT_STATEMENT_TIMEOUT_MS",
    "EntryLimitReached",
    "FIELD_COLUMNS",
    "IdempotencyKeyReused",
    "MemoryCoordinatorStore",
    "MemorySiteStore",
    "OutcomeAlreadyRecorded",
    "PILOT_REPORTS_KEPT",
    "PostgresCoordinatorStore",
    "PostgresSiteStore",
    "ROW_COLUMNS",
    "RecordRejected",
    "SITE_JOB_LOG_TABLE",
    "SiteStore",
    "CoordinatorStore",
    "StoreNotSetUpError",
    "StoreUnavailableError",
    "TABLE_COLUMNS",
    "UnknownEncounterError",
    "not_set_up_errors",
    "site_database_url",
    "validate_fields",
    "validate_stored_row",
]


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


class EntryLimitReached(RuntimeError):
    """The hospital already holds the most entered records the demo accepts."""


#: Most records one hospital accepts from the demo's data-entry form. The public
#: demo has no accounts, so this keeps one visitor from filling a store.
DEFAULT_MAX_ENTERED = 300


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


def _check_max_entered(max_entered: int) -> int:
    if isinstance(max_entered, bool) or not isinstance(max_entered, int) or max_entered < 1:
        raise ValueError(f"max_entered must be a whole number of 1 or more, got {max_entered!r}")
    return max_entered


def _entry_limit(site: Site, max_entered: int) -> EntryLimitReached:
    return EntryLimitReached(
        f"{site.display_name} already holds {max_entered} entered records, the most this demo accepts"
    )


# --- The interface -------------------------------------------------------------


class SiteStore(Protocol):
    """One hospital's store. Both implementations behave the same way."""

    kind: str
    location: dict[str, str]
    site_id: str
    #: The role every operation acts as. ``None`` for the in-memory store.
    role: str | None
    #: The kept connection, or ``None`` for the in-memory store.
    lease: KeptConnection | None
    #: Most entered records the store accepts.
    max_entered: int

    @property
    def write_count(self) -> int:
        """How many writes through this store object changed its records."""

    def records(self) -> list[dict[str, Any]]:
        """Every row: the row columns, with missing values as absent keys."""

    def count(self) -> dict[str, int]:
        """``{"total", "entered", "labelled"}``."""

    def append(self, record: Mapping[str, Any], client_request_id: str | None = None) -> dict[str, Any]:
        """Store a new entry and return it. A repeated key returns the first entry.

        Raises :class:`EntryLimitReached` when the store already holds
        :attr:`max_entered` entered records. A retry of an earlier entry is
        refused too once the limit is reached.
        """

    def set_outcome(self, encounter_id: str, poor_outcome_6m: bool) -> dict[str, Any]:
        """Record the six-month outcome of an entered record and return it.

        An outcome is recorded once. The same value again returns the record
        unchanged; a different value raises :class:`OutcomeAlreadyRecorded`.
        """

    def list_entered(self, limit: int) -> list[dict[str, Any]]:
        """Entered records, newest first."""

    def audit_sink(self) -> AuditSink:
        """The hospital's own audit chain. The same sink each time."""

    def close(self) -> None:
        """Release the store's database connection, if it has one."""


# --- In memory -------------------------------------------------------------------


class MemorySiteStore:
    """A hospital's rows in this process. Nothing survives a restart."""

    kind = "in-memory"
    role: str | None = None
    lease: KeptConnection | None = None

    def __init__(
        self, site: Site, records: Iterable[Mapping[str, Any]], *, max_entered: int = DEFAULT_MAX_ENTERED
    ) -> None:
        self.site = site
        self.site_id = site.site_id
        self.location = {"memory": "process"}
        self.max_entered = _check_max_entered(max_entered)
        self._lock = threading.Lock()
        loaded_at = datetime.now(timezone.utc)
        rows = []
        for record in records:
            row = validate_stored_row(record, site.site_id)
            row["recorded_at"] = loaded_at
            rows.append(row)
        _check_unique(rows)
        self._rows: list[dict[str, Any]] = sorted(rows, key=_sort_key)
        self._writes = 0
        self._audit = InMemoryAuditLog()

    @property
    def write_count(self) -> int:
        """How many writes through this store changed its records."""
        with self._lock:
            return self._writes

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
            entered = [row for row in self._rows if row["origin"] == "entered"]
            if len(entered) >= self.max_entered:
                raise _entry_limit(self.site, self.max_entered)
            if key is not None:
                for row in entered:
                    if row.get("client_request_id") == key:
                        if not _same_entry(row, fields):
                            raise IdempotencyKeyReused(
                                "this idempotency key was already used for a different record"
                            )
                        return _entry_view(row)
            number = 1 + max((_entry_number(row["encounter_id"]) for row in entered), default=0)
            row = {
                "encounter_id": f"{self.site_id}-e{number}",
                "origin": "entered",
                **fields,
                "recorded_at": datetime.now(timezone.utc),
                "client_request_id": key,
            }
            self._rows.append(row)
            self._writes += 1
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
                    self._writes += 1
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

#: The framework table each hospital's LMA keeps its job log in
#: (``PostgresJobLog``), acting as the hospital's role. Row-level security
#: shows each hospital only its own rows (migration 0002).
SITE_JOB_LOG_TABLE = "coordinator.site_contributions"


def site_database_url(site: Site, default: str, environ: Mapping[str, str] | None = None) -> str:
    """The connection string for one hospital.

    ``HEALTHCARE_DEMO_SITE_<SITE>_DATABASE_URL`` (``uk-134`` becomes
    ``UK_134``) when it is set, otherwise ``default``.
    """
    env = os.environ if environ is None else environ
    name = f"HEALTHCARE_DEMO_SITE_{site.site_id.upper().replace('-', '_')}_DATABASE_URL"
    return (env.get(name) or "").strip() or default


class _LeasedStore:
    """A store that runs every operation on one kept connection.

    Params:
    - `dsn`: the connection string. The store builds its own lease from it and
      closes that lease in :meth:`close`.
    - `lease`: a lease to run on instead, for example an LMA's. The store
      leaves it open in :meth:`close`; its owner closes it.
    - `name`: names the store in error messages, so a 503 says which store
      failed. Never a host or a credential.
    - `statement_timeout_ms`, `liveness_idle_seconds`: passed to the lease it
      builds.

    Give exactly one of `dsn` and `lease`.
    """

    def __init__(
        self,
        dsn: str | None,
        lease: KeptConnection | None,
        *,
        name: str,
        statement_timeout_ms: int,
        liveness_idle_seconds: float,
    ) -> None:
        if lease is not None:
            if dsn is not None:
                raise ValueError("give a Postgres store a connection string or a lease, not both")
            self.lease = lease
            self._owns_lease = False
        else:
            if not isinstance(dsn, str) or not dsn.strip():
                raise ValueError("a Postgres store needs a connection string or a lease")
            self.lease = KeptConnection(
                dsn,
                name=name,
                statement_timeout_ms=statement_timeout_ms,
                liveness_idle_seconds=liveness_idle_seconds,
                hint=connection_hint,
            )
            self._owns_lease = True
        self._writes = 0
        self._writes_lock = threading.Lock()

    @property
    def connections_opened(self) -> int:
        """How many connections the store's lease has opened."""
        return self.lease.connections_opened

    @property
    def write_count(self) -> int:
        """How many writes through this store changed its records."""
        with self._writes_lock:
            return self._writes

    def _wrote(self) -> None:
        """Count one write. Called after its transaction committed."""
        with self._writes_lock:
            self._writes += 1

    def close(self) -> None:
        """Close the kept connection if the store built it. The next operation opens a new one."""
        if self._owns_lease:
            self.lease.close()


class PostgresSiteStore(_LeasedStore):
    """A hospital's rows in its own schema, read and written as its own role."""

    kind = "postgres"

    def __init__(
        self,
        site: Site,
        dsn: str | None = None,
        *,
        lease: KeptConnection | None = None,
        max_entered: int = DEFAULT_MAX_ENTERED,
        statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
        liveness_idle_seconds: float = DEFAULT_LIVENESS_IDLE_SECONDS,
    ) -> None:
        super().__init__(
            dsn,
            lease,
            name=f"hospital {site.site_id}",
            statement_timeout_ms=statement_timeout_ms,
            liveness_idle_seconds=liveness_idle_seconds,
        )
        from psycopg import sql

        self.site = site
        self.site_id = site.site_id
        self.role = site.role
        self.location = {"schema": site.schema, "role": site.role}
        self.max_entered = _check_max_entered(max_entered)
        self._sql = sql
        self._table = sql.Identifier(site.schema, "encounters")
        self._audit: PostgresAuditSink | None = None
        self._audit_lock = threading.Lock()

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
        with self.lease.acting_as(self.role) as conn:
            rows = conn.execute(query).fetchall()
        return [_record_view(row) for row in rows]

    def count(self) -> dict[str, int]:
        query = self._q(
            "SELECT count(*) AS total, count(*) FILTER (WHERE origin = 'entered') AS entered, "
            "count(poor_outcome_6m) AS labelled FROM {table}"
        )
        with self.lease.acting_as(self.role) as conn:
            row = conn.execute(query).fetchone()
        return {key: int(row[key]) for key in ("total", "entered", "labelled")}

    def append(self, record: Mapping[str, Any], client_request_id: str | None = None) -> dict[str, Any]:
        fields = validate_fields(record)
        key = _check_request_id(client_request_id)
        with self.lease.acting_as(self.role) as conn:
            # One writer at a time, so two entries cannot take the same number
            # or both pass the cap.
            conn.execute(self._q("LOCK TABLE {table} IN SHARE ROW EXCLUSIVE MODE"))
            entered = conn.execute(
                self._q(
                    "SELECT count(*) AS n, "
                    "coalesce(max(substring(encounter_id from '-e([0-9]+)$')::int), 0) + 1 AS next "
                    "FROM {table} WHERE origin = 'entered'"
                )
            ).fetchone()
            if int(entered["n"]) >= self.max_entered:
                raise _entry_limit(self.site, self.max_entered)
            if key is not None:
                existing = conn.execute(
                    self._q("SELECT * FROM {table} WHERE client_request_id = %s"), (key,)
                ).fetchone()
                if existing is not None:
                    if not _same_entry(existing, fields):
                        raise IdempotencyKeyReused("this idempotency key was already used for a different record")
                    return _entry_view(existing)
            row = {
                "encounter_id": f"{self.site_id}-e{entered['next']}",
                "origin": "entered",
                **fields,
                "client_request_id": key,
            }
            columns = list(row)
            inserted = conn.execute(
                self._sql.SQL("INSERT INTO {table} ({columns}) VALUES ({values}) RETURNING *").format(
                    table=self._table,
                    columns=self._select(columns),
                    values=self._sql.SQL(", ").join(self._sql.Placeholder() * len(columns)),
                ),
                [row[column] for column in columns],
            ).fetchone()
        self._wrote()
        return _entry_view(inserted)

    def set_outcome(self, encounter_id: str, poor_outcome_6m: bool) -> dict[str, Any]:
        if not isinstance(poor_outcome_6m, bool):
            raise RecordRejected({"poor_outcome_6m": "must be true or false"})
        with self.lease.acting_as(self.role) as conn:
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
        self._wrote()
        return _entry_view(updated)

    def list_entered(self, limit: int) -> list[dict[str, Any]]:
        _check_limit(limit)
        with self.lease.acting_as(self.role) as conn:
            rows = conn.execute(
                self._q(
                    "SELECT * FROM {table} WHERE origin = 'entered' "
                    "ORDER BY length(encounter_id) DESC, encounter_id DESC LIMIT %s"
                ),
                (limit,),
            ).fetchall()
        return [_entry_view(row) for row in rows]

    def audit_sink(self) -> AuditSink:
        # Built on first use, and it opens no connection until it is used.
        # The sink takes the store's own connection for each operation; the
        # lease releases it when the sink's ``with`` block ends and does not
        # close it. The anchor table (0003) lets a restarted API continue the
        # chain check where the last process left it.
        with self._audit_lock:
            if self._audit is None:
                self._audit = PostgresAuditSink(
                    self.lease.lease_tuples,
                    self.role,
                    f"{self.site.schema}.guard_audit",
                    settings=self.lease.settings,
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
        with self.lease.acting_as(self.role) as conn:
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
        self._wrote()
        return self.count()


# --- The coordinator ---------------------------------------------------------------

COORDINATOR_ROLE = "dagents_coordinator"
COORDINATOR_SCHEMA = "coordinator"

#: The framework table the GMA keeps its model versions in
#: (``PostgresModelStore``), acting as :data:`COORDINATOR_ROLE`.
COORDINATOR_MODELS_TABLE = "coordinator.models"


def _json_copy(value: Any) -> Any:
    """Copy through JSON, as the database does, so both stores return the same types."""
    return json.loads(json.dumps(value, allow_nan=False))


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


class CoordinatorStore(Protocol):
    """What the coordinator keeps itself: pilot reports and the release Guard's audit chain."""

    kind: str
    location: dict[str, str]
    #: The role every operation acts as. ``None`` for the in-memory store.
    role: str | None
    #: The kept connection, or ``None`` for the in-memory store. The GMA keeps
    #: its model versions on it.
    lease: KeptConnection | None

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
    role: str | None = None
    lease: KeptConnection | None = None

    def __init__(self) -> None:
        self.location = {"memory": "process"}
        self._lock = threading.Lock()
        # Oldest first. A dict keeps the order in which each pilot was first saved.
        self._pilot_reports: dict[str, dict[str, Any]] = {}
        self._audit = InMemoryAuditLog()

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


class PostgresCoordinatorStore(_LeasedStore):
    """The coordinator's records in the ``coordinator`` schema, read and written as ``dagents_coordinator``."""

    kind = "postgres"

    def __init__(
        self,
        dsn: str | None = None,
        *,
        lease: KeptConnection | None = None,
        statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
        liveness_idle_seconds: float = DEFAULT_LIVENESS_IDLE_SECONDS,
    ) -> None:
        super().__init__(
            dsn,
            lease,
            name="coordinator store",
            statement_timeout_ms=statement_timeout_ms,
            liveness_idle_seconds=liveness_idle_seconds,
        )
        self.role = COORDINATOR_ROLE
        self.location = {"schema": COORDINATOR_SCHEMA, "role": COORDINATOR_ROLE}
        self._audit: PostgresAuditSink | None = None
        self._audit_lock = threading.Lock()

    def save_pilot_report(self, pilot_id: str, report: Mapping[str, Any]) -> None:
        from psycopg.types.json import Jsonb

        stored = _check_pilot_report(pilot_id, report)
        # coordinator.pilot_reports is created by 0003_coordinator_records.sql.
        # seq orders the reports; a pilot saved again keeps its place.
        with self.lease.acting_as(self.role) as conn:
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
        with self.lease.acting_as(self.role) as conn:
            row = conn.execute("SELECT report FROM coordinator.pilot_reports ORDER BY seq DESC LIMIT 1").fetchone()
        return row["report"] if row is not None else None

    def audit_sink(self) -> AuditSink:
        # Built on first use, and it opens no connection until it is used, so
        # building the app still opens none.
        with self._audit_lock:
            if self._audit is None:
                self._audit = PostgresAuditSink(
                    self.lease.lease_tuples,
                    self.role,
                    "coordinator.guard_audit",
                    settings=self.lease.settings,
                    anchor_table="coordinator.guard_audit_anchor",
                )
            return self._audit
