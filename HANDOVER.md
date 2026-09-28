# Handover

For the next agent picking up ParlayTracker. Read [SPEC.md](SPEC.md) first: it is the design and the build plan. This file says where the build stands and what to do next.

## Where things stand (2026-09-28)

| | |
|---|---|
| Code | All on `main`. Phases 0–2 were merged in [dhynesmnk-cyber/parlaytracker#4](https://github.com/dhynesmnk-cyber/parlaytracker/pull/4). Work on a branch, and open a PR into `main`: the laptop deploys whatever is merged there |
| Done | Phases 0 and 1, and the **code** for Phase 2 (SPEC.md section 13) |
| Phase 2 still open | Its exit criteria need the real laptop: install it, log real slips from both phones over Tailscale, reboot, update, and restore a backup once (below) |
| Next after that | Phase 3: closing lines |
| Tests | 1,029 passing locally on Python 3.12 + PostgreSQL 16 (unit, database and Streamlit app tests) |
| CI | Two jobs on every push: `test` (ruff + pytest against Postgres 16) and `deploy` (build the image, run the Compose stack as on the laptop, check web + worker, restore a backup). See the CI section at the end |
| Hosting | A dedicated laptop at home with Docker Compose, reachable only over Tailscale (section 12) |
| AI | Qwen through OpenRouter (section 6.3); not needed until Phase 6 |
| Network | Full access is enabled for this cloud environment, and `ODDS_API_KEY` is set as an environment variable |

## What exists

| Path | What it is |
|---|---|
| `parlaytracker/core/` | Models (0001, 0002), schemas, config, odds maths, settlement, services. `models.py` and `schemas.py` are canonical and identical to the SPEC.md code blocks |
| `parlaytracker/core/services.py` | The only write path. Phase 1: `create_slip`, `find_duplicates`, `slip_warnings`. Phase 2: `upsert_event`, `settle_leg_manually`, `reopen_leg`, `refresh_slip`, `enter_slip_payout`, `mark_cashed_out`, `set_closing_line`, `review_queue`/`review_count`, tag and sportsbook management, and read helpers (`sportsbooks`, `all_tags`, `open_slips`, `recent_slips`, `event_ids_for`) |
| `parlaytracker/ingest/http.py` | Shared httpx client (`ParlayTracker/1.0`, no cookies), `RateLimiter`, and `FetchError` with a `FailureKind` for every failure |
| `parlaytracker/ingest/espn.py` | Scoreboard and roster parsers (Pydantic-validated, per-event errors), status mapping, US Eastern game days, and host failover (`site.web.api` → `site.api`). No breakers yet: those are Phase 3–4 |
| `parlaytracker/app/` | Streamlit app: `main.py` (navigation, auth, health banner), `auth.py` (Tailscale guard), `common.py`, `components/slip_form.py`, `pages/log.py`, `review.py`, `settings.py` |
| `parlaytracker/worker/__main__.py` | Phase 2 worker: advisory lock + heartbeat every 60 s. Phase 3 adds APScheduler and the jobs |
| `Dockerfile`, `.dockerignore`, `.streamlit/config.toml` | One image for web, worker and migrations; the config hides Streamlit's developer menu |
| `deploy/` | `docker-compose.yml`, `setup.sh`, `update.sh`, `backup.sh`, `restore.sh`, `status.sh`, `compose.sh`, `lib.sh`, `env.example`, systemd units, and `README.md` (the user's step-by-step guide) |
| `tests/fixtures/espn/` | Real ESPN responses: NFL final, overtime and scheduled scoreboards; an MLB day with 2 postponements; NBA and NHL scoreboards; rosters for all four sports (NBA's is a flat list, the others are grouped); an Akamai 403 page |
| `tests/fixtures/odds_api/` | A real NFL events list and one event's odds for all 14 NFL market keys. They cost 14 credits: reuse them, don't re-fetch |

## Decisions made while building (not spelled out in SPEC.md)

1. **Services flush, never commit.** Callers wrap them in `db.session_scope()`.
2. **In Streamlit, writes happen in widget callbacks.** Each callback runs one service call in its own `session_scope()` and reports through `common.flash()`. Never call `st.rerun()` inside a `session_scope()`: the exception it raises rolls the transaction back.
3. **The slip form never writes to a widget's own session-state key once it exists.** Defaults live under `leg{uid}_{field}_default`. Saving and resetting happen in the Save callback, before the next run creates the widgets.
4. **`upsert_event` only refreshes an event while it's still `scheduled`**, so the picker's 10-minute cache can never overwrite what the worker has written.
5. **`review_queue`:**
   - "Result needed" means a pending leg 4 hours after kickoff. Phase 4 automates most of these.
   - Missing closing lines are offered for the last 7 days only, and aren't counted in the nav badge.
6. **Leg invariant:** `result` is `pending` exactly when `settlement_source` is NULL. Manual settlement sets `manual`; `reopen_leg` clears it.
7. **Secrets:** `odds_api_key` and `qwen_api_key` are `SecretStr` (use `.get_secret_value()`), and `Settings` hides input values in its errors. That was added after a config error printed most of the Odds API key during testing (see "Needs the user").
8. **Rate limits:** the web app waits up to 5 s for a slot (`common.WEB_MAX_WAIT`); the worker must pass `max_wait=0` and skip the request.
9. **Ruff rules are E, F, W, B, UP**, without import sorting, to keep `models.py`/`schemas.py` identical to the spec blocks.
10. **Dependencies:** each phase adds its own. Phase 3 needs `apscheduler<4`; later phases need `rapidfuzz`, `pandas`, `plotly`, `Pillow`, `openai` and `nflreadpy`.

## Gotchas

- **Alembic autogenerate escapes `%`** in CHECK constraints (it wrote `'player_%%'` in 0001; fixed by hand). `tests/db/test_migrations.py` compares the migrated schema with the models column by column, constraint by constraint and index by index. Run it after every new migration.
- **Streamlit's test harness** (`AppTest`) sends no headers, so app tests sign in through `DEV_LOGIN`. `AppTest.from_function` needs a function defined in a real file.
- **Phone-width navigation:** at phone width Streamlit folds the top navigation into a menu. Browser tests should open pages by URL (`/review`, `/settings`).
- **Moving test fixtures:** `engine` and `make_alembic_config` live in `tests/conftest.py` (shared by `tests/db` and `tests/app`). App tests commit for real, so their fixture truncates the tables afterwards.

## Running it

```bash
python3.12 -m venv .venv            # or: uv venv --python 3.12 .venv
.venv/bin/pip install -e ".[dev,localdb]"
export TEST_DATABASE_URL="$(.venv/bin/python scripts/dev_postgres.py)"
.venv/bin/ruff check . && .venv/bin/shellcheck -x -P deploy deploy/*.sh
.venv/bin/pytest
```

- The local Postgres lives in `.pgdata/` and is lost when the cloud container is reclaimed; re-run the script in a new session. The test suite wipes `parlaytracker_test`. For clicking around, create a separate `parlaytracker_dev` database on the same server.
- **Running the app locally:**

  ```bash
  DATABASE_URL=<dev url> DISPLAY_TZ=Europe/London ALLOWED_LOGINS=you@example.com DEV_LOGIN=you@example.com \
    .venv/bin/streamlit run parlaytracker/app/main.py
  ```

  Add `SSL_CERT_FILE=/root/.ccr/ca-bundle.crt` in this cloud environment so httpx trusts the proxy.
- **Testing the Tailscale path without Tailscale:** run a reverse proxy in front of Streamlit that adds `Tailscale-User-Login`, including on the websocket upgrade. The one used for Phase 2 is described in SPEC.md 9.1; it's 50 lines of aiohttp.
- **Docker** isn't available in this cloud environment. The CI `deploy` job is the check for the image and the Compose stack.
- **`uv`** needs `SSL_CERT_FILE=/root/.ccr/ca-bundle.crt` to reach PyPI through the proxy here.

## Checked by hand in Phase 2

- **In a real browser (Chromium, phone-sized window), through a header-adding proxy:**
  - no header → refused; an unlisted login → refused; an allowed login → signed in;
  - a real single logged for Monday Night Football (PHI @ CHI) from live ESPN data, with the player picked from the live roster;
  - the duplicate warning shown, a tag added, and the Review page loading.
- **The image's contents,** installed into a clean environment exactly as the `Dockerfile` copies them: migrations to head, web health OK, and the worker writing its heartbeat and stopping cleanly on SIGTERM. A second worker exits with "another worker already holds the lock".
- **`docker compose config`:** only `web` is published, on `127.0.0.1:8501`; the database isn't exposed; migrations gate web and worker; a missing `POSTGRES_PASSWORD` is refused.

## Next steps

**Phase 2, on the laptop (needs the user):**
1. The user follows `deploy/README.md`:
   - install Ubuntu Server;
   - set up Tailscale (MagicDNS and HTTPS certificates on);
   - run `setup.sh`, which follows `main`.

   Or they start `claude remote-control` on the laptop, and a session there does it.
2. Then check the exit criteria in SPEC.md section 13:
   - both phones log and settle slips;
   - the data survives a reboot and an automatic update;
   - a backup restores once.

   Also confirm the Tailscale header on real Serve, and whether `site.api.espn.com` answers the home connection (section 15).

**Phase 3 (can start now, in parallel):** closing lines (section 8.2):
- APScheduler worker skeleton with the circuit breaker (8.3) and `source_health` writes;
- Odds API client (use the saved fixtures);
- `capture_closing` with the credit budget and reserve.

## Needs the user

1. **Rotate the Odds API key.** A config error during testing printed about 29 of its 32 characters into this session's tool output. It was never committed or sent anywhere else, and the leak is fixed (decision 7), but a new key from the-odds-api.com is the safe move. Then update the `ODDS_API_KEY` environment variable here, and the laptop's `.env` once it exists.
2. **Prepare the laptop:** `deploy/README.md` steps 1–3.
3. **Both users' Tailscale login names** for `ALLOWED_LOGINS`. They're entered during setup and don't need to go in chat.
4. **A backup destination** for `rclone` (README step 4), then one test restore (step 5).
5. **For Phase 6 only:** an OpenRouter key as `QWEN_API_KEY`, with training-permitted providers turned off.

Never paste keys into chat.

## CI

- **Runs 1–2** (`1d789d7`, `efc7880`) passed. From run 2 the workflow names the database in the Postgres health check and uses `actions/checkout@v5` / `actions/setup-python@v6`.
- **Run 9** on `d4fdc38` (Phase 2) [passed](https://github.com/dhynesmnk-cyber/parlaytracker/actions/runs/36494175532), including the new `deploy` job:
  - the image built and the stack started in 40 s;
  - the web app answered on `127.0.0.1:8501` only, and the worker heartbeat appeared;
  - a backup (28 KB) restored a deleted row, and the app came back healthy.
- Later commits only touch this file. Check the latest run on the PR is green before building on it.
