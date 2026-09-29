"""Database models (SPEC.md section 4)."""
import enum
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint, Column, DateTime, Enum, ForeignKey, Index, MetaData, Numeric,
    String, Table, Text, UniqueConstraint, false, func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention={
        "ix": "ix_%(column_0_label)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    })


class Sport(enum.StrEnum):
    NFL = "nfl"
    NBA = "nba"
    MLB = "mlb"
    NHL = "nhl"


class MarketType(enum.StrEnum):
    GAME_TOTAL = "game_total"
    TEAM_TOTAL = "team_total"
    ALT_SPREAD = "alt_spread"
    PLAYER_RECEPTIONS = "player_receptions"
    PLAYER_RECEIVING_YARDS = "player_receiving_yards"
    PLAYER_RUSHING_YARDS = "player_rushing_yards"
    PLAYER_PASSING_YARDS = "player_passing_yards"
    PLAYER_PASS_COMPLETIONS = "player_pass_completions"
    PLAYER_TOUCHDOWNS = "player_touchdowns"  # rushing + receiving + returns, never passing
    PLAYER_INTERCEPTIONS = "player_interceptions"  # thrown by a quarterback
    PLAYER_FIELD_GOALS = "player_field_goals"  # made
    PLAYER_POINTS = "player_points"
    OTHER = "other"  # out-of-scope parlay leg (Under, moneyline): manual settle, not analysed


class TeamSide(enum.StrEnum):
    HOME = "home"
    AWAY = "away"


class SlipType(enum.StrEnum):
    SINGLE = "single"
    PARLAY = "parlay"
    SGP = "sgp"


class LegResult(enum.StrEnum):
    PENDING = "pending"
    WIN = "win"
    LOSS = "loss"
    PUSH = "push"
    VOID = "void"


class SlipStatus(enum.StrEnum):
    PENDING = "pending"
    WIN = "win"
    LOSS = "loss"
    PUSH = "push"
    VOID = "void"
    CASHED_OUT = "cashed_out"


class EventStatus(enum.StrEnum):
    SCHEDULED = "scheduled"
    IN_PROGRESS = "in_progress"
    BREAK = "break"      # halftime, end of period
    DELAYED = "delayed"  # weather or other stoppage
    FINAL = "final"
    POSTPONED = "postponed"
    CANCELLED = "cancelled"


class DataSource(enum.StrEnum):
    ESPN_WEB = "espn_web"    # site.web.api.espn.com
    ESPN_SITE = "espn_site"  # site.api.espn.com
    ESPN_CDN = "espn_cdn"    # cdn.espn.com (only if verified, section 15)
    NFLVERSE = "nflverse"
    MANUAL = "manual"


class HealthState(enum.StrEnum):
    OK = "ok"
    DEGRADED = "degraded"  # recent failures, breaker still closed
    OPEN = "open"          # breaker open until open_until


class FailureKind(enum.StrEnum):
    TRANSIENT = "transient"
    BLOCKED = "blocked"
    THROTTLED = "throttled"
    SCHEMA = "schema"
    IMPLAUSIBLE = "implausible"
    FROZEN = "frozen"


class EntrySource(enum.StrEnum):
    QUICK_ADD = "quick_add"
    SCREENSHOT = "screenshot"


class ClosingSource(enum.StrEnum):
    ODDS_API = "odds_api"
    MANUAL = "manual"


def _enum(e: type[enum.Enum], name: str) -> Enum:
    # VARCHAR + CHECK instead of a native Postgres ENUM: adding a value later is a
    # one-line constraint migration instead of ALTER TYPE.
    return Enum(e, name=name, native_enum=False, create_constraint=True, length=32,
                values_callable=lambda members: [m.value for m in members])


TZ = DateTime(timezone=True)


class Sportsbook(Base):
    __tablename__ = "sportsbooks"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50), unique=True)
    odds_api_key: Mapped[str | None] = mapped_column(String(50))  # e.g. "draftkings"


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    sport: Mapped[Sport] = mapped_column(_enum(Sport, "sport"))
    espn_event_id: Mapped[str] = mapped_column(String(20), unique=True)
    odds_api_event_id: Mapped[str | None] = mapped_column(String(64))
    home_team: Mapped[str] = mapped_column(String(60))  # ESPN display name
    away_team: Mapped[str] = mapped_column(String(60))
    home_espn_team_id: Mapped[str] = mapped_column(String(10))
    away_espn_team_id: Mapped[str] = mapped_column(String(10))
    start_time: Mapped[datetime] = mapped_column(TZ)
    status: Mapped[EventStatus] = mapped_column(
        _enum(EventStatus, "event_status"), default=EventStatus.SCHEDULED)
    home_score: Mapped[int | None]
    away_score: Mapped[int | None]
    period: Mapped[int | None]         # progress key (section 8.3); OT is period >= 5
    clock_seconds: Mapped[int | None]  # game clock remaining in the period
    final_at: Mapped[datetime | None] = mapped_column(TZ)
    last_polled_at: Mapped[datetime | None] = mapped_column(TZ)
    last_progress_at: Mapped[datetime | None] = mapped_column(TZ)  # progress key last advanced
    last_error: Mapped[str | None] = mapped_column(Text)

    legs: Mapped[list["Leg"]] = relationship(back_populates="event")


class Slip(Base):
    __tablename__ = "slips"
    id: Mapped[int] = mapped_column(primary_key=True)
    logged_by: Mapped[str] = mapped_column(String(255))  # st.user.email
    is_placed: Mapped[bool]
    slip_type: Mapped[SlipType] = mapped_column(_enum(SlipType, "slip_type"))
    sportsbook_id: Mapped[int] = mapped_column(ForeignKey("sportsbooks.id"))
    american_odds: Mapped[int]  # total odds exactly as shown on the slip
    boosted: Mapped[bool] = mapped_column(default=False)
    stake: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    potential_payout: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    status: Mapped[SlipStatus] = mapped_column(
        _enum(SlipStatus, "slip_status"), default=SlipStatus.PENDING)
    payout: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))  # total returned, incl. stake
    needs_review: Mapped[bool] = mapped_column(default=False)
    review_reason: Mapped[str | None] = mapped_column(Text)
    source: Mapped[EntrySource] = mapped_column(_enum(EntrySource, "entry_source"))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())
    settled_at: Mapped[datetime | None] = mapped_column(TZ)

    sportsbook: Mapped[Sportsbook] = relationship()
    legs: Mapped[list["Leg"]] = relationship(
        back_populates="slip", cascade="all, delete-orphan", order_by="Leg.id")

    __table_args__ = (
        CheckConstraint("american_odds >= 100 OR american_odds <= -100", name="odds_valid"),
        CheckConstraint("NOT is_placed OR stake IS NOT NULL", name="placed_has_stake"),
    )


leg_tags = Table(
    "leg_tags", Base.metadata,
    Column("leg_id", ForeignKey("legs.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True),
)


class Tag(Base):
    __tablename__ = "tags"
    id: Mapped[int] = mapped_column(primary_key=True)
    category: Mapped[str] = mapped_column(String(50))
    name: Mapped[str] = mapped_column(String(100))
    # Retired tags stay on old legs but are hidden from pickers (section 9.7).
    retired: Mapped[bool] = mapped_column(default=False, server_default=false())

    legs: Mapped[list["Leg"]] = relationship(secondary=leg_tags, back_populates="tags")

    __table_args__ = (UniqueConstraint("category", "name", name="uq_tags_category_name"),)


class Leg(Base):
    __tablename__ = "legs"
    id: Mapped[int] = mapped_column(primary_key=True)
    slip_id: Mapped[int] = mapped_column(ForeignKey("slips.id", ondelete="CASCADE"), index=True)
    event_id: Mapped[int] = mapped_column(ForeignKey("events.id"), index=True)
    market_type: Mapped[MarketType] = mapped_column(_enum(MarketType, "market_type"))
    side: Mapped[TeamSide | None] = mapped_column(_enum(TeamSide, "team_side"))
    espn_athlete_id: Mapped[str | None] = mapped_column(String(20))
    player_name: Mapped[str | None] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text)  # required for OTHER legs
    line: Mapped[Decimal | None] = mapped_column(Numeric(6, 1))  # NULL only for OTHER legs
    american_odds: Mapped[int | None]  # NULL only allowed on SGP legs
    result: Mapped[LegResult] = mapped_column(
        _enum(LegResult, "leg_result"), default=LegResult.PENDING)
    live_value: Mapped[Decimal | None] = mapped_column(Numeric(8, 1))
    live_updated_at: Mapped[datetime | None] = mapped_column(TZ)
    live_source: Mapped[DataSource | None] = mapped_column(_enum(DataSource, "live_source"))
    final_value: Mapped[Decimal | None] = mapped_column(Numeric(8, 1))
    settled_at: Mapped[datetime | None] = mapped_column(TZ)
    settlement_source: Mapped[DataSource | None] = mapped_column(
        _enum(DataSource, "settlement_source"))
    verified_at: Mapped[datetime | None] = mapped_column(TZ)  # a second source agreed
    verified_source: Mapped[DataSource | None] = mapped_column(
        _enum(DataSource, "verified_source"))
    closing_line: Mapped[Decimal | None] = mapped_column(Numeric(6, 1))
    closing_odds: Mapped[int | None]
    closing_opposite_odds: Mapped[int | None]  # the Under / other side, for no-vig CLV
    closing_source: Mapped[ClosingSource | None] = mapped_column(
        _enum(ClosingSource, "closing_source"))
    closing_captured_at: Mapped[datetime | None] = mapped_column(TZ)
    needs_review: Mapped[bool] = mapped_column(default=False)
    review_reason: Mapped[str | None] = mapped_column(Text)

    slip: Mapped[Slip] = relationship(back_populates="legs")
    event: Mapped[Event] = relationship(back_populates="legs")
    tags: Mapped[list[Tag]] = relationship(secondary=leg_tags, back_populates="legs")

    __table_args__ = (
        CheckConstraint(
            "american_odds IS NULL OR american_odds >= 100 OR american_odds <= -100",
            name="odds_valid"),
        CheckConstraint(
            "(market_type IN ('team_total', 'alt_spread')) = (side IS NOT NULL)",
            name="side_iff_team_market"),
        CheckConstraint(
            "(market_type LIKE 'player_%') = (espn_athlete_id IS NOT NULL)",
            name="athlete_iff_player_market"),
        CheckConstraint(
            "market_type = 'other' OR line IS NOT NULL", name="line_required"),
        CheckConstraint(
            "market_type <> 'other' OR description IS NOT NULL", name="other_has_description"),
        CheckConstraint(
            "(result = 'pending') = (settlement_source IS NULL)", name="settled_has_source"),
        CheckConstraint(
            "(verified_at IS NULL) = (verified_source IS NULL)", name="verified_pair"),
    )


class SourceHealth(Base):
    __tablename__ = "source_health"
    # One row per provider: "espn_web", "espn_site", "espn_cdn", "nflverse", "odds_api",
    # plus "worker" for the heartbeat. Written by the worker, read by the UI banner.
    source: Mapped[str] = mapped_column(String(30), primary_key=True)
    state: Mapped[HealthState] = mapped_column(
        _enum(HealthState, "health_state"), default=HealthState.OK)
    failure_kind: Mapped[FailureKind | None] = mapped_column(_enum(FailureKind, "failure_kind"))
    open_until: Mapped[datetime | None] = mapped_column(TZ)
    last_success_at: Mapped[datetime | None] = mapped_column(TZ)
    last_failure_at: Mapped[datetime | None] = mapped_column(TZ)
    consecutive_failures: Mapped[int] = mapped_column(default=0)
    requests_last_hour: Mapped[int] = mapped_column(default=0)
    errors_last_hour: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    quota_remaining: Mapped[int | None]  # Odds API x-requests-remaining


class RawSample(Base):
    """Raw response bodies: parse failures (last 5 per source) and recordings (section 8.3)."""
    __tablename__ = "raw_samples"
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(30))
    reason: Mapped[str] = mapped_column(String(20))  # "failure" or "recording"
    url: Mapped[str] = mapped_column(Text)
    status_code: Mapped[int | None]
    espn_event_id: Mapped[str | None] = mapped_column(String(20))
    error: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)  # truncated to 1 MB before insert
    fetched_at: Mapped[datetime] = mapped_column(TZ, server_default=func.now())

    __table_args__ = (
        CheckConstraint("reason IN ('failure', 'recording')", name="reason_valid"),
        Index("ix_raw_samples_source_fetched_at", "source", "fetched_at"),
    )
