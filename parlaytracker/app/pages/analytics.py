"""Analytics: where is the edge, and what did the money do? (SPEC.md section 10)

Two views. Selections work per leg and pool both users; Money works per slip. Every figure
shows its sample size, small groups are greyed out and labelled, and CLV comes first because
it says something long before ROI does.
"""
import pandas as pd
import streamlit as st
from sqlalchemy import select

from parlaytracker.core import analytics as an
from parlaytracker.core.analytics import (
    LEG_DIMENSIONS,
    SLIP_DIMENSIONS,
    Filters,
    MoneyStats,
    SelectionStats,
)
from parlaytracker.core.config import get_settings
from parlaytracker.core.db import session_scope
from parlaytracker.core.models import Tag

DASH = "—"
ALL = "all"
LOW_SAMPLE = "low sample"


# --- Formatting (pure) -------------------------------------------------------------------------


def pct(value: float | None, digits: int = 1) -> str:
    return DASH if value is None else f"{value * 100:.{digits}f}%"


def signed(value: float | None, unit: str, digits: int = 1) -> str:
    return DASH if value is None else f"{value:+.{digits}f} {unit}"


def hit_rate_text(s: SelectionStats) -> str:
    """60.0% (42–75%): the interval always sits next to the rate (section 10.3)."""
    if s.hit_rate is None:
        return DASH
    low, high = s.interval
    return f"{pct(s.hit_rate)} ({low * 100:.0f}–{high * 100:.0f}%)"


def evidence_text(s: SelectionStats) -> str:
    beats = s.interval_beats_break_even
    if beats is None:
        return DASH
    return "interval above break-even" if beats else "not yet evidence of an edge"


def clv_text(mean: an.Mean, unit: str, digits: int) -> str:
    return DASH if mean.value is None else f"{signed(mean.value, unit, digits)} (n {mean.n})"


def selection_frame(stats: list[SelectionStats], group_label: str) -> pd.DataFrame:
    """One row per group, in the order the columns are read: CLV first, then hit rate."""
    return pd.DataFrame([{
        group_label: s.label,
        "n": s.n,
        "Price CLV": clv_text(s.price_clv, "pp", 2),
        "Line CLV": clv_text(s.line_clv, "pts", 1),
        "Hit rate (95% interval)": hit_rate_text(s),
        "Break-even": pct(s.break_even) + (f" (n {s.n_odds})" if s.n_odds != s.n else ""),
        "Edge": signed(s.edge, "pp"),
        "ROI (1 unit)": DASH if s.roi is None else f"{s.roi * 100:+.1f}%",
        "Evidence": evidence_text(s),
        "Push": s.pushes,
        "Note": LOW_SAMPLE if s.low_sample else "",
    } for s in stats])


def money_frame(stats: list[MoneyStats], group_label: str, units: bool) -> pd.DataFrame:
    suffix = " (units)" if units else ""

    def fmt(amount) -> str:
        return f"{amount:+.2f}" if units else f"{amount:.2f}"

    return pd.DataFrame([{
        group_label: m.label,
        "n": m.n,
        f"Staked{suffix}": f"{m.staked:.2f}",
        f"Returned{suffix}": f"{m.returned:.2f}",
        f"Profit{suffix}": fmt(m.profit),
        "ROI": DASH if m.roi is None else f"{m.roi * 100:+.1f}%",
        "Note": LOW_SAMPLE if m.low_sample else "",
    } for m in stats])


def grey_low_sample(row: pd.Series) -> list[str]:
    """Grey out a whole row whose `Note` says low sample."""
    style = "color: #9aa0a6" if row.get("Note") == LOW_SAMPLE else ""
    return [style] * len(row)


def show_table(frame: pd.DataFrame) -> None:
    st.dataframe(frame.style.apply(grey_low_sample, axis=1), hide_index=True)


# --- The page ----------------------------------------------------------------------------------


def _filters(options: dict[str, list[str]], tags: list[Tag]) -> Filters:
    values: dict[str, frozenset[str]] = {}
    with st.expander("Filters"):
        for dim, (label, _, _) in LEG_DIMENSIONS.items():
            if len(options[dim]) > 1:
                chosen = st.multiselect(label, options[dim], key=f"an_f_{dim}")
                values[dim] = frozenset(chosen)
        tag_ids: frozenset[int] = frozenset()
        mode = "any"
        if tags:
            names = {t.id: f"{t.category}: {t.name}" for t in tags}
            chosen_tags = st.multiselect("Tags", list(names), format_func=names.get,
                                         key="an_f_tags")
            tag_ids = frozenset(chosen_tags)
            if len(tag_ids) > 1:
                mode = st.radio("Match", ["any", "all"], horizontal=True, key="an_f_tagmode")
    return Filters(values, tag_ids, mode)


def _selections(leg_rows: list[an.LegRow], tags: list[Tag], min_sample: int) -> None:
    st.caption("Every settled leg, both of you together. The same selection logged twice "
               "counts once. Hit rate is wins ÷ (wins + losses); pushes and voids are not in it.")
    filters = _filters(an.filter_options(leg_rows), tags)
    rows = an.apply_filters(leg_rows, filters)
    if warning := an.caution(filters):
        st.warning(warning)

    (overall,) = an.group_selections(rows, None, min_sample)
    st.subheader("Closing line value")
    price, line = st.columns(2)
    price.metric("Price CLV, same line at close",
                 DASH if overall.price_clv.value is None else
                 signed(overall.price_clv.value, "pp", 2),
                 help="The no-vig closing probability minus the implied probability of the "
                      "odds taken, for legs whose closing line is the line you took.")
    price.caption(f"n {overall.price_clv.n}")
    line.metric("Line CLV, line moved",
                DASH if overall.line_clv.value is None else signed(overall.line_clv.value, "pts"),
                help="Points the line moved in your favour, for legs whose closing line "
                     "differs from the one taken.")
    line.caption(f"n {overall.line_clv.n}")
    if not overall.price_clv.n and not overall.line_clv.n:
        st.info("No legs have a closing line yet: they are captured shortly before kickoff "
                "once the worker runs, or entered on the Review page.")

    st.subheader("By group")
    # A real value for "no grouping": Streamlit reads None as "nothing selected".
    choices = {ALL: "Nothing: all legs together"} | {k: v[0] for k, v in LEG_DIMENSIONS.items()}
    choice = st.selectbox("Group by", list(choices), format_func=choices.get, index=1,
                          key="an_group")
    dimension = None if choice == ALL else choice
    stats = an.group_selections(rows, dimension, min_sample)
    if not overall.n:
        st.info("No settled legs match yet.")
        return
    show_table(selection_frame(stats, choices[dimension] if dimension else "Group"))
    st.caption(f"Greyed-out rows have fewer than {min_sample} decided legs. Break-even is the "
               "mean implied probability of the odds taken, over the legs that have odds "
               "(SGP legs without their own odds count toward hit rate only).")


def _money(slip_rows: list[an.SlipRow], min_sample: int) -> None:
    dimension = st.selectbox("Break down by", list(SLIP_DIMENSIONS), index=0,
                             format_func=lambda k: SLIP_DIMENSIONS[k][0], key="an_slip_group")
    label = SLIP_DIMENSIONS[dimension][0]
    st.subheader("Placed slips")
    placed = an.group_slips(slip_rows, dimension, placed=True, min_sample=min_sample)
    if any(m.n for m in placed):
        show_table(money_frame(placed, label, units=False))
        series = an.cumulative_profit(slip_rows, placed=True)
        if series:
            st.caption("Cumulative profit, in the order slips settled")
            st.line_chart(pd.DataFrame({"Profit": [float(p) for _, p in series]},
                                       index=[t for t, _ in series]))
    else:
        st.info("No placed slip has settled yet.")
    st.subheader("Unplaced slips: if bet at 1 unit")
    unplaced = an.group_slips(slip_rows, dimension, placed=False, min_sample=min_sample)
    if any(m.n for m in unplaced):
        show_table(money_frame(unplaced, label, units=True))
    else:
        st.info("No unplaced slip has settled yet.")


def render() -> None:
    st.title("Analytics")
    min_sample = get_settings().min_sample
    with session_scope() as session:
        leg_rows = an.load_leg_rows(session)
        slip_rows = an.load_slip_rows(session)
        tags = list(session.scalars(select(Tag).order_by(Tag.category, Tag.name)))
    if not slip_rows:
        st.info("Nothing logged yet. Log a bet, and this page fills in as games finish.")
        return
    selections, money = st.tabs(["Selections", "Money"])
    with selections:
        _selections(leg_rows, tags, min_sample)
    with money:
        _money(slip_rows, min_sample)
