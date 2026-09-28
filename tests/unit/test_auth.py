"""The Tailscale identity guard (SPEC.md section 9.1)."""
from parlaytracker.app.auth import decide

ALLOWED = ["alice@example.com", "bob@github"]


def test_allowed_login_from_the_tailscale_header():
    assert decide({"Tailscale-User-Login": "alice@example.com"}, ALLOWED, None).login == \
        "alice@example.com"


def test_header_name_and_login_are_case_insensitive():
    assert decide({"tailscale-user-login": " Bob@GitHub "}, ALLOWED, None).login == "bob@github"


def test_other_login_is_refused():
    d = decide({"Tailscale-User-Login": "mallory@example.com"}, ALLOWED, None)
    assert d.login is None and "mallory@example.com" in d.message


def test_missing_header_is_refused_without_dev_login():
    d = decide({}, ALLOWED, None)
    assert d.login is None and "through Tailscale" in d.message


def test_dev_login_only_applies_when_the_header_is_missing():
    assert decide({}, ALLOWED, "alice@example.com").login == "alice@example.com"
    assert decide({"Tailscale-User-Login": "mallory@example.com"}, ALLOWED,
                  "alice@example.com").login is None


def test_dev_login_must_still_be_allowed():
    assert decide({}, ALLOWED, "mallory@example.com").login is None


def test_empty_allow_list_lets_nobody_in():
    d = decide({"Tailscale-User-Login": "alice@example.com"}, [], None)
    assert d.login is None and "ALLOWED_LOGINS" in d.message
