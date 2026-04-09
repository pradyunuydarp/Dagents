"""Shared Pydantic base types for Dagents contracts."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class DagentsModel(BaseModel):
    """Shared base model with relaxed protected namespace handling."""

    model_config = ConfigDict(protected_namespaces=(), populate_by_name=True, serialize_by_alias=True)
