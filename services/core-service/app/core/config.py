"""Application configuration for the core service."""

from dataclasses import dataclass
import os


@dataclass(frozen=True, slots=True)
class Settings:
    app_name: str = os.getenv("APP_NAME", "dagents-core-service")
    app_env: str = os.getenv("APP_ENV", "development")
    api_host: str = os.getenv("API_HOST", "0.0.0.0")
    api_port: int = int(os.getenv("API_PORT", "8040"))
    log_level: str = os.getenv("LOG_LEVEL", "info")
    lma_url: str = os.getenv("LMA_URL", "http://lma:8010")
    gma_url: str = os.getenv("GMA_URL", "http://gma:8020")
    model_service_url: str = os.getenv("MODEL_SERVICE_URL", "http://model-service:8000")
    pipeline_service_url: str = os.getenv("PIPELINE_SERVICE_URL", "http://pipeline-service:8030")

    def as_health_payload(self) -> dict[str, str]:
        return {
            "status": "ok",
            "service": self.app_name,
            "environment": self.app_env,
            "transport": "http",
        }


settings = Settings()
