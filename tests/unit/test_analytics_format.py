"""How the Analytics page words its numbers (SPEC.md section 10.3)."""
from decimal import Decimal as D

import pandas as pd
from support import analytics_leg as leg

from parlaytracker.app.pages import analytics as page
from parlaytracker.core import analytics as an
from parlaytracker.core.models import LegResult as R


def stats(rows, min_sample=30):
    return an.group_selections(rows, None, min_sample)


def test_the_specs_example_reads_60_percent_with_its_interval():
    (s,) = stats([leg(R.WIN) for _ in range(18)] + [leg(R.LOSS) for _ in range(12)])
    assert page.hit_rate_text(s) == "60.0% (42–75%)"
    assert page.evidence_text(s) == "not yet evidence of an edge"


def test_a_clear_edge_says_the_interval_is_above_break_even():
    (s,) = stats([leg(R.WIN, odds=100) for _ in range(90)]
                 + [leg(R.LOSS, odds=100) for _ in range(10)])
    assert page.evidence_text(s) == "interval above break-even"


def test_missing_values_show_a_dash_never_a_zero():
    (s,) = stats([])
    assert (page.hit_rate_text(s), page.evidence_text(s)) == (page.DASH, page.DASH)
    assert page.pct(None) == page.signed(None, "pp") == page.DASH
    assert page.clv_text(an.Mean(None, 0), "pp", 2) == page.DASH


def test_number_formats():
    assert page.pct(0.5238) == "52.4%"
    assert page.signed(0.8657, "pp", 2) == "+0.87 pp"
    assert page.signed(-2.0, "pts") == "-2.0 pts"
    assert page.clv_text(an.Mean(2.0, 4), "pts", 1) == "+2.0 pts (n 4)"


def test_the_selection_table_leads_with_clv_and_flags_low_samples():
    rows = [leg(R.WIN, closing_line=D("47.5")) for _ in range(3)]
    frame = page.selection_frame(stats(rows), "Group")
    assert list(frame.columns)[:4] == ["Group", "n", "Price CLV", "Line CLV"]
    (row,) = frame.to_dict("records")
    assert row["n"] == 3 and row["Note"] == "low sample"
    assert row["Line CLV"] == "+2.0 pts (n 3)" and row["Price CLV"] == page.DASH


def test_the_breakeven_column_shows_its_own_n_when_it_differs_from_the_hit_rate_n():
    rows = [leg(R.WIN, odds=None), leg(R.WIN, odds=-110), leg(R.LOSS, odds=-110)]
    (row,) = page.selection_frame(stats(rows), "Group").to_dict("records")
    assert row["n"] == 3 and row["Break-even"] == "52.4% (n 2)"


def test_a_group_at_the_minimum_is_not_flagged():
    rows = [leg(R.WIN) for _ in range(30)]
    (row,) = page.selection_frame(stats(rows), "Group").to_dict("records")
    assert row["Note"] == ""


def test_low_sample_rows_are_greyed_and_others_are_not():
    frame = pd.DataFrame([{"n": 3, "Note": "low sample"}, {"n": 40, "Note": ""}])
    styled = frame.style.apply(page.grey_low_sample, axis=1)
    css = styled._compute().ctx
    assert css[(0, 0)] and css[(0, 1)] and "9aa0a6" in css[(0, 0)][0][1]
    assert not css.get((1, 0))
    assert page.grey_low_sample(frame.iloc[1]) == ["", ""]


def test_money_table_labels_unplaced_slips_as_units():
    m = an.MoneyStats("single", 2, D("2"), D("1.91"), True)
    (placed,) = page.money_frame([m], "Slip type", units=False).to_dict("records")
    (unplaced,) = page.money_frame([m], "Slip type", units=True).to_dict("records")
    assert placed["Profit"] == "-0.09" and "Staked" in placed
    assert unplaced["Profit (units)"] == "-0.09" and "Staked (units)" in unplaced
    assert placed["ROI"] == "-4.5%" and placed["Note"] == "low sample"


def test_money_with_nothing_staked_shows_a_dash_for_roi():
    (row,) = page.money_frame([an.MoneyStats("x", 0, D(0), D(0), True)], "G",
                              units=False).to_dict("records")
    assert row["ROI"] == page.DASH
