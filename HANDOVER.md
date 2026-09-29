# Handover

For the next agent picking up ParlayTracker. Read [SPEC.md](SPEC.md) first: it is the design and the build plan. This file says where the build stands and what to do next.

## Where things stand (2026-09-28)

| | |
|---|---|
| Code | All on `main`. Phases 0–2 were merged in [dhynesmnk-cyber/parlaytracker#4](https://github.com/dhynesmnk-cyber/parlaytracker/pull/4), Phases 3–5 in [dhynesmnk-cyber/parlaytracker#6](https://github.com/dhynesmnk-cyber/parlaytracker/pull/6). Work on a branch, and open a PR into `main`: the laptop deploys whatever is merged there |
| Done | Phases 0, 1 and 5 (SPEC.md section 13), and the **code** for Phases 2, 3, 4 and 6 |
| Phase 2 still open | Its exit criteria need the real laptop: install it, log real slips from both phones over Tailscale, reboot, update, and restore a backup once (below) |
| Phase 3 still open | Its exit criteria need a real game day with logged slips (below) |
| Phase 4 still open | Its exit criteria need a real weekend of games (below) |
| Phase 6 still open | The code is done and tested against synthetic replies. The exit needs a `QWEN_API_KEY` and 10 real slips (below) |
| Next after that | Phase 7: live tracking |
| Tests | 1,590 passing locally on Python 3.12 + PostgreSQL 16 (unit, database and Streamlit app tests), plus 2 `live` tests (`-m live`, 0 credits) |
| CI | Two jobs on every push: `test` (ruff + pytest against Postgres 16) and `deploy` (build the image, run the Compose stack as on the laptop, check web + worker, restore a backup). See the CI section at the end |
| Hosting | A dedicated laptop at home with Docker Compose, reachable only over Tailscale (section 12) |
| AI | Qwen through OpenRouter (section 6.3). Without `QWEN_API_KEY` the Screenshot page says so and shows the ordinary form: nothing else depends on it |
| Network | Full access is enabled for this cloud environment, and `ODDS_API_KEY` is set as an environment variable |

## What exists

| Path | What it is |
|---|---|
| `parlaytracker/core/` | Models (0001, 0002), schemas, config, odds maths, settlement, services. `models.py` and `schemas.py` are canonical and identical to the SPEC.md code blocks |
| `parlaytracker/core/services.py` | The only write path. Phase 1: `create_slip`, `find_duplicates`, `slip_warnings`. Phase 2: `upsert_event`, `settle_leg_manually`, `reopen_leg`, `refresh_slip`, `enter_slip_payout`, `mark_cashed_out`, `set_closing_line`, `review_queue`/`review_count`, tag and sportsbook management, and read helpers (`sportsbooks`, `all_tags`, `open_slips`, `recent_slips`, `event_ids_for`) |
| `parlaytracker/ingest/http.py` | Shared httpx client (`ParlayTracker/1.0`, no cookies), `RateLimiter`, and `FetchError` with a `FailureKind` for every failure |
| `parlaytracker/ingest/espn.py` | Box-score parser (`parse_box_score`: summary and cdn documents, read by column key), scoreboard and roster parsers (Pydantic-validated, per-event errors), status mapping, US Eastern game days, and host failover (`site.web.api` → `site.api`). Not yet routed through the breakers in `router.py`: that is Phase 4 |
| `parlaytracker/core/analytics.py` | Pure analytics over `LegRow` / `SlipRow` (section 10): hit rate + Wilson interval, break-even, flat ROI, price and line CLV, dedupe, every dimension and filter, slip money. `load_leg_rows` / `load_slip_rows` fill the rows from Postgres |
| `parlaytracker/ingest/extraction.py` | Screenshot reading (section 6.3, 9.6): `prepare_image` (png/jpg/webp by magic bytes, 8 MB, longest side 2000 px, EXIF upright, pixel guard), `Extractor` (one OpenAI-SDK call to OpenRouter, `temperature=0`, 45 s, `json_schema`), and `parse_reply` (forgiving: code fences, Unicode minus, string numbers, a bad field is dropped and the rest kept). Never logs or raises the key, the image or the raw reply |
| `parlaytracker/app/` | Streamlit app: `main.py` (navigation, auth, health banner), `pages/screenshot.py` (upload, read, then the same form pre-filled), `pages/analytics.py`, `auth.py` (Tailscale guard), `common.py`, `components/slip_form.py`, `pages/log.py`, `review.py`, `settings.py` |
| `parlaytracker/ingest/router.py` | `EspnRouter` (first provider whose breaker isn't open, falls through in the same run, classifies every failure, keeps raw samples, records watched events; the web app uses one too, with in-memory breakers), `CircuitBreaker` (one per provider, section 8.3 rules) and `Breakers`, which writes every transition to `source_health` at once and the hourly counters at each heartbeat, and restores open breakers and the Odds API quota on restart. The ESPN failover router is Phase 4 |
| `parlaytracker/ingest/guards.py` | Pure integrity rules: progress key, stale / correction / advance, final never goes back to play, frozen-feed and probe rules, plausibility bounds. Frozen-feed *use* (the probe) is Phase 7 |
| `parlaytracker/ingest/nflverse.py` | `NflverseData`: schedules, players, weekly stats and snap counts through `nflreadpy`, mapped only by ID, loaded once per job run, failures through the `nflverse` breaker |
| `parlaytracker/ingest/odds_api.py` | Odds API client (`events`, `event_odds`) and Pydantic parsers. Reads `x-requests-remaining`/`x-requests-last`; feeds the breaker |
| `parlaytracker/ingest/closing.py` | Pure closing-line selection: exact main, exact alternate, book's main line, median main (section 8.2 step 5), on parsed odds |
| `parlaytracker/ingest/resolve.py` | Market and sport keys, team-name matching and rapidfuzz player matching (score >= 90, suffixes like Jr./III ignored). Phase 6 adds sportsbook, market, sport, team-alias, event and roster-player resolution for screenshots (`resolve_slip`) |
| `parlaytracker/worker/settle.py` | `CheckFinals`, `Settle`, `RecheckSettled`, `VerifyNfl`, `Canary`, `prune_samples`, `sample_sink` (section 8.1, 7.1) |
| `parlaytracker/cli.py` | `export-sample <id> <path>`, `backfill` (below) and `read-slip <image> [--save path]` (reads a screenshot as the page does; `--save` keeps the raw reply as a fixture) |
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
24. **Dependencies:** each phase adds its own. Phase 3 added `apscheduler<4` and `rapidfuzz`, Phase 4 `nflreadpy` (which brings polars, pandas and pyarrow: the image is larger), Phase 6 `openai` and `Pillow`; later phases need `pandas`, `plotly`, `Pillow`, `openai` and `nflreadpy`.

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
