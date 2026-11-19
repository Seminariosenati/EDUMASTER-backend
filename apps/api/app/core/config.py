from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="EDUMASTER_", extra="ignore")

    env: str = "local"
    database_url: str = "sqlite:///./edumaster.db"
    jwt_secret: str = "cambiar-este-secreto-en-produccion"
    jwt_expire_minutes: int = 480
    cors_origins: list[str] = ["http://localhost:5173"]
    model_dir: Path = Path("./artifacts/models")
    web_dist: Path | None = None
    seed_demo: bool = True
    demo_students: int = 240
    auto_train_on_seed: bool = True
    resource_interval_seconds: float = 2.0
    live_inference_seconds: float = 5.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
