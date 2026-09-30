"""Runtime settings for the healthcare stroke-triage demo.

Every URL and port is environment-driven, matching the framework's convention.
Nothing is hardcoded, and the defaults point at a local run.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from agents.common.env import load_env_files

load_env_files("env/.env.shared", "env/.env.healthcare-demo", "apps/healthcare-demo/env/.env.healthcare-demo")


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
        """The CORS origin list, falling back to any origin when unset."""
        origins = [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]
        return origins or ["*"]

    def as_health_payload(self) -> dict[str, str]:
        return {
            "status": "ok",
            "service": self.app_name,
            "environment": self.app_env,
            "data": "synthetic-only",
        }


settings = Settings()
