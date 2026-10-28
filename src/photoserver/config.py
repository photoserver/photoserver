from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings read from environment variables and an optional .env file."""

    model_config = SettingsConfigDict(
        env_prefix="PHOTOSERVER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    storage_root: Path
    database_url: SecretStr
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    host: str = "127.0.0.1"
    port: int = 8000
    max_upload_size: int = 20 * 1024**3  # 20 GiB
    upload_chunk_size: int = 8 * 1024**2  # 8 MiB


@lru_cache
def get_settings() -> Settings:
    return Settings()
