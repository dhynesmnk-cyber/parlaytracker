"""The Odds API client: quota headers, failure classification and key hygiene (SPEC.md 6.2)."""
import json
import logging
from pathlib import Path

import httpx
import pytest
import respx

from parlaytracker.core.models import FailureKind, Sport
from parlaytracker.ingest.http import FetchError, RateLimited, RateLimiter
from parlaytracker.ingest.odds_api import OddsApiClient, RequestRejected
from parlaytracker.ingest.router import Breakers, ProviderOpen

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "odds_api"
KEY = "test-key-0123456789abcdef"
EVENTS = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl/events"
GAME = "47dc7baa254659f3beb2ed2b38c207b6"
ODDS = f"{EVENTS}/{GAME}/odds"


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def breakers() -> Breakers:
    return Breakers(engine=None)


@pytest.fixture
def client(breakers) -> OddsApiClient:
    # No pauses: the limiter's own behaviour is tested elsewhere.
    return OddsApiClient(KEY, breakers, RateLimiter(per_host_interval=0, per_minute=1000))


def headers(remaining="486", last="14") -> dict[str, str]:
    return {"x-requests-remaining": remaining, "x-requests-last": last}


@respx.mock
def test_events_parse_and_cost_nothing(client, breakers):
    route = respx.get(EVENTS).mock(return_value=httpx.Response(
        200, json=load("nfl_events_2026-09-28.json"), headers=headers("486", "0")))
    events = client.events(Sport.NFL)
    assert events[0].id == GAME
    assert (events[0].home_team, events[0].away_team) == ("Chicago Bears", "Philadelphia Eagles")
    assert route.calls.last.request.url.params["apiKey"] == KEY
    assert (client.quota_remaining, client.last_cost) == (486, 0)
    assert breakers["odds_api"].quota_remaining == 486


@respx.mock
def test_event_odds_sends_the_documented_query_and_reads_the_quota(client):
    route = respx.get(ODDS).mock(return_value=httpx.Response(
        200, json=load("nfl_event_odds_2026-09-28_PHI-CHI.json"), headers=headers("472", "14")))
    odds = client.event_odds(Sport.NFL, GAME, ["totals", "player_receptions"])
    params = route.calls.last.request.url.params
    assert (params["regions"], params["oddsFormat"], params["markets"]) == (
        "us", "american", "totals,player_receptions")
    assert len(odds.bookmakers) == 9
    assert (client.quota_remaining, client.last_cost) == (472, 14)


@respx.mock
def test_a_missing_quota_header_keeps_the_last_known_value(client):
    respx.get(EVENTS).mock(side_effect=[
        httpx.Response(200, json=[], headers=headers("300")),
        httpx.Response(200, json=[]),
    ])
    client.events(Sport.NFL)
    client.events(Sport.NFL)
    assert client.quota_remaining == 300


@respx.mock
@pytest.mark.parametrize(("response", "kind"), [
    (httpx.Response(500), FailureKind.TRANSIENT),
    (httpx.Response(403, text="<html>Access Denied</html>"), FailureKind.BLOCKED),
    (httpx.Response(429, headers={"Retry-After": "30"}), FailureKind.THROTTLED),
    (httpx.Response(200, text="<html>not json"), FailureKind.TRANSIENT),
])
def test_failures_are_classified_and_reach_the_breaker(client, breakers, response, kind):
    respx.get(EVENTS).mock(return_value=response)
    with pytest.raises(FetchError) as e:
        client.events(Sport.NFL)
    assert e.value.kind is kind
    assert breakers["odds_api"].consecutive_failures == 1
    assert breakers["odds_api"].last_error


@respx.mock
def test_a_403_opens_the_breaker_and_the_next_call_is_not_made(client, breakers):
    route = respx.get(EVENTS).mock(return_value=httpx.Response(403))
    with pytest.raises(FetchError):
        client.events(Sport.NFL)
    with pytest.raises(ProviderOpen):
        client.events(Sport.NFL)
    assert route.call_count == 1


@respx.mock
def test_a_timeout_is_transient(client):
    respx.get(EVENTS).mock(side_effect=httpx.ConnectTimeout("slow"))
    with pytest.raises(FetchError) as e:
        client.events(Sport.NFL)
    assert e.value.kind is FailureKind.TRANSIENT


@respx.mock
def test_json_that_does_not_fit_the_model_is_a_schema_failure_with_the_raw_body(
        client, breakers):
    respx.get(ODDS).mock(return_value=httpx.Response(200, json={"id": GAME, "oops": 1}))
    with pytest.raises(FetchError) as e:
        client.event_odds(Sport.NFL, GAME, ["totals"])
    assert e.value.kind is FailureKind.SCHEMA
    assert GAME in e.value.body
    assert breakers["odds_api"].failure_kind is FailureKind.SCHEMA
    assert not breakers["odds_api"].allow()


@respx.mock
def test_a_rejected_market_is_not_the_providers_fault(client, breakers):
    respx.get(ODDS).mock(return_value=httpx.Response(
        422, json={"error_code": "INVALID_MARKET", "message": "bad market"}))
    with pytest.raises(RequestRejected):
        client.event_odds(Sport.NFL, GAME, ["nonsense"])
    assert breakers["odds_api"].consecutive_failures == 0
    assert breakers["odds_api"].allow()


@respx.mock
def test_rate_limiting_skips_the_request_without_a_failure(breakers):
    limited = OddsApiClient(KEY, breakers, RateLimiter(per_host_interval=60, per_minute=1000))
    route = respx.get(EVENTS).mock(return_value=httpx.Response(200, json=[]))
    limited.events(Sport.NFL)
    with pytest.raises(RateLimited):
        limited.events(Sport.NFL)
    assert route.call_count == 1
    assert breakers["odds_api"].consecutive_failures == 0


@respx.mock
def test_the_api_key_never_reaches_logs_or_errors(client, caplog):
    caplog.set_level(logging.DEBUG)
    respx.get(EVENTS).mock(return_value=httpx.Response(500, text="boom"))
    with pytest.raises(FetchError) as e:
        client.events(Sport.NFL)
    assert KEY not in str(e.value) and KEY not in e.value.url
    respx.get(EVENTS).mock(return_value=httpx.Response(200, json=[]))
    client.events(Sport.NFL)
    assert KEY not in caplog.text
    # httpx would log the full URL, key included, at INFO.
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
