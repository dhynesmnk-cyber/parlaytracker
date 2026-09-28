"""Settings: the only place environment variables are read (SPEC.md section 3)."""
from functools import lru_cache
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def normalize_database_url(url: str) -> str:
    """Point Postgres URLs at the psycopg 3 driver; leave any other URL untouched."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url.removeprefix(prefix)
    return url


class Settings(BaseSettings):
    # Empty variables (e.g. "ODDS_API_KEY=" in .env) count as unset.
    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore")

    database_url: str
    display_tz: str
    odds_api_key: str | None = None
    odds_api_reserve: int = 50
    dashscope_api_key: str | None = None
    dashscope_base_url: str = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    qwen_vision_model: str = "qwen-vl-max"
    min_sample: int = 30
    record_event_ids: Annotated[list[str], NoDecode] = []

    @field_validator("database_url")
    @classmethod
    def _normalize_url(cls, v: str) -> str:
        return normalize_database_url(v)

    @field_validator("display_tz")
    @classmethod
    def _known_tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(f"unknown IANA time zone: {v!r}") from e
        return v

    @field_validator("record_event_ids", mode="before")
    @classmethod
    def _split_ids(cls, v: object) -> object:
        if isinstance(v, str):
            return [part.strip() for part in v.split(",") if part.strip()]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
