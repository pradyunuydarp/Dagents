"""Application configuration for the model service."""

from dataclasses import dataclass
import os

from agents.common.env import load_env_files


load_env_files("env/.env.shared", "env/.env.model-service")


@dataclass(frozen=True, slots=True)
class Settings:
    app_name: str = os.getenv("APP_NAME", "dagents-model-service")
    app_env: str = os.getenv("APP_ENV", "development")
    api_host: str = os.environ["API_HOST"]
    api_port: int = int(os.environ["API_PORT"])
    log_level: str = os.getenv("LOG_LEVEL", "info")
    model_artifact_dir: str = os.getenv("MODEL_ARTIFACT_DIR", "artifacts")
    raw_data_dir: str = os.getenv("RAW_DATA_DIR", "data/raw")
    model_device: str = os.getenv("MODEL_DEVICE", "cpu")
    random_seed: int = int(os.getenv("RANDOM_SEED", "42"))

    def as_health_payload(self) -> dict[str, str]:
        return {
            "status": "ok",
            "service": self.app_name,
            "environment": self.app_env,
            "transport": "http",
        }


settings = Settings()
