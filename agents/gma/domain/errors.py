"""Errors only the GMA raises, with the JSON ``code`` and HTTP status it answers with.

| Error | `code` | HTTP status |
|---|---|---|
| `SourceReadsDisabled` | `forbidden` | 403 |
| `InvalidReleaseRequest` | `invalid_request` | 422 |
| `UnknownConditionPack` | `not_found` | 404 |
| `UnknownRound` | `not_found` | 404 |

Like the errors in :mod:`agents.common.domain.errors`, a message never holds a
connection string, a host or a credential.
"""

from __future__ import annotations

from typing import ClassVar

from agents.common.domain.errors import DagentsError


class SourceReadsDisabled(DagentsError, PermissionError):
    """The GMA was started with ``GMA_ALLOW_SOURCE_READS=0``, so it reads no source."""

    code: ClassVar[str] = "forbidden"
    http_status: ClassVar[int] = 403


class InvalidReleaseRequest(DagentsError, ValueError):
    """A release evaluation mixes the two flows or lacks what its flow needs."""

    code: ClassVar[str] = "invalid_request"
    http_status: ClassVar[int] = 422


class UnknownConditionPack(DagentsError, LookupError):
    """No registered extension contributes the condition pack a request names."""

    code: ClassVar[str] = "not_found"
    http_status: ClassVar[int] = 404


class UnknownRound(DagentsError, LookupError):
    """A request names a round the GMA does not hold, other than the round in its path."""

    code: ClassVar[str] = "not_found"
    http_status: ClassVar[int] = 404


__all__ = ["InvalidReleaseRequest", "SourceReadsDisabled", "UnknownConditionPack", "UnknownRound"]
