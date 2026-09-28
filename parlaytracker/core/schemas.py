"""Validation gate: the only way data reaches the database (SPEC.md section 5)."""
from decimal import Decimal

from pydantic import BaseModel, Field, field_validator, model_validator

from parlaytracker.core.models import EntrySource, MarketType, SlipType, TeamSide

PLAYER_MARKETS = {m for m in MarketType if m.value.startswith("player_")}
TEAM_MARKETS = {MarketType.TEAM_TOTAL, MarketType.ALT_SPREAD}


def _american(v: int | None) -> int | None:
    if v is not None and -100 < v < 100:
        raise ValueError("American odds must be <= -100 or >= +100")
    return v


class LegIn(BaseModel):
    event_id: int
    market_type: MarketType
    side: TeamSide | None = None
    espn_athlete_id: str | None = None
    player_name: str | None = None
    description: str | None = None
    line: Decimal | None = None
    american_odds: int | None = None
    tag_ids: list[int] = []

    _odds = field_validator("american_odds")(_american)

    @field_validator("line")
    @classmethod
    def _half_step(cls, v: Decimal | None) -> Decimal | None:
        if v is not None and (v * 2) % 1 != 0:
            raise ValueError("line must be a whole or half number")
        return v

    @model_validator(mode="after")
    def _market_fields(self) -> "LegIn":
        is_player = self.market_type in PLAYER_MARKETS
        is_team = self.market_type in TEAM_MARKETS
        if is_player != (self.espn_athlete_id is not None):
            raise ValueError("player markets need a player; other markets must not have one")
        if is_team != (self.side is not None):
            raise ValueError("team total / alt spread need a side; other markets must not have one")
        if self.market_type is MarketType.OTHER:
            if not self.description:
                raise ValueError("an 'other' leg needs a description")
            return self
        if self.line is None:
            raise ValueError("line is required")
        if self.market_type is not MarketType.ALT_SPREAD and self.line <= 0:
            raise ValueError("Over lines must be positive")
        return self

    def selection_key(self) -> tuple:
        return (self.event_id, self.market_type, self.side, self.espn_athlete_id, self.line,
                self.description)


class SlipIn(BaseModel):
    is_placed: bool
    slip_type: SlipType
    sportsbook_id: int
    american_odds: int
    boosted: bool = False
    stake: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    potential_payout: Decimal | None = Field(default=None, gt=0, max_digits=12, decimal_places=2)
    source: EntrySource
    notes: str | None = None
    legs: list[LegIn] = Field(min_length=1)

    _odds = field_validator("american_odds")(_american)

    @model_validator(mode="after")
    def _slip_rules(self) -> "SlipIn":
        if self.is_placed and self.stake is None:
            raise ValueError("a placed bet needs a stake")
        if self.slip_type is SlipType.SINGLE:
            if len(self.legs) != 1:
                raise ValueError("a single has exactly one leg")
            if self.legs[0].american_odds != self.american_odds:
                raise ValueError("a single's leg odds must equal the slip odds")
        elif len(self.legs) < 2:
            raise ValueError("a parlay needs at least two legs")
        missing_odds = any(leg.american_odds is None for leg in self.legs)
        if self.slip_type is SlipType.PARLAY and missing_odds:
            raise ValueError("every leg of a standard parlay needs its own odds")
        if self.slip_type is SlipType.SGP and len({leg.event_id for leg in self.legs}) != 1:
            raise ValueError("every leg of a same-game parlay must be in the same game")
        keys = [leg.selection_key() for leg in self.legs]
        if len(keys) != len(set(keys)):
            raise ValueError("the same selection appears twice on this slip")
        return self


# Loose model for the vision model's output: everything optional, never the DB gate.
class ExtractedLeg(BaseModel):
    sport: str | None = None
    event_text: str | None = None
    team_text: str | None = None
    player_name: str | None = None
    market_text: str | None = None
    line: float | None = None
    american_odds: int | None = None


class ExtractedSlip(BaseModel):
    sportsbook_text: str | None = None
    slip_type_text: str | None = None
    american_odds: int | None = None
    stake: float | None = None
    potential_payout: float | None = None
    legs: list[ExtractedLeg] = []
