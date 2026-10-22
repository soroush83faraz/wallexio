# Risk

Status: limits are defined in `src/wallexbot/default_config.yaml` and loaded by `config.py`. The risk engine (`risk/engine.py`) and kill switch (`risk/killswitch.py`) exist and are tested (`tests/test_risk.py`, `tests/test_killswitch.py`). The guardian (`guardian/monitor.py`, `flatten.py`, `main.py`) and the hard-breach flatten procedure are implemented and tested against `MockWallex` only (`tests/test_guardian_*.py`). They have never run against the real exchange. The bot loop (`bot/`, mock and shadow only) writes the strategy heartbeat each tick and runs the guardian in-process, on its own DB connection and the bot's own broker object (`--no-guardian` disables it).

## Limits

Values from `src/wallexbot/default_config.yaml`. Percentages are of equity; order value is in the quote currency (USDT).

| Limit | Config key | Default | Notes |
|---|---|---|---|
| Risk per trade | `risk.risk_per_trade_pct` | 1.0% | Basis for position sizing (STRATEGY.md) |
| Max position | `risk.max_position_pct` | 25% | Checked per symbol in `risk/engine.py`; sizing basis in STRATEGY.md not yet fixed |
| Max exposure | `risk.max_exposure_pct` | 60% | Checked against held plus pending value in `risk/engine.py` |
| Max open positions | `risk.max_open_positions` | 3 | Dust is flat (see Dust) |
| Max order value | `risk.max_order_value` | 1000 USDT | |
| Daily loss, soft | `risk.daily_loss_soft_pct` | 4% | |
| Daily loss, hard | `risk.daily_loss_hard_pct` | 6% | |
| Drawdown, soft | `risk.drawdown_soft_pct` | 8% | |
| Drawdown, hard | `risk.drawdown_hard_pct` | 12% | Engages the kill switch (per plan) |
| Consecutive losses | `risk.consecutive_loss_cooldown` | 3 | Then a 60-minute cooldown (per plan) |
| Min notional | `risk.min_notional` | 5 USDT | **Placeholder, unverified on Wallex** |
| Max spread | `universe.max_spread_bps` | 15 bps | Entry check |
| Max slippage | `universe.max_slippage_bps` | 20 bps | |
| Min depth multiple | `universe.min_depth_multiple` | 5× | Loaded into config; no depth check in `risk/engine.py` yet |
| Max data age | `universe.max_data_age_s` | 30 s | Staleness check on entries (`stale_quote`) |

The 60-minute cooldown after consecutive losses is stated in the plan. The engine takes its cooldown length as a parameter (`cooldown_s`), not from a config key yet.

## Rate limiting

Wallex documents 20 order-creates per 10 s [from docs]. Our client-side `TokenBucket` (`exchange/ratelimit.py`) is deliberately 10 per 10 s (half) as a safety margin.

## Check order

The risk engine runs ordered, fail-closed checks on entries (BUY, not reduce-only). Gate checks stop the run at once. Other checks collect reasons. An entry is approved only if no reason is found. Any exception inside a check rejects the order.

1. Kill switch
2. Long-only (a plain SELL is rejected; sells must be reduce-only)
3. Quote staleness, symbol known (gates)
4. Spread
5. Quantity and price precision (gate)
6. Min notional, max order value
7. Equity positive (gate)
8. Max position, max exposure, max open positions
9. Cash, including fee reserve
10. One open order per symbol
11. Start-of-day equity is for today (UTC)
12. Soft and hard daily-loss and drawdown limits
13. Loss cooldown

There is no depth check in the engine yet, so the "min depth multiple" limit is not enforced.

Exit orders are reduce-only. They skip entry limits, the kill switch and loss breaches. They still need known symbol rules and exact precision, and they cannot sell more than is held and uncommitted (see `tests/test_risk.py`). Committed = SELLs in `risk_approved`, `submitted`, `partially_filled`, `unknown_pending`, plus SELLs still `proposed` and young (created less than 30 s ago and at most 5 s in the future) that were inserted before the checked one. Any other `proposed` (crash or store-error leftover, or future-dated after a backward clock step) can never reach the broker by itself: it neither blocks nor counts; open intents and the exchange balance still prevent an oversell.

**Store errors.** The risk decision row and the state change commit together or not at all: an error inside that transaction (e.g. `database is locked` after the 5 s busy timeout) rolls it back and propagates, leaving a `proposed` intent. Submitting the same intent again resumes it (nothing was sent: the broker is called only after `risk_approved` commits). The bot resumes its own `proposed` exit on the next poll (not the next bar), resumes its own `proposed` entry while younger than 20 s (else rejects it) and moves its own `risk_approved` leftovers to `unknown_pending`; the guardian's flatten resumes its own episode's exit on the next tick (`tests/test_oms_store_faults.py`, `tests/test_exit_race.py`). A failing COMMIT is rolled back too (secondary errors ignored, nesting depth reset) and re-raised, so the connection is never left inside a write transaction and a retry works (`tests/test_store_commit_failure.py`).

**Exit race (bot exit vs guardian flatten).** The risk check, its `risk_decisions` row and the `proposed -> risk_approved | rejected` change are one store transaction, so no other connection sees a decided-but-proposed exit. Flatten treats a young `proposed` SELL as ambiguous and waits for it. With both layers exactly one SELL reaches the broker in every interleaving tested (`tests/test_exit_race.py`).

**Dust** (`dust.py`, used by the risk engine, the bot and the guardian): quantity below one amount step, or notional at the mark below `max(exchange min, risk.min_notional)`. Fee dust left after an exit is flat everywhere, so it neither blocks new symbols (max open positions) nor triggers a flatten.

**Clock steps.** Data dated after the clock is invalid, not fresh: quotes, bars not yet closed by the clock (`bars_future`), cached shadow depth. Timers on stored timestamps (OMS not-found grace and stale-entry cancel, LIMIT exit timeout, feed retries) re-anchor to the new clock after a backward step instead of stalling (`clockstep.py`, `tests/test_clock_step.py`).

## Soft breach

- Crossing a soft threshold (daily loss 4%, drawdown 8%) is a soft breach.
- In code, a soft breach adds a reason to the `loss_limits` check, so new entries are rejected while it holds. Reduce-only exits are not affected.
- The plan does not specify any further action (for example size reduction). It must be defined before Day 2.

## Guardian (`guardian/`)

Runs inside the bot process (`InProcessGuardian`), with its own `Store` connection and its own `KillSwitch`/`RiskEngine`/`OMS` around the bot's broker object; ticked after every bot tick and at least every 15 s. It imports nothing from `strategy`, `backtest` or `ai` (`tests/test_guardian_isolation.py`). The broker is the source of truth: equity is quote balance plus base holdings marked at the bid. The standalone `python -m wallexbot.guardian.main --db NEW.db --mode mock --demo-db` is a demo on its own mock broker and refuses a bot's database.

Each tick (`GuardianConfig` defaults) the guardian reconciles the OMS, reads balances, open orders and quotes, then checks:

| Condition | Action |
|---|---|
| Soft breach (daily loss 4%, drawdown 8%) | Engage kill switch (entries off) |
| Hard breach (daily loss 6%, drawdown 12%) | Flatten everything |
| Exchange base balance differs from the OMS ledger by more than max(1 amount step, 0.1% of ledger) on 2 consecutive ticks | Kill switch + `needs_review`; exchange wins, nothing in the ledger is "fixed" |
| A symbol with a non-dust position or a live order has had no fresh quote for more than `max_data_age_s` (30 s). Only such symbols are checked: with nothing held or ordered the guardian never acts on feed age (a universe-only symbol's stale or missing quote only stops entries on it, in the bot and the risk engine). A quote dated more than `max_quote_future_skew_s` (5 s) after the guardian's clock is invalid (`future_quote_ts`, not used to mark); freshness is clamped to now | Condition `stale_feed:<symbols>`; **no** kill switch (entries on a stale quote are refused per symbol anyway); start-of-day / high-water mark not written |
| Same, for more than 120 s (`stale_action_after_s`), on a held or ordered symbol | `stale_action` (`bot`/`guardian --stale-action hold\|flatten`): `hold` (default) or `flatten`. Either way a `guardian.alert` error row, repeated every 5 min while it lasts, and the symbols in the tick audit event's `held_stale`; the healthcheck fails on both. With `flatten` and no quote (it raises) the exit is priced from the broker's order book, else the guardian's last valid quote, else the attempt fails into needs_review + alert (`tests/test_guardian_held_stale.py`) |
| Strategy heartbeat older than 60 s | Kill switch, **transient** (reason `auto:heartbeat_lost`) |
| Error while evaluating breaches | Kill switch (fail closed) |

A failed tick is logged and the loop continues; 3 consecutive failed ticks engage the kill switch (`max_consecutive_failures`); the bot engages it after 5 consecutive failing bot ticks. Every decision is appended to the `audit_events` table with `actor='guardian'`.

**Kill-switch release.** Every engagement stays until an operator disengages it (a new `killswitch_events` row), except one: the guardian's own transient engagement (reason starting `auto:`, actor `guardian`) while it is the latest row. The guardian releases that itself once no freezing condition has been seen for 3 consecutive ticks spanning at least 60 s of wall-clock **and** monotonic time (`auto_release_clear_ticks`, `auto_release_clear_s`, minimum 60 s; a forward clock jump cannot shorten it, a backward step restarts it), as an audited `release_kill_switch` event and a disengage row with actor `guardian`. The release is a compare-and-set in one `BEGIN IMMEDIATE` transaction: the latest row is re-read and released only if it is still the row the clear streak observed (actor `guardian`, reason `auto:`); an operator/bot row written meanwhile by any connection is left alone (`release_kill_switch_refused`). A hard breach, soft breach, balance mismatch, breach-evaluation error, tick failures, a flatten or an operator/bot engagement written while only a transient one holds the switch adds a new non-transient row on top (`KillSwitch.engage_unless_held`), so it is never auto-released (`tests/test_killswitch_auto_release.py`).

## Hard breach and flatten procedure (`guardian/flatten.py`, `FlattenConfig` defaults)

1. Engage the kill switch.
2. Cancel entry orders only (open BUYs) and verify with `open_orders()`; sync the ledger with `oms.reconcile()`. Protective stops and other resting SELLs for the symbol are cancelled first (`cancel_protective_stops=True`) because Wallex stop-order balance locking is unverified.
3. Per position, place reduce-only SELL exits **through `OMS.submit`** (the reviewed entry in the AST allow-list is `_Flattener._place_exit`). Hard breach goes straight to MARKET; otherwise LIMIT at bid minus 20 bps, escalating to MARKET after a LIMIT timeout (10 s) or failure; a MARKET still open after 5 s is cancelled. Each child order is at most 50% of the reachable visible bid depth (MARKET: within 2% of the best bid, matching Wallex's auto-cancel). Sizes come from confirmed exchange balances, capped by the ledger.
4. Poll until flat, or give up after 3 failed exits per symbol and record `needs_review` plus an alert row in `errors` (source `guardian.alert`).

Safeguards: no resubmission (a new exit is placed only when every earlier SELL intent for the symbol is terminal; `unknown_pending` and a young `proposed` SELL are waited out; the episode's own exit left `proposed` by a store error is resumed, not replaced); deterministic ids (`gflat:<UTC day>-<cause>:<symbol>:<n>`), so a re-run cannot send the same client id twice; bounded orders (20 per symbol), waits and loop iterations. `dry_run` plans and audits without broker writes (the standalone demo's `--mode shadow`; the in-process guardian of a shadow bot sells on the simulated ShadowBroker).

Unverified: Wallex cancellation behaviour, market-order behaviour in thin books, and stop-order balance locking. All are exercised only against the mock.

## Failure behaviour

| Failure | Behaviour |
|---|---|
| Invalid config, unknown mode, host not on allow-list | `ConfigError`, startup refused (implemented) |
| `mode: live` without `WALLEX_ARM_LIVE=YES_I_UNDERSTAND`, trade key, or `cli_armed` | `ConfigError`, startup refused (implemented) |
| Kill-switch read error | Treated as **engaged**; entries blocked, reduce-only exits still allowed (implemented in `killswitch.py`) |
| Stale market data | Entry rejected (implemented, `stale_quote`) |
| Spread check fails | Entry rejected (implemented). Depth check: not yet in the engine |
| POST timeout on order create | No resubmit. Intent moves to `unknown_pending` and is reconciled by client id and open orders (implemented in `oms.py`) |
| Duplicate client id | Rejected; no second order (implemented in `mock_wallex.py`, adopted by the OMS) |
| Partial fill, reject | Recorded as deltas or a terminal state (implemented in `oms.py`, mock-tested) |
| Risk engine or store error | Order not sent (fail-closed; implemented) |

Fault-injection cases for the mock exchange: timeout after accept, duplicate client id, partial fill, reject, and stale feed. They are covered by `tests/test_oms.py`, `tests/test_mock_wallex.py` and `tests/test_risk.py`. The pass criterion is zero duplicate orders and a defined end state for every case.
