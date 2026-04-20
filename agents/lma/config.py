"""Configuration for the Local Monitoring Agent."""

from dataclasses import dataclass
import os

from agents.common.env import load_env_files


load_env_files("env/.env.shared", "env/.env.lma")


@dataclass(frozen=True, slots=True)
class Settings:
    app_name: str = os.getenv("LMA_APP_NAME", "dagents-lma")
    app_env: str = os.getenv("LMA_APP_ENV", "development")
    api_host: str = os.environ["LMA_API_HOST"]
    api_port: int = int(os.environ["LMA_API_PORT"])
    log_level: str = os.getenv("LMA_LOG_LEVEL", "info")
    gma_endpoint: str = os.environ["LMA_GMA_ENDPOINT"]


settings = Settings()
