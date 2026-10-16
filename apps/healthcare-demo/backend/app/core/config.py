"""Runtime settings for the healthcare stroke-triage demo.

Every URL and port is environment-driven, matching the framework's convention.
Nothing is hardcoded, and the defaults point at a local run.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
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

    #: Framework services, reached over HTTP when they are running.
    core_service_url: str = os.getenv("CORE_SERVICE_PUBLIC_URL", "http://127.0.0.1:8040")
    pipeline_service_url: str = os.getenv("PIPELINE_SERVICE_PUBLIC_URL", "http://127.0.0.1:8030")
    model_service_url: str = os.getenv("MODEL_SERVICE_PUBLIC_URL", "http://127.0.0.1:8000")
    lma_url: str = os.getenv("LMA_PUBLIC_URL", "http://127.0.0.1:8010")
    gma_url: str = os.getenv("GMA_PUBLIC_URL", "http://127.0.0.1:8020")

    #: Seconds to wait for a framework service's status before reporting it unavailable.
    request_timeout_seconds: float = float(os.getenv("HEALTHCARE_DEMO_REQUEST_TIMEOUT_SECONDS", "2.0"))

    #: Whether the app registers its Dagents extension on startup.
    register_extension: bool = _bool_env("HEALTHCARE_DEMO_REGISTER_EXTENSION", True)

    #: Which part of the demo this process runs:
    #:
    #: - ``combined``: every hospital and the coordinator in this one process,
    #:   with the framework composed in process. The hosted deployment and the
    #:   test suite use it.
    #: - ``lma``: one hospital, ``HEALTHCARE_DEMO_SITE_ID``. It serves that
    #:   hospital's pages and runs the round jobs the Dagents GMA sends it.
    #: - ``app``: the API the frontend calls. It runs the study through the
    #:   Dagents GMA service's API (``GMA_PUBLIC_URL``), keeps the model
    #:   registry and the update store, and forwards each hospital's pages to
    #:   that hospital's LMA (``HEALTHCARE_DEMO_LMA_URLS``). It never reads a
    #:   hospital's records. The LMAs reach the registry and the update store
    #:   on a second listener (``internal_host`` and ``internal_port``).
    role: str = field(default_factory=lambda: os.getenv("HEALTHCARE_DEMO_ROLE", "combined").strip().lower())

    #: The hospital an ``lma`` process runs, such as ``uk-134``.
    site_id: str = field(default_factory=lambda: os.getenv("HEALTHCARE_DEMO_SITE_ID", "").strip())

    #: Where an ``lma`` process reaches the ``app`` process's internal
    #: listener: it loads the models a round names from there and sends its
    #: bounded updates there.
    coordinator_url: str = field(
        default_factory=lambda: os.getenv("HEALTHCARE_DEMO_COORDINATOR_URL", "http://127.0.0.1:8081")
    )

    #: The address and port of the ``app`` process's internal listener, which
    #: serves only the routes the LMAs call (the model registry and the update
    #: store). They have no authentication, so they are never on the public
    #: port. Bind an address only the LMAs reach: 127.0.0.1 on one machine, or
    #: 0.0.0.0 in a container whose port is not published.
    internal_host: str = field(default_factory=lambda: os.getenv("HEALTHCARE_DEMO_INTERNAL_HOST", "127.0.0.1"))
    internal_port: int = field(default_factory=lambda: int(os.getenv("HEALTHCARE_DEMO_INTERNAL_PORT", "8081")))

    #: Where each hospital's LMA is, for the ``app`` process, in display
    #: order: ``uk-134=http://127.0.0.1:8082,it-002=...``. The Dagents GMA
    #: reaches the same addresses through its own ``GMA_FEDERATION_SITES``.
    lma_urls: str = field(
        default_factory=lambda: os.getenv(
            "HEALTHCARE_DEMO_LMA_URLS",
            "uk-134=http://127.0.0.1:8082,it-002=http://127.0.0.1:8083,ch-245=http://127.0.0.1:8084",
        )
    )

    #: Seconds the Dagents GMA waits for one call to a hospital, its
    #: ``GMA_FEDERATION_TIMEOUT_SECONDS``. Give both the same value. The
    #: ``app`` process waits this long for each call to the GMA, and
    #: ``2 * hospitals + 1`` times this for a round's dispatch, because the GMA
    #: offers and runs the job at each hospital in turn. A hospital waits a
    #: quarter of it for each call to the ``app`` process, so it answers the
    #: GMA in time (see ``app.services.roles``).
    federation_timeout_seconds: float = field(
        default_factory=lambda: float(os.getenv("HEALTHCARE_DEMO_FEDERATION_TIMEOUT_SECONDS", "120"))
    )

    #: Seconds the ``app`` process waits for a hospital's LMA when it
    #: forwards a page request.
    upstream_timeout_seconds: float = field(
        default_factory=lambda: float(os.getenv("HEALTHCARE_DEMO_UPSTREAM_TIMEOUT_SECONDS", "30"))
    )

    #: How the ``app`` process starts a hospital that joins while the demo
    #: runs: ``off`` (the default), ``process`` (a local process) or
    #: ``compose`` (a Docker container, through ``docker compose``). Only the
    #: ``app`` role of a split run on one's own machine may turn it on; the
    #: app refuses to start with it on under ``RENDER`` or in another role
    #: (see ``app.services.deployment.check_deployer``).
    deployer: str = field(default_factory=lambda: os.getenv("HEALTHCARE_DEMO_DEPLOYER", "off").strip().lower())

    #: Where each hospital that can join will listen, for the ``app``
    #: process: ``pl-290=http://127.0.0.1:8085``. The Dagents GMA accepts the
    #: same address through its ``GMA_FEDERATION_JOINABLE_SITES``.
    joining_lma_urls: str = field(
        default_factory=lambda: os.getenv("HEALTHCARE_DEMO_JOINING_LMA_URLS", "pl-290=http://127.0.0.1:8085")
    )

    #: Seconds the ``app`` process waits for a new hospital to start and
    #: answer its health check.
    deploy_timeout_seconds: float = field(
        default_factory=lambda: float(os.getenv("HEALTHCARE_DEMO_DEPLOY_TIMEOUT_SECONDS", "90"))
    )

    #: Where the deployer writes its files: in process mode the new hospital's
    #: log and process id, in compose mode the Compose files it passes to
    #: ``docker compose``.
    #: Empty means a new temporary directory.
    deploy_dir: str = field(default_factory=lambda: os.getenv("HEALTHCARE_DEMO_DEPLOY_DIR", ""))

    #: The image a new hospital's container runs, in compose mode. It is the
    #: image ``docker-compose.yml`` builds for the backend.
    deploy_image: str = field(
        default_factory=lambda: os.getenv("HEALTHCARE_DEMO_DEPLOY_IMAGE", "dagents/healthcare-demo-backend:local")
    )

    #: The Compose network a new hospital's container joins, in compose mode:
    #: the network of the running demo, so the GMA and the app reach it by name.
    deploy_network: str = field(
        default_factory=lambda: os.getenv("HEALTHCARE_DEMO_DEPLOY_NETWORK", "healthcare-demo_default")
    )

    @staticmethod
    def _url_map(value: str, name: str) -> dict[str, str]:
        urls: dict[str, str] = {}
        for entry in value.split(","):
            entry = entry.strip()
            if not entry:
                continue
            site_id, sep, url = entry.partition("=")
            if not sep or not site_id.strip() or not url.strip():
                raise ValueError(f"{name} entry {entry!r} is not site=url")
            urls[site_id.strip()] = url.strip().rstrip("/")
        return urls

    def lma_url_map(self) -> dict[str, str]:
        """``HEALTHCARE_DEMO_LMA_URLS`` as ``{site_id: url}``, in the order given.

        Raises ``ValueError`` for an entry that is not ``site=url``.
        """
        return self._url_map(self.lma_urls, "HEALTHCARE_DEMO_LMA_URLS")

    def joining_lma_url_map(self) -> dict[str, str]:
        """``HEALTHCARE_DEMO_JOINING_LMA_URLS`` as ``{site_id: url}``, in the order given.

        Raises ``ValueError`` for an entry that is not ``site=url``.
        """
        return self._url_map(self.joining_lma_urls, "HEALTHCARE_DEMO_JOINING_LMA_URLS")

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
