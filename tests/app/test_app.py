"""The Log, Review and Settings pages, driven through streamlit.testing (SPEC.md section 11)."""
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from streamlit.testing.v1 import AppTest

from parlaytracker.core import services
from parlaytracker.core.db import session_scope
from parlaytracker.core.models import (
    DataSource,
    EventStatus,
    FailureKind,
    HealthState,
    Leg,
    LegResult,
    MarketType,
    Slip,
    SlipStatus,
    SlipType,
    SourceHealth,
    Sport,
    Sportsbook,
    Tag,
)
from parlaytracker.core.schemas import SlipIn
from parlaytracker.ingest import espn

pytestmark = pytest.mark.db

MAIN = str(Path(__file__).resolve().parents[2] / "parlaytracker" / "app" / "main.py")
GAME = "401872963"  # PHI @ CHI in the recorded scheduled scoreboard
ROSTER = Path(__file__).resolve().parents[1] / "fixtures" / "espn" / "nfl_roster_22.json"
PLAYER = espn.parse_roster(json.loads(ROSTER.read_text()))[0]


def run_main() -> AppTest:
    at = AppTest.from_file(MAIN, default_timeout=30).run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def button(at: AppTest, label: str):
    return next(b for b in at.button if b.label == label)


def fill_player_leg(at: AppTest, uid: int, line: float, odds: int | None) -> AppTest:
    at.selectbox(key=f"leg{uid}_game").set_value(GAME).run()
    at.selectbox(key=f"leg{uid}_market").set_value(MarketType.PLAYER_RECEPTIONS).run()
    at.selectbox(key=f"leg{uid}_player").set_value(PLAYER.espn_athlete_id).run()
    at.number_input(key=f"leg{uid}_line").set_value(line).run()
    if odds is not None:
        at.number_input(key=f"leg{uid}_odds").set_value(odds).run()
    return at


def slips() -> list[Slip]:
    with session_scope() as session:
        rows = session.scalars(select(Slip).order_by(Slip.id)).all()
        for s in rows:
            _ = [(leg.event, leg.tags) for leg in s.legs]
        return list(rows)


# --- Auth and page shell -------------------------------------------------------------------


def test_signed_in_through_dev_login(app_env):
    at = run_main()
    assert at.header[0].value == "Log a slip"
    assert any("Signed in through Tailscale as alice@example.com" in c.value for c in at.caption)


def test_without_tailscale_or_dev_login_nothing_renders(app_env):
    app_env.delenv("DEV_LOGIN")
    from parlaytracker.core.config import get_settings
    get_settings.cache_clear()
    at = AppTest.from_file(MAIN, default_timeout=30).run()
    assert "through Tailscale" in at.error[0].value
    assert not at.header


# --- Log -----------------------------------------------------------------------------------


def test_log_a_single_player_prop(app_env):
    at = fill_player_leg(run_main(), 1, 4.5, -115)
    button(at, "Save").click().run()
    assert not at.exception
    assert any(s.value.startswith("Saved: Single -115") for s in at.success)
    [slip] = slips()
    assert (slip.slip_type, slip.american_odds, slip.is_placed, slip.logged_by) == (
        SlipType.SINGLE, -115, False, "alice@example.com")
    [leg] = slip.legs
    assert (leg.market_type, leg.line, leg.espn_athlete_id, leg.player_name) == (
        MarketType.PLAYER_RECEPTIONS, Decimal("4.5"), PLAYER.espn_athlete_id, PLAYER.name)
    assert (leg.event.espn_event_id, leg.event.home_team) == (GAME, "Chicago Bears")
    # the form is reset for the next slip, remembering the game
    assert at.selectbox(key="leg2_game").value == GAME


def test_log_a_same_game_parlay_needs_slip_odds(app_env):
    at = fill_player_leg(run_main(), 1, 4.5, None)
    button(at, "Add another leg").click().run()
    at.selectbox(key="leg2_market").set_value(MarketType.GAME_TOTAL).run()
    at.number_input(key="leg2_line").set_value(44.5).run()
    at.number_input(key="leg2_odds").set_value(-110).run()
    assert at.toggle(key="slip_sgp").value is True
    button(at, "Save").click().run()
    assert any("slip's odds" in e.value for e in at.error)
    assert slips() == []

    at.number_input(key="slip_odds").set_value(450).run()
    at.toggle(key="slip_placed").set_value(True).run()
    at.number_input(key="slip_stake").set_value(10.0).run()
    button(at, "Save").click().run()
    [slip] = slips()
    assert (slip.slip_type, slip.american_odds, slip.stake) == (SlipType.SGP, 450, Decimal("10.00"))
    assert [leg.american_odds for leg in slip.legs] == [None, -110]


def test_validation_errors_are_shown_and_nothing_is_saved(app_env):
    at = run_main()
    at.selectbox(key="leg1_game").set_value(GAME).run()
    at.number_input(key="leg1_line").set_value(45.3).run()
    at.number_input(key="leg1_odds").set_value(-110).run()
    button(at, "Save").click().run()
    assert any("whole or half number" in e.value for e in at.error)
    assert slips() == []


def test_duplicate_selection_warning(app_env):
    at = fill_player_leg(run_main(), 1, 4.5, -115)
    button(at, "Save").click().run()
    at = fill_player_leg(at, 2, 4.5, -120)
    assert any("already logged by alice@example.com at -115" in w.value for w in at.warning)


# --- Review --------------------------------------------------------------------------------


def _review_page():
    import streamlit as st

    from parlaytracker.app.pages import review

    st.session_state["login"] = "alice@example.com"
    review.render()


def _log_finished_game_slip() -> int:
    with session_scope() as session:
        event = services.upsert_event(
            session, sport=Sport.NFL, espn_event_id="401872958",
            start_time=datetime.now(tz=UTC) - timedelta(hours=6), home_team="San Francisco 49ers",
            away_team="Arizona Cardinals", home_espn_team_id="25", away_espn_team_id="22",
            status=EventStatus.SCHEDULED)
        book = session.scalars(select(Sportsbook).where(Sportsbook.name == "DraftKings")).one()
        slip = services.create_slip(session, SlipIn(
            is_placed=True, stake="10", slip_type="single", sportsbook_id=book.id,
            american_odds=-110, source="quick_add",
            legs=[{"event_id": event.id, "market_type": "game_total", "line": "45.5",
                   "american_odds": -110}]), "alice@example.com")
        return slip.legs[0].id


def test_review_settles_a_finished_game_from_the_final_score(app_env):
    leg_id = _log_finished_game_slip()
    at = AppTest.from_function(_review_page, default_timeout=30).run()
    assert not at.exception
    assert any("Result needed" in m.value for m in at.markdown)
    at.number_input(key=f"rv_value_{leg_id}").set_value(66).run()
    next(b for b in at.button if b.key == f"rv_settle_{leg_id}").click().run()
    assert any(s.value == "Leg settled." for s in at.success)
    with session_scope() as session:
        leg = session.get(Leg, leg_id)
        assert (leg.result, leg.settlement_source) == (LegResult.WIN, DataSource.MANUAL)
        assert (leg.slip.status, leg.slip.payout) == (SlipStatus.WIN, Decimal("19.09"))


def test_review_rejects_a_contradicting_result(app_env):
    leg_id = _log_finished_game_slip()
    at = AppTest.from_function(_review_page, default_timeout=30).run()
    at.number_input(key=f"rv_value_{leg_id}").set_value(66).run()
    at.selectbox(key=f"rv_result_{leg_id}").set_value(LegResult.LOSS).run()
    next(b for b in at.button if b.key == f"rv_settle_{leg_id}").click().run()
    assert any("makes this leg a win" in e.value for e in at.error)


# --- Settings ------------------------------------------------------------------------------


def _settings_page():
    from parlaytracker.app.pages import settings

    settings.render()


def test_settings_adds_and_retires_a_tag(app_env):
    at = AppTest.from_function(_settings_page, default_timeout=30).run()
    at.text_input(key="st_new_cat").input("Injury")
    at.text_input(key="st_new_name").input("WR1 out")
    next(b for b in at.button if b.label == "Add tag").click().run()
    assert any(s.value == "Tag added." for s in at.success)
    with session_scope() as session:
        tag = session.scalars(select(Tag)).one()
        assert (tag.category, tag.name, tag.retired) == ("injury", "WR1 out", False)
    next(b for b in at.button if b.label == "Retire").click().run()
    with session_scope() as session:
        assert session.scalars(select(Tag)).one().retired is True


# --- Health banner (SPEC.md section 9.2) ----------------------------------------------------


def set_health(engine, **rows):
    """Give each named source a row: name=dict(...) with SourceHealth columns."""
    with session_scope() as session:
        for source, values in rows.items():
            session.add(SourceHealth(source=source, **values))


def warnings(at: AppTest) -> str:
    return " ".join(w.value for w in at.warning) + " ".join(e.value for e in at.error)


def test_banner_is_quiet_when_the_worker_and_sources_are_healthy(app_env, engine):
    now = datetime.now(tz=UTC)
    set_health(engine, worker=dict(state=HealthState.OK, last_success_at=now),
               odds_api=dict(state=HealthState.OK, last_success_at=now, quota_remaining=480))
    assert warnings(run_main()) == ""


def test_banner_warns_when_the_worker_has_never_run(app_env):
    assert "never run" in warnings(run_main())


def test_banner_warns_when_the_worker_has_stopped(app_env, engine):
    set_health(engine, worker=dict(
        state=HealthState.OK, last_success_at=datetime.now(tz=UTC) - timedelta(minutes=10)))
    assert "last seen" in warnings(run_main())


def test_banner_warns_when_credits_run_low(app_env, engine):
    set_health(engine, worker=dict(state=HealthState.OK, last_success_at=datetime.now(tz=UTC)),
               odds_api=dict(state=HealthState.OK, quota_remaining=57))
    text = warnings(run_main())
    assert "57 credits" in text and "50" in text


def test_banner_names_a_source_that_has_been_failing(app_env, engine):
    now = datetime.now(tz=UTC)
    set_health(engine, worker=dict(state=HealthState.OK, last_success_at=now),
               odds_api=dict(state=HealthState.OPEN, failure_kind=FailureKind.BLOCKED,
                             open_until=now + timedelta(minutes=5),
                             last_success_at=now - timedelta(hours=1)))
    text = warnings(run_main())
    assert "odds_api is failing (blocked)" in text


def test_banner_says_when_the_data_format_changed(app_env, engine):
    now = datetime.now(tz=UTC)
    set_health(engine, worker=dict(state=HealthState.OK, last_success_at=now),
               odds_api=dict(state=HealthState.OPEN, failure_kind=FailureKind.SCHEMA,
                             open_until=now + timedelta(minutes=30)))
    assert "the data format changed" in warnings(run_main())
