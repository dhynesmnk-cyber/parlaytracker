"""The only functions that write slips and legs (SPEC.md sections 2.2 and 5).

Service functions flush but never commit: wrap calls in db.session_scope().
"""
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from parlaytracker.core.markets import MARKET_SPORTS
from parlaytracker.core.models import Event, Leg, Slip, SlipType, Sportsbook, Tag
from parlaytracker.core.odds import decimal_odds, parlay_decimal
from parlaytracker.core.schemas import LegIn, SlipIn

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
