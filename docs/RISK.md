# Risk

Status: limits are defined in `config/default.yaml` and loaded by `config.py`. The risk engine (`risk/engine.py`) and kill switch (`risk/killswitch.py`) exist and are tested (`tests/test_risk.py`, `tests/test_killswitch.py`). The guardian and the hard-breach flatten procedure are planned and not yet present.

## Limits

Values from `config/default.yaml`. Percentages are of equity; order value is in the quote currency (USDT).

| Limit | Config key | Default | Notes |
|---|---|---|---|
| Risk per trade | `risk.risk_per_trade_pct` | 1.0% | Basis for position sizing (STRATEGY.md) |
| Max position | `risk.max_position_pct` | 25% | Checked per symbol in `risk/engine.py`; sizing basis in STRATEGY.md not yet fixed |
| Max exposure | `risk.max_exposure_pct` | 60% | Checked against held plus pending value in `risk/engine.py` |
| Max open positions | `risk.max_open_positions` | 3 | |
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

Exit orders are reduce-only. They skip entry limits, the kill switch and loss breaches. They still need known symbol rules and exact precision, and they cannot sell more than is held and uncommitted (see `tests/test_risk.py`).

## Soft breach

- Crossing a soft threshold (daily loss 4%, drawdown 8%) is a soft breach.
- In code, a soft breach adds a reason to the `loss_limits` check, so new entries are rejected while it holds. Reduce-only exits are not affected.
- The plan does not specify any further action (for example size reduction). It must be defined before Day 2.

## Hard breach and flatten procedure

Hard thresholds: daily loss 6%, drawdown 12%. The plan requires the guardian to flatten on a hard breach. The steps below follow from the rules in this document. They are **planned, not implemented**, and the exact sequence must be confirmed in code. The engine already flags a hard breach (`hard` in the breach result), but no guardian reads it yet.

1. The guardian reads the audit store and detects the hard breach.
2. It engages the kill switch by appending a new row. New entries are blocked from this point.
3. Open entry orders are cancelled. Cancellation behaviour on Wallex is unverified.
4. For each open position, the guardian submits a reduce-only exit through the OMS. The kill switch does not block it.
5. Each exit is reconciled by client id. Any intent in `unknown_pending` is reconciled before the position is treated as flat.
6. The kill switch stays engaged after the flatten. How it is cleared, and by whom, is not specified in the plan. Proposed: manual action only.

Market-order exits may be rejected by the market-order guard (in the mock, a market order larger than 2% of the book or larger than the book is cancelled). Live behaviour is unverified.

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
