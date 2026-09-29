"""The Analytics page, driven through streamlit.testing on seeded data (SPEC.md section 10)."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy import select
from streamlit.testing.v1 import AppTest

from parlaytracker.core import services
from parlaytracker.core.config import get_settings
from parlaytracker.core.db import session_scope
from parlaytracker.core.models import EventStatus, Sport, Sportsbook, Tag
from parlaytracker.core.schemas import SlipIn

pytestmark = pytest.mark.db

START = datetime(2026, 9, 27, 20, 5, tzinfo=UTC)


def _analytics_page():
    from parlaytracker.app.pages import analytics

    analytics.render()


def run() -> AppTest:
    at = AppTest.from_function(_analytics_page, default_timeout=30).run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def seed() -> None:
    """Three settled singles and one pending, on two games (numbers as in the DB tests):
    total 45.5 wins and closes at 47.5; total 40.5 loses; a +120 rushing prop wins and closes
    at the same line, -120 / +100."""
    with session_scope() as session:
        book = session.scalars(select(Sportsbook).where(Sportsbook.name == "DraftKings")).one()
        events = [services.upsert_event(
            session, sport=Sport.NFL, espn_event_id=f"4019{n}",
            start_time=START + timedelta(days=n),
            home_team="Chicago Bears", away_team="Philadelphia Eagles", home_espn_team_id="3",
            away_espn_team_id="21", status=EventStatus.SCHEDULED) for n in (0, 1)]

        def single(event, leg, odds=-110, **kw):
            return services.create_slip(session, SlipIn(
                is_placed=True, stake="10", slip_type="single", sportsbook_id=book.id,
                american_odds=odds, source="quick_add",
                legs=[{"event_id": event.id, "american_odds": odds, **leg}], **kw),
                "alice@example.com")

        a = single(events[0], {"market_type": "game_total", "line": "45.5"})
        services.settle_leg_manually(session, a.legs[0], final_value=D("50"))
        services.set_closing_line(session, a.legs[0], closing_line=D("47.5"), closing_odds=-110)
        b = single(events[1], {"market_type": "game_total", "line": "40.5"})
        services.settle_leg_manually(session, b.legs[0], final_value=D("30"))
        c = single(events[0], {"market_type": "player_rushing_yards", "espn_athlete_id": "7",
                               "line": "55.5"}, odds=120)
        services.settle_leg_manually(session, c.legs[0], final_value=D("60"))
        services.set_closing_line(session, c.legs[0], closing_line=D("55.5"), closing_odds=-120,
                                  closing_opposite_odds=100)
        single(events[1], {"market_type": "game_total", "line": "60.5"})  # pending


def frames(at: AppTest):
    return [d.value for d in at.dataframe]


def test_an_empty_database_says_so(app_env):
    at = run()
    assert any("Nothing logged yet" in i.value for i in at.info)
    assert not at.dataframe


def test_the_selection_view_shows_clv_first_then_the_grouped_table(app_env):
    seed()
    at = run()
    price, line = at.metric
    assert price.label.startswith("Price CLV") and line.label.startswith("Line CLV")
    assert price.value == "+3.6 pp" or price.value.endswith("pp")
    assert line.value == "+2.0 pts"
    # each metric is followed by its own n, and the two are never merged
    assert [c.value for c in at.caption if c.value.startswith("n ")] == ["n 1", "n 1"]
    table = frames(at)[0]
    assert table.columns[0] == "Sport"
    (row,) = table.to_dict("records")
    assert row["Sport"] == "nfl" and row["n"] == 3
    assert row["Hit rate (95% interval)"].startswith("66.7% (")
    assert row["Note"] == "low sample"  # 3 decided legs, minimum 30
    assert row["Price CLV"].endswith("(n 1)") and row["Line CLV"] == "+2.0 pts (n 1)"


def test_the_price_clv_figure_is_the_hand_computed_one(app_env):
    seed()
    price_clv = (((120 / 220) / (120 / 220 + 0.5)) - 100 / 220) * 100
    at = run()
    assert at.metric[0].value == f"{price_clv:+.2f} pp"


def test_grouping_by_market_splits_the_legs(app_env):
    seed()
    at = run()
    at.selectbox(key="an_group").set_value("market").run()
    table = frames(at)[0]
    assert {r["Market"]: r["n"] for r in table.to_dict("records")} == {
        "game_total": 2, "player_rushing_yards": 1}


def test_group_by_nothing_gives_one_overall_row(app_env):
    seed()
    at = run()
    at.selectbox(key="an_group").set_value("all").run()
    (row,) = frames(at)[0].to_dict("records")
    assert row["Group"] == "All legs" and row["n"] == 3


def test_a_group_at_the_minimum_sample_is_no_longer_low(app_env):
    seed()
    app_env.setenv("MIN_SAMPLE", "3")
    get_settings.cache_clear()
    (row,) = frames(run())[0].to_dict("records")
    assert row["Note"] == ""


def test_filters_narrow_the_table_and_a_third_one_brings_the_caution(app_env):
    seed()
    at = run()
    at.multiselect(key="an_f_market").set_value(["game_total"]).run()
    (row,) = frames(at)[0].to_dict("records")
    assert row["n"] == 2 and not at.warning
    at.multiselect(key="an_f_odds_band").set_value(["−110 to +100"]).run()
    assert not at.warning  # two filters
    at.multiselect(key="an_f_weekday").set_value(["Sunday"]).run()
    assert any("chance" in w.value for w in at.warning)


def test_tags_filter_any_and_all(app_env):
    seed()
    with session_scope() as session:
        wind = services.create_tag(session, "weather", "wind")
        cold = services.create_tag(session, "weather", "cold")
        from parlaytracker.core.models import Leg
        legs = session.scalars(select(Leg).order_by(Leg.id)).all()
        legs[0].tags.extend([wind, cold])
        legs[1].tags.append(wind)
        ids = {t.name: t.id for t in session.scalars(select(Tag))}
    at = run()
    at.multiselect(key="an_f_tags").set_value([ids["wind"], ids["cold"]]).run()
    at.selectbox(key="an_group").set_value("all").run()
    assert frames(at)[0].to_dict("records")[0]["n"] == 2  # any: two legs carry a selected tag
    at.radio(key="an_f_tagmode").set_value("all").run()
    assert frames(at)[0].to_dict("records")[0]["n"] == 1  # all: only one carries both


def test_the_money_tab_shows_profit_roi_and_the_cumulative_chart(app_env):
    seed()
    at = run()
    money = frames(at)[1:]  # after the selection table
    placed = money[0].to_dict("records")
    assert {r["Slip type"]: r["n"] for r in placed} == {"single": 3}
    row = placed[0]
    # staked 30, returned 19.09 + 0 + 22.00 = 41.09
    assert (row["Staked"], row["Returned"], row["Profit"]) == ("30.00", "41.09", "11.09")
    assert row["ROI"] == "+37.0%"
    assert len(at.get("vega_lite_chart")) == 1  # the cumulative profit line


def test_money_with_only_pending_slips_says_none_has_settled(app_env):
    with session_scope() as session:
        book = session.scalars(select(Sportsbook).where(Sportsbook.name == "DraftKings")).one()
        event = services.upsert_event(
            session, sport=Sport.NFL, espn_event_id="4019X", start_time=START,
            home_team="A", away_team="B", home_espn_team_id="1", away_espn_team_id="2",
            status=EventStatus.SCHEDULED)
        services.create_slip(session, SlipIn(
            is_placed=False, slip_type="single", sportsbook_id=book.id, american_odds=-110,
            source="quick_add",
            legs=[{"event_id": event.id, "market_type": "game_total", "line": "45.5",
                   "american_odds": -110}]), "alice@example.com")
    at = run()
    text = " ".join(i.value for i in at.info)
    assert "No settled legs match yet" in text
    assert "No placed slip has settled yet" in text
    assert "No unplaced slip has settled yet" in text


def test_the_page_is_in_the_navigation(app_env):
    from pathlib import Path

    main = str(Path(__file__).resolve().parents[2] / "parlaytracker" / "app" / "main.py")
    at = AppTest.from_file(main, default_timeout=30).run()
    assert not at.exception
    assert "Analytics" in Path(main).read_text()
