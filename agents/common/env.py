"""Minimal dotenv loader shared across Dagents services."""

from __future__ import annotations

from pathlib import Path
import os


REPO_ROOT = Path(__file__).resolve().parents[2]


def load_env_files(*relative_paths: str) -> None:
    """Load env files without overriding already exported variables."""

    candidates = [REPO_ROOT / path for path in relative_paths]
    for path in candidates:
        if not path.exists() or not path.is_file():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if not key:
                continue
            if value and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1]
            os.environ.setdefault(key, value)
