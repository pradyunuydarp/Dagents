"""Error and paging contracts shared across Dagents services.

The exception classes below are raised by framework code and answered by the
LMA and GMA routes. Each one names the JSON ``code`` and the HTTP status an
agent answers with, so the two agents and the agent client map an error the
same way:

| Error | `code` | HTTP status |
|---|---|---|
| `StoreUnavailableError` | `store_unavailable` | 503 |
| `StoreNotSetUpError` | `store_not_set_up` | 503 |
| `RoundConflict` | `conflict` | 409 |
| `InvalidRoundRecord` | `invalid_request` | 422 |
| `NotSelected` | `forbidden` | 403 |
| `AggregationRefused` | `aggregation_refused` | 409 |
| `ClassificationConflict` | `conflict` | 409 |
| `ModelVerificationError` | `conflict` | 409 |

A message must never hold a connection string, a host or a credential: agents
put it in the response body's ``detail``.
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import Field

from dagents.common.domain.base import DagentsModel


class ErrorEnvelope(DagentsModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = None


class PageResponse(DagentsModel):
    items: list[Any] = Field(default_factory=list)
    next_cursor: str | None = None
    total: int | None = None


class DagentsError(Exception):
    """Base class for the errors an agent answers with a JSON ``code``.

    ``code`` is the value of the ``code`` key in the agent's error body, and
    ``http_status`` is the status it answers with.
    """

    code: ClassVar[str] = "error"
    http_status: ClassVar[int] = 500


class StoreUnavailableError(DagentsError, RuntimeError):
    """A site's or the coordinator's store could not be reached, or an operation on it failed."""

    code: ClassVar[str] = "store_unavailable"
    http_status: ClassVar[int] = 503


class StoreNotSetUpError(StoreUnavailableError):
    """The store lacks a schema, table, column, role or grant this version needs.

    The migrations have not been applied, or not all of them.
    """

    code: ClassVar[str] = "store_not_set_up"
    http_status: ClassVar[int] = 503


class RoundConflict(DagentsError, RuntimeError):
    """The round cannot take this change in its current state.

    Raised for a round id that is already stored, a result for a round that
    was aggregated or closed, and an update that does not fit the round.
    """

    code: ClassVar[str] = "conflict"
    http_status: ClassVar[int] = 409


class InvalidRoundRecord(DagentsError, ValueError):
    """A round record holds a value no round store can keep, such as NaN or an infinite number.

    Round stores keep records as JSON, which has no form for either, so every
    round store refuses such a record before it writes anything. It is a
    ``ValueError``, so a caller that catches bad input that way keeps working.
    """

    code: ClassVar[str] = "invalid_request"
    http_status: ClassVar[int] = 422


class NotSelected(DagentsError, PermissionError):
    """A site that the round's plan did not select tried to take part in it."""

    code: ClassVar[str] = "forbidden"
    http_status: ClassVar[int] = 403


class AggregationRefused(DagentsError, PermissionError):
    """Aggregation may not run: the planner did not permit it, or an update failed a check.

    It is a ``PermissionError``, so code that already answers a refused
    aggregation with 409 keeps working.
    """

    code: ClassVar[str] = "aggregation_refused"
    http_status: ClassVar[int] = 409


class ClassificationConflict(DagentsError, ValueError):
    """A classification differs from the one registered under its id, or no classification has that id.

    Raised when a runtime registration would replace an extension's
    classification, and, in strict mode, when a request carries a
    classification that is not the registered one.
    """

    code: ClassVar[str] = "conflict"
    http_status: ClassVar[int] = 409


class ModelVerificationError(DagentsError, ValueError):
    """Model parameters do not verify: unknown names, values that are not finite, or a digest that differs.

    A model kind raises it from ``parameter_digest``. It is a ``ValueError``,
    so a caller that catches bad input that way keeps working.
    """

    code: ClassVar[str] = "conflict"
    http_status: ClassVar[int] = 409
