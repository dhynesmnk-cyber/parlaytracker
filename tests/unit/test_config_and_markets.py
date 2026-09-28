import pytest
from pydantic import ValidationError

from parlaytracker.core.config import Settings, normalize_database_url
from parlaytracker.core.markets import markets_for
from parlaytracker.core.models import MarketType, Sport


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("postgres://u:p@h:5432/db", "postgresql+psycopg://u:p@h:5432/db"),
        ("postgresql://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        ("postgresql+psycopg://u:p@h/db", "postgresql+psycopg://u:p@h/db"),
        ("sqlite:///x.db", "sqlite:///x.db"),
    ],
)
def test_normalize_database_url(url, expected):
    assert normalize_database_url(url) == expected


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://u:p@h/db")
    monkeypatch.setenv("DISPLAY_TZ", "Europe/London")
    return monkeypatch


def test_settings_from_env(env):
    env.setenv("RECORD_EVENT_IDS", "401, 402,,")
    env.setenv("ODDS_API_KEY", "")
    s = Settings(_env_file=None)
    assert s.database_url == "postgresql+psycopg://u:p@h/db"
    assert s.record_event_ids == ["401", "402"]
    assert s.odds_api_key is None
    assert s.odds_api_reserve == 50
    assert s.qwen_base_url == "https://openrouter.ai/api/v1"
    assert s.qwen_vision_model == "qwen/qwen3-vl-32b-instruct"
    assert s.allowed_logins == []
    assert s.dev_login is None


def test_allowed_logins_are_split_and_lowercased(env):
    env.setenv("ALLOWED_LOGINS", "Alice@Example.com, bob@github ,")
    assert Settings(_env_file=None).allowed_logins == ["alice@example.com", "bob@github"]


def test_settings_reject_unknown_time_zone(env):
    env.setenv("DISPLAY_TZ", "Mars/Olympus")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_settings_require_database_url_and_tz(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DISPLAY_TZ", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_markets_per_sport():
    assert MarketType.PLAYER_RECEPTIONS in markets_for(Sport.NFL)
    assert MarketType.PLAYER_POINTS not in markets_for(Sport.NFL)
    assert MarketType.PLAYER_POINTS in markets_for(Sport.NBA)
    assert MarketType.PLAYER_POINTS in markets_for(Sport.NHL)
    assert not [m for m in markets_for(Sport.MLB) if m.value.startswith("player_")]
    for sport in Sport:
        assert {MarketType.GAME_TOTAL, MarketType.OTHER} <= set(markets_for(sport))
