# ParlayTracker

Private, two-user analytics for US sports betting. It records placed bets and unplaced
opportunities on **Over** markets (singles, parlays and same-game parlays) and shows, from
the pooled history, where there is a real edge.

- **[SPEC.md](SPEC.md)** is the design and the build plan. Build its phases in order.
- **[HANDOVER.md](HANDOVER.md)** says which phases are done and what to do next.
- **[deploy/README.md](deploy/README.md)** sets the app up on the home laptop, reachable over Tailscale.

## Development

Needs Python 3.12 and PostgreSQL 15 or newer.

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev,localdb]"

# A throwaway local Postgres (no Docker needed); prints the URL to use
export TEST_DATABASE_URL="$(.venv/bin/python scripts/dev_postgres.py)"

.venv/bin/ruff check .
.venv/bin/pytest            # database tests are skipped if TEST_DATABASE_URL is unset
```

Migrations run against `DATABASE_URL` (see `.env.example`):

```bash
DATABASE_URL=... DISPLAY_TZ=Europe/London .venv/bin/alembic upgrade head
```

The test suite wipes the database in `TEST_DATABASE_URL`, and refuses to run unless the
database name contains `test`.
