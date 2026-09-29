"""Live tracking end to end, in simulation (SPEC.md sections 11 and 13, Phase 7 exit).

The real `EspnRouter`, breakers, rate limiter, `PollNflLive`, `Settle` and Postgres, on a fake
clock, with only the network mocked. The frames are the real scoreboard and box-score JSON
edited to a moment in the game: a synthetic timeline, not a recording of a live game. A real
recording (RECORD_EVENT_IDS) is the remaining Phase 7 step and can be replayed the same way.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
import respx
from support import (
    KICKOFF,
    MCBRIDE,
    SimClock,
    add_event,
    add_slip,
    box_json,
    commit,
    event_of,
    legs_of,
    load,
    receptions,
    scoreboard_json,
    team_total,
    total,
)
from sqlalchemy.orm import Session

from parlaytracker.core import live
from parlaytracker.core.models import DataSource, EventStatus as S, HealthState, LegResult
from parlaytracker.ingest import guards
from parlaytracker.ingest.http import RateLimiter
from parlaytracker.ingest.router import Breakers, EspnRouter
from parlaytracker.worker.live import PollNflLive
from parlaytracker.worker.settle import Settle

pytestmark = pytest.mark.db

BASE = load("espn", "nfl_scoreboard_2026-09-27_final.json")
AKAMAI = (load.__globals__["FIXTURES"] / "espn" / "akamai_403.html").read_text()
WEB, SITE, CDN = DataSource.ESPN_WEB, DataSource.ESPN_SITE, DataSource.ESPN_CDN
HOSTS = {"site.web.api.espn.com": "web", "site.api.espn.com": "site", "cdn.espn.com": "cdn"}


class Espn:
    """The three ESPN hosts, mocked. Each can be `ok` or `blocked` (Akamai's 403 page)."""

    def __init__(self, clock: SimClock):
        self.clock = clock
        self.state = {"web": "ok", "site": "ok", "cdn": "ok"}
        self.board = scoreboard_json(BASE, S.SCHEDULED, 0, 0, 0, 0)
        self.box = box_json(0)
        self.requests: list[tuple[datetime, str, str]] = []

    def _answer(self, request: httpx.Request) -> httpx.Response:
        host = HOSTS[request.url.host]
        kind = "board" if request.url.path.endswith("scoreboard") else "box"
        self.requests.append((self.clock.now, host, kind))
        if self.state[host] != "ok":
            return httpx.Response(403, text=AKAMAI)
        return httpx.Response(200, json=self.board if kind == "board" else self.box)

    def install(self, mock: respx.MockRouter) -> None:
        for host in HOSTS:
            mock.get(host=host).mock(side_effect=self._answer)

    def block(self, *hosts: str) -> None:
        self.state.update({h: "blocked" for h in hosts})

    def unblock(self, *hosts: str) -> None:
        self.state.update({h: "ok" for h in hosts})

    def show(self, status, period, clock, home, away, receptions_=0, others=None) -> None:
        self.board = scoreboard_json(BASE, status, period, clock, home, away, others=others)
        self.box = box_json(receptions_)


@pytest.fixture
def rig(engine, clean):
    clock = SimClock(KICKOFF + timedelta(minutes=10))
    espn = Espn(clock)
    breakers = Breakers(engine, clock=lambda: clock.now)
    router = EspnRouter(breakers, limiter=RateLimiter(2.0, 20, clock=clock.monotonic,
                                                      sleep=clock.sleep))
    job = PollNflLive(engine, router, sleep=clock.sleep)

    def tick():
        start = clock.now
        out = job(start)
        clock.now = start + timedelta(seconds=clock.tick_seconds)
        return out

    commit(engine, lambda s: add_slip(s, add_event(s), [
        total("65.5"), team_total("home", "35.5"), receptions(MCBRIDE, "8.5")]))
    with respx.mock(assert_all_called=False) as mock:
        espn.install(mock)
        yield SimpleNamespace(clock=clock, espn=espn, breakers=breakers, router=router,
                              job=job, tick=tick, engine=engine)


def view(rig):
    """What the Live page would show now: the cards and the unavailable-since time."""
    with Session(rig.engine) as s:
        return live.live_cards(s, rig.clock.now), live.load_espn_unavailable_since(s)


def leg_sources(engine) -> set:
    return {leg.live_source for leg in legs_of(engine)}


# --- Exit criteria ----------------------------------------------------------------------------


def test_blocking_the_primary_host_mid_game_switches_to_the_fallback_within_one_run(rig):
    rig.espn.show(S.IN_PROGRESS, 2, 600, 14, 10, 3)
    for _ in range(4):
        rig.tick()
    assert leg_sources(rig.engine) == {WEB}
    rig.espn.block("web")
    rig.espn.show(S.IN_PROGRESS, 2, 500, 14, 17, 4)
    out = rig.tick()                                    # the very next run
    assert out.scoreboards == 1
    assert leg_sources(rig.engine) == {SITE}
    assert event_of(rig.engine).away_score == 17        # and it has the newest data
    assert not rig.breakers["espn_web"].allow()         # blocked: open for 10 minutes
    assert rig.breakers["espn_site"].state is HealthState.OK


def test_blocking_every_host_turns_the_cards_red_within_five_minutes_and_says_so(rig):
    rig.espn.show(S.IN_PROGRESS, 2, 600, 14, 10, 3)
    for _ in range(4):
        rig.tick()
    cards, unavailable = view(rig)
    assert [c.severity for c in cards] == [live.Severity.OK] and unavailable is None

    blocked_at = rig.clock.now
    rig.espn.block("web", "site", "cdn")
    seen = {}
    while rig.clock.now < blocked_at + timedelta(minutes=6):
        rig.tick()
        (card,), unavailable = view(rig)
        seen[(rig.clock.now - blocked_at).total_seconds()] = (card.severity, unavailable)
    assert seen[60][0] is live.Severity.OK           # still fresh: the last poll was recent
    assert seen[180][0] is live.Severity.AMBER       # more than 2 minutes old
    assert seen[330][0] is live.Severity.RED         # more than 5 minutes old
    # every ESPN breaker is open, so the page shows "Live data unavailable since HH:MM"
    since = seen[330][1]
    assert since is not None and blocked_at <= since <= blocked_at + timedelta(seconds=90)


def test_tracking_recovers_without_a_restart_once_the_hosts_are_unblocked(rig):
    rig.espn.show(S.IN_PROGRESS, 2, 600, 14, 10, 3)
    for _ in range(4):
        rig.tick()
    rig.espn.block("web", "site", "cdn")
    for _ in range(24):                               # twelve minutes of nothing
        rig.tick()
    (card,), unavailable = view(rig)
    assert card.severity is live.Severity.RED and unavailable is not None

    rig.espn.unblock("web", "site", "cdn")
    rig.espn.show(S.IN_PROGRESS, 3, 800, 24, 17, 6)
    recovered = None
    for n in range(1, 60):                            # the same job object, no restart
        rig.tick()
        if event_of(rig.engine).period == 3:
            recovered = n
            break
    assert recovered is not None
    (card,), unavailable = view(rig)
    assert card.severity is live.Severity.OK and unavailable is None
    # the primary works again. The fallbacks were blocked too and are not retried while it
    # answers, so they sit half-open: not failing now (the banner ignores them), and a trial
    # is allowed the moment they are needed.
    assert rig.breakers["espn_web"].state is HealthState.OK
    assert all(rig.breakers[s].allow() for s in ("espn_web", "espn_site", "espn_cdn"))
    assert legs_of(rig.engine)[2].live_value == 6


# --- Request budget ---------------------------------------------------------------------------


def test_a_14_game_sunday_stays_inside_the_request_limits(engine, clean):
    """Every game in play, player props in 5 of them, for 30 minutes (section 6.1)."""
    clock = SimClock(KICKOFF + timedelta(minutes=10))
    espn = Espn(clock)
    ids = [e["id"] for e in BASE["events"]]
    assert len(ids) == 14

    def build(s):
        for n, espn_id in enumerate(ids):
            legs = [total("50.5")] + ([receptions(MCBRIDE, "5.5")] if n < 5 else [])
            add_slip(s, add_event(s, espn_id=espn_id), legs)

    commit(engine, build)
    breakers = Breakers(engine, clock=lambda: clock.now)
    router = EspnRouter(breakers, limiter=RateLimiter(2.0, 20, clock=clock.monotonic,
                                                      sleep=clock.sleep))
    job = PollNflLive(engine, router, sleep=clock.sleep)
    skipped = 0
    with respx.mock(assert_all_called=False) as mock:
        espn.install(mock)
        while clock.now < KICKOFF + timedelta(minutes=40):
            start = clock.now
            elapsed = (start - KICKOFF).total_seconds()
            # every game's clock runs and its score creeps up: a slate in play, not a frozen one
            espn.show(S.IN_PROGRESS, 2, int(1200 - elapsed / 2), int(elapsed // 90),
                      int(elapsed // 120), int(elapsed // 400), others=S.IN_PROGRESS)
            skipped += job(start).skipped
            clock.now = start + timedelta(seconds=30)

    times = [(when - KICKOFF).total_seconds() for when, _, _ in espn.requests]
    assert skipped == 0                                    # the rate limiter never had to refuse
    for i, when in enumerate(times):                        # never more than 20 in any minute
        assert sum(1 for t in times[i:] if t < when + 60) <= 20
    per_host = {}
    for when, host, _ in espn.requests:
        last = per_host.get(host)
        assert last is None or (when - last).total_seconds() >= 2.0, host  # 1 per 2 s per host
        per_host[host] = when
    minutes = (KICKOFF + timedelta(minutes=40) - (KICKOFF + timedelta(minutes=10))).seconds / 60
    assert len(times) / minutes <= 8       # the spec's worst case is about 7 a minute
    assert sum(1 for _, _, kind in espn.requests if kind == "board") == pytest.approx(
        2 * minutes, abs=2)                                 # one scoreboard a tick, not one a game


# --- A whole game, replayed -------------------------------------------------------------------

# seconds after kickoff: Q1 0-2400, Q2 2400-4800, halftime 4800-5580, Q3 5580-7980,
# Q4 7980-10380, final after. The clock stands still in Q3 from 6000 to 6400 (a review).
Q = {1: 0, 2: 2400, 3: 5580, 4: 7980}
HALF = (4800, 5580)
REVIEW = (6000, 6400)
END = 10380


def frame(elapsed: float):
    """(status, period, clock, home, away, receptions) at `elapsed` seconds after kickoff."""
    progress = min(1.0, elapsed / END)
    home, away, rec = int(36 * progress), int(30 * progress), int(9 * progress)
    if elapsed >= END:
        return S.FINAL, 4, 0, 36, 30, 9
    if HALF[0] <= elapsed < HALF[1]:
        return S.BREAK, 2, 0, home, away, rec
    period = max(p for p, start in Q.items() if elapsed >= start)
    at = min(max(elapsed, Q[period]), REVIEW[0]) if REVIEW[0] <= elapsed < REVIEW[1] \
        else elapsed
    clock = 900 - int(900 * (at - Q[period]) / 2400)
    return S.IN_PROGRESS, period, clock, home, away, rec


def test_a_whole_game_replayed_with_faults(rig):
    rig.clock.now = KICKOFF
    web_blocked = (3000, 4200)
    keys, sources, probes, switched = [], {}, 0, 0
    web_states = {}
    while (rig.clock.now - KICKOFF).total_seconds() < END + 120:
        elapsed = (rig.clock.now - KICKOFF).total_seconds()
        rig.espn.show(*frame(elapsed))
        if web_blocked[0] <= elapsed < web_blocked[1]:
            rig.espn.block("web")
        else:
            rig.espn.unblock("web")
        out = rig.tick()
        probes, switched = probes + out.probes, switched + out.switched
        e = event_of(rig.engine)
        keys.append((elapsed, guards.progress_key(e.sport, e.status, e.period, e.clock_seconds)))
        sources[elapsed] = next(iter(leg_sources(rig.engine)), None)
        web_states[elapsed] = rig.breakers["espn_web"].state

    # live values never go backwards in progress, however the responses arrive
    ordered = [k for _, k in keys if k is not None]
    assert ordered == sorted(ordered)
    # halftime and the stopped clock never cause a provider switch
    assert switched == 0
    # mid-game failover to the fallback, and back to the primary once its breaker recovers
    assert sources[3060] is SITE and sources[3900] is SITE
    assert sources[4500] is WEB and sources[9000] is WEB
    # the game ended and was found final
    event = event_of(rig.engine)
    assert event.status is S.FINAL and event.final_at is not None
    assert all(leg.result is LegResult.PENDING for leg in legs_of(rig.engine))  # live never settles

    # settlement: nothing before the ten-minute gate, then the right results
    settle = Settle(rig.engine, rig.router)
    assert settle(event.final_at + timedelta(minutes=9)).settled == 0
    assert settle(event.final_at + timedelta(minutes=10)).settled == 3
    assert [leg.result for leg in legs_of(rig.engine)] == [LegResult.WIN] * 3
    assert [leg.final_value for leg in legs_of(rig.engine)] == [66, 36, 9]

    # and the whole replay respected the request limits
    times = [(when - KICKOFF).total_seconds() for when, _, _ in rig.espn.requests]
    for i, when in enumerate(times):
        assert sum(1 for t in times[i:] if t < when + 60) <= 20


def test_the_replay_frames_are_a_plausible_game():
    assert frame(0)[:3] == (S.IN_PROGRESS, 1, 900)
    assert frame(2000)[0] is S.IN_PROGRESS and frame(4900)[0] is S.BREAK
    assert frame(6100)[2] == frame(6300)[2]           # the clock stands still in the review
    assert frame(END) == (S.FINAL, 4, 0, 36, 30, 9)
    assert [frame(e)[3] for e in range(0, END, 600)] == sorted(frame(e)[3] for e in
                                                                range(0, END, 600))


def test_a_whole_slate_looking_frozen_costs_one_probe_a_day_not_one_per_game(engine, clean):
    clock = SimClock(KICKOFF + timedelta(minutes=10))
    espn = Espn(clock)
    espn.show(S.IN_PROGRESS, 2, 600, 14, 10, 3, others=S.IN_PROGRESS)  # nothing ever moves
    ids = [e["id"] for e in BASE["events"]]
    commit(engine, lambda s: [add_slip(s, add_event(s, espn_id=i), [total("50.5")]) for i in ids])
    breakers = Breakers(engine, clock=lambda: clock.now)
    router = EspnRouter(breakers, limiter=RateLimiter(2.0, 20, clock=clock.monotonic,
                                                      sleep=clock.sleep))
    job = PollNflLive(engine, router, sleep=clock.sleep)
    probes = switched = 0
    with respx.mock(assert_all_called=False) as mock:
        espn.install(mock)
        while clock.now < KICKOFF + timedelta(minutes=45):    # 35 minutes of a stuck slate
            out = job(clock.now)
            probes, switched = probes + out.probes, switched + out.switched
            clock.now += timedelta(seconds=30)
    assert switched == 0                       # the other provider agrees: nothing is ahead
    # first at 5:30 after the first poll, then every 5 minutes: 14 games, but a handful of
    # requests in all, not 14 per interval
    assert 5 <= probes <= 8
    other = [1 for _, host, kind in espn.requests if host == "site" and kind == "board"]
    assert len(other) == probes


def test_a_probe_that_shows_one_game_ahead_switches_the_provider_for_the_whole_day(engine, clean):
    clock = SimClock(KICKOFF + timedelta(minutes=10))
    espn = Espn(clock)
    espn.show(S.IN_PROGRESS, 2, 600, 14, 10, 3, others=S.IN_PROGRESS)
    ids = [e["id"] for e in BASE["events"]]
    commit(engine, lambda s: [add_slip(s, add_event(s, espn_id=i), [total("50.5")]) for i in ids])
    breakers = Breakers(engine, clock=lambda: clock.now)
    router = EspnRouter(breakers, limiter=RateLimiter(2.0, 20, clock=clock.monotonic,
                                                      sleep=clock.sleep))
    job = PollNflLive(engine, router, sleep=clock.sleep)
    ahead = scoreboard_json(BASE, S.IN_PROGRESS, 2, 100, 21, 10, others=S.IN_PROGRESS)
    with respx.mock(assert_all_called=False) as mock:
        espn.install(mock)
        # web is stuck; site (asked only by the probe) has moved on for every game
        site = mock.get(host="site.api.espn.com")
        site.mock(side_effect=lambda r: httpx.Response(200, json=ahead))
        switched = 0
        while clock.now < KICKOFF + timedelta(minutes=20):
            switched += job(clock.now).switched
            clock.now += timedelta(seconds=30)
    assert switched == 1
    assert not breakers["espn_web"].allow()                 # web is out, as `frozen`
    assert breakers["espn_web"].failure_kind.value == "frozen"
    assert {event_of(engine, i).clock_seconds for i in ids} == {100}   # everything refreshed
