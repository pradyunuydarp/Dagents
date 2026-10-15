"""Configuration for the Global Monitoring Agent."""

from dataclasses import dataclass, field
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
    # The sites the GMA dispatches rounds to over HTTP, as site_id=url entries
    # separated by commas. Empty: rounds run in the GMA's own process.
    federation_sites: str = field(default_factory=lambda: os.getenv("GMA_FEDERATION_SITES", ""))
    # The sites that may get their address at run time, in the same format.
    # Each entry is the one address the GMA accepts for that site. Empty: no
    # site may join while the GMA runs.
    federation_joinable_sites: str = field(
        default_factory=lambda: os.getenv("GMA_FEDERATION_JOINABLE_SITES", "")
    )
    # Seconds to wait for each call to a site. A training job runs before the
    # site answers.
    federation_timeout_seconds: float = field(
        default_factory=lambda: float(os.getenv("GMA_FEDERATION_TIMEOUT_SECONDS", "120"))
    )


settings = Settings()
