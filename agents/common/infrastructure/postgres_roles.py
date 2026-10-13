"""Run database work as a named PostgreSQL role, one transaction at a time.

A service that holds one connection string can still keep each data source's
rows apart. Each source gets its own NOLOGIN role that owns its own schema. The
service's login role is a member of each source role. Every operation then runs
in one transaction that starts with ``SET LOCAL ROLE <role>``.

Rules this module enforces:

- **SET LOCAL, never a session-level SET ROLE.** ``SET LOCAL`` ends with the
  transaction, on COMMIT, ROLLBACK or error. A session-level ``SET ROLE`` stays
  on the server connection, and a pool or a transaction-mode pooler (such as
  Supabase's on port 6543) can hand that connection to the next caller. The
  same holds for settings such as ``statement_timeout``.
- **An autocommit connection in the idle state.** ``SET LOCAL`` outside a
  transaction block only prints a warning, and the next query runs as the login
  role. ``BEGIN`` inside a transaction that is already open only prints a
  warning too, and the role would outlive the block.
- **current_user is read back** before any work is done.
- **Role names are never bound parameters.** ``SET LOCAL ROLE $1`` is a syntax
  error in PostgreSQL. Names are checked against a strict pattern and quoted
  with ``psycopg.sql.Identifier``. Setting names are checked and quoted the
  same way; their values are quoted as literals.

:func:`acting_as` costs two round trips besides the caller's own queries. The
first sends ``BEGIN``, ``SET LOCAL ROLE``, any ``SET LOCAL`` settings and
``SELECT current_user`` as one message. This works because the message binds no
parameters, so the server runs it with the simple query protocol. The second is
``COMMIT`` (or ``ROLLBACK``). Through a pooler 20 ms away, each round trip
saved is 20 ms per operation.

On PostgreSQL 16 or later the membership is granted ``WITH INHERIT FALSE,
SET TRUE``, so the login role holds none of a source role's privileges until it
switches. PostgreSQL 15 has no per-grant options: a plain grant lets the login
role inherit every source role's privileges, so the isolation then binds the
code paths that use this module, not the credential. This module works the same
on both versions.
"""

from __future__ import annotations

from contextlib import contextmanager
import re
from typing import Any, Iterator, Mapping

try:  # psycopg is optional for callers that only validate names
    import psycopg
    from psycopg import sql
    from psycopg.pq import TransactionStatus
    from psycopg.rows import tuple_row
except ModuleNotFoundError:  # pragma: no cover - exercised only without the driver
    psycopg = None  # type: ignore[assignment]
    sql = None  # type: ignore[assignment]
    TransactionStatus = None  # type: ignore[assignment]
    tuple_row = None  # type: ignore[assignment]


class RoleScopeError(RuntimeError):
    """A role-scoped transaction could not be opened safely."""


#: Lower-case letters, digits and underscores, starting with a letter, at most
#: 63 characters (PostgreSQL's identifier limit).
_ROLE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,62}$")

#: Names that are keywords in SET ROLE, built-in roles, or platform roles with
#: wide privileges. Acting as one of them would defeat the scoping.
_RESERVED_NAMES = frozenset(
    {
        "all",
        "anon",
        "authenticated",
        "authenticator",
        "current_role",
        "current_user",
        "dashboard_user",
        "none",
        "pgbouncer",
        "postgres",
        "public",
        "service_role",
        "session_user",
        "user",
    }
)

#: ``pg_`` is reserved by PostgreSQL. ``supabase_`` roles are the platform's own.
_RESERVED_PREFIXES = ("pg_", "supabase_")

#: A setting name: a lower-case identifier, or two joined by a dot for a
#: custom setting such as ``app.site``.
_SETTING_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,62}(\.[a-z][a-z0-9_]{0,62})?$")

#: Settings that change who the transaction runs as. The role is set by
#: :func:`acting_as` itself, and nothing else may change it.
_FORBIDDEN_SETTINGS = frozenset({"role", "session_authorization"})

#: Seconds to wait for a connection when the caller does not say.
DEFAULT_CONNECT_TIMEOUT_SECONDS = 10


def validate_role_name(role: str) -> str:
    """Return ``role`` unchanged if it is safe to act as, or raise RoleScopeError.

    Params:
    - `role`: the role name, taken from the caller's own configuration. Never
      take it from a request.

    Returns:
    - The same string.
    """
    if not isinstance(role, str) or not _ROLE_PATTERN.fullmatch(role):
        raise RoleScopeError(
            f"refusing role name {role!r}: use 1-63 lower-case letters, digits or "
            "underscores, starting with a letter"
        )
    if role in _RESERVED_NAMES or role.startswith(_RESERVED_PREFIXES):
        raise RoleScopeError(f"refusing role name {role!r}: it is reserved")
    return role


def _require_driver() -> None:
    if psycopg is None:
        raise RoleScopeError("role-scoped database access needs psycopg 3")


def validate_setting_name(name: str) -> str:
    """Return ``name`` unchanged if it may be set with ``SET LOCAL``, or raise RoleScopeError.

    ``role`` and ``session_authorization`` are refused: only :func:`acting_as`
    sets the role.
    """
    if not isinstance(name, str) or not _SETTING_PATTERN.fullmatch(name):
        raise RoleScopeError(f"refusing setting name {name!r}: use a lower-case identifier")
    if name in _FORBIDDEN_SETTINGS:
        raise RoleScopeError(f"refusing setting {name!r}: acting_as sets the role itself")
    return name


def _current_user_of(row: Any) -> Any:
    return next(iter(row.values())) if isinstance(row, Mapping) else row[0]


def set_local_role(cursor_or_connection: Any, role: str) -> None:
    """Run ``SET LOCAL ROLE`` for a validated name and check ``current_user``.

    The caller must already be inside a transaction. Use :func:`acting_as`
    unless the connection is managed elsewhere, as in the framework's Postgres
    source adapter.
    """
    _require_driver()
    validate_role_name(role)
    cursor_or_connection.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
    current = _current_user_of(cursor_or_connection.execute("SELECT current_user").fetchone())
    if current != role:
        raise RoleScopeError(f"expected current_user {role!r}, found {current!r}")


def _opening(role: str, settings: Mapping[str, Any] | None) -> "sql.Composed":
    """``BEGIN; SET LOCAL ROLE ...; SET LOCAL ...; SELECT current_user``, checked and quoted."""
    statements = [sql.SQL("BEGIN"), sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(validate_role_name(role)))]
    for name, value in (settings or {}).items():
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise RoleScopeError(f"setting {name!r} needs a text or number value, got {value!r}")
        identifier = sql.SQL(".").join(sql.Identifier(part) for part in validate_setting_name(name).split("."))
        statements.append(sql.SQL("SET LOCAL {} = {}").format(identifier, sql.Literal(str(value))))
    statements.append(sql.SQL("SELECT current_user"))
    return sql.SQL("; ").join(statements)


def _rollback_quietly(conn: Any) -> None:
    """Roll back an open or failed transaction while another error is raised.

    If the rollback fails too, the connection is broken. The caller's error is
    the one worth reporting, and the next :func:`acting_as` on this connection
    refuses it, because it is not idle.
    """
    if conn.info.transaction_status in (TransactionStatus.INTRANS, TransactionStatus.INERROR):
        try:
            conn.execute("ROLLBACK")
        except psycopg.Error:
            pass


@contextmanager
def acting_as(
    conn: "psycopg.Connection", role: str, settings: Mapping[str, Any] | None = None
) -> Iterator["psycopg.Connection"]:
    """Run the block in one transaction whose ``current_user`` is ``role``.

    Params:
    - `conn`: an autocommit connection with no transaction open.
    - `role`: the role to act as. See :func:`validate_role_name`.
    - `settings`: optional ``SET LOCAL`` settings for the same transaction,
      for example ``{"statement_timeout": "15000ms"}``. Names are checked with
      :func:`validate_setting_name`.

    What it does:
    - Sends ``BEGIN``, ``SET LOCAL ROLE``, the settings and
      ``SELECT current_user`` as one message, and checks the user it returns
      before the block runs.
    - Commits when the block ends, or rolls back if it raises. Either way the
      role and the settings end with the transaction.
    - Rolls back and raises :class:`RoleScopeError` if the block ends normally
      but the transaction is not open any more: a statement failed and its
      error was caught, or the block ended the transaction itself.

    Yields:
    - The same connection.
    """
    _require_driver()
    opening = _opening(role, settings)
    if not conn.autocommit:
        raise RoleScopeError("role-scoped access needs an autocommit connection")
    if conn.info.transaction_status != TransactionStatus.IDLE:
        raise RoleScopeError(
            f"role-scoped access needs an idle connection, found {conn.info.transaction_status.name}"
        )
    try:
        with conn.cursor(row_factory=tuple_row) as cursor:
            cursor.execute(opening)
            while cursor.nextset():  # the last result is SELECT current_user
                pass
            current = _current_user_of(cursor.fetchone())
        if current != role:
            raise RoleScopeError(f"expected current_user {role!r}, found {current!r}")
        yield conn
    except BaseException:
        _rollback_quietly(conn)
        raise
    status = conn.info.transaction_status
    if status == TransactionStatus.INERROR:
        _rollback_quietly(conn)
        raise RoleScopeError("a statement failed inside the role-scoped block; the transaction was rolled back")
    if status == TransactionStatus.IDLE:
        raise RoleScopeError(
            "the role-scoped transaction ended inside the block, so later statements did not run as the role"
        )
    conn.execute("COMMIT")


def connect(dsn_or_args: str | Mapping[str, Any], **kwargs: Any) -> "psycopg.Connection":
    """Open an autocommit connection that works through a transaction pooler.

    Params:
    - `dsn_or_args`: a connection string (URI or ``key=value``), or a mapping of
      libpq keywords.
    - `kwargs`: other ``psycopg.connect`` arguments, such as ``row_factory``.

    What it does:
    - Sets ``autocommit=True``, so :func:`acting_as` can open real transactions.
    - Sets ``prepare_threshold=None``. Transaction-mode poolers do not support
      prepared statements.
    - Adds a connect timeout of 10 seconds when none is given.

    Returns:
    - An open ``psycopg.Connection``. Close it, or use it in a ``with`` block.
    """
    _require_driver()
    if isinstance(dsn_or_args, str):
        conninfo = dsn_or_args
        params: dict[str, Any] = {}
        given = psycopg.conninfo.conninfo_to_dict(conninfo)
    else:
        conninfo = ""
        params = {key: value for key, value in dsn_or_args.items() if value not in (None, "")}
        given = params
    if "connect_timeout" not in given and "connect_timeout" not in kwargs:
        params["connect_timeout"] = DEFAULT_CONNECT_TIMEOUT_SECONDS
    params.update(kwargs)
    params["autocommit"] = True
    params["prepare_threshold"] = None
    return psycopg.connect(conninfo, **params)
