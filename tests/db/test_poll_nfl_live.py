"""`poll_nfl_live` against Postgres: cadence, live values, the integrity guards and the
frozen-feed rule (SPEC.md sections 8.1 and 8.3). ESPN is a fake with one feed per provider."""
from datetime import timedelta
from decimal import Decimal as D

import pytest
from support import (
    ARI_SF,
    KICKOFF,
    MCBRIDE,
    LiveRouter,
    add_event,
    add_slip,
    commit,
    event_of,
    legs_of,
    live_game,
    player,
    receptions,
    team_total,
    total,
)

from parlaytracker.core import services
from parlaytracker.core.models import DataSource, EventStatus as S, HealthState, LegResult
from parlaytracker.core.models import MarketType as M
from parlaytracker.core.models import Sport
from parlaytracker.ingest import espn
from parlaytracker.worker.live import REQUEST_GAP, PollNflLive

pytestmark = pytest.mark.db

DAY = espn.game_day(KICKOFF)
WEB, SITE = DataSource.ESPN_WEB, DataSource.ESPN_SITE


def t(seconds: float):
    return KICKOFF + timedelta(seconds=seconds)


def live_box(receptions_=4, event_id=ARI_SF, status=S.IN_PROGRESS):
    return espn.BoxScore(
        espn_event_id=event_id, status=status, home_espn_team_id="25", away_espn_team_id="22",
        home_score=0, away_score=0, stats={M.PLAYER_RECEPTIONS: {MCBRIDE: D(receptions_)}},
        did_not_play=frozenset())


@pytest.fixture
def setup(engine, clean):
    router = LiveRouter()
    sleeps: list[float] = []
    job = PollNflLive(engine, router, sleep=sleeps.append)
    return router, job, sleeps


def serve(router, game, box=None):
    router.serve(DAY, game)
    for provider in router.boxes:
        router.boxes[provider][ARI_SF] = box or live_box()


def scheduled(engine, legs=None, **event):
    commit(engine, lambda s: add_slip(s, add_event(s, **event), legs or [
        total("55.5"), team_total("home", "24.5"), receptions(MCBRIDE, "5.5")]))


# --- Which events, and when -----------------------------------------------------------------


def test_nothing_to_do_costs_no_requests(setup):
    router, job, _ = setup
    assert job(t(0)).scoreboards == 0 and router.calls == []


def test_an_event_more_than_30_minutes_before_kickoff_is_left_alone(engine, setup):
    router, job, _ = setup
    scheduled(engine)
    serve(router, live_game(S.SCHEDULED, period=0, clock=None))
    job(t(-31 * 60))
    assert router.calls == []
    job(t(-30 * 60))
    assert router.count("board") == 1


def test_a_game_without_pending_legs_is_not_polled(engine, setup):
    router, job, _ = setup

    def build(s):
        slip = add_slip(s, add_event(s), [total("55.5")])
        services.settle_leg_manually(s, slip.legs[0], result=LegResult.WIN)

    commit(engine, build)
    job(t(0))
    assert router.calls == []


@pytest.mark.parametrize("status", [S.FINAL, S.POSTPONED, S.CANCELLED])
def test_a_finished_game_stops_being_polled(engine, setup, status):
    router, job, _ = setup
    scheduled(engine, status=status)
    job(t(600))
    assert router.calls == []


def test_only_nfl_games_are_live_polled(engine, setup):
    router, job, _ = setup
    scheduled(engine, sport=Sport.NBA, legs=[total("210.5")])
    job(t(600))
    assert router.calls == []


# --- The cadence table ----------------------------------------------------------------------


def test_before_kickoff_the_scoreboard_is_asked_every_five_minutes_and_no_box_score(
        engine, setup):
    router, job, _ = setup
    scheduled(engine)
    serve(router, live_game(S.SCHEDULED, period=0, clock=None))
    start = -25 * 60
    for offset in (0, 60, 299, 300, 301, 599, 600):
        job(t(start + offset))
    assert router.count("board") == 3   # at 0, 300 and 600 seconds after the first
    assert router.count("box") == 0


def test_in_progress_polls_the_scoreboard_every_30_seconds_and_the_box_every_60(engine, setup):
    router, job, _ = setup
    scheduled(engine)
    serve(router, live_game())
    for tick in range(5):
        job(t(tick * 30))
    assert router.count("board") == 5   # 0, 30, 60, 90, 120
    assert router.count("box") == 3     # 0, 60, 120


def test_a_box_score_is_only_fetched_for_a_game_with_pending_player_legs(engine, setup):
    router, job, _ = setup
    scheduled(engine, legs=[total("55.5"), team_total("home", "24.5")])  # no player legs
    serve(router, live_game())
    job(t(0))
    assert router.count("board") == 1 and router.count("box") == 0


@pytest.mark.parametrize("status", [S.BREAK, S.DELAYED])
def test_a_break_or_delay_polls_every_2_minutes_and_the_box_every_3(engine, setup, status):
    router, job, _ = setup
    scheduled(engine)
    serve(router, live_game(status, period=2, clock=0))
    for seconds in (0, 30, 60, 119, 120, 150, 179, 180, 240, 360):
        job(t(seconds))
    assert router.count("board") == 4   # 0, 120, 240, 360
    assert router.count("box") == 3     # 0, 180, 360


def test_one_scoreboard_call_covers_every_game_that_day(engine, setup):
    router, job, _ = setup

    def build(s):
        for espn_id in ("401872958", "401872954", "401872953"):
            add_slip(s, add_event(s, espn_id=espn_id), [total("55.5")])

    commit(engine, build)
    games = [live_game(espn_id=i, home=10 + n) for n, i in
             enumerate(("401872958", "401872954", "401872953"))]
    router.serve(DAY, games)
    job(t(0))
    assert router.count("board") == 1
    assert [event_of(engine, i).home_score for i in ("401872958", "401872954", "401872953")] == [
        10, 11, 12]


def test_the_shortest_interval_any_game_needs_sets_the_scoreboard_pace(engine, setup):
    router, job, _ = setup

    def build(s):
        add_slip(s, add_event(s, espn_id="A", status=S.BREAK, period=2, clock=0), [total("55")])
        add_slip(s, add_event(s, espn_id="B", status=S.IN_PROGRESS, period=1, clock=500),
                 [total("50")])

    commit(engine, build)
    router.serve(DAY, [live_game(S.BREAK, period=2, clock=0, espn_id="A"),
                       live_game(S.IN_PROGRESS, period=1, clock=500, espn_id="B")])
    for tick in range(4):
        job(t(tick * 30))
    assert router.count("board") == 4  # B is in play: every 30 seconds for the whole day


# --- Live values ----------------------------------------------------------------------------


def test_live_values_come_from_the_scoreboard_and_the_box_score(engine, setup):
    router, job, _ = setup
    scheduled(engine)
    serve(router, live_game(period=2, clock=522, home=17, away=10), live_box(4))
    job(t(60))
    total_leg, team_leg, rec_leg = legs_of(engine)
    assert total_leg.live_value == 27          # 17 + 10
    assert team_leg.live_value == 17           # the home team's score
    assert rec_leg.live_value == 4             # McBride's receptions so far
    for leg in (total_leg, team_leg, rec_leg):
        assert (leg.live_source, leg.live_updated_at) == (WEB, t(60))
        assert leg.result is LegResult.PENDING
    event = event_of(engine)
    assert (event.status, event.period, event.clock_seconds) == (S.IN_PROGRESS, 2, 522)
    assert event.last_polled_at == t(60)


def test_a_player_missing_from_the_box_score_has_no_value_never_a_made_up_zero(engine, setup):
    router, job, _ = setup
    scheduled(engine, legs=[player("player_rushing_yards", "999", "50.5")])
    serve(router, live_game())
    job(t(0))
    assert legs_of(engine)[0].live_value is None


def test_live_values_never_settle_anything(engine, setup):
    router, job, _ = setup
    scheduled(engine)
    serve(router, live_game(period=4, clock=0, home=30, away=28))  # the total is long over
    job(t(0))
    assert all(leg.result is LegResult.PENDING and leg.settlement_source is None
               for leg in legs_of(engine))


def test_when_the_game_ends_polling_stops_and_the_legs_stay_pending_for_settle(engine, setup):
    router, job, _ = setup
    scheduled(engine)
    serve(router, live_game(S.FINAL, period=4, clock=0, home=30, away=28))
    job(t(60))
    assert event_of(engine).status is S.FINAL
    assert all(leg.result is LegResult.PENDING for leg in legs_of(engine))
    router.calls.clear()
    job(t(120))
    assert router.calls == []


# --- The integrity guards -------------------------------------------------------------------


def test_a_stale_response_is_discarded_and_changes_nothing(engine, setup):
    router, job, _ = setup
    scheduled(engine)
    serve(router, live_game(period=3, clock=400, home=20, away=13), live_box(5))
    job(t(0))
    serve(router, live_game(period=2, clock=100, home=10, away=7))   # a cached, older answer
    job(t(30))
    event = event_of(engine)
    assert (event.period, event.clock_seconds, (event.home_score, event.away_score)) == (
        3, 400, (20, 13))
    assert legs_of(engine)[0].live_value == 33  # still the newer total


def test_a_correction_at_the_same_progress_is_accepted_even_if_a_score_falls(engine, setup):
    router, job, _ = setup
    scheduled(engine)
    serve(router, live_game(period=3, clock=400, home=20, away=13))
    job(t(0))
    serve(router, live_game(period=3, clock=400, home=14, away=13))  # a touchdown overturned
    job(t(30))
    assert event_of(engine).home_score == 14 and legs_of(engine)[0].live_value == 27


def test_progress_never_goes_backwards_across_a_whole_sequence(engine, setup):
    router, job, _ = setup
    scheduled(engine)
    frames = [(1, 900), (1, 600), (1, 300), (2, 900), (1, 100), (2, 800), (2, 500), (1, 50),
              (3, 700), (3, 900), (4, 100)]
    seen = []
    for n, (period, clock) in enumerate(frames):
        serve(router, live_game(period=period, clock=clock))
        job(t(n * 30))
        e = event_of(engine)
        seen.append((e.period, 900 - e.clock_seconds))
    assert seen == sorted(seen)  # non-decreasing, however the responses arrive


def test_an_event_missing_from_the_scoreboard_records_why(engine, setup):
    router, job, _ = setup
    scheduled(engine)
    router.serve(DAY, [live_game(espn_id="someone-else")])
    job(t(0))
    assert "scoreboard" in event_of(engine).last_error


# --- ESPN failing and recovering --------------------------------------------------------------


def test_espn_being_down_leaves_the_last_good_data_and_never_raises(engine, setup):
    router, job, _ = setup
    scheduled(engine)
    serve(router, live_game(period=2, clock=300, home=17, away=10), live_box(4))
    job(t(0))
    router.down = {WEB, SITE, DataSource.ESPN_CDN}
    out = job(t(30))
    assert out.scoreboards == 0 and out.skipped >= 1
    assert legs_of(engine)[0].live_value == 27 and event_of(engine).home_score == 17


def test_tracking_recovers_without_a_restart_once_espn_is_back(engine, setup):
    router, job, _ = setup
    scheduled(engine)
    serve(router, live_game(period=2, clock=300, home=17, away=10))
    job(t(0))
    router.down = {WEB, SITE}
    for tick in range(1, 6):
        job(t(tick * 30))
    router.down = set()
    for provider in (WEB, SITE):   # breakers half-open once their open time has passed
        router.breakers[provider.value].open_until = t(0)
    serve(router, live_game(period=2, clock=100, home=24, away=10))
    out = job(t(6 * 30))
    assert out.scoreboards == 1
    assert event_of(engine).home_score == 24 and legs_of(engine)[1].live_value == 24


def test_a_failed_request_is_retried_on_the_next_tick_not_after_a_full_interval(engine, setup):
    router, job, _ = setup
    scheduled(engine, status=S.BREAK, period=2, clock=0)
    serve(router, live_game(S.BREAK, period=2, clock=0))
    router.down = {WEB, SITE}
    job(t(0))                      # due, fails
    router.down = set()
    for provider in (WEB, SITE):
        router.breakers[provider.value].open_until = None
    assert job(t(30)).scoreboards == 1   # not waiting the 2-minute break interval


# --- The frozen feed (section 8.3) ----------------------------------------------------------


def frozen_setup(engine, setup):
    router, job, sleeps = setup
    scheduled(engine)
    frozen = live_game(period=2, clock=300, home=17, away=10)
    router.serve(DAY, frozen, providers=[WEB])
    router.serve(DAY, frozen, providers=[SITE])
    for provider in router.boxes:
        router.boxes[provider][ARI_SF] = live_box(4)
    job(t(0))
    return router, job


def test_a_frozen_feed_is_replaced_by_the_provider_that_is_ahead(engine, setup):
    router, job = frozen_setup(engine, setup)
    router.serve(DAY, live_game(period=2, clock=120, home=24, away=10), providers=[SITE])
    out = None
    for tick in range(1, 13):        # web keeps answering with the same frozen game
        out = job(t(tick * 30))
        if out.switched:
            break
    assert out.switched == 1 and out.probes == 1
    assert t(330) == t(11 * 30)      # the first tick with no change for more than 5 minutes
    event = event_of(engine)
    assert (event.clock_seconds, event.home_score) == (120, 24)   # the probe's data was used
    assert not router.breakers["espn_web"].allow()                 # web is open as `frozen`
    assert router.breakers["espn_web"].failure_kind.value == "frozen"
    assert legs_of(engine)[0].live_source is SITE


def test_the_next_scoreboard_goes_to_the_provider_that_works(engine, setup):
    router, job = frozen_setup(engine, setup)
    router.serve(DAY, live_game(period=2, clock=120, home=24, away=10), providers=[SITE])
    for tick in range(1, 12):
        job(t(tick * 30))
    router.calls.clear()
    router.serve(DAY, live_game(period=2, clock=60, home=24, away=10), providers=[SITE])
    job(t(12 * 30))
    assert [c[1] for c in router.calls if c[0] == "board"] == [SITE]  # web is open: skipped
    assert event_of(engine).clock_seconds == 60


def test_a_stopped_clock_during_a_review_does_not_switch_provider(engine, setup):
    router, job = frozen_setup(engine, setup)   # both providers stay on the same game
    switched = probes = 0
    for tick in range(1, 25):                   # twelve minutes of nothing happening
        out = job(t(tick * 30))
        switched, probes = switched + out.switched, probes + out.probes
    assert switched == 0
    assert router.breakers["espn_web"].allow() and router.breakers["espn_site"].allow()
    assert router.breakers["espn_web"].state is HealthState.OK
    assert event_of(engine).clock_seconds == 300      # nothing changed
    assert probes >= 1


def test_the_probe_is_made_at_most_once_every_five_minutes(engine, setup):
    router, job = frozen_setup(engine, setup)
    probes = 0
    for tick in range(1, 51):                    # 25 minutes of a stopped game
        probes += job(t(tick * 30)).probes
    # first at 5:30, then 5 minutes apart: 5:30, 10:30, 15:30, 20:30 (and 25:00 is too early)
    assert probes == 4
    assert sum(1 for c in router.calls if c[0] == "board" and c[1] is SITE) == 4


@pytest.mark.parametrize("status", [S.BREAK, S.DELAYED])
def test_halftime_and_delays_are_never_a_frozen_feed(engine, setup, status):
    router, job, _ = setup
    scheduled(engine, status=status, period=2, clock=0)
    serve(router, live_game(status, period=2, clock=0))
    probes = sum(job(t(tick * 30)).probes for tick in range(0, 80))   # 40 minutes
    assert probes == 0


def test_a_probe_that_finds_the_other_provider_down_changes_nothing(engine, setup):
    router, job = frozen_setup(engine, setup)
    router.down = {SITE}
    for tick in range(1, 13):
        job(t(tick * 30))
    assert router.breakers["espn_web"].allow()
    assert event_of(engine).clock_seconds == 300


def test_the_probes_game_missing_is_not_evidence_of_a_freeze(engine, setup):
    router, job = frozen_setup(engine, setup)
    router.serve(DAY, [live_game(espn_id="other")], providers=[SITE])
    for tick in range(1, 13):
        job(t(tick * 30))
    assert router.breakers["espn_web"].allow() and event_of(engine).clock_seconds == 300


# --- Pacing ---------------------------------------------------------------------------------


def test_requests_in_one_tick_are_spaced_a_host_interval_apart(engine, setup):
    router, job, sleeps = setup

    def build(s):
        for espn_id in ("A", "B", "C"):
            add_slip(s, add_event(s, espn_id=espn_id), [receptions(MCBRIDE, "5.5")])

    commit(engine, build)
    router.serve(DAY, [live_game(espn_id=i) for i in "ABC"])
    for provider in router.boxes:
        for i in "ABC":
            router.boxes[provider][i] = live_box(event_id=i)
    out = job(t(0))
    assert (out.scoreboards, out.boxes) == (1, 3)
    assert sleeps == [REQUEST_GAP.total_seconds()] * 3   # between four requests: three gaps
    assert REQUEST_GAP >= timedelta(seconds=2)
