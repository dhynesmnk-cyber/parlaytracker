"""The CSV slip importer's parsing and planning (no database), against ESPN's recorded
2026-09-27 scoreboard: the real ARI @ SF game (20:05 UTC) and its neighbours."""
import json
from datetime import UTC, date, datetime
from decimal import Decimal as D
from pathlib import Path

import pytest

from parlaytracker.core.models import MarketType as M
from parlaytracker.core.models import Sport
from parlaytracker.ingest import espn, slip_import
from parlaytracker.ingest.http import FetchError, RateLimited
from parlaytracker.core.models import FailureKind
from import_support import ROSTERS, csv_text, row, two_legs

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "espn"
BOARD = espn.parse_scoreboard(
    Sport.NFL, json.loads((FIXTURES / "nfl_scoreboard_2026-09-27_final.json").read_text()))
SUNDAY = date(2026, 9, 27)


def games_for(sport, day):
    return BOARD.games if day == SUNDAY else []


def roster_for(sport, team_id):
    return ROSTERS[team_id]


def plan(rows, **kw):
    (slip,) = slip_import.parse_csv(csv_text(rows))
    return slip_import.plan_slip(slip, games_for=kw.get("games_for", games_for),
                                 roster_for=kw.get("roster_for", roster_for))


# --- parse_csv --------------------------------------------------------------------------------


def test_a_csv_parses_into_slips_with_legs_in_order():
    rows = two_legs()
    slips = slip_import.parse_csv(csv_text(rows[::-1]))  # rows out of order
    (slip,) = slips
    assert (slip.slip_id, slip.odds, slip.wager, slip.paid, slip.boost_pct) == (
        "111", 450, D("40"), None, None)
    assert [leg.seq for leg in slip.legs] == [1, 2]
    assert slip.legs[1].line == D("0.5")
    assert slip.game_start == datetime(2026, 9, 27, 20, 5, tzinfo=UTC)
    assert slip.note == "import Hard Rock #111"


def test_the_odds_may_be_written_with_a_plus_sign_or_without():
    (a,) = slip_import.parse_csv(csv_text(two_legs(odds_american="+450")))
    (b,) = slip_import.parse_csv(csv_text(two_legs(odds_american="450")))
    assert a.odds == b.odds == 450


@pytest.mark.parametrize(("mutate", "message"), [
    (lambda rows: [{**r, "leg_count": "3"} for r in rows], "leg_count 3 but 2 rows"),
    (lambda rows: [rows[0], {**rows[1], "wager": "50"}], "wager differs"),
    (lambda rows: [{**r, "game_start_iso": "2026-09-27T16:05:00"} for r in rows], "no time zone"),
    (lambda rows: [{**r, "line": ""} for r in rows], "has no line"),
    (lambda rows: [{**r, "wager": "forty"} for r in rows], "not a number"),
    (lambda rows: [{**r, "odds_american": "even"} for r in rows], "odds"),
])
def test_a_malformed_file_is_rejected_whole(mutate, message):
    with pytest.raises(slip_import.CsvError, match=message):
        slip_import.parse_csv(csv_text(mutate(two_legs())))


def test_a_missing_column_is_named():
    text = csv_text(two_legs()).replace("game_start_iso", "start")
    with pytest.raises(slip_import.CsvError, match="game_start_iso"):
        slip_import.parse_csv(text)


# --- plan_slip: what is sound -----------------------------------------------------------------


def test_a_sound_slip_is_matched_to_its_game_and_players():
    p = plan(two_legs())
    assert p.status == "ok" and (p.errors, p.doubts) == ([], [])
    assert p.game.espn_event_id == "401872958"
    assert [(leg.athlete_id, leg.player_name, leg.market) for leg in p.legs] == [
        ("4361307", "Trey McBride", M.PLAYER_RECEPTIONS),
        ("3040151", "George Kittle", M.PLAYER_TOUCHDOWNS)]


def test_a_players_team_is_found_from_either_roster():
    """Kittle is on SF, McBride on ARI: the slip lists both, as an SGP does."""
    assert plan(two_legs()).status == "ok"


def test_the_printed_wording_is_checked_against_the_market_and_line():
    """Real wording is the resolver's test: a ladder is Over one half less, anytime is 0.5."""
    rows = [row(market="rushing_yards", line="64.5", raw_text="X - TO RECORD 65+ RUSHING YARDS",
                player="Trey McBride"),
            row(leg_seq="2", player="George Kittle", market="touchdowns", line="1.5",
                raw_text="GEORGE KITTLE - TO SCORE 2+ TDS")]
    assert plan(rows).status == "ok"


# --- plan_slip: what is not -------------------------------------------------------------------


@pytest.mark.parametrize(("mutate", "expected"), [
    (lambda r: {**r, "matchup": "Chiefs vs Bengals"}, 'no game on 2026-09-27 matches'),
    (lambda r: {**r, "game_start_iso": "2026-09-27T14:05:00-04:00"}, "ESPN has ARI @ SF at"),
    (lambda r: {**r, "bet_type": "parlay"}, "bet type 'parlay' is not supported"),
    (lambda r: {**r, "odds_american": "+50"}, "not valid American odds"),
    (lambda r: {**r, "status": "cashed"}, "is not won, lost, push or void"),
])
def test_slip_level_problems_are_errors(mutate, expected):
    p = plan([mutate(r) for r in two_legs()])
    assert p.status == "error"
    assert any(expected in e for e in p.errors), p.errors


def test_a_player_on_neither_roster_is_an_error():
    rows = two_legs()
    rows[0]["player"] = "Patrick Mahomes"
    p = plan(rows)
    assert p.status == "error"
    assert p.errors == ['leg 1: no player like "Patrick Mahomes" on either roster']


def test_a_close_but_not_certain_name_is_a_doubt_that_names_who_it_matched():
    rows = two_legs()
    rows[1]["player"] = "Chris McCaffrey"  # the roster has Christian
    p = plan([{**rows[0]}, {**rows[1], "market": "rushing_yards", "line": "80.5",
                            "raw_text": "CHRIS MCCAFFREY - RUSHING YARDS"}])
    assert p.status == "doubtful"
    assert p.legs[1].athlete_id == "3117251"
    assert "matched Christian McCaffrey" in p.doubts[0]


def test_a_slip_whose_status_disagrees_with_its_legs_is_an_error():
    rows = two_legs(status="won", paid="300")  # a leg lost, so it can't be won
    assert "disagrees" in plan(rows).errors[0]
    rows = two_legs(status="lost")
    rows[0]["result"] = "won"  # every leg won, so it can't be lost
    assert "disagrees" in plan(rows).errors[0]


def test_a_won_slip_needs_its_paid_amount_and_it_must_fit_the_odds():
    won = two_legs(status="won", paid="")
    won[0]["result"] = "won"
    assert "no paid amount" in plan(won).errors[0]
    close = two_legs(status="won", paid="220.10")  # 40 at +450 pays 220.00: rounded odds
    close[0]["result"] = "won"
    p = plan(close)
    assert p.status == "ok" and "the odds give 220.00" in p.notes[0]
    far = two_legs(status="won", paid="250.00")  # a boost the odds don't show
    far[0]["result"] = "won"
    p = plan(far)
    assert p.status == "doubtful" and "give 220.00" in p.doubts[0]


def test_wording_that_reads_as_another_market_is_a_doubt():
    rows = two_legs()
    rows[0]["raw_text"] = "TREY MCBRIDE - RECEIVING YARDS"  # the market column says receptions
    p = plan(rows)
    assert p.status == "doubtful"
    assert 'reads as player_receiving_yards, not player_receptions' in p.doubts[0]


def test_a_ladder_line_that_is_not_one_half_less_is_a_doubt():
    rows = two_legs()
    rows[0].update(market="rushing_yards", line="65.5", raw_text="X - TO RECORD 65+ RUSHING YARDS")
    p = plan(rows)
    assert p.status == "doubtful" and "means Over 64.5, not 65.5" in p.doubts[0]


def test_a_bet_placed_after_kickoff_is_a_doubt():
    p = plan(two_legs(placed_at_iso="2026-09-27T17:00:00-04:00"))
    assert p.status == "doubtful" and p.doubts == ["placed at or after kickoff"]


def test_a_boost_is_noted_and_the_shown_odds_are_kept():
    p = plan(two_legs(boost_pct="33"))
    assert p.status == "ok" and "33% boost: odds +450 taken as the boosted odds" in p.notes


def test_a_line_that_is_not_a_half_or_whole_number_is_an_error():
    rows = two_legs()
    rows[0]["line"] = "9.3"
    assert "not a positive whole or half number" in plan(rows).errors[0]


@pytest.mark.parametrize("error", [FetchError("scoreboard", FailureKind.TRANSIENT, "down"),
                                   RateLimited("espn", 0.0)])
def test_espn_being_down_is_an_error_on_the_slip_not_a_crash(error):
    def failing(sport, day):
        raise error

    p = plan(two_legs(), games_for=failing)
    assert p.status == "error" and "couldn't get 2026-09-27's games" in p.errors[0]


def test_espn_is_asked_once_per_day_and_per_team_however_many_slips_share_them():
    calls = []

    def counting_games(sport, day):
        calls.append(("games", day))
        return games_for(sport, day)

    def counting_roster(sport, team):
        calls.append(("roster", team))
        return roster_for(sport, team)

    slips = slip_import.parse_csv(csv_text(two_legs(slip_id="1") + two_legs(slip_id="2")))
    games, rosters = slip_import._once(counting_games), slip_import._once(counting_roster)
    plans = [slip_import.plan_slip(s, games_for=games, roster_for=rosters) for s in slips]
    assert [p.status for p in plans] == ["ok", "ok"]
    assert sorted(calls) == [("games", SUNDAY), ("roster", "22"), ("roster", "25")]


def test_a_failure_is_remembered_too_so_a_down_provider_is_not_asked_again():
    calls = []

    def failing(sport, day):
        calls.append(day)
        raise RateLimited("espn", 0.0)

    cached = slip_import._once(failing)
    for _ in range(3):
        with pytest.raises(RateLimited):
            cached(Sport.NFL, SUNDAY)
    assert calls == [SUNDAY]


def test_summary_lists_each_slip_with_its_problems():
    good, bad = plan(two_legs(slip_id="1")), plan(two_legs(slip_id="2", matchup="Nobody vs Nobody"))
    text = slip_import.summarize([good, bad])
    assert "OK        1 " in text and "ERROR     2 " in text
    assert "1 error, 1 ok" in text
