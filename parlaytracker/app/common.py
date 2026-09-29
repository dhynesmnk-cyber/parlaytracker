"""Helpers shared by the pages: formatting, cached ESPN lookups, health banner, messages."""
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import streamlit as st
from sqlalchemy import select

from parlaytracker.core.config import get_settings
from parlaytracker.core.db import session_scope
from parlaytracker.core.models import (
    FailureKind,
    HealthState,
    MarketType,
    Slip,
    SourceHealth,
    Sport,
    TeamSide,
)
from parlaytracker.ingest import espn

MARKET_LABELS = {
    MarketType.GAME_TOTAL: "Game total",
    MarketType.TEAM_TOTAL: "Team total",
    MarketType.ALT_SPREAD: "Alt spread",
    MarketType.PLAYER_RECEPTIONS: "Receptions",
    MarketType.PLAYER_RECEIVING_YARDS: "Receiving yards",
    MarketType.PLAYER_RUSHING_YARDS: "Rushing yards",
    MarketType.PLAYER_PASSING_YARDS: "Passing yards",
    MarketType.PLAYER_POINTS: "Points",
    MarketType.OTHER: "Other (settled by hand)",
}

# Web requests may wait briefly for a rate-limit slot; the worker never waits (section 6.1).
WEB_MAX_WAIT = 5.0
WORKER_STALE_AFTER = timedelta(minutes=3)
SOURCE_OPEN_WARN_AFTER = timedelta(minutes=5)
# One capture costs a credit per market, so warn a few calls before the reserve is reached.
MARKETS_PER_CAPTURE = 10


def display_tz() -> ZoneInfo:
    return ZoneInfo(get_settings().display_tz)


def fmt_time(moment: datetime | None) -> str:
    if moment is None:
        return "—"
    return moment.astimezone(display_tz()).strftime("%a %d %b %H:%M")


def fmt_odds(odds: int | None) -> str:
    return "—" if odds is None else f"{odds:+d}"


def fmt_money(amount: Decimal | None, placed: bool = True) -> str:
    if amount is None:
        return "—"
    return f"${amount:,.2f}" if placed else f"{amount:.2f}u"


def _num(value: Decimal) -> str:
    return f"{value:.1f}".removesuffix(".0")


def fmt_line(line: Decimal | None) -> str:
    """A spread as shown on a slip: +3.5, -7."""
    if line is None:
        return ""
    return f"+{_num(line)}" if line > 0 else _num(line)


def _side_team(leg) -> str:
    return leg.event.home_team if leg.side is TeamSide.HOME else leg.event.away_team


def leg_summary(leg) -> str:
    """One line describing a leg, e.g. 'PHI @ CHI · Rushing yards · Saquon Barkley o84.5 -115'."""
    game = f"{leg.event.away_team} @ {leg.event.home_team}"
    odds = fmt_odds(leg.american_odds) if leg.american_odds is not None else ""
    market = MARKET_LABELS[leg.market_type]
    if leg.market_type is MarketType.OTHER:
        pick = leg.description
    elif leg.market_type is MarketType.ALT_SPREAD:
        pick = f"{market} · {_side_team(leg)} {fmt_line(leg.line)}"
    elif leg.market_type is MarketType.TEAM_TOTAL:
        pick = f"{market} · {_side_team(leg)} o{_num(leg.line)}"
    elif leg.market_type is MarketType.GAME_TOTAL:
        pick = f"{market} o{_num(leg.line)}"
    else:
        pick = f"{market} · {leg.player_name or 'player ' + leg.espn_athlete_id} o{_num(leg.line)}"
    return f"{game} · {pick} {odds}".rstrip()


def slip_summary(slip: Slip) -> str:
    kind = {"single": "Single", "parlay": "Parlay", "sgp": "Same-game parlay"}[slip.slip_type.value]
    money = (f"{fmt_money(slip.stake)} to win" if slip.is_placed else "Not placed")
    return (f"{kind} {fmt_odds(slip.american_odds)} · {slip.sportsbook.name} · {money} · "
            f"logged by {slip.logged_by}")


def flash(message: str, kind: str = "success") -> None:
    """Show a message on the next run (used by callbacks)."""
    st.session_state.setdefault("_flash", []).append((kind, message))


def show_flashes() -> None:
    for kind, message in st.session_state.pop("_flash", []):
        getattr(st, kind)(message)


@st.cache_data(ttl=600, show_spinner=False)
def scoreboard(sport: str, day: date) -> list[espn.Game]:
    """Games for a game day, cached for 10 minutes. Errors are raised, never cached."""
    return espn.fetch_scoreboard(Sport(sport), day, max_wait=WEB_MAX_WAIT).games


@st.cache_data(ttl=12 * 3600, show_spinner=False)
def roster(sport: str, team_id: str) -> list[espn.RosterPlayer]:
    return espn.fetch_roster(Sport(sport), team_id, max_wait=WEB_MAX_WAIT)


def health_banner(now: datetime | None = None) -> None:
    """Warn when the worker has stopped or a data source is failing (section 9.2)."""
    now = now or datetime.now(tz=UTC)
    with session_scope() as session:
        rows = {r.source: r for r in session.scalars(select(SourceHealth))}
        worker = rows.pop("worker", None)
        if worker is None or worker.last_success_at is None:
            st.warning("The background worker has never run on this database.")
        elif now - worker.last_success_at > WORKER_STALE_AFTER:
            st.warning(f"The background worker was last seen {fmt_time(worker.last_success_at)}. "
                       "Live data and closing lines are paused until it's back.")
        odds = rows.get("odds_api")
        reserve = get_settings().odds_api_reserve
        if odds is not None and odds.quota_remaining is not None and (
                odds.quota_remaining < reserve + MARKETS_PER_CAPTURE):
            st.warning(f"The Odds API has {odds.quota_remaining} credits left this month. "
                       f"Closing lines stop being captured at {reserve}; enter them by hand "
                       "on the Review page after that.")
        for row in rows.values():
            if row.state is not HealthState.OPEN:
                continue
            if row.failure_kind is FailureKind.SCHEMA:
                st.error(f"{row.source}: the data format changed; the parser needs updating.")
                continue
            failing_since = row.last_success_at or row.last_failure_at
            if failing_since and now - failing_since > SOURCE_OPEN_WARN_AFTER:
                st.warning(f"{row.source} is failing ({row.failure_kind}); last worked "
                           f"{fmt_time(row.last_success_at)}.")
