"""The one slip form, used by Log now and by Screenshot in Phase 6 (SPEC.md section 9.3).

Widget values live in st.session_state under per-leg keys. Defaults for new widgets are kept
under separate `*_default` keys, so the code never writes to a widget's own key once it
exists. Saving runs in a button callback: it can then reset the form before the next run.
"""
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal

import streamlit as st
from pydantic import ValidationError

from parlaytracker.app import common
from parlaytracker.core import services
from parlaytracker.core.db import session_scope
from parlaytracker.core.markets import markets_for
from parlaytracker.core.models import (
    EntrySource,
    EventStatus,
    MarketType,
    SlipType,
    Sport,
    TeamSide,
)
from parlaytracker.core.odds import american_odds, parlay_decimal
from parlaytracker.core.schemas import LegIn, SlipIn
from parlaytracker.ingest import espn
from parlaytracker.ingest.http import FetchError, RateLimited

PLAYER_MARKETS = {m for m in MarketType if m.value.startswith("player_")}
TEAM_MARKETS = {MarketType.TEAM_TOTAL, MarketType.ALT_SPREAD}
SLIP_KEYS = ("slip_sgp", "slip_odds", "slip_placed", "slip_stake", "slip_payout",
             "slip_boosted", "slip_notes")
LEG_FIELDS = ("sport", "day", "game", "market", "player", "side", "desc", "line", "odds", "tags")


def _k(uid: int, name: str) -> str:
    return f"leg{uid}_{name}"


@dataclass
class LegDraft:
    uid: int
    sport: Sport
    day: date
    game: espn.Game | None
    market: MarketType
    athlete_id: str | None = None
    player_name: str | None = None
    side: TeamSide | None = None
    description: str | None = None
    line: Decimal | None = None
    odds: int | None = None
    tag_ids: list[int] = field(default_factory=list)


# --- Cached lookups that never raise into the page -----------------------------------------


def _games(sport: Sport, day: date) -> tuple[list[espn.Game], str | None]:
    try:
        return common.scoreboard(sport.value, day), None
    except (FetchError, RateLimited) as e:
        return [], f"Couldn't load {sport.value.upper()} games from ESPN ({e}). Try again shortly."


def _players(sport: Sport, game: espn.Game) -> tuple[dict[str, tuple[str, str]], str | None]:
    """{athlete id: (name, label)} for both teams, away team first."""
    players: dict[str, tuple[str, str]] = {}
    for team in (game.away, game.home):
        try:
            for p in common.roster(sport.value, team.espn_id):
                note = " · out" if p.unavailable else ""
                players.setdefault(p.espn_athlete_id,
                                   (p.name, f"{p.label} · {team.abbreviation}{note}"))
        except (FetchError, RateLimited) as e:
            return players, f"Couldn't load the {team.abbreviation} roster from ESPN ({e})."
    return players, None


def _game_label(game: espn.Game) -> str:
    status = "" if game.status is EventStatus.SCHEDULED else f" · {game.status_detail}"
    return f"{game.label} · {common.fmt_time(game.start_time)}{status}"


# --- Form state ----------------------------------------------------------------------------


def _new_leg(sport: Sport, day: date, game_id: str | None) -> int:
    uid = st.session_state.get("slip_next_uid", 1)
    st.session_state["slip_next_uid"] = uid + 1
    st.session_state[_k(uid, "sport_default")] = sport
    st.session_state[_k(uid, "day_default")] = day
    st.session_state[_k(uid, "game_default")] = game_id
    st.session_state.setdefault("slip_legs", []).append(uid)
    return uid


def _init() -> None:
    if not st.session_state.get("slip_legs"):
        _new_leg(st.session_state.get("last_sport", Sport.NFL),
                 st.session_state.get("last_day", espn.today_game_day()),
                 st.session_state.get("last_game"))


def _add_leg() -> None:
    last = st.session_state["slip_legs"][-1]
    _new_leg(st.session_state.get(_k(last, "sport"), Sport.NFL),
             st.session_state.get(_k(last, "day"), espn.today_game_day()),
             st.session_state.get(_k(last, "game")))


def _forget_leg(uid: int) -> None:
    for name in LEG_FIELDS:
        for key in (_k(uid, name), _k(uid, f"{name}_default")):
            st.session_state.pop(key, None)


def _remove_leg(uid: int) -> None:
    st.session_state["slip_legs"].remove(uid)
    _forget_leg(uid)


def _read_leg(uid: int) -> LegDraft:
    """The current values of one leg, from session state."""
    ss = st.session_state
    sport = ss.get(_k(uid, "sport"), ss.get(_k(uid, "sport_default"), Sport.NFL))
    day = ss.get(_k(uid, "day"), ss.get(_k(uid, "day_default"), espn.today_game_day()))
    games, _ = _games(sport, day)
    game_id = ss.get(_k(uid, "game"))
    game = next((g for g in games if g.espn_event_id == game_id), None)
    market = ss.get(_k(uid, "market"), MarketType.GAME_TOTAL)
    draft = LegDraft(uid, sport, day, game, market, tag_ids=list(ss.get(_k(uid, "tags"), [])))
    if market in PLAYER_MARKETS:
        draft.athlete_id = ss.get(_k(uid, "player"))
        if game and draft.athlete_id:
            players, _ = _players(sport, game)
            draft.player_name = players.get(draft.athlete_id, (None, None))[0]
    elif market in TEAM_MARKETS:
        draft.side = ss.get(_k(uid, "side"))
    elif market is MarketType.OTHER:
        draft.description = (ss.get(_k(uid, "desc")) or "").strip() or None
    line = ss.get(_k(uid, "line"))
    draft.line = None if line is None or market is MarketType.OTHER else Decimal(str(line))
    odds = ss.get(_k(uid, "odds"))
    draft.odds = None if odds is None else int(odds)
    return draft


def _slip_type(drafts: list[LegDraft]) -> SlipType:
    if len(drafts) == 1:
        return SlipType.SINGLE
    ids = {d.game.espn_event_id if d.game else None for d in drafts}
    same_game = len(ids) == 1 and None not in ids
    return SlipType.SGP if same_game and st.session_state.get("slip_sgp", True) else SlipType.PARLAY


def _computed_parlay_odds(drafts: list[LegDraft]) -> int | None:
    odds = [d.odds for d in drafts]
    if None in odds or any(-100 < o < 100 for o in odds):
        return None
    return american_odds(parlay_decimal(odds))


def _slip_odds(drafts: list[LegDraft], slip_type: SlipType) -> int | None:
    if slip_type is SlipType.SINGLE:
        return drafts[0].odds
    entered = st.session_state.get("slip_odds")
    if entered is not None:
        return int(entered)
    return _computed_parlay_odds(drafts) if slip_type is SlipType.PARLAY else None


def _build(drafts: list[LegDraft], event_ids: dict[str, int], source: EntrySource) -> SlipIn:
    """A SlipIn from the form; raises ValueError or ValidationError with readable messages."""
    for n, d in enumerate(drafts, start=1):
        if d.game is None:
            raise ValueError(f"Leg {n}: choose a game")
    slip_type = _slip_type(drafts)
    odds = _slip_odds(drafts, slip_type)
    if odds is None:
        raise ValueError("Enter the slip's odds as shown by the sportsbook")
    ss = st.session_state
    placed = bool(ss.get("slip_placed", False))
    stake = ss.get("slip_stake") if placed else None
    payout = ss.get("slip_payout") if placed else None
    return SlipIn(
        is_placed=placed,
        slip_type=slip_type,
        sportsbook_id=ss.get("slip_book"),
        american_odds=odds,
        boosted=bool(ss.get("slip_boosted", False)) and slip_type is not SlipType.SINGLE,
        stake=None if stake is None else Decimal(str(stake)).quantize(Decimal("0.01")),
        potential_payout=None if payout is None else Decimal(str(payout)).quantize(Decimal("0.01")),
        source=source,
        notes=(ss.get("slip_notes") or "").strip() or None,
        legs=[
            LegIn(event_id=event_ids.get(d.game.espn_event_id, -1), market_type=d.market,
                  side=d.side, espn_athlete_id=d.athlete_id, player_name=d.player_name,
                  description=d.description, line=d.line, american_odds=d.odds,
                  tag_ids=d.tag_ids)
            for d in drafts
        ],
    )


def _messages(error: Exception) -> list[str]:
    if not isinstance(error, ValidationError):
        return [str(error)]
    out = []
    for e in error.errors():
        where = ""
        if len(e["loc"]) >= 2 and e["loc"][0] == "legs":
            where = f"Leg {e['loc'][1] + 1}: "
        elif e["loc"]:
            where = f"{str(e['loc'][-1]).replace('_', ' ').capitalize()}: "
        out.append(where + e["msg"].removeprefix("Value error, "))
    return out


def _save(source: EntrySource) -> None:
    """Button callback: validate, store the events and the slip, then reset the form."""
    drafts = [_read_leg(uid) for uid in st.session_state["slip_legs"]]
    try:
        with session_scope() as session:
            event_ids = {}
            for d in drafts:
                if d.game is not None and d.game.espn_event_id not in event_ids:
                    g = d.game
                    event = services.upsert_event(
                        session, sport=g.sport, espn_event_id=g.espn_event_id,
                        start_time=g.start_time, home_team=g.home.name, away_team=g.away.name,
                        home_espn_team_id=g.home.espn_id, away_espn_team_id=g.away.espn_id,
                        status=g.status)
                    event_ids[g.espn_event_id] = event.id
            data = _build(drafts, event_ids, source)
            slip = services.create_slip(session, data, st.session_state["login"])
            summary = common.slip_summary(slip)
    except (ValueError, ValidationError) as e:  # ServiceError is a ValueError
        st.session_state["slip_errors"] = _messages(e)
        return
    st.session_state.pop("slip_errors", None)
    common.flash(f"Saved: {summary}")
    first = drafts[0]
    st.session_state["last_sport"], st.session_state["last_day"] = first.sport, first.day
    st.session_state["last_game"] = first.game.espn_event_id if first.game else None
    st.session_state["last_book"] = st.session_state.get("slip_book")
    for uid in list(st.session_state["slip_legs"]):
        _forget_leg(uid)
    st.session_state["slip_legs"] = []
    for key in SLIP_KEYS:
        st.session_state.pop(key, None)


# --- Rendering -----------------------------------------------------------------------------


def _render_leg(uid: int, n: int, removable: bool, tags: dict[int, str]) -> None:
    ss = st.session_state
    with st.container(border=True):
        top = st.columns([3, 1]) if removable else [st.container()]
        top[0].markdown(f"**Leg {n}**")
        if removable:
            top[1].button("Remove", key=_k(uid, "remove"), on_click=_remove_leg, args=(uid,))
        c1, c2 = st.columns(2)
        sports = list(Sport)
        sport = c1.selectbox("Sport", sports, key=_k(uid, "sport"),
                             index=sports.index(ss.get(_k(uid, "sport_default"), Sport.NFL)),
                             format_func=lambda s: s.value.upper())
        day = c2.date_input("Game day (US Eastern)", key=_k(uid, "day"),
                            value=ss.get(_k(uid, "day_default"), espn.today_game_day()))
        games, problem = _games(sport, day)
        if problem:
            st.warning(problem)
        by_id = {g.espn_event_id: g for g in games}
        default = ss.get(_k(uid, "game_default"))
        game_id = st.selectbox(
            "Game", list(by_id), key=_k(uid, "game"),
            index=list(by_id).index(default) if default in by_id else None,
            format_func=lambda i: _game_label(by_id[i]), placeholder="Choose a game")
        game = by_id.get(game_id)
        market = st.selectbox("Market", markets_for(sport), key=_k(uid, "market"),
                              format_func=common.MARKET_LABELS.get)
        if market in PLAYER_MARKETS:
            if game is None:
                st.caption("Choose a game to pick a player.")
            else:
                players, problem = _players(sport, game)
                if problem:
                    st.warning(problem)
                st.selectbox("Player", list(players), key=_k(uid, "player"), index=None,
                             format_func=lambda i: players[i][1], placeholder="Type a name")
        elif market in TEAM_MARKETS:
            if game is None:
                st.caption("Choose a game to pick a team.")
            else:
                st.radio("Team", [TeamSide.AWAY, TeamSide.HOME], key=_k(uid, "side"),
                         horizontal=True, index=None,
                         format_func=lambda s: game.away.name if s is TeamSide.AWAY
                         else game.home.name)
        elif market is MarketType.OTHER:
            st.text_input("What was the pick? (e.g. Bears moneyline, Under 44.5)",
                          key=_k(uid, "desc"))
        c1, c2 = st.columns(2)
        if market is not MarketType.OTHER:
            label = "Spread (e.g. -7.5)" if market is MarketType.ALT_SPREAD else "Over line"
            c1.number_input(label, key=_k(uid, "line"), value=None, step=0.5, format="%.1f")
        c2.number_input("Odds (American)", key=_k(uid, "odds"), value=None, step=1,
                        format="%d", help="e.g. -110 or 150. Leave blank on a same-game "
                        "parlay leg if the slip doesn't show it.")
        if tags:
            st.multiselect("Tags", list(tags), key=_k(uid, "tags"), format_func=tags.get)


def _render_slip(drafts: list[LegDraft], books: dict[int, str]) -> SlipType:
    ss = st.session_state
    slip_type = SlipType.SINGLE
    if len(drafts) > 1:
        ids = {d.game.espn_event_id if d.game else None for d in drafts}
        if len(ids) == 1 and None not in ids:
            st.toggle("Same-game parlay", value=True, key="slip_sgp")
        slip_type = _slip_type(drafts)
        if slip_type is SlipType.PARLAY:
            computed = _computed_parlay_odds(drafts)
            hint = (f"Leave blank to use the legs' {common.fmt_odds(computed)}"
                    if computed is not None else "Enter every leg's odds, or the slip's")
            st.number_input("Slip odds as shown", key="slip_odds", value=None, step=1,
                            format="%d", help=hint, placeholder=hint)
        else:
            st.number_input("Slip odds as shown", key="slip_odds", value=None, step=1,
                            format="%d", placeholder="Same-game parlay odds, e.g. 450")
    book_ids = list(books)
    last_book = ss.get("last_book")
    st.selectbox("Sportsbook", book_ids, key="slip_book", format_func=books.get,
                 index=book_ids.index(last_book) if last_book in books else 0)
    placed = st.toggle("I placed this bet", key="slip_placed")
    if placed:
        c1, c2 = st.columns(2)
        c1.number_input("Stake ($)", key="slip_stake", value=None, min_value=0.01, step=5.0,
                        format="%.2f")
        c2.number_input("Potential payout ($, optional)", key="slip_payout", value=None,
                        min_value=0.01, step=5.0, format="%.2f")
    if slip_type is not SlipType.SINGLE:
        st.checkbox("Odds boost applied", key="slip_boosted")
    st.text_input("Notes (optional)", key="slip_notes")
    return slip_type


def _live_warnings(drafts: list[LegDraft], source: EntrySource) -> list[str]:
    """Non-blocking warnings for a complete draft (section 5), plus kickoff already passed."""
    now = datetime.now(tz=UTC)
    started = [f"Leg {n}: this game has already started"
               for n, d in enumerate(drafts, start=1) if d.game and d.game.start_time <= now]
    try:
        with session_scope() as session:
            ids = services.event_ids_for(
                session, [d.game.espn_event_id for d in drafts if d.game])
            data = _build(drafts, ids, source)
            return services.slip_warnings(data, services.find_duplicates(session, data.legs)) + \
                started
    except (ValueError, ValidationError):
        return started


def render(source: EntrySource) -> None:
    _init()
    with session_scope() as session:
        tags = {t.id: f"{t.category}: {t.name}" for t in services.tag_choices(session)}
        books = {b.id: b.name for b in services.sportsbooks(session)}
    uids = list(st.session_state["slip_legs"])
    for n, uid in enumerate(uids, start=1):
        _render_leg(uid, n, len(uids) > 1, tags)
    st.button("Add another leg", on_click=_add_leg)
    drafts = [_read_leg(uid) for uid in uids]
    _render_slip(drafts, books)
    for warning in _live_warnings(drafts, source):
        st.warning(warning)
    for error in st.session_state.get("slip_errors", []):
        st.error(error)
    st.button("Save", type="primary", on_click=_save, args=(source,), use_container_width=True)
