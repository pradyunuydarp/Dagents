"""Application configuration for the pipeline service."""

from dataclasses import dataclass
import os

from agents.common.env import load_env_files


load_env_files("env/.env.shared", "env/.env.pipeline-service")


@dataclass(frozen=True, slots=True)
class Settings:
    app_name: str = os.getenv("APP_NAME", "dagents-pipeline-service")
    app_env: str = os.getenv("APP_ENV", "development")
    api_host: str = os.environ["API_HOST"]
    api_port: int = int(os.environ["API_PORT"])
    log_level: str = os.getenv("LOG_LEVEL", "info")

    def as_health_payload(self) -> dict[str, str]:
        return {
            "status": "ok",
            "service": self.app_name,
            "environment": self.app_env,
            "transport": "http",
        }


settings = Settings()
