"""Schema formatting helpers for NL2SQL prompts and Dagents validation."""

from __future__ import annotations

import re

from app.models import SchemaTable


def normalize_identifier(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", value.strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    if not cleaned:
        return "unnamed"
    if cleaned[0].isdigit():
        return f"_{cleaned}"
    return cleaned


def ddl_from_tables(tables: list[SchemaTable]) -> str:
    parts: list[str] = []
    for table in tables:
        table_name = normalize_identifier(table.name)
        columns = [
            f"{normalize_identifier(column.name)} {column.dtype.upper() or 'TEXT'}"
            for column in table.columns
        ]
        parts.append(f"CREATE TABLE {table_name} ({', '.join(columns)})")
    return " ".join(parts)


def schema_records(tables: list[SchemaTable]) -> list[dict[str, str]]:
    return [
        {
            "table_name": normalize_identifier(table.name),
            "column_name": normalize_identifier(column.name),
            "dtype": column.dtype.upper() or "TEXT",
        }
        for table in tables
        for column in table.columns
    ]


def schema_contract() -> dict[str, object]:
    return {
        "required_fields": [
            {"name": "table_name", "type": "string"},
            {"name": "column_name", "type": "string"},
            {"name": "dtype", "type": "string"},
        ],
        "optional_fields": [],
        "allow_extra_fields": False,
    }
