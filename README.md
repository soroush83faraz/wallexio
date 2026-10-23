# wallexbot

A long-only, spot momentum-breakout trading agent for the Wallex exchange, quoted in USDT.

**Status: the pieces are wired end to end in `mock` (paper, offline replay possible) and `shadow` (live public order book, simulated fills, no orders sent) modes: `python -m wallexbot demo|backtest|doctor|guardian|bot`. The bot runs its guardian in-process. Everything is tested against the mock, synthetic or recorded data only. Live trading does not exist in this build.**

This is a hackathon submission. It does not promise profit. "No trades" is a valid outcome of the strategy, and no validated backtest result is reported here. The exploratory grids under `reports/` (see [reports/README.md](reports/README.md)) are smoke tests of the tooling, not evidence.

## What it is

- Spot only, long only, USDT quote currency, top 12 markets by 24h volume (config-driven).
- A single order path: every order goes through the OMS, which calls the broker. Nothing else submits orders.
- Fail-closed risk engine: any failed check blocks the order. A kill switch blocks new entries but not reduce-only exits.
- An optional AI advisor (`ai/`, `advisor_bridge.py`) that can only veto or size down. It is off by default; `bot --advisor shadow` records its verdicts without acting on them (`apply` is never enabled). Its calls to the Anthropic API are tested against a fake transport only.
- A guardian that reads the broker state and flattens positions on a hard breach (`guardian/`; besides the bot's own repeated internal-failure check (an exchange outage is "degraded", never a failure), the only component that engages the kill switch automatically, and it releases only its own transient heartbeat engagement, docs/RISK.md). In this build it runs **inside the bot process**, on its own database connection and the bot's own paper/shadow broker (`--no-guardian` disables it). A separate guardian process only makes sense against a live exchange account, which does not exist here.
- An append-only SQLite audit store (implemented in `store.py`).

## What exists now

Implemented and unit-tested (against the mock or synthetic data; nothing has run against the real exchange):

- Public Wallex data client, unauthenticated GET only, host pinned (`exchange/wallex_public.py`); candle aggregation, cache, quality checks and a market-data recorder (`data/`).
- Resumable historical 1m candle downloader (`data/history.py`), writing `data/history/<SYMBOL>/<SYMBOL>_1m_<YYYY-MM>.csv` (git-ignored), run by `python -m wallexbot history --out DIR --symbols A,B | --top-n 12 --days 45 --max-requests 1500`. `data/recorder.py::Recorder` appends depth and trades to `data/recorder/*.jsonl` (git-ignored), run by `python -m wallexbot record --out DIR --symbols A,B | --top-n 12 --interval-s 15 [--max-cycles N]` (SIGTERM stops after the current cycle). Both are public GETs only, host-pinned and outside any order path (docs/DEMO.md).
- Strategy: S1 momentum-breakout entry engine, S0 trend baseline, cooldown, risk-based sizing and exit policies E1-E4 (`strategy/`). Parameter defaults are unvalidated placeholders ([docs/STRATEGY.md](docs/STRATEGY.md)).
- Backtest engine with cost model, metrics, chronological splits with a single-use test-set guard, sensitivity analysis and a report writer, plus the runner behind `python -m wallexbot backtest` (`backtest/`). Execution costs are calibrated from a small sample of recorded order books ([docs/COSTS.md](docs/COSTS.md), low confidence).
- Append-only SQLite store (`store.py`), fail-closed risk engine and kill switch (`risk/`), the OMS, the only caller of `broker.submit_order` (`oms.py`). The risk decision and the intent's state change commit in one transaction (or not at all), and a SELL still being proposed counts as committed, so the bot's exit and the guardian's flatten never both sell one position; an intent left proposed by a store error (a failing COMMIT included) is resumed on the next poll (docs/RISK.md).
- Guardian: monitor and hard-breach flatten through the OMS (`guardian/`), run in-process by `bot`. A held position without a fresh quote for > 120 s is held (default) or flattened (`--stale-action hold|flatten`) and re-alerted every 5 min either way; the healthcheck fails while it lasts. `python -m wallexbot guardian --db NEW.db --mode mock --demo-db` is a standalone DEMO on its own mock broker; it refuses a bot's database.
- Bot loop (`bot/`): S1 entries, E4 exits, risk-based sizing, every order through `pipeline.route_to_oms` -> `OMS.submit`; state is persisted and restored after a restart. `python -m wallexbot bot ...` (mock or shadow only). `--profile demo` is an **EXERCISE profile** (relaxed thresholds, `strategy/profiles.py`): with realistic costs strict S1 almost never trades (`reports/v2_summary.md`), so demo exists only to show the full lifecycle in mock/shadow. No edge is claimed; it prints a banner, is written into every bot audit event and the healthcheck, and is refused on any live path.
- `ShadowBroker` (`exchange/shadow.py`): simulates fills from the cached public depth with conservative assumptions; local JSONL journal. Its fill model is an assumption set, not validated against real fills.
- `MockWallex`, an in-memory exchange simulator with no network (`exchange/mock_wallex.py`).
- `WallexBroker` private client and a read-only `doctor` module (`exchange/wallex_private.py`, `doctor.py`). **UNVERIFIED against the real API**: its endpoint, field and status assumptions are listed in [docs/WALLEX_INTEGRATION.md](docs/WALLEX_INTEGRATION.md). It cannot send or cancel orders unless live arming succeeded.
- Redaction, auth and host guards, live-arming checks (`redact.py`, `exchange/auth.py`, `config.py`).
- A scripted mock round trip: `python -m wallexbot demo` (see [docs/DEMO.md](docs/DEMO.md)).

**Not done**: any validated backtest, the single test-set evaluation, a 24-48 h shadow run, any check of the private client against Wallex, any live run. Live is refused: `--arm-live` and `bot --mode live` both exit with an error.

## Quickstart

Requires Python 3.12. No credentials are needed or used.

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]' && .venv/bin/pytest -q
```

Verified on the code at this commit: `pytest -q` reports 2031 passed.

Run it (all offline except `bot --mode shadow`, which makes public GET requests only):

```bash
.venv/bin/python -m wallexbot demo                                   # scripted mock round trip
.venv/bin/python -m wallexbot backtest                               # exploratory grid -> reports/ (train+validation only)
.venv/bin/python -m wallexbot bot --db /tmp/bot.db --mode mock --replay-from data/history --max-ticks 400
.venv/bin/python -m wallexbot bot --db /tmp/demo.db --mode mock --replay-from data/history --profile demo  # EXERCISE profile
.venv/bin/python -m wallexbot bot --db /tmp/shadow.db --mode shadow [--balance 1000] [--advisor shadow]
.venv/bin/python -m wallexbot guardian --db /tmp/g_demo.db --mode mock --demo-db --ticks 3 --interval 1   # demo only
```

## Gold (wallexio)

A research add-on asks one question: can this repo make about 3 % per month on the Wallex gold tokens (PAXGUSDT / XAUTUSDT)? One long-only trend strategy (daily EMA-50 filter, 20-day breakout, ATR trailing stop, the repo's 3 % risk sizing and risk caps) was written down in [gold/README.md](gold/README.md) **before** any run, then backtested on Binance PAXGUSDT 1h data (committed under `gold/data/`, evaluation 2026-04-01 to 2026-10-23) with the repo's engine and cost model, and replayed through the real bot / OMS / risk engine / ShadowBroker path.

**Result: the target was not met.** The pre-registered strategy returned +0.32 % in total (about +0.05 % per month, 2 trades, max drawdown 1.55 %) while PAXG fell about 14 %; no parameter neighbour or stress variant came close to 3 % per month. Wallex PAXGUSDT itself is thin and wide (about 42 bps half-spread in our snapshots, about 1,500 USDT daily volume); XAUTUSDT is the liquid gold market on Wallex. Full numbers, every variant, buy-and-hold and caveats: [gold/REPORT.md](gold/REPORT.md); shadow replay vs backtest: [gold/SHADOW.md](gold/SHADOW.md). As everywhere in this repo: no profit promise, and a backtest is not live trading.

```bash
.venv/bin/python -m wallexbot gold-backtest     # offline, deterministic: gold/report.json + gold/REPORT.md
.venv/bin/python -m wallexbot gold-shadow       # offline replay through the bot path: runs/ (audit DB, journal) + gold/SHADOW.md
.venv/bin/python -m wallexbot gold-shadow --live --max-ticks 5   # Wallex public book, public GETs only, logs would-be orders
```

## Documentation

- [docs/HANDOFF.md](docs/HANDOFF.md): **start here when taking over**: state, quality gates, known limitations, what is not done, recommended next steps.
- [docs/STRATEGY.md](docs/STRATEGY.md): S1 entry rules, S0 baseline, sizing, exits E1–E4 as implemented (placeholder parameters), the demo profile.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): modules, single-order-path rule, in-process guardian, AI boundary.
- [docs/RISK.md](docs/RISK.md): limits, soft and hard breaches, guardian and flatten procedure, exit race, dust, clock steps, failure behaviour.
- [docs/COSTS.md](docs/COSTS.md): execution-cost calibration from recorded order books and its limits.
- [docs/DEPLOY.md](docs/DEPLOY.md): shadow deployment (Docker, systemd), healthcheck, backups, what not to do.
- [docs/WALLEX_INTEGRATION.md](docs/WALLEX_INTEGRATION.md): Wallex API facts, observed vs unverified, private-client assumptions, API key rules.
- [docs/BACKTEST.md](docs/BACKTEST.md): engine assumptions, history downloader, splits, test-set rule, promotion criteria, limitations.
- [docs/CONFIG_ENV.md](docs/CONFIG_ENV.md): environment variable names, config keys, live arming conditions.
- [docs/DEMO.md](docs/DEMO.md): five-minute demo outline and the exact commands.
- [reports/README.md](reports/README.md): what each report file is, which ones are superseded.

## Safety defaults

- `mode: mock` in `src/wallexbot/default_config.yaml`. Live mode is rejected unless `WALLEX_ARM_LIVE=YES_I_UNDERSTAND`, a `WALLEX_API_KEY_TRADE` and `cli_armed=True` in code are all present. The CLI refuses `--arm-live` and never passes `cli_armed`, so no command can reach live.
- Host allow-list: only `api.wallex.ir`.
- Secrets live in a local `.env` (git-ignored). Only variable names are committed, in `.env.example`.
- Wallex keys, when used, are read-only where possible. No key may hold withdrawal rights.
