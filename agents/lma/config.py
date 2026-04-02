"""Configuration for the Local Monitoring Agent."""

from dataclasses import dataclass
import os


@dataclass(frozen=True, slots=True)
class Settings:
    app_name: str = os.getenv("LMA_APP_NAME", "dagents-lma")
    app_env: str = os.getenv("LMA_APP_ENV", "development")
    api_host: str = os.getenv("LMA_API_HOST", "0.0.0.0")
    api_port: int = int(os.getenv("LMA_API_PORT", "8010"))
    log_level: str = os.getenv("LMA_LOG_LEVEL", "info")
    gma_endpoint: str = os.getenv("LMA_GMA_ENDPOINT", "http://localhost:8020")


settings = Settings()
