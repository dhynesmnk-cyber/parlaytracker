"""Analytics against hand-computed numbers (SPEC.md sections 10 and 11)."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from itertools import count
from zoneinfo import ZoneInfo

import pytest

from parlaytracker.core import analytics as an
from parlaytracker.core.analytics import Filters, LegRow, SlipRow
from parlaytracker.core.models import LegResult as R
from parlaytracker.core.models import MarketType as M
from parlaytracker.core.models import SlipStatus as S
from parlaytracker.core.models import SlipType, Sport

ET = ZoneInfo("America/New_York")
_ids = count(1)
START = datetime(2026, 9, 27, 20, 5, tzinfo=UTC)  # Sunday 4:05 pm ET
LOGGED = START - timedelta(hours=3)
MIN = 30


def leg(result=R.WIN, odds=-110, line="45.5", market=M.GAME_TOTAL, key=None, logged=LOGGED,
        start=START, **kw) -> LegRow:
    n = next(_ids)
    defaults = dict(
        leg_id=n, slip_id=n, selection_key=key if key is not None else ("sel", n),
        logged_at=logged, start_time=start, sport=Sport.NFL, market=market, sportsbook="DraftKings",
        slip_type=SlipType.SINGLE, leg_count=1, is_placed=True, logged_by="a@example.com",
        line=D(line), odds=odds, result=result)
    return LegRow(**{**defaults, **kw})


def overall(rows, min_sample=MIN):
    (stats,) = an.group_selections(rows, None, min_sample)
    return stats


# --- Hit rate, interval, break-even, ROI ----------------------------------------------------


def test_the_specs_worked_example_18_wins_from_30_at_minus_110():
    rows = [leg(R.WIN) for _ in range(18)] + [leg(R.LOSS) for _ in range(12)]
    s = overall(rows)
    assert (s.wins, s.losses, s.n, s.hit_rate) == (18, 12, 30, pytest.approx(0.6))
    low, high = s.interval
    assert (round(low, 3), round(high, 3)) == (0.423, 0.754)
    assert s.break_even == pytest.approx(110 / 210)  # 52.38%
    assert s.edge == pytest.approx((0.6 - 110 / 210) * 100)  # +7.6 points
    # 60% is not yet evidence of an edge: the interval contains the break-even
    assert s.interval_beats_break_even is False
    assert not s.low_sample


def test_flat_one_unit_roi():
    rows = [leg(R.WIN) for _ in range(18)] + [leg(R.LOSS) for _ in range(12)]
    s = overall(rows)
    # each win pays 100/110 of a unit, each loss costs one
    assert s.profit_units == pytest.approx(18 * 100 / 110 - 12)
    assert s.roi == pytest.approx((18 * 100 / 110 - 12) / 30)


def test_roi_and_break_even_with_mixed_odds():
    rows = [leg(R.WIN, odds=+150), leg(R.LOSS, odds=-200)]
    s = overall(rows)
    assert s.profit_units == pytest.approx(1.5 - 1)
    assert s.roi == pytest.approx(0.25)
    assert s.break_even == pytest.approx((0.4 + 200 / 300) / 2)


def test_a_clear_edge_has_an_interval_above_break_even():
    rows = [leg(R.WIN, odds=+100) for _ in range(90)] + [leg(R.LOSS, odds=+100) for _ in range(10)]
    s = overall(rows)
    assert s.break_even == 0.5 and s.interval_beats_break_even is True


def test_pushes_are_counted_but_are_neither_a_win_nor_a_loss():
    s = overall([leg(R.WIN), leg(R.PUSH), leg(R.PUSH)])
    assert (s.wins, s.losses, s.pushes, s.n) == (1, 0, 2, 1)
    assert s.roi == pytest.approx(100 / 110)  # only the win was staked


def test_voids_and_pending_legs_are_not_in_the_unit():
    s = overall([leg(R.WIN), leg(R.VOID), leg(R.PENDING)])
    assert (s.wins, s.losses, s.pushes) == (1, 0, 0)


def test_an_sgp_leg_without_odds_counts_toward_hit_rate_only():
    rows = [leg(R.WIN, odds=None), leg(R.LOSS, odds=None), leg(R.WIN, odds=-110)]
    s = overall(rows)
    assert (s.n, s.hit_rate) == (3, pytest.approx(2 / 3))
    assert (s.n_odds, s.hit_rate_odds) == (1, 1.0)  # both sample sizes are available
    assert s.break_even == pytest.approx(110 / 210)


def test_a_group_with_no_legs_with_odds_has_no_break_even_or_roi():
    s = overall([leg(R.WIN, odds=None)])
    assert (s.break_even, s.roi, s.edge, s.interval_beats_break_even) == (None, None, None, None)


def test_an_empty_group_does_not_divide_by_zero():
    s = overall([])
    assert (s.n, s.hit_rate, s.interval, s.roi) == (0, None, None, None)
    assert s.low_sample


# --- Low sample and the unit ----------------------------------------------------------------


@pytest.mark.parametrize(("n", "low"), [(29, True), (30, False)])
def test_low_sample_below_min_sample(n, low):
    assert overall([leg() for _ in range(n)]).low_sample is low


def test_low_sample_uses_the_given_minimum():
    assert not overall([leg() for _ in range(5)], min_sample=5).low_sample


def test_identical_selections_count_once_using_the_earliest_record():
    early = leg(R.WIN, key=("k",), logged=LOGGED, odds=-110)
    later = leg(R.LOSS, key=("k",), logged=LOGGED + timedelta(hours=1), odds=+200)
    other = leg(R.LOSS, key=("other",))
    for rows in ([early, later, other], [later, other, early]):  # input order is irrelevant
        s = overall(rows)
        assert (s.wins, s.losses) == (1, 1)
        assert s.break_even == pytest.approx((110 / 210 + 110 / 210) / 2)  # the early -110


# --- Closing line value ---------------------------------------------------------------------


def test_price_clv_the_specs_example():
    # Over taken at -110 (52.38%), closing Over -125 / Under +105: no-vig 53.25%, so +0.87 pp
    row = leg(odds=-110, closing_line=D("45.5"), closing_odds=-125, closing_opposite_odds=+105)
    assert an.price_clv(row) == pytest.approx(0.8657, abs=1e-3)
    assert round(an.price_clv(row), 2) == 0.87


def test_price_clv_without_the_opposite_price_uses_the_raw_implied_probability():
    row = leg(odds=-110, closing_line=D("45.5"), closing_odds=-125)
    assert an.price_clv(row) == pytest.approx((125 / 225 - 110 / 210) * 100)


def test_price_clv_is_negative_when_the_price_got_worse():
    row = leg(odds=-125, closing_line=D("45.5"), closing_odds=-110, closing_opposite_odds=-110)
    assert an.price_clv(row) == pytest.approx((0.5 - 125 / 225) * 100)


def test_price_clv_needs_the_same_line_at_the_close():
    moved = leg(closing_line=D("47.5"), closing_odds=-110)
    assert an.price_clv(moved) is None
    assert an.price_clv(leg()) is None  # no closing line at all
    assert an.price_clv(leg(odds=None, closing_line=D("45.5"), closing_odds=-110)) is None


def test_line_clv_for_overs_is_closing_minus_taken():
    assert an.line_clv(leg(line="45.5", closing_line=D("47.5"))) == 2.0
    assert an.line_clv(leg(line="45.5", closing_line=D("44.5"))) == -1.0
    assert an.line_clv(leg(market=M.PLAYER_RECEPTIONS, line="5.5", closing_line=D("6.5"))) == 1.0


def test_line_clv_for_alt_spreads_is_taken_minus_closing():
    # took -3.5, the market moved to -6.5: our -3.5 is 3 points better than the close
    assert an.line_clv(leg(market=M.ALT_SPREAD, line="-3.5", closing_line=D("-6.5"))) == 3.0
    assert an.line_clv(leg(market=M.ALT_SPREAD, line="+3.5", closing_line=D("+2.5"))) == 1.0


def test_line_clv_only_exists_when_the_line_moved():
    assert an.line_clv(leg(closing_line=D("45.5"))) is None
    assert an.line_clv(leg()) is None


def test_clv_columns_are_separate_means_each_with_its_own_n():
    rows = [
        leg(odds=-110, closing_line=D("45.5"), closing_odds=-125, closing_opposite_odds=+105),
        leg(odds=-110, closing_line=D("45.5"), closing_odds=-110, closing_opposite_odds=-110),
        leg(line="45.5", closing_line=D("47.5"), closing_odds=-110),
        leg(),  # no closing line
    ]
    s = overall(rows)
    # +0.87 on the first; the second closed at a fair 50% against 52.38% taken: -2.38
    assert s.price_clv.n == 2
    assert s.price_clv.value == pytest.approx((0.8657 + (0.5 - 110 / 210) * 100) / 2, abs=1e-3)
    assert s.line_clv.n == 1 and s.line_clv.value == 2.0


def test_no_clv_data_gives_a_none_mean_with_n_zero():
    s = overall([leg(), leg()])
    assert (s.price_clv, s.line_clv) == (an.Mean(None, 0), an.Mean(None, 0))


# --- Dimensions -----------------------------------------------------------------------------


@pytest.mark.parametrize(("odds", "band"), [
    (-300, "≤ −150"), (-150, "≤ −150"), (-149, "−149 to −111"), (-111, "−149 to −111"),
    (-110, "−110 to +100"), (-105, "−110 to +100"), (100, "−110 to +100"),
    (101, "+101 to +150"), (150, "+101 to +150"), (151, "> +150"), (400, "> +150"),
    (None, "no odds")])
def test_odds_bands(odds, band):
    assert an.odds_band(odds) == band


def et(hour, minute=0, day=27):
    return datetime(2026, 9, day, hour, minute, tzinfo=ET)


@pytest.mark.parametrize(("start", "window"), [
    (et(13), "before 5pm"), (et(16, 59), "before 5pm"), (et(17), "5–8pm"),
    (et(19, 59), "5–8pm"), (et(20), "after 8pm"), (et(23), "after 8pm")])
def test_start_windows_are_eastern(start, window):
    assert an.start_window(start) == window


@pytest.mark.parametrize(("hours", "bucket"), [
    (-0.5, "logged after the start"), (0.0, "under 1 h"), (0.99, "under 1 h"), (1, "1–6 h"),
    (5.99, "1–6 h"), (6, "6–24 h"), (23.99, "6–24 h"), (24, "over 24 h"), (72, "over 24 h")])
def test_lead_times(hours, bucket):
    assert an.lead_time(START - timedelta(hours=hours), START) == bucket


def test_the_weekday_and_month_are_of_the_eastern_game_day():
    # 00:15 UTC on the 29th is 8:15 pm on the 28th, a Monday, in New York
    row = leg(start=datetime(2026, 9, 29, 0, 15, tzinfo=UTC))
    assert an.LEG_DIMENSIONS["weekday"][1](row) == "Monday"
    late_month_end = datetime(2026, 10, 1, 1, 0, tzinfo=UTC)  # September 30th, 9 pm ET
    assert an.game_month(late_month_end) == "2026-09"


def test_grouping_by_a_dimension_keeps_natural_order_and_counts_each_bucket():
    rows = [leg(odds=+120), leg(odds=-200), leg(odds=-110), leg(odds=-110), leg(odds=None)]
    groups = an.group_selections(rows, "odds_band", MIN)
    assert [(g.label, g.n) for g in groups] == [
        ("≤ −150", 1), ("−110 to +100", 2), ("+101 to +150", 1), ("no odds", 1)]
    assert all(g.low_sample for g in groups)


def test_buckets_without_a_natural_order_come_biggest_first():
    rows = [leg(sportsbook="B"), leg(sportsbook="A"), leg(sportsbook="A"), leg(sportsbook="C")]
    assert [(g.label, g.n) for g in an.group_selections(rows, "sportsbook", MIN)] == [
        ("A", 2), ("B", 1), ("C", 1)]


def test_grouping_dedupes_before_it_groups():
    rows = [leg(key=("k",), sportsbook="A"), leg(key=("k",), sportsbook="B")]
    groups = an.group_selections(rows, "sportsbook", MIN)
    assert [(g.label, g.n) for g in groups] == [("A", 1)]  # the earliest record only


# --- Filters --------------------------------------------------------------------------------


def test_filters_keep_only_the_chosen_buckets():
    rows = [leg(sportsbook="A"), leg(sportsbook="B"), leg(sportsbook="A", sport=Sport.NBA)]
    f = Filters(values={"sportsbook": frozenset({"A"}), "sport": frozenset({"nfl"})})
    assert [r.sportsbook for r in an.apply_filters(rows, f)] == ["A"]
    assert len(an.apply_filters(rows, Filters())) == 3
    assert len(an.apply_filters(rows, Filters(values={"sport": frozenset()}))) == 3  # empty = off


def test_tag_filter_any_and_all():
    rows = [leg(tag_ids=frozenset({1})), leg(tag_ids=frozenset({1, 2})),
            leg(tag_ids=frozenset({2})), leg()]
    any_of = Filters(tag_ids=frozenset({1, 2}), tag_mode="any")
    all_of = Filters(tag_ids=frozenset({1, 2}), tag_mode="all")
    assert len(an.apply_filters(rows, any_of)) == 3
    assert len(an.apply_filters(rows, all_of)) == 1


def test_the_multiple_comparisons_caution_appears_after_two_filters():
    one = Filters(values={"sport": frozenset({"nfl"})})
    two = Filters(values={"sport": frozenset({"nfl"}), "market": frozenset({"game_total"})})
    three = Filters(values={"sport": frozenset({"nfl"}), "market": frozenset({"game_total"})},
                    tag_ids=frozenset({1}))
    assert (one.active, two.active, three.active) == (1, 2, 3)
    assert an.caution(one) is None and an.caution(two) is None
    assert "chance" in an.caution(three)


def test_filter_options_list_what_is_in_the_data_in_display_order():
    rows = [leg(odds=-110), leg(odds=+200), leg(sportsbook="B")]
    options = an.filter_options(rows)
    assert options["odds_band"] == ["−110 to +100", "> +150"]
    assert options["sportsbook"] == ["B", "DraftKings"]


# --- Slips: money ---------------------------------------------------------------------------


def slip(status=S.WIN, stake="10.00", payout="19.09", placed=True, **kw) -> SlipRow:
    n = next(_ids)
    defaults = dict(
        slip_id=n, is_placed=placed, slip_type=SlipType.SINGLE, leg_count=1,
        sportsbook="DraftKings", logged_by="a@example.com", status=status,
        stake=D(stake) if placed else None, payout=None if payout is None else D(payout),
        start_time=START, settled_at=START + timedelta(hours=4, minutes=n))
    return SlipRow(**{**defaults, **kw})


def test_placed_slips_profit_and_roi():
    rows = [slip(S.WIN, "10.00", "19.09"), slip(S.LOSS, "10.00", "0.00"),
            slip(S.PUSH, "10.00", "10.00"), slip(S.PENDING, "10.00", None)]
    (m,) = an.group_slips(rows, None, placed=True, min_sample=MIN)
    assert (m.n, m.staked, m.returned, m.profit) == (3, D("30.00"), D("29.09"), D("-0.91"))
    assert m.roi == pytest.approx(-0.91 / 30)  # the pending slip isn't in it


def test_unplaced_slips_are_units_labelled_as_if_bet_at_one_unit():
    rows = [slip(S.WIN, payout="1.91", placed=False), slip(S.LOSS, payout="0.00", placed=False),
            slip(S.WIN, "10.00", "19.09")]  # a placed slip stays out
    (m,) = an.group_slips(rows, None, placed=False, min_sample=MIN)
    assert (m.n, m.staked, m.returned, m.profit) == (2, D("2"), D("1.91"), D("-0.09"))


def test_cashed_out_slips_count_with_the_amount_entered():
    (m,) = an.group_slips([slip(S.CASHED_OUT, "10.00", "6.50")], None, placed=True,
                          min_sample=MIN)
    assert m.profit == D("-3.50")


def test_a_group_with_no_stake_has_no_roi():
    (m,) = an.group_slips([], None, placed=True, min_sample=MIN)
    assert (m.n, m.roi) == (0, None) and m.low_sample


def test_slip_breakdowns_by_type_leg_count_sportsbook_and_month():
    rows = [
        slip(slip_type=SlipType.SINGLE), slip(slip_type=SlipType.PARLAY, leg_count=3),
        slip(slip_type=SlipType.PARLAY, leg_count=10), slip(slip_type=SlipType.PARLAY, leg_count=3,
                                                          sportsbook="FanDuel"),
    ]
    by = lambda dim: [(g.label, g.n) for g in an.group_slips(rows, dim, placed=True,  # noqa: E731
                                                            min_sample=MIN)]
    assert by("slip_type") == [("parlay", 3), ("single", 1)]
    assert by("leg_count") == [("1", 1), ("3", 2), ("10", 1)]  # numerically, not "10" < "3"
    assert by("sportsbook") == [("DraftKings", 3), ("FanDuel", 1)]
    assert by("month") == [("2026-09", 4)]


def test_months_are_in_calendar_order():
    rows = [slip(start_time=datetime(2026, 11, 1, 18, tzinfo=UTC)), slip(),
            slip(start_time=datetime(2026, 10, 4, 18, tzinfo=UTC))]
    assert [g.label for g in an.group_slips(rows, "month", placed=True, min_sample=MIN)] == [
        "2026-09", "2026-10", "2026-11"]


def test_cumulative_profit_runs_in_settlement_order_and_skips_the_unsettled():
    t = START
    rows = [
        slip(S.LOSS, "10.00", "0.00", settled_at=t + timedelta(hours=2)),
        slip(S.WIN, "10.00", "19.09", settled_at=t + timedelta(hours=1)),
        slip(S.PENDING, "10.00", None, settled_at=None),
        slip(S.WIN, "5.00", "9.55", placed=True, settled_at=t + timedelta(hours=3)),
        slip(S.WIN, payout="1.91", placed=False, settled_at=t),  # unplaced: its own series
    ]
    assert [(when, total) for when, total in an.cumulative_profit(rows)] == [
        (t + timedelta(hours=1), D("9.09")), (t + timedelta(hours=2), D("-0.91")),
        (t + timedelta(hours=3), D("3.64"))]
    assert an.cumulative_profit(rows, placed=False) == [(t, D("0.91"))]
