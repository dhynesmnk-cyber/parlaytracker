"""Talks to the real ESPN and nflverse. Run with `-m live`; skipped otherwise.

The same check the worker's `canary` job makes: fully parse the latest completed NFL game from
every ESPN provider, and load the nflverse datasets. Free, and a good first thing to run on the
laptop or after ESPN changes something.
"""
import pytest

from parlaytracker.ingest.nflverse import NflverseData
from parlaytracker.ingest.router import Breakers, EspnRouter
from parlaytracker.worker.settle import Canary

pytestmark = pytest.mark.live


def test_every_provider_still_parses_a_real_game():
    breakers = Breakers(engine=None)
    report = Canary(EspnRouter(breakers), lambda: NflverseData(breakers))()
    assert report, "no provider was asked: is there a completed NFL game in the last 7 days?"
    failed = {name: why for name, why in report.items() if why != "ok"}
    assert not failed, failed
