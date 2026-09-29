"""Pass completions, touchdowns, interceptions and field goals, settled end to end from the
recorded ARI @ SF box score (values read off the recorded document by hand)."""
from datetime import timedelta

import pytest
from support import (
    ARI_SF,
    BRISSETT,
    DAY_AFTER,
    FINAL_AT,
    SEUMALO,
    FakeRouter,
    add_event,
    add_slip,
    ari_sf_box,
    commit,
    legs_of,
    nflverse_loader,
    player,
)

from parlaytracker.core.models import DataSource, EventStatus, LegResult, SlipStatus
from parlaytracker.ingest.nflverse import NflverseData
from parlaytracker.ingest.router import Breakers
from parlaytracker.worker.settle import Settle, VerifyNfl

pytestmark = pytest.mark.db

KITTLE, MCCAFFREY, RYLAND = "3040151", "3117251", "4363538"
FINAL = {"status": EventStatus.FINAL, "final_at": FINAL_AT, "scores": (36, 30), "period": 4,
         "clock": 0}
SETTLED_AT = FINAL_AT + timedelta(minutes=11)


def settle(engine):
    router = FakeRouter()
    router.boxes[ARI_SF] = ari_sf_box()
    Settle(engine, router)(SETTLED_AT)


def by_market(engine):
    return {(leg.market_type.value, leg.espn_athlete_id, float(leg.line)): leg
            for leg in legs_of(engine)}


def test_each_new_market_settles_from_espn_at_its_line(engine, clean):
    commit(engine, lambda s: add_slip(s, add_event(s, **FINAL), [
        player("player_pass_completions", BRISSETT, "37.5"),   # 38: win
        player("player_pass_completions", BRISSETT, "38.5"),   # 38: loss
        player("player_interceptions", BRISSETT, "0.5"),       # 0: loss
        player("player_field_goals", RYLAND, "2.5"),           # 3: win
        player("player_touchdowns", KITTLE, "1.5"),            # 2: win ("2+ TDs")
        player("player_touchdowns", MCCAFFREY, "0.5"),         # 1: win ("anytime")
        player("player_touchdowns", MCCAFFREY, "1.5"),         # 1: loss
    ]))
    settle(engine)
    got = {k: (leg.result, leg.final_value, leg.settlement_source)
           for k, leg in by_market(engine).items()}
    assert got == {
        ("player_pass_completions", BRISSETT, 37.5): (LegResult.WIN, 38, DataSource.ESPN_WEB),
        ("player_pass_completions", BRISSETT, 38.5): (LegResult.LOSS, 38, DataSource.ESPN_WEB),
        ("player_interceptions", BRISSETT, 0.5): (LegResult.LOSS, 0, DataSource.ESPN_WEB),
        ("player_field_goals", RYLAND, 2.5): (LegResult.WIN, 3, DataSource.ESPN_WEB),
        ("player_touchdowns", KITTLE, 1.5): (LegResult.WIN, 2, DataSource.ESPN_WEB),
        ("player_touchdowns", MCCAFFREY, 0.5): (LegResult.WIN, 1, DataSource.ESPN_WEB),
        ("player_touchdowns", MCCAFFREY, 1.5): (LegResult.LOSS, 1, DataSource.ESPN_WEB),
    }


def test_a_touchdown_leg_wins_when_the_player_scores_and_loses_when_he_does_not(engine, clean):
    commit(engine, lambda s: add_slip(s, add_event(s, **FINAL), [
        player("player_touchdowns", KITTLE, "0.5")]))
    settle(engine)
    (leg,) = legs_of(engine)
    assert (leg.result, leg.final_value) == (LegResult.WIN, 2)


def test_a_player_who_took_snaps_but_is_in_no_touchdown_table_scored_none(engine, clean):
    """SF lineman: not in ESPN's rushing/receiving tables, 87 offensive snaps, no nflverse
    stat line: "Played, no stat", value 0, so an Over 0.5 loses (section 7.1)."""
    commit(engine, lambda s: add_slip(s, add_event(s, **FINAL), [
        player("player_touchdowns", SEUMALO, "0.5")]))
    settle(engine)
    assert legs_of(engine)[0].result is LegResult.PENDING  # ESPN has nothing on him
    out = VerifyNfl(engine, lambda: NflverseData(Breakers(engine=None), nflverse_loader()))(
        DAY_AFTER)
    (leg,) = legs_of(engine)
    assert out.settled == 1
    assert (leg.result, leg.final_value, leg.settlement_source) == (
        LegResult.LOSS, 0, DataSource.NFLVERSE)


def test_the_slip_settles_when_all_its_new_market_legs_do(engine, clean):
    commit(engine, lambda s: add_slip(s, add_event(s, **FINAL), [
        player("player_field_goals", RYLAND, "1.5"), player("player_touchdowns", KITTLE, "0.5"),
        player("player_pass_completions", BRISSETT, "30.5")]))
    settle(engine)
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from parlaytracker.core.models import Slip
    with Session(engine) as s:
        assert s.scalars(select(Slip)).one().status is SlipStatus.WIN


def test_a_new_market_leg_is_not_sent_to_the_odds_api_or_the_missing_closing_queue(
        engine, clean):
    from parlaytracker.core import services
    from sqlalchemy.orm import Session

    commit(engine, lambda s: add_slip(s, add_event(s, **FINAL), [
        player("player_touchdowns", KITTLE, "0.5")]))
    with Session(engine) as s:
        kinds = {item.kind for item in services.review_queue(s, FINAL_AT)}
    assert "closing_line" not in kinds
