"""ESPN parsers against recorded fixtures, and fetching with host failover (SPEC.md 6.1)."""
import json
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
import pytest
import respx

from parlaytracker.core.models import DataSource, EventStatus, FailureKind, Sport
from parlaytracker.ingest import espn
from parlaytracker.ingest.http import FetchError, RateLimiter

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "espn"


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


def games(sport: Sport, name: str) -> dict[str, espn.Game]:
    result = espn.parse_scoreboard(sport, load(name))
    assert result.errors == {}
    return {g.espn_event_id: g for g in result.games}


# --- Scoreboards ---------------------------------------------------------------------------


def test_nfl_final_scoreboard():
    by_id = games(Sport.NFL, "nfl_scoreboard_2026-09-27_final.json")
    assert len(by_id) == 14
    g = by_id["401872958"]
    assert (g.label, g.away_score, g.home_score) == ("ARI @ SF", 30, 36)
    assert (g.home.espn_id, g.home.name) == ("25", "San Francisco 49ers")
    assert g.status is EventStatus.FINAL
    assert g.start_time == datetime(2026, 9, 27, 20, 5, tzinfo=UTC)


def test_sunday_night_game_belongs_to_sunday():
    g = games(Sport.NFL, "nfl_scoreboard_2026-09-27_final.json")["401872962"]
    assert g.start_time == datetime(2026, 9, 28, 0, 20, tzinfo=UTC)
    assert espn.game_day(g.start_time) == date(2026, 9, 27)


def test_overtime_final():
    g = games(Sport.NFL, "nfl_scoreboard_2026-09-13_overtime.json")["401872923"]
    assert (g.status, g.period, g.status_detail) == (EventStatus.FINAL, 5, "Final/OT")


def test_scheduled_game_has_no_scores():
    g = games(Sport.NFL, "nfl_scoreboard_2026-09-28_scheduled.json")["401872963"]
    assert g.status is EventStatus.SCHEDULED
    assert (g.home_score, g.away_score) == (None, None)
    assert g.label == "PHI @ CHI"


def test_postponed_is_not_final():
    by_id = games(Sport.MLB, "mlb_scoreboard_2026-05-05_postponed.json")
    postponed = [g for g in by_id.values() if g.status is EventStatus.POSTPONED]
    assert len(postponed) == 2
    assert "401815223" in {g.espn_event_id for g in postponed}


@pytest.mark.parametrize(
    ("sport", "name", "count"),
    [(Sport.NBA, "nba_scoreboard_2026-03-01.json", 11),
     (Sport.NHL, "nhl_scoreboard_2026-03-01.json", 6)],
)
def test_other_sports_parse(sport, name, count):
    by_id = games(sport, name)
    assert len(by_id) == count
    assert all(g.sport is sport and g.status is EventStatus.FINAL for g in by_id.values())


def test_games_are_sorted_by_start_time():
    result = espn.parse_scoreboard(Sport.NFL, load("nfl_scoreboard_2026-09-27_final.json"))
    starts = [g.start_time for g in result.games]
    assert starts == sorted(starts)


def test_a_broken_event_does_not_stop_the_others():
    payload = load("nfl_scoreboard_2026-09-27_final.json")
    del payload["events"][0]["competitions"]
    payload["events"][1]["status"]["type"]["state"] = "sideways"
    result = espn.parse_scoreboard(Sport.NFL, payload)
    assert len(result.games) == 12
    assert set(result.errors) == {payload["events"][0]["id"], payload["events"][1]["id"]}


@pytest.mark.parametrize("payload", [None, [], {"leagues": []}, {"events": "none"}])
def test_malformed_scoreboard_is_a_schema_error(payload):
    with pytest.raises(espn.SchemaError):
        espn.parse_scoreboard(Sport.NFL, payload)


@pytest.mark.parametrize(
    ("name", "state", "completed", "expected"),
    [
        ("STATUS_SCHEDULED", "pre", False, EventStatus.SCHEDULED),
        ("STATUS_IN_PROGRESS", "in", False, EventStatus.IN_PROGRESS),
        ("STATUS_HALFTIME", "in", False, EventStatus.BREAK),
        ("STATUS_END_PERIOD", "in", False, EventStatus.BREAK),
        ("STATUS_DELAYED", "in", False, EventStatus.DELAYED),
        ("STATUS_RAIN_DELAY", "pre", False, EventStatus.DELAYED),
        ("STATUS_FINAL", "post", True, EventStatus.FINAL),
        ("STATUS_FINAL_PEN", "post", True, EventStatus.FINAL),
        ("STATUS_POSTPONED", "post", False, EventStatus.POSTPONED),
        ("STATUS_CANCELED", "post", False, EventStatus.CANCELLED),
        ("STATUS_SOMETHING_NEW", "in", False, EventStatus.IN_PROGRESS),
        ("STATUS_SOMETHING_NEW", "post", False, EventStatus.POSTPONED),
    ],
)
def test_status_mapping(name, state, completed, expected):
    assert espn.map_status(name, state, completed) is expected


# --- Rosters -------------------------------------------------------------------------------


def test_nfl_roster_is_grouped_and_puts_unavailable_players_last():
    players = espn.parse_roster(load("nfl_roster_22.json"))
    assert len(players) == 82
    assert {p.group for p in players} >= {"offense", "defense", "injuredReserveOrOut"}
    flags = [p.unavailable for p in players]
    assert flags == sorted(flags)  # available first
    assert all(p.unavailable for p in players if p.group == "injuredReserveOrOut")


def test_nba_roster_is_a_flat_list():
    players = espn.parse_roster(load("nba_roster_18.json"))
    assert len(players) == 17
    assert {p.group for p in players} == {""}
    assert all(p.position for p in players)


@pytest.mark.parametrize("name", ["nhl_roster_16.json", "mlb_roster_10.json"])
def test_grouped_rosters_for_other_sports(name):
    players = espn.parse_roster(load(name))
    assert players and all(p.espn_athlete_id and p.name for p in players)
    assert len({p.espn_athlete_id for p in players}) == len(players)


def test_malformed_roster_is_a_schema_error():
    with pytest.raises(espn.SchemaError):
        espn.parse_roster({"athletes": [{"items": [{"fullName": "No Id"}]}]})


# --- Fetching with failover ----------------------------------------------------------------

WEB = "https://site.web.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
SITE = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"


@pytest.fixture(autouse=True)
def fresh_limiter(monkeypatch):
    """No waiting in tests: every host gets a fresh limiter with no spacing."""
    monkeypatch.setattr(espn, "LIMITER", RateLimiter(per_host_interval=0, per_minute=1000))


@respx.mock
def test_fetch_uses_primary_host_with_single_date_and_honest_user_agent():
    route = respx.get(WEB).mock(return_value=httpx.Response(
        200, json=load("nfl_scoreboard_2026-09-28_scheduled.json")))
    result = espn.fetch_scoreboard(Sport.NFL, date(2026, 9, 28))
    assert [g.espn_event_id for g in result.games] == ["401872963"]
    request = route.calls.last.request
    assert request.url.params["dates"] == "20260928"
    assert request.headers["User-Agent"] == "ParlayTracker/1.0"


@respx.mock
def test_fetch_falls_back_when_primary_is_blocked():
    respx.get(WEB).mock(return_value=httpx.Response(
        403, text=(FIXTURES / "akamai_403.html").read_text(), headers={"Server": "AkamaiGHost"}))
    respx.get(SITE).mock(return_value=httpx.Response(
        200, json=load("nfl_scoreboard_2026-09-28_scheduled.json")))
    provider, _ = espn.fetch_json("football/nfl/scoreboard", {"dates": "20260928"})
    assert provider is DataSource.ESPN_SITE


@pytest.mark.parametrize(
    ("response", "kind"),
    [
        (httpx.Response(403, text="<HTML>Access Denied</HTML>"), FailureKind.BLOCKED),
        (httpx.Response(429), FailureKind.THROTTLED),
        (httpx.Response(503, headers={"Retry-After": "120"}), FailureKind.THROTTLED),
        (httpx.Response(500), FailureKind.TRANSIENT),
        (httpx.Response(200, text="<html>not json</html>"), FailureKind.TRANSIENT),
        (httpx.Response(200, text='{"events": [tr'), FailureKind.TRANSIENT),
    ],
)
@respx.mock
def test_failures_are_classified(response, kind):
    respx.get(WEB).mock(return_value=response)
    respx.get(SITE).mock(return_value=response)
    with pytest.raises(FetchError) as info:
        espn.fetch_json("football/nfl/scoreboard")
    assert info.value.kind is kind


@respx.mock
def test_retry_after_is_kept():
    respx.get(WEB).mock(return_value=httpx.Response(429, headers={"Retry-After": "90"}))
    respx.get(SITE).mock(return_value=httpx.Response(429, headers={"Retry-After": "90"}))
    with pytest.raises(FetchError) as info:
        espn.fetch_json("football/nfl/scoreboard")
    assert info.value.retry_after == 90


@respx.mock
def test_timeout_is_transient():
    respx.get(WEB).mock(side_effect=httpx.ConnectTimeout("slow"))
    respx.get(SITE).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(FetchError) as info:
        espn.fetch_json("football/nfl/scoreboard")
    assert info.value.kind is FailureKind.TRANSIENT


@respx.mock
def test_changed_format_is_a_schema_failure():
    respx.get(WEB).mock(return_value=httpx.Response(200, json={"games": []}))
    with pytest.raises(FetchError) as info:
        espn.fetch_scoreboard(Sport.NFL, date(2026, 9, 28))
    assert info.value.kind is FailureKind.SCHEMA


@respx.mock
def test_fetch_roster():
    url = "https://site.web.api.espn.com/apis/site/v2/sports/basketball/nba/teams/18/roster"
    respx.get(url).mock(return_value=httpx.Response(200, json=load("nba_roster_18.json")))
    assert len(espn.fetch_roster(Sport.NBA, "18")) == 17
