"""The nflverse loaders and ID mapping against a recorded slice (SPEC.md section 6.4)."""
from decimal import Decimal as D

import polars as pl
import pytest
from support import (
    ARI_SF,
    BRISSETT,
    JAYDEN_WILLIAMS,
    KICKOFF,
    MCBRIDE,
    SEUMALO,
    nflverse_loader,
)

from parlaytracker.core.models import FailureKind, HealthState, MarketType as M
from parlaytracker.ingest.nflverse import NflverseData, NflverseError, nfl_season
from parlaytracker.ingest.router import Breakers, ProviderOpen

SEASON = 2026
UNPLAYED = "401872963"  # PHI @ CHI: in the schedule, no scores yet


@pytest.fixture
def breakers() -> Breakers:
    return Breakers(engine=None)


@pytest.fixture
def data(breakers) -> NflverseData:
    return NflverseData(breakers, nflverse_loader())


def test_season_of_a_game():
    from datetime import UTC, datetime
    assert nfl_season(KICKOFF) == 2026
    assert nfl_season(datetime(2027, 1, 10, tzinfo=UTC)) == 2026  # a January playoff game
    assert nfl_season(datetime(2027, 2, 14, tzinfo=UTC)) == 2026


def test_final_score_by_espn_event_id(data):
    assert data.final_score(SEASON, ARI_SF) == (36, 30)  # home, away


def test_no_final_score_until_nflverse_has_one(data):
    assert data.final_score(SEASON, UNPLAYED) is None
    assert data.final_score(SEASON, "not-a-game") is None


@pytest.mark.parametrize(("market", "expected"), [
    (M.PLAYER_RECEPTIONS, D("9")),
    (M.PLAYER_RECEIVING_YARDS, D("75")),
    (M.PLAYER_RUSHING_YARDS, D("0")),
    (M.PLAYER_PASSING_YARDS, D("0")),
])
def test_player_stats_map_through_the_id_columns(data, market, expected):
    assert data.stat_value(SEASON, ARI_SF, MCBRIDE, market) == expected


def test_a_player_with_a_row_but_no_receptions_is_a_real_zero(data):
    assert data.stat_value(SEASON, ARI_SF, BRISSETT, M.PLAYER_RECEPTIONS) == 0
    assert data.stat_value(SEASON, ARI_SF, BRISSETT, M.PLAYER_PASSING_YARDS) == 280


def test_a_player_with_no_stat_row_has_no_value(data):
    assert data.stat_value(SEASON, ARI_SF, SEUMALO, M.PLAYER_RECEPTIONS) is None


def test_an_unmapped_player_is_never_matched_by_name(data):
    assert not data.player_mapped("999")
    assert data.stat_value(SEASON, ARI_SF, "999", M.PLAYER_RECEPTIONS) is None
    assert data.offense_snaps(SEASON, ARI_SF, "999") is None


def test_offense_snaps_through_the_pfr_id(data):
    assert data.offense_snaps(SEASON, ARI_SF, SEUMALO) == 87.0
    assert data.offense_snaps(SEASON, ARI_SF, JAYDEN_WILLIAMS) == 4.0
    assert data.offense_snaps(SEASON, "not-a-game", SEUMALO) is None


def test_each_dataset_loads_once_however_many_lookups(breakers):
    calls: list[str] = []
    inner = nflverse_loader()

    def counting(dataset, season):
        calls.append(dataset)
        return inner(dataset, season)

    data = NflverseData(breakers, counting)
    for _ in range(3):
        data.final_score(SEASON, ARI_SF)
        data.stat_value(SEASON, ARI_SF, MCBRIDE, M.PLAYER_RECEPTIONS)
        data.offense_snaps(SEASON, ARI_SF, SEUMALO)
    assert sorted(calls) == ["player_stats", "players", "schedules", "snap_counts"]


# --- Failures reach the breaker -------------------------------------------------------------


def test_a_download_failure_is_transient_and_recorded(breakers):
    def broken(dataset, season):
        raise ConnectionError("github is down")

    data = NflverseData(breakers, broken)
    with pytest.raises(NflverseError) as info:
        data.final_score(SEASON, ARI_SF)
    assert info.value.kind is FailureKind.TRANSIENT
    assert breakers["nflverse"].consecutive_failures == 1


def test_a_missing_column_is_a_schema_failure_that_opens_the_breaker(breakers):
    def renamed(frames):
        frames["schedules"] = frames["schedules"].rename({"espn": "espn_game"})

    data = NflverseData(breakers, nflverse_loader(renamed))
    with pytest.raises(NflverseError) as info:
        data.final_score(SEASON, ARI_SF)
    assert info.value.kind is FailureKind.SCHEMA and "espn" in str(info.value)
    assert breakers["nflverse"].state is HealthState.OPEN
    with pytest.raises(ProviderOpen):  # the open breaker stops the next attempt
        NflverseData(breakers, nflverse_loader()).final_score(SEASON, ARI_SF)


def test_warm_loads_everything_and_reports_a_healthy_provider(data, breakers):
    data.warm(SEASON)
    assert breakers["nflverse"].state is HealthState.OK
    assert breakers["nflverse"].last_success_at is not None


def test_null_and_nan_stats_count_as_missing(breakers):
    def blank(frames):
        stats = frames["player_stats"]
        frames["player_stats"] = stats.with_columns(
            pl.when(pl.col("player_id") == stats.filter(
                pl.col("player_display_name") == "Trey McBride")["player_id"][0])
            .then(None).otherwise(pl.col("receptions")).alias("receptions"))

    data = NflverseData(breakers, nflverse_loader(blank))
    assert data.stat_value(SEASON, ARI_SF, MCBRIDE, M.PLAYER_RECEPTIONS) is None
