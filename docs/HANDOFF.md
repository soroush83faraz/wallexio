# Handoff

Read this first, then [README.md](../README.md). Snapshot date: 2026-10-22. Release tag: `v0.1.0-shadow`.

## State in one paragraph

A long-only USDT spot momentum bot for Wallex that runs end to end in `mock` (paper, offline replay) and `shadow`
(live public order book, simulated fills, no orders sent) modes. **Live trading does not exist in this build.**
The safety layers (single order path, fail-closed risk engine, kill switch, in-process guardian, append-only audit
store, redaction, live-arming gates) are implemented and were attacked by independent verification rounds.
**No net edge has been demonstrated**: with realistic costs the strict strategy takes no trades and the baseline
loses money on every train/validation configuration ([reports/README.md](../reports/README.md),
[reports/v2_summary.md](../reports/v2_summary.md)). "Deployable" here means *deployable in shadow mode*, not
"profitable" and not "cleared for real money".

## Quality gates at handoff

| Check | Result |
|---|---|
| `pytest -q` | 1974 passed (fresh venv, ~2 min; 1970 before the public-docs check of the private client, 1948 at the first handoff) |
| `ruff check --select F,E9` (src, tests, deploy) | clean (run in a throwaway venv; ruff is not a project dependency) |
| `mypy --ignore-missing-imports src` | **113 findings in 24 files**, informational. The project has no type-check config and was never type-clean; not addressed |
| `pip wheel .` / non-editable install | wheel contains `wallexbot/default_config.yaml` (inspected by `tests/test_packaging.py`); installed into a fresh venv **outside the repo** it ran `demo`, loaded the default config and refused `bot --mode live` (exit 2) |
| `docker build` of the shipped Dockerfile | builds with `--build-arg BASE_IMAGE=docker.arvancloud.ir/library/python:3.12.11-slim-bookworm` (same pinned tag via a mirror). In the image: offline `demo`, `bot --mode mock --replay-from` (synthetic 3-day history, 600 ticks) and `bot --mode live` (exit 2) with `--network none --read-only --cap-drop ALL --user 10001`; shadow default CMD 160 s with network: HEALTHCHECK `healthy`, `docker stop` exit 0 (about 2 s), `PRAGMA integrity_check` ok, no WAL left behind |
| `docker compose` | `config` ok; `up -d` of the bot service healthy, `stop` exit 0, `down -v` clean. **`no-new-privileges:true` fails on the tested snap-Docker host for every image**, so compose was run with `security_opt` reset (docs/DEPLOY.md) |
| systemd units | `systemd-analyze verify` plus `systemd-run --user` transient units with the supported sandbox properties: bot ran, health ok, `systemctl stop` graceful; recorder ran. This found a real bug (`SystemCallFilter=~@resources` killed numpy/pandas with SIGSYS), fixed. The system units were not installed |
| Offline commands in README/DEMO | `demo`, `bot --mode mock --replay-from data/history` (strict and demo), `guardian --demo-db` re-run and match the docs |
| Live shadow runs on the public API | 12 min demo profile and 2 h strict profile: 0 guardian failures, kill switch never engaged, healthcheck ok. Neither produced a signal (quiet market / strict gate), so **no live fill path was exercised** |

## How to resume

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]' && .venv/bin/pytest -q   # a plain `pip install .` works too
.venv/bin/python -m wallexbot demo
.venv/bin/python -m wallexbot bot --db /tmp/shadow.db --mode shadow --balance 1000     # public GETs only
.venv/bin/python deploy/healthcheck.py --db /tmp/shadow.db
```

Deployment (Docker, systemd, healthcheck, backup): [DEPLOY.md](DEPLOY.md). Module map and invariants:
[ARCHITECTURE.md](ARCHITECTURE.md). Limits and failure behaviour: [RISK.md](RISK.md).

## Data on disk (git-ignored, not in the repo)

- `data/history/`: 1m candles, 45 days, 7 USDT symbols (BTC ETH XRP XAUT USOON PEPE SOL) plus PUMP (15 days only).
  The 4 lowest-ranked symbols of the top 12 were never fetched. `MANIFEST.json` describes it. Exchange-wide gaps exist.
- `data/recorder/`: order-book and trade snapshots. **A recorder process was still running when this was written**
  (started from a throwaway script outside the repo; it stops by itself after 1440 cycles of 15 s). Recorders are now
  started with `python -m wallexbot record` (DEMO.md). The cost calibration used only about 1.2 hours of it (sessions had gaps).

## Decisions taken with the user at the start

New clean repo; USDT quote, spot, long only; AI advisory-only (veto/shrink, default off); risk-gated auto-approval
instead of per-order human approval; no ICT strategy (the code was not available). Live arming requires
`WALLEX_ARM_LIVE`, a CLI flag, a trade key, a green `doctor` and an explicit user go-ahead; none of it is wired to a command.

## Known limitations and residual risks

1. **Our fee is unknown.** The config default is now 0.2% maker/taker (`fees.*_rate` 0.002, formerly a 0.35%
   planning placeholder); `/v1/account/fee` needs an API key (`doctor` reads it). Public pages
   (retrieved 2026-10-22, [COSTS.md](COSTS.md)) say 0.2% flat for USDT pairs (undated help page) but also that spot fees
   are tiered by 30-day volume since 2025-08, with 0.1% as an offer or top tier; the tier table is login-gated. "No edge"
   is sensitive to it: the reports' 0.2% and 0.1% scenarios match these public figures but are not our verified rate;
   the realistic scenario in the v2 reports is now the 0.002 column ([reports/README.md](../reports/README.md)).
2. **Cost model is low-confidence**: about 1.2 h of books, p90 charged on every fill, candle volume unreliable
   (71-91% of 1m bars are forward-filled zeros).
3. **`WallexBroker` is checked against Wallex's public docs and Swagger only** (2026-10-22); no private request has ever
   been made. Paths, headers, envelope, create body (`stop_Price`), order and fill objects, 404 for an unknown order,
   paging parameters and balance/fee rows match the docs; two mismatches were fixed (responses spell the stop price
   `stopPrice`; every request needs `Content-Type: application/json`). Still unverified: the status enum, the openOrders
   list key, "balance value is the total", the HTTP status of a duplicate client_id / insufficient balance / rate limit,
   minimum order size ([WALLEX_INTEGRATION.md](WALLEX_INTEGRATION.md)). A same-host 301-303 redirect after a POST becomes
   a GET that still carries the key header (low severity: pinned host, TLS).
4. **ShadowBroker fill model is an assumption set**, not validated against real fills; it reads a cached book up to 60 s old.
5. **Statistical power is tiny**: 45 days, few trades (many cells n < 30). The test split is reserved and unused.
6. **Kill switch semantics**: "latest" means highest row id, so someone with raw write access to the SQLite file can defeat
   it. Soft breaches stay engaged until an operator clears them; only the guardian's own `auto:heartbeat_lost` row is
   auto-released (compare-and-set, 3 clear ticks and 60 s, wall and monotonic).
7. **One guardian per database.** The standalone `guardian` command is a demo only and refuses a bot's database.
   A separate guardian process is meaningful only against a live exchange account.
8. **The AST single-order-path guard is a lint, not a proof**; its known misses are listed in ARCHITECTURE.md.
9. **Redaction** misses a few exotic encodings (base64 of UTF-16, JSON `\u` escapes inside strings). Keep secrets in `Secret`.
10. ~~Packaging~~ **Fixed**: the default config is package data (`src/wallexbot/default_config.yaml`), so a plain
    `pip install .` works; `WALLEXBOT_CONFIG=/path.yaml` overrides it (docs/CONFIG_ENV.md). `config.py` locates the file
    next to the module instead of using `importlib.resources`, because the single-order-path AST guard bans `importlib`
    in `src/` (the guard was left untouched). Zip-imported installs are therefore not supported (pip never makes them).
11. **Docker/systemd verified on one host only** (see the quality-gates table). Docker Hub is blocked on some networks:
    build with `--build-arg BASE_IMAGE=<mirror>/library/python:3.12.11-slim-bookworm` (compose: `BASE_IMAGE=...`).
    `no-new-privileges` could not be tested here (it breaks every container on the snap-Docker host); the setting stays in
    the shipped compose file and DEPLOY.md documents the override. A bare container (no `init`) ignores a SIGTERM that
    arrives before Python installs its handler (about the first 1-2 s: 137 after the grace period); compose sets
    `init: true`, plain `docker run` needs `--init`. The system-level systemd units were never installed.
12. **Universe oddities**: XAUT and USOON (non-crypto tokens) rank high by volume; their `SymbolRules` are derived
    conservatively, not read from the exchange. 15m bars with missing minutes are flagged, not repaired.
13. `backtest/runner.py` still wraps exits in `TickExit`, a workaround from before the engine rounded stops itself.
    It is redundant but harmless; remove it only together with a re-run of the grid.
14. Some commit trailers say `Claude Opus 5.5` (written by sub-agents); the others say `Claude Sonnet 5.5`.
15. ~~No CLI for the recorder and history downloader~~ **Fixed**: `python -m wallexbot record` and `history`
    (public GETs, host pinned, outside any order path, tested with fake clients and run in the image against the real
    API). `deploy/run_recorder.py` was removed; compose and systemd call the CLI.

## Not done

Any validated backtest; the one-time test-set evaluation (`backtest --final-test` exists, refuses without frozen
params, and has never been consumed); a 24-48 h shadow run (the promotion criterion in BACKTEST.md); any check of the
private client against the live API (only the public docs were compared); any live run; the official competition rules
(login-gated, never read).

## Recommended next steps (in order)

1. Read the official rules and learn the real fee tier (maker/taker). Re-run the grid with the real fee before any
   strategy work; if round-trip cost stays above the expected move, the honest product is "abstain".
2. Let the recorder run for days, re-calibrate (`docs/COSTS.md`), re-run `python -m wallexbot backtest`.
3. Run a 24-48 h shadow deployment on a host (DEPLOY.md) and evaluate the criteria in BACKTEST.md. Consider
   `--profile demo` only to exercise the pipeline, never as evidence.
4. If a strategy variant is worth testing (other horizon, maker-only entries, other markets), pre-register it, pick it
   on train/validation only, freeze parameters, then spend the test split exactly once.
5. The Swagger check is done (WALLEX_INTEGRATION.md). Next run `doctor` with a **read-only** key to confirm the real
   response shapes, then, only with explicit owner approval, a tiny-size live test. Never enable Withdraw.
6. Housekeeping: decide on a type-checking policy (113 mypy findings).

## Verification history (why the safety claims are believable)

Four independent adversarial verification rounds ran against the merged code. They found and caused fixes for, among
others: look-ahead on the entry bar and via private view attributes; an engine crash under default costs; SQLite
`REPLACE` bypassing the append-only triggers; non-exact P&L; equal-valued quantities producing two client ids; the
bot/guardian double-SELL race; a kill-switch latch that could stop an unattended run forever; a stuck PROPOSED exit
after a database lock; an auto-release race; and a held position with a dead feed going unnoticed. Each has a
regression test. Residual items they reported are the list above.
