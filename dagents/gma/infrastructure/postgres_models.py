"""Where the GMA's model registry keeps its versions: in memory, or in a Postgres table.

Both stores implement :class:`dagents.gma.application.model_registry.ModelStore`
and pass the same test suite (``dagents/tests/test_model_registry.py``).

``PostgresModelStore`` runs every operation in one transaction on a kept
connection, acting as one role
(:meth:`dagents.common.infrastructure.postgres_lease.KeptConnection.acting_as`).
The table must have these columns, which are the generic columns of the
healthcare demo's ``coordinator.models`` table (migration ``0002``), except
that ``params`` is ``json``::

    version          text primary key check (length(version) between 1 and 200)
    kind             text not null check (length(kind) between 1 and 64)
    params           json not null
    digest           text not null check (length(digest) between 1 and 200)
    parent_version   text references <the same table> (version)
    created_at       timestamptz not null default now()
    status           text not null check (status in ('candidate', 'released', 'rejected', 'superseded'))
    release_decision jsonb
    decided_at       timestamptz

and a unique index that allows one released version::

    create unique index models_one_release on <table> ((true)) where status = 'released';

A lifecycle event runs in one transaction. Each status change is an
``UPDATE`` that names the status it expects, so a version another request
changed first is not changed again, and the unique index refuses a second
release. The changes run in this order: changes away from ``released`` and
other statuses, then the new version, then the change to ``released``, so an
approval never holds two released versions at once.

A stored release decision holds ``action``, ``guard_permitted``,
``gate_results``, ``round_id``, and ``baseline_version`` and
``metrics_source`` when they are known; ``decided_at`` is its own column.
Decisions written before ``metrics_source`` existed read with it empty.

**Parameters are stored as JSON text.** The store binds them with psycopg's
``Json``, and a ``json`` column keeps the text it was sent, so a version's
parameters come back as they were registered and its digest matches when a
restarted GMA loads it. A ``jsonb`` column returns ``-0.0`` as ``0.0``, and
``1e16`` and larger floats as whole numbers. A ``Jsonb`` parameter does the
same even into a ``json`` column. A model kind whose digest tells these apart
then refuses the version. A kind that writes ``-0.0`` into the text it hashes
refuses it for ``-0.0``, and a kind that hashes the JSON text as stored
refuses it for both. A table whose ``params`` column is still ``jsonb`` works
as it did before, with that limit, until an operator runs
:data:`PARAMS_TO_JSON_SQL` on it.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import threading
from typing import Any, Mapping, Sequence

from pydantic import ValidationError

from dagents.common.domain.errors import StoreNotSetUpError
from dagents.common.domain.federation import ModelReleaseDecision, ModelStatusChange, ModelVersion
from dagents.common.infrastructure.postgres_lease import KeptConnection
from dagents.common.infrastructure.postgres_roles import table_identifier, validate_role_name
from dagents.gma.application.model_registry import (
    InvalidModelVersion,
    ModelStatusConflict,
    ModelVersionExists,
    UnknownModelVersion,
    adds_version,
    check_limit,
    check_new_model,
    check_version_name,
    json_copy,
)

try:  # psycopg is optional for callers that keep the registry in memory
    from psycopg import errors, sql
    from psycopg.rows import dict_row
    from psycopg.types.json import Json, Jsonb
except ModuleNotFoundError:  # pragma: no cover - exercised only without the driver
    errors = None  # type: ignore[assignment]
    sql = None  # type: ignore[assignment]
    dict_row = None  # type: ignore[assignment]
    Json = None  # type: ignore[assignment]
    Jsonb = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

#: Changes a ``params`` column from ``jsonb`` to ``json``. ``{table}`` is the
#: models table, such as ``coord.models``. Rows stored before the change keep
#: the values ``jsonb`` gave them; rows stored after it keep what was sent.
PARAMS_TO_JSON_SQL = "alter table {table} alter column params type json using params::json;"

COLUMNS = (
    "version",
    "kind",
    "params",
    "digest",
    "parent_version",
    "created_at",
    "status",
    "release_decision",
    "decided_at",
)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _now() -> str:
    return _timestamp(datetime.now(timezone.utc))


def _stored_decision(decision: ModelReleaseDecision) -> dict[str, Any]:
    """The decision as it is stored: without ``decided_at``, and without the keys that are not known."""
    data = decision.model_dump(mode="json", exclude={"decided_at"})
    for key in ("baseline_version", "metrics_source"):
        if data.get(key) is None:
            data.pop(key, None)
    return json_copy(data)


def _read_decision(
    version: str, stored: Mapping[str, Any] | None, decided_at: str | None
) -> ModelReleaseDecision | None:
    """Read a stored decision. One that cannot be read counts as no decision, so an approval is refused."""
    if stored is None:
        return None
    try:
        return ModelReleaseDecision.model_validate({**stored, "decided_at": decided_at})
    except ValidationError:
        logger.warning("the stored release decision of model version %s cannot be read; it is ignored", version)
        return None


def _check_changes(changes: Sequence[ModelStatusChange], new: ModelVersion | None) -> None:
    """Check that the changes add at most ``new``, and with ``new``'s status."""
    for item in changes:
        check_version_name(item.version)
        if not adds_version(item):
            continue
        if new is None or item.version != new.version:
            raise ValueError(f"a status change adds {item.version}, but no new version was given for it")
        if item.to_status != new.status:
            raise ValueError(f"{new.version} is added as {new.status}, but its status change says {item.to_status}")


class InMemoryModelStore:
    """Model versions in this process. One lock covers every read and write."""

    kind = "in-memory"

    def __init__(self) -> None:
        self.location = {"memory": "process"}
        self._rows: dict[str, dict[str, Any]] = {}
        self._sequence = 0
        self._lock = threading.Lock()

    @staticmethod
    def _model(row: Mapping[str, Any]) -> ModelVersion:
        return ModelVersion(
            version=row["version"],
            kind=row["kind"],
            digest=row["digest"],
            status=row["status"],
            parent_version=row["parent_version"],
            created_at=row["created_at"],
            params=json_copy(row["params"]),
            release_decision=_read_decision(row["version"], row["release_decision"], row["decided_at"]),
        )

    def get(self, version: str) -> ModelVersion | None:
        with self._lock:
            row = self._rows.get(version)
            return self._model(row) if row is not None else None

    def released(self) -> ModelVersion | None:
        with self._lock:
            for row in self._rows.values():
                if row["status"] == "released":
                    return self._model(row)
        return None

    def list(self, limit: int) -> list[ModelVersion]:
        check_limit(limit)
        with self._lock:
            newest = sorted(self._rows.values(), key=lambda row: row["sequence"], reverse=True)[:limit]
            return [self._model(row) for row in newest]

    def apply(
        self,
        changes: Sequence[ModelStatusChange],
        *,
        new: ModelVersion | None = None,
        decision: tuple[str, ModelReleaseDecision] | None = None,
    ) -> None:
        if new is not None:
            check_new_model(new)
        _check_changes(changes, new)
        stored_decision = _stored_decision(decision[1]) if decision is not None else None
        with self._lock:
            # Work out the result first, so a refusal changes nothing.
            statuses = {version: row["status"] for version, row in self._rows.items()}
            if new is not None:
                if new.version in statuses:
                    raise ModelVersionExists(f"model version {new.version} is already registered")
                if new.parent_version is not None and new.parent_version not in statuses:
                    raise UnknownModelVersion(f"the parent version {new.parent_version} is not registered")
                statuses[new.version] = new.status
            for item in changes:
                if adds_version(item):
                    continue
                if statuses.get(item.version) != item.from_status:
                    raise ModelStatusConflict(
                        f"model version {item.version} is not stored as {item.from_status}; nothing was changed"
                    )
                statuses[item.version] = item.to_status
            if sum(1 for status in statuses.values() if status == "released") > 1:
                raise ModelStatusConflict("another version is already released; nothing was changed")
            if decision is not None and decision[0] not in statuses:
                raise UnknownModelVersion(f"model version {decision[0]} is not registered")

            if new is not None:
                self._sequence += 1
                self._rows[new.version] = {
                    "version": new.version,
                    "kind": new.kind,
                    "params": json_copy(new.params),
                    "digest": new.digest,
                    "parent_version": new.parent_version,
                    "created_at": _now(),
                    "status": new.status,
                    "release_decision": None,
                    "decided_at": None,
                    "sequence": self._sequence,
                }
            for version, status in statuses.items():
                self._rows[version]["status"] = status
            if decision is not None:
                row = self._rows[decision[0]]
                row["release_decision"] = stored_decision
                row["decided_at"] = _now()


class PostgresModelStore:
    """Model versions in one Postgres table, every operation acting as one role.

    Params:
    - `lease`: the kept connection, shared with the agent's other stores.
    - `role`: the role every read and write acts as. Take it from
      configuration, never from a request.
    - `table`: the models table, as ``schema.table``.

    Errors from the connection come from the lease: ``StoreUnavailableError``
    and ``StoreNotSetUpError``, with no connection details in their messages.
    """

    kind = "postgres"

    def __init__(self, lease: KeptConnection, role: str, table: str) -> None:
        if sql is None:
            raise RuntimeError("PostgresModelStore needs psycopg 3")
        self._lease = lease
        self.role = validate_role_name(role)
        self.table = table
        self._table = table_identifier(table, "models table")
        self.location = {"table": table, "role": self.role}
        self._select = sql.SQL("SELECT {columns} FROM {table}").format(
            columns=sql.SQL(", ").join(sql.Identifier(column) for column in COLUMNS), table=self._table
        )

    def _model(self, row: Mapping[str, Any]) -> ModelVersion:
        decided_at = _timestamp(row["decided_at"]) if row["decided_at"] is not None else None
        return ModelVersion(
            version=row["version"],
            kind=row["kind"],
            digest=row["digest"],
            status=row["status"],
            parent_version=row["parent_version"],
            created_at=_timestamp(row["created_at"]),
            params=row["params"],
            release_decision=_read_decision(row["version"], row["release_decision"], decided_at),
        )

    def _rows(self, query: Any, values: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self._lease.acting_as(self.role) as conn, conn.cursor(row_factory=dict_row) as cursor:
            return list(cursor.execute(query, values).fetchall())

    def get(self, version: str) -> ModelVersion | None:
        rows = self._rows(sql.SQL("{select} WHERE version = %s").format(select=self._select), (version,))
        return self._model(rows[0]) if rows else None

    def released(self) -> ModelVersion | None:
        rows = self._rows(sql.SQL("{select} WHERE status = 'released' LIMIT 2").format(select=self._select))
        if len(rows) > 1:
            raise StoreNotSetUpError(
                f"more than one version is released in {self.table}; the table needs its one-release unique index"
            )
        return self._model(rows[0]) if rows else None

    def list(self, limit: int) -> list[ModelVersion]:
        query = sql.SQL("{select} ORDER BY created_at DESC, version DESC LIMIT %s").format(select=self._select)
        return [self._model(row) for row in self._rows(query, (check_limit(limit),))]

    def apply(
        self,
        changes: Sequence[ModelStatusChange],
        *,
        new: ModelVersion | None = None,
        decision: tuple[str, ModelReleaseDecision] | None = None,
    ) -> None:
        if new is not None:
            check_new_model(new)
        _check_changes(changes, new)
        moves = [item for item in changes if not adds_version(item)]
        first = [item for item in moves if item.to_status != "released"]
        last = [item for item in moves if item.to_status == "released"]
        with self._lease.acting_as(self.role) as conn, conn.cursor(row_factory=dict_row) as cursor:
            for item in first:
                self._move(cursor, item)
            if new is not None:
                self._insert(cursor, new)
            for item in last:
                self._move(cursor, item)
            if decision is not None:
                self._decide(cursor, decision[0], decision[1])

    def _move(self, cursor: Any, item: ModelStatusChange) -> None:
        query = sql.SQL("UPDATE {table} SET status = %s WHERE version = %s AND status = %s RETURNING version").format(
            table=self._table
        )
        try:
            row = cursor.execute(query, (item.to_status, item.version, item.from_status)).fetchone()
        except errors.UniqueViolation as error:
            raise ModelStatusConflict("another version is already released; nothing was changed") from error
        if row is None:
            raise ModelStatusConflict(
                f"model version {item.version} is not stored as {item.from_status}; nothing was changed"
            )

    def _insert(self, cursor: Any, new: ModelVersion) -> None:
        query = sql.SQL(
            "INSERT INTO {table} (version, kind, params, digest, parent_version, status) "
            "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (version) DO NOTHING RETURNING version"
        ).format(table=self._table)
        # Json, never Jsonb: a Jsonb parameter changes -0.0 and 1e16 even in a json column.
        values = (new.version, new.kind, Json(json_copy(new.params)), new.digest, new.parent_version, new.status)
        try:
            row = cursor.execute(query, values).fetchone()
        except errors.ForeignKeyViolation as error:
            raise UnknownModelVersion(f"the parent version {new.parent_version} is not registered") from error
        except errors.UniqueViolation as error:
            raise ModelStatusConflict("another version is already released; nothing was changed") from error
        except errors.CheckViolation as error:
            raise InvalidModelVersion(f"model version {new.version} does not fit the table's checks") from error
        if row is None:
            raise ModelVersionExists(f"model version {new.version} is already registered")

    def _decide(self, cursor: Any, version: str, decision: ModelReleaseDecision) -> None:
        query = sql.SQL(
            "UPDATE {table} SET release_decision = %s, decided_at = now() WHERE version = %s RETURNING version"
        ).format(table=self._table)
        try:
            # The decision column stays jsonb: the healthcare demo's migration checks it with jsonb functions.
            row = cursor.execute(query, (Jsonb(_stored_decision(decision)), version)).fetchone()
        except errors.CheckViolation as error:
            raise InvalidModelVersion(f"the release decision for {version} does not fit the table's checks") from error
        if row is None:
            raise UnknownModelVersion(f"model version {version} is not registered")


__all__ = ["COLUMNS", "PARAMS_TO_JSON_SQL", "InMemoryModelStore", "PostgresModelStore"]
