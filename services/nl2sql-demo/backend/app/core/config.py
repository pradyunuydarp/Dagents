"""Runtime settings for the Dagents NL2SQL demo app."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from agents.common.env import load_env_files

load_env_files("env/.env.shared", "env/.env.nl2sql-demo")


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    """Environment-backed settings for the NL2SQL demo backend."""

    app_name: str = os.getenv("NL2SQL_APP_NAME", "dagents-nl2sql-demo")
    app_env: str = os.getenv("NL2SQL_APP_ENV", "development")
    api_host: str = os.getenv("NL2SQL_API_HOST", "0.0.0.0")
    api_port: int = int(os.getenv("NL2SQL_API_PORT", "8070"))

    models_dir: Path = Path(os.getenv("NL2SQL_MODELS_DIR", "models"))
    model_cache_dir: Path = Path(os.getenv("NL2SQL_MODEL_CACHE_DIR", "/tmp/dagents-nl2sql-models"))
    default_model_id: str = os.getenv("NL2SQL_DEFAULT_MODEL_ID", "codet5p_spider_model")
    allow_fallback: bool = _bool_env("NL2SQL_ALLOW_FALLBACK", True)
    eager_load_model: bool = _bool_env("NL2SQL_EAGER_LOAD_MODEL", False)

    core_service_url: str = os.getenv("CORE_SERVICE_PUBLIC_URL", "http://127.0.0.1:8040")
    pipeline_service_url: str = os.getenv("PIPELINE_SERVICE_PUBLIC_URL", "http://127.0.0.1:8030")
    model_service_url: str = os.getenv("MODEL_SERVICE_PUBLIC_URL", "http://127.0.0.1:8000")
    lma_url: str = os.getenv("LMA_PUBLIC_URL", "http://127.0.0.1:8010")
    gma_url: str = os.getenv("GMA_PUBLIC_URL", "http://127.0.0.1:8020")

    request_timeout_seconds: float = float(os.getenv("NL2SQL_REQUEST_TIMEOUT_SECONDS", "2.0"))

    def as_health_payload(self) -> dict[str, str]:
        return {
            "status": "ok",
            "service": self.app_name,
            "environment": self.app_env,
            "transport": "http",
        }


settings = Settings()
