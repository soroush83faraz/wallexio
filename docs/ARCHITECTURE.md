# Architecture

Status: mock and shadow vertical slices run end to end (bot loop, ShadowBroker, optional shadow-only AI advisor, CLI), plus strategy, backtest, guardian, history downloader, recorder and an unverified private client. All of it is tested against the mock, synthetic or recorded data only.

Implemented modules (under `src/wallexbot/`):

- `exchange/`: `wallex_public` (GET only), `wallex_private` (`WallexBroker`, `WallexAccountReader`; unverified, needs arming), `mock_wallex` (incl. `depth()`), `ratelimit`, `auth` (host pin, arming checks, `is_live_armed_token`), `base` (Broker protocol).
- `data/`: `candles`, `aggregate`, `quality`, `recorder`, `history` (resumable 1m downloader, writes `data/history/`).
- `strategy/`: `contract`, `indicators`, `features`, `config`, `momentum` (S1), `trend_baseline` (S0), `cooldown`, `sizing`, `exits` (E1-E4), `lookahead`.
- `backtest/`: `engine`, `costs`, `metrics`, `splits`, `sensitivity`, `report`, `runner` (behind `wallexbot backtest`).
- `bot/`: `feed` (PublicFeed for live public data, ReplayFeed for recorded candles), `state` (persisted positions, plans, cooldown), `loop` (the per-tick `Bot`), `advice` (advisor gate: timeout, abstain on any failure), `main` (mock/shadow entry; live refused). Orders leave only through `pipeline.route_to_oms`.
- `ai/`: `schema` (strict context and verdict types), `advisor` (`NullAdvisor`, `AnthropicAdvisor`, `LoggedAdvisor`, `make_advisor`), `evaluate`. Imports nothing from the order path. `advisor_bridge.py` (top level) adapts the bot's dict context to it and persists verdicts as `audit_events` (actor `advisor`).
- `exchange/shadow.py`: `ShadowBroker`, a Broker that answers from memory using public depth snapshots; `step()` matches resting orders; optional JSONL journal.
- `cli.py`, `__main__.py`: `demo`, `backtest`, `doctor`, `guardian`, `bot`.
- `guardian/`: `monitor`, `flatten`, `main` (`InProcessGuardian`, run by the bot; standalone demo CLI on its own mock broker).
- Shared helpers: `dust.py` (one dust rule for risk, bot and guardian), `clockstep.py` (timers that re-anchor after a backward clock step), `strategy/profiles.py` (`strict` and the `demo` EXERCISE profile).
- `risk/` (`engine`, `killswitch`), `oms.py`, `store.py` (incl. `audit_events` table and heartbeat), `pipeline.py` (scripted mock round trip, `build_mock_broker`), `doctor.py` (read-only preflight, `wallexbot doctor`), `config.py`, `types.py`, `redact.py`.

**Not implemented**: any live broker wiring (the CLI and the bot refuse live). The diagram marks the live broker as *planned*.

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
                      |                 |  momentum, S0,   |
                      |                 |  sizing, exits   |
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
            | broker (mock,     |        | store.py         |
            | shadow; live is   |        |  SQLite WAL,     |
            | not wired)        |        |  append-only,    |
            +-------------------+        |  redacted writes |
                                         +-------+----------+
                                                 ^
                                                 | reads / writes
                       +-------------------------+-----------------+
                       | guardian/ (in the bot process, own Store  |
                       |  connection, same broker object)          |
                       |  watches P&L, drawdown, daily loss;       |
                       |  engages kill switch, flattens on hard    |
                       |  breach                                   |
                       +-------------------------------------------+

   bot/main.py (via cli.py `bot`) wires the modules together and ticks the guardian after each bot tick.
   backtest/ replays the strategy engines and exit policies on stored candles (it has its own
   simulated fills and a RiskConfig-based loss check, not the OMS).
```

Support modules: `config.py` (fail-closed loader), `types.py` (Decimal values, intent ids), `redact.py` (secret scrubbing of payload and free-text columns before they are written; identifier columns are validated instead, see Persistence).

## Single-order-path rule

- Only `OMS.submit()` may call `broker.submit_order`.
- Guarded by `tests/test_single_order_path_ast.py`, a **lint-style** AST check over `src/wallexbot`, not a proof (Python is too dynamic for a static check to rule out every path). It fails on:
  - any `submit_order` attribute other than the single call `self.broker.submit_order(...)` in `OMS.submit` (so aliasing, such as `send = b.submit_order`, and passing the bound method are flagged too), and on `submit_order` as a bare name, import alias, keyword or parameter;
  - string constants containing `submit_order` or a fragment of it, and concatenations, f-strings and `%`/`format`/`join` builds whose literal pieces contain a fragment or fold to the name (docstrings are ignored);
  - `getattr`, `setattr`, `delattr`, `vars`, `eval`, `exec`, `compile`, `globals`, `locals`, `__import__`, `attrgetter`, `methodcaller`, `import_module`, `__getattribute__`, `__getattr__`, `__setattr__` and `__dict__`, and importing `operator`, `importlib`, `inspect` or `builtins`. Six reviewed uses are allow-listed by file, scope and count (`ALLOWED_DYNAMIC` in the test: two `getattr` in `store.py`; in `redact.py` one `object.__setattr__`, one `getattr` for numpy scalar types and two `vars` for logging records). Known misses (not flagged): bytes-built attribute names, `sys.modules[...]` access to `getattr`, `string.Formatter().get_field`, `marshal`/`pickle`/`code` execution and use of mock internals. Treat the guard as a tripwire against accidents, not as a defence against hostile code;
  - outside `oms.py`, `pipeline.py` and `exchange/`: importing the broker modules (`exchange.mock_wallex`, `exchange.base`, the `exchange` package), referencing `MockWallex`/`mock_wallex` or the live `WallexBroker`/`wallex_private` (read-only names such as `WallexAccountReader` stay importable, the doctor uses them), or calling a submit-like method (`*submit*`, `place_order`, `send_order`, `create_order`, ...). One reviewed exception (`ALLOWED_OMS_SUBMIT`): the guardian's `_Flattener._place_exit` in `guardian/flatten.py` may call `self.oms.submit(...)` exactly once, because flatten exits must go through the OMS.
  The test also runs the scanner over rogue snippets, one per bypass pattern, and asserts that each is flagged.
- Every order, including reduce-only exits and guardian flattens, goes through the OMS state machine.
- Intent ids are deterministic: uuid5 over a JSON array of signal, symbol, side, type, quantity, price, stop price and reduce-only flag. Numbers are keyed by value, not spelling (`0.01` and `0.010000` give the same id), so resubmitting the same intent, at any decimal scale, produces the same client id (`wb_` + 32 hex) and at most one broker order. Two exits that differ only in stop price are different intents.
- After a POST timeout the intent moves to `unknown_pending`. It is **never resubmitted**. It is reconciled by looking up the client id and open orders.
- In `OMS.submit` the risk view, the `risk_decisions` row and `proposed -> risk_approved | rejected` are one `BEGIN IMMEDIATE` transaction that commits whole or not at all (a store error rolls it back and propagates); a SELL still `proposed` and young (< 30 s old, <= 5 s in the future) counts as committed. Bot exits and guardian flattens therefore never both sell one position (`tests/test_exit_race.py`; RISK.md).
- A `proposed` intent is resumable: submitting it again re-runs the risk step and sends at most once. After a store error (`database is locked` beyond the 5 s busy timeout) the bot resumes its own exit on the next poll and the guardian its own flatten exit on the next tick.

## Guardian (in-process in this build)

- `bot --mode mock|shadow` runs `InProcessGuardian` (`guardian/main.py`): its own `Store` connection on the bot's database, its own kill switch, risk engine and OMS, and the bot's broker **object** as the source of truth. It ticks after every bot tick and at least every `--guardian-interval-s` (15 s) while the bot sleeps; a failed guardian tick never stops the bot. `--no-guardian` disables it.
- It watches daily loss, drawdown, broker-vs-ledger mismatch and the strategy heartbeat (RISK.md has the thresholds). Data staleness is checked only for symbols held or with a live order; it never engages the kill switch (`stale_action` hold/flatten after 120 s).
- On a soft breach, mismatch or breach-evaluation error it engages the kill switch until an operator clears it; on a hard breach it flattens open positions through `OMS.submit`. A lost strategy heartbeat engages it as a transient `auto:` row, which only the guardian releases, after 3 clear ticks and 60 s.
- `bot --interval-s` (live feed) is at most `max_data_age_s / 2` (15 s): data refreshes only on bot ticks, so the guardian's wake-ups in between never see a healthy feed as stale.
- It must not import `strategy`, `backtest` or `ai` (`tests/test_guardian_isolation.py`).
- Trade-off: in-process means a hang of the bot process also stops the guardian. A separate process would only help against a real exchange account (live), which this build does not have; a separate process cannot see an in-process paper or shadow broker. The standalone `wallexbot guardian` is therefore a demo on its own mock broker, and refuses a bot's database.

## AI boundary

- The AI advisor is **advisory only**. It can veto an entry or size it down. It cannot create orders, increase size, or change limits.
- The AI package (`src/wallexbot/ai/`) must not import `risk`, the broker, or the OMS. Enforced by `tests/test_ai_isolation.py` and `tests/test_bot_ast.py`.
- It is **off in the critical path by default**. Shadow mode (`bot --advisor shadow`) records its verdicts without acting on them; the wiring never sets `apply=True`.
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
- The kill switch is the latest row of an append-only table. A read error is treated as **engaged**. Known limit: "latest" is the highest `id`, so a raw writer with file access could append a row with a huge `id` and `engaged=0` and make later re-engagements invisible. The database file must be protected like a credential; the guardian does not yet keep its own copy of the engaged state.
