"""Error and paging contracts shared across Dagents services."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from agents.common.domain.base import DagentsModel


class ErrorEnvelope(DagentsModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = None


class PageResponse(DagentsModel):
    items: list[Any] = Field(default_factory=list)
    next_cursor: str | None = None
    total: int | None = None
