# Architecture

Status: Day-1 mock vertical slice. Implemented: `exchange/` (wallex_public, mock_wallex, ratelimit, auth), `data/`, `store.py`, `risk/`, `oms.py`, `redact.py`, `config.py`, `types.py`, `pipeline.py`. Not yet written: `strategy/`, `guardian/`, `backtest/` and `ai/` (empty packages), `cli.py`, `bot/`, and the shadow and private broker clients. Modules marked *planned* do not exist yet.

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

Support modules: `config.py` (fail-closed loader), `types.py` (Decimal values, intent ids), `redact.py` (secret scrubbing of payload and free-text columns before they are written; identifier columns are validated instead, see Persistence).

## Single-order-path rule

- Only `OMS.submit()` may call `broker.submit_order`.
- Guarded by `tests/test_single_order_path_ast.py`, a **lint-style** AST check over `src/wallexbot`, not a proof (Python is too dynamic for a static check to rule out every path). It fails on:
  - any `submit_order` attribute other than the single call `self.broker.submit_order(...)` in `OMS.submit` (so aliasing, such as `send = b.submit_order`, and passing the bound method are flagged too), and on `submit_order` as a bare name, import alias, keyword or parameter;
  - string constants containing `submit_order` or a fragment of it, and concatenations, f-strings and `%`/`format`/`join` builds whose literal pieces contain a fragment or fold to the name (docstrings are ignored);
  - `getattr`, `setattr`, `delattr`, `vars`, `eval`, `exec`, `compile`, `globals`, `locals`, `__import__`, `attrgetter`, `methodcaller`, `import_module`, `__getattribute__`, `__getattr__`, `__setattr__` and `__dict__`, and importing `operator`, `importlib`, `inspect` or `builtins`. Six reviewed uses are allow-listed by file, scope and count (`ALLOWED_DYNAMIC` in the test: two `getattr` in `store.py`; in `redact.py` one `object.__setattr__`, one `getattr` for numpy scalar types and two `vars` for logging records). Known misses (not flagged): bytes-built attribute names, `sys.modules[...]` access to `getattr`, `string.Formatter().get_field`, `marshal`/`pickle`/`code` execution and use of mock internals. Treat the guard as a tripwire against accidents, not as a defence against hostile code;
  - outside `oms.py`, `pipeline.py` and `exchange/`: importing the broker modules (`exchange.mock_wallex`, `exchange.base`, the `exchange` package), referencing `MockWallex`, or calling a submit-like method (`*submit*`, `place_order`, `send_order`, `create_order`, ...).
  The test also runs the scanner over rogue snippets, one per bypass pattern, and asserts that each is flagged.
- Every order, including reduce-only exits and guardian flattens, goes through the OMS state machine.
- Intent ids are deterministic: uuid5 over a JSON array of signal, symbol, side, type, quantity, price, stop price and reduce-only flag. Numbers are keyed by value, not spelling (`0.01` and `0.010000` give the same id), so resubmitting the same intent, at any decimal scale, produces the same client id (`wb_` + 32 hex) and at most one broker order. Two exits that differ only in stop price are different intents.
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

- SQLite in WAL mode. Audit tables are append-only. Triggers stored in the schema (so they apply to every connection, including a plain `sqlite3` shell) enforce this:
  - `UPDATE` and `DELETE` abort on every table. The one exception is `order_intents`, whose `state` and `updated_at` may change, and only along legal state transitions.
  - An `INSERT` whose primary key, rowid or `UNIQUE` key already exists aborts *before* SQLite resolves the conflict. This means `INSERT OR REPLACE`, `REPLACE INTO` and upserts cannot rewrite or delete an existing row. Without this, `REPLACE` deletes the old row without firing `DELETE` triggers and could, for example, rewrite a kill-switch row. Explicit negative rowids are refused, and the `order_intents` rowid cannot be changed.
  - The store's own connection also sets `PRAGMA recursive_triggers=ON` as a second layer.
  - Not covered: appending new rows (that is how the log grows; disengaging the kill switch is a new, logged row), and anyone with raw write access to the file who drops the triggers.
- Decimal values are stored as TEXT, never as floats.
- Payload and free-text columns (payloads, reasons, messages, kill-switch reason and actor) pass through `redact()` before every write. Identifier columns are **not** redacted, because redacting them would break idempotency and lookups. They are **validated** at the store boundary instead: `client_id` must match `^wb_[0-9a-f]{32}$`; signal id, symbol, broker event kind, error source, fee asset and day must match `^[A-Za-z0-9_.:-]{1,128}$` and pass `redact()` unchanged. Any other value raises `StoreError` and nothing is written.
- Realized P&L of a closed round trip equals the exact quote cash flows, fees included. Ledger additions, subtractions and multiplications run in an exact decimal context that raises instead of rounding. The only rounded step is the pro-rata cost release on a partial sell, and the closing fill releases the exact remaining cost basis, so that rounding cancels (see `apply_fill` in `oms.py`).
- The kill switch is the latest row of an append-only table. A read error is treated as **engaged**. Known limit: "latest" is the highest `id`, so a raw writer with file access could append a row with a huge `id` and `engaged=0` and make later re-engagements invisible. The database file must be protected like a credential; the guardian (planned) should also keep its own copy of the engaged state.
