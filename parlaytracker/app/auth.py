"""Who is using the app: the Tailscale login forwarded by Tailscale Serve (SPEC.md 9.1)."""
from collections.abc import Mapping
from dataclasses import dataclass

import streamlit as st

from parlaytracker.core.config import get_settings

HEADER = "tailscale-user-login"


@dataclass(frozen=True)
class Decision:
    login: str | None
    message: str | None = None


def decide(headers: Mapping[str, str], allowed: list[str], dev_login: str | None) -> Decision:
    """Pure decision, so it can be tested without Streamlit. Header names are case-insensitive.

    The header is only trustworthy because the app listens on localhost behind Serve.
    """
    lowered = {k.lower(): v for k, v in headers.items()}
    login = (lowered.get(HEADER) or "").strip().lower()
    if not login:
        if not dev_login:
            return Decision(None, "Open ParlayTracker through Tailscale: this request didn't "
                                  "come through Tailscale Serve, so it can't tell who you are.")
        login = dev_login.strip().lower()
    if not allowed:
        return Decision(None, "Nobody is allowed in yet: set ALLOWED_LOGINS on the laptop.")
    if login not in allowed:
        return Decision(None, f"This Tailscale account ({login}) isn't allowed to use "
                              "ParlayTracker.")
    return Decision(login)


def require_login() -> str:
    """The signed-in login, or stop the page with an explanation."""
    settings = get_settings()
    decision = decide(dict(st.context.headers), settings.allowed_logins, settings.dev_login)
    if decision.login is None:
        st.error(decision.message)
        st.stop()
    st.session_state["login"] = decision.login
    return decision.login
