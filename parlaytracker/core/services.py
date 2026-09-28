"""The only functions that write slips and legs (SPEC.md sections 2.2 and 5).

Service functions flush but never commit: wrap calls in db.session_scope().
"""
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session, selectinload

from parlaytracker.core.markets import MARKET_SPORTS
from parlaytracker.core.models import (
    ClosingSource,
    DataSource,
    Event,
    EventStatus,
    Leg,
    LegResult,
    MarketType,
    Slip,
    SlipStatus,
    SlipType,
    Sport,
    Sportsbook,
    Tag,
    leg_tags,
)
from parlaytracker.core.odds import decimal_odds, implied_probability, parlay_decimal
from parlaytracker.core.schemas import LegIn, SlipIn
from parlaytracker.core.settlement import LegOutcome, settle_leg, settle_slip

# Warning thresholds (SPEC.md section 5).
PARLAY_ODDS_TOLERANCE = Decimal("0.02")
PAYOUT_TOLERANCE = Decimal("0.05")


class ServiceError(ValueError):
    """The slip is well-formed but refers to something missing or not allowed."""


def create_slip(session: Session, data: SlipIn, logged_by: str) -> Slip:
    """Insert a validated slip and its legs. Raises ServiceError for bad references."""
    if not logged_by:
        raise ServiceError("logged_by is required")
    if session.get(Sportsbook, data.sportsbook_id) is None:
        raise ServiceError(f"unknown sportsbook {data.sportsbook_id}")

    event_ids = {leg.event_id for leg in data.legs}
    events = {e.id: e for e in session.scalars(select(Event).where(Event.id.in_(event_ids)))}
    if missing := event_ids - events.keys():
        raise ServiceError(f"unknown event(s) {sorted(missing)}")

    tag_ids = {t for leg in data.legs for t in leg.tag_ids}
    tags = {t.id: t for t in session.scalars(select(Tag).where(Tag.id.in_(tag_ids)))}
    if missing := tag_ids - tags.keys():
        raise ServiceError(f"unknown tag(s) {sorted(missing)}")

    for n, leg in enumerate(data.legs, start=1):
        sport = events[leg.event_id].sport
        if sport not in MARKET_SPORTS[leg.market_type]:
            raise ServiceError(f"leg {n}: {leg.market_type} is not offered for {sport}")

    slip = Slip(
        logged_by=logged_by,
        is_placed=data.is_placed,
        slip_type=data.slip_type,
        sportsbook_id=data.sportsbook_id,
        american_odds=data.american_odds,
        boosted=data.boosted,
        stake=data.stake,
        potential_payout=data.potential_payout,
        source=data.source,
        notes=data.notes,
    )
    slip.legs = [
        Leg(
            event_id=leg.event_id,
            market_type=leg.market_type,
            side=leg.side,
            espn_athlete_id=leg.espn_athlete_id,
            player_name=leg.player_name,
            description=leg.description,
            line=leg.line,
            american_odds=leg.american_odds,
            tags=[tags[t] for t in leg.tag_ids],
        )
        for leg in data.legs
    ]
    session.add(slip)
    session.flush()
    return slip


@dataclass(frozen=True)
class Duplicate:
    leg_number: int  # 1-based position on the slip being entered
    logged_by: str
    american_odds: int | None
    slip_id: int


def find_duplicates(session: Session, legs: Sequence[LegIn]) -> list[Duplicate]:
    """Selections already in the database with the same selection_key() (section 5)."""
    found = []
    for n, leg in enumerate(legs, start=1):
        same = and_(
            Leg.event_id == leg.event_id,
            Leg.market_type == leg.market_type,
            Leg.side.is_not_distinct_from(leg.side),
            Leg.espn_athlete_id.is_not_distinct_from(leg.espn_athlete_id),
            Leg.line.is_not_distinct_from(leg.line),
            Leg.description.is_not_distinct_from(leg.description),
        )
        rows = session.execute(
            select(Slip.logged_by, Leg.american_odds, Slip.id)
            .join(Leg.slip)
            .where(same)
            .order_by(Leg.id)
        )
        found += [Duplicate(n, by, odds, slip_id) for by, odds, slip_id in rows]
    return found


def slip_warnings(data: SlipIn, duplicates: Sequence[Duplicate] = ()) -> list[str]:
    """Non-blocking warnings shown on the form (section 5)."""
    warnings = []
    slip_decimal = decimal_odds(data.american_odds)
    if data.slip_type is SlipType.PARLAY and not data.boosted:
        legs_decimal = parlay_decimal(leg.american_odds for leg in data.legs)
        if abs(legs_decimal - slip_decimal) / slip_decimal > PARLAY_ODDS_TOLERANCE:
            warnings.append("Slip odds don't match the legs. Boosted, or a typo?")
    if data.stake is not None and data.potential_payout is not None:
        if abs(data.stake * slip_decimal - data.potential_payout) > PAYOUT_TOLERANCE:
            warnings.append("Payout doesn't match stake × odds.")
    for d in duplicates:
        odds = "no odds" if d.american_odds is None else f"{d.american_odds:+d}"
        warnings.append(f"Leg {d.leg_number}: already logged by {d.logged_by} at {odds}.")
    return warnings


def _now() -> datetime:
    return datetime.now(tz=UTC)


# --- Events ----------------------------------------------------------------------------------


def upsert_event(
    session: Session,
    *,
    sport: Sport,
    espn_event_id: str,
    start_time: datetime,
    home_team: str,
    away_team: str,
    home_espn_team_id: str,
    away_espn_team_id: str,
    status: EventStatus,
) -> Event:
    """The event for a game picked from ESPN's scoreboard, created on first use.

    An existing event is only refreshed while it is still `scheduled` (a kickoff can move);
    once the worker has moved it on, the picker's cached data never overwrites it.
    """
    event = session.scalars(select(Event).where(Event.espn_event_id == espn_event_id)).one_or_none()
    if event is None:
        event = Event(sport=sport, espn_event_id=espn_event_id)
        session.add(event)
    elif event.sport is not sport:
        raise ServiceError(f"event {espn_event_id} is {event.sport}, not {sport}")
    elif event.status is not EventStatus.SCHEDULED:
        return event
    event.start_time = start_time
    event.home_team, event.away_team = home_team, away_team
    event.home_espn_team_id, event.away_espn_team_id = home_espn_team_id, away_espn_team_id
    event.status = status
    session.flush()
    return event


def event_ids_for(session: Session, espn_event_ids: Sequence[str]) -> dict[str, int]:
    """Database ids of the events already stored, keyed by ESPN event id."""
    if not espn_event_ids:
        return {}
    rows = session.execute(
        select(Event.espn_event_id, Event.id).where(Event.espn_event_id.in_(set(espn_event_ids)))
    )
    return dict(rows.tuples().all())


# --- Manual settlement (SPEC.md sections 7.2 and 9.5) ----------------------------------------


def refresh_slip(session: Session, slip: Slip, now: datetime | None = None) -> Slip:
    """Re-evaluate a slip from its legs (section 7.2). Cashed-out slips are left alone."""
    if slip.status is SlipStatus.CASHED_OUT:
        return slip
    outcome = settle_slip(
        slip.slip_type, slip.is_placed, slip.boosted, slip.stake, slip.american_odds,
        [LegOutcome(leg.result, leg.american_odds) for leg in slip.legs],
    )
    slip.status = outcome.status
    slip.payout = outcome.payout
    slip.needs_review = outcome.review_reason is not None
    slip.review_reason = outcome.review_reason
    slip.settled_at = None if outcome.status is SlipStatus.PENDING else (now or _now())
    session.flush()
    return slip


def settle_leg_manually(
    session: Session,
    leg: Leg,
    *,
    result: LegResult | None = None,
    final_value: Decimal | None = None,
    now: datetime | None = None,
) -> Slip:
    """Settle a leg by hand, from the final stat or an explicit result, then its slip.

    Giving the final value is preferred: the result is then computed (section 7.1). A void
    has no value. An `other` leg needs an explicit result.
    """
    if result is LegResult.PENDING:
        raise ServiceError("use reopen_leg to un-settle a leg")
    if result is LegResult.VOID and final_value is not None:
        raise ServiceError("a void leg has no final value")
    if final_value is not None and leg.market_type is not MarketType.OTHER:
        computed = settle_leg(leg.market_type, leg.line, final_value)
        if result is not None and result is not computed:
            raise ServiceError(f"a final value of {final_value} makes this leg a {computed}")
        result = computed
    if result is None:
        raise ServiceError("give the final value or the result")
    leg.result = result
    leg.final_value = final_value
    leg.settlement_source = DataSource.MANUAL
    leg.settled_at = now or _now()
    leg.needs_review = False
    leg.review_reason = None
    return refresh_slip(session, leg.slip, now)


def reopen_leg(session: Session, leg: Leg) -> Slip:
    """Undo a leg's settlement (to correct a mistake), and re-evaluate its slip."""
    leg.result = LegResult.PENDING
    leg.final_value = None
    leg.settlement_source = None
    leg.settled_at = None
    leg.verified_at = None
    leg.verified_source = None
    return refresh_slip(session, leg.slip)


def enter_slip_payout(session: Session, slip: Slip, payout: Decimal,
                      now: datetime | None = None) -> Slip:
    """Record what the sportsbook actually paid, e.g. for a reduced SGP (section 7.2)."""
    if not slip.is_placed or slip.stake is None:
        raise ServiceError("only a placed slip has a payout")
    if any(leg.result is LegResult.PENDING for leg in slip.legs):
        raise ServiceError("settle every leg first")
    if payout < 0:
        raise ServiceError("a payout can't be negative")
    if payout > slip.stake:
        slip.status = SlipStatus.WIN
    elif payout == slip.stake:
        slip.status = SlipStatus.PUSH
    else:
        slip.status = SlipStatus.LOSS
    slip.payout = payout
    slip.needs_review = False
    slip.review_reason = None
    slip.settled_at = now or _now()
    session.flush()
    return slip


def mark_cashed_out(session: Session, slip: Slip, amount: Decimal,
                    now: datetime | None = None) -> Slip:
    """A cash-out ends the slip at `amount`; its legs still settle for analytics."""
    if not slip.is_placed:
        raise ServiceError("only a placed slip can be cashed out")
    if slip.status is not SlipStatus.PENDING:
        raise ServiceError(f"the slip is already {slip.status}")
    if amount < 0:
        raise ServiceError("a cash-out can't be negative")
    slip.status = SlipStatus.CASHED_OUT
    slip.payout = amount
    slip.needs_review = False
    slip.review_reason = None
    slip.settled_at = now or _now()
    session.flush()
    return slip


def set_closing_line(session: Session, leg: Leg, *, closing_line: Decimal, closing_odds: int,
                     closing_opposite_odds: int | None = None,
                     source: ClosingSource = ClosingSource.MANUAL,
                     now: datetime | None = None) -> Leg:
    """Record a closing line: by hand (section 8.2, step 6) or, from the worker, `odds_api`."""
    if leg.market_type is MarketType.OTHER:
        raise ServiceError("'other' legs have no closing line")
    if (closing_line * 2) % 1 != 0:
        raise ServiceError("a line must be a whole or half number")
    for odds in (closing_odds, closing_opposite_odds):
        if odds is not None:
            try:
                implied_probability(odds)
            except ValueError as e:
                raise ServiceError(str(e)) from e
    leg.closing_line = closing_line
    leg.closing_odds = closing_odds
    leg.closing_opposite_odds = closing_opposite_odds
    leg.closing_source = source
    leg.closing_captured_at = now or _now()
    session.flush()
    return leg


# --- Review queue (SPEC.md section 9.5) ------------------------------------------------------

# Until the worker settles games (Phase 4), a pending leg this long after kickoff needs a
# person to enter its result.
AWAITING_RESULT_AFTER = timedelta(hours=4)
# Closing lines are only worth entering by hand while the game is recent.
CLOSING_LINE_WINDOW = timedelta(days=7)


@dataclass(frozen=True)
class ReviewItem:
    kind: str  # "leg", "slip", "result" or "closing_line"
    reason: str
    slip: Slip
    leg: Leg | None = None


def review_queue(session: Session, now: datetime | None = None) -> list[ReviewItem]:
    """Everything a person needs to look at, most urgent kind first."""
    now = now or _now()
    awaiting = and_(Leg.result == LegResult.PENDING,
                    Event.start_time <= now - AWAITING_RESULT_AFTER)
    no_closing = and_(
        Leg.closing_captured_at.is_(None), Leg.market_type != MarketType.OTHER,
        Event.start_time <= now, Event.start_time > now - CLOSING_LINE_WINDOW,
    )
    legs = session.scalars(
        select(Leg).join(Leg.event).where(Leg.needs_review | awaiting | no_closing)
        .options(selectinload(Leg.slip).selectinload(Slip.legs), selectinload(Leg.event))
        .order_by(Event.start_time, Leg.id)
    ).all()
    items = [ReviewItem("leg", leg.review_reason or "Needs review", leg.slip, leg)
             for leg in legs if leg.needs_review]
    slips = session.scalars(
        select(Slip).where(Slip.needs_review).options(selectinload(Slip.legs))
        .order_by(Slip.created_at)
    ).all()
    items += [ReviewItem("slip", s.review_reason or "Needs review", s) for s in slips]
    items += [
        ReviewItem("result", "The game should be over: enter the result", leg.slip, leg)
        for leg in legs
        if leg.result is LegResult.PENDING and not leg.needs_review
        and leg.event.start_time <= now - AWAITING_RESULT_AFTER
    ]
    items += [
        ReviewItem("closing_line", "No closing line was captured: enter it if you can",
                   leg.slip, leg)
        for leg in legs
        if leg.closing_captured_at is None and leg.market_type is not MarketType.OTHER
        and now - CLOSING_LINE_WINDOW < leg.event.start_time <= now
    ]
    return items


def review_count(session: Session, now: datetime | None = None) -> int:
    """Items that need action; missing closing lines are optional and not counted."""
    return sum(1 for item in review_queue(session, now) if item.kind != "closing_line")


# --- Tags and sportsbooks (SPEC.md section 9.7) ----------------------------------------------


def _clean(value: str, what: str) -> str:
    value = " ".join(value.split())
    if not value:
        raise ServiceError(f"{what} can't be empty")
    return value


def create_tag(session: Session, category: str, name: str) -> Tag:
    category, name = _clean(category, "category").lower(), _clean(name, "name")
    existing = session.scalars(
        select(Tag).where(func.lower(Tag.category) == category,
                          func.lower(Tag.name) == name.lower())
    ).one_or_none()
    if existing is not None:
        raise ServiceError(f"tag {category}: {name} already exists")
    tag = Tag(category=category, name=name)
    session.add(tag)
    session.flush()
    return tag


def rename_tag(session: Session, tag: Tag, category: str, name: str) -> Tag:
    category, name = _clean(category, "category").lower(), _clean(name, "name")
    clash = session.scalars(
        select(Tag).where(func.lower(Tag.category) == category,
                          func.lower(Tag.name) == name.lower(), Tag.id != tag.id)
    ).one_or_none()
    if clash is not None:
        raise ServiceError(f"tag {category}: {name} already exists; merge instead")
    tag.category, tag.name = category, name
    session.flush()
    return tag


def merge_tags(session: Session, source: Tag, target: Tag) -> Tag:
    """Move every leg from `source` to `target`, then delete `source`."""
    if source.id == target.id:
        raise ServiceError("can't merge a tag into itself")
    for leg in list(source.legs):
        if target not in leg.tags:
            leg.tags.append(target)
        leg.tags.remove(source)
    session.flush()
    session.delete(source)
    session.flush()
    return target


def set_tag_retired(session: Session, tag: Tag, retired: bool) -> Tag:
    tag.retired = retired
    session.flush()
    return tag


def tag_choices(session: Session) -> list[Tag]:
    """Active tags, most used first (section 9.3)."""
    uses = func.count(leg_tags.c.leg_id)
    rows = session.execute(
        select(Tag, uses).outerjoin(leg_tags, leg_tags.c.tag_id == Tag.id)
        .where(Tag.retired.is_(False)).group_by(Tag.id)
        .order_by(uses.desc(), Tag.category, Tag.name)
    ).all()
    return [tag for tag, _ in rows]


def create_sportsbook(session: Session, name: str, odds_api_key: str | None = None) -> Sportsbook:
    name = _clean(name, "name")
    same_name = select(Sportsbook).where(func.lower(Sportsbook.name) == name.lower())
    if session.scalars(same_name).first():
        raise ServiceError(f"sportsbook {name} already exists")
    book = Sportsbook(name=name, odds_api_key=(odds_api_key or "").strip() or None)
    session.add(book)
    session.flush()
    return book


def update_sportsbook(session: Session, book: Sportsbook, name: str,
                      odds_api_key: str | None) -> Sportsbook:
    name = _clean(name, "name")
    clash = session.scalars(
        select(Sportsbook).where(func.lower(Sportsbook.name) == name.lower(),
                                 Sportsbook.id != book.id)
    ).first()
    if clash is not None:
        raise ServiceError(f"sportsbook {name} already exists")
    book.name = name
    book.odds_api_key = (odds_api_key or "").strip() or None
    session.flush()
    return book


def recent_slips(session: Session, limit: int = 10) -> list[Slip]:
    return list(session.scalars(
        select(Slip).options(selectinload(Slip.legs).selectinload(Leg.event),
                             selectinload(Slip.sportsbook))
        .order_by(Slip.created_at.desc(), Slip.id.desc()).limit(limit)
    ))


def sportsbooks(session: Session) -> list[Sportsbook]:
    return list(session.scalars(select(Sportsbook).order_by(Sportsbook.name)))


def all_tags(session: Session) -> list[Tag]:
    """Every tag, retired ones included, for the Settings page."""
    return list(session.scalars(select(Tag).order_by(Tag.retired, Tag.category, Tag.name)))


def open_slips(session: Session) -> list[Slip]:
    """Placed slips still pending: the ones that can be cashed out."""
    return list(session.scalars(
        select(Slip).where(Slip.is_placed, Slip.status == SlipStatus.PENDING)
        .options(selectinload(Slip.legs).selectinload(Leg.event), selectinload(Slip.sportsbook))
        .order_by(Slip.created_at)
    ))
