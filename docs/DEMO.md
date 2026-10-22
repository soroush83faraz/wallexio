# Demo outline (about 5 minutes)

Status: **runnable.** `python -m wallexbot` has `demo`, `backtest`, `doctor`, `guardian` and `bot`. There is still no validated backtest, no shadow run and no real-exchange check, so there are no results to show; `reports/backtest_grid.md` is exploratory, placeholder costs, pre-fix engine; to be re-run. The *PLANNED* item below does not exist yet.

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
| 4:15–5:00 | Limits and next steps | Show the risk limits table (RISK.md). State what is missing: backtest results, shadow run, unverified Wallex facts (WALLEX_INTEGRATION.md). | Talk |

## Works now, in one command

```bash
.venv/bin/pytest -q
```

Expected on the code at this commit: 1744 tests pass.

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
# guardian (separate process, same DB)
.venv/bin/python -m wallexbot guardian --db /tmp/shadow.db --mode shadow --ticks 3 --interval 1
```

The shadow journal is written next to the database (`shadow_journal.jsonl`, once an order exists). The first replay ticks may place no order: "no trades" is a valid outcome. `bot --mode live` and `--arm-live` are refused. In mock/replay runs the paper broker lives inside the bot process, so a separate guardian watches its own demo broker; the ledger, heartbeat and kill switch are shared through the database.

## Works now: market-data download and recorder (public GETs, need network)

`wallexbot.data.history.fetch_history` downloads 1m candles to `data/history/<SYMBOL>/` (resumable). `wallexbot.data.recorder.Recorder` appends depth and trades to `data/recorder/depth_<YYYYMMDD>.jsonl` and `trades_<YYYYMMDD>.jsonl`:

```python
from wallexbot.exchange.wallex_public import WallexPublicClient
from wallexbot.data.recorder import Recorder
Recorder(WallexPublicClient(), ["BTCUSDT", "ETHUSDT"], "data/recorder", interval_s=15).run_forever(handle_signals=True)
```

Neither has a CLI. Do not make network calls during a live demo unless intended.

## Lines to say

- "No trades is a valid outcome."
- "Nothing here is a profit claim. The one backtest grid is exploratory and must be re-run."
- "The AI can only veto or shrink an order. It cannot place one."
- "Every Wallex fact marked unverified is an assumption until a fixture confirms it."

## Do not show

- Any `.env` file or key value.
- Any claim of backtest returns (the grid in `reports/` is exploratory only).
- A live order, or any live-arming step.
