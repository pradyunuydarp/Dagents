"""Errors the LMA's site and job services raise, with the JSON ``code`` and status the LMA answers with.

Each is a :class:`agents.common.domain.errors.DagentsError`, so the LMA's error
handler answers ``{detail, code}`` with the class's ``http_status``. A message
never holds a host, an address, a token or a connection string.

| Error | `code` | HTTP status |
|---|---|---|
| `SiteNotConfigured` | `not_found` | 404 |
| `NoReleasedModel` | `not_found` | 404 |
| `LabelFieldMissing` | `conflict` | 409 |
| `GmaUnavailable` | `agent_unavailable` | 503 |
| `GmaRefused` | `bad_gateway` | 502 |
"""

from __future__ import annotations

from typing import ClassVar

from agents.common.domain.errors import DagentsError


class SiteNotConfigured(DagentsError, LookupError):
    """The LMA was started without a site, so it has no site routes to serve."""

    code: ClassVar[str] = "not_found"
    http_status: ClassVar[int] = 404


class NoReleasedModel(DagentsError, LookupError):
    """The GMA has no released model for the site to use."""

    code: ClassVar[str] = "not_found"
    http_status: ClassVar[int] = 404


class LabelFieldMissing(DagentsError, ValueError):
    """The condition pack names no label field, so the site cannot evaluate or train a model."""

    code: ClassVar[str] = "conflict"
    http_status: ClassVar[int] = 409


class GmaUnavailable(DagentsError, RuntimeError):
    """The GMA did not answer, or this LMA has no GMA configured."""

    code: ClassVar[str] = "agent_unavailable"
    http_status: ClassVar[int] = 503


class GmaRefused(DagentsError, RuntimeError):
    """The GMA answered a call from the LMA with an error."""

    code: ClassVar[str] = "bad_gateway"
    http_status: ClassVar[int] = 502
