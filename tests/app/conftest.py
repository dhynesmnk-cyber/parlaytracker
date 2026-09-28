"""Run the real Streamlit pages with streamlit.testing, against the test database, with ESPN
answered from recorded fixtures. The app commits for real, so tables are emptied after each
test."""
import json
import os
from pathlib import Path

import pytest
import streamlit as st
from sqlalchemy import Engine, text

from parlaytracker.core.config import get_settings
from parlaytracker.core.db import session_factory
from parlaytracker.core.models import Sport
from parlaytracker.ingest import espn

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "espn"
LOGIN = "alice@example.com"


def _fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


def fake_scoreboard(sport, day, max_wait=0.0):
    if sport is Sport.NFL:
        return espn.parse_scoreboard(sport, _fixture("nfl_scoreboard_2026-09-28_scheduled.json"))
    return espn.ScoreboardResult([], {})


def fake_roster(sport, team_id, max_wait=0.0):
    return espn.parse_roster(_fixture("nfl_roster_22.json"))


def _reset_caches() -> None:
    get_settings.cache_clear()
    if session_factory.cache_info().currsize:
        session_factory().kw["bind"].dispose()
    session_factory.cache_clear()
    st.cache_data.clear()


@pytest.fixture
def app_env(engine: Engine, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    monkeypatch.setenv("DISPLAY_TZ", "Europe/London")
    monkeypatch.setenv("ALLOWED_LOGINS", LOGIN)
    monkeypatch.setenv("DEV_LOGIN", LOGIN)
    monkeypatch.setattr(espn, "fetch_scoreboard", fake_scoreboard)
    monkeypatch.setattr(espn, "fetch_roster", fake_roster)
    _reset_caches()
    yield monkeypatch
    _reset_caches()
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE slips, legs, leg_tags, events, tags, source_health "
                          "RESTART IDENTITY CASCADE"))
