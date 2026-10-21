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
| `GMA_JOINABLE_SITE_ENDPOINTS` | none | `SITE=URL,...`: sites that may join while the GMA runs, each with the one address the GMA accepts for it (`PUT /api/v1/federation/sites/{site_id}/address`). Needs `GMA_ENGINE=http`; with tokens, each site also needs entries in `GMA_LMA_TOKENS` and `GMA_SITE_TOKENS` |
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
| `GMA_ROUND_TABLE` | none | `schema.table` for the round records; in memory without it. Needs `GMA_MODEL_TABLE` and `GMA_AUDIT_TABLE`, or their stores in `GmaDeps` |
| `GMA_ENROLMENT_TABLE` | none | `schema.table` for the site enrolments; in memory without it |
| `GMA_UPDATE_TABLE` | none | `schema.table` for the clipped updates of running training rounds; in memory without it. Needs `GMA_ENGINE=http`, `GMA_UPDATE_ROLE`, and `GMA_ROUND_TABLE` or a round store in `GmaDeps` that survives a restart |
| `GMA_UPDATE_ROLE` | none | The role that owns the updates table and that every read and write of an update runs as. It must differ from `GMA_DATABASE_ROLE`, so nothing acting as the coordinator role can read an update. Needs `GMA_UPDATE_TABLE` |
| `GMA_UPDATE_MAX_AGE_SECONDS` | `86400` when the update store survives a restart; otherwise no limit | How old, in seconds, a round's oldest stored update may be. After a restart and before each dispatch, the planner's recovery rule closes a `collecting` training round with an older update, unless its candidate is registered, and its updates are deleted. A GMA that keeps its updates in memory runs this check only when the setting is set. Needs `GMA_ENGINE=http`, and `GMA_UPDATE_TABLE` or an update store in `GmaDeps` |
| `GMA_ALLOW_SOURCE_READS` | `1` | `0` makes the source, dataset profile and model job routes answer 403 |
| `GMA_REQUIRE_TOKENS` | `0` | `1` refuses to start without a token |

Every table is created by the operator from the SQL in the GMA's README; the
GMA creates none. All the tables share one kept connection to
`GMA_DATABASE_URL`. :func:`dagents.gma.app_factory.create_app` refuses
settings that cannot work, and a table whose store an application hands the
GMA in ``GmaDeps`` is not used.

A GMA with none of these set behaves as it did before they existed: the
simulator engine, no tokens, the default extension registry, everything in
memory and source reads on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import os
from typing import Mapping

from dagents.common.env import load_env_files
from dagents.common.infrastructure.agent_auth import parse_site_map

#: The engines a GMA can run its rounds on.
ENGINES = ("in-process", "http")

#: The age limit for stored updates when ``GMA_UPDATE_MAX_AGE_SECONDS`` is
#: not set: 24 hours.
DEFAULT_UPDATE_MAX_AGE_SECONDS = 86400.0

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


def _number(environ: Mapping[str, str], name: str, default: float | None, cast: type) -> float | None:
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

    Tokens, the database address and the joinable sites' addresses are left out of ``repr``.
    """

    app_name: str = "dagents-gma"
    app_env: str = "development"
    api_host: str | None = None
    api_port: int | None = None
    log_level: str = "info"
    engine: str = "in-process"
    site_endpoints: Mapping[str, str] = field(default_factory=dict)
    joinable_site_endpoints: Mapping[str, str] = field(default_factory=dict, repr=False)
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
    round_table: str | None = None
    enrolment_table: str | None = None
    update_table: str | None = None
    update_role: str | None = None
    #: ``None`` when ``GMA_UPDATE_MAX_AGE_SECONDS`` is not set; the limit is
    #: then :data:`DEFAULT_UPDATE_MAX_AGE_SECONDS` (see
    #: :attr:`update_age_limit_seconds`).
    update_max_age_seconds: float | None = None
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
        age = self.update_max_age_seconds
        if age is not None and (
            isinstance(age, bool) or not isinstance(age, (int, float)) or not math.isfinite(age) or age <= 0
        ):
            raise ValueError("GMA_UPDATE_MAX_AGE_SECONDS must be a finite number of seconds, more than 0")

    @property
    def site_ids(self) -> list[str]:
        """The sites whose LMA addresses are configured, sorted."""
        return sorted(self.site_endpoints)

    @property
    def update_age_limit_seconds(self) -> float:
        """How old, in seconds, a round's oldest stored update may be.

        ``GMA_UPDATE_MAX_AGE_SECONDS`` when it is set, otherwise
        :data:`DEFAULT_UPDATE_MAX_AGE_SECONDS` (24 hours). Whether the GMA
        applies it depends on its update store:
        :func:`dagents.gma.app_factory.update_age_limit`.
        """
        if self.update_max_age_seconds is None:
            return DEFAULT_UPDATE_MAX_AGE_SECONDS
        return float(self.update_max_age_seconds)

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
            joinable_site_endpoints=parse_site_map(
                environ.get("GMA_JOINABLE_SITE_ENDPOINTS"), setting="GMA_JOINABLE_SITE_ENDPOINTS"
            ),
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
            round_table=_text(environ, "GMA_ROUND_TABLE"),
            enrolment_table=_text(environ, "GMA_ENROLMENT_TABLE"),
            update_table=_text(environ, "GMA_UPDATE_TABLE"),
            update_role=_text(environ, "GMA_UPDATE_ROLE"),
            update_max_age_seconds=_number(environ, "GMA_UPDATE_MAX_AGE_SECONDS", None, float),
            allow_source_reads=_flag(environ, "GMA_ALLOW_SOURCE_READS", True),
            require_tokens=_flag(environ, "GMA_REQUIRE_TOKENS", False),
        )


__all__ = ["DEFAULT_UPDATE_MAX_AGE_SECONDS", "ENGINES", "Settings"]
