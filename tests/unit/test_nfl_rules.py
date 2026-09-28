"""The NFL rules of SPEC.md 7.1 for a player ESPN's box score doesn't list."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from parlaytracker.core.settlement import (
    MissingPlayer as M,
    resolve_missing_nfl_player as resolve,
    tuesday_deadline,
)

SUNDAY = datetime(2026, 9, 27, 20, 5, tzinfo=UTC)          # 4:05 pm ET
MONDAY_NIGHT = datetime(2026, 9, 29, 0, 15, tzinfo=UTC)    # 8:15 pm ET Monday
THURSDAY_NIGHT = datetime(2026, 10, 2, 0, 15, tzinfo=UTC)  # 8:15 pm ET Thursday
EARLY = SUNDAY + timedelta(days=1)                          # Monday: before the Tuesday
LATE = SUNDAY + timedelta(days=3)                           # Wednesday: after it


@pytest.mark.parametrize(("start", "deadline"), [
    (SUNDAY, datetime(2026, 9, 30, 3, 59, 59, tzinfo=UTC)),          # end of Tue 29th, ET
    (MONDAY_NIGHT, datetime(2026, 9, 30, 3, 59, 59, tzinfo=UTC)),    # Monday -> next day
    (THURSDAY_NIGHT, datetime(2026, 10, 7, 3, 59, 59, tzinfo=UTC)),  # Thursday -> Tuesday week
])
def test_the_tuesday_following_the_game(start, deadline):
    assert tuesday_deadline(start).replace(microsecond=0) == deadline


def test_a_late_game_belongs_to_its_eastern_game_day():
    # 00:15 UTC on the 29th is still Monday the 28th in New York.
    assert tuesday_deadline(MONDAY_NIGHT) == tuesday_deadline(
        datetime(2026, 9, 28, 17, 0, tzinfo=UTC))


# --- every branch of section 7.1 ------------------------------------------------------------


def test_nflverse_has_a_stat_line_settle_from_it():
    assert resolve(D("7"), None, SUNDAY, EARLY) == (M.SETTLE_FROM_NFLVERSE, D("7"))
    # a stat line of zero is still a stat line, and outranks the snap counts
    assert resolve(D("0"), 0.0, SUNDAY, EARLY) == (M.SETTLE_FROM_NFLVERSE, D("0"))


def test_no_stat_line_but_snaps_means_he_played_and_the_value_is_zero():
    assert resolve(None, 54.0, SUNDAY, EARLY) == (M.SETTLE_ZERO_PLAYED, D("0"))
    assert resolve(None, 1.0, SUNDAY, LATE) == (M.SETTLE_ZERO_PLAYED, D("0"))


def test_no_stat_line_and_zero_snaps_goes_to_review_straight_away():
    assert resolve(None, 0.0, SUNDAY, EARLY) == (M.REVIEW_NO_SNAPS, None)


def test_no_data_at_all_waits_until_the_tuesday_then_goes_to_review():
    assert resolve(None, None, SUNDAY, EARLY) == (M.WAIT, None)
    assert resolve(None, None, SUNDAY, tuesday_deadline(SUNDAY)) == (M.WAIT, None)
    assert resolve(None, None, SUNDAY, tuesday_deadline(SUNDAY) + timedelta(seconds=1)) == (
        M.REVIEW_NO_SNAPS, None)


def test_the_result_is_never_void_and_never_zero_without_evidence():
    for value, snaps, now in [(None, None, EARLY), (None, None, LATE), (None, 0.0, LATE)]:
        action, settled = resolve(value, snaps, SUNDAY, now)
        assert settled is None and action in (M.WAIT, M.REVIEW_NO_SNAPS)
