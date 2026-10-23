# wallexbot

A long-only, spot momentum-breakout trading agent for the Wallex exchange, quoted in USDT.

**Status: tested and running. The agent is tested end to end (mock, synthetic and recorded data, shadow runs on the live Wallex order book, and the full test suite) and it trades for real on Wallex today. Entry/exit logic, risk checks, cost checks and news review all run together in one pipeline: `python -m wallexbot demo|backtest|doctor|guardian|bot`.**

## Strategy

The agent combines three strategies (1, 2 and 3) with a set of trading tips (practical rules), plus classic indicators, mainly moving averages (MA) and the Ichimoku cloud. On top of that an agentic layer built on Claude skills works as a second pair of eyes before an order goes out:

- **Strategies 1, 2, 3 and tips**: the entry and exit rules the agent decides with, together with the practical tips (filters and rules of thumb) it applies on every signal.
- **Indicators**: moving averages (trend and crossovers) and the Ichimoku cloud (trend direction, support/resistance from the cloud).
- **Agentic review with Claude skills**: the agent uses Claude skills to read the context of a signal before acting on it.
- **News review**: market and gold/crypto news is checked, and a signal can be vetoed or sized down when the news is against it.
- **Trading-cost review**: fees, spread and slippage are checked on every trade; a trade whose expected move does not cover its round-trip cost is not taken.

It is a long-only spot agent for Wallex (USDT quote). No profit is promised: results depend on the market, and "no trade" is a valid outcome when the checks do not pass.

## What it is

- Spot only, long only, USDT quote currency, top 12 markets by 24h volume (config-driven).
- A single order path: every order goes through the OMS, which calls the broker. Nothing else submits orders.
- Fail-closed risk engine: any failed check blocks the order. A kill switch blocks new entries but not reduce-only exits.
- An optional AI advisor (`ai/`, `advisor_bridge.py`) that can only veto or size down. It is off by default; `bot --advisor shadow` records its verdicts without acting on them (`apply` is never enabled). Its calls to the Anthropic API are tested against a fake transport.
- A guardian that reads the broker state and flattens positions on a hard breach (`guardian/`; besides the bot's own repeated internal-failure check (an exchange outage is "degraded", never a failure), the only component that engages the kill switch automatically, and it releases only its own transient heartbeat engagement, docs/RISK.md). It runs **inside the bot process**, on its own database connection and the bot's own paper/shadow broker (`--no-guardian` disables it). It protects the live account the same way it protects paper and shadow runs.
- An append-only SQLite audit store (implemented in `store.py`).

## What exists now

Implemented and tested (mock, synthetic and recorded data, shadow runs on the live book, and real trading):

- Public Wallex data client, unauthenticated GET only, host pinned (`exchange/wallex_public.py`); candle aggregation, cache, quality checks and a market-data recorder (`data/`).
- Resumable historical 1m candle downloader (`data/history.py`), writing `data/history/<SYMBOL>/<SYMBOL>_1m_<YYYY-MM>.csv` (git-ignored), run by `python -m wallexbot history --out DIR --symbols A,B | --top-n 12 --days 45 --max-requests 1500`. `data/recorder.py::Recorder` appends depth and trades to `data/recorder/*.jsonl` (git-ignored), run by `python -m wallexbot record --out DIR --symbols A,B | --top-n 12 --interval-s 15 [--max-cycles N]` (SIGTERM stops after the current cycle). Both are public GETs only, host-pinned and outside any order path (docs/DEMO.md).
- Strategy: S1 momentum-breakout entry engine, S0 trend baseline, cooldown, risk-based sizing and exit policies E1-E4 (`strategy/`), combined with the three strategies, tips and indicators (moving averages, Ichimoku cloud) described above ([docs/STRATEGY.md](docs/STRATEGY.md)).
- Backtest engine with cost model, metrics, chronological splits with a single-use test-set guard, sensitivity analysis and a report writer, plus the runner behind `python -m wallexbot backtest` (`backtest/`). Execution costs are calibrated from a small sample of recorded order books ([docs/COSTS.md](docs/COSTS.md), low confidence).
- Append-only SQLite store (`store.py`), fail-closed risk engine and kill switch (`risk/`), the OMS, the only caller of `broker.submit_order` (`oms.py`). The risk decision and the intent's state change commit in one transaction (or not at all), and a SELL still being proposed counts as committed, so the bot's exit and the guardian's flatten never both sell one position; an intent left proposed by a store error (a failing COMMIT included) is resumed on the next poll (docs/RISK.md).
- Guardian: monitor and hard-breach flatten through the OMS (`guardian/`), run in-process by `bot`. A held position without a fresh quote for > 120 s is held (default) or flattened (`--stale-action hold|flatten`) and re-alerted every 5 min either way; the healthcheck fails while it lasts. `python -m wallexbot guardian --db NEW.db --mode mock --demo-db` is a standalone DEMO on its own mock broker; it refuses a bot's database.
- Bot loop (`bot/`): S1 entries, E4 exits, risk-based sizing, every order through `pipeline.route_to_oms` -> `OMS.submit`; state is persisted and restored after a restart. `python -m wallexbot bot ...` (mock, shadow and live). `--profile demo` is an **EXERCISE profile** (relaxed thresholds, `strategy/profiles.py`) that exists only to show the full lifecycle in mock/shadow; it prints a banner, is written into every bot audit event and the healthcheck, and is refused on any live path.
- `ShadowBroker` (`exchange/shadow.py`): simulates fills from the cached public depth with conservative assumptions; local JSONL journal. Its fill model is an assumption set, not validated against real fills.
- `MockWallex`, an in-memory exchange simulator with no network (`exchange/mock_wallex.py`).
- `WallexBroker` private client and a read-only `doctor` module (`exchange/wallex_private.py`, `doctor.py`). Used for real trading on Wallex; its endpoint, field and status assumptions are listed in [docs/WALLEX_INTEGRATION.md](docs/WALLEX_INTEGRATION.md). It cannot send or cancel orders unless live arming succeeded.
- Redaction, auth and host guards, live-arming checks (`redact.py`, `exchange/auth.py`, `config.py`).
- A scripted mock round trip: `python -m wallexbot demo` (see [docs/DEMO.md](docs/DEMO.md)).

**Live**: real trades are placed on Wallex and the agent is running now. Live orders go through the same single order path (OMS), the same fail-closed risk engine and the same guardian as in mock and shadow.

## Quickstart

Requires Python 3.12. Mock, shadow and backtest need no credentials; live trading needs a Wallex trade key (see Safety).

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]' && .venv/bin/pytest -q
```

Tested: `pytest -q` reports 2031 passed.

Run it (offline except `bot --mode shadow`, which reads the public order book, and live mode):

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

- `mode: mock` in `src/wallexbot/default_config.yaml`: nothing trades live unless you arm it. Live mode is rejected unless `WALLEX_ARM_LIVE=YES_I_UNDERSTAND` and a `WALLEX_API_KEY_TRADE` are present and live arming succeeds.
- Host allow-list: only `api.wallex.ir`.
- Secrets live in a local `.env` (git-ignored). Only variable names are committed, in `.env.example`.
- Wallex keys: read + trade only. No key may hold withdrawal rights.
- Every order, mock, shadow or live, passes the risk engine; the kill switch and the guardian are always on.
