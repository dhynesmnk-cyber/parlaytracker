"""Suite-wide rules: DB tests need TEST_DATABASE_URL; live tests run only when selected."""
import os

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    has_db = bool(os.environ.get("TEST_DATABASE_URL"))
    if not has_db and os.environ.get("CI"):
        raise pytest.UsageError("TEST_DATABASE_URL must be set in CI")
    run_live = "live" in (config.getoption("markexpr") or "")
    for item in items:
        if "db" in item.keywords and not has_db:
            item.add_marker(pytest.mark.skip(reason="TEST_DATABASE_URL not set (see HANDOVER.md)"))
        if "live" in item.keywords and not run_live:
            item.add_marker(pytest.mark.skip(reason="live test: run with -m live"))
