# Backtest

Status: **the engine, cost model, metrics, splits, sensitivity, report writer and a runner/CLI (`python -m wallexbot backtest`) are implemented and unit-tested. One exploratory run on train+validation of the local 45-day history exists under `reports/` (placeholder costs, unvalidated; see "Runner and CLI").** The test segment has not been evaluated. Nothing here supports a claim of profitability.

Modules (`backtest/`): `engine.py` (bar-by-bar simulator), `costs.py`, `metrics.py`, `splits.py`, `sensitivity.py`, `report.py`, `runner.py` (grid driver, used by `cli.py`).

## Data

- Source: public 1-minute candles from `udf/history`, aggregated into **closed** 15m and 1h bars (and 4h, aggregated from 1m or derived from 1h). Wallex has no native 15m or 4h series: the documented resolutions 15/240/480/720/2D/3D are not honoured server-side. The aggregate is cross-checked against the native 1h series (resolution `60`) in `tests/test_aggregate.py` (`test_1m_to_1h_matches_native_hourly_fixture`, on recorded fixtures), not in `data/aggregate.py`, which does no runtime cross-check.
- Candle data caveats: the 1m series forward-fills no-trade minutes as zero-volume flat bars; the 1m range cap is about 1500 bars per request; native-60 truncates to about 696 bars; candle volume is in base units.
- Universe: top 10–15 markets by 24h volume, per `universe.top_n` (default 12).
- Quality checks (`data/quality.py`): 24/7 time grid, explicit zero-volume policy.
- Depth and trade data come from the recorder (`data/recorder.py`), which samples every 15 s by default (`interval_s`) into daily-rotating files. Recorder history only exists from the time recording started, so cost estimates based on it cover a shorter period than candle data.

## Historical downloader (`data/history.py`)

`fetch_history(client, symbol, start_ts, end_ts, out_dir)` downloads closed 1m candles with public GETs only, walking backwards in 600-bar chunks (one request each) with a request budget (default 1500), retries and an abort after repeated failures. It is resumable: `state.json` records the bar ranges the API has already answered, so re-runs fetch only the rest and do not re-request true exchange gaps or the pre-listing region. `load_history(...)` reads the files back.

Layout under the output directory (the repo uses `data/history/`, git-ignored via `/data/`): `<SYMBOL>/<SYMBOL>_1m_<YYYY-MM>.csv` (one file per UTC month, with a `.sha256` sidecar) plus `<SYMBOL>/state.json`. There is no CLI for the downloader yet. The 600-bar chunk is the client's `MAX_BARS_PER_REQUEST`; the "about 1500 bars" cap noted above is the observed server limit, not what the downloader uses.

## Engine assumptions (as implemented in `engine.py` and `costs.py`)

These are design choices, not measured facts:

- Decimal arithmetic for money and quantity; floats only in `metrics.py`. Quantities rounded down to precision. Money (notional, fees, spread/impact attribution, cash, equity, P&L) is computed in an exact context that traps any rounding, so net = gross - fees - spread - impact holds to the last digit.
- Stop and target levels are put on the symbol's price tick: the signal's initial stop rounded down, a policy's stop update rounded up (unless that alone would lift it to the close, then down; stops only move up), take-profit levels rounded up.
- Strategies and exit policies are handed a tuple of at most `BacktestParams.lookback_bars` (default 400) closed bars ending at the current bar; nothing later is reachable from it. The engine refuses a lookback shorter than a component's declared `history_bars` (S1: 300).
- On the entry bar the position state seen by take-profit logic has `highest_high_since_entry` = the entry bar's open; bar highs are folded in only after the bar closes.
- E4's breakeven stop covers both fees and the exit's own price give-up: `entry*(1+f)/((1-f)*(1-buffer))`, `ExitParams.breakeven_buffer_bps` default 20 (derive it from the cost model with `CostModel.exit_cost_bps()`). A gap below the stop still fills at the lower open.
- Signals are evaluated on a closed bar and fill at the **next bar's open**; entry caps (positions, exposure, soft/hard breach, cooldown) are checked at fill time, best score first.
- Every fill is a taker fill: reference price moved by a half-spread (7.5 bps default) plus a linear impact term (`impact_k` 0.2 x order notional / previous bar's notional, capped at 50 bps). Fee is charged in quote at `taker_rate` 0.0035 (placeholder, unverified; Wallex may charge the received asset).
- Stops are gap-aware: a gap through the stop fills at the open. If one bar touches both stop and target, the stop is assumed first.
- Partial take-profit, an engine time stop and a daily-loss/drawdown breach (flatten at the next open, halt entries for the UTC day) are simulated, using the `RiskConfig` percentages.
- Positions open at the end of the data are sold at the last close with full costs and flagged `end_of_data`. Equity is marked at closes; loss limits are checked at closes only.
- Runs are deterministic and carry an `inputs_hash` and `result_hash`.

## Runner and CLI

`python -m wallexbot backtest [--history-dir data/history] [--out-dir reports] [--symbols ...] [--workers 4]` loads the 1m history, aggregates closed 15m bars (bars with absent minutes are kept, flagged and counted), builds `SymbolRules` (from `tests/fixtures/markets.json` where listed; otherwise derived: price precision = finest tick seen in the data, amount precision = smallest whose lot is worth <= 0.15 USDT at the median price; both are guesses, not exchange facts) and runs S0/S1 x E1-E4 x taker 0.0035 (placeholder) / 0.002 / 0.001 (ASSUMED scenarios, real fee unverified) on **train and validation only**, plus an equal-weight buy-and-hold benchmark, the S1 cost-gate feasibility scan and a +/-20% sensitivity of the top S1 candidate (train and validation separately). Outputs: `reports/backtest_grid.{json,md}`, `reports/backtest_top_{train,validation}.{json,md}`. Deterministic; the worker count does not change the output.

- Fixed split (code constants, not tuned): 50% train / 25% validation / remainder test by 15m bar time, 300-bar embargo (= the strategies' indicator window) between segments, 300-bar warm-up taken from the embargo, entries from the segment's first bar. Warm-up equity points are trimmed before metrics.
- Engine cooldown hook: 3 consecutive losses block entries for 60 min; per symbol 8 bars after an exit (2 after a win). The "same breakout level after a stop-out" and "trend intact" refinements of `CooldownTracker` are not modelled.
- The runner still wraps exit policies in `TickExit`, which rounds trailing/breakeven stop and take-profit levels DOWN to the price tick. It was added when the engine aborted on ~60-digit Decimal levels; the engine (`bt-0.2`) now puts levels on the tick itself, so the wrapper is redundant but kept.
- `reports/` was produced before that fix (`bt-0.1`), with placeholder costs, from the train and validation segments only. Treat `reports/backtest_grid.md` as exploratory, placeholder costs, pre-fix engine; to be re-run.
- The runner never calls `TestSetGuard`. `python -m wallexbot backtest --final-test --params-file frozen.json [--ledger PATH]` is the single-use test evaluation: it refuses without a valid params file (exit 2), validates the file, consumes the guard (ledger default `reports/test_set_ledger.jsonl`) before running, writes `reports/backtest_final_test.{json,md}`, and a second use is refused (exit 3). Frozen file: `{"strategy": "S0|S1", "exit": "E1..E4", "taker_rate": "0.0035", "momentum_params": {...}, "exit_params": {...}}`. Thresholds for judging the result must be written down before it is used.
- `--arm-live` is refused on every command; live is not available in this build.

## Splits and the test set

- `chronological_split` lays out `[train][embargo][validation][embargo][test]` on the bar timestamps. Defaults in code: 60% train, 20% validation, remainder test, with an embargo of at least the longest indicator lookback. These fractions are code defaults, not a committed decision: the actual boundaries must be fixed and committed **before** any run. Changing them after results are seen invalidates the split. `walk_forward` windows also exist.
- Splits refuse negative embargo/lookback/step sizes and verify that segments are disjoint and separated by the embargo.
- `TestSetGuard` enforces the single-use rule with an append-only JSON-lines ledger: a second `consume()` of the same test set raises, across processes. The id (`make_test_set_id(split, symbols, dataset=...)`) is the dataset name, the symbols and the test period in whole UTC days (each end rounded to the nearest midnight), so shifting the window by a bar does not create a "new" test set; the guard also refuses any id of the same dataset that shares a symbol and a test day with one already consumed.
- `sensitivity.py` perturbs each numeric parameter by +/-pct and reports the fraction of neighbours with the same sign of expectancy; it refuses any split except train and validation.
- Grid runs (S0/S1 × E1–E4) use train and validation only. Variant choice is made on validation.
- The **test set is used once**. After the variant and the code are frozen, it is evaluated a single time and the result is recorded.
- Re-running on the test set after any change to code, parameters, or variant is not valid evidence. A new, untouched period is required for that.
- E4 is the **pre-registered leader** in the plan. It is the variant for the single test-set evaluation.

## Evidence required before live

All of the following are required. The plan does not give numeric pass thresholds. **Those thresholds are TBD and must be written down before the test-set evaluation.**

1. Backtest on train and validation completed with frozen code and config.
2. Single test-set evaluation completed and recorded, with thresholds fixed in advance.
3. Shadow run of 24–48 hours (`python -m wallexbot bot --mode shadow`, `ShadowBroker`: sends no orders). The code exists; **no shadow run has been made**.
4. Doctor green (`python -m wallexbot doctor`, needs `WALLEX_API_KEY_READ`; not yet run against Wallex).
5. Explicit go-ahead from the user.
6. Credentials and arming conditions met (see CONFIG_ENV.md).

A passing backtest alone is not evidence for live trading.

## Limitations

- No results exist. Nothing in this repo supports a claim of profitability. The report writer (`report.py`) recomputes every metric itself and renders missing values as `n/a`, but it has only run on synthetic test data.
- Fees, spreads, and minimum notional are placeholders or unverified.
- Candle volume is in base units; only relative volume is used.
- Fill and impact models are approximations. Real fills may differ, especially for market orders in thin books.
- Universe selection by current 24h volume can introduce look-ahead bias. The universe at each point in time must use only data available then.
- Delisted or suspended markets may be missing from the data.
- The sample may be small. Results from a short period will not generalise on their own.
