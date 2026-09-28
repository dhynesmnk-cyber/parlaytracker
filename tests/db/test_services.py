from decimal import Decimal

import pytest

from parlaytracker.core.models import EntrySource, MarketType, SlipType, TeamSide
from parlaytracker.core.schemas import LegIn, SlipIn
from parlaytracker.core.services import (
    Duplicate,
    ServiceError,
    create_slip,
    find_duplicates,
    slip_warnings,
)

pytestmark = pytest.mark.db

USER = "a@example.com"


def game_total(event, line="45.5", odds=-110, **extra) -> dict:
    return {"event_id": event.id, "market_type": "game_total", "line": line,
            "american_odds": odds, **extra}


def slip_in(book, legs, **overrides) -> SlipIn:
    data = {"is_placed": False, "slip_type": "single", "sportsbook_id": book.id,
            "american_odds": legs[0].get("american_odds") or -110, "source": "quick_add",
            "legs": legs}
    return SlipIn(**{**data, **overrides})


def test_create_single(session, book, nfl_event):
    slip = create_slip(session, slip_in(book, [game_total(nfl_event)]), USER)
    assert slip.id is not None
    assert slip.logged_by == USER
    assert slip.slip_type is SlipType.SINGLE
    assert slip.source is EntrySource.QUICK_ADD
    assert [(leg.market_type, leg.line) for leg in slip.legs] == [
        (MarketType.GAME_TOTAL, Decimal("45.5"))]


def test_create_parlay_with_tags_and_other_leg(session, book, nfl_event, nfl_event_2, tag):
    legs = [
        game_total(nfl_event, tag_ids=[tag.id]),
        {"event_id": nfl_event_2.id, "market_type": "alt_spread", "side": "away",
         "line": "3.5", "american_odds": 120},
        {"event_id": nfl_event_2.id, "market_type": "other", "description": "Under 40.5",
         "american_odds": -115},
    ]
    data = slip_in(book, legs, slip_type="parlay", american_odds=900, is_placed=True,
                   stake="5.00")
    slip = create_slip(session, data, USER)
    assert slip.stake == Decimal("5.00")
    assert [leg.market_type for leg in slip.legs] == [
        MarketType.GAME_TOTAL, MarketType.ALT_SPREAD, MarketType.OTHER]
    assert slip.legs[0].tags == [tag]
    assert slip.legs[1].side is TeamSide.AWAY
    assert slip.legs[2].line is None


def test_create_sgp_with_leg_without_odds(session, book, nfl_event):
    legs = [game_total(nfl_event),
            {"event_id": nfl_event.id, "market_type": "player_receptions",
             "espn_athlete_id": "3139477", "player_name": "Player One", "line": "4.5"}]
    slip = create_slip(session, slip_in(book, legs, slip_type="sgp", american_odds=450), USER)
    assert slip.legs[1].american_odds is None
    assert slip.legs[1].espn_athlete_id == "3139477"


@pytest.mark.parametrize("problem", ["book", "event", "tag", "user"])
def test_unknown_references_are_rejected(session, book, nfl_event, problem):
    leg = game_total(nfl_event, tag_ids=[999_999] if problem == "tag" else [])
    if problem == "event":
        leg["event_id"] = 999_999
    data = slip_in(book, [leg], sportsbook_id=999_999 if problem == "book" else book.id)
    with pytest.raises(ServiceError):
        create_slip(session, data, "" if problem == "user" else USER)


def test_market_not_offered_for_sport_is_rejected(session, book, nba_event):
    leg = {"event_id": nba_event.id, "market_type": "player_receptions",
           "espn_athlete_id": "1", "line": "4.5", "american_odds": -110}
    with pytest.raises(ServiceError, match="not offered for nba"):
        create_slip(session, slip_in(book, [leg]), USER)


def test_find_duplicates_matches_same_selection_only(session, book, nfl_event):
    create_slip(session, slip_in(book, [game_total(nfl_event, odds=-115)],
                                 american_odds=-115), "b@example.com")
    same = LegIn(**game_total(nfl_event))
    other_line = LegIn(**game_total(nfl_event, line="46.5"))
    dupes = find_duplicates(session, [other_line, same])
    assert [(d.leg_number, d.logged_by, d.american_odds) for d in dupes] == [
        (2, "b@example.com", -115)]


def test_find_duplicates_for_player_and_side_markets(session, book, nfl_event):
    player = {"event_id": nfl_event.id, "market_type": "player_rushing_yards",
              "espn_athlete_id": "7", "line": "50.5", "american_odds": -110}
    create_slip(session, slip_in(book, [player]), USER)
    assert len(find_duplicates(session, [LegIn(**player)])) == 1
    other_player = LegIn(**{**player, "espn_athlete_id": "8"})
    assert find_duplicates(session, [other_player]) == []


def test_duplicates_flow_into_warnings(session, book, nfl_event):
    create_slip(session, slip_in(book, [game_total(nfl_event)]), USER)
    data = slip_in(book, [game_total(nfl_event)])
    dupes = find_duplicates(session, data.legs)
    assert dupes == [Duplicate(1, USER, -110, dupes[0].slip_id)]
    assert slip_warnings(data, dupes) == [f"Leg 1: already logged by {USER} at -110."]
