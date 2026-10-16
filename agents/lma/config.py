"""Configuration for the Local Monitoring Agent.

Nothing here reads the environment when the module is imported.
:meth:`Settings.from_env` reads it when it is called, so a process can start
several LMAs with different settings (one per site), and tests can build
settings without touching the environment.

An LMA without ``LMA_SITE_ID`` behaves as before sites existed: it serves the
generic profiling, model, source and governance routes, and its site and
federation job routes answer 404. With ``LMA_SITE_ID`` and
``LMA_CONDITION_ID`` it serves one site: it reads that site's records through
a site source, plans against the condition pack's classification, and takes
federated jobs from the GMA.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from typing import Mapping

from agents.common.env import load_env_files

#: The env files ``from_env`` loads, without replacing variables already set.
ENV_FILES = ("env/.env.shared", "env/.env.lma")

#: How long the LMA keeps the records it read before it asks the source for its
#: version again, in milliseconds.
DEFAULT_SOURCE_MAX_STALENESS_MS = 30_000

#: The statement timeout of each database operation the LMA runs, in milliseconds.
DEFAULT_STATEMENT_TIMEOUT_MS = 15_000

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"", "0", "false", "no", "off"}


def _text(environ: Mapping[str, str], name: str) -> str | None:
    value = environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _number(environ: Mapping[str, str], name: str, default: int | None) -> int | None:
    value = _text(environ, name)
    if value is None:
        return default
    try:
        number = int(value)
    except ValueError:
        raise ValueError(f"{name} must be a whole number") from None
    if number < 0:
        raise ValueError(f"{name} must not be negative")
    return number


def _flag(environ: Mapping[str, str], name: str) -> bool:
    value = (environ.get(name) or "").strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ValueError(f"{name} must be 1 or 0")


def _names(environ: Mapping[str, str], name: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in (environ.get(name) or "").split(",") if part.strip())


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything an LMA is started with.

    Tokens and the database URL are left out of ``repr``, so logging the
    settings shows neither.

    Site settings (used only when ``site_id`` is set):

    - ``site_id``, ``condition_id``: the site and the condition pack it serves.
      The pack comes from an extension named in ``extensions``.
    - ``study_id``, ``reporting_purpose``: who asks for the site's summary and
      why. The label-rate decision runs with them. They default to the
      condition id and the pack's first approved purpose.
    - ``profile_fields``: the fields the running profile tracks. Empty tracks
      every field of the pack's feature contract.
    - ``capabilities``, ``policy_version``: what the site's registration says.
    - ``site_source``: the name of a site source factory an extension
      contributes. The LMA reads the site's records through it.
    - ``database_url``, ``site_role``: the site's database and the role every
      operation acts as. The site source, the audit chain and the job log use
      one kept connection.
    - ``audit_table``, ``audit_anchor_table``, ``job_log_table``: where the
      audit chain and the job log are kept in that database. Without them the
      LMA keeps both in memory.
    - ``gma_endpoint``, ``gma_token``: where the GMA is, and the GMA ``site``
      token bound to this site. The LMA reads the released model there and
      sends training updates there.
    - ``site_token``, ``coordinator_token``: the tokens this LMA accepts, for
      the ``site`` and ``coordinator`` scopes.
    """

    app_name: str = "dagents-lma"
    app_env: str = "development"
    api_host: str | None = None
    api_port: int | None = None
    log_level: str = "info"
    gma_endpoint: str | None = None
    gma_token: str | None = field(default=None, repr=False)
    site_id: str | None = None
    condition_id: str | None = None
    study_id: str | None = None
    reporting_purpose: str | None = None
    profile_fields: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    policy_version: str | None = None
    extensions: str = ""
    site_source: str | None = None
    database_url: str | None = field(default=None, repr=False)
    site_role: str | None = None
    audit_table: str | None = None
    audit_anchor_table: str | None = None
    job_log_table: str | None = None
    site_token: str | None = field(default=None, repr=False)
    coordinator_token: str | None = field(default=None, repr=False)
    source_max_staleness_ms: int = DEFAULT_SOURCE_MAX_STALENESS_MS
    statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS
    require_tokens: bool = False

    @property
    def site_configured(self) -> bool:
        """Whether these settings start the LMA for one site."""
        return self.site_id is not None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        """Read the settings from ``LMA_*`` variables.

        Params:
        - `environ`: the variables to read. ``None`` loads ``env/.env.shared``
          and ``env/.env.lma`` (without replacing variables already set) and
          reads ``os.environ``.

        Raises ``ValueError`` naming the variable when a number or a flag
        cannot be read. The message never repeats the value.
        """
        if environ is None:
            load_env_files(*ENV_FILES)
            environ = os.environ
        return cls(
            app_name=_text(environ, "LMA_APP_NAME") or "dagents-lma",
            app_env=_text(environ, "LMA_APP_ENV") or "development",
            api_host=_text(environ, "LMA_API_HOST"),
            api_port=_number(environ, "LMA_API_PORT", None),
            log_level=_text(environ, "LMA_LOG_LEVEL") or "info",
            gma_endpoint=_text(environ, "LMA_GMA_ENDPOINT"),
            gma_token=_text(environ, "LMA_GMA_TOKEN"),
            site_id=_text(environ, "LMA_SITE_ID"),
            condition_id=_text(environ, "LMA_CONDITION_ID"),
            study_id=_text(environ, "LMA_STUDY_ID"),
            reporting_purpose=_text(environ, "LMA_REPORTING_PURPOSE"),
            profile_fields=_names(environ, "LMA_PROFILE_FIELDS"),
            capabilities=_names(environ, "LMA_CAPABILITIES"),
            policy_version=_text(environ, "LMA_POLICY_VERSION"),
            extensions=_text(environ, "LMA_EXTENSIONS") or "",
            site_source=_text(environ, "LMA_SITE_SOURCE"),
            database_url=_text(environ, "LMA_DATABASE_URL"),
            site_role=_text(environ, "LMA_SITE_ROLE"),
            audit_table=_text(environ, "LMA_AUDIT_TABLE"),
            audit_anchor_table=_text(environ, "LMA_AUDIT_ANCHOR_TABLE"),
            job_log_table=_text(environ, "LMA_JOB_LOG_TABLE"),
            site_token=_text(environ, "LMA_SITE_TOKEN"),
            coordinator_token=_text(environ, "LMA_COORDINATOR_TOKEN"),
            source_max_staleness_ms=_number(
                environ, "LMA_SOURCE_MAX_STALENESS_MS", DEFAULT_SOURCE_MAX_STALENESS_MS
            ),
            statement_timeout_ms=_number(environ, "LMA_STATEMENT_TIMEOUT_MS", DEFAULT_STATEMENT_TIMEOUT_MS),
            require_tokens=_flag(environ, "LMA_REQUIRE_TOKENS"),
        )
