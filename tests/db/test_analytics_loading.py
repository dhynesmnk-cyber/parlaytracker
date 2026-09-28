"""The analytics loaders against Postgres: real slips through create_slip, settled and closed
through the services, then read back and summarised (SPEC.md sections 10 and 11)."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest
from sqlalchemy import select

from parlaytracker.core import analytics as an
from parlaytracker.core import services as svc
from parlaytracker.core.models import LegResult, MarketType, Tag
from parlaytracker.core.schemas import SlipIn

pytestmark = pytest.mark.db

MIN = 30


def slip(session, book, event, legs, user="a@example.com", **overrides):
    single = len(legs) == 1
    data = {"is_placed": True, "stake": "10.00", "slip_type": "single" if single else "sgp",
            "sportsbook_id": book.id, "american_odds": -110, "source": "quick_add",
            "legs": [{"event_id": event.id, "american_odds": -110 if single else None, **leg}
                     for leg in legs], **overrides}
    return svc.create_slip(session, SlipIn(**data), user)


def total(line):
    return {"market_type": "game_total", "line": line}


def rush(line):
    return {"market_type": "player_rushing_yards", "espn_athlete_id": "7", "line": line}


def settle(session, leg, value):
    svc.settle_leg_manually(session, leg, final_value=D(value))


def test_rows_carry_everything_the_dimensions_need(session, book, nfl_event, tag):
    s = slip(session, book, nfl_event, [{**total("45.5"), "tag_ids": [tag.id]}])
    settle(session, s.legs[0], "50")
    (row,) = an.load_leg_rows(session)
    assert (row.sport.value, row.market, row.sportsbook, row.slip_type.value) == (
        "nfl", MarketType.GAME_TOTAL, "DraftKings", "single")
    assert (row.leg_count, row.is_placed, row.logged_by, row.line, row.odds) == (
        1, True, "a@example.com", D("45.5"), -110)
    assert (row.result, row.tag_ids, row.start_time) == (
        LegResult.WIN, frozenset({tag.id}), nfl_event.start_time)
    assert row.selection_key == (nfl_event.id, MarketType.GAME_TOTAL, None, None, D("45.5"), None)


def test_other_legs_are_not_selections(session, book, nfl_event):
    slip(session, book, nfl_event, [{"market_type": "other", "description": "Moneyline",
                                     "line": None}])
    assert an.load_leg_rows(session) == []


def test_a_slip_row_has_the_earliest_game_and_the_leg_count(session, book, nfl_event, nfl_event_2):
    nfl_event_2.start_time = nfl_event.start_time - timedelta(days=1)
    s = svc.create_slip(session, SlipIn(
        is_placed=True, stake="10.00", slip_type="parlay", sportsbook_id=book.id,
        american_odds=+264, source="quick_add",
        legs=[{"event_id": nfl_event.id, "market_type": "game_total", "line": "45.5",
               "american_odds": -110},
              {"event_id": nfl_event_2.id, "market_type": "game_total", "line": "40.5",
               "american_odds": -110}]), "a@example.com")
    (row,) = an.load_slip_rows(session)
    assert (row.slip_id, row.leg_count, row.start_time) == (
        s.id, 2, nfl_event_2.start_time)


def test_two_users_logging_the_same_selection_count_once(session, book, nfl_event):
    first = slip(session, book, nfl_event, [total("45.5")], user="a@example.com")
    second = slip(session, book, nfl_event, [total("45.5")], user="b@example.com")
    first.created_at = datetime(2026, 10, 4, 10, tzinfo=UTC)
    second.created_at = datetime(2026, 10, 4, 11, tzinfo=UTC)
    for s in (first, second):
        settle(session, s.legs[0], "50")
    session.flush()
    stats = an.group_selections(an.load_leg_rows(session), "logged_by", MIN)
    assert [(g.label, g.n) for g in stats] == [("a@example.com", 1)]  # the earliest record


def test_an_end_to_end_hand_computed_summary(session, book, nfl_event, nfl_event_2):
    """Three legs on two games: totals 45.5 (wins, closes at 47.5), 40.5 (loses), and a rushing
    prop at +120 (wins, same-line close)."""
    a = slip(session, book, nfl_event, [total("45.5")])
    settle(session, a.legs[0], "50")
    svc.set_closing_line(session, a.legs[0], closing_line=D("47.5"), closing_odds=-110)
    b = slip(session, book, nfl_event_2, [total("40.5")])
    settle(session, b.legs[0], "30")
    c = slip(session, book, nfl_event, [{**rush("55.5"), "american_odds": 120}],
             american_odds=120)
    settle(session, c.legs[0], "60")
    svc.set_closing_line(session, c.legs[0], closing_line=D("55.5"), closing_odds=-120,
                         closing_opposite_odds=100)

    (s,) = an.group_selections(an.load_leg_rows(session), None, MIN)
    assert (s.wins, s.losses, s.n) == (2, 1, 3) and s.low_sample
    assert s.hit_rate == pytest.approx(2 / 3)
    # profit: -110 win +0.9091, -110 loss -1, +120 win +1.2  => +1.1091 over 3 legs
    assert s.profit_units == pytest.approx(100 / 110 - 1 + 1.2)
    assert s.roi == pytest.approx((100 / 110 - 1 + 1.2) / 3)
    assert s.break_even == pytest.approx((110 / 210 + 110 / 210 + 100 / 220) / 3)
    # the rushing prop: taken +120 (45.45%); closed -120 / +100 -> no-vig 0.5454/(0.5454+0.5)
    no_vig = (120 / 220) / (120 / 220 + 0.5)
    assert s.price_clv.n == 1
    assert s.price_clv.value == pytest.approx((no_vig - 100 / 220) * 100)
    assert (s.line_clv.n, s.line_clv.value) == (1, 2.0)


def test_slip_money_from_real_slips(session, book, nfl_event):
    win = slip(session, book, nfl_event, [total("45.5")])
    settle(session, win.legs[0], "50")
    loss = slip(session, book, nfl_event, [total("50.5")])
    settle(session, loss.legs[0], "50")
    slip(session, book, nfl_event, [total("55.5")])  # still pending
    unplaced = slip(session, book, nfl_event, [total("46.5")], is_placed=False, stake=None)
    settle(session, unplaced.legs[0], "50")
    cashed = slip(session, book, nfl_event, [total("47.5")], stake="20.00")
    svc.mark_cashed_out(session, cashed, D("12.00"))

    rows = an.load_slip_rows(session)
    (placed,) = an.group_slips(rows, None, placed=True, min_sample=MIN)
    # $10 won 19.09, $10 lost, $20 cashed out at 12: staked 40, returned 31.09
    assert (placed.n, placed.staked, placed.returned) == (3, D("40.00"), D("31.09"))
    assert placed.profit == D("-8.91")
    (units,) = an.group_slips(rows, None, placed=False, min_sample=MIN)
    assert (units.n, units.staked, units.returned) == (1, D("1"), D("1.91"))
    assert [p for _, p in an.cumulative_profit(rows)][-1] == D("-8.91")


def test_tags_are_loaded_per_leg_and_filter_any_all(session, book, nfl_event):
    other = Tag(category="weather", name="wind")
    session.add(other)
    session.flush()
    tag = session.scalars(select(Tag)).first()
    s = slip(session, book, nfl_event, [total("45.5")])
    s.legs[0].tags.extend(session.scalars(select(Tag)).all())
    session.flush()
    rows = an.load_leg_rows(session)
    ids = frozenset(t.id for t in session.scalars(select(Tag)))
    assert rows[0].tag_ids == ids
    assert an.apply_filters(rows, an.Filters(tag_ids=ids, tag_mode="all")) == rows
    assert an.apply_filters(rows, an.Filters(tag_ids=frozenset({tag.id + 999}))) == []


def test_an_empty_database_gives_empty_analytics(session):
    assert an.load_leg_rows(session) == [] and an.load_slip_rows(session) == []
    (s,) = an.group_selections([], None, MIN)
    assert s.n == 0
