"""FrameExtractor settings (environment-driven, prefix FX_)."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="FX_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # HTTP
    http_host: str = "0.0.0.0"
    http_port: int = 9110

    # DB
    db_host: str = "fx-postgres"
    db_port: int = 5432
    db_name: str = "frames_db"
    db_user: str = "extractor_role"
    db_password: str = "dev_extractor_pw"
    db_pool_min: int = 1
    db_pool_max: int = 5

    # Storage paths (inside container — bind-mounted from host)
    media_dir: str = "/data/media"
    frames_dir: str = "/data/frames"

    # Worker pool
    workers: int = 2

    # Defaults exposed to the UI as form initial values
    default_target_fps: int = 5

    # Upload limit (multipart)
    max_upload_mb: int = 2048

    # Logging
    log_level: str = "INFO"
    log_format: str = "json"   # "json" | "console"

    @property
    def dsn(self) -> str:
        return (
            f"postgresql://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )
