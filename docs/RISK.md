# Risk

Status: limits are defined in `config/default.yaml` and loaded by `config.py`. The risk engine (`risk/engine.py`) and kill switch (`risk/killswitch.py`) are planned and not yet present.

## Limits

Values from `config/default.yaml`. Percentages are of equity; order value is in the quote currency (USDT).

| Limit | Config key | Default | Notes |
|---|---|---|---|
| Risk per trade | `risk.risk_per_trade_pct` | 1.0% | Basis for position sizing (STRATEGY.md) |
| Max position | `risk.max_position_pct` | 25% | Basis (per symbol or otherwise) not yet fixed in code |
| Max exposure | `risk.max_exposure_pct` | 60% | Basis not yet fixed in code |
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
| Min depth multiple | `universe.min_depth_multiple` | 5× | Basis of the multiple is not yet fixed in code |
| Max data age | `universe.max_data_age_s` | 30 s | Staleness check on entries |

The 60-minute cooldown after consecutive losses is stated in the plan but is not a config key yet.

## Check order

The risk engine runs ordered, fail-closed checks. The first failure rejects the order:

1. Kill switch
2. Data staleness
3. Spread and depth
4. Min notional and precision
5. Notional, position, exposure and open-position limits
6. Soft and hard daily-loss and drawdown limits

Exit orders are reduce-only. The kill switch does not block them (see below).

## Soft breach

- Crossing a soft threshold (daily loss 4%, drawdown 8%) is a soft breach.
- The action taken at a soft breach is **not specified in the source plan**. It must be defined before Day 2.

## Hard breach and flatten procedure

Hard thresholds: daily loss 6%, drawdown 12%. The plan requires the guardian to flatten on a hard breach. The steps below follow from the rules in this document. They are **planned, not implemented**, and the exact sequence must be confirmed in code.

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
| `mode: live` without `WALLEX_ARM_LIVE=YES_I_UNDERSTAND` | `ConfigError`, startup refused (implemented) |
| Kill-switch read error | Treated as **engaged**; entries blocked, reduce-only exits still allowed (planned) |
| Stale market data | Entry rejected (planned) |
| Spread or depth check fails | Entry rejected (planned) |
| POST timeout on order create | No resubmit. Intent moves to `unknown_pending` and is reconciled by client id and open orders (planned) |
| Duplicate client id | Rejected; no second order (planned, mock fault-injection) |
| Partial fill, reject | Recorded in a defined state (planned, mock fault-injection) |
| Risk engine or store error | Order not sent (fail-closed; planned) |

Fault-injection cases for the mock exchange are planned: timeout after accept, duplicate client id, partial fill, reject, and stale feed. The pass criterion is zero duplicate orders and a defined end state for every case.
