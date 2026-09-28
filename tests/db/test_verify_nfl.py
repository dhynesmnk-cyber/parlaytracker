"""verify_nfl: checking ESPN's settlements against nflverse, and settling from nflverse when
ESPN's box score doesn't cover a leg (SPEC.md section 7.1)."""
from datetime import timedelta
from decimal import Decimal as D

import polars as pl
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from support import (
    ARI_SF,
    BRISSETT,
    DAY_AFTER,
    FINAL_AT,
    JAYDEN_WILLIAMS,
    KICKOFF,
    MCBRIDE,
    SEUMALO,
    FakeRouter,
    add_event,
    add_slip,
    ari_sf_box,
    commit,
    legs_of,
    nflverse_loader,
    player,
    receptions,
    spread,
    team_total,
    total,
)

from parlaytracker.core import services
from parlaytracker.core.models import DataSource, EventStatus, LegResult, Slip, SlipStatus
from parlaytracker.ingest.nflverse import NflverseData
from parlaytracker.ingest.router import Breakers
from parlaytracker.worker.settle import Settle, VerifyNfl

pytestmark = pytest.mark.db

ESPN_SETTLED_AT = FINAL_AT + timedelta(minutes=11)
NOW = DAY_AFTER  # Monday 11:00 ET, after nflverse's overnight publish
FINAL = {"status": EventStatus.FINAL, "final_at": FINAL_AT, "scores": (36, 30), "period": 4,
         "clock": 0}


def espn_settle(engine):
    router = FakeRouter()
    router.boxes[ARI_SF] = ari_sf_box()
    Settle(engine, router)(ESPN_SETTLED_AT)


def verify(engine, now=NOW, patch=None, breakers=None):
    breakers = breakers or Breakers(engine=None)
    job = VerifyNfl(engine, lambda: NflverseData(breakers, nflverse_loader(patch)))
    return job(now)


# --- A: check what ESPN settled -------------------------------------------------------------


def test_legs_that_agree_with_nflverse_are_verified(engine, clean):
    commit(engine, lambda s: add_slip(s, add_event(s, **FINAL), [
        total("65.5"), team_total("home", "35.5"), spread("away", "6.5"),
        receptions(MCBRIDE, "8.5"), player("player_receiving_yards", MCBRIDE, "70.5")]))
    espn_settle(engine)
    out = verify(engine)
    assert out.verified == 5 and out.flagged == 0
    for leg in legs_of(engine):
        assert (leg.verified_at, leg.verified_source) == (NOW, DataSource.NFLVERSE)
        assert not leg.needs_review


def test_a_disagreement_goes_to_review_with_both_values_and_the_result_stands(engine, clean):
    def build(s):
        slip = add_slip(s, add_event(s, **FINAL), [receptions(MCBRIDE, "8.5")])
        return slip

    commit(engine, build)
    espn_settle(engine)

    def nflverse_says_8(frames):  # a stat correction: nflverse has McBride at 8, ESPN said 9
        stats = frames["player_stats"]
        frames["player_stats"] = stats.with_columns(
            pl.when(pl.col("player_display_name") == "Trey McBride").then(8)
            .otherwise(pl.col("receptions")).alias("receptions"))

    out = verify(engine, patch=nflverse_says_8)
    leg = legs_of(engine)[0]
    assert out.flagged == 1 and out.verified == 0
    assert leg.review_reason == "Sources disagree: ESPN 9, nflverse 8"
    assert (leg.result, leg.final_value, leg.verified_at) == (LegResult.WIN, D("9"), None)


def test_a_score_disagreement_is_caught_for_team_legs(engine, clean):
    commit(engine, lambda s: add_slip(s, add_event(s, **FINAL), [total("65.5")]))
    espn_settle(engine)

    def other_score(frames):
        frames["schedules"] = frames["schedules"].with_columns(
            pl.when(pl.col("espn") == ARI_SF).then(35).otherwise(pl.col("home_score"))
            .alias("home_score"))

    verify(engine, patch=other_score)
    assert legs_of(engine)[0].review_reason == "Sources disagree: ESPN 66, nflverse 65"


def test_a_leg_nflverse_cannot_check_stays_unverified_and_out_of_review(engine, clean):
    commit(engine, lambda s: add_slip(s, add_event(s, **FINAL), [
        receptions(MCBRIDE, "8.5"), receptions("4692835", "0.5")]))  # second: Brooks, 0 rec
    espn_settle(engine)

    def drop_player_stats(frames):  # nflverse hasn't published the stat lines yet
        frames["player_stats"] = frames["player_stats"].clear()

    out = verify(engine, patch=drop_player_stats)
    assert (out.verified, out.flagged, out.skipped) == (0, 0, 2)
    assert all(leg.verified_at is None and not leg.needs_review for leg in legs_of(engine))


def test_nothing_is_checked_twice_or_after_a_week(engine, clean):
    commit(engine, lambda s: add_slip(s, add_event(s, **FINAL), [total("65.5")]))
    espn_settle(engine)
    assert verify(engine).verified == 1
    assert verify(engine, NOW + timedelta(days=1)).verified == 0  # already verified

    commit(engine, lambda s: add_slip(s, add_event(s, espn_id="401872954", **FINAL),
                                      [total("60.5")]))
    router = FakeRouter()
    router.boxes["401872954"] = ari_sf_box()
    Settle(engine, router)(ESPN_SETTLED_AT)
    assert verify(engine, ESPN_SETTLED_AT + timedelta(days=7, seconds=1)).verified == 0


def test_a_manual_settlement_with_a_value_is_checked_too(engine, clean):
    def build(s):
        slip = add_slip(s, add_event(s, **FINAL), [receptions(MCBRIDE, "8.5")])
        services.settle_leg_manually(s, slip.legs[0], final_value=D("9"), now=ESPN_SETTLED_AT)

    commit(engine, build)
    assert verify(engine).verified == 1


# --- B: settle from nflverse what ESPN's box score didn't cover -----------------------------


def test_a_player_espn_left_out_settles_from_a_nflverse_stat_line(engine, clean):
    commit(engine, lambda s: add_slip(s, add_event(s, **FINAL), [receptions(BRISSETT, "0.5")]))
    espn_settle(engine)  # ESPN's box score has no receiving line for him: still pending
    assert legs_of(engine)[0].result is LegResult.PENDING
    out = verify(engine)
    leg = legs_of(engine)[0]
    assert out.settled == 1
    assert (leg.result, leg.final_value, leg.settlement_source) == (
        LegResult.LOSS, D("0"), DataSource.NFLVERSE)
    assert leg.verified_at is None  # one source only: not verified


def test_no_stat_line_but_snaps_settles_at_zero_and_notes_it(engine, clean):
    commit(engine, lambda s: add_slip(s, add_event(s, **FINAL), [receptions(SEUMALO, "0.5")]))
    espn_settle(engine)
    verify(engine)
    leg = legs_of(engine)[0]
    assert (leg.result, leg.final_value, leg.settlement_source) == (
        LegResult.LOSS, D("0"), DataSource.NFLVERSE)
    assert leg.review_reason == "Played, no stat" and not leg.needs_review


def test_no_stat_line_and_no_snaps_goes_to_review(engine, clean):
    commit(engine, lambda s: add_slip(s, add_event(s, **FINAL),
                                      [receptions(JAYDEN_WILLIAMS, "0.5")]))
    espn_settle(engine)

    def no_snaps(frames):
        frames["snap_counts"] = frames["snap_counts"].with_columns(
            pl.when(pl.col("player") == "Jayden Williams").then(0.0)
            .otherwise(pl.col("offense_snaps")).alias("offense_snaps"))
        frames["player_stats"] = frames["player_stats"].filter(
            pl.col("player_display_name") != "Jayden Williams")

    verify(engine, patch=no_snaps)
    leg = legs_of(engine)[0]
    assert (leg.result, leg.needs_review, leg.review_reason) == (
        LegResult.PENDING, True, "No stat line and no snaps: likely void")


def test_no_data_at_all_waits_for_the_tuesday_and_is_then_reviewed(engine, clean):
    commit(engine, lambda s: add_slip(s, add_event(s, **FINAL), [receptions("999", "0.5")]))
    espn_settle(engine)
    verify(engine, NOW)  # Monday
    leg = legs_of(engine)[0]
    assert (leg.result, leg.needs_review) == (LegResult.PENDING, False)
    verify(engine, KICKOFF + timedelta(days=4))  # Thursday, after the Tuesday following
    assert legs_of(engine)[0].review_reason == "No stat line and no snaps: likely void"


def test_a_leg_already_in_review_is_left_alone(engine, clean):
    def build(s):
        slip = add_slip(s, add_event(s, **FINAL), [receptions(BRISSETT, "0.5")])
        services.flag_leg(s, slip.legs[0], "A person is looking")

    commit(engine, build)
    verify(engine)
    leg = legs_of(engine)[0]
    assert (leg.result, leg.review_reason) == (LegResult.PENDING, "A person is looking")


# --- ESPN unavailable -----------------------------------------------------------------------


def test_espn_never_finishing_lets_nflverse_settle_after_ten_hours(engine, clean):
    commit(engine, lambda s: add_slip(s, add_event(s), [total("65.5")]))  # still "scheduled"
    verify(engine, KICKOFF + timedelta(hours=9, minutes=59))
    assert legs_of(engine)[0].result is LegResult.PENDING
    verify(engine, KICKOFF + timedelta(hours=10))
    leg = legs_of(engine)[0]
    assert (leg.result, leg.settlement_source, leg.verified_at) == (
        LegResult.WIN, DataSource.NFLVERSE, None)  # unverified: no second source agreed


def test_espn_unavailable_settles_a_player_leg_from_nflverse_too(engine, clean):
    commit(engine, lambda s: add_slip(s, add_event(s), [receptions(MCBRIDE, "8.5")]))
    verify(engine, KICKOFF + timedelta(hours=10))
    leg = legs_of(engine)[0]
    assert (leg.result, leg.final_value, leg.settlement_source) == (
        LegResult.WIN, D("9"), DataSource.NFLVERSE)


def test_nflverse_without_a_final_score_settles_nothing(engine, clean):
    commit(engine, lambda s: add_slip(s, add_event(s, espn_id="401872963"),
                                      [total("40.5")]))  # PHI @ CHI: no scores yet
    verify(engine, KICKOFF + timedelta(hours=12))
    assert legs_of(engine)[0].result is LegResult.PENDING


def test_an_event_espn_marked_postponed_is_not_settled_from_nflverse(engine, clean):
    commit(engine, lambda s: add_slip(s, add_event(s, status=EventStatus.POSTPONED),
                                      [total("65.5")]))
    verify(engine, KICKOFF + timedelta(hours=12))
    assert legs_of(engine)[0].result is LegResult.PENDING


def test_the_slip_settles_when_its_last_leg_does(engine, clean):
    commit(engine, lambda s: add_slip(s, add_event(s, **FINAL), [
        total("65.5"), receptions(BRISSETT, "0.5")]))  # second leg loses at 0
    espn_settle(engine)
    assert legs_of(engine)[1].result is LegResult.PENDING
    verify(engine)
    with Session(engine) as s:
        assert s.scalars(select(Slip)).one().status is SlipStatus.LOSS


# --- Failures -------------------------------------------------------------------------------


def test_nflverse_being_down_changes_nothing_and_never_raises(engine, clean):
    commit(engine, lambda s: add_slip(s, add_event(s, **FINAL), [total("65.5")]))
    espn_settle(engine)
    breakers = Breakers(engine=None)

    def down(dataset, season):
        raise ConnectionError("github is down")

    out = VerifyNfl(engine, lambda: NflverseData(breakers, down))(NOW)
    assert (out.verified, out.settled, out.flagged) == (0, 0, 0)
    assert legs_of(engine)[0].verified_at is None
    assert breakers["nflverse"].consecutive_failures == 1
