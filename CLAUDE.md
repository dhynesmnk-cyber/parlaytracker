# ParlayTracker

Private, two-user sports-betting analytics (Over markets; singles, parlays, same-game parlays),
built in phases from a written spec.

**Before doing anything, read [HANDOVER.md](HANDOVER.md) (where the build stands, what's next)
and [SPEC.md](SPEC.md) (the design and the build phases).** Update HANDOVER.md before you
finish a session.

## Commands

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev,localdb]"
export TEST_DATABASE_URL="$(.venv/bin/python scripts/dev_postgres.py)"   # local Postgres, no Docker
.venv/bin/ruff check . && .venv/bin/shellcheck -x -P deploy deploy/*.sh
.venv/bin/pytest
```

In the Claude Code cloud environment, set `SSL_CERT_FILE=/root/.ccr/ca-bundle.crt` for `uv` and
for live HTTP calls through the proxy.

## Rules that are easy to break

- Slips and legs are written only through `parlaytracker/core/services.py`. Services flush;
  callers commit with `db.session_scope()`.
- `parlaytracker/core/models.py` and `schemas.py` are identical to the SPEC.md code blocks:
  change both together.
- After any new Alembic migration, read the generated file and run `tests/db/test_migrations.py`.
- Never print, log or commit API keys. `Settings` holds them as `SecretStr`.
- No test calls a real external service; use the recorded fixtures in `tests/fixtures/`.
  The Odds API fixtures cost credits, so reuse them.
- Deployment is a home laptop behind Tailscale (`deploy/README.md`), never exposed publicly.
