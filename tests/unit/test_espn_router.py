"""The ESPN router and its fault matrix (SPEC.md sections 8.3 and 11)."""
import copy
import json
from datetime import date
from pathlib import Path

import httpx
import pytest
import respx

from parlaytracker.core.models import DataSource, EventStatus, FailureKind, Sport
from parlaytracker.ingest.http import RateLimited, RateLimiter
from parlaytracker.ingest.router import (
    AllProvidersFailed,
    Breakers,
    EspnRouter,
    SampleRecord,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "espn"
GAME = "401872958"
WEB = f"https://site.web.api.espn.com/apis/site/v2/sports/football/nfl/summary?event={GAME}"
SITE = f"https://site.api.espn.com/apis/site/v2/sports/football/nfl/summary?event={GAME}"
CDN = "https://cdn.espn.com/core/nfl/game"
URLS = {"espn_web": WEB, "espn_site": SITE, "espn_cdn": CDN}
ORDER = ["espn_web", "espn_site", "espn_cdn"]


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


SUMMARY = load("nfl_summary_401872958_final.json")
CDN_DOC = load("nfl_cdn_game_401872958.json")


def implausible():
    doc = copy.deepcopy(SUMMARY)
    rec = next(s for s in doc["boxscore"]["players"][0]["statistics"] if s["name"] == "receiving")
    rec["athletes"][0]["stats"][1] = "999"  # 999 receiving yards
    return doc


def route(url: str):
    """respx matches on path and query; the cdn URL varies only by params."""
    return respx.get(url.split("?")[0], params={"event": GAME} if "event=" in url
                     else {"xhr": "1", "gameId": GAME})


def ok_response(provider: str) -> httpx.Response:
    return httpx.Response(200, json=CDN_DOC if provider == "espn_cdn" else SUMMARY)


# name -> (what the provider does, expected kind, seconds the breaker opens, opens at once?)
FAILURES = {
    "500": (lambda: httpx.Response(500), FailureKind.TRANSIENT),
    "timeout": (lambda: httpx.ConnectTimeout("slow"), FailureKind.TRANSIENT),
    "truncated_json": (lambda: httpx.Response(200, text='{"header": {"id": "40'),
                       FailureKind.TRANSIENT),
    "akamai_403": (lambda: httpx.Response(403, text=(FIXTURES / "akamai_403.html").read_text(),
                                          headers={"Server": "AkamaiGHost"}),
                   FailureKind.BLOCKED),
    "429": (lambda: httpx.Response(429), FailureKind.THROTTLED),
    "429_retry_after": (lambda: httpx.Response(429, headers={"Retry-After": "42"}),
                        FailureKind.THROTTLED),
    "changed_format": (lambda: httpx.Response(200, json={"header": {}, "boxscore": []}),
                       FailureKind.SCHEMA),
    "implausible_value": (lambda: httpx.Response(200, json=implausible()),
                          FailureKind.IMPLAUSIBLE),
}
OPEN_SECONDS = {"akamai_403": 600, "429": 300, "429_retry_after": 42,
                "changed_format": 1800, "implausible_value": 1800}


def inject(provider: str, failure: str) -> None:
    what = FAILURES[failure][0]()
    mock = route(URLS[provider])
    if isinstance(what, Exception):
        mock.mock(side_effect=what)
    else:
        mock.mock(return_value=what)


@pytest.fixture
def samples() -> list[SampleRecord]:
    return []


@pytest.fixture
def router(samples) -> EspnRouter:
    limiter = RateLimiter(per_host_interval=0, per_minute=10_000)
    return EspnRouter(Breakers(engine=None), limiter, samples.append)


def open_earlier_providers(router: EspnRouter, provider: str) -> None:
    """Make `provider` the first one tried, by opening the breakers in front of it."""
    for earlier in ORDER[:ORDER.index(provider)]:
        router.breakers.failure(earlier, FailureKind.BLOCKED, "test setup")


# --- The fault matrix: every provider x every failure ---------------------------------------


@pytest.mark.parametrize("failure", FAILURES)
@pytest.mark.parametrize("provider", ORDER)
@respx.mock
def test_fault_matrix(router, samples, provider, failure):
    open_earlier_providers(router, provider)
    inject(provider, failure)
    later = ORDER[ORDER.index(provider) + 1:]
    for name in later:
        route(URLS[name]).mock(return_value=ok_response(name))
    kind = FAILURES[failure][1]

    if later:
        # The router moves to the next provider in the same run...
        routed = router.box_score(Sport.NFL, GAME)
        assert routed.provider is DataSource(later[0])
        assert routed.value.status is EventStatus.FINAL
    else:
        # ...and when there is no next provider the caller is told why, not given bad data.
        with pytest.raises(AllProvidersFailed) as info:
            router.box_score(Sport.NFL, GAME)
        assert info.value.kind is kind
        assert provider in info.value.errors

    breaker = router.breakers[provider]
    assert breaker.consecutive_failures == 1
    assert breaker.last_error
    if kind is FailureKind.TRANSIENT:
        assert breaker.allow()  # a single transient failure doesn't open it: three do
    else:
        assert not breaker.allow() and breaker.failure_kind is kind
        assert (breaker.open_until - breaker.last_failure_at).total_seconds() == \
            OPEN_SECONDS[failure]
    # Only a format change or an implausible value is worth keeping the body for.
    kept = [s for s in samples if s.source == provider]
    if kind in (FailureKind.SCHEMA, FailureKind.IMPLAUSIBLE):
        assert len(kept) == 1 and kept[0].reason == "failure" and kept[0].body
    else:
        assert kept == []


@respx.mock
def test_three_transient_failures_open_the_breaker_for_thirty_seconds(router):
    route(WEB).mock(return_value=httpx.Response(500))
    route(SITE).mock(return_value=httpx.Response(200, json=SUMMARY))
    for _ in range(3):
        assert router.box_score(Sport.NFL, GAME).provider is DataSource.ESPN_SITE
    web = router.breakers["espn_web"]
    assert not web.allow()
    assert (web.open_until - web.last_failure_at).total_seconds() == 30


@respx.mock
def test_the_open_provider_is_skipped_without_a_request(router):
    router.breakers.failure("espn_web", FailureKind.BLOCKED, "403")
    web = route(WEB).mock(return_value=httpx.Response(200, json=SUMMARY))
    route(SITE).mock(return_value=httpx.Response(200, json=SUMMARY))
    assert router.box_score(Sport.NFL, GAME).provider is DataSource.ESPN_SITE
    assert web.call_count == 0


@respx.mock
def test_the_primary_is_used_again_once_its_trial_succeeds(router):
    router.breakers.failure("espn_web", FailureKind.BLOCKED, "403")
    route(WEB).mock(return_value=httpx.Response(200, json=SUMMARY))
    route(SITE).mock(return_value=httpx.Response(200, json=SUMMARY))
    assert router.box_score(Sport.NFL, GAME).provider is DataSource.ESPN_SITE  # stays on fallback
    router.breakers["espn_web"].open_until = router.breakers["espn_web"].last_failure_at
    assert router.box_score(Sport.NFL, GAME).provider is DataSource.ESPN_WEB  # trial succeeds
    assert router.breakers["espn_web"].allow() and router.breakers["espn_web"].open_until is None


@respx.mock
def test_a_failed_trial_reopens_for_the_next_duration(router):
    router.breakers.failure("espn_web", FailureKind.BLOCKED, "403")
    router.breakers["espn_web"].open_until = router.breakers["espn_web"].last_failure_at
    route(WEB).mock(return_value=httpx.Response(403, text="<html>Access Denied</html>"))
    route(SITE).mock(return_value=httpx.Response(200, json=SUMMARY))
    assert router.box_score(Sport.NFL, GAME).provider is DataSource.ESPN_SITE
    web = router.breakers["espn_web"]
    assert not web.allow()
    assert (web.open_until - web.last_failure_at).total_seconds() == 600


@respx.mock
def test_every_provider_failing_says_why_for_each(router):
    for name in ORDER:
        route(URLS[name]).mock(return_value=httpx.Response(500))
    with pytest.raises(AllProvidersFailed) as info:
        router.box_score(Sport.NFL, GAME)
    assert set(info.value.errors) == set(ORDER)


@respx.mock
def test_all_providers_open_is_reported_without_any_request(router):
    for name in ORDER:
        router.breakers.failure(name, FailureKind.BLOCKED, "403")
    with pytest.raises(AllProvidersFailed) as info:
        router.box_score(Sport.NFL, GAME)
    assert info.value.kind is None  # nothing was tried, so nothing new failed


@respx.mock
def test_rate_limited_requests_are_skipped_not_counted_as_failures(samples):
    tight = EspnRouter(Breakers(engine=None), RateLimiter(per_host_interval=60, per_minute=1000),
                       samples.append)
    route(WEB).mock(return_value=httpx.Response(200, json=SUMMARY))
    route(SITE).mock(return_value=httpx.Response(200, json=SUMMARY))
    route(CDN).mock(return_value=httpx.Response(200, json=CDN_DOC))
    assert tight.box_score(Sport.NFL, GAME).provider is DataSource.ESPN_WEB
    # web's slot is used up; site's is free, so the router falls through to it
    assert tight.box_score(Sport.NFL, GAME).provider is DataSource.ESPN_SITE
    assert tight.box_score(Sport.NFL, GAME).provider is DataSource.ESPN_CDN
    with pytest.raises(RateLimited):
        tight.box_score(Sport.NFL, GAME)
    assert all(tight.breakers[n].consecutive_failures == 0 for n in ORDER)


@respx.mock
def test_the_canary_can_ask_one_provider_only(router):
    web = route(WEB).mock(return_value=httpx.Response(200, json=SUMMARY))
    cdn = route(CDN).mock(return_value=httpx.Response(200, json=CDN_DOC))
    box = router.box_score(Sport.NFL, GAME, only=DataSource.ESPN_CDN).value
    assert box.home_score == 36 and cdn.call_count == 1 and web.call_count == 0
    route(CDN).mock(return_value=httpx.Response(500))
    with pytest.raises(AllProvidersFailed):  # no fallback for a canary
        router.box_score(Sport.NFL, GAME, only=DataSource.ESPN_CDN)
    assert web.call_count == 0


# --- Scoreboards ----------------------------------------------------------------------------

BOARD = "https://site.web.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"


@respx.mock
def test_scoreboard_with_one_bad_event_still_returns_the_others(router):
    board = load("nfl_scoreboard_2026-09-27_final.json")
    board["events"][3]["competitions"] = []  # one malformed event
    respx.get(BOARD).mock(return_value=httpx.Response(200, json=board))
    result = router.scoreboard(Sport.NFL, date(2026, 9, 27)).value
    assert len(result.games) == 13 and len(result.errors) == 1


@respx.mock
def test_an_implausible_score_rejects_the_whole_scoreboard(router, samples):
    board = load("nfl_scoreboard_2026-09-27_final.json")
    board["events"][0]["competitions"][0]["competitors"][0]["score"] = "240"
    respx.get(BOARD).mock(return_value=httpx.Response(200, json=board))
    respx.get(BOARD.replace("site.web.api", "site.api")).mock(
        return_value=httpx.Response(200, json=board))
    with pytest.raises(AllProvidersFailed) as info:
        router.scoreboard(Sport.NFL, date(2026, 9, 27))
    assert info.value.kind is FailureKind.IMPLAUSIBLE
    assert len(samples) == 2  # both providers served it, both bodies kept


# --- Recording ------------------------------------------------------------------------------


@respx.mock
def test_recording_saves_every_response_for_watched_events(samples):
    watching = EspnRouter(Breakers(engine=None), RateLimiter(0, 1000), samples.append,
                          record_event_ids={GAME})
    route(WEB).mock(return_value=httpx.Response(200, json=SUMMARY))
    watching.box_score(Sport.NFL, GAME)
    assert [(s.reason, s.espn_event_id, s.source) for s in samples] == [
        ("recording", GAME, "espn_web")]
    assert json.loads(samples[0].body)["header"]["id"] == GAME


@respx.mock
def test_other_events_are_not_recorded(router, samples):
    route(WEB).mock(return_value=httpx.Response(200, json=SUMMARY))
    router.box_score(Sport.NFL, GAME)
    assert samples == []
