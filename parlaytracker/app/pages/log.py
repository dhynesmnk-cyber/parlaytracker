"""Log: the quick-add form and the latest slips (SPEC.md section 9.3)."""
import streamlit as st

from parlaytracker.app import common
from parlaytracker.app.components import slip_form
from parlaytracker.core import services
from parlaytracker.core.db import session_scope
from parlaytracker.core.models import EntrySource, SlipStatus


def render() -> None:
    st.header("Log a slip")
    common.show_flashes()
    slip_form.render(EntrySource.QUICK_ADD)

    st.divider()
    st.subheader("Recently logged")
    with session_scope() as session:
        slips = services.recent_slips(session, 5)
        if not slips:
            st.caption("Nothing logged yet.")
        for slip in slips:
            status = "" if slip.status is SlipStatus.PENDING else f" — **{slip.status.value}**"
            st.markdown(f"{common.slip_summary(slip)}{status}")
            for leg in slip.legs:
                st.caption(common.leg_summary(leg))
