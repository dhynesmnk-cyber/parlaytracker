"""Non-blocking form warnings (SPEC.md section 5)."""
from parlaytracker.core.schemas import SlipIn
from parlaytracker.core.services import Duplicate, slip_warnings


def parlay(odds=670, **overrides):
    legs = [
        {"event_id": 1, "market_type": "game_total", "line": "45.5", "american_odds": -110},
        {"event_id": 2, "market_type": "game_total", "line": "40.5", "american_odds": 120},
        {"event_id": 3, "market_type": "game_total", "line": "50.5", "american_odds": -120},
    ]
    data = {"is_placed": False, "slip_type": "parlay", "sportsbook_id": 1,
            "american_odds": odds, "source": "quick_add", "legs": legs}
    return SlipIn(**{**data, **overrides})


def test_matching_parlay_has_no_warnings():
    assert slip_warnings(parlay(670)) == []


def test_parlay_odds_within_two_percent_are_fine():
    assert slip_warnings(parlay(680)) == []  # 7.80 vs 7.70 is 1.3%


def test_parlay_odds_mismatch_warns():
    assert slip_warnings(parlay(800)) == ["Slip odds don't match the legs. Boosted, or a typo?"]


def test_boosted_parlay_skips_odds_check():
    assert slip_warnings(parlay(800, boosted=True)) == []


def test_payout_mismatch_warns():
    ok = parlay(is_placed=True, stake="10", potential_payout="77.00")
    assert slip_warnings(ok) == []
    bad = parlay(is_placed=True, stake="10", potential_payout="70.00")
    assert slip_warnings(bad) == ["Payout doesn't match stake × odds."]


def test_duplicates_are_listed():
    dupes = [Duplicate(2, "a@example.com", 115, 7), Duplicate(3, "b@example.com", None, 8)]
    assert slip_warnings(parlay(), dupes) == [
        "Leg 2: already logged by a@example.com at +115.",
        "Leg 3: already logged by b@example.com at no odds.",
    ]
