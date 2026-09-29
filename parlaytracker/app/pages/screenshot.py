"""Screenshot: read a slip from an image, then confirm it in the ordinary form (SPEC.md 9.6).

The reader (Qwen, through OpenRouter) only pre-fills the form. It never writes to the
database: a person checks the fields marked with a warning and presses Save, and the slip goes
through `services.create_slip` like any other. Any failure shows the same form, empty.
"""
import hashlib
from dataclasses import dataclass
from datetime import date

import streamlit as st

from parlaytracker.app import common
from parlaytracker.app.components import slip_form
from parlaytracker.core import services
from parlaytracker.core.config import get_settings
from parlaytracker.core.db import session_scope
from parlaytracker.core.models import EntrySource, Sport
from parlaytracker.ingest import espn, resolve
from parlaytracker.ingest.extraction import (
    CANT_READ,
    ExtractionError,
    Extractor,
    PreparedImage,
    prepare_image,
)

NOT_SET_UP = ("Reading screenshots isn't set up on this server yet (there is no QWEN_API_KEY). "
              "You can still enter the slip below.")


def make_extractor() -> Extractor | None:
    """The reader, or None when no key is configured."""
    s = get_settings()
    return Extractor.from_settings(s.qwen_api_key, s.qwen_base_url, s.qwen_vision_model)


@dataclass(frozen=True)
class Outcome:
    """What reading one upload came to: a slip to pre-fill, or a message and an empty form."""
    slip: resolve.ResolvedSlip | None
    image: PreparedImage | None
    message: str


def process_upload(data: bytes, extractor: Extractor, *, books: dict[int, str], day: date,
                   default_sport: Sport, games_for, roster_for) -> Outcome:
    """Downscale, read, and resolve one screenshot. Never raises: every failure is a message."""
    try:
        image = prepare_image(data)
    except ExtractionError as e:
        return Outcome(None, None, str(e))
    try:
        extracted = extractor.extract(image)
    except ExtractionError:
        return Outcome(None, image, CANT_READ)
    slip = resolve.resolve_slip(extracted, books=books, day=day, default_sport=default_sport,
                                games_for=games_for, roster_for=roster_for)
    return Outcome(slip, image, "")


def _games_for(sport: Sport, day: date) -> list[espn.Game]:
    return common.scoreboard(sport.value, day)  # raises FetchError / RateLimited: handled


def _roster_for(sport: Sport, team_id: str) -> list[espn.RosterPlayer]:
    return common.roster(sport.value, team_id)


def _read(data: bytes, extractor: Extractor) -> Outcome:
    with session_scope() as session:
        books = {b.id: b.name for b in services.sportsbooks(session)}
    ss = st.session_state
    return process_upload(
        data, extractor, books=books, day=ss.get("shot_day", espn.today_game_day()),
        default_sport=ss.get("last_sport", Sport.NFL), games_for=_games_for,
        roster_for=_roster_for)


def _forget_image() -> None:
    ss = st.session_state
    ss["shot_n"] = ss.get("shot_n", 0) + 1  # a new key gives an empty uploader
    for key in ("shot_done", "shot_image", "shot_note"):
        ss.pop(key, None)


def render() -> None:
    ss = st.session_state
    st.header("Read a slip from a screenshot")
    common.show_flashes()
    if ss.pop("slip_saved", False) and ss.get("shot_done"):
        _forget_image()  # this image has been saved: start fresh

    extractor = make_extractor()
    if extractor is None:
        st.info(NOT_SET_UP)
    else:
        st.date_input("Game day on the slip (US Eastern)", key="shot_day",
                      value=ss.get("last_day", espn.today_game_day()))
        upload = st.file_uploader("Screenshot of a bet slip", type=["png", "jpg", "jpeg", "webp"],
                                  key=f"shot_upload_{ss.get('shot_n', 0)}",
                                  help="Up to 8 MB. Nothing is saved until you press Save.")
        if upload is not None:
            data = upload.getvalue()
            digest = hashlib.sha256(data).hexdigest()
            if ss.get("shot_done") != digest:  # once per image, not on every rerun
                with st.spinner("Reading the slip…"):
                    outcome = _read(data, extractor)
                if outcome.slip is not None:
                    slip_form.prefill(outcome.slip)
                else:
                    slip_form.reset()
                ss["shot_done"], ss["shot_note"] = digest, outcome.message
                ss["shot_image"] = outcome.image.data if outcome.image else None

    note = ss.get("shot_note")
    if note:
        st.error(note)
    elif ss.get("shot_done"):
        marked = slip_form.doubt_count()
        st.success(f"Read the slip. Check the {marked} field{'s' if marked != 1 else ''} marked "
                   "⚠, then Save." if marked else "Read the slip. Check it, then Save.")

    image = ss.get("shot_image")
    if image:
        side, main = st.columns([2, 3])
        side.image(image, caption="Your screenshot", use_container_width=True)
        with main:
            slip_form.render(EntrySource.SCREENSHOT)
    else:
        slip_form.render(EntrySource.SCREENSHOT)
