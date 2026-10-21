# Demo outline (about 5 minutes)

Status: **planned.** The demo replays one **stored** decision lifecycle from the audit database. It does not place orders and needs no credentials. Items marked *works now* run at commit `0e7ee8e`. Items marked *PLANNED* do not exist yet, and their command names are placeholders until `cli.py` is written.

## Setup (before the demo)

- Run the quickstart from README.md.
- Have one stored run in the audit DB (planned: produced by the scripted mock trade).

## Script

| Time | Segment | What to show | Status |
|---|---|---|---|
| 0:00–0:30 | Problem and status | Long-only momentum agent for Wallex, USDT, mock mode. Live trading disabled by default. No profit claim. | Talk |
| 0:30–1:15 | Architecture | The module diagram in ARCHITECTURE.md. Point out that only `OMS.submit()` calls the broker. | Talk |
| 1:15–1:45 | Safety defaults | Load the default config. Then show live refusal: `load_config(env={})` with `mode: live` raises `ConfigError`. | *works now* (the same check is in `tests/test_skeleton.py::test_live_requires_arming`) |
| 1:45–3:30 | Replay one decision | Print the stored lifecycle for one scripted trade: signal, cost gate, each risk check, OMS states (`proposed` → `risk_approved` → `submitted` → `filled`), the mock fill with fee in the fee asset, and the resulting P&L row. | *PLANNED*: `.venv/bin/python -m wallexbot.cli replay --run <id>` (name is a placeholder) |
| 3:30–4:15 | Failure handling | Run the fault-injection tests: timeout after accept, duplicate client id, partial fill, reject, stale feed. Show zero duplicate orders and a defined end state for each. | *PLANNED*: `.venv/bin/pytest -q -k fault` (name is a placeholder) |
| 4:15–5:00 | Limits and next steps | Show the risk limits table (RISK.md). State what is missing: backtest results, shadow run, unverified Wallex facts (WALLEX_INTEGRATION.md). | Talk |

## Works now, in one command

```bash
.venv/bin/pytest -q
```

Expected at this commit: 4 tests pass.

## Lines to say

- "No trades is a valid outcome."
- "Nothing here is a profit claim. No backtest has been run."
- "The AI can only veto or shrink an order. It cannot place one."
- "Every Wallex fact marked unverified is an assumption until a fixture confirms it."

## Do not show

- Any `.env` file or key value.
- Any claim of backtest returns.
- A live order, or any live-arming step.
