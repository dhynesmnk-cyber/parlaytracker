"""Phase 2 services: events, manual settlement, review queue, tags and sportsbooks."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from parlaytracker.core import services as svc
from parlaytracker.core.models import (
    ClosingSource,
    DataSource,
    EventStatus,
    LegResult,
    SlipStatus,
    Sport,
    Tag,
)
from parlaytracker.core.schemas import SlipIn
from parlaytracker.core.services import ServiceError

pytestmark = pytest.mark.db

USER = "a@example.com"
KICKOFF = datetime(2026, 10, 4, 17, 0, tzinfo=UTC)


def make_slip(session, book, legs, **overrides):
    data = {"is_placed": True, "stake": "10.00", "slip_type": "single",
            "sportsbook_id": book.id, "american_odds": legs[0].get("american_odds") or -110,
            "source": "quick_add", "legs": legs}
    return svc.create_slip(session, SlipIn(**{**data, **overrides}), USER)


def total(event, odds=-110, line="45.5"):
    return {"event_id": event.id, "market_type": "game_total", "line": line,
            "american_odds": odds}


def rush(event, odds=None, line="50.5"):
    return {"event_id": event.id, "market_type": "player_rushing_yards",
            "espn_athlete_id": "7", "line": line, "american_odds": odds}


# --- upsert_event --------------------------------------------------------------------------

EVENT = {"sport": Sport.NFL, "espn_event_id": "401900001", "start_time": KICKOFF,
         "home_team": "Chicago Bears", "away_team": "Philadelphia Eagles",
         "home_espn_team_id": "3", "away_espn_team_id": "21",
         "status": EventStatus.SCHEDULED}


def test_upsert_creates_then_refreshes_a_scheduled_event(session):
    event = svc.upsert_event(session, **EVENT)
    moved = KICKOFF + timedelta(hours=3)
    again = svc.upsert_event(session, **{**EVENT, "start_time": moved})
    assert again.id == event.id
    assert again.start_time == moved


def test_upsert_never_overwrites_an_event_the_worker_has_moved_on(session):
    event = svc.upsert_event(session, **EVENT)
    event.status = EventStatus.IN_PROGRESS
    svc.upsert_event(session, **{**EVENT, "status": EventStatus.SCHEDULED,
                                 "start_time": KICKOFF + timedelta(hours=1)})
    assert (event.status, event.start_time) == (EventStatus.IN_PROGRESS, KICKOFF)


def test_upsert_rejects_a_sport_mismatch(session):
    svc.upsert_event(session, **EVENT)
    with pytest.raises(ServiceError):
        svc.upsert_event(session, **{**EVENT, "sport": Sport.NBA})


# --- manual settlement ---------------------------------------------------------------------


def test_single_settles_from_the_final_value(session, book, nfl_event):
    slip = make_slip(session, book, [total(nfl_event)])
    leg = slip.legs[0]
    svc.settle_leg_manually(session, leg, final_value=Decimal("51"))
    assert (leg.result, leg.settlement_source) == (LegResult.WIN, DataSource.MANUAL)
    assert (slip.status, slip.payout) == (SlipStatus.WIN, Decimal("19.09"))
    assert slip.settled_at is not None


def test_contradicting_value_and_result_is_rejected(session, book, nfl_event):
    leg = make_slip(session, book, [total(nfl_event)]).legs[0]
    with pytest.raises(ServiceError, match="makes this leg a win"):
        svc.settle_leg_manually(session, leg, result=LegResult.LOSS, final_value=Decimal("51"))


def test_void_has_no_value_and_other_legs_need_a_result(session, book, nfl_event):
    leg = make_slip(session, book, [total(nfl_event)]).legs[0]
    with pytest.raises(ServiceError):
        svc.settle_leg_manually(session, leg, result=LegResult.VOID, final_value=Decimal("3"))
    other = make_slip(session, book, [{"event_id": nfl_event.id, "market_type": "other",
                                       "description": "Bears ML", "american_odds": -150}],
                      american_odds=-150).legs[0]
    with pytest.raises(ServiceError):
        svc.settle_leg_manually(session, other, final_value=Decimal("1"))
    svc.settle_leg_manually(session, other, result=LegResult.WIN)
    assert other.slip.status is SlipStatus.WIN


def test_parlay_settles_leg_by_leg_and_loses_on_first_loss(
        session, book, nfl_event, nfl_event_2):
    legs = [total(nfl_event), total(nfl_event_2, odds=120, line="40.5")]
    slip = make_slip(session, book, legs, slip_type="parlay", american_odds=320)
    svc.settle_leg_manually(session, slip.legs[0], final_value=Decimal("50"))
    assert slip.status is SlipStatus.PENDING and slip.payout is None
    svc.settle_leg_manually(session, slip.legs[1], final_value=Decimal("30"))
    assert (slip.status, slip.payout) == (SlipStatus.LOSS, Decimal("0.00"))


def test_reduced_parlay_worked_example(session, book, nfl_event, nfl_event_2):
    # SPEC.md 7.2: $10 at -110, +120, -120; the +120 leg pushes -> $35.00
    legs = [total(nfl_event), total(nfl_event_2, odds=120, line="40"),
            {"event_id": nfl_event_2.id, "market_type": "team_total", "side": "home",
             "line": "20.5", "american_odds": -120}]
    slip = make_slip(session, book, legs, slip_type="parlay", american_odds=670)
    svc.settle_leg_manually(session, slip.legs[0], final_value=Decimal("50"))
    svc.settle_leg_manually(session, slip.legs[1], final_value=Decimal("40"))
    svc.settle_leg_manually(session, slip.legs[2], final_value=Decimal("24"))
    assert slip.legs[1].result is LegResult.PUSH
    assert (slip.status, slip.payout) == (SlipStatus.WIN, Decimal("35.00"))


def test_reduced_sgp_goes_to_review_until_the_payout_is_entered(session, book, nfl_event):
    slip = make_slip(session, book, [total(nfl_event), rush(nfl_event)],
                     slip_type="sgp", american_odds=450)
    svc.settle_leg_manually(session, slip.legs[0], final_value=Decimal("50"))
    svc.settle_leg_manually(session, slip.legs[1], result=LegResult.VOID)
    assert slip.needs_review and slip.status is SlipStatus.PENDING
    svc.enter_slip_payout(session, slip, Decimal("19.09"))
    assert (slip.status, slip.payout, slip.needs_review) == (
        SlipStatus.WIN, Decimal("19.09"), False)


def test_payout_needs_every_leg_settled(session, book, nfl_event):
    slip = make_slip(session, book, [total(nfl_event), rush(nfl_event)],
                     slip_type="sgp", american_odds=450)
    with pytest.raises(ServiceError):
        svc.enter_slip_payout(session, slip, Decimal("10"))


def test_reopen_puts_the_slip_back_to_pending(session, book, nfl_event):
    slip = make_slip(session, book, [total(nfl_event)])
    svc.settle_leg_manually(session, slip.legs[0], final_value=Decimal("40"))
    assert slip.status is SlipStatus.LOSS
    svc.reopen_leg(session, slip.legs[0])
    assert (slip.status, slip.payout, slip.settled_at) == (SlipStatus.PENDING, None, None)
    assert slip.legs[0].settlement_source is None


def test_cash_out_ends_the_slip_but_legs_still_settle(session, book, nfl_event):
    slip = make_slip(session, book, [total(nfl_event)])
    svc.mark_cashed_out(session, slip, Decimal("7.50"))
    svc.settle_leg_manually(session, slip.legs[0], final_value=Decimal("51"))
    assert (slip.status, slip.payout) == (SlipStatus.CASHED_OUT, Decimal("7.50"))
    assert slip.legs[0].result is LegResult.WIN
    with pytest.raises(ServiceError):
        svc.mark_cashed_out(session, slip, Decimal("1"))


def test_unplaced_slips_cannot_be_cashed_out(session, book, nfl_event):
    slip = make_slip(session, book, [total(nfl_event)], is_placed=False, stake=None)
    with pytest.raises(ServiceError):
        svc.mark_cashed_out(session, slip, Decimal("1"))


def test_manual_closing_line(session, book, nfl_event):
    leg = make_slip(session, book, [total(nfl_event)]).legs[0]
    svc.set_closing_line(session, leg, closing_line=Decimal("47.5"), closing_odds=-110,
                         closing_opposite_odds=-110)
    assert (leg.closing_line, leg.closing_source) == (Decimal("47.5"), ClosingSource.MANUAL)
    with pytest.raises(ServiceError):
        svc.set_closing_line(session, leg, closing_line=Decimal("47.3"), closing_odds=-110)
    with pytest.raises(ServiceError):
        svc.set_closing_line(session, leg, closing_line=Decimal("47.5"), closing_odds=50)


# --- review queue --------------------------------------------------------------------------


def test_review_queue_kinds_and_count(session, book, nfl_event, nfl_event_2):
    nfl_event.start_time = KICKOFF
    nfl_event_2.start_time = KICKOFF - timedelta(days=10)
    flagged = make_slip(session, book, [total(nfl_event)]).legs[0]
    flagged.needs_review, flagged.review_reason = True, "No stat line"
    awaiting = make_slip(session, book, [total(nfl_event, line="50.5")]).legs[0]
    sgp = make_slip(session, book, [total(nfl_event, line="41.5"), rush(nfl_event)],
                    slip_type="sgp", american_odds=450)
    svc.settle_leg_manually(session, sgp.legs[0], final_value=Decimal("50"))
    svc.settle_leg_manually(session, sgp.legs[1], result=LegResult.VOID)
    old = make_slip(session, book, [total(nfl_event_2)]).legs[0]
    session.flush()

    now = KICKOFF + timedelta(hours=5)
    items = svc.review_queue(session, now)
    kinds = [(i.kind, i.leg.id if i.leg else i.slip.id) for i in items]
    assert ("leg", flagged.id) in kinds
    assert ("slip", sgp.id) in kinds
    assert ("result", awaiting.id) in kinds
    assert ("result", old.id) in kinds  # still awaiting a result, however old
    assert ("closing_line", awaiting.id) in kinds
    assert ("closing_line", old.id) not in kinds  # outside the 7-day window
    assert [k for k, _ in kinds] == sorted(
        (k for k, _ in kinds), key=["leg", "slip", "result", "closing_line"].index)
    assert svc.review_count(session, now) == sum(1 for k, _ in kinds if k != "closing_line")


def test_nothing_awaits_a_result_before_the_game_is_over(session, book, nfl_event):
    nfl_event.start_time = KICKOFF
    make_slip(session, book, [total(nfl_event)])
    assert [i.kind for i in svc.review_queue(session, KICKOFF + timedelta(hours=1))] == [
        "closing_line"]


# --- tags and sportsbooks ------------------------------------------------------------------


def test_tags_create_rename_retire(session):
    tag = svc.create_tag(session, " Situation ", "Primetime")
    assert (tag.category, tag.name) == ("situation", "Primetime")
    with pytest.raises(ServiceError):
        svc.create_tag(session, "situation", "primetime")
    svc.rename_tag(session, tag, "situation", "Prime time")
    assert tag.name == "Prime time"
    svc.set_tag_retired(session, tag, True)
    assert tag not in svc.tag_choices(session)


def test_rename_into_an_existing_tag_is_refused(session):
    a = svc.create_tag(session, "weather", "Wind")
    svc.create_tag(session, "weather", "Rain")
    with pytest.raises(ServiceError, match="merge"):
        svc.rename_tag(session, a, "weather", "rain")


def test_merge_moves_legs_and_deletes_the_source(session, book, nfl_event):
    src = svc.create_tag(session, "injury", "WR1 out")
    dst = svc.create_tag(session, "injury", "Top WR out")
    both = make_slip(session, book, [total(nfl_event, line="44.5")]).legs[0]
    both.tags = [src, dst]
    only_src = make_slip(session, book, [total(nfl_event)]).legs[0]
    only_src.tags = [src]
    session.flush()
    svc.merge_tags(session, src, dst)
    assert both.tags == [dst] and only_src.tags == [dst]
    assert session.get(Tag, src.id) is None


def test_tag_choices_most_used_first(session, book, nfl_event):
    rare = svc.create_tag(session, "a", "rare")
    common = svc.create_tag(session, "z", "common")
    for line in ("40.5", "41.5"):
        make_slip(session, book, [total(nfl_event, line=line)]).legs[0].tags = [common]
    make_slip(session, book, [total(nfl_event, line="42.5")]).legs[0].tags = [rare]
    session.flush()
    assert svc.tag_choices(session)[:2] == [common, rare]


def test_sportsbooks(session, book):
    with pytest.raises(ServiceError):
        svc.create_sportsbook(session, "draftkings")
    new = svc.create_sportsbook(session, "BetRivers", " betrivers ")
    assert new.odds_api_key == "betrivers"
    svc.update_sportsbook(session, new, "BetRivers", "")
    assert new.odds_api_key is None
    with pytest.raises(ServiceError):
        svc.update_sportsbook(session, new, "DraftKings", None)


def test_recent_slips_newest_first(session, book, nfl_event):
    first = make_slip(session, book, [total(nfl_event)])
    second = make_slip(session, book, [total(nfl_event, line="50.5")])
    assert [s.id for s in svc.recent_slips(session, 2)] == [second.id, first.id]
