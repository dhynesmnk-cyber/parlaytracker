"""Loading slips transcribed into a CSV, for `cli import-slips` and `cli check-import`.

One row per leg. The plan step matches each slip to its ESPN game and each player to a roster
with the same resolver the Screenshot page uses, and cross-checks the printed wording against
the market it was given, so a real slip is a test of that resolver. Nothing here writes: `apply`
does, through `services`, and only for slips with no errors (and no doubts unless allowed).

A slip is remembered by its sportsbook slip id, in `notes`, so importing twice adds nothing.
"""
import csv
import io
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from parlaytracker.core import services
from parlaytracker.core.models import (
    EventStatus,
    Leg,
    LegResult,
    MarketType,
    Slip,
    SlipStatus,
    SlipType,
    Sport,
    Sportsbook,
)
from parlaytracker.core.odds import decimal_odds, payout as expected_payout
from parlaytracker.core.schemas import LegIn, SlipIn
from parlaytracker.ingest import espn, resolve
from parlaytracker.ingest.http import FetchError, RateLimited

COLUMNS = ("slip_id", "bet_type", "leg_count", "odds_american", "boost_pct", "status", "wager",
           "paid", "matchup", "game_start_iso", "placed_at_iso", "bookmaker", "leg_seq",
           "player", "market", "line", "result", "raw_text")

# The market names in the CSV. `touchdowns` covers "Anytime TD" (0.5) and "2+ TDs" (1.5).
CSV_MARKETS: dict[str, MarketType] = {
    "receptions": MarketType.PLAYER_RECEPTIONS,
    "receiving_yards": MarketType.PLAYER_RECEIVING_YARDS,
    "rushing_yards": MarketType.PLAYER_RUSHING_YARDS,
    "passing_yards": MarketType.PLAYER_PASSING_YARDS,
    "pass_completions": MarketType.PLAYER_PASS_COMPLETIONS,
    "touchdowns": MarketType.PLAYER_TOUCHDOWNS,
    "interceptions": MarketType.PLAYER_INTERCEPTIONS,
    "field_goals_made": MarketType.PLAYER_FIELD_GOALS,
}
RESULTS = {"won": LegResult.WIN, "lost": LegResult.LOSS, "push": LegResult.PUSH,
           "void": LegResult.VOID}
SLIP_STATUS = {"won": SlipStatus.WIN, "lost": SlipStatus.LOSS, "push": SlipStatus.PUSH,
               "void": SlipStatus.VOID}
# What The Odds API calls the books this importer may have to add.
BOOK_KEYS = {"hard rock": "hardrockbet"}

MAX_START_DIFFERENCE = 30 * 60  # seconds between the slip's game time and ESPN's
PAYOUT_TOLERANCE = Decimal("1.00")  # the printed odds are rounded, so a payout is a few cents off
NOTE_PREFIX = "import "


class CsvError(ValueError):
    """The file is not the shape this importer reads (nothing has been changed)."""


@dataclass(frozen=True)
class CsvLeg:
    seq: int
    player: str
    market: str
    line: Decimal
    result: str
    raw_text: str


@dataclass(frozen=True)
class CsvSlip:
    slip_id: str
    bet_type: str
    odds: int
    boost_pct: Decimal | None
    status: str
    wager: Decimal
    paid: Decimal | None
    matchup: str
    game_start: datetime
    placed_at: datetime
    bookmaker: str
    legs: list[CsvLeg]

    @property
    def note(self) -> str:
        return f"{NOTE_PREFIX}{self.bookmaker} #{self.slip_id}"


def _decimal(text: str, what: str) -> Decimal | None:
    text = text.strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation as e:
        raise CsvError(f"{what}: {text!r} is not a number") from e


def _moment(text: str, what: str) -> datetime:
    try:
        moment = datetime.fromisoformat(text.strip())
    except ValueError as e:
        raise CsvError(f"{what}: {text!r} is not an ISO date and time") from e
    if moment.tzinfo is None:
        raise CsvError(f"{what}: {text!r} has no time zone offset")
    return moment


def parse_csv(text: str) -> list[CsvSlip]:
    """The slips in a CSV, legs in order. Raises CsvError for anything malformed: a file that
    can't be read is better rejected whole than half-imported."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    missing = set(COLUMNS) - set(reader.fieldnames or ())
    if missing:
        raise CsvError(f"missing column(s): {', '.join(sorted(missing))}")
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in reader:
        grouped[row["slip_id"].strip()].append(row)
    slips = []
    for slip_id, rows in grouped.items():
        first = rows[0]
        for row in rows:
            for column in ("bet_type", "odds_american", "wager", "matchup", "game_start_iso",
                           "placed_at_iso", "bookmaker", "status"):
                if row[column].strip() != first[column].strip():
                    raise CsvError(f"slip {slip_id}: {column} differs between its rows")
        if int(first["leg_count"]) != len(rows):
            raise CsvError(f"slip {slip_id}: leg_count {first['leg_count']} but {len(rows)} rows")
        try:
            odds = int(first["odds_american"])
        except ValueError as e:
            raise CsvError(f"slip {slip_id}: odds {first['odds_american']!r}") from e
        wager = _decimal(first["wager"], f"slip {slip_id} wager")
        if wager is None:
            raise CsvError(f"slip {slip_id}: no wager")
        legs = []
        for row in sorted(rows, key=lambda r: int(r["leg_seq"])):
            line = _decimal(row["line"], f"slip {slip_id} line")
            if line is None:
                raise CsvError(f"slip {slip_id}: leg {row['leg_seq']} has no line")
            legs.append(CsvLeg(int(row["leg_seq"]), row["player"].strip(),
                               row["market"].strip(), line, row["result"].strip(),
                               row["raw_text"].strip()))
        slips.append(CsvSlip(
            slip_id=slip_id, bet_type=first["bet_type"].strip(), odds=odds,
            boost_pct=_decimal(first["boost_pct"], f"slip {slip_id} boost"),
            status=first["status"].strip(), wager=wager,
            paid=_decimal(first["paid"], f"slip {slip_id} paid"),
            matchup=first["matchup"].strip(),
            game_start=_moment(first["game_start_iso"], f"slip {slip_id} game_start_iso"),
            placed_at=_moment(first["placed_at_iso"], f"slip {slip_id} placed_at_iso"),
            bookmaker=first["bookmaker"].strip(), legs=legs))
    return slips


# --- Planning ----------------------------------------------------------------------------------


@dataclass
class PlannedLeg:
    csv: CsvLeg
    market: MarketType | None = None
    athlete_id: str | None = None
    player_name: str | None = None


@dataclass
class PlannedSlip:
    csv: CsvSlip
    game: espn.Game | None = None
    legs: list[PlannedLeg] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)   # the slip cannot be imported
    doubts: list[str] = field(default_factory=list)   # it can, if a person says so
    notes: list[str] = field(default_factory=list)    # for information
    already_imported: bool = False

    @property
    def status(self) -> str:
        if self.already_imported:
            return "already imported"
        return "error" if self.errors else "doubtful" if self.doubts else "ok"


def _wording(raw_text: str) -> str:
    """The market part of "PATRICK MAHOMES - PASSING YARDS"."""
    return raw_text.split(" - ", 1)[1] if " - " in raw_text else raw_text


def _check_wording(leg: PlannedLeg) -> str | None:
    """What the resolver makes of the printed wording, if that isn't the market given."""
    csv_leg = leg.csv
    wording = _wording(csv_leg.raw_text)
    resolved = resolve.resolve_market(wording, Sport.NFL, has_player=True)
    if resolved.market is not leg.market:
        return (f"leg {csv_leg.seq}: \"{wording}\" reads as {resolved.market.value}, "
                f"not {leg.market.value if leg.market else csv_leg.market}")
    implied = resolve.implied_line(wording, None)
    if implied is not None and implied != csv_leg.line:
        return f"leg {csv_leg.seq}: \"{wording}\" means Over {implied}, not {csv_leg.line}"
    return None


def plan_slip(
    slip: CsvSlip, *, games_for: Callable[[Sport, date], list[espn.Game]],
    roster_for: Callable[[Sport, str], list[espn.RosterPlayer]],
) -> PlannedSlip:
    plan = PlannedSlip(slip)
    if slip.bet_type not in ("sgp_parlay", "single"):
        plan.errors.append(f"bet type {slip.bet_type!r} is not supported by this importer")
    if slip.bet_type == "single" and len(slip.legs) != 1:
        plan.errors.append("a single has exactly one leg")
    if slip.bet_type == "sgp_parlay" and len(slip.legs) < 2:
        plan.errors.append("a same-game parlay has at least two legs")
    if not (slip.odds >= 100 or slip.odds <= -100):
        plan.errors.append(f"odds {slip.odds} are not valid American odds")
    if slip.status not in SLIP_STATUS:
        plan.errors.append(f"slip status {slip.status!r} is not won, lost, push or void")
    if [leg.seq for leg in slip.legs] != list(range(1, len(slip.legs) + 1)):
        plan.errors.append("leg numbers are not 1..n")

    results = [RESULTS.get(leg.result) for leg in slip.legs]
    for leg in slip.legs:
        if leg.result not in RESULTS:
            plan.errors.append(f"leg {leg.seq}: result {leg.result!r} is not won, lost, push "
                               "or void")
    if None not in results and slip.status in SLIP_STATUS:
        losing = LegResult.LOSS in results
        winning = all(r is LegResult.WIN for r in results)
        if (slip.status == "lost") != losing or (slip.status == "won") != winning:
            plan.errors.append("the slip's status disagrees with its legs' results")

    if slip.status == "won":
        if slip.paid is None:
            plan.errors.append("a won slip has no paid amount")
        elif slip.odds >= 100 or slip.odds <= -100:
            computed = expected_payout(slip.wager, decimal_odds(slip.odds))
            if abs(computed - slip.paid) > PAYOUT_TOLERANCE:
                plan.doubts.append(
                    f"paid {slip.paid} but the odds {slip.odds:+d} on {slip.wager} give "
                    f"{computed} (a boost applied after the odds were shown?)")
            elif computed != slip.paid:
                plan.notes.append(f"paid {slip.paid}; the odds give {computed} (rounded odds)")
    if slip.boost_pct is not None:
        plan.notes.append(f"{slip.boost_pct}% boost: odds {slip.odds:+d} taken as the boosted odds")
    if slip.placed_at >= slip.game_start:
        plan.doubts.append("placed at or after kickoff")

    # --- the game ---
    day = espn.game_day(slip.game_start)
    try:
        games = games_for(Sport.NFL, day)
    except (FetchError, RateLimited) as e:
        plan.errors.append(f"couldn't get {day}'s games from ESPN: {e}")
        games = []
    if games:
        found = resolve.match_game(slip.matchup, None, games)
        plan.game = found.game
        if found.game is None:
            plan.errors.append(f"no game on {day} matches \"{slip.matchup}\"")
        else:
            if found.doubtful:
                plan.doubts.append(f"\"{slip.matchup}\" fits more than one game on {day}")
            gap = abs((found.game.start_time - slip.game_start).total_seconds())
            if gap > MAX_START_DIFFERENCE:
                plan.errors.append(
                    f"ESPN has {found.game.label} at {found.game.start_time:%H:%M}Z, the slip "
                    f"says {slip.game_start.astimezone(found.game.start_time.tzinfo):%H:%M}Z")
    elif not plan.errors:
        plan.errors.append(f"ESPN has no games on {day}")

    # --- the legs ---
    roster: list[espn.RosterPlayer] = []
    if plan.game is not None:
        for team in (plan.game.away, plan.game.home):
            try:
                roster += roster_for(Sport.NFL, team.espn_id)
            except (FetchError, RateLimited) as e:
                plan.errors.append(f"couldn't get the {team.name} roster: {e}")
    for csv_leg in slip.legs:
        leg = PlannedLeg(csv_leg, CSV_MARKETS.get(csv_leg.market))
        plan.legs.append(leg)
        if leg.market is None:
            plan.errors.append(f"leg {csv_leg.seq}: market {csv_leg.market!r} is unknown")
            continue
        if (csv_leg.line * 2) % 1 != 0 or csv_leg.line <= 0:
            plan.errors.append(f"leg {csv_leg.seq}: line {csv_leg.line} is not a positive "
                               "whole or half number")
        if wording := _check_wording(leg):
            plan.doubts.append(wording)
        if roster:
            match = resolve.match_roster_player(csv_leg.player, roster)
            if match.athlete_id is None:
                plan.errors.append(f"leg {csv_leg.seq}: no player like \"{csv_leg.player}\" on "
                                   f"either roster")
            else:
                leg.athlete_id, leg.player_name = match.athlete_id, match.name
                if match.doubtful:
                    plan.doubts.append(f"leg {csv_leg.seq}: \"{csv_leg.player}\" matched "
                                       f"{match.name} ({match.score:.0f})")
    return plan


def _once(fetch: Callable) -> Callable:
    """Ask ESPN once per distinct question, even when 25 slips share a day: a failure is
    remembered too, so a down provider isn't asked again for every slip."""
    seen: dict[tuple, object] = {}

    def cached(*args):
        if args not in seen:
            try:
                seen[args] = fetch(*args)
            except (FetchError, RateLimited) as e:
                seen[args] = e
        result = seen[args]
        if isinstance(result, Exception):
            raise result
        return result

    return cached


def plan_import(session: Session, slips: list[CsvSlip], *, games_for, roster_for
                ) -> list[PlannedSlip]:
    done = set(session.scalars(select(Slip.notes).where(Slip.notes.like(f"{NOTE_PREFIX}%"))))
    games_for, roster_for = _once(games_for), _once(roster_for)  # one request per day / team
    plans = []
    for slip in slips:
        plan = plan_slip(slip, games_for=games_for, roster_for=roster_for)
        plan.already_imported = slip.note in done
        if _book_id(session, slip.bookmaker) is None:
            plan.notes.append(f"the sportsbook \"{slip.bookmaker}\" will be added")
        plans.append(plan)
    return plans


# --- Applying ----------------------------------------------------------------------------------


def _book_id(session: Session, name: str) -> int | None:
    books = {b.id: b.name for b in session.scalars(select(Sportsbook))}
    return resolve.resolve_sportsbook(name, books)


def _ensure_book(session: Session, name: str) -> int:
    if (found := _book_id(session, name)) is not None:
        return found
    return services.create_sportsbook(session, name, BOOK_KEYS.get(name.lower())).id


@dataclass
class ApplyReport:
    imported: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)  # slip id -> why


def apply_plans(session: Session, plans: list[PlannedSlip], logged_by: str, *,
                include_doubtful: bool = False) -> ApplyReport:
    """Write the slips that may be written. The caller commits. Every slip goes in through
    `services.create_slip`, in its own savepoint, so one failure leaves the rest intact."""
    report = ApplyReport()
    for plan in plans:
        slip = plan.csv
        if plan.status == "already imported":
            report.skipped[slip.slip_id] = "already imported"
            continue
        if plan.errors or (plan.doubts and not include_doubtful):
            report.skipped[slip.slip_id] = plan.status
            continue
        game = plan.game
        assert game is not None  # a slip with a missing game has an error
        try:
            with session.begin_nested():
                event = services.upsert_event(
                    session, sport=game.sport, espn_event_id=game.espn_event_id,
                    start_time=game.start_time, home_team=game.home.name,
                    away_team=game.away.name, home_espn_team_id=game.home.espn_id,
                    away_espn_team_id=game.away.espn_id,
                    # Not the scoreboard's status: a game that is over is found final by the
                    # worker, which dates it so the ten-minute gate holds (section 7.1).
                    status=EventStatus.SCHEDULED)
                single = slip.bet_type == "single"
                data = SlipIn(
                    is_placed=True, slip_type=SlipType.SINGLE if single else SlipType.SGP,
                    sportsbook_id=_ensure_book(session, slip.bookmaker),
                    american_odds=slip.odds, boosted=slip.boost_pct is not None,
                    stake=slip.wager, potential_payout=slip.paid, source="screenshot",
                    notes=slip.note,
                    legs=[LegIn(event_id=event.id, market_type=leg.market,
                                espn_athlete_id=leg.athlete_id, player_name=leg.player_name,
                                line=leg.csv.line, american_odds=slip.odds if single else None)
                          for leg in plan.legs])
                services.create_slip(session, data, logged_by, placed_at=slip.placed_at)
        except (ValueError, ValidationError) as e:  # ServiceError is a ValueError
            report.skipped[slip.slip_id] = f"rejected: {str(e).splitlines()[0]}"
            continue
        report.imported.append(slip.slip_id)
    return report


# --- Checking what the worker settled against what the slips say --------------------------------


@dataclass
class Mismatch:
    slip_id: str
    what: str


@dataclass
class CheckReport:
    slips: int = 0
    legs_agree: int = 0
    legs_pending: int = 0
    legs_review: int = 0
    slips_agree: int = 0
    not_imported: list[str] = field(default_factory=list)
    mismatches: list[Mismatch] = field(default_factory=list)
    payout_notes: list[str] = field(default_factory=list)


def check_import(session: Session, slips: list[CsvSlip]) -> CheckReport:
    """After `backfill`: does each settled leg and slip match the sportsbook's result?"""
    report = CheckReport()
    for csv_slip in slips:
        row = session.scalars(select(Slip).where(Slip.notes == csv_slip.note).options(
            selectinload(Slip.legs).selectinload(Leg.event))).one_or_none()
        if row is None:
            report.not_imported.append(csv_slip.slip_id)
            continue
        report.slips += 1
        for csv_leg in csv_slip.legs:
            market = CSV_MARKETS[csv_leg.market]
            leg = next((leg for leg in row.legs
                        if leg.market_type is market and leg.line == csv_leg.line
                        and resolve.same_player(csv_leg.player, leg.player_name or "")), None)
            if leg is None:
                report.mismatches.append(Mismatch(
                    csv_slip.slip_id, f"leg {csv_leg.seq} ({csv_leg.player}) is not stored"))
            elif leg.needs_review:
                report.legs_review += 1
                report.mismatches.append(Mismatch(
                    csv_slip.slip_id, f"leg {csv_leg.seq} {csv_leg.player} "
                    f"{csv_leg.market} {csv_leg.line}: in Review ({leg.review_reason})"))
            elif leg.result is LegResult.PENDING:
                report.legs_pending += 1
            elif leg.result is RESULTS[csv_leg.result]:
                report.legs_agree += 1
            else:
                report.mismatches.append(Mismatch(
                    csv_slip.slip_id,
                    f"leg {csv_leg.seq} {csv_leg.player} {csv_leg.market} over {csv_leg.line}: "
                    f"the slip says {csv_leg.result}, we settled {leg.result.value} at "
                    f"{leg.final_value.normalize():f}"))
        if row.status is SLIP_STATUS.get(csv_slip.status):
            report.slips_agree += 1
            if row.status is SlipStatus.WIN and csv_slip.paid is not None \
                    and row.payout is not None and row.payout != csv_slip.paid:
                report.payout_notes.append(
                    f"{csv_slip.slip_id}: paid {csv_slip.paid}, computed {row.payout}")
        elif row.status is not SlipStatus.PENDING:
            report.mismatches.append(Mismatch(
                csv_slip.slip_id, f"the slip says {csv_slip.status}, we have {row.status.value}"))
    return report


def summarize(plans: list[PlannedSlip]) -> str:
    """A plain-text dry-run report."""
    lines = []
    for plan in plans:
        s = plan.csv
        head = (f"{plan.status.upper():9} {s.slip_id} {s.matchup}, {len(s.legs)} legs "
                f"{s.odds:+d} ${s.wager}")
        lines.append(head)
        for kind, items in (("error", plan.errors), ("doubt", plan.doubts),
                            ("note", plan.notes)):
            lines += [f"          {kind}: {item}" for item in items]
    counts = defaultdict(int)
    for plan in plans:
        counts[plan.status] += 1
    lines.append("")
    lines.append(", ".join(f"{n} {status}" for status, n in sorted(counts.items())))
    return "\n".join(lines)

