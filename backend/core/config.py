from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "sqlite:///data/inv_plan.db"
    attachments_dir: Path = Path("data/attachments")
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-3.5-flash-lite"
    enable_phoenix: bool = False
    default_sim_start: str = "2026-10-01T07:30:00+00:00"


settings = Settings()
