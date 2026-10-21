# Architecture

Status: Day-1 skeleton. Modules marked *planned* do not exist at commit `0e7ee8e`.

## Module diagram

```
            +-------------------+       +------------------+
            | exchange/         |       | data/            |
            |  wallex_public    |<------|  candles, aggr,  |
            |  (GET only, host  |       |  quality,        |
            |   pinned)         |       |  recorder        |
            |  mock_wallex      |       +--------+---------+
            |  ratelimit        |                |
            +---------+---------+                v
                      ^                 +--------+---------+
                      |                 | strategy/        |
                      |                 |  indicators,     |
                      |                 |  features,       |
                      |                 |  momentum,       |
                      |                 |  regime,         |
                      |                 |  costgate, exits |
                      |                 +--------+---------+
                      |                          | OrderIntent
                      |                          v
   +------------------+-----+           +--------+---------+        +------------------+
   | OMS (oms.py)           |<----------| risk/engine.py   |<-------| ai/ (advisory,   |
   |  submit() is the ONLY  | approved  |  ordered fail-   | veto / | veto or size-    |
   |  caller of             |           |  closed checks   | size-  | down only)       |
   |  broker.submit_order   |           | risk/killswitch  | down   |                  |
   +------------------+-----+           +--------+---------+        +------------------+
                      |                          ^
                      v                          | reads audit store
            +---------+---------+        +-------+----------+
            | broker (mock now; |        | store.py         |
            | shadow, live      |        |  SQLite WAL,     |
            | planned)          |        |  append-only,    |
            +-------------------+        |  redacted writes |
                                         +-------+----------+
                                                 ^
                                                 | reads / writes
                       +-------------------------+-----------------+
                       | guardian/ (separate process, planned)     |
                       |  watches P&L, drawdown, daily loss;       |
                       |  engages kill switch, flattens on hard    |
                       |  breach                                   |
                       +-------------------------------------------+

   bot/main.py and cli.py (planned) wire the modules together.
   backtest/ (planned) replays the same strategy and risk code on stored data.
```

Support modules: `config.py` (fail-closed loader), `types.py` (Decimal values, intent ids), `redact.py` (planned, secret scrubbing before any write).

## Single-order-path rule

- Only `OMS.submit()` may call `broker.submit_order`.
- Planned enforcement: `tests/test_single_order_path_ast.py` parses the source with `ast` and fails if any other call site exists.
- Every order, including reduce-only exits and guardian flattens, goes through the OMS state machine.
- Intent ids are deterministic (uuid5 over signal, symbol, side, type, quantity, price, reduce-only flag). Resubmitting the same intent produces the same client id.
- After a POST timeout the intent moves to `unknown_pending`. It is **never resubmitted**. It is reconciled by looking up the client id and open orders.

## Guardian as a separate process

- The guardian runs as its own process (planned, Day 2/3), reading the audit store rather than trusting the trading process's memory.
- It watches daily loss and drawdown against the limits in RISK.md.
- On a hard breach it engages the kill switch and flattens open positions through the OMS.
- Separation means a crash or hang in the trading loop does not disable the guardian.

## AI boundary

- The AI advisor is **advisory only**. It can veto an entry or size it down. It cannot create orders, increase size, or change limits.
- The AI package (`src/wallexbot/ai/`) must not import `risk`, the broker, or the OMS. Planned enforcement: `tests/test_agents_boundary.py`.
- It is **off in the critical path by default**. Shadow mode records its decisions without acting on them.
- It may also produce explanations for the demo.
- Only `ANTHROPIC_API_KEY` is relevant to it (see CONFIG_ENV.md).

## Persistence

- SQLite in WAL mode. Tables are append-only, enforced by triggers.
- Decimal values are stored as TEXT, never as floats.
- Redaction runs before every write.
- The kill switch is the latest row of an append-only table. A read error is treated as **engaged**.
