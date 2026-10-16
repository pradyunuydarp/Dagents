"""One kept Postgres connection, used by one operation at a time, each acting as one role.

A store that reaches Postgres through a pooler spends much longer opening a
connection than running a query. ``KeptConnection`` keeps one autocommit
connection and lends it to one operation at a time, behind a lock. Each
operation runs in its own transaction that starts with ``SET LOCAL ROLE`` and a
``SET LOCAL`` statement timeout (:func:`postgres_roles.acting_as`), so the role
and the timeout end with the operation and nothing carries over to the next.

What the lease does besides lending the connection:

- It replaces a connection that is closed, broken or not idle. A connection
  that has been idle for longer than ``liveness_idle_seconds`` is checked with
  ``SELECT 1`` first, because poolers and proxies close idle connections.
- An operation that starts while the same thread already holds the connection
  gets a connection of its own for that operation, because the kept one is
  inside a transaction.
- It maps database errors to the framework's store errors:

  | Database error | Raised as |
  |---|---|
  | A connection failure, a lost connection or a statement timeout (``OperationalError``, ``InterfaceError``) | :class:`StoreUnavailableError`; the connection is discarded |
  | A missing schema, table, column, function or role, or a missing grant | :class:`StoreNotSetUpError`; the connection is kept |
  | Any other error | Unchanged, so callers can handle it (the audit sink retries on ``UniqueViolation``) |

- An error message never holds the connection string, its password or its
  host. Agents put these messages in the ``detail`` of their responses. The
  driver's own text is kept only when the error carries a SQLSTATE, which
  means the server sent it, and server messages describe statements and
  objects, not the connection. The connection string and the password are
  removed from that text as well. The driver's error stays available to the
  process as the ``__cause__``, except for a malformed connection string,
  whose error quotes the string.

``lease`` fits :class:`dagents.common.infrastructure.postgres_audit.PostgresAuditSink`'s
connection factory, so an audit chain can share the store's connection::

    lease = KeptConnection(dsn, name="site a")
    sink = PostgresAuditSink(lease.lease, "site_a", "site_a.guard_audit", settings=lease.settings)
"""

from __future__ import annotations

from contextlib import contextmanager
import threading
import time
from typing import Any, Callable, Iterator, Mapping
from urllib.parse import quote
import weakref

from dagents.common.domain.errors import StoreNotSetUpError, StoreUnavailableError
from dagents.common.infrastructure.postgres_roles import acting_as, connect

try:  # psycopg is optional for callers that never open a connection
    import psycopg
    from psycopg.pq import TransactionStatus
    from psycopg.rows import dict_row, tuple_row
except ModuleNotFoundError:  # pragma: no cover - exercised only without the driver
    psycopg = None  # type: ignore[assignment]
    TransactionStatus = None  # type: ignore[assignment]
    dict_row = None  # type: ignore[assignment]
    tuple_row = None  # type: ignore[assignment]


#: Statement timeout for one operation, in milliseconds. Role settings do not
#: apply after ``SET ROLE``, so the lease sets it with ``SET LOCAL`` in each
#: transaction.
DEFAULT_STATEMENT_TIMEOUT_MS = 15_000

#: Seconds the connection may sit idle before the next operation checks it
#: with ``SELECT 1``.
DEFAULT_LIVENESS_IDLE_SECONDS = 30.0

#: What replaces a secret in an error message.
_REDACTED = "[redacted]"


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


def _secrets_of(dsn: str) -> list[str]:
    """The texts an error message must not hold: the connection string and its password."""
    secrets = [dsn]
    try:
        password = psycopg.conninfo.conninfo_to_dict(dsn).get("password") if psycopg is not None else None
    except Exception:  # noqa: BLE001 - a malformed string has no password to find
        password = None
    if password:
        secrets += [str(password), quote(str(password), safe="")]
    # Longer secrets first, so the connection string is replaced as a whole.
    return sorted({secret for secret in secrets if secret}, key=len, reverse=True)


def _scrub(text: str, dsn: str) -> str:
    for secret in _secrets_of(dsn):
        text = text.replace(secret, _REDACTED)
    return text


def describe_database_error(error: BaseException, dsn: str) -> str:
    """Name a database error in words that hold no connection detail.

    Params:
    - `error`: the error the driver raised.
    - `dsn`: the connection string in use. It and its password are removed from
      the text.

    Returns:
    - ``"ClassName: message"`` when the error carries a SQLSTATE (the server
      sent it), with the first line of the message only.
    - ``"ClassName"`` otherwise. libpq writes the host and port into its own
      messages, so their text is left out.
    """
    name = type(error).__name__
    if getattr(error, "sqlstate", None) is None:
        return name
    message = str(error).strip().splitlines()
    if not message:
        return name
    return f"{name}: {_scrub(message[0], dsn)}"


class _ConnectionSlot:
    """Holds the connection, so it can be closed when the lease is dropped."""

    def __init__(self) -> None:
        self.conn: Any = None

    def close(self) -> None:
        conn, self.conn = self.conn, None
        if conn is not None and not conn.closed:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - the connection is being thrown away
                pass


class KeptConnection:
    """Keep one autocommit connection and lend it to one operation at a time.

    Params:
    - `dsn`: the connection string. It is never put in an error message.
    - `name`: names the store in error messages, so a 503 says which store
      failed, for example ``"site a"``. Never a host or a credential.
    - `statement_timeout_ms`: the statement timeout inside each operation.
    - `liveness_idle_seconds`: how long the connection may be idle before the
      next operation checks it. ``0`` checks before every operation.
    - `row_factory`: how rows are returned. Rows are dicts by default.
    - `hint`: optional advice added to a connection failure, such as how to
      reach a provider's pooler. It gets the connection string; the connection
      string and its password are removed from what it returns.

    ``connections_opened`` counts the connections this lease has opened.
    """

    def __init__(
        self,
        dsn: str,
        *,
        name: str = "",
        statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
        liveness_idle_seconds: float = DEFAULT_LIVENESS_IDLE_SECONDS,
        row_factory: Any = None,
        hint: Callable[[str], str] | None = None,
    ) -> None:
        if psycopg is None:
            raise RuntimeError("a kept Postgres connection needs psycopg 3")
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("a kept Postgres connection needs a connection string")
        if int(statement_timeout_ms) <= 0:
            raise ValueError("the statement timeout must be a positive number of milliseconds")
        self._dsn = dsn
        self.name = name
        self.statement_timeout_ms = int(statement_timeout_ms)
        self.liveness_idle_seconds = float(liveness_idle_seconds)
        self._row_factory = row_factory
        self._hint = hint
        self._slot = _ConnectionSlot()
        # A lease that is dropped without close() still closes its connection.
        weakref.finalize(self, self._slot.close)
        self._lock = threading.RLock()
        self._depth = 0
        self._last_used = 0.0
        self.connections_opened = 0

    def __repr__(self) -> str:
        return f"KeptConnection(name={self.name!r}, connections_opened={self.connections_opened})"

    @property
    def settings(self) -> dict[str, str]:
        """``SET LOCAL`` settings for every operation: the statement timeout. A new dict each time."""
        return {"statement_timeout": f"{self.statement_timeout_ms}ms"}

    def _prefix(self) -> str:
        return f"{self.name}: " if self.name else ""

    def _open(self) -> Any:
        try:
            conn = connect(self._dsn, row_factory=self._row_factory or dict_row)
        except psycopg.OperationalError as error:
            hint = _scrub(self._hint(self._dsn), self._dsn) if self._hint is not None else ""
            raise StoreUnavailableError(
                f"{self._prefix()}could not connect to the database ({describe_database_error(error, self._dsn)}){hint}"
            ) from error
        except psycopg.ProgrammingError:
            # The driver's message quotes part of the string, so it is left out,
            # and so is the error itself.
            raise StoreUnavailableError(f"{self._prefix()}the connection string is not valid") from None
        self.connections_opened += 1
        return conn

    def _usable(self, conn: Any) -> bool:
        """Whether the kept connection can run the next operation."""
        if conn.closed or conn.broken or conn.info.transaction_status != TransactionStatus.IDLE:
            return False
        if time.monotonic() - self._last_used < self.liveness_idle_seconds:
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
        with self._lock:
            self._discard()

    def _failed(self, error: Exception) -> StoreUnavailableError:
        return StoreUnavailableError(
            f"{self._prefix()}the database operation failed ({describe_database_error(error, self._dsn)})"
        )

    def _not_set_up(self, error: Exception) -> StoreNotSetUpError:
        return StoreNotSetUpError(
            f"{self._prefix()}the database is not set up for this version "
            f"({describe_database_error(error, self._dsn)}). Apply the migrations."
        )

    @contextmanager
    def lease(self, row_factory: Any = None) -> Iterator[Any]:
        """The connection, held by one operation until the block ends.

        Params:
        - `row_factory`: rows as this factory makes them during the block, for
          example ``tuple_row``. The lease's own factory is restored afterwards.

        Yields:
        - An autocommit connection with no transaction open. Wrap the work in
          :func:`postgres_roles.acting_as`, or use :meth:`acting_as`.
        """
        not_set_up = not_set_up_errors()
        with self._lock:
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
                # acting_as has already rolled the transaction back, so the
                # connection is idle and can serve the next operation.
                raise self._not_set_up(error) from error
            finally:
                self._depth -= 1
                self._last_used = time.monotonic()
                if self._slot.conn is conn:
                    if conn.closed or conn.broken or conn.info.transaction_status != TransactionStatus.IDLE:
                        self._discard()
                    else:
                        conn.row_factory = previous_factory

    def lease_tuples(self) -> Any:
        """:meth:`lease` with tuple rows."""
        return self.lease(row_factory=tuple_row)

    @contextmanager
    def acting_as(self, role: str, settings: Mapping[str, Any] | None = None) -> Iterator[Any]:
        """Run the block in one transaction on the kept connection, acting as ``role``.

        Params:
        - `role`: the role to act as. Take it from configuration, never from a
          request.
        - `settings`: more ``SET LOCAL`` settings for the same transaction. The
          statement timeout is always set; a ``statement_timeout`` here
          replaces it for this operation.

        One operation costs its own queries plus two round trips: the message
        that opens the transaction, sets the role and settings and checks
        ``current_user``, and ``COMMIT``.
        """
        merged = {**self.settings, **(settings or {})}
        with self.lease() as conn, acting_as(conn, role, merged):
            yield conn
