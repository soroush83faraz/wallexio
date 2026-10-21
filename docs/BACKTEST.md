# Backtest

Status: **planned (Day 2). No backtest has been run, and no results are reported.** The backtest engine (`backtest/`) does not exist at commit `0e7ee8e`.

## Data

- Source: public 1-minute candles from `udf/history`, aggregated into **closed** 15m and 1h bars. The aggregate is cross-checked against native 15m and 60m resolutions (planned `data/aggregate.py`).
- Universe: top 10–15 markets by 24h volume, per `universe.top_n` (default 12).
- Quality checks (planned `data/quality.py`): 24/7 time grid, explicit zero-volume policy.
- Depth and trade data come from the recorder, which samples every 10–15 s into rotating files (planned). Recorder history only exists from the time recording started, so cost estimates based on it cover a shorter period than candle data.

## Engine assumptions

These are design choices, not measured facts:

- Fractional quantities, rounded down to exchange precision.
- Fees are percentage-based and charged in the fee asset.
- Spread and an impact term are applied to fills.
- Entries fill at the **next bar's open**.
- Stops are gap-aware: a gap through the stop fills at the gap price, not at the stop.
- Partial take-profit and a time stop are supported.
- Fee rate `0.0035` is a placeholder and unverified.

## Splits and the test set

- The data is divided into **train**, **validation** and **test** periods. The plan does not give the boundaries or ratios. They must be fixed and committed to the repo **before** any run. Changing them after results are seen invalidates the split.
- Grid runs (S0/S1 × E1–E4) use train and validation only. Variant choice is made on validation.
- The **test set is used once**. After the variant and the code are frozen, it is evaluated a single time and the result is recorded.
- Re-running on the test set after any change to code, parameters, or variant is not valid evidence. A new, untouched period is required for that.
- E4 is the **pre-registered leader** in the plan. It is the variant for the single test-set evaluation.

## Evidence required before live

All of the following are required. The plan does not give numeric pass thresholds. **Those thresholds are TBD and must be written down before the test-set evaluation.**

1. Backtest on train and validation completed with frozen code and config.
2. Single test-set evaluation completed and recorded, with thresholds fixed in advance.
3. Shadow run of 24–48 hours using `ShadowBroker`, which logs decisions and sends no orders (planned).
4. `doctor --read-only` green (planned).
5. Explicit go-ahead from the user.
6. Credentials and arming conditions met (see CONFIG_ENV.md).

A passing backtest alone is not evidence for live trading.

## Limitations

- No results exist. Nothing in this repo supports a claim of profitability.
- Fees, spreads, and minimum notional are placeholders or unverified.
- Candle volume semantics are unknown; only relative volume is used.
- Fill and impact models are approximations. Real fills may differ, especially for market orders in thin books.
- Universe selection by current 24h volume can introduce look-ahead bias. The universe at each point in time must use only data available then.
- Delisted or suspended markets may be missing from the data.
- The sample may be small. Results from a short period will not generalise on their own.
