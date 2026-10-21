# Strategy: long-only momentum breakout (S1)

Status: specification. Nothing here is implemented or backtested yet (Day 2 of the plan). Parameter values not listed below are not yet fixed in code or in `config/default.yaml`; they must be fixed before any backtest run.

## Scope

- Spot, long only, USDT quote, top 10–15 markets by 24h volume (`universe.top_n: 12` in config).
- Bar inputs are **closed** 15m and 1h bars, aggregated from 1m candles and cross-checked against native 15m and 60m resolutions. The forming candle is always dropped.
- The variant **S1** is the breakout strategy below. **S2** = S1 plus a trend filter; it is only a variant, and no ICT-based strategy is included unless the user supplies code.
- **S0** is the baseline used for comparison. The source plan names it but does not define it; it must be defined before backtesting.

## Entry rules (S1)

An entry signal requires all of the following on a closed bar:

1. **Donchian-20 breakout**: close breaks above the 20-bar Donchian high. Exact bar convention is to be pinned in code.
2. **EMA50 trend filter**: trend side relative to EMA50, as implemented in code.
3. **Rate of change (ROC) acceleration**: momentum is increasing. Threshold: not yet specified.
4. **Volume confirmation**: relative volume ≥ 1.8× the median. Only relative volume is used, because candle volume semantics on Wallex are unverified.
5. **ATR band**: breakout distance is within an ATR-based band. Band width: not yet specified.
6. **Not-late filter**: the price has not already run too far from the breakout level. Threshold: not yet specified.
7. **Regime tightening**: entry criteria are stricter in an unfavourable regime. Regime definition: not yet specified.
8. **Cost gate** (below) must pass.
9. **Cooldown**: after a signal in a market, no new signal in that market for 8 bars.

If any condition fails, the strategy abstains. Abstention is a valid and expected outcome.

## Cost gate

An entry is taken only if the expected move is at least **3× the round-trip cost**.

- Round-trip cost includes both-side fees, the spread, and an impact/slippage allowance.
- Fee rates in config are `taker_rate: 0.0035` and `maker_rate: 0.0035`. **Both are placeholders and unverified** (see WALLEX_INTEGRATION.md).
- Spread is checked separately by the risk engine (`max_spread_bps: 15`).
- The exact round-trip cost formula is not fixed yet.

## Position sizing

Risk-based sizing:

```
quantity = equity × risk_per_trade_pct / (entry − stop)
```

- `risk_per_trade_pct` = 1% (config).
- The result is clamped by: max position (25% of equity), max order value (1000 quote), available balance, exchange precision (rounded **down**, never up), and order-book depth.
- The stop must be below entry for a long. A stop that is missing or not below entry means no trade.

## Exits (E1–E4)

Exit variants are compared in backtests on the same entries:

- **E1–E3**: exit variants whose exact rules are not specified in the source plan. They are to be written down before backtesting.
- **E4**: a hybrid exit and the **pre-registered leader**. It is the variant expected to be evaluated on the test set.
- The backtest engine supports partial take-profit, a time stop, and gap-aware stops (see BACKTEST.md).

Exit orders are reduce-only. The kill switch does not block them (see RISK.md).

## What the strategy does not claim

- No profitability, win rate, or expected return is claimed. No backtest has been run.
- The strategy may produce zero trades in a period. That is correct behaviour when filters fail.
