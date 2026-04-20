"""Configuration for the Global Monitoring Agent."""

from dataclasses import dataclass
import os

from agents.common.env import load_env_files


load_env_files("env/.env.shared", "env/.env.gma")


@dataclass(frozen=True, slots=True)
class Settings:
    app_name: str = os.getenv("GMA_APP_NAME", "dagents-gma")
    app_env: str = os.getenv("GMA_APP_ENV", "development")
    api_host: str = os.environ["GMA_API_HOST"]
    api_port: int = int(os.environ["GMA_API_PORT"])
    log_level: str = os.getenv("GMA_LOG_LEVEL", "info")


settings = Settings()
