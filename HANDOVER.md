# Handover

For the next agent picking up ParlayTracker. Read [SPEC.md](SPEC.md) first: it is the design and the build plan. This file says where the build stands and what to do next.

## Where things stand (2026-09-29)

| | |
|---|---|
| Code | All on `main`. Phases 0–2 were merged in [dhynesmnk-cyber/parlaytracker#4](https://github.com/dhynesmnk-cyber/parlaytracker/pull/4), Phases 3–5 in [dhynesmnk-cyber/parlaytracker#6](https://github.com/dhynesmnk-cyber/parlaytracker/pull/6). Work on a branch, and open a PR into `main`: the laptop deploys whatever is merged there |
| Done | Phases 0, 1 and 5 (SPEC.md section 13), and the **code** for Phases 2, 3, 4, 6 and 7 |
| Phase 2 still open | Its exit criteria need the real laptop: install it, log real slips from both phones over Tailscale, reboot, update, and restore a backup once (below) |
| Phase 3 still open | Its exit criteria need a real game day with logged slips (below) |
| Phase 4 still open | Its exit criteria need a real weekend of games (below) |
| Phase 6 still open | The code is done and tested against synthetic replies. The exit needs a `QWEN_API_KEY` and 10 real slips (below) |
| Phase 7 still open | The code is done and simulated. The exit needs one real live game with a real recording (below) |
| Next after that | All eight phases are built. What is left is real-world verification (Phases 2, 3, 4, 6, 7) |
| Real data | 25 real Hard Rock slips (user 1, 2026-09-14 to 09-27) were imported and settled: all 77 legs and 25 slips match what the sportsbook said, and nflverse independently agreed on all 77 (see "Checked by hand with real slips"). The CSV is **not** in the repo (stakes and slip ids) |
| Tests | 1,849 passing locally on Python 3.12 + PostgreSQL 16 (unit, database and Streamlit app tests), plus 2 `live` tests (`-m live`, 0 credits) |
| CI | Two jobs on every push: `test` (ruff + pytest against Postgres 16) and `deploy` (build the image, run the Compose stack as on the laptop, check web + worker, restore a backup). See the CI section at the end |
| Hosting | A dedicated laptop at home with Docker Compose, reachable only over Tailscale (section 12) |
| AI | Qwen through OpenRouter (section 6.3). Without `QWEN_API_KEY` the Screenshot page says so and shows the ordinary form: nothing else depends on it |
| Network | Full access is enabled for this cloud environment, and `ODDS_API_KEY` is set as an environment variable |

## What exists

| Path | What it is |
|---|---|
| `parlaytracker/core/` | Models (0001–0003), schemas, config, odds maths, settlement, services. `models.py` and `schemas.py` are canonical and identical to the SPEC.md code blocks |
| `parlaytracker/core/services.py` | The only write path. Phase 1: `create_slip`, `find_duplicates`, `slip_warnings`. Phase 2: `upsert_event`, `settle_leg_manually`, `reopen_leg`, `refresh_slip`, `enter_slip_payout`, `mark_cashed_out`, `set_closing_line`, `review_queue`/`review_count`, tag and sportsbook management, and read helpers (`sportsbooks`, `all_tags`, `open_slips`, `recent_slips`, `event_ids_for`) |
| `parlaytracker/ingest/http.py` | Shared httpx client (`ParlayTracker/1.0`, no cookies), `RateLimiter`, and `FetchError` with a `FailureKind` for every failure |
| `parlaytracker/ingest/espn.py` | Box-score parser (`parse_box_score`: summary and cdn documents, read by column key), scoreboard and roster parsers (Pydantic-validated, per-event errors), status mapping, US Eastern game days, and host failover (`site.web.api` → `site.api`). Not yet routed through the breakers in `router.py`: that is Phase 4 |
| `parlaytracker/core/analytics.py` | Pure analytics over `LegRow` / `SlipRow` (section 10): hit rate + Wilson interval, break-even, flat ROI, price and line CLV, dedupe, every dimension and filter, slip money. `load_leg_rows` / `load_slip_rows` fill the rows from Postgres |
| `parlaytracker/ingest/extraction.py` | Screenshot reading (section 6.3, 9.6): `prepare_image` (png/jpg/webp by magic bytes, 8 MB, longest side 2000 px, EXIF upright, pixel guard), `Extractor` (one OpenAI-SDK call to OpenRouter, `temperature=0`, 45 s, `json_schema`), and `parse_reply` (forgiving: code fences, Unicode minus, string numbers, a bad field is dropped and the rest kept). Never logs or raises the key, the image or the raw reply |
| `parlaytracker/app/` | Streamlit app: `main.py` (navigation, auth, health banner), `pages/screenshot.py` (upload, read, then the same form pre-filled), `pages/analytics.py`, `auth.py` (Tailscale guard), `common.py`, `components/slip_form.py`, `pages/log.py`, `review.py`, `settings.py` |
| `parlaytracker/ingest/router.py` | `EspnRouter` (first provider whose breaker isn't open, falls through in the same run, classifies every failure, keeps raw samples, records watched events; the web app uses one too, with in-memory breakers), `CircuitBreaker` (one per provider, section 8.3 rules) and `Breakers`, which writes every transition to `source_health` at once and the hourly counters at each heartbeat, and restores open breakers and the Odds API quota on restart. The ESPN failover router is Phase 4 |
| `parlaytracker/ingest/guards.py` | Pure integrity rules: progress key, stale / correction / advance, final never goes back to play, frozen-feed and probe rules, plausibility bounds. The frozen-feed *use* (the probe) is in `worker/live.py` |
| `parlaytracker/ingest/nflverse.py` | `NflverseData`: schedules, players, weekly stats and snap counts through `nflreadpy`, mapped only by ID, loaded once per job run, failures through the `nflverse` breaker |
| `parlaytracker/ingest/odds_api.py` | Odds API client (`events`, `event_odds`) and Pydantic parsers. Reads `x-requests-remaining`/`x-requests-last`; feeds the breaker |
| `parlaytracker/ingest/closing.py` | Pure closing-line selection: exact main, exact alternate, book's main line, median main (section 8.2 step 5), on parsed odds |
| `parlaytracker/ingest/resolve.py` | Market and sport keys, team-name matching and rapidfuzz player matching (score >= 90, suffixes like Jr./III ignored). Phase 6 adds sportsbook, market, sport, team-alias, event and roster-player resolution for screenshots (`resolve_slip`) |
| `parlaytracker/ingest/slip_import.py` | Loads slips transcribed into a CSV (`cli import-slips`, dry run unless `--apply`) and checks settled results against them (`cli check-import`). One row per leg; plans each slip against ESPN (game by matchup and start time, players by roster, printed wording cross-checked with `resolve_market` / `implied_line`), writes through `services.create_slip(placed_at=...)`, remembers a slip by `notes = "import <book> #<slip id>"` so a second run adds nothing |
| `parlaytracker/worker/settle.py` | `CheckFinals`, `Settle`, `RecheckSettled`, `VerifyNfl`, `Canary`, `prune_samples`, `sample_sink` (section 8.1, 7.1) |
| `parlaytracker/core/live.py` | The Live page's rules and read-model, pure and tested with exact numbers: which events are active (30 min before kickoff to 8 h after), the cadence table, a leg's live value and over/under state, freshness colours (amber after 2 min, red after 5 in play), "no change for N min", game status text, parlay progress, "ESPN unavailable since", and the verified / awaiting / unverified label |
| `parlaytracker/worker/live.py` | `PollNflLive` (`poll_nfl_live`, every 30 s): only the requests due under the cadence table, one scoreboard call per game day, a box score per game with pending player legs, the integrity guards, `live_value` / `live_source` on pending legs through `services.set_live_value`, and the frozen-feed probe. Never settles anything |
| `parlaytracker/app/pages/live.py` | The Live page: cards for pending slips with an active NFL game, in an `st.fragment(run_every=15)`, then "Settled in the last 7 days" |
| `parlaytracker/cli.py` | `export-recording <event> <dir>` (a recorded game, in order, with an index, for replay), `export-sample <id> <path>`, `backfill` (below) and `read-slip <image> [--save path]` (reads a screenshot as the page does; `--save` keeps the raw reply as a fixture) |
| `parlaytracker/worker/` | `__main__.py`: advisory lock, `BlockingScheduler` (defaults `coalesce=True, max_instances=1, misfire_grace_time=30`). `jobs.py`: `heartbeat`, `guarded` (a job never raises into the scheduler) and `ClosingCapture` (the `capture_closing` job). Without `ODDS_API_KEY` only the heartbeat runs |
| `Dockerfile`, `.dockerignore`, `.streamlit/config.toml` | One image for web, worker and migrations; the config hides Streamlit's developer menu |
| `deploy/` | `docker-compose.yml`, `setup.sh`, `update.sh`, `backup.sh`, `restore.sh`, `status.sh`, `compose.sh`, `lib.sh`, `env.example`, systemd units, and `README.md` (the user's step-by-step guide) |
| `tests/fixtures/espn/` | Real ESPN responses: NFL final, overtime and scheduled scoreboards; an MLB day with 2 postponements; NBA and NHL scoreboards; rosters for all four sports (NBA's is a flat list, the others are grouped); an Akamai 403 page |
| `tests/live/` | Tests that call real services, run with `-m live`, all free: the Odds API events endpoint, and the canary (every ESPN provider and nflverse) |
| `tests/fixtures/espn/*summary*`, `nfl_cdn_game_*` | Real box scores (NFL, NFL overtime, NBA, NHL, NHL overtime, and the cdn wrapper), trimmed to the fields the parser reads |
| `tests/fixtures/nflverse/week3_2026.json` | Slices of the four nflverse datasets for week 3 of 2026 (including a game with no scores yet) |
| `tests/fixtures/qwen/` | **Synthetic** Qwen replies, hand-written to the schema (see its README). Replace or add real ones at the Phase 6 exit |
| `tests/support.py` | Shared helpers for worker tests: committed data, `FakeRouter` (real parsers on fixtures), `nflverse_loader` |
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
10. **`capture_closing` tries each game once per successful response**, tracked in memory (`ClosingCapture._attempted`). A leg the API has no line for isn't retried and paid for every minute until kickoff. A failed call (transient error, open breaker, rate limit) *is* retried on the next tick. A worker restart inside the 5-minute window can cost one repeat.
11. **A missing closing line isn't a `needs_review` flag.** The Review queue already lists legs on games that started in the last 7 days with no closing line, and keeps them out of the nav badge (decision 5). The worker only logs why.
12. **A 422 from the Odds API (for example an unknown market key) is `RequestRejected`, not a breaker failure.** Otherwise one unverified market (`player_points`) could open the breaker and block NFL captures.
13. **Median closing odds are the median of implied probabilities**, converted back to American, because averaging American odds across the +100/-100 gap is meaningless. The median line is `median_low`, so it is always a real line.
14. **httpx's loggers are set to WARNING** in `ingest/http.py`. httpx logs full request URLs at INFO, and the Odds API key is a query parameter: without this the worker's log would contain the key. `tests/unit/test_odds_api.py` checks it.
16. **Settlement rules the worker follows** (SPEC.md 7.1, plus what the spec left open):
    - `settle` calls the summary once per final event, even when only team legs are pending: the box score's final score is authoritative and also corrects the event, and it names the provider for `settlement_source`.
    - `services.settle_leg_auto` enforces the 10-minute gate itself (constraint 2) and refuses to touch a settled leg. ESPN values need `final_at + 10 min`; nflverse values need kickoff + 4 h (its data is overnight, so there is no ESPN "final" to wait on).
    - Legs already `needs_review` are left to a person; the worker never overwrites a flag's reason.
    - "Played, no stat" is kept in `review_reason` with `needs_review` false: invisible in Review, queryable.
    - A player with a nflverse row but a null stat counts as having no stat line.
17. **`check_finals` also covers NFL until Phase 7** (the spec says NBA/NHL/MLB only). Without `poll_nfl_live` nothing else would ever mark an NFL game final. It starts 3 h after kickoff, every 15 min.
18. **`apply_event_reading` compares a box score (no period or clock) as if the event hadn't moved**; otherwise a final box score would look older than the scoreboard's final and be discarded as stale.
19. **Values in Review messages are normalised** (`fmt`): the database keeps `Numeric(8,1)`, so a stored 25 reads back as 25.0.
21. **Analytics definitions** (SPEC.md section 10, plus what it left open):
    - The unit is a settled non-`other` leg, deduplicated by selection key (earliest logged wins, across both users). **Voids are dropped** (a refunded bet that never happened); **pushes are kept but are neither a win nor a loss**, and are shown in their own column.
    - Hit rate is wins / (wins + losses). ROI stakes one unit on each win or loss *that has odds*; pushes stake nothing.
    - **Both sample sizes are shown**: `n` (decided legs) and the number with odds, which break-even, ROI and price CLV use.
    - **Price CLV** needs the closing line to equal the line taken; **line CLV** is only counted for legs whose line moved (a leg that closed on the same line has no line CLV, rather than a zero that would dilute the mean). They are separate columns, each with its own n.
    - "Low sample" is `wins + losses < MIN_SAMPLE`. The evidence column says "interval above break-even" only when the whole Wilson interval (over the legs with odds) clears break-even.
    - Weekday, month and start window are of the **Eastern** game day. Lead time has a fifth bucket, "logged after the start", which the spec doesn't list.
    - The multiple-comparisons caution appears once more than two slices are applied; tags count as one slice.
    - The group-by selectbox uses the string `"all"`, not `None`: Streamlit reads `None` as "nothing selected".
23. **Screenshot reading** (SPEC.md 6.3 and 9.6, plus what the spec left open):
    - The reader only pre-fills. `slip_form.prefill` writes `*_default` keys, never a widget's own key, so the person can change anything and Save is the only write (through `create_slip`, `source = 'screenshot'`).
    - **Doubt is marked, never resolved silently.** A leg or slip field the resolver wasn't sure of gets a "⚠ check" label. Doubtful: sport guessed from the default; a game found from one team only or a doubleheader; a market with no wording; a player scoring 75-89 (pre-selected) or with a runner-up within 3 points; a team market with no team; a line that isn't a whole or half number; odds between -99 and +99 (dropped); a sportsbook that doesn't match.
    - A game is looked for on the slip's day, then a day either side. A name that fits two games ("New York" on a day both New York teams play) matches neither: first words shared by many teams (`new`, `los`, `san`...) are never aliases, and an alias both teams of one game share identifies neither.
    - An **Under**, or wording we don't support, becomes an `other` leg with the slip's own words as its description. "Over/Under" and "O/U" are game totals, not Unders.
    - A stake on the slip pre-ticks "I placed this bet" (a screenshot of a bet with a stake is usually placed): check it.
    - Each image is read **once** (sha256 in session state), not on every rerun; after Save the uploader and form start fresh.
    - **The SDK ships its own HTTP library** (`httpx2`), so `respx` can't intercept it. Tests give the real client a mock transport and assert the mock was reached; `tests/unit/test_extraction.py` verifies the actual wire request (URL, bearer header, body, 45 s timeout).
25. **Live tracking** (SPEC.md 8.1, 8.3 and 9.4, plus what the spec left open):
    - **Live values are display only.** `services.set_live_value` refuses a settled leg and never touches `result`; the tests check that a total already far past its line stays pending. Settlement still needs `final_at + 10 min` from the box score.
    - **A player missing from the live box score shows "no stat line yet", never 0** (section 6.1: absence means neither zero nor void). Team legs take their live value from the scoreboard's score.
    - **The frozen-feed probe is one request per game day every 5 minutes**, not one per game. It returns the whole day, and a stuck 14-game slate would otherwise cost 14 identical requests per interval. If any frozen game is *ahead* on the other provider, that provider's breaker opens as `frozen` (5 min) and the fresher data is applied to every game that day. A probe that finds the same key means the game is stopped (a review, an injury): nothing changes, and no provider switch. Halftime and delays are never "frozen".
    - **Requests in one tick are spaced 2.1 s apart** inside the job (a pause, not a wait in the rate limiter), so a busy Sunday isn't refused by the 1-per-2-s-per-host limit. A 14-game slate with props in 5 games measures about 7 requests a minute.
    - **A parlay with a lost leg has lost** (section 7.2), so it is no longer pending and has no live card: it appears under "Settled". "N lost" in the progress line exists in the code and its tests but can't show on a card for that reason.
    - **Break and delay freshness limits are 4 and 7 minutes** (the spec gives 2 and 5 for a game in play only): those states are polled every 2 minutes, so 2 and 5 would always be amber.
    - **The health banner ignores a half-open provider** (open time passed, awaiting a trial). Fallbacks that were blocked aren't retried while the primary answers, so their rows would otherwise say "failing" until the next daily canary. A schema failure is still shown until a trial succeeds.
    - **Scoreboard recordings are tagged with the watched event** (they hold the whole day), so `export-recording` can pick a game out.
26. **Dependencies:** each phase adds its own. Phase 3 added `apscheduler<4` and `rapidfuzz`, Phase 4 `nflreadpy` (which brings polars, pandas and pyarrow: the image is larger), Phase 6 `openai` (3.x: it ships its own HTTP library, `httpx2`) and `Pillow`, plus `pandas` declared explicitly (Phase 5 imported it without declaring it). `tests/unit/test_dependencies.py` fails if the application imports anything `pyproject.toml` doesn't declare: PR #7's first CI run failed because `openai` was installed locally but not declared, and it would have broken the Screenshot page in the image; later phases need `pandas`, `plotly`, `Pillow`, `openai` and `nflreadpy`.

27. **Four more NFL player markets** (added 2026-09-29 after the first real slips: 15 of 77 legs were in them): `player_pass_completions`, `player_touchdowns`, `player_interceptions`, `player_field_goals` (migration 0003 swaps the `market_type` CHECK; SPEC.md updated with the models).
    - **Touchdowns** = rushing + receiving + kick return + punt return + defensive TDs, never a passing one. "Anytime TD" is Over 0.5, "To score 2+ TDs" Over 1.5. ESPN: five tables added up (`STAT_COLUMNS` now holds several sources per market); nflverse: `rushing_tds + receiving_tds + special_teams_tds + def_tds`. A player in none of the tables is "missing", so the usual snaps rule settles him at 0. `interceptionTouchdowns` is left out on purpose (it may double-count `defensiveTouchdowns`); untested against a real defensive score.
    - **Interceptions** are thrown ones (the `passing` table). **Completions** and **field goals** read the first number of ESPN's "38/52" and "3/3" columns.
    - **No Odds API keys yet**, so `NO_AUTO_CLOSING` (`core/markets.py`) keeps these legs out of closing capture (a wrong key would get a whole request rejected) and out of the "no closing line" Review item; they can still be entered by hand. To turn them on: verify the keys on a real game (SPEC.md section 6.2 lists candidates), add them to `MARKET_KEYS`, shrink `NO_AUTO_CLOSING`.
    - The screenshot resolver reads the wording Hard Rock prints ("ANYTIME TD", "TO SCORE 2+ TDS", "TO RECORD 65+ RUSHING YARDS"): `implied_line` turns a ladder into Over N - 0.5 and "anytime" into 0.5. Passing / first / last touchdown wording resolves to `other`.
28. **Importing real slips** (`cli import-slips`, `cli check-import`): the CSV is the ground truth, so the importer only writes slips it can fully explain. A slip with an *error* (no such game, wrong kickoff time, player on neither roster, status disagreeing with its legs, unsupported bet type) is never written; one with a *doubt* (paid amount off by more than $1.00 from the odds, printed wording reading as another market, a fuzzy player match, placed after kickoff) needs `--include-doubtful`. Events are stored `scheduled` on purpose: saving a game that is already `final` would leave it with no `final_at` and it would never settle; the backfill finds it final and dates it. (The slip *form* has that same trap when a slip is logged after the game ended: `upsert_event(status=g.status)`. Not fixed; worth a look.)
    - **Payouts:** a won slip's printed `paid` is stored as `potential_payout`, but `slips.payout` is computed from the rounded odds, so it differs by cents (4 wins: +0.14, +0.16, +0.04, -0.14). ROI is off by that much; not corrected.
    - **The backfill now waits** for ESPN's request slots (`EspnRouter(default_max_wait=30)`) and **stops without settling or flagging anything** if any game is unreachable, because `Settle` would otherwise flag it "Not final 8 hours after its start". Before this, a 25-slip backfill flagged 43 legs for Review.

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

## Checked by hand in Phase 3

- **The real worker process** against the migrated test database: it starts with the heartbeat and closing-line jobs, writes a `source_health` row for all five providers, a second worker exits with code 1, and SIGTERM stops it cleanly (exit 0). The real API key appeared in its log 0 times.
- **The live Odds API** (`pytest -m live`, the free events endpoint): the events list parses and the quota headers are read, at a cost of 0 credits. Nothing has yet called the paid odds endpoint through the client: that is covered only by the recorded fixture.

## Checked by hand in Phase 4

- **The real worker and the canary** (`pytest -m live`, and the worker's startup run): all three ESPN providers (`site.web.api`, `site.api` and `cdn.espn.com`) parsed a real completed NFL game, and nflverse loaded all four datasets. `site.api.espn.com` answered from this cloud environment on 2026-09-28, though the spec recorded it blocked the day before; the canary will tell you about the laptop's connection on first start.
- **ESPN and nflverse agree on real data:** all 18 receiving lines in ARI @ SF match nflverse's weekly stats.
- **Mutation checks:** breaking the disagreement comparison and the Tuesday deadline each made the intended tests fail.

## Checked by hand in Phase 5

- **Against the spec's own numbers:** 18 wins from 30 at -110 reads 60.0% (42-75%) against a 52.4% break-even and "not yet evidence of an edge"; Over -110 closing at Over -125 / Under +105 is +0.87 pp of price CLV; Over 45.5 closing at 47.5 is +2.0 of line CLV.
- **End to end:** real slips created through `create_slip`, settled and closed through the services, then summarised: hit rate, ROI, break-even and both CLV figures match hand calculation.
- **Mutation checks:** flipping the alt-spread sign, keeping the latest duplicate instead of the earliest, and an off-by-one in the low-sample threshold each made the intended tests fail.
- **Not looked at in a browser:** the page is exercised through `AppTest`. Greying is tested on the styled frame's CSS, not by eye. Worth a glance on a phone once there is real data.

## Checked by hand in Phase 6

- **The wire request**, not just a fake client: with the OpenAI SDK's own transport mocked, the request is a single POST to `openrouter.ai/api/v1/chat/completions` with the bearer key, the model, `temperature` 0, a `json_schema` response format, one message holding a base64 image and the prompt, and a 45 s timeout.
- **Failure paths through the real SDK** (401, 403, 429, 500, 503, non-JSON 200, empty completion, timeout) are all a single "Couldn't read this slip. Enter it manually." with an empty form, and no key or response body reaches the logs.
- **Mutation checks:** dropping the player default, reading on every rerun, dropping the doubt marks, and saving as `quick_add` each made the intended tests fail; breaking the "no key in errors" safeguard made the leak test fail.
- **Not done, and can't be without a key:** any real model call. No real slip has been read. Every Qwen reply in the tests is synthetic.

## Checked by hand in Phase 7

- **Simulated, not recorded.** No game was live while this was built (next NFL kickoff is Thursday 2026-10-01, 8:15 pm ET), so there is **no real recording**. Instead the real `EspnRouter`, breakers, rate limiter, `PollNflLive`, `Settle` and Postgres ran on a fake clock with only the network mocked, over the real scoreboard and box-score JSON edited to a moment in a game (`tests/db/test_live_simulation.py`). That covers: failover within one run when the primary is blocked; every host blocked turns cards red within 5 minutes and shows the unavailable message; recovery without a restart; halftime and a six-minute stopped clock never switching provider; live never settling; `Settle` honouring the ten-minute gate; and the request limits over a whole game and over a 14-game Sunday.
- **The record, export and replay loop** (`tests/db/test_live_recording.py`): recorded through the real router with an outage in it, exported with the new command, state wiped, replayed through a fresh job: identical final state.
- **Mutation-style findings while building:** a stuck 14-game slate cost 14 probes per interval (now one per day); the health banner kept calling a recovered fallback "failing" (now ignores half-open providers).
- **Not done:** any real live game. The status names for halftime and delays (`STATUS_HALFTIME`, `STATUS_END_PERIOD`, delay wording) and the live `period`/`clock` fields are still the spec's expectations, not observed (SPEC.md section 15). The page was tested through `AppTest`, not in a browser on a phone during a game.

## Checked by hand with real slips (2026-09-29)

- **25 real Hard Rock slips** (user 1: 23 three-leg and 2 four-leg SGPs, 77 legs, stakes $970, 21 lost, 4 won) from 2026-09-14 to 09-27, transcribed to a CSV by the user. Every game, kickoff time and player matched against live ESPN data; the printed wording agreed with the market on every leg.
- **Imported into a throwaway Postgres and settled by the real jobs** (`import-slips --apply`, `backfill`, `check-import`): 77 of 77 legs settled from ESPN, **all 77 verified by nflverse** (an independent source), and every leg and slip result matched what the sportsbook said. That includes the new markets (7 touchdown, 4 completion, 2 interception, 2 field goal legs).
- **The Analytics page renders on it** (75 legs after dedupe: two selections appeared on two slips). Hit rate 49.3% (38-60%), sgp slips: staked 970.00, returned 873.20 computed (873.40 as paid), ROI -10.0%, "low sample". No CLV: past games have no closing lines.
- **Mutation-checked:** dropping the kickoff-time check, the status-vs-legs check, the payout check, the placed-after-kickoff check, the `placed_at` write, the result comparison, the `scheduled` event status and the backfill stop each made a test fail.
- **Not done:** the same on the laptop's real database (`import-slips` needs ESPN and a `--user` login; the CSV is on the user's side); the 25 screenshots were not read by Qwen (no key), so this says nothing about Qwen's accuracy; closing lines and CLV for these slips can't be recovered.

## Next steps

**Loading real slips on the laptop (needs the CSV):**
1. Copy the CSV to the laptop (not into the repo). `compose.sh run --rm worker python -m parlaytracker.cli import-slips /path/slips.csv --user <login>` is a dry run: read every ERROR and doubt. Then add `--apply` (and `--include-doubtful` for the ones you have checked).
2. Stop the worker, `... cli backfill` (re-run it if it says games were unreachable), start the worker, then `... cli check-import /path/slips.csv`: it exits non-zero and names every leg or slip where our result differs from the sportsbook's.
3. New slips: the same importer takes any CSV of this shape; other bet types (`single` is supported, `parlay` is not) need extending.

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

**Phase 3, on a real game day (needs the laptop worker running with `ODDS_API_KEY` set):**
1. Log some real slips (singles and an SGP, at least one at a book the API doesn't return, such as Caesars) for games starting soon.
2. Check that every eligible leg got a closing line (`closing_source = 'odds_api'`) or shows in Review as "No closing line was captured".
3. Compare the credits used (`source_health.quota_remaining` before and after) with the estimate: one credit per market in the first call, plus one per alternate market in a follow-up.
4. Verify `player_points` and `player_points_alternate` on an NBA or NHL game (section 6.2). Until then a 422 is logged and the legs go to manual entry.

**Phase 4, on a real weekend (needs the laptop worker running):**
1. First run the one-off backfill for anything logged since Phase 2. Stop the worker (the CLI refuses to run beside it), run the backfill, then start the worker again: `compose.sh stop worker`, `compose.sh run --rm worker python -m parlaytracker.cli backfill`, `compose.sh start worker` (see `deploy/README.md`).
2. Log real slips for a weekend of games, including player props. After the games, check every settled leg against what the sportsbook paid.
3. Check every NFL leg from that weekend is either `verified_at` set (nflverse agreed) or has a Review item explaining why not. Legs settled from nflverse (ESPN had no line) stay unverified by design.
4. Look at `source_health` and the banner for the breakers, and `raw_samples` for anything the parsers rejected. `python -m parlaytracker.cli export-sample <id> <path>` turns a sample into a fixture.
5. Statuses still **to verify on a live game** (SPEC.md section 15): the halftime and delay names, and the live `period`/`clock` fields. That is Phase 7's recording.

**Phase 7, on the next live game (the code is done):**
1. Before kickoff, put the game's ESPN event id in `RECORD_EVENT_IDS` in the laptop's `.env` and `compose.sh up -d`. Find the id from the scoreboard (`https://site.web.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?dates=YYYYMMDD`, field `events[].id`). The next game is Thursday 2026-10-01, 8:15 pm ET (Steelers at Browns on the Odds API's list).
2. Log a slip with a game total, a team total and a player prop on it, and watch the Live page on the phone during the game. Look for: the clock and status text, the amber and red colours, and that the numbers match the broadcast.
3. Prove the exit by hand: block `site.web.api.espn.com` mid-game (on the laptop, a `/etc/hosts` line pointing it at `127.0.0.1` works) and check the next poll uses `site.api.espn.com` (Settings and the banner; `source_health`); then block every ESPN host and check the cards turn red within 5 minutes with "Live data unavailable"; then unblock and check it recovers without restarting the worker.
4. Afterwards `python -m parlaytracker.cli export-recording <event id> tests/fixtures/espn/recording_<id>/` (through `compose.sh run --rm worker ...`), commit it, and add a test that replays it with `tests/support.py::RecordingRouter` through `PollNflLive`. Check the recorded status names against `espn.map_status` and update SPEC.md section 15.
5. The frozen-feed rule may fire on the real feed (a stopped clock for a review is normal): the logs say "stopped, not frozen" when it doesn't switch.

**Phase 5 is done** (its exit is test-verifiable and passes). Look at the page with real data once a few games have settled; the interesting number early on is CLV, not ROI.

**Phase 6, when the OpenRouter key arrives (the code is done):**
1. Put `QWEN_API_KEY` in the laptop's `.env` (and, to try it here, as an environment variable), turn off training-permitted providers in OpenRouter's privacy settings, and `compose.sh up -d`. The Screenshot page then shows the uploader.
2. Collect 10 real slip screenshots from at least two sportsbooks (singles, parlays and SGPs). For each: `python -m parlaytracker.cli read-slip slip.png --save tests/fixtures/qwen/real_NN.json`, and look at what came back. **Read each saved reply before committing it** (a slip can show a stake or account details), and note in `tests/fixtures/qwen/README.md` which files are real.
3. For each, also try it on the Screenshot page. The exit is that most fields pre-fill correctly and every failure lands on the manual form. Whatever it gets wrong is a new alias or a prompt fix: add a test with the real reply.
4. Likeliest first corrections: sportsbook wording (`BOOK_ALIASES`), market wording (`_MARKET_PATTERNS`), and whether the model puts the team in `team_text` for team markets.
5. Pick the model deliberately: `qwen/qwen3-vl-32b-instruct` is the default (cents per hundred slips); the free `qwen/qwen3.8-27b:free` is capped at 50 requests a day. It is only a `QWEN_VISION_MODEL` setting.

## Needs the user

1. **Rotate the Odds API key.** A config error during testing printed about 29 of its 32 characters into this session's tool output. It was never committed or sent anywhere else, and the leak is fixed (decision 7), but a new key from the-odds-api.com is the safe move. Then update the `ODDS_API_KEY` environment variable here, and the laptop's `.env` once it exists.
2. **Prepare the laptop:** `deploy/README.md` steps 1–3.
3. **Both users' Tailscale login names** for `ALLOWED_LOGINS`. They're entered during setup and don't need to go in chat.
4. **A backup destination** for `rclone` (README step 4), then one test restore (step 5).
5. **An OpenRouter key** as `QWEN_API_KEY`, with training-permitted providers turned off (you're waiting on customer service for this). The Screenshot page works as a plain manual form until then, and the rest of the app doesn't use it.

Never paste keys into chat.

## CI

- **Runs 1–2** (`1d789d7`, `efc7880`) passed. From run 2 the workflow names the database in the Postgres health check and uses `actions/checkout@v5` / `actions/setup-python@v6`.
- **Run 9** on `d4fdc38` (Phase 2) [passed](https://github.com/dhynesmnk-cyber/parlaytracker/actions/runs/36494175532), including the new `deploy` job:
  - the image built and the stack started in 40 s;
  - the web app answered on `127.0.0.1:8501` only, and the worker heartbeat appeared;
  - a backup (28 KB) restored a deleted row, and the app came back healthy.
- **PR #6 (Phases 3–5)**, head `c9016fe`: [`test` and `deploy` both passed](https://github.com/dhynesmnk-cyber/parlaytracker/actions/runs/36502601316). `deploy` now builds the image with the Phase 3–4 dependencies (`apscheduler`, `rapidfuzz`, `nflreadpy` and its polars/pandas/pyarrow), so that is the check that the image still builds and the worker starts with all its jobs registered.
- **Merging deploys.** The laptop follows `main` and updates within about 5 minutes of a merge. The first start after this merge runs the worker's new `canary` job, and the banner will name any provider it can't parse. Then follow "Phase 4, on a real weekend" under Next steps, starting with the backfill.
- Check the latest run on the PR is green before building on it.
