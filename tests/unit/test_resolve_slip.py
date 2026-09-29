"""Resolving what the model read into what the form pre-fills (SPEC.md section 9.6), against
the recorded ESPN scoreboards and rosters. The Qwen replies are synthetic (tests/fixtures/qwen)."""
import json
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from parlaytracker.core.models import MarketType as M
from parlaytracker.core.models import Sport, TeamSide
from parlaytracker.core.schemas import ExtractedLeg, ExtractedSlip
from parlaytracker.ingest import espn, resolve
from parlaytracker.ingest.extraction import parse_reply
from parlaytracker.ingest.http import FetchError, RateLimited
from parlaytracker.core.models import FailureKind

ESPN = Path(__file__).resolve().parents[1] / "fixtures" / "espn"
QWEN = Path(__file__).resolve().parents[1] / "fixtures" / "qwen"
SUNDAY, MONDAY = date(2026, 9, 27), date(2026, 9, 28)
BOOKS = {1: "DraftKings", 2: "FanDuel", 3: "BetMGM", 4: "Caesars"}


def board(sport, name):
    return espn.parse_scoreboard(sport, json.loads((ESPN / name).read_text())).games


NFL_SUNDAY = board(Sport.NFL, "nfl_scoreboard_2026-09-27_final.json")
NFL_MONDAY = board(Sport.NFL, "nfl_scoreboard_2026-09-28_scheduled.json")
NBA_DAY = board(Sport.NBA, "nba_scoreboard_2026-03-01.json")
ARI_ROSTER = espn.parse_roster(json.loads((ESPN / "nfl_roster_22.json").read_text()))
NYK_ROSTER = espn.parse_roster(json.loads((ESPN / "nba_roster_18.json").read_text()))
ARI_SF = next(g for g in NFL_SUNDAY if g.espn_event_id == "401872958")
PHI_CHI = NFL_MONDAY[0]


def games_for(sport, day):
    return {(Sport.NFL, SUNDAY): NFL_SUNDAY, (Sport.NFL, MONDAY): NFL_MONDAY,
            (Sport.NBA, date(2026, 3, 1)): NBA_DAY}.get((sport, day), [])


def roster_for(sport, team_id):
    return {(Sport.NFL, "22"): ARI_ROSTER, (Sport.NBA, "18"): NYK_ROSTER}.get((sport, team_id), [])


def resolve_reply(name, day=SUNDAY, games=games_for, rosters=roster_for, default=Sport.NFL):
    slip = parse_reply((QWEN / name).read_text())
    return resolve.resolve_slip(slip, books=BOOKS, day=day, default_sport=default,
                                games_for=games, roster_for=rosters)


# --- Markets --------------------------------------------------------------------------------


@pytest.mark.parametrize(("text", "sport", "player", "market"), [
    ("Receiving Yards", Sport.NFL, True, M.PLAYER_RECEIVING_YARDS),
    ("Rec Yds", Sport.NFL, True, M.PLAYER_RECEIVING_YARDS),
    ("Rec. Yds", Sport.NFL, True, M.PLAYER_RECEIVING_YARDS),
    ("Receptions", Sport.NFL, True, M.PLAYER_RECEPTIONS),
    ("Recs", Sport.NFL, True, M.PLAYER_RECEPTIONS),
    ("Rushing Yards", Sport.NFL, True, M.PLAYER_RUSHING_YARDS),
    ("Rush Yds", Sport.NFL, True, M.PLAYER_RUSHING_YARDS),
    ("Passing Yards", Sport.NFL, True, M.PLAYER_PASSING_YARDS),
    ("Pass Yds", Sport.NFL, True, M.PLAYER_PASSING_YARDS),
    ("Alt Spread", Sport.NFL, False, M.ALT_SPREAD),
    ("Alternate Spread", Sport.NFL, False, M.ALT_SPREAD),
    ("Point Spread", Sport.NBA, False, M.ALT_SPREAD),
    ("Run Line", Sport.MLB, False, M.ALT_SPREAD),
    ("Puck Line", Sport.NHL, False, M.ALT_SPREAD),
    ("Total Points", Sport.NFL, False, M.GAME_TOTAL),
    ("Game Total", Sport.NFL, False, M.GAME_TOTAL),
    ("Over/Under", Sport.NFL, False, M.GAME_TOTAL),
    ("O/U", Sport.NBA, False, M.GAME_TOTAL),
    ("Team Total", Sport.NFL, False, M.TEAM_TOTAL),
    ("Team Total Points", Sport.NFL, False, M.TEAM_TOTAL),
    ("Points", Sport.NBA, True, M.PLAYER_POINTS),
    ("Player Points", Sport.NHL, True, M.PLAYER_POINTS),
])
def test_market_wording(text, sport, player, market):
    assert resolve.resolve_market(text, sport, player) == resolve.ResolvedMarket(market)


@pytest.mark.parametrize(("text", "sport", "player"), [
    ("Under 44.5 Total Points", Sport.NFL, False),   # an Under is out of scope
    ("Total Points Under", Sport.NFL, False),
    ("Under 5.5 Receptions", Sport.NFL, True),
    ("u44.5", Sport.NFL, False),
    ("Moneyline", Sport.NFL, False),
    ("First Touchdown Scorer", Sport.NFL, True),
    ("Passing Touchdowns", Sport.NFL, True),        # a passing TD is not the TD market
    ("Rec Yds", Sport.NBA, True),                    # not a market that sport has
    ("Points", Sport.NFL, True),                     # player points exist for NBA and NHL only
    ("Points", Sport.NBA, False),                    # "points" with no player isn't a prop
])
def test_unsupported_or_under_becomes_an_other_leg_without_doubt(text, sport, player):
    assert resolve.resolve_market(text, sport, player) == resolve.ResolvedMarket(M.OTHER)


@pytest.mark.parametrize("text", [None, "", "   "])
def test_an_unreadable_market_is_other_and_doubtful(text):
    assert resolve.resolve_market(text, Sport.NFL, False) == resolve.ResolvedMarket(
        M.OTHER, doubtful=True)


def test_team_total_wins_over_the_word_total():
    assert resolve.resolve_market("Team Total", Sport.NFL, False).market is M.TEAM_TOTAL


@pytest.mark.parametrize(("text", "sport"), [
    ("Puck Line", Sport.NHL), ("Run Line", Sport.MLB), ("Rec Yds", Sport.NFL),
    ("Rushing Yards", Sport.NFL), ("Total Points", None), (None, None)])
def test_some_market_wording_says_which_sport(text, sport):
    assert resolve.market_sport_hint(text) == sport


@pytest.mark.parametrize(("text", "sport"), [
    ("NFL", Sport.NFL), ("nba", Sport.NBA), ("Hockey", Sport.NHL), ("MLB baseball", Sport.MLB),
    ("football", Sport.NFL), ("NFL basketball", None), ("cricket", None), (None, None)])
def test_sport_wording(text, sport):
    assert resolve.resolve_sport(text) == sport


# --- Sportsbooks ----------------------------------------------------------------------------


@pytest.mark.parametrize(("text", "book"), [
    ("DraftKings", 1), ("draftkings", 1), ("Draft Kings", 1), ("DK", 1),
    ("DraftKings Sportsbook", 1), ("DraftKing", 1),                      # a typo
    ("FanDuel", 2), ("FanDuel Sportsbook", 2), ("FD", 2),
    ("BetMGM", 3), ("MGM", 3), ("Bet MGM", 3),
    ("Caesars", 4), ("Caesars Sportsbook", 4), ("William Hill", 4),
    ("DraftKings Sportsbook NJ", 1)])
def test_sportsbook_wording(text, book):
    assert resolve.resolve_sportsbook(text, BOOKS) == book


@pytest.mark.parametrize("text", [None, "", "Hard Rock Bet", "Bovada", "Fan"])
def test_an_unknown_sportsbook_is_left_for_the_person(text):
    assert resolve.resolve_sportsbook(text, BOOKS) is None


def test_a_sportsbook_added_in_settings_is_found_too():
    assert resolve.resolve_sportsbook("ESPN Bet", {**BOOKS, 5: "ESPN BET"}) == 5
    assert resolve.resolve_sportsbook("espnbet", {**BOOKS, 5: "ESPN BET"}) == 5


# --- Teams and games ------------------------------------------------------------------------


def test_team_aliases():
    assert {"chicago bears", "chicago", "bears", "chi"} <= resolve.team_aliases(
        "Chicago Bears", "CHI")
    assert {"boston red sox", "boston", "red sox", "sox", "bos"} <= resolve.team_aliases(
        "Boston Red Sox", "BOS")


def test_a_first_word_many_teams_share_is_not_an_alias():
    for name, abbr in (("New York Giants", "NYG"), ("Los Angeles Chargers", "LAC"),
                       ("San Francisco 49ers", "SF")):
        aliases = resolve.team_aliases(name, abbr)
        assert not aliases & resolve.GENERIC_WORDS
    assert "49ers" in resolve.team_aliases("San Francisco 49ers", "SF")


@pytest.mark.parametrize("text", [
    "ARI @ SF", "Cardinals @ 49ers", "Cardinals at 49ers",
    "Arizona Cardinals vs San Francisco 49ers", "arizona at san francisco", "SF vs ARI"])
def test_both_teams_named_matches_the_game(text):
    match = resolve.match_game(text, None, NFL_SUNDAY)
    assert (match.game.espn_event_id, match.doubtful) == ("401872958", False)


def test_the_team_text_can_supply_the_second_team():
    match = resolve.match_game("Cardinals", "49ers", NFL_SUNDAY)
    assert (match.game.espn_event_id, match.doubtful) == ("401872958", False)


def test_one_team_named_matches_only_if_that_is_unique_and_is_doubtful():
    match = resolve.match_game("Cardinals -3.5", None, NFL_SUNDAY)
    assert (match.game.espn_event_id, match.doubtful) == ("401872958", True)


def test_an_abbreviation_only_matches_as_a_whole_word():
    # "Cardinals" contains "car" (Carolina's abbreviation) but must not match CAR @ CLE
    match = resolve.match_game("Cardinals", None, NFL_SUNDAY)
    assert match.game.espn_event_id == "401872958"
    assert resolve.match_game("carbon", None, NFL_SUNDAY).game is None


def test_no_match_when_nothing_is_named_or_the_teams_are_not_playing():
    assert resolve.match_game(None, None, NFL_SUNDAY) == resolve.GameMatch(None)
    assert resolve.match_game("Packers", None, NFL_SUNDAY).game is None  # not playing that day
    assert resolve.match_game("ARI @ SF", None, []).game is None


def test_a_wrong_team_next_to_a_right_one_is_only_a_doubtful_match():
    # The Packers aren't playing, the Lions are: the game is offered, highlighted, not trusted
    match = resolve.match_game("Packers @ Lions", None, NFL_SUNDAY)
    assert match.game.home.abbreviation == "DET" and match.doubtful


def test_a_name_that_fits_two_games_is_not_guessed():
    # Both New York teams play on the 27th (Giants @ Titans, Jets @ Lions): "New York" alone
    # identifies neither, and "Giants" alone is unique but doubtful.
    assert resolve.match_game("New York", None, NFL_SUNDAY).game is None
    assert resolve.match_game("Giants", None, NFL_SUNDAY).doubtful


def test_a_shared_word_within_one_game_identifies_neither_team():
    fake = replace(ARI_SF, home=espn.Team("1", "NYG", "New York Giants"),
                   away=espn.Team("2", "NYJ", "New York Jets"))
    assert resolve.match_game("New York", None, [fake]).game is None
    assert resolve.match_game("Jets at Giants", None, [fake]).doubtful is False


def test_a_doubleheader_picks_the_first_game_and_says_so():
    late = replace(ARI_SF, espn_event_id="999", start_time=ARI_SF.start_time + timedelta(hours=4))
    match = resolve.match_game("ARI @ SF", None, [late, ARI_SF])
    assert (match.game.espn_event_id, match.doubtful) == ("401872958", True)


@pytest.mark.parametrize(("text", "side"), [
    ("49ers", TeamSide.HOME), ("San Francisco", TeamSide.HOME), ("SF -3.5", TeamSide.HOME),
    ("Cardinals", TeamSide.AWAY), ("ARI", TeamSide.AWAY),
    ("Cardinals vs 49ers", None), ("Packers", None), (None, None), ("", None)])
def test_a_teams_side(text, side):
    assert resolve.match_side(text, ARI_SF) == side
    assert resolve.match_side("49ers", None) is None


# --- Players --------------------------------------------------------------------------------


def player(name, roster=ARI_ROSTER):
    return resolve.match_roster_player(name, roster)


def test_an_exact_name_is_selected_automatically():
    m = player("Trey McBride")
    assert (m.athlete_id, m.name, m.doubtful) == ("4361307", "Trey McBride", False)
    assert m.score == 100


@pytest.mark.parametrize("name", ["trey mcbride", "Trey  McBride", "McBride Trey", "Trey Mcbride"])
def test_case_spacing_and_order_do_not_matter(name):
    m = player(name)
    assert (m.athlete_id, m.doubtful) == ("4361307", False)


def test_a_suffix_is_ignored_both_ways():
    assert player("Marvin Harrison").athlete_id == "4432708"    # roster says "Jr."
    assert player("Marvin Harrison Jr.").doubtful is False


def test_a_look_alike_on_the_same_team_does_not_make_an_exact_name_doubtful():
    # Michael Wilson and Mack Wilson Sr. are both on the roster
    m = player("Michael Wilson")
    assert (m.athlete_id, m.doubtful) == ("4360761", False)


def test_a_middling_match_is_preselected_but_highlighted():
    m = player("T. McBride")
    assert m.athlete_id == "4361307" and m.doubtful
    assert resolve.PLAYER_DOUBTFUL_SCORE <= m.score < resolve.PLAYER_AUTO_SCORE


@pytest.mark.parametrize("name", ["Wilson", "Patrick Mahomes", "Q", "12345"])
def test_a_poor_match_is_left_blank(name):
    m = player(name)
    assert (m.athlete_id, m.name) == (None, None)


def test_no_name_or_no_roster_is_no_match():
    assert player(None).athlete_id is None and player("").athlete_id is None
    assert player("Trey McBride", []).athlete_id is None


def test_two_players_named_alike_are_not_silently_chosen():
    roster = [espn.RosterPlayer("1", "Jalen Williams", "G", "", False),
              espn.RosterPlayer("2", "Jalen Williams", "F", "", False)]
    m = player("Jalen Williams", roster)
    assert m.athlete_id in ("1", "2") and m.doubtful


def test_a_player_who_is_out_loses_a_tie():
    roster = [espn.RosterPlayer("1", "Jalen Williams", "G", "injured", True),
              espn.RosterPlayer("2", "Jalen Williams", "F", "", False)]
    assert player("Jalen Williams", roster).athlete_id == "2"


# --- The whole slip -------------------------------------------------------------------------


def test_a_clean_single_prefills_everything_with_no_doubt():
    slip = resolve_reply("single_valid.json")
    (leg,) = slip.legs
    assert leg.market is M.PLAYER_RECEIVING_YARDS and leg.athlete_id == "4361307"
    assert (leg.sport, leg.day, leg.line, leg.odds) == (Sport.NFL, SUNDAY, D("70.5"), -115)
    assert leg.game.espn_event_id == "401872958" and leg.doubts == frozenset()
    assert (slip.book_id, slip.stake, slip.payout, slip.placed) == (
        1, D("10.00"), D("18.70"), True)
    assert slip.sgp is None and slip.doubts == frozenset()


def test_a_same_game_parlay():
    slip = resolve_reply("sgp_valid.json")
    assert slip.book_id == 2 and slip.sgp is True and slip.slip_odds == 645
    receptions, total, spread = slip.legs
    assert receptions.market is M.PLAYER_RECEPTIONS and receptions.athlete_id == "4361307"
    assert total.market is M.GAME_TOTAL and total.line == D("60.5")
    assert (spread.market, spread.side, spread.line) == (M.ALT_SPREAD, TeamSide.HOME, D("-3.5"))
    assert {leg.game.espn_event_id for leg in slip.legs} == {"401872958"}
    assert all(leg.odds is None and not leg.doubts for leg in slip.legs)  # SGP legs have none


def test_a_team_market_without_a_team_is_highlighted():
    slip = resolve_reply("unicode_minus.json", day=MONDAY)
    (leg,) = slip.legs
    assert leg.market is M.TEAM_TOTAL and leg.side is None and "side" in leg.doubts
    assert (leg.odds, leg.line, slip.slip_odds, slip.book_id, slip.stake) == (
        150, D("-3.5"), -110, 3, D("25.00"))
    assert leg.game.espn_event_id == PHI_CHI.espn_event_id and "game" not in leg.doubts


def test_a_partial_read_is_highlighted_everywhere_it_is_unsure():
    slip = resolve_reply("partial.json", day=MONDAY)
    (leg,) = slip.legs
    assert leg.market is M.OTHER and leg.line is None
    assert leg.description == "Bears 44.5"  # the slip's own words are kept
    assert leg.game.espn_event_id == PHI_CHI.espn_event_id  # only one game has the Bears
    assert leg.doubts == {"sport", "game", "market"}
    assert slip.book_id is None and slip.doubts == {"book"} and slip.placed is False


def test_the_game_is_found_give_or_take_a_day():
    slip = resolve_reply("fenced.txt", day=SUNDAY)  # "PHI @ CHI" is on the Monday board
    (leg,) = slip.legs
    assert leg.game.espn_event_id == PHI_CHI.espn_event_id and leg.day == MONDAY
    assert leg.market is M.GAME_TOTAL and "game" not in leg.doubts


def test_an_under_becomes_an_other_leg_with_the_slips_words():
    slip = resolve.resolve_slip(
        ExtractedSlip(legs=[ExtractedLeg(sport="NFL", event_text="ARI @ SF",
                                         player_name="Trey McBride",
                                         market_text="Under 5.5 Receptions", line=5.5)]),
        books=BOOKS, day=SUNDAY, default_sport=Sport.NFL, games_for=games_for,
        roster_for=roster_for)
    (leg,) = slip.legs
    assert leg.market is M.OTHER and leg.line is None and leg.athlete_id is None
    assert leg.description == "Trey McBride Under 5.5 Receptions 5.5"
    assert "market" not in leg.doubts


def test_an_nba_points_prop():
    extracted = ExtractedSlip(
        sportsbook_text="FanDuel", american_odds=-120,
        legs=[ExtractedLeg(sport="NBA", event_text="SA @ NY", player_name="Jalen Brunson",
                           market_text="Points", line=27.5, american_odds=-120)])
    slip = resolve.resolve_slip(extracted, books=BOOKS, day=date(2026, 3, 1),
                                default_sport=Sport.NFL, games_for=games_for,
                                roster_for=roster_for)
    (leg,) = slip.legs
    assert (leg.sport, leg.market, leg.doubts) == (Sport.NBA, M.PLAYER_POINTS, frozenset())
    assert leg.athlete_id and leg.game.away.abbreviation == "SA"


def test_the_sport_comes_from_the_market_wording_when_the_slip_omits_it():
    extracted = ExtractedSlip(legs=[ExtractedLeg(event_text="ARI @ SF", market_text="Rec Yds",
                                                 player_name="Trey McBride", line=70.5)])
    slip = resolve.resolve_slip(extracted, books=BOOKS, day=SUNDAY, default_sport=Sport.NBA,
                                games_for=games_for, roster_for=roster_for)
    assert slip.legs[0].sport is Sport.NFL and "sport" not in slip.legs[0].doubts


def test_a_single_takes_the_slips_odds_when_the_leg_shows_none():
    extracted = ExtractedSlip(american_odds=-110, legs=[ExtractedLeg(
        event_text="ARI @ SF", market_text="Total Points", line=60.5)])
    slip = resolve.resolve_slip(extracted, books=BOOKS, day=SUNDAY, default_sport=Sport.NFL,
                                games_for=games_for, roster_for=roster_for)
    assert slip.legs[0].odds == -110


@pytest.mark.parametrize(("odds", "expected", "doubt"), [
    (-110, -110, False), (100, 100, False), (-100, -100, False),
    (50, None, True), (-99, None, True), (0, None, True), (None, None, False)])
def test_odds_between_minus_99_and_plus_99_are_not_odds(odds, expected, doubt):
    extracted = ExtractedSlip(legs=[ExtractedLeg(
        event_text="ARI @ SF", market_text="Total Points", line=60.5, american_odds=odds)])
    leg = resolve.resolve_slip(extracted, books=BOOKS, day=SUNDAY, default_sport=Sport.NFL,
                               games_for=games_for, roster_for=roster_for).legs[0]
    assert leg.odds == expected and ("odds" in leg.doubts) is doubt


def test_a_line_that_is_not_a_half_or_whole_number_is_highlighted():
    extracted = ExtractedSlip(legs=[ExtractedLeg(
        event_text="ARI @ SF", market_text="Total Points", line=60.25)])
    leg = resolve.resolve_slip(extracted, books=BOOKS, day=SUNDAY, default_sport=Sport.NFL,
                               games_for=games_for, roster_for=roster_for).legs[0]
    assert leg.line == D("60.25") and "line" in leg.doubts


@pytest.mark.parametrize(("text", "sgp"), [
    ("Same Game Parlay", True), ("SGP", True), ("Parlay", False), ("3-Leg Parlay", False),
    ("Single", None), (None, None)])
def test_what_the_slip_calls_itself(text, sgp):
    extracted = ExtractedSlip(slip_type_text=text, legs=[ExtractedLeg(market_text="x")])
    slip = resolve.resolve_slip(extracted, books=BOOKS, day=SUNDAY, default_sport=Sport.NFL,
                                games_for=games_for, roster_for=roster_for)
    assert slip.sgp is sgp


@pytest.mark.parametrize(("stake", "placed"), [(10.0, True), (None, False), (0, False)])
def test_a_stake_means_it_was_placed(stake, placed):
    slip = resolve.resolve_slip(
        ExtractedSlip(stake=stake, legs=[ExtractedLeg(market_text="x")]), books=BOOKS,
        day=SUNDAY, default_sport=Sport.NFL, games_for=games_for, roster_for=roster_for)
    assert slip.placed is placed


# --- ESPN unavailable -----------------------------------------------------------------------


@pytest.mark.parametrize("error", [
    FetchError("u", FailureKind.TRANSIENT, "down"), RateLimited("espn.com", 3.0)])
def test_espn_being_down_leaves_the_game_blank_and_highlighted(error):
    def down(*_):
        raise error

    slip = resolve_reply("single_valid.json", games=down)
    (leg,) = slip.legs
    assert leg.game is None and {"game", "player"} <= leg.doubts
    assert leg.market is M.PLAYER_RECEIVING_YARDS and leg.line == D("70.5")  # the rest survives
    assert slip.book_id == 1


def test_a_roster_failing_leaves_only_the_player_blank():
    def down(*_):
        raise FetchError("u", FailureKind.TRANSIENT, "down")

    (leg,) = resolve_reply("single_valid.json", rosters=down).legs
    assert leg.game is not None and leg.athlete_id is None and "player" in leg.doubts


def test_one_days_scoreboard_failing_does_not_hide_the_others():
    def flaky(sport, day):
        if day == SUNDAY:
            raise FetchError("u", FailureKind.TRANSIENT, "down")
        return games_for(sport, day)

    (leg,) = resolve_reply("fenced.txt", day=SUNDAY, games=flaky).legs
    assert leg.game.espn_event_id == PHI_CHI.espn_event_id


# --- The four markets added after the first real slips ---------------------------------------


@pytest.mark.parametrize(("text", "market"), [
    ("PASS COMPLETIONS", M.PLAYER_PASS_COMPLETIONS),
    ("Passing Completions", M.PLAYER_PASS_COMPLETIONS),      # "passing" must not read as yards
    ("Completions", M.PLAYER_PASS_COMPLETIONS),
    ("INTERCEPTIONS", M.PLAYER_INTERCEPTIONS),
    ("Passing Interceptions", M.PLAYER_INTERCEPTIONS),
    ("FIELD GOALS MADE", M.PLAYER_FIELD_GOALS),
    ("Field Goals", M.PLAYER_FIELD_GOALS),
    ("ANYTIME TD", M.PLAYER_TOUCHDOWNS),
    ("Anytime Touchdown Scorer", M.PLAYER_TOUCHDOWNS),
    ("TO SCORE 2+ TDS", M.PLAYER_TOUCHDOWNS),
    ("Player Touchdowns", M.PLAYER_TOUCHDOWNS),
    ("TO RECORD 65+ RUSHING YARDS", M.PLAYER_RUSHING_YARDS),  # still yards
])
def test_the_new_markets_resolve_from_the_wording_hard_rock_prints(text, market):
    assert resolve.resolve_market(text, Sport.NFL, True) == resolve.ResolvedMarket(market)


@pytest.mark.parametrize("text", [
    "Passing Touchdowns", "First TD Scorer", "Last Touchdown", "Under 1.5 Touchdowns",
])
def test_touchdown_wording_that_is_not_any_non_passing_td_is_other(text):
    assert resolve.resolve_market(text, Sport.NFL, True) == resolve.ResolvedMarket(M.OTHER)


@pytest.mark.parametrize(("market_text", "printed", "line"), [
    ("ANYTIME TD", None, "0.5"),                       # no line printed: Over 0.5
    ("Anytime Touchdown Scorer", None, "0.5"),
    ("TO SCORE 2+ TDS", 2, "1.5"),                     # a ladder: one half less
    ("TO RECORD 100+ RUSHING YARDS", 100, "99.5"),
    ("TO RECORD 65+ RUSHING YARDS", 64.5, "64.5"),     # already the Over line: unchanged
    ("Rushing Yards", 64.5, "64.5"),
    ("Rushing Yards", None, None),
])
def test_the_line_a_slip_means(market_text, printed, line):
    got = resolve.implied_line(market_text, printed)
    assert got == (None if line is None else D(line))
