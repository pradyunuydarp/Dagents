"""Runtime settings for the healthcare stroke-triage demo.

Every URL and port is environment-driven, matching the framework's convention.
Nothing is hardcoded, and the defaults point at a local run.

The Dagents agents (three LMAs and one GMA) run inside this process by default
(``HEALTHCARE_DEMO_AGENTS=in-process``): the app builds them and calls them
through their HTTP routes without a socket, with tokens it generates when it
first builds them.
With ``HEALTHCARE_DEMO_AGENTS=network`` the app calls agents that run as their
own processes. Their addresses and tokens then come from the environment:

- ``HEALTHCARE_DEMO_GMA_URL`` and ``HEALTHCARE_DEMO_GMA_TOKEN``: the GMA and
  its ``admin`` token;
- ``HEALTHCARE_DEMO_LMA_<SITE>_URL`` and ``HEALTHCARE_DEMO_LMA_<SITE>_TOKEN``
  for each hospital (``uk-134`` becomes ``UK_134``): its LMA and that LMA's
  ``site`` token.

A network-mode run started with ``--deploy`` can add a hospital while the demo
runs. ``HEALTHCARE_DEMO_DEPLOYER`` and the ``HEALTHCARE_DEMO_DEPLOY_*``
settings below say how; the joining hospital's address and token use the same
two names as the others.

No token or address is ever logged or returned.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Mapping
from urllib.parse import urlparse

from agents.common.env import load_env_files

load_env_files("env/.env.shared", "env/.env.healthcare-demo", "apps/healthcare-demo/env/.env.healthcare-demo")


#: Characters a paste wraps a URL in — brackets and markdown punctuation.
#:
#: `[` and `]` are not in the list: an IPv6 origin can end in
#: one, as in `http://[::1]:8080`, and stripping it would corrupt a correct
#: value in order to tidy a wrong one.
_PASTE_NOISE = " \t<>()'\"`,;"

#: A host, with an optional port: a name, an IPv4 address, or a bracketed IPv6
#: literal. Used to decide whether an entry parsed into something a browser
#: could send, rather than into something that only has a `://`.
_HOST = re.compile(r"^(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+)(:\d+)?$")


def normalize_origins(configured: str) -> list[str]:
    """Turn a comma-separated setting into origins a browser can match.

    Params:
    - `configured`: the raw `HEALTHCARE_DEMO_CORS_ORIGINS` value.

    Returns:
    - Bare origins, in the order given, without duplicates. `*` is passed
      through untouched, since it is a wildcard rather than a URL.

    Three paste artifacts are repaired, because each produces an entry that
    cannot match any origin a browser will ever send, and so means "block
    everything" rather than anything a deployment could have intended: a path or
    trailing slash (`https://x.github.io/Dagents/`), wrapping punctuation
    (`(https://x.github.io)` — the real one, which cost an afternoon), and an
    uppercased host, since browsers send hosts lowercased and the comparison is
    exact.

    Repairing configuration is not free — a silent fix can hide a mistake — so
    the balance is struck twice over: an entry that does not parse into a
    plausible host is kept exactly as written, and the service reports the list
    it ended up with at `/api/v1/framework/status`. Nothing is corrected
    invisibly.
    """
    origins: list[str] = []
    for entry in configured.split(","):
        entry = entry.strip().strip(_PASTE_NOISE)
        if not entry:
            continue
        if entry == "*":
            origins.append(entry)
            continue
        parsed = urlparse(entry)
        if parsed.scheme and _HOST.match(parsed.netloc):
            entry = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
        if entry not in origins:
            origins.append(entry)
    return origins


def _on_render() -> bool:
    """Render sets ``RENDER`` in every service's environment."""
    return bool(os.getenv("RENDER"))


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


#: Where the agents run: in this process, or as their own processes.
AGENT_MODES = ("in-process", "network")


def _site_key(site_id: str) -> str:
    """``uk-134`` as it appears in a variable name: ``UK_134``."""
    return site_id.upper().replace("-", "_")


def lma_url_variable(site_id: str) -> str:
    """The variable that holds one hospital's LMA address in network mode."""
    return f"HEALTHCARE_DEMO_LMA_{_site_key(site_id)}_URL"


def lma_token_variable(site_id: str) -> str:
    """The variable that holds one hospital's LMA ``site`` token in network mode."""
    return f"HEALTHCARE_DEMO_LMA_{_site_key(site_id)}_TOKEN"


def site_value(variable: str, environ: Mapping[str, str] | None = None) -> str:
    """A per-hospital setting, stripped, or ``""`` when it is not set."""
    env = os.environ if environ is None else environ
    return (env.get(variable) or "").strip()


@dataclass(frozen=True, slots=True)
class Settings:
    """Environment-backed settings for the demo backend."""

    app_name: str = os.getenv("HEALTHCARE_DEMO_APP_NAME", "dagents-healthcare-stroke-demo")
    app_env: str = os.getenv("HEALTHCARE_DEMO_APP_ENV", "development")
    api_host: str = os.getenv("HEALTHCARE_DEMO_API_HOST", "0.0.0.0")
    api_port: int = int(os.getenv("HEALTHCARE_DEMO_API_PORT", "8080"))

    #: Browser origins allowed to call this API, comma-separated. Empty means
    #: any origin, which is right for a local run and for a public read-only
    #: demo that holds no credentials — but a deployment should name the
    #: frontend it serves, so an unexpected origin is a question rather than a
    #: default.
    cors_origins: str = os.getenv("HEALTHCARE_DEMO_CORS_ORIGINS", "")

    #: Postgres connection string for the hospitals' stores and the
    #: coordinator's registry (schema per hospital, see
    #: ``supabase/migrations/0002_site_isolation.sql``). Empty means each
    #: hospital keeps its records in memory, loaded from the committed IST
    #: extract. When it is set and the database cannot be read, the endpoints
    #: that need records fail; the app never falls back to the extract.
    #:
    #: A deployment that gives each hospital its own login can also set
    #: ``HEALTHCARE_DEMO_SITE_<SITE>_DATABASE_URL`` (for example
    #: ``HEALTHCARE_DEMO_SITE_UK_134_DATABASE_URL``). The site store reads
    #: those itself; see ``app.services.site_store.site_database_url``.
    database_url: str = os.getenv("HEALTHCARE_DEMO_DATABASE_URL", "")

    #: Most records each hospital's data-entry form may add. The public demo
    #: has no accounts, so this keeps one visitor from filling a store.
    max_entered_per_site: int = int(os.getenv("HEALTHCARE_DEMO_MAX_ENTERED_PER_SITE", "300"))

    #: Sites below which secure aggregation must not reveal a contribution.
    secure_aggregation_threshold: int = int(os.getenv("HEALTHCARE_DEMO_SECURE_THRESHOLD", "3"))

    #: Requests each client may make per minute, per group of POST endpoints.
    #: The public demo has no accounts, so these keep one visitor from using
    #: up the server. ``0`` turns a limit off (local runs and the recording
    #: script do this). A client may send this many at once, then one more
    #: each ``60 / limit`` seconds.
    rate_limit_entries_per_minute: int = int(os.getenv("HEALTHCARE_DEMO_RATE_LIMIT_ENTRIES", "30"))
    rate_limit_probes_per_minute: int = int(os.getenv("HEALTHCARE_DEMO_RATE_LIMIT_PROBES", "60"))
    rate_limit_pilots_per_minute: int = int(os.getenv("HEALTHCARE_DEMO_RATE_LIMIT_PILOTS", "3"))
    rate_limit_approvals_per_minute: int = int(os.getenv("HEALTHCARE_DEMO_RATE_LIMIT_APPROVALS", "10"))

    #: Caps on actions in any 60 minutes, whoever asks, counted in this
    #: process. Each Guard probe adds a record to a hospital's audit chain and
    #: each pilot adds model versions and audit records, all kept in the
    #: database, so these bound how fast the public demo's tables grow. Beyond
    #: a cap the API answers 429 with Retry-After. ``0`` turns a cap off.
    probes_per_site_per_hour: int = field(
        default_factory=lambda: int(os.getenv("HEALTHCARE_DEMO_PROBES_PER_SITE_PER_HOUR", "600"))
    )
    pilots_per_hour: int = field(default_factory=lambda: int(os.getenv("HEALTHCARE_DEMO_PILOTS_PER_HOUR", "20")))

    #: Whether the rate limits read the client's address from
    #: ``X-Forwarded-For`` instead of using the TCP peer. Turn it on only behind
    #: proxies that append to the header. It is on by default when ``RENDER``
    #: is set: behind Render's proxy the TCP peer is the proxy, so every
    #: visitor would share one count.
    trust_forwarded_for: bool = field(
        default_factory=lambda: _bool_env("HEALTHCARE_DEMO_TRUST_FORWARDED_FOR", _on_render())
    )

    #: How many proxies in front of the app append to ``X-Forwarded-For``. The
    #: client is the entry this many places from the right. Entries further
    #: left were sent by the client, which can write anything there, so the
    #: leftmost entry is never used. A published test of Render's edge
    #: (github.com/johnbeynon/header-test) shows three appended entries: the
    #: client, a Cloudflare address and an internal one. So the default is 3
    #: when ``RENDER`` is set and 1 otherwise. ``/api/v1/framework/status``
    #: reports how many entries each request arrives with, to check the value.
    forwarded_for_hops: int = field(
        default_factory=lambda: int(os.getenv("HEALTHCARE_DEMO_FORWARDED_FOR_HOPS", "3" if _on_render() else "1"))
    )

    #: Largest request body a POST may send, in bytes.
    max_body_bytes: int = int(os.getenv("HEALTHCARE_DEMO_MAX_BODY_BYTES", str(64 * 1024)))

    #: Seconds to wait for an agent's health check before ``/api/v1/framework/status``
    #: reports it unavailable.
    request_timeout_seconds: float = float(os.getenv("HEALTHCARE_DEMO_REQUEST_TIMEOUT_SECONDS", "2.0"))

    #: Where the Dagents agents run: ``in-process`` (the default; the app builds
    #: three LMAs and a GMA and calls them without a socket) or ``network``
    #: (the app calls agents that run as their own processes; it needs
    #: ``HEALTHCARE_DEMO_DATABASE_URL``, because separate processes cannot share
    #: an in-memory store).
    agents_mode: str = os.getenv("HEALTHCARE_DEMO_AGENTS", "in-process")

    #: Network mode: the GMA's base address and its ``admin`` token. Set them in
    #: the environment, never in a committed file. Each hospital's LMA has
    #: ``HEALTHCARE_DEMO_LMA_<SITE>_URL`` and ``HEALTHCARE_DEMO_LMA_<SITE>_TOKEN``
    #: (see :func:`lma_url_variable` and :func:`lma_token_variable`).
    gma_url: str = os.getenv("HEALTHCARE_DEMO_GMA_URL", "")
    gma_token: str = field(default_factory=lambda: os.getenv("HEALTHCARE_DEMO_GMA_TOKEN", ""), repr=False)

    #: Most public requests the app works on at once. A request waits up to
    #: ``in_flight_wait_seconds`` for a slot, then gets 503 "busy". In process,
    #: each request can hold several of the agents' threads while agents call
    #: each other, so the cap keeps them from running out.
    max_in_flight: int = int(os.getenv("HEALTHCARE_DEMO_MAX_IN_FLIGHT", "12"))
    in_flight_wait_seconds: float = float(os.getenv("HEALTHCARE_DEMO_IN_FLIGHT_WAIT_SECONDS", "20"))

    #: In process: how many of the agents' handlers may run at once, across
    #: the three LMAs and the GMA. It must be at least 4 x ``max_in_flight`` + 8
    #: (see :meth:`check_agent_capacity`).
    agent_thread_tokens: int = int(os.getenv("HEALTHCARE_DEMO_AGENT_THREAD_TOKENS", "64"))

    #: In process: how many hospitals the GMA sends a round's jobs to at once.
    dispatch_concurrency: int = int(os.getenv("HEALTHCARE_DEMO_DISPATCH_CONCURRENCY", "3"))

    # --- Adding a hospital while the demo runs (app.services.deployment) ---------------

    #: How a hospital that joins while the demo runs is started: ``off`` (the
    #: default), ``process`` (a local process) or ``compose`` (a container,
    #: with ``docker compose``). Read stripped and in lower case. The app
    #: refuses to start with it on unless the run can work and only this
    #: machine can ask (``app.services.deployment.check_deployer``). The
    #: joining hospital's LMA address and the app's ``site`` token for it are
    #: ``HEALTHCARE_DEMO_LMA_<SITE>_URL`` and ``HEALTHCARE_DEMO_LMA_<SITE>_TOKEN``,
    #: read only with this on.
    deployer: str = field(default_factory=lambda: os.getenv("HEALTHCARE_DEMO_DEPLOYER", "off").strip().lower())

    #: The Dagents core service, which renders the new LMA's Compose file.
    core_service_url: str = os.getenv("HEALTHCARE_DEMO_CORE_SERVICE_URL", "")

    #: Seconds to wait for a new LMA to answer its health check.
    deploy_timeout_seconds: float = float(os.getenv("HEALTHCARE_DEMO_DEPLOY_TIMEOUT_SECONDS", "90"))

    #: Where the deployer writes its logs and pid files (process) or the
    #: rendered files (compose). Empty means a new temporary folder.
    deploy_dir: str = os.getenv("HEALTHCARE_DEMO_DEPLOY_DIR", "")

    #: The folder that holds each new LMA's env file, ``lma-<site>.env``, with
    #: its tokens and its database address. Empty means ``deploy_dir``.
    deploy_env_dir: str = os.getenv("HEALTHCARE_DEMO_DEPLOY_ENV_DIR", "")

    #: Compose mode: the image a new LMA runs, and the running demo's network it joins.
    deploy_image: str = os.getenv("HEALTHCARE_DEMO_DEPLOY_IMAGE", "dagents/healthcare-demo-backend:local")
    deploy_network: str = os.getenv("HEALTHCARE_DEMO_DEPLOY_NETWORK", "healthcare-demo_default")

    def check_agent_capacity(self) -> None:
        """Refuse settings that could leave the in-process agents without threads.

        One public request can hold up to four agent handlers at once (the GMA
        dispatching, an LMA running a job, the LMA's upload to the GMA, and a
        nested read), so the agents need at least 4 x ``max_in_flight`` + 8
        thread tokens. Raises ``ValueError`` naming the settings.
        """
        if self.agents_mode not in AGENT_MODES:
            raise ValueError(f"HEALTHCARE_DEMO_AGENTS must be one of {', '.join(AGENT_MODES)}")
        if self.max_in_flight < 1:
            raise ValueError("HEALTHCARE_DEMO_MAX_IN_FLIGHT must be at least 1")
        if not 1 <= self.dispatch_concurrency <= 64:
            raise ValueError("HEALTHCARE_DEMO_DISPATCH_CONCURRENCY must be from 1 to 64")
        needed = 4 * self.max_in_flight + 8
        if self.agents_mode == "in-process" and self.agent_thread_tokens < needed:
            raise ValueError(
                f"HEALTHCARE_DEMO_AGENT_THREAD_TOKENS must be at least {needed} "
                "(4 x HEALTHCARE_DEMO_MAX_IN_FLIGHT + 8)"
            )

    def client_proxy_hops(self) -> int:
        """The proxies the rate limits trust: ``forwarded_for_hops``, or 0 when the header is not trusted."""
        return self.forwarded_for_hops if self.trust_forwarded_for else 0

    def allowed_origins(self) -> list[str]:
        """The CORS origin list, falling back to any origin when unset.

        Each entry is reduced to a bare origin — scheme, host, and port if one
        is given — because that is the only thing a browser ever sends in an
        `Origin` header. An entry written as a page URL,
        `https://example.github.io/Dagents`, cannot match anything, so a
        service configured that way blocks every request from the site it
        was configured for, and the page looks down rather than refused. The
        path is dropped rather than honoured: in an origin allowlist it has no
        meaning to honour.
        """
        return normalize_origins(self.cors_origins) or ["*"]

    def as_health_payload(self) -> dict[str, str]:
        """Liveness. ``data`` names the dataset the records come from.

        It reads no store, so the health check answers while the database is
        down. ``/api/v1/framework/status`` reports where the records are stored.
        """
        return {
            "status": "ok",
            "service": self.app_name,
            "environment": self.app_env,
            "data": "ist-1-deidentified",
        }


settings = Settings()
