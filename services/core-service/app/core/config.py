"""Application configuration for the core service."""

from dataclasses import dataclass
import os

from agents.common.env import load_env_files


load_env_files("env/.env.shared", "env/.env.core-service")


@dataclass(frozen=True, slots=True)
class Settings:
    app_name: str = os.getenv("APP_NAME", "dagents-core-service")
    app_env: str = os.getenv("APP_ENV", "development")
    api_host: str = os.environ["API_HOST"]
    api_port: int = int(os.environ["API_PORT"])
    log_level: str = os.getenv("LOG_LEVEL", "info")
    lma_url: str = os.environ["LMA_URL"]
    gma_url: str = os.environ["GMA_URL"]
    model_service_url: str = os.environ["MODEL_SERVICE_URL"]
    pipeline_service_url: str = os.environ["PIPELINE_SERVICE_URL"]

    def as_health_payload(self) -> dict[str, str]:
        return {
            "status": "ok",
            "service": self.app_name,
            "environment": self.app_env,
            "transport": "http",
        }


settings = Settings()
