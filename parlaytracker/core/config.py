"""Settings: the only place environment variables are read (SPEC.md section 3)."""
from functools import lru_cache
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def normalize_database_url(url: str) -> str:
    """Point Postgres URLs at the psycopg 3 driver; leave any other URL untouched."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url.removeprefix(prefix)
    return url


class Settings(BaseSettings):
    # Empty variables (e.g. "ODDS_API_KEY=" in .env) count as unset. Errors never echo the
    # input: a bad setting must not print the API keys into the logs.
    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore",
                                      hide_input_in_errors=True)

    database_url: str
    display_tz: str
    odds_api_key: SecretStr | None = None  # read with .get_secret_value()
    odds_api_reserve: int = 50
    # Qwen through OpenRouter's OpenAI-compatible API (SPEC.md section 6.3).
    qwen_api_key: SecretStr | None = None
    qwen_base_url: str = "https://openrouter.ai/api/v1"
    qwen_vision_model: str = "qwen/qwen3-vl-32b-instruct"
    min_sample: int = 30
    record_event_ids: Annotated[list[str], NoDecode] = []
    # Tailscale login names allowed to use the web app (section 9.1), compared lowercased.
    allowed_logins: Annotated[list[str], NoDecode] = []
    # Local development only: the login assumed when no Tailscale header is present.
    dev_login: str | None = None

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

    @field_validator("record_event_ids", "allowed_logins", mode="before")
    @classmethod
    def _split_list(cls, v: object) -> object:
        if isinstance(v, str):
            return [part.strip() for part in v.split(",") if part.strip()]
        return v

    @field_validator("allowed_logins")
    @classmethod
    def _lowercase_logins(cls, v: list[str]) -> list[str]:
        return [login.lower() for login in v]


@lru_cache
def get_settings() -> Settings:
    return Settings()
