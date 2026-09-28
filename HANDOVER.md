# Handover

For the next agent picking up ParlayTracker. Read [SPEC.md](SPEC.md) first: it is the design and the build plan. This file says where the build stands and what to do next.

## Where things stand (2026-09-28)

| | |
|---|---|
| Branch | `claude/vigilant-sagan-acmryz`, open as [dhynesmnk-cyber/parlaytracker#4](https://github.com/dhynesmnk-cyber/parlaytracker/pull/4). New commits on the branch update the PR |
| Done | Phase 0 (reset and tooling) and Phase 1 (core domain) of SPEC.md section 13 |
| Next | Phase 2: logging UI, then deploy (below) |
| Tests | 940 passing locally on Python 3.12 + PostgreSQL 16: 904 unit, 36 database |
| CI | `.github/workflows/ci.yml`: ruff and pytest against a Postgres 16 service, on every push and PR. See the CI section at the end |
| Hosting | **Decided:** a dedicated laptop at home with Docker Compose, reachable only over Tailscale (SPEC.md section 12). Fly.io, hosted Postgres and Google OAuth are all dropped; `fly.toml` is deleted |
| AI | **Decided:** Qwen through OpenRouter (section 6.3); DashScope is no longer used |
| Network | Full access is enabled for this cloud environment, and `ODDS_API_KEY` is set as an environment variable |

### Verified on 2026-09-28

SPEC.md section 15 lists what was checked against the real APIs, and what still needs a live game. The main surprises:
- **ESPN:** `site.api.espn.com` answers this cloud environment with Akamai's 403, while `site.web.api.espn.com` and `cdn.espn.com` work. Record fixtures from `site.web.api.espn.com`.
- **The Odds API:**
  - Caesars (`williamhill_us`) wasn't among the bookmakers returned for an NFL game.
  - The account had used 49 credits this month before the build began. After the 14-credit verification call, 437 were left on 2026-09-28. Use the saved fixtures in `tests/fixtures/odds_api/` rather than spending more.
- **nflverse:** it agreed with ESPN on 290 of 290 stat lines. Release downloads go through `release-assets.githubusercontent.com`, which works here.

### What exists

| Path | What it is |
|---|---|
| `parlaytracker/core/models.py` | SQLAlchemy models. Identical to the SPEC.md section 4 code block |
| `parlaytracker/core/schemas.py` | `SlipIn`/`LegIn` (strict gate) and `ExtractedSlip` (loose). Identical to the section 5 block |
| `parlaytracker/core/config.py` | `Settings` (pydantic-settings) and `normalize_database_url()`. The only place env vars are read |
| `parlaytracker/core/db.py` | `make_engine()`, `session_factory()`, `session_scope()` (commits on success, rolls back on error) |
| `parlaytracker/core/odds.py` | `decimal_odds`, `american_odds`, `parlay_decimal`, `payout`, `implied_probability`, `no_vig`, `wilson_interval` |
| `parlaytracker/core/settlement.py` | Pure `score_value`, `settle_leg`, `settle_slip` (sections 7.1 and 7.2). No database access |
| `parlaytracker/core/markets.py` | `MARKET_SPORTS` / `markets_for(sport)`: which markets each sport offers (section 1.2) |
| `parlaytracker/core/services.py` | `create_slip` (the only slip write path), `find_duplicates`, `slip_warnings` |
| `migrations/versions/0001_initial_schema.py` | Whole schema, plus seeded sportsbooks (DraftKings, FanDuel, BetMGM, Caesars) |
| `scripts/dev_postgres.py` | Starts a local Postgres without Docker (via `pgserver`), prints its URL |
| `tests/unit/`, `tests/db/` | See "Tests" below |
| `tests/fixtures/odds_api/` | A real NFL events list and one event's odds for all 14 NFL market keys (cost 14 credits; reuse them, don't re-fetch) |

### Decisions made while building (not spelled out in SPEC.md)

1. **Services flush, never commit.** Callers wrap them in `db.session_scope()`. Keep this for every new service.
2. **`create_slip` rejects a market the event's sport doesn't offer** (e.g. `player_receptions` on an NBA game), using `MARKET_SPORTS`. The UI should use `markets_for(sport)` to fill the market dropdown.
3. **`settle_slip` signals "needs review" by returning status `PENDING` with a `review_reason`.** The caller sets `slip.needs_review` and `slip.review_reason`. Cash-outs aren't handled there; they're a user action (Phase 2 service).
4. **Money rounding:** payouts are rounded half-up to the cent. Unplaced slips settle in units, also to 2 decimal places (a −110 winner returns 1.91).
5. **`american_odds()`** returns +100 for even money, so −100 comes back as +100.
6. **Leg invariant in the database:** a leg's `result` is `pending` exactly when `settlement_source` is NULL. Manual settlement in Phase 2 must therefore set `settlement_source = 'manual'`.
7. **Ruff rules are E, F, W, B, UP, without import sorting.** That keeps `models.py` and `schemas.py` byte-identical to the SPEC.md blocks. If you change either file, update SPEC.md in the same commit, or state in SPEC.md that the code is now canonical.
8. **Dependencies are only what Phases 0–1 need.** Each phase adds its own (section 2.1): `streamlit` (no `[auth]` extra now), `httpx`, `apscheduler<4`, `rapidfuzz`, `pandas`, `plotly`, `Pillow`, `openai`, `nflreadpy`, and `respx` for dev.
9. **`Settings`** already has the Tailscale and OpenRouter fields: `allowed_logins` (split on commas, lowercased), `dev_login`, `qwen_api_key`, `qwen_base_url`, `qwen_vision_model`.

### Alembic gotcha (already fixed once)

Autogenerate wrote the LIKE pattern in `ck_legs_athlete_iff_player_market` as `'player_%%'`, so the database stored `%%`. It's fixed by hand in `0001`. `tests/db/test_migrations.py::test_migrated_schema_matches_models` compares the migrated schema with `create_all()` column by column, constraint by constraint and index by index. **Run it after generating any new migration**, and read every autogenerated file before committing it.

## Running it

```bash
python3.12 -m venv .venv            # or: uv venv --python 3.12 .venv
.venv/bin/pip install -e ".[dev,localdb]"
export TEST_DATABASE_URL="$(.venv/bin/python scripts/dev_postgres.py)"
.venv/bin/ruff check .
.venv/bin/pytest
```

- The local Postgres lives in `.pgdata/` and is lost when the cloud container is reclaimed. Re-run the script in a new session. `--stop` stops it.
- **Install gotcha:** install the package (`pip install -e .`) only after `parlaytracker/` exists. If you get `No module named 'parlaytracker'`, reinstall.
- **In this cloud environment:** `uv` needs `SSL_CERT_FILE=/root/.ccr/ca-bundle.crt` to reach PyPI through the proxy.
- **Test database safety:** the suite drops and recreates schema `public` in `TEST_DATABASE_URL`. It refuses any database whose name doesn't contain `test`. Without `TEST_DATABASE_URL`, database tests are skipped; with `CI` set, a missing URL is an error.
- **Migrations by hand:** `DATABASE_URL=... DISPLAY_TZ=UTC .venv/bin/alembic upgrade head`. `alembic check` confirms the models and migrations agree.

## Tests

- **`tests/unit/test_settlement.py`:** every row of SPEC.md tables 7.1 and 7.2, including the $77 / $35 worked example.
- **`tests/unit/test_schemas.py`:** every "Rejected by `SlipIn` / `LegIn`" case in section 11, plus a few more.
- **`tests/unit/test_odds.py`:** the cases ported from the old `test_math_calculator.py`, the section 7.3 values, and an American ↔ decimal round trip from −500 to +500.
- **`tests/db/test_constraints.py`:** every "Rejected by the database" case in section 11. It inserts through the ORM or raw SQL, bypassing Pydantic, and also checks cascades.
- **`tests/db/test_services.py`:** `create_slip` for single, parlay (with tags and an `other` leg) and SGP; bad references; the sport/market check; duplicate detection.
- **`tests/db/test_migrations.py`:** the migrated schema equals the models; nothing is pending for autogenerate; downgrade and upgrade again, with the seed data restored.

## Next: Phase 2 (logging, then deploy)

The goal is for both users to log singles, parlays and SGPs from their phones and settle them by hand (exit criteria in SPEC.md section 13). Suggested order:

1. **Record fixtures first.** The network is open now. Record real ESPN scoreboard, summary and roster responses for all four sports into `tests/fixtures/espn/` **before** writing parsers (section 6.1), from `site.web.api.espn.com`, one request every 2 seconds. Useful IDs:
   - NFL week 3 finals on `dates=20260927` (e.g. `401872958`, ARI@SF);
   - overtime finals on `20260913` (`401872923`) and `20260920` (`401872936`, `401872945`);
   - a postponed MLB game `401815223` (May 2026);
   - NBA `401810723` and NHL `401803298` (both 2026-03-01).

   Halftime and delay statuses need a live game.
2. **`ingest/http.py`:**
   - one shared httpx client with timeouts;
   - the `ParlayTracker/1.0` User-Agent;
   - single-date queries only;
   - the per-host and global rate limits from section 6.1.

   The full router and circuit breaker are Phase 3–4 work. Building the provider list in order (`site.web.api.espn.com` first) now makes that easy.
3. **`ingest/espn.py`:** scoreboard and roster parsers. Validate with Pydantic and return typed dataclasses. Map statuses to `EventStatus`, including `break` and `delayed`.
4. **New services:**
   - get-or-create an `Event` from a scoreboard game when a user picks it;
   - set a leg result/value manually (`settlement_source = 'manual'`, then re-run `settle_slip` and store the outcome);
   - enter a slip payout, mark cashed out, and enter a closing line manually (`closing_source = 'manual'`);
   - manage tags (rename, merge, retire) and sportsbooks.
5. **App:**
   - `app/auth.py`: the Tailscale guard in section 9.1, reading `Tailscale-User-Login` from `st.context.headers` and checking it against `allowed_logins`, with the `dev_login` fallback;
   - `app/main.py`: `st.navigation`;
   - `components/slip_form.py`: the one shared form, with warnings from `slip_warnings(find_duplicates(...))`;
   - pages Log, Review and Settings.

   Test them with `streamlit.testing.v1.AppTest`.
6. **Deploy to the laptop (section 12):**
   - Rewrite the `Dockerfile`: it still runs the deleted `frontend.py`, so it is broken.
   - Write `deploy/`: `docker-compose.yml` (`db`, `migrate`, `web` on `127.0.0.1:8501`, `worker`), `setup.sh`, `update.sh`, `backup.sh`, `status.sh`, the systemd units and a README with the restore procedure.
   - Add a worker that only writes its heartbeat (Phase 3 fills it in).
   - The laptop is the user's. This cloud session can't reach it: either the user runs `deploy/setup.sh`, or they start `claude remote-control` in the repo folder on the laptop so a session there can.

## Needs the user

Done:
- ~~Network access~~: full access enabled.
- ~~Odds API key~~: set as `ODDS_API_KEY` in the environment.
- ~~Hosting choice~~: laptop + Tailscale.

Still to do:
1. **Prepare the laptop** (before Phase 2 can finish):
   - install Ubuntu Server 24.04 LTS;
   - set it never to sleep and to ignore the lid;
   - create a Tailscale account, install Tailscale on the laptop and both phones, and invite the second user.

   Then run `deploy/setup.sh` once it exists, or start `claude remote-control` on the laptop.
2. **Both users' Tailscale login names** for `ALLOWED_LOGINS`. They can be entered during setup; they don't need to go in chat.
3. **A backup destination** for `rclone`, such as a cloud drive folder.
4. **For Phase 6 only:** an OpenRouter account with a few dollars of credit, and training-permitted providers turned off in its privacy settings. Add its key as the environment variable `QWEN_API_KEY` here, and in the laptop's `.env`.

Never paste keys into chat.

## CI

- **Run 1** on `1d789d7` [passed](https://github.com/dhynesmnk-cyber/parlaytracker/actions/runs/36452614623): ruff clean, pytest green. The Postgres service log shows every expected constraint rejection, so the database tests really ran; they weren't skipped.
- **Run 2** on `efc7880` [passed](https://github.com/dhynesmnk-cyber/parlaytracker/actions/runs/36452792029). That commit changed the workflow in two ways:
  - the health check now names the database (`-d parlaytracker_test`), so it stops logging `FATAL: database "parlaytracker" does not exist`;
  - it moved to `actions/checkout@v5` and `actions/setup-python@v6`, because GitHub warned that the v4/v5 actions target the deprecated Node 20.
- Later commits only touch this file. Check the latest run is green before building on it.
