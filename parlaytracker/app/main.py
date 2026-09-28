"""ParlayTracker web app: `streamlit run parlaytracker/app/main.py` (SPEC.md section 9)."""
import streamlit as st

from parlaytracker.app import auth, common
from parlaytracker.app.pages import log, review, settings
from parlaytracker.core import services
from parlaytracker.core.db import session_scope

st.set_page_config(page_title="ParlayTracker", page_icon="🎯", layout="centered")
login = auth.require_login()
common.health_banner()
with session_scope() as session:
    pending = services.review_count(session)

pages = [
    st.Page(log.render, title="Log", icon="✍️", url_path="log", default=True),
    st.Page(review.render, title=f"Review ({pending})" if pending else "Review", icon="✅",
            url_path="review"),
    st.Page(settings.render, title="Settings", icon="⚙️", url_path="settings"),
]
st.navigation(pages, position="top").run()
st.caption(f"Signed in through Tailscale as {login}")
