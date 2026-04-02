"""Configuration for the Global Monitoring Agent."""

from dataclasses import dataclass
import os


@dataclass(frozen=True, slots=True)
class Settings:
    app_name: str = os.getenv("GMA_APP_NAME", "dagents-gma")
    app_env: str = os.getenv("GMA_APP_ENV", "development")
    api_host: str = os.getenv("GMA_API_HOST", "0.0.0.0")
    api_port: int = int(os.getenv("GMA_API_PORT", "8020"))
    log_level: str = os.getenv("GMA_LOG_LEVEL", "info")


settings = Settings()
