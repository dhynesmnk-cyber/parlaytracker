"""Review: everything a person has to settle or fill in (SPEC.md section 9.5)."""
from collections.abc import Callable
from decimal import Decimal

import streamlit as st
from sqlalchemy.orm import Session

from parlaytracker.app import common
from parlaytracker.core import services
from parlaytracker.core.db import session_scope
from parlaytracker.core.models import Leg, LegResult, MarketType, Slip

RESULT_CHOICES = [None, LegResult.WIN, LegResult.LOSS, LegResult.PUSH, LegResult.VOID]
KIND_TITLES = {
    "leg": "Leg needs review",
    "slip": "Slip needs review",
    "result": "Result needed",
    "closing_line": "Closing line missing",
}


def _run(action: Callable[[Session], object], success: str) -> None:
    """Run one service call in its own transaction and report the outcome on the next run."""
    try:
        with session_scope() as session:
            action(session)
    except ValueError as e:  # ServiceError included
        common.flash(str(e), "error")
    else:
        common.flash(success)


def _decimal(key: str) -> Decimal | None:
    value = st.session_state.get(key)
    return None if value is None else Decimal(str(value))


def _settle(leg_id: int) -> None:
    value, result = _decimal(f"rv_value_{leg_id}"), st.session_state.get(f"rv_result_{leg_id}")
    _run(lambda s: services.settle_leg_manually(s, s.get(Leg, leg_id), result=result,
                                                final_value=value), "Leg settled.")


def _reopen(leg_id: int) -> None:
    _run(lambda s: services.reopen_leg(s, s.get(Leg, leg_id)), "Leg reopened.")


def _payout(slip_id: int) -> None:
    amount = _decimal(f"rv_payout_{slip_id}")
    if amount is None:
        common.flash("Enter the amount the sportsbook paid.", "error")
        return
    _run(lambda s: services.enter_slip_payout(s, s.get(Slip, slip_id), amount), "Payout saved.")


def _closing(leg_id: int) -> None:
    line, odds = _decimal(f"rv_cl_line_{leg_id}"), st.session_state.get(f"rv_cl_odds_{leg_id}")
    other = st.session_state.get(f"rv_cl_other_{leg_id}")
    if line is None or odds is None:
        common.flash("Enter the closing line and its odds.", "error")
        return
    _run(lambda s: services.set_closing_line(
        s, s.get(Leg, leg_id), closing_line=line, closing_odds=int(odds),
        closing_opposite_odds=None if other is None else int(other)), "Closing line saved.")


def _cash_out(slip_id: int) -> None:
    amount = _decimal(f"rv_cash_{slip_id}")
    if amount is None:
        common.flash("Enter the cash-out amount.", "error")
        return
    _run(lambda s: services.mark_cashed_out(s, s.get(Slip, slip_id), amount), "Marked cashed out.")


def _slip_block(slip: Slip) -> None:
    st.markdown(common.slip_summary(slip))
    for leg in slip.legs:
        state = "" if leg.result is LegResult.PENDING else f" — {leg.result.value}"
        st.caption(common.leg_summary(leg) + state)


def _leg_actions(leg: Leg) -> None:
    c1, c2 = st.columns(2)
    if leg.market_type is not MarketType.OTHER:
        c1.number_input("Final stat or score", key=f"rv_value_{leg.id}", value=None, step=1,
                        format="%d", help="For an alt spread, the team's winning margin "
                        "(negative if it lost).")
    c2.selectbox("Result", RESULT_CHOICES, key=f"rv_result_{leg.id}",
                 format_func=lambda r: "Work it out from the stat" if r is None
                 else r.value.capitalize())
    b1, b2 = st.columns(2)
    b1.button("Settle leg", key=f"rv_settle_{leg.id}", on_click=_settle, args=(leg.id,),
              type="primary")
    if leg.result is not LegResult.PENDING:
        b2.button("Reopen leg", key=f"rv_reopen_{leg.id}", on_click=_reopen, args=(leg.id,))


def _closing_actions(leg: Leg) -> None:
    c1, c2, c3 = st.columns(3)
    c1.number_input("Closing line", key=f"rv_cl_line_{leg.id}", value=None, step=0.5,
                    format="%.1f")
    c2.number_input("Closing odds", key=f"rv_cl_odds_{leg.id}", value=None, step=1, format="%d")
    c3.number_input("Other side's odds", key=f"rv_cl_other_{leg.id}", value=None, step=1,
                    format="%d", help="The Under (or the other team) at the same line, if shown.")
    st.button("Save closing line", key=f"rv_cl_save_{leg.id}", on_click=_closing,
              args=(leg.id,))


def render() -> None:
    st.header("Review")
    common.show_flashes()
    with session_scope() as session:
        items = services.review_queue(session)
        if not items:
            st.success("Nothing to review.")
        for n, item in enumerate(items):
            with st.container(border=True):
                st.markdown(f"**{KIND_TITLES[item.kind]}** · {item.reason}")
                _slip_block(item.slip)
                if item.kind in ("leg", "result"):
                    _leg_actions(item.leg)
                elif item.kind == "slip":
                    st.number_input("Amount the sportsbook paid ($)", value=None,
                                    key=f"rv_payout_{item.slip.id}", min_value=0.0, step=1.0,
                                    format="%.2f")
                    st.button("Save payout", key=f"rv_payout_save_{n}", on_click=_payout,
                              args=(item.slip.id,), type="primary")
                else:
                    _closing_actions(item.leg)

        slips = services.open_slips(session)
        if slips:
            st.divider()
            st.subheader("Open bets")
            st.caption("Cashed out early? Record it here; the legs still settle for analytics.")
            for slip in slips:
                with st.expander(common.slip_summary(slip)):
                    _slip_block(slip)
                    st.number_input("Cash-out amount ($)", key=f"rv_cash_{slip.id}", value=None,
                                    min_value=0.0, step=1.0, format="%.2f")
                    st.button("Mark cashed out", key=f"rv_cash_save_{slip.id}",
                              on_click=_cash_out, args=(slip.id,))
