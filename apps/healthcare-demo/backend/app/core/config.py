"""Runtime settings for the healthcare stroke-triage demo.

Every URL and port is environment-driven, matching the framework's convention.
Nothing is hardcoded, and the defaults point at a local run.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from agents.common.env import load_env_files

load_env_files("env/.env.shared", "env/.env.healthcare-demo", "apps/healthcare-demo/env/.env.healthcare-demo")


#: Characters a paste wraps a URL in — brackets and markdown punctuation.
#:
#: `[` and `]` are deliberately absent: an IPv6 origin legitimately ends in
#: one, as in `http://[::1]:8080`, and stripping it would corrupt a correct
#: value in order to tidy a wrong one.
_PASTE_NOISE = " \t<>()'\"`,;"

#: A host, with an optional port: a name, an IPv4 address, or a bracketed IPv6
#: literal. Used to decide whether an entry parsed into something a browser
#: could actually send, rather than into something that merely has a `://`.
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

    #: Postgres/Supabase URL for the encounter store. Empty means the cohort is
    #: generated in process instead. When it is set and cannot be read the app
    #: fails rather than falling back: showing generated data while reporting a
    #: live database would be the one lie this demo cannot afford.
    database_url: str = os.getenv("HEALTHCARE_DEMO_DATABASE_URL", "")

    #: Cohort size per simulated hospital — generated, or read back per site.
    cohort_size: int = int(os.getenv("HEALTHCARE_DEMO_COHORT_SIZE", "400"))

    #: Sites below which secure aggregation must not reveal a contribution.
    secure_aggregation_threshold: int = int(os.getenv("HEALTHCARE_DEMO_SECURE_THRESHOLD", "3"))

    #: Framework services, reached over HTTP when they are running.
    core_service_url: str = os.getenv("CORE_SERVICE_PUBLIC_URL", "http://127.0.0.1:8040")
    pipeline_service_url: str = os.getenv("PIPELINE_SERVICE_PUBLIC_URL", "http://127.0.0.1:8030")
    model_service_url: str = os.getenv("MODEL_SERVICE_PUBLIC_URL", "http://127.0.0.1:8000")
    lma_url: str = os.getenv("LMA_PUBLIC_URL", "http://127.0.0.1:8010")
    gma_url: str = os.getenv("GMA_PUBLIC_URL", "http://127.0.0.1:8020")

    request_timeout_seconds: float = float(os.getenv("HEALTHCARE_DEMO_REQUEST_TIMEOUT_SECONDS", "2.0"))

    #: Whether the app registers its Dagents extension on startup.
    register_extension: bool = _bool_env("HEALTHCARE_DEMO_REGISTER_EXTENSION", True)

    artifacts_dir: Path = Path(os.getenv("HEALTHCARE_DEMO_ARTIFACTS_DIR", "artifacts"))

    def allowed_origins(self) -> list[str]:
        """The CORS origin list, falling back to any origin when unset.

        Each entry is reduced to a bare origin — scheme, host, and port if one
        is given — because that is the only thing a browser ever sends in an
        `Origin` header. An entry written as a page URL,
        `https://example.github.io/Dagents`, cannot match anything, so a
        service configured that way blocks every request from the very site it
        was configured for, and the page looks down rather than refused. The
        path is dropped rather than honoured: in an origin allowlist it has no
        meaning to honour.
        """
        return normalize_origins(self.cors_origins) or ["*"]

    def as_health_payload(self) -> dict[str, str]:
        return {
            "status": "ok",
            "service": self.app_name,
            "environment": self.app_env,
            "data": "synthetic-only",
        }


settings = Settings()
