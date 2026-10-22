# Demo outline (about 5 minutes)

Status: **runnable.** `python -m wallexbot` has `demo`, `backtest`, `doctor`, `guardian` and `bot`. Backtest reports with recorded per-symbol costs exist (`reports/v2_*`, summary `reports/v2_summary.md`, verdict in `reports/README.md`): exploratory, train and validation only, low-confidence costs, and they show **no net edge**. The test segment is not evaluated; `reports/backtest_grid.md` is the older pre-fix run, superseded. There is still no shadow run of 24-48 h and no real-exchange check. The *PLANNED* item below does not exist yet.

## Setup (before the demo)

- Run the quickstart from README.md.
- Have one stored run in the audit DB: `python -m wallexbot demo --db /tmp/demo.db` writes one to a file. The default `:memory:` keeps nothing after the run.

## Script

| Time | Segment | What to show | Status |
|---|---|---|---|
| 0:00–0:30 | Problem and status | Long-only momentum agent for Wallex, USDT, mock mode. Live trading disabled by default. No profit claim. | Talk |
| 0:30–1:15 | Architecture | The module diagram in ARCHITECTURE.md. Point out that only `OMS.submit()` calls the broker. | Talk |
| 1:15–1:45 | Safety defaults | Load the default config. Then show live refusal: `load_config(env={})` with `mode: live` raises `ConfigError`. | *works now* (the same check is in `tests/test_skeleton.py::test_live_requires_arming`) |
| 1:45–3:30 | Replay one decision | Print the stored lifecycle for one scripted trade: signal, cost gate, each risk check, OMS states (`proposed` → `risk_approved` → `submitted` → `filled`), the mock fill with fee in the fee asset, and the resulting P&L row. | *PLANNED*: a `replay --run <id>` command. *Works now* in raw form: `python -m wallexbot demo --db /tmp/demo.db`, then read the audit tables, or run the bot replay below and read `decision_log` (`bot/state.py`). |
| 3:30–4:15 | Failure handling | Run the fault-injection tests: timeout after accept, duplicate client id, partial fill, reject, stale feed. Show zero duplicate orders and a defined end state for each. | *Works now*: `.venv/bin/pytest -q tests/test_oms.py tests/test_mock_wallex.py tests/test_risk.py`. The tests exist in `tests/test_oms.py`, `tests/test_mock_wallex.py` and `tests/test_risk.py`. |
| 4:15–5:00 | Limits and next steps | Show the risk limits table (RISK.md). State what is missing: a validated backtest (the v2 grid shows no edge), the test-segment evaluation, a shadow run, unverified Wallex facts (WALLEX_INTEGRATION.md). | Talk |

## Works now, in one command

```bash
.venv/bin/pytest -q
```

Expected on the code at this commit: 1924 tests pass.

## Works now: the CLI (all offline unless noted)

```bash
.venv/bin/python -m wallexbot --help          # demo, backtest, doctor, guardian, bot
.venv/bin/python -m wallexbot demo [--db /tmp/demo.db]
```

`demo` runs one BUY and one reduce-only SELL of BTCUSDT against `MockWallex` with no network and prints the states, the realized P&L and the audit row counts.

```bash
.venv/bin/python -m wallexbot backtest [--history-dir data/history] [--out-dir reports]
```

Exploratory grid on train and validation only; the test segment is not evaluated. Needs the downloaded history; runs for minutes.

```bash
# bot, paper, offline: replays recorded 1m candles under an accelerated clock (60 simulated s per tick)
.venv/bin/python -m wallexbot bot --db /tmp/bot.db --mode mock --replay-from data/history --max-ticks 400
# bot, shadow: public GETs to api.wallex.ir only, simulated fills against the live book, no orders sent
.venv/bin/python -m wallexbot bot --db /tmp/shadow.db --mode shadow --balance 1000 --interval-s 12
# optional log-only AI verdicts (needs ANTHROPIC_API_KEY, else it warns and runs without calls)
.venv/bin/python -m wallexbot bot --db /tmp/shadow.db --mode shadow --advisor shadow
# EXERCISE profile (relaxed thresholds, no edge claimed, mock/shadow only): shows a full lifecycle
.venv/bin/python -m wallexbot bot --db /tmp/demo_bot.db --mode mock --replay-from data/history --profile demo --max-ticks 4000
# standalone guardian DEMO on its own mock broker and a NEW database (it refuses a bot's database)
.venv/bin/python -m wallexbot guardian --db /tmp/g_demo.db --mode mock --demo-db --ticks 3 --interval 1
```

What the two replays produced (with the former 0.0035 fee default; at the current 0.002 default the demo run's fees and realized P&L differ and were not re-measured; strict S1 abstaining on the synthetic replay is checked by `tests/test_profiles.py`) on the local `data/history` (8 symbols, 2026-09-06 to 2026-10-21; PUMPUSDT data starts only in October) at this commit, each on a new database (the replay clock starts after a 120-bar warm-up, 2026-09-08 03:40):

- strict, 400 ticks (about 6.7 simulated hours): 0 signals, 0 orders, kill switch not engaged. S1 abstains (`no_signal`); "no trades" is a valid outcome.
- demo, 4000 ticks (about 2.8 simulated days, to 2026-09-10 22:19; under a minute of wall time): 1 signal (USOONUSDT), one LIMIT BUY and one LIMIT SELL, both filled; the position was closed after 45 minutes by the exit policy (`momentum_fail_roc`) at a realized loss of about 2.37 USDT (fee dust of 7.4e-7 USOON stays in `open_positions`). Kill switch never engaged, no guardian errors, 4000 guardian ticks, 0 failed. A symbol without data yet (PUMPUSDT) does not stop the run: only symbols held or with a live order are checked for feed staleness.

`--interval-s` is simulated seconds per tick in replay (up to 900). Without `--replay-from` it is wall-clock seconds and must be at most `max_data_age_s / 2` (15 s), else the bot exits 2, because market data refreshes only on bot ticks. The shadow journal is written next to the database (`shadow_journal.jsonl`, once an order exists). `bot --mode live` and `--arm-live` are refused. The bot runs the guardian in-process on its own broker (`--no-guardian` disables it); the standalone `guardian` command is only a demo on its own mock broker.

## Works now: market-data download and recorder (public GETs, need network)

```bash
python -m wallexbot history --out data/history --top-n 12 --days 45 --max-requests 1500   # resumable: re-run to continue
python -m wallexbot record  --out data/recorder --symbols BTCUSDT,ETHUSDT --interval-s 15 # until SIGTERM/Ctrl-C (or --max-cycles N)
```

`history` downloads 1m candles to `data/history/<SYMBOL>/` (newest first; re-runs fetch only what is missing; exit 1 = stopped early, run it again). `record` appends depth and trades to `data/recorder/depth_<YYYYMMDD>.jsonl` and `trades_<YYYYMMDD>.jsonl` and, on SIGTERM, finishes the current cycle and exits 0. `--symbols` takes commas and/or spaces; `--top-n N` picks the N highest 24h-volume USDT markets (one public GET); with neither, the 7-symbol backtest universe is used. Both are public GETs to `api.wallex.ir` only and have no order path. Do not make network calls during a live demo unless intended.

## Lines to say

- "No trades is a valid outcome."
- "Nothing here is a profit claim. The one backtest grid is exploratory and must be re-run."
- "The AI can only veto or shrink an order. It cannot place one."
- "Every Wallex fact marked unverified is an assumption until a fixture confirms it."

## Do not show

- Any `.env` file or key value.
- Any claim of backtest returns (the grid in `reports/` is exploratory only).
- A live order, or any live-arming step.
