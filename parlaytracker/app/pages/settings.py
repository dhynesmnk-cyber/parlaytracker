"""Settings: tags and sportsbooks (SPEC.md section 9.7)."""
from collections.abc import Callable

import streamlit as st
from sqlalchemy.orm import Session

from parlaytracker.app import common
from parlaytracker.core import services
from parlaytracker.core.db import session_scope
from parlaytracker.core.models import Sportsbook, Tag


def _run(action: Callable[[Session], object], success: str) -> None:
    try:
        with session_scope() as session:
            action(session)
    except ValueError as e:
        common.flash(str(e), "error")
    else:
        common.flash(success)


def _get(key: str) -> str:
    return st.session_state.get(key) or ""


def _create_tag() -> None:
    _run(lambda s: services.create_tag(s, _get("st_new_cat"), _get("st_new_name")), "Tag added.")


def _rename_tag(tag_id: int) -> None:
    _run(lambda s: services.rename_tag(s, s.get(Tag, tag_id), _get(f"st_ren_cat_{tag_id}"),
                                       _get(f"st_ren_name_{tag_id}")), "Tag renamed.")


def _merge_tag(tag_id: int) -> None:
    target = st.session_state.get(f"st_merge_into_{tag_id}")
    if target is None:
        common.flash("Choose the tag to merge into.", "error")
        return
    _run(lambda s: services.merge_tags(s, s.get(Tag, tag_id), s.get(Tag, target)), "Tags merged.")


def _retire_tag(tag_id: int, retired: bool) -> None:
    _run(lambda s: services.set_tag_retired(s, s.get(Tag, tag_id), retired),
         "Tag retired." if retired else "Tag restored.")


def _create_book() -> None:
    _run(lambda s: services.create_sportsbook(s, _get("sb_new_name"), _get("sb_new_key")),
         "Sportsbook added.")


def _update_book(book_id: int) -> None:
    _run(lambda s: services.update_sportsbook(s, s.get(Sportsbook, book_id),
                                              _get(f"sb_ed_name_{book_id}"),
                                              _get(f"sb_ed_key_{book_id}")), "Sportsbook saved.")


def _tags_section(tags: list[Tag]) -> None:
    st.subheader("Tags")
    st.caption("Tags are for context the columns don't already capture: injuries, weather, "
               "situation, where the idea came from.")
    with st.form("st_new", clear_on_submit=True, border=True):
        c1, c2 = st.columns(2)
        c1.text_input("Category", key="st_new_cat", placeholder="e.g. injury")
        c2.text_input("Name", key="st_new_name", placeholder="e.g. WR1 out")
        st.form_submit_button("Add tag", on_click=_create_tag)
    if not tags:
        return
    labels = {t.id: f"{t.category}: {t.name}" + (" (retired)" if t.retired else "") for t in tags}
    tag_id = st.selectbox("Edit a tag", list(labels), format_func=labels.get, key="st_pick")
    tag = next(t for t in tags if t.id == tag_id)
    c1, c2 = st.columns(2)
    c1.text_input("Category", value=tag.category, key=f"st_ren_cat_{tag.id}")
    c2.text_input("Name", value=tag.name, key=f"st_ren_name_{tag.id}")
    b1, b2 = st.columns(2)
    b1.button("Rename", key="st_rename", on_click=_rename_tag, args=(tag.id,))
    b2.button("Restore" if tag.retired else "Retire", key="st_retire", on_click=_retire_tag,
              args=(tag.id, not tag.retired))
    others = [t.id for t in tags if t.id != tag.id]
    if others:
        st.selectbox("Merge into", others, index=None, format_func=labels.get,
                     key=f"st_merge_into_{tag.id}", placeholder="Choose the tag to keep")
        st.button("Merge", key="st_merge", on_click=_merge_tag, args=(tag.id,))


def _books_section(books: list[Sportsbook]) -> None:
    st.subheader("Sportsbooks")
    st.caption("The Odds API key links a book to its closing lines (e.g. draftkings).")
    with st.form("sb_new", clear_on_submit=True, border=True):
        c1, c2 = st.columns(2)
        c1.text_input("Name", key="sb_new_name")
        c2.text_input("Odds API key (optional)", key="sb_new_key")
        st.form_submit_button("Add sportsbook", on_click=_create_book)
    if not books:
        return
    names = {b.id: b.name for b in books}
    book_id = st.selectbox("Edit a sportsbook", list(names), format_func=names.get, key="sb_pick")
    book = next(b for b in books if b.id == book_id)
    c1, c2 = st.columns(2)
    c1.text_input("Name", value=book.name, key=f"sb_ed_name_{book.id}")
    c2.text_input("Odds API key", value=book.odds_api_key or "", key=f"sb_ed_key_{book.id}")
    st.button("Save sportsbook", key="sb_save", on_click=_update_book, args=(book.id,))


def render() -> None:
    st.header("Settings")
    common.show_flashes()
    with session_scope() as session:
        _tags_section(services.all_tags(session))
        st.divider()
        _books_section(services.sportsbooks(session))
