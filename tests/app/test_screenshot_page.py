"""The Screenshot page through streamlit.testing: a fake reader, the real form, database and
resolver, and recorded ESPN data. The Qwen replies are synthetic (tests/fixtures/qwen)."""
import io
import json
from datetime import date
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import select
from streamlit.testing.v1 import AppTest

from parlaytracker.app.pages import screenshot
from parlaytracker.core.db import session_scope
from parlaytracker.core.models import (
    EntrySource,
    MarketType,
    Slip,
    SlipType,
    Sport,
    Sportsbook,
)
from parlaytracker.ingest import espn
from parlaytracker.ingest.extraction import CANT_READ, ExtractionError, parse_reply

pytestmark = pytest.mark.db

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SUNDAY = date(2026, 9, 27)
PNG = io.BytesIO()
Image.new("RGB", (300, 400), "white").save(PNG, "PNG")
IMAGE = ("slip.png", PNG.getvalue(), "image/png")


class FakeExtractor:
    def __init__(self, reply: str | Exception):
        self.reply, self.calls = reply, 0

    def extract(self, image):
        self.calls += 1
        if isinstance(self.reply, Exception):
            raise self.reply
        return parse_reply(self.reply)


def qwen(name: str) -> str:
    return (FIXTURES / "qwen" / name).read_text()


def _page():
    from parlaytracker.app.pages import screenshot

    screenshot.render()


def board(name: str):
    return espn.parse_scoreboard(
        Sport.NFL, json.loads((FIXTURES / "espn" / name).read_text()))


@pytest.fixture
def env(app_env):
    """The app's environment, with the Sunday and Monday NFL boards and Arizona's roster."""
    boards = {SUNDAY: board("nfl_scoreboard_2026-09-27_final.json"),
              date(2026, 9, 28): board("nfl_scoreboard_2026-09-28_scheduled.json")}
    roster = espn.parse_roster(json.loads((FIXTURES / "espn" / "nfl_roster_22.json").read_text()))
    app_env.setattr(espn, "fetch_scoreboard",
                    lambda sport, day, max_wait=0.0: boards.get(day, espn.ScoreboardResult([], {})))
    app_env.setattr(espn, "fetch_roster", lambda sport, team_id, max_wait=0.0: roster)
    return app_env


def open_page(env, reply: str | Exception | None) -> tuple[AppTest, FakeExtractor | None]:
    fake = None if reply is None else FakeExtractor(reply)
    env.setattr(screenshot, "make_extractor", lambda: fake)
    at = AppTest.from_function(_page, default_timeout=30)
    at.session_state["login"] = "alice@example.com"
    at.session_state["last_day"] = SUNDAY
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at, fake


def upload(at: AppTest, file=IMAGE) -> AppTest:
    at.file_uploader[0].set_value(file).run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def uid(at: AppTest, n: int = 0) -> int:
    return at.session_state["slip_legs"][n]


def book_id(name: str) -> int:
    with session_scope() as session:
        return session.scalars(select(Sportsbook).where(Sportsbook.name == name)).one().id


def slips() -> list[Slip]:
    with session_scope() as session:
        rows = session.scalars(select(Slip).order_by(Slip.id)).all()
        for s in rows:
            s.legs  # noqa: B018  (load before the session closes)
        return rows


def labels(at: AppTest) -> list[str]:
    widgets = (list(at.selectbox) + list(at.number_input) + list(at.radio)
               + list(at.text_input) + list(at.toggle))
    return [w.label for w in widgets]


# --- No key ---------------------------------------------------------------------------------


def test_without_a_key_the_page_says_so_and_still_offers_the_form(env):
    at, _ = open_page(env, None)
    assert any("QWEN_API_KEY" in i.value for i in at.info)
    assert not at.file_uploader
    assert any(b.label == "Save" for b in at.button)  # the manual form is there


# --- A good read ----------------------------------------------------------------------------


def test_a_clean_single_prefills_the_form_and_saves_nothing(env):
    at, fake = open_page(env, qwen("single_valid.json"))
    assert not at.success  # nothing uploaded yet
    upload(at)
    assert fake.calls == 1
    u = uid(at)
    assert at.selectbox(key=f"leg{u}_market").value is MarketType.PLAYER_RECEIVING_YARDS
    assert at.selectbox(key=f"leg{u}_player").value == "4361307"
    assert at.selectbox(key=f"leg{u}_game").value == "401872958"
    assert at.number_input(key=f"leg{u}_line").value == 70.5
    assert at.number_input(key=f"leg{u}_odds").value == -115
    assert at.selectbox(key="slip_book").value == book_id("DraftKings")
    assert at.toggle(key="slip_placed").value is True
    assert at.number_input(key="slip_stake").value == 10.0
    assert any("Read the slip" in s.value for s in at.success)
    assert not any("⚠" in label for label in labels(at))  # nothing to check
    assert slips() == []  # nothing is saved until Save


def test_saving_goes_through_the_normal_path_tagged_as_a_screenshot(env):
    at, _ = open_page(env, qwen("single_valid.json"))
    upload(at)
    next(b for b in at.button if b.label == "Save").click().run()
    assert not at.exception and not at.error
    (slip,) = slips()
    assert slip.source is EntrySource.SCREENSHOT and slip.slip_type is SlipType.SINGLE
    assert (slip.american_odds, slip.stake, slip.logged_by) == (-115, 10, "alice@example.com")
    (leg,) = slip.legs
    assert (leg.market_type, leg.espn_athlete_id, leg.line) == (
        MarketType.PLAYER_RECEIVING_YARDS, "4361307", 70.5)
    assert any(s.value.startswith("Saved:") for s in at.success)


def test_after_saving_the_uploader_and_form_start_fresh(env):
    at, fake = open_page(env, qwen("single_valid.json"))
    upload(at)
    next(b for b in at.button if b.label == "Save").click().run()
    assert at.file_uploader[0].value is None  # a new, empty uploader
    assert not any("Read the slip" in s.value for s in at.success)
    assert at.session_state["slip_legs"] and at.number_input(key=f"leg{uid(at)}_line").value is None
    assert fake.calls == 1


def test_a_value_changed_after_the_read_is_what_gets_saved(env):
    at, _ = open_page(env, qwen("single_valid.json"))
    upload(at)
    at.number_input(key=f"leg{uid(at)}_line").set_value(72.5).run()
    at.number_input(key=f"leg{uid(at)}_odds").set_value(-120).run()
    next(b for b in at.button if b.label == "Save").click().run()
    (leg,) = slips()[0].legs
    assert (leg.line, leg.american_odds) == (72.5, -120)


def test_a_same_game_parlay_is_prefilled_as_one(env):
    at, _ = open_page(env, qwen("sgp_valid.json"))
    upload(at)
    assert len(at.session_state["slip_legs"]) == 3
    assert at.toggle(key="slip_sgp").value is True
    assert at.number_input(key="slip_odds").value == 645
    assert at.selectbox(key="slip_book").value == book_id("FanDuel")
    markets = [at.selectbox(key=f"leg{uid(at, n)}_market").value for n in range(3)]
    assert markets == [MarketType.PLAYER_RECEPTIONS, MarketType.GAME_TOTAL, MarketType.ALT_SPREAD]
    next(b for b in at.button if b.label == "Save").click().run()
    assert not at.error
    (slip,) = slips()
    assert (slip.slip_type, slip.american_odds, len(slip.legs)) == (SlipType.SGP, 645, 3)
    assert slip.legs[2].side.value == "home"  # "49ers"


# --- Doubt ----------------------------------------------------------------------------------


def test_what_the_reader_was_unsure_of_is_marked(env):
    at, _ = open_page(env, qwen("partial.json"))
    at.session_state["last_day"] = date(2026, 9, 28)
    at.run()
    upload(at)
    marked = [label for label in labels(at) if "⚠" in label]
    assert any(label.startswith("Sportsbook") for label in marked)
    assert any(label.startswith("Game") for label in marked)
    assert any(label.startswith("Market") for label in marked)
    assert any("Check the" in s.value for s in at.success)


def test_an_unread_market_leaves_an_other_leg_with_the_slips_words(env):
    at, _ = open_page(env, qwen("partial.json"))
    at.session_state["last_day"] = date(2026, 9, 28)
    at.run()
    upload(at)
    assert at.selectbox(key=f"leg{uid(at)}_market").value is MarketType.OTHER
    assert at.text_input(key=f"leg{uid(at)}_desc").value == "Bears 44.5"


# --- Failure --------------------------------------------------------------------------------


@pytest.mark.parametrize("reply", [ExtractionError("nothing"), qwen("malformed.txt"),
                                   qwen("empty.json")])
def test_a_failed_read_shows_the_same_empty_form_and_the_message(env, reply):
    at, fake = open_page(env, reply)
    upload(at)
    assert fake.calls == 1
    assert [e.value for e in at.error] == [CANT_READ]
    u = uid(at)
    assert at.number_input(key=f"leg{u}_line").value is None
    assert at.number_input(key=f"leg{u}_odds").value is None
    assert slips() == []
    assert any(b.label == "Save" for b in at.button)  # entering it by hand still works


def test_a_file_that_is_not_an_image_is_refused_without_calling_the_reader(env):
    at, fake = open_page(env, qwen("single_valid.json"))
    upload(at, ("slip.png", b"this is not an image", "image/png"))
    assert fake.calls == 0
    assert any("png, jpg or webp" in e.value for e in at.error)


def test_a_reader_that_blows_up_unexpectedly_is_still_only_a_message(env):
    at, _ = open_page(env, ExtractionError("boom"))
    upload(at)
    assert [e.value for e in at.error] == [CANT_READ]


# --- Not read twice -------------------------------------------------------------------------


def test_the_same_image_is_read_once_not_on_every_rerun(env):
    at, fake = open_page(env, qwen("single_valid.json"))
    upload(at)
    at.number_input(key=f"leg{uid(at)}_line").set_value(71.5).run()
    at.toggle(key="slip_placed").set_value(False).run()
    assert fake.calls == 1
    assert at.number_input(key=f"leg{uid(at)}_line").value == 71.5  # the edit survived


def test_a_different_image_is_read_again(env):
    at, fake = open_page(env, qwen("single_valid.json"))
    upload(at)
    other = io.BytesIO()
    Image.new("RGB", (310, 410), "white").save(other, "PNG")
    upload(at, ("second.png", other.getvalue(), "image/png"))
    assert fake.calls == 2
