"""check_finals, settle and recheck_settled against Postgres (SPEC.md sections 7.1 and 8.1).
ESPN is answered by a fake router that returns the real parsers' output for recorded games."""
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D

import pytest
from support import (
    ARI_SF,
    FINAL_AT,
    KICKOFF,
    MCBRIDE,
    FakeRouter,
    add_event,
    add_slip,
    ari_sf_box,
    commit,
    event_of,
    legs_of,
    load,
    nba_box,
    player,
    receptions,
    spread,
    team_total,
    total,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from parlaytracker.core import services
from parlaytracker.core.models import (
    DataSource,
    EventStatus,
    LegResult,
    Slip,
    SlipStatus,
    Sport,
)
from parlaytracker.ingest import espn
from parlaytracker.ingest.http import RateLimited
from parlaytracker.ingest.router import AllProvidersFailed
from parlaytracker.worker.settle import CheckFinals, RecheckSettled, Settle

pytestmark = pytest.mark.db

NOW = FINAL_AT + timedelta(minutes=11)  # ten minutes after final, plus a minute
SUNDAY = date(2026, 9, 27)


def final_event(session, **kwargs):
    kwargs.setdefault("status", EventStatus.FINAL)
    kwargs.setdefault("final_at", FINAL_AT)
    kwargs.setdefault("scores", (36, 30))
    kwargs.setdefault("period", 4)
    kwargs.setdefault("clock", 0)
    return add_event(session, **kwargs)


def slips(engine) -> list[Slip]:
    with Session(engine) as s:
        return list(s.scalars(select(Slip).order_by(Slip.id)))


@pytest.fixture
def router() -> FakeRouter:
    r = FakeRouter()
    r.boxes[ARI_SF] = ari_sf_box()
    return r


# --- settle ---------------------------------------------------------------------------------


def test_settles_team_and_player_legs_from_the_box_score(engine, clean, router):
    commit(engine, lambda s: add_slip(s, final_event(s), [
        total("65.5"),                       # 66: win
        team_total("home", "35.5"),          # SF 36: win
        spread("away", "6.5"),               # ARI margin -6, +6.5 covers: win
        receptions(MCBRIDE, "8.5"),          # 9: win
    ]))
    out = Settle(engine, router)(NOW)
    assert out.settled == 4
    legs = legs_of(engine)
    assert [leg.result for leg in legs] == [LegResult.WIN] * 4
    assert [leg.final_value for leg in legs] == [D("66"), D("36"), D("-6"), D("9")]
    assert all(leg.settlement_source is DataSource.ESPN_WEB and leg.settled_at == NOW
               for leg in legs)
    (slip,) = slips(engine)
    assert slip.status is SlipStatus.WIN and slip.payout == D("19.09")


def test_a_lost_leg_loses_the_slip_at_once(engine, clean, router):
    commit(engine, lambda s: add_slip(s, final_event(s), [receptions(MCBRIDE, "9.5")]))
    Settle(engine, router)(NOW)
    (slip,) = slips(engine)
    assert (legs_of(engine)[0].result, slip.status, slip.payout) == (
        LegResult.LOSS, SlipStatus.LOSS, D("0.00"))


def test_a_push_refunds_a_single(engine, clean, router):
    commit(engine, lambda s: add_slip(s, final_event(s), [receptions(MCBRIDE, "9")]))
    Settle(engine, router)(NOW)
    (slip,) = slips(engine)
    assert (slip.status, slip.payout) == (SlipStatus.PUSH, D("10.00"))


def test_the_source_is_the_provider_that_served_the_box_score(engine, clean):
    router = FakeRouter(DataSource.ESPN_CDN)
    router.boxes[ARI_SF] = ari_sf_box()
    commit(engine, lambda s: add_slip(s, final_event(s), [total("65.5")]))
    Settle(engine, router)(NOW)
    assert legs_of(engine)[0].settlement_source is DataSource.ESPN_CDN


def test_nothing_settles_before_the_event_has_been_final_for_ten_minutes(engine, clean, router):
    commit(engine, lambda s: add_slip(s, final_event(s), [total("65.5")]))
    assert Settle(engine, router)(FINAL_AT + timedelta(minutes=9, seconds=59)).settled == 0
    assert router.calls == []  # not even a request
    assert legs_of(engine)[0].result is LegResult.PENDING
    assert Settle(engine, router)(FINAL_AT + timedelta(minutes=10)).settled == 1


def test_a_live_event_never_settles(engine, clean, router):
    commit(engine, lambda s: add_slip(s, add_event(
        s, status=EventStatus.IN_PROGRESS, scores=(30, 24), period=3, clock=100),
        [total("50.5")]))
    Settle(engine, router)(NOW)
    assert router.calls == [] and legs_of(engine)[0].result is LegResult.PENDING


def test_a_box_score_that_is_not_final_settles_nothing(engine, clean, router):
    live = ari_sf_box()
    router.boxes[ARI_SF] = espn.BoxScore(**{**live.__dict__, "status": EventStatus.IN_PROGRESS})
    commit(engine, lambda s: add_slip(s, final_event(s), [total("65.5")]))
    assert Settle(engine, router)(NOW).skipped == 1
    assert legs_of(engine)[0].result is LegResult.PENDING


@pytest.mark.parametrize("failure", [
    AllProvidersFailed({"espn_web": "500"}), RateLimited("espn", 0.0), None])
def test_espn_unavailable_changes_nothing_and_never_raises(engine, clean, router, failure):
    router.boxes[ARI_SF] = failure  # None: nothing registered at all
    commit(engine, lambda s: add_slip(s, final_event(s), [total("65.5")]))
    assert Settle(engine, router)(NOW).skipped == 1
    assert legs_of(engine)[0].result is LegResult.PENDING


def test_a_settled_result_is_never_changed_by_a_later_run(engine, clean, router):
    commit(engine, lambda s: add_slip(s, final_event(s), [total("65.5")]))
    Settle(engine, router)(NOW)
    router.boxes[ARI_SF] = espn.BoxScore(**{**ari_sf_box().__dict__, "home_score": 3,
                                            "away_score": 0})
    assert Settle(engine, router)(NOW + timedelta(hours=1)).settled == 0
    assert legs_of(engine)[0].result is LegResult.WIN


def test_legs_already_in_review_are_left_to_a_person(engine, clean, router):
    def build(s):
        slip = add_slip(s, final_event(s), [total("65.5")])
        slip.legs[0].needs_review = True
        slip.legs[0].review_reason = "Someone is on it"

    commit(engine, build)
    Settle(engine, router)(NOW)
    leg = legs_of(engine)[0]
    assert (leg.result, leg.review_reason) == (LegResult.PENDING, "Someone is on it")


def test_a_final_box_score_corrects_the_events_scores(engine, clean, router):
    commit(engine, lambda s: add_slip(s, final_event(s, scores=(35, 30)), [total("65.5")]))
    Settle(engine, router)(NOW)
    event = event_of(engine)
    assert (event.home_score, event.away_score) == (36, 30)


# --- Players missing from the box score -----------------------------------------------------


def test_an_nfl_player_missing_from_the_box_score_stays_pending_for_verify_nfl(
        engine, clean, router):
    commit(engine, lambda s: add_slip(s, final_event(s), [receptions("2578570", "0.5")]))
    out = Settle(engine, router)(NOW)
    leg = legs_of(engine)[0]
    assert (out.settled, out.flagged) == (0, 0)
    assert (leg.result, leg.needs_review) == (LegResult.PENDING, False)


def test_an_nba_player_missing_from_the_box_score_goes_to_review(engine, clean):
    router = FakeRouter()
    router.boxes["401810723"] = nba_box()
    commit(engine, lambda s: add_slip(s, final_event(
        s, espn_id="401810723", sport=Sport.NBA, scores=(114, 89), period=4),
        [player("player_points", "999", "10.5")]))
    Settle(engine, router)(NOW)
    leg = legs_of(engine)[0]
    assert (leg.result, leg.needs_review, leg.review_reason) == (
        LegResult.PENDING, True, "No stat line: enter 0 or void")


def test_an_nba_player_who_did_not_play_goes_to_review_and_is_never_voided(engine, clean):
    router = FakeRouter()
    router.boxes["401810723"] = nba_box()
    commit(engine, lambda s: add_slip(s, final_event(
        s, espn_id="401810723", sport=Sport.NBA, scores=(114, 89), period=4),
        [player("player_points", "2528426", "10.5")]))  # Jordan Clarkson: didNotPlay
    Settle(engine, router)(NOW)
    leg = legs_of(engine)[0]
    assert leg.result is LegResult.PENDING
    assert leg.review_reason == "Did not play: enter 0 or void"


def test_an_nba_player_with_points_settles(engine, clean):
    router = FakeRouter()
    router.boxes["401810723"] = nba_box()
    commit(engine, lambda s: add_slip(s, final_event(
        s, espn_id="401810723", sport=Sport.NBA, scores=(114, 89), period=4),
        [player("player_points", _wembanyama(), "24.5")]))
    Settle(engine, router)(NOW)
    leg = legs_of(engine)[0]
    assert (leg.result, leg.final_value) == (LegResult.WIN, D("25"))


def _wembanyama() -> str:
    payload = load("espn", "nba_summary_401810723_final.json")
    return next(a["athlete"]["id"] for t in payload["boxscore"]["players"]
                for s in t["statistics"] for a in s["athletes"]
                if a["athlete"]["displayName"] == "Victor Wembanyama")


# --- Flags ----------------------------------------------------------------------------------


def test_other_legs_go_to_review_once_their_game_is_final(engine, clean, router):
    commit(engine, lambda s: add_slip(s, final_event(s), [
        {"market_type": "other", "description": "Cardinals ML", "line": None}]))
    Settle(engine, router)(NOW)
    leg = legs_of(engine)[0]
    assert leg.needs_review and leg.review_reason.startswith("Settle manually")
    assert leg.result is LegResult.PENDING


@pytest.mark.parametrize("status", [EventStatus.POSTPONED, EventStatus.CANCELLED])
def test_postponed_and_cancelled_games_go_to_review(engine, clean, router, status):
    commit(engine, lambda s: add_slip(s, add_event(s, status=status), [total("65.5")]))
    Settle(engine, router)(NOW)
    leg = legs_of(engine)[0]
    assert leg.needs_review and status.value in leg.review_reason
    assert router.calls == []


def test_a_game_not_final_eight_hours_after_its_start_goes_to_review(engine, clean, router):
    commit(engine, lambda s: add_slip(s, add_event(
        s, status=EventStatus.IN_PROGRESS, period=3, clock=100), [total("65.5")]))
    Settle(engine, router)(KICKOFF + timedelta(hours=8) - timedelta(seconds=1))
    assert not legs_of(engine)[0].needs_review
    Settle(engine, router)(KICKOFF + timedelta(hours=8))
    assert legs_of(engine)[0].review_reason.startswith("Not final 8 hours")


def test_flagging_twice_keeps_the_first_reason(engine, clean, router):
    commit(engine, lambda s: add_slip(s, add_event(s, status=EventStatus.POSTPONED),
                                      [total("65.5")]))
    Settle(engine, router)(NOW)
    first = legs_of(engine)[0].review_reason
    Settle(engine, router)(NOW + timedelta(hours=1))
    assert legs_of(engine)[0].review_reason == first


# --- check_finals ---------------------------------------------------------------------------


def board(name: str = "nfl_scoreboard_2026-09-27_final.json") -> espn.ScoreboardResult:
    return espn.parse_scoreboard(Sport.NFL, load("espn", name))


def test_check_finals_marks_a_finished_game_final_and_dates_it(engine, clean):
    router = FakeRouter()
    router.boards[(Sport.NFL, SUNDAY)] = board()
    commit(engine, lambda s: add_slip(s, add_event(s), [total("65.5")]))
    now = KICKOFF + timedelta(hours=3, minutes=30)
    CheckFinals(engine, router)(now)
    event = event_of(engine)
    assert (event.status, event.home_score, event.away_score, event.final_at) == (
        EventStatus.FINAL, 36, 30, now)


def test_check_finals_waits_for_the_expected_duration(engine, clean):
    router = FakeRouter()
    router.boards[(Sport.NFL, SUNDAY)] = board()
    commit(engine, lambda s: add_slip(s, add_event(s), [total("65.5")]))
    CheckFinals(engine, router)(KICKOFF + timedelta(hours=2, minutes=59))
    assert router.calls == []
    CheckFinals(engine, router)(KICKOFF + timedelta(hours=3))
    assert len(router.calls) == 1


def test_one_scoreboard_call_per_sport_and_game_day(engine, clean):
    router = FakeRouter()
    router.boards[(Sport.NFL, SUNDAY)] = board()

    def build(s):
        add_slip(s, add_event(s), [total("65.5")])
        add_slip(s, add_event(s, espn_id="401872954"), [total("50.5")])
        add_slip(s, add_event(s, espn_id="401872953"), [total("40.5")])

    commit(engine, build)
    CheckFinals(engine, router)(KICKOFF + timedelta(hours=4))
    assert len(router.calls_of("board")) == 1


def test_check_finals_ignores_events_without_pending_legs(engine, clean):
    router = FakeRouter()

    def build(s):
        slip = add_slip(s, add_event(s), [total("65.5")])
        services.settle_leg_manually(s, slip.legs[0], result=LegResult.WIN)

    commit(engine, build)
    CheckFinals(engine, router)(KICKOFF + timedelta(hours=4))
    assert router.calls == []


def test_a_stale_scoreboard_is_discarded(engine, clean):
    router = FakeRouter()
    router.boards[(Sport.NFL, SUNDAY)] = espn.parse_scoreboard(Sport.NFL, _rewound())
    commit(engine, lambda s: add_slip(s, add_event(
        s, espn_id="401872954", status=EventStatus.IN_PROGRESS, period=4, clock=300,
        scores=(28, 24)), [total("50.5")]))
    CheckFinals(engine, router)(KICKOFF + timedelta(hours=4))  # the response says 3rd quarter
    event = event_of(engine, "401872954")
    assert (event.status, event.period, event.clock_seconds, event.home_score) == (
        EventStatus.IN_PROGRESS, 4, 300, 28)


def _rewound():
    """The Sunday scoreboard with game 401872954 back in the third quarter."""
    payload = load("espn", "nfl_scoreboard_2026-09-27_final.json")
    for e in payload["events"]:
        if e["id"] == "401872954":
            e["status"]["type"] = {"name": "STATUS_IN_PROGRESS", "state": "in",
                                   "completed": False, "detail": "3rd Quarter"}
            e["status"]["period"] = 3
            e["status"]["clock"] = 400.0
    return payload


def test_final_never_goes_back_to_play(engine, clean):
    router = FakeRouter()
    router.boards[(Sport.NFL, SUNDAY)] = espn.parse_scoreboard(Sport.NFL, _rewound())

    def build(s):
        add_slip(s, final_event(s, espn_id="401872954", final_at=FINAL_AT), [total("50.5")])
        s.flush()

    commit(engine, build)
    # The event is final, so check_finals doesn't even ask; force it to prove the guard.
    CheckFinals(engine, router)(KICKOFF + timedelta(hours=4), force=True)
    assert event_of(engine, "401872954").status is EventStatus.FINAL


def test_a_postponed_game_is_recorded_as_postponed(engine, clean):
    router = FakeRouter()
    day = date(2026, 5, 5)
    router.boards[(Sport.MLB, day)] = espn.parse_scoreboard(
        Sport.MLB, load("espn", "mlb_scoreboard_2026-05-05_postponed.json"))
    start = datetime(2026, 5, 5, 23, 40, tzinfo=UTC)
    commit(engine, lambda s: add_slip(s, add_event(
        s, espn_id="401815223", sport=Sport.MLB, start=start), [total("8.5")]))
    CheckFinals(engine, router)(start + timedelta(hours=4))
    assert event_of(engine, "401815223").status is EventStatus.POSTPONED


def test_espn_failing_leaves_events_alone_and_never_raises(engine, clean):
    router = FakeRouter()
    router.boards[(Sport.NFL, SUNDAY)] = AllProvidersFailed({"espn_web": "403"})
    commit(engine, lambda s: add_slip(s, add_event(s), [total("65.5")]))
    out = CheckFinals(engine, router)(KICKOFF + timedelta(hours=4))
    assert out.skipped == 1 and event_of(engine).status is EventStatus.SCHEDULED


def test_an_event_missing_from_the_scoreboard_records_why(engine, clean):
    router = FakeRouter()
    router.boards[(Sport.NFL, SUNDAY)] = board("nfl_scoreboard_2026-09-28_scheduled.json")
    commit(engine, lambda s: add_slip(s, add_event(s), [total("65.5")]))
    CheckFinals(engine, router)(KICKOFF + timedelta(hours=4))
    assert "scoreboard" in event_of(engine).last_error


def test_a_backfill_dates_an_old_final_to_the_games_expected_end(engine, clean):
    router = FakeRouter()
    router.boards[(Sport.NFL, SUNDAY)] = board()
    commit(engine, lambda s: add_slip(s, add_event(s), [total("65.5")]))
    weeks_later = KICKOFF + timedelta(days=20)
    CheckFinals(engine, router)(weeks_later, backfill=True)
    assert event_of(engine).final_at == KICKOFF + timedelta(hours=3)
    router.boxes[ARI_SF] = ari_sf_box()
    assert Settle(engine, router)(weeks_later).settled == 1  # the gate holds, honestly


# --- recheck_settled ------------------------------------------------------------------------


def settled_nba_leg(engine, *, sport=Sport.NBA, line="24.5", settled_ago=timedelta(hours=24,
                                                                                  minutes=30)):
    router = FakeRouter()
    router.boxes["401810723"] = nba_box()
    commit(engine, lambda s: add_slip(s, final_event(
        s, espn_id="401810723", sport=sport, scores=(114, 89), period=4),
        [player("player_points", _wembanyama(), line)]))
    Settle(engine, router)(NOW)
    return router


def test_a_changed_stat_goes_to_review_and_the_result_stands(engine, clean):
    settled_nba_leg(engine)
    box = nba_box()
    wemby = _wembanyama()
    router = FakeRouter()
    router.boxes["401810723"] = espn.BoxScore(**{
        **box.__dict__, "stats": {**box.stats, "player_points": {
            **box.stats[next(iter(box.stats))], wemby: D("27")}}})
    RecheckSettled(engine, router)(NOW + timedelta(hours=24, minutes=30))
    leg = legs_of(engine)[0]
    assert leg.review_reason == "Stat correction: was 25, now 27"
    assert (leg.result, leg.final_value) == (LegResult.WIN, D("25"))


def test_an_unchanged_stat_is_not_flagged(engine, clean):
    settled_nba_leg(engine)
    router = FakeRouter()
    router.boxes["401810723"] = nba_box()
    out = RecheckSettled(engine, router)(NOW + timedelta(hours=24, minutes=30))
    assert out.flagged == 0 and not legs_of(engine)[0].needs_review


@pytest.mark.parametrize(("later", "expected_calls"), [
    (timedelta(hours=23, minutes=59), 0),   # not yet 24 hours after settling
    (timedelta(hours=24), 1),
    (timedelta(hours=24, minutes=59), 1),
    (timedelta(hours=25), 0),               # the window has passed: once only
])
def test_recheck_looks_once_between_24_and_25_hours_after_settling(engine, clean, later,
                                                                   expected_calls):
    settled_nba_leg(engine)
    router = FakeRouter()
    router.boxes["401810723"] = nba_box()
    RecheckSettled(engine, router)(NOW + later)
    assert len(router.calls_of("box")) == expected_calls


def test_nfl_legs_are_not_rechecked_by_this_job(engine, clean, router):
    commit(engine, lambda s: add_slip(s, final_event(s), [total("65.5")]))
    Settle(engine, router)(NOW)
    router.calls.clear()
    RecheckSettled(engine, router)(NOW + timedelta(hours=24, minutes=30))
    assert router.calls == []


def test_manually_settled_legs_are_not_rechecked(engine, clean):
    def build(s):
        slip = add_slip(s, final_event(
            s, espn_id="401810723", sport=Sport.NBA, scores=(114, 89), period=4),
            [player("player_points", _wembanyama(), "24.5")])
        services.settle_leg_manually(s, slip.legs[0], final_value=D("25"), now=NOW)

    commit(engine, build)
    router = FakeRouter()
    router.boxes["401810723"] = nba_box()
    RecheckSettled(engine, router)(NOW + timedelta(hours=24, minutes=30))
    assert router.calls == []
