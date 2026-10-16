"""Settings for the Global Monitoring Agent.

Nothing is read from the environment when this module is imported.
``Settings.from_env()`` reads the variables below (after loading
``env/.env.shared`` and ``env/.env.gma`` without overriding exported
variables), and ``Settings(...)`` builds settings in code, for a GMA that an
application runs in its own process.

| Variable | Default | Meaning |
|---|---|---|
| `GMA_APP_NAME`, `GMA_APP_ENV`, `GMA_LOG_LEVEL` | `dagents-gma`, `development`, `info` | Names and logging |
| `GMA_API_HOST`, `GMA_API_PORT` | none | Where uvicorn serves the GMA; the app does not read them |
| `GMA_ENGINE` | `in-process` | `in-process` (the simulator) or `http` (jobs go to the LMAs over their HTTP API) |
| `GMA_SITE_ENDPOINTS` | none | `SITE=URL,...`: each LMA's base address. Only these addresses are called, so an enrolment cannot point the GMA elsewhere |
| `GMA_LMA_TOKENS` | none | `SITE=TOKEN,...`: the coordinator token the GMA sends to each LMA |
| `GMA_ADMIN_TOKEN` | none | The token of the study operator (`admin` scope) |
| `GMA_SITE_TOKENS` | none | `SITE=TOKEN,...`: each LMA's token for its update uploads and the released model (`site` scope, bound to the site) |
| `GMA_DISPATCH_CONCURRENCY` | `3` | How many sites a round's jobs run at at once |
| `GMA_SUMMARY_TTL_SECONDS` | `2` | How long the site directory keeps the sites' summaries |
| `GMA_SUMMARY_TIMEOUT_SECONDS` | `30` | How long the site directory waits for each LMA's summary, audit state and site description; an LMA that takes longer is reported as not reachable |
| `GMA_EXTENSIONS` | none | `module:attribute,...`: extensions for this GMA's own registry. Without it the GMA reads the default registry |
| `GMA_DATABASE_URL` | none | The coordinator database. Never shown in a log or a response |
| `GMA_DATABASE_ROLE` | none | The role every coordinator query runs as. Needed with `GMA_DATABASE_URL` |
| `GMA_MODEL_TABLE` | none | `schema.table` for the model registry; in memory without it |
| `GMA_AUDIT_TABLE`, `GMA_AUDIT_ANCHOR_TABLE` | none | `schema.table` for the release Guard's audit chain and its anchor; in memory without them |
| `GMA_ALLOW_SOURCE_READS` | `1` | `0` makes the source, dataset profile and model job routes answer 403 |
| `GMA_REQUIRE_TOKENS` | `0` | `1` refuses to start without a token |

A GMA with none of these set behaves as it did before they existed: the
simulator engine, no tokens, the default extension registry, everything in
memory and source reads on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from typing import Mapping

from agents.common.env import load_env_files
from agents.common.infrastructure.agent_auth import parse_site_map

#: The engines a GMA can run its rounds on.
ENGINES = ("in-process", "http")

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}


def _flag(environ: Mapping[str, str], name: str, default: bool) -> bool:
    raw = environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ValueError(f"{name} must be 1 or 0 (or true or false)")


def _text(environ: Mapping[str, str], name: str) -> str | None:
    raw = environ.get(name)
    if raw is None or not raw.strip():
        return None
    return raw.strip()


def _number(environ: Mapping[str, str], name: str, default: float, cast: type) -> float:
    raw = environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return cast(raw.strip())
    except ValueError:
        raise ValueError(f"{name} must be a number") from None


@dataclass(frozen=True)
class Settings:
    """What one GMA runs with. ``Settings()`` is a GMA with nothing configured.

    Tokens and the database address are left out of ``repr``.
    """

    app_name: str = "dagents-gma"
    app_env: str = "development"
    api_host: str | None = None
    api_port: int | None = None
    log_level: str = "info"
    engine: str = "in-process"
    site_endpoints: Mapping[str, str] = field(default_factory=dict)
    lma_tokens: Mapping[str, str] = field(default_factory=dict, repr=False)
    admin_token: str | None = field(default=None, repr=False)
    site_tokens: Mapping[str, str] = field(default_factory=dict, repr=False)
    dispatch_concurrency: int = 3
    summary_ttl_seconds: float = 2.0
    summary_timeout_seconds: float = 30.0
    extensions: str = ""
    database_url: str | None = field(default=None, repr=False)
    database_role: str | None = None
    model_table: str | None = None
    audit_table: str | None = None
    audit_anchor_table: str | None = None
    allow_source_reads: bool = True
    require_tokens: bool = False

    def __post_init__(self) -> None:
        if self.engine not in ENGINES:
            raise ValueError(f"GMA_ENGINE must be one of {', '.join(ENGINES)}")
        if isinstance(self.dispatch_concurrency, bool) or not isinstance(self.dispatch_concurrency, int):
            raise ValueError("GMA_DISPATCH_CONCURRENCY must be a whole number")
        if not 1 <= self.dispatch_concurrency <= 64:
            raise ValueError("GMA_DISPATCH_CONCURRENCY must be from 1 to 64")
        if not self.summary_ttl_seconds >= 0:
            raise ValueError("GMA_SUMMARY_TTL_SECONDS must be 0 or more")
        if not self.summary_timeout_seconds > 0:
            raise ValueError("GMA_SUMMARY_TIMEOUT_SECONDS must be more than 0")

    @property
    def site_ids(self) -> list[str]:
        """The sites whose LMA addresses are configured, sorted."""
        return sorted(self.site_endpoints)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        """Read the settings from ``environ``, or from the process environment.

        With no ``environ``, ``env/.env.shared`` and ``env/.env.gma`` are
        loaded first, without overriding variables that are already set.
        Raises ``ValueError`` for a value it cannot use; the message names the
        variable, never a token or an address.
        """
        if environ is None:
            load_env_files("env/.env.shared", "env/.env.gma")
            environ = os.environ
        port = _text(environ, "GMA_API_PORT")
        return cls(
            app_name=_text(environ, "GMA_APP_NAME") or "dagents-gma",
            app_env=_text(environ, "GMA_APP_ENV") or "development",
            api_host=_text(environ, "GMA_API_HOST"),
            api_port=int(port) if port is not None and port.isdigit() else None,
            log_level=_text(environ, "GMA_LOG_LEVEL") or "info",
            engine=(_text(environ, "GMA_ENGINE") or "in-process").lower(),
            site_endpoints=parse_site_map(environ.get("GMA_SITE_ENDPOINTS"), setting="GMA_SITE_ENDPOINTS"),
            lma_tokens=parse_site_map(environ.get("GMA_LMA_TOKENS"), setting="GMA_LMA_TOKENS"),
            admin_token=_text(environ, "GMA_ADMIN_TOKEN"),
            site_tokens=parse_site_map(environ.get("GMA_SITE_TOKENS"), setting="GMA_SITE_TOKENS"),
            dispatch_concurrency=int(_number(environ, "GMA_DISPATCH_CONCURRENCY", 3, int)),
            summary_ttl_seconds=float(_number(environ, "GMA_SUMMARY_TTL_SECONDS", 2.0, float)),
            summary_timeout_seconds=float(_number(environ, "GMA_SUMMARY_TIMEOUT_SECONDS", 30.0, float)),
            extensions=_text(environ, "GMA_EXTENSIONS") or "",
            database_url=_text(environ, "GMA_DATABASE_URL"),
            database_role=_text(environ, "GMA_DATABASE_ROLE"),
            model_table=_text(environ, "GMA_MODEL_TABLE"),
            audit_table=_text(environ, "GMA_AUDIT_TABLE"),
            audit_anchor_table=_text(environ, "GMA_AUDIT_ANCHOR_TABLE"),
            allow_source_reads=_flag(environ, "GMA_ALLOW_SOURCE_READS", True),
            require_tokens=_flag(environ, "GMA_REQUIRE_TOKENS", False),
        )


__all__ = ["ENGINES", "Settings"]
