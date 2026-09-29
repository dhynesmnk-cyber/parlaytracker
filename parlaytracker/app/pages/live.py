"""Live: where each pending NFL bet stands right now (SPEC.md section 9.4).

Only the live section reruns every 15 seconds (`st.fragment`). Live values are for watching:
nothing on this page settles anything, and a settled result always comes from the box score
after the game (section 7.1).
"""
from datetime import UTC, datetime
from decimal import Decimal

import streamlit as st

from parlaytracker.app import common
from parlaytracker.core import live
from parlaytracker.core.db import session_scope
from parlaytracker.core.live import Card, LegView, Severity, State, Verification
from parlaytracker.core.models import LegResult

REFRESH_SECONDS = 15
NOT_TRACKED = "Not tracked live: settles after the game"
COLOURS = {Severity.OK: "green", Severity.AMBER: "orange", Severity.RED: "red"}
STATE_WORDS = {State.OVER: "over", State.UNDER: "under", State.LEVEL: "level",
               State.UNKNOWN: "no value yet"}
SPREAD_WORDS = {State.OVER: "covering", State.UNDER: "not covering"}


def now() -> datetime:
    return datetime.now(tz=UTC)


def fmt_value(value: Decimal | None) -> str:
    return "—" if value is None else f"{value:.1f}".removesuffix(".0")


def fmt_age(seconds: int | None) -> str:
    if seconds is None:
        return "never"
    return f"{seconds} s ago" if seconds < 120 else f"{seconds // 60} min ago"


def leg_line(view: LegView) -> str:
    """One leg: what it is, and where it stands."""
    leg = view.leg
    text = common.leg_summary(leg)
    if view.settled is not None:
        return f"{text} — **{view.settled.value}**"
    if not view.tracked:
        return f"{text} — _{NOT_TRACKED}_"
    if view.live_value is None and view.state is State.UNKNOWN:
        standing = "no stat line yet" if leg.market_type.value.startswith("player_") \
            else "waiting for a score"
    else:
        words = SPREAD_WORDS if leg.market_type.value == "alt_spread" else STATE_WORDS
        standing = f"{fmt_value(view.live_value)} · {words.get(view.state, 'level')}"
    return f"{text} — **{standing}** · {view.status_text}".rstrip(" ·")


def show_card(card: Card) -> None:
    with st.container(border=True):
        st.markdown(f"**{common.slip_summary(card.slip)}**")
        for view in card.legs:
            st.markdown(leg_line(view))
        if card.progress:
            st.markdown(f"**{card.progress}**")
        colour = COLOURS[card.severity]
        provider = card.provider.value if card.provider else "no provider yet"
        st.markdown(f":{colour}[updated {fmt_age(card.updated_seconds_ago)} · via {provider}]")
        if card.no_change_min is not None:
            st.markdown(f":orange[No change for {card.no_change_min} min]")


@st.fragment(run_every=REFRESH_SECONDS)
def live_section() -> None:
    moment = now()
    with session_scope() as session:
        cards = live.live_cards(session, moment)
        since = live.load_espn_unavailable_since(session)
    if since is not None:
        st.error(f"Live data unavailable since {common.fmt_time(since)} (ESPN is blocking or "
                 "erroring). Results will still settle after the game.")
    for card in cards:
        show_card(card)
    if not cards:
        st.info("Nothing to watch right now. A card appears for a pending slip from half an "
                "hour before an NFL game kicks off until it finishes.")
    st.caption(f"Refreshed {common.fmt_time(moment)}")


def show_settled() -> None:
    st.subheader("Settled in the last 7 days")
    with session_scope() as session:
        rows = live.recent_settled(session, now())
    if not rows:
        st.caption("Nothing has settled in the last 7 days.")
    for row in rows:
        slip = row.slip
        returned = "" if slip.payout is None else (
            f" · returned {common.fmt_money(slip.payout, slip.is_placed)}")
        st.markdown(f"{common.slip_summary(slip)} — **{slip.status.value}**{returned}")
        for leg, label in zip(slip.legs, row.verifications, strict=True):
            result = "pending" if leg.result is LegResult.PENDING else leg.result.value
            note = f" · {label.value}" if label is not Verification.NOT_APPLICABLE else ""
            st.caption(f"{common.leg_summary(leg)} — {result}{note}")


def render() -> None:
    st.header("Live")
    live_section()
    st.divider()
    show_settled()
