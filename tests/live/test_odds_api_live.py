"""Talks to the real Odds API. Run with `-m live`; skipped otherwise.

Only the events list is called: it costs 0 credits (SPEC.md section 6.2), so this can run any
time to check that the client and models still match the real service.
"""
import os

import pytest

from parlaytracker.core.models import Sport
from parlaytracker.ingest.odds_api import OddsApiClient
from parlaytracker.ingest.router import Breakers

pytestmark = pytest.mark.live


@pytest.mark.skipif(not os.environ.get("ODDS_API_KEY"), reason="ODDS_API_KEY not set")
def test_the_events_list_parses_and_reports_the_quota():
    client = OddsApiClient(os.environ["ODDS_API_KEY"], Breakers(engine=None))
    events = client.events(Sport.NFL)
    assert events and all(e.home_team and e.away_team for e in events)
    assert client.quota_remaining is not None
    assert client.last_cost == 0
