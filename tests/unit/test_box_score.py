"""The summary / cdn box-score parser against real recorded games (SPEC.md section 6.1)."""
import copy
import json
from decimal import Decimal as D
from pathlib import Path

import pytest

from parlaytracker.core.models import EventStatus, MarketType as M, Sport
from parlaytracker.ingest import espn

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "espn"


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


def names(payload) -> dict[str, str]:
    return {a["athlete"]["displayName"]: a["athlete"]["id"]
            for t in payload["boxscore"]["players"] for s in t["statistics"]
            for a in s["athletes"]}


def value(box: espn.BoxScore, payload, market: M, player: str) -> D | None:
    return box.stats[market].get(names(payload)[player])


@pytest.fixture(scope="module")
def nfl_payload():
    return load("nfl_summary_401872958_final.json")


@pytest.fixture(scope="module")
def nfl(nfl_payload) -> espn.BoxScore:
    return espn.parse_box_score(Sport.NFL, nfl_payload)


def test_nfl_final_box_score(nfl):
    assert nfl.espn_event_id == "401872958"
    assert nfl.status is EventStatus.FINAL
    assert (nfl.home_espn_team_id, nfl.home_score) == ("25", 36)  # SF
    assert (nfl.away_espn_team_id, nfl.away_score) == ("22", 30)  # ARI


def test_nfl_stats_are_read_by_column_key(nfl, nfl_payload):
    assert value(nfl, nfl_payload, M.PLAYER_RECEPTIONS, "Trey McBride") == 9
    assert value(nfl, nfl_payload, M.PLAYER_RECEIVING_YARDS, "Trey McBride") == 75
    assert value(nfl, nfl_payload, M.PLAYER_RUSHING_YARDS, "Christian McCaffrey") == 75
    assert value(nfl, nfl_payload, M.PLAYER_PASSING_YARDS, "Jacoby Brissett") == 280
    assert isinstance(value(nfl, nfl_payload, M.PLAYER_RECEPTIONS, "George Kittle"), D)


def test_a_targeted_player_with_no_catches_is_a_real_zero(nfl, nfl_payload):
    assert value(nfl, nfl_payload, M.PLAYER_RECEPTIONS, "Jalen Brooks") == 0
    assert value(nfl, nfl_payload, M.PLAYER_RECEIVING_YARDS, "Kyle Juszczyk") == 0


def test_a_player_who_never_appears_in_the_table_has_no_value(nfl, nfl_payload):
    # Jacoby Brissett threw the ball but has no receiving line: absent, which is neither zero
    # nor void (section 6.1).
    assert value(nfl, nfl_payload, M.PLAYER_RECEPTIONS, "Jacoby Brissett") is None


def test_overtime_game_is_final():
    box = espn.parse_box_score(Sport.NFL, load("nfl_summary_401872923_overtime.json"))
    assert (box.status, box.home_score, box.away_score) == (EventStatus.FINAL, 31, 30)


def test_the_cdn_wrapper_gives_the_same_box_score(nfl):
    assert espn.parse_box_score(Sport.NFL, load("nfl_cdn_game_401872958.json")) == nfl


def test_nba_points_and_players_who_did_not_play():
    payload = load("nba_summary_401810723_final.json")
    box = espn.parse_box_score(Sport.NBA, payload)
    assert (box.status, box.home_score, box.away_score) == (EventStatus.FINAL, 114, 89)
    assert value(box, payload, M.PLAYER_POINTS, "Victor Wembanyama") == 25
    clarkson = names(payload)["Jordan Clarkson"]
    assert clarkson in box.did_not_play
    assert clarkson not in box.stats[M.PLAYER_POINTS]


def test_nhl_points_are_goals_plus_assists_for_forwards_and_defensemen():
    payload = load("nhl_summary_401803298_final.json")
    box = espn.parse_box_score(Sport.NHL, payload)
    assert (box.home_score, box.away_score) == (5, 0)
    assert value(box, payload, M.PLAYER_POINTS, "Bryan Rust") == 2   # a forward
    assert value(box, payload, M.PLAYER_POINTS, "Kris Letang") == 1   # a defenseman
    assert value(box, payload, M.PLAYER_POINTS, "Evgeni Malkin") == 1


def test_nhl_overtime_is_final():
    box = espn.parse_box_score(Sport.NHL, load("nhl_summary_401803301_overtime.json"))
    assert (box.status, box.home_score, box.away_score) == (EventStatus.FINAL, 2, 1)


def test_mlb_has_no_player_markets(nfl_payload):
    box = espn.parse_box_score(Sport.MLB, nfl_payload)  # the shape is what matters
    assert box.stats == {}


# --- Documents that don't fit ---------------------------------------------------------------


def broken(mutate) -> dict:
    payload = copy.deepcopy(load("nfl_summary_401872958_final.json"))
    mutate(payload)
    return payload


@pytest.mark.parametrize("mutate", [
    lambda p: p.pop("header"),
    lambda p: p.pop("boxscore"),
    lambda p: p["header"]["competitions"].clear(),
    lambda p: p["header"]["competitions"][0]["competitors"].pop(),
    # A renamed column: the key must be found by name, never by position.
    lambda p: p["boxscore"]["players"][0]["statistics"][2]["keys"].remove("receptions"),
    # A row that doesn't line up with its columns.
    lambda p: p["boxscore"]["players"][0]["statistics"][2]["athletes"][0]["stats"].pop(),
    lambda p: p["boxscore"]["players"][0]["statistics"][2]["athletes"][0]["stats"].__setitem__(
        0, "n/a"),
])
def test_a_changed_format_is_a_schema_error(mutate):
    with pytest.raises(espn.SchemaError):
        espn.parse_box_score(Sport.NFL, broken(mutate))


def test_not_a_document_at_all():
    for payload in (None, [], "<html>Access Denied</html>", {"gamepackageJSON": {}}):
        with pytest.raises(espn.SchemaError):
            espn.parse_box_score(Sport.NFL, payload)


def test_columns_reordered_still_parse_correctly():
    def reorder(p):
        for team in p["boxscore"]["players"]:
            for group in team["statistics"]:
                if group["name"] == "receiving":
                    group["keys"].reverse()
                    for a in group["athletes"]:
                        a["stats"].reverse()

    payload = broken(reorder)
    box = espn.parse_box_score(Sport.NFL, payload)
    assert value(box, payload, M.PLAYER_RECEPTIONS, "Trey McBride") == 9
    assert value(box, payload, M.PLAYER_RECEIVING_YARDS, "Trey McBride") == 75


# --- Completions, touchdowns, interceptions, field goals (read off the same recorded game) ----


def test_completions_are_the_first_number_of_a_made_over_attempted_column(nfl, nfl_payload):
    assert value(nfl, nfl_payload, M.PLAYER_PASS_COMPLETIONS, "Jacoby Brissett") == 38  # 38/52
    assert value(nfl, nfl_payload, M.PLAYER_PASS_COMPLETIONS, "Brock Purdy") == 15      # 15/27


def test_interceptions_are_the_ones_a_passer_threw(nfl, nfl_payload):
    assert value(nfl, nfl_payload, M.PLAYER_INTERCEPTIONS, "Jacoby Brissett") == 0
    # Someone in the defensive "interceptions" table is not a passer: no value.
    assert value(nfl, nfl_payload, M.PLAYER_INTERCEPTIONS, "Christian McCaffrey") is None


def test_field_goals_made_are_the_first_number_of_made_over_attempted(nfl, nfl_payload):
    assert value(nfl, nfl_payload, M.PLAYER_FIELD_GOALS, "Chad Ryland") == 3    # 3/3
    assert value(nfl, nfl_payload, M.PLAYER_FIELD_GOALS, "Eddy Pineiro") == 1   # 1/1


def test_touchdowns_add_up_across_tables_and_leave_passing_ones_out(nfl, nfl_payload):
    tds = lambda who: value(nfl, nfl_payload, M.PLAYER_TOUCHDOWNS, who)  # noqa: E731
    assert tds("George Kittle") == 2            # receiving 2
    assert tds("Christian McCaffrey") == 1      # rushing 1 + receiving 0
    assert tds("Deebo Samuel Sr.") == 1         # rushing 0 + receiving 1 + kick return 0
    assert tds("Jacoby Brissett") == 1          # a rushing TD; he threw 2 more, which don't count
    assert tds("Tyler Allgeier") == 0           # listed, no touchdowns: a real zero


def test_a_player_in_no_touchdown_table_has_no_value(nfl, nfl_payload):
    assert value(nfl, nfl_payload, M.PLAYER_TOUCHDOWNS, "Chad Ryland") is None


def test_a_missing_field_goal_column_is_a_schema_error(nfl_payload):
    broken = copy.deepcopy(nfl_payload)
    for team in broken["boxscore"]["players"]:
        for group in team["statistics"]:
            if group["name"] == "kicking":
                group["keys"][0] = "fieldGoalsRenamed"
    with pytest.raises(espn.SchemaError):
        espn.parse_box_score(Sport.NFL, broken)
