# Strategy: long-only momentum breakout (S1)

Status: **S1 entry engine, S0 baseline, sizing and exit policies E1-E4 are implemented and unit-tested. They have not been backtested.** Every numeric default below is a **placeholder**, to be chosen on train/validation data only (never the test set). No results are reported anywhere.

Code: `strategy/{momentum,config,features,indicators,cooldown,trend_baseline,lookahead,sizing,exits,contract}.py`. Not implemented: S2 (S1 plus a trend filter), any ICT-based strategy, and any validated parameter set. The bot loop (`bot/`) calls S1 with E4 live in mock and shadow modes only.

## Scope

- Spot, long only, USDT quote, top 10-15 markets by 24h volume (`universe.top_n: 12`).
- Inputs are **closed** bars only. 15m and 4h are aggregated from 1m candles (Wallex has no native 15m/4h series; see WALLEX_INTEGRATION.md). The forming candle is dropped.
- Engines are stateless and look-ahead-safe: they read `bars[-1]` and earlier. Callers hand them a tuple that contains only bars up to the current one (the backtest at most `lookback_bars`). `strategy/lookahead.py` evaluates each cut, then shows the engine the cut continued by wild future bars, then requires the cut's decision to be unchanged (no information carried back through state) and repeatable. Any error or non-finite value means abstain (`None`). On the downloaded Wallex history (BTC/ETH/PEPE, 15m, about 4300 bars each, fee 0.001) S1 with default parameters produced **no** signals; the look-ahead check is exercised on a synthetic breakout with a future present (`tests/test_strategy_baseline_lookahead.py`). A one-off run with relaxed parameters (cost_mult 1, min_score 0.4, roc_z_min 0.5, volume_mult 1.3, max_zero_volume_frac 0.5) found 1/3/6 signal bars on BTC/ETH/PEPE and the check passed at each of them with 200 wild future bars; that run is not part of the test suite (tests do not read `data/`).

## S1 entry rules (`MomentumSignalEngine`, params in `strategy/config.py::MomentumParams`)

All must hold on the last closed bar; otherwise the engine abstains:

1. `breakout`: close > highest high of the prior `donchian_period` (20) bars.
2. `trend`: close > EMA(`ema_period`=50, a **bar** count: 50 x 15m bars is about 12.5h, not 50 hourly bars; use 200 on 15m bars to match).
3. `roc_accel`, `roc_positive`, `roc_z`: ROC(6) is rising, the prior ROC is positive, and the ROC z-score over 96 bars is >= 1.0.
4. `volume`: volume / median of the prior 48 bars >= 1.8 (2.2 when the regime is unfavourable). Abstain if more than 20% of that window has zero volume.
5. `atr_band`: ATR(14)/close in [0.002, 0.03] (placeholders).
6. Not-late filters: close - breakout <= 1.0 ATR; RSI(14) <= 85; advance from the 24-bar low <= 5 ATR.
7. Regime: favourable iff efficiency ratio(20) >= 0.3 AND EMA slope (4 bars) > 0. Otherwise the volume multiple (2.2) and cost multiple (4.0) are tightened.
8. `cost_gate`: `expected_move_atr` (1.5) x ATR in bps >= `cost_mult` (3.0; 4.0 tightened) x round-trip cost bps. The round-trip cost comes from `MarketContext.round_trip_cost_bps`, supplied by the caller.
9. `min_score`: a 0..1 score (weights: volume 0.25, roc_z 0.20, efficiency 0.20, cost 0.20, earliness 0.15; each component is 0.5 at its threshold) must be >= 0.6.

Needs at least `min_bars` = 120 bars (the engine uses the last 300). The signal's stop is `stop_atr_mult` (1.5) ATR below the close. `diagnose()` returns every rule's observed value for the decision log. Abstention is a valid, expected outcome.

**Cooldown** (`CooldownTracker`, kept by the caller): 8 bars after an exit, 2 after a profitable exit with the trend intact; after a stop-out, no re-entry on the same breakout level.

## S0 baseline (`TrendBaselineEngine`)

Benchmark only: close > SMA50, SMA20 > SMA50, close > prior 20-bar high; stop 2.5 ATR below. No volume, cost or regime logic, so it trades more often and is not comparable on selectivity. This is a definition chosen in code; the source plan names S0 but does not define it.

## Position sizing (`strategy/sizing.py`)

`qty = equity x risk_pct / 100 / (entry - stop)`, with `risk_pct` = `risk.risk_per_trade_pct` (1.0), halved when score < 0.7. Clamped by max position (25% of equity), max order value, free cash minus a fee reserve, 1/`min_depth_multiple` of depth (only if a depth figure is given) and optional exposure headroom; rounded **down** to the amount precision. Result is 0 (with a reason) if the stop is not below entry, an input is invalid, or notional is below the larger of `rules.min_notional` and `risk.min_notional` (both unverified).

## Exits E1-E4 (`strategy/exits.py`, `ExitParams` defaults)

The hard stop (the signal's stop) applies to all. `on_bar` runs on each closed bar; its exit executes at the next open. Intrabar stops and targets are the caller's job (stop assumed first if one bar touches both).

- **E1 `FixedRR`**: full take-profit at entry + 1.5 R.
- **E2 `ChandelierTrail`**: once the highest high >= entry + 1 R, trail the stop at highest high - 2 x ATR(14), ratcheting up only.
- **E3 `MomentumFailure`**: exit at the next open after a close below EMA(9), or ROC(3) < 0, or two consecutive volume-dry bars (< 0.5 x prior 20-bar median).
- **E4 `Hybrid`** (pre-registered leader): sell 50% at +1 R, then move the stop to breakeven plus fees plus an exit-cost buffer (`breakeven_buffer_bps`, default 20 bps for spread and impact on the stop fill) and chandelier-trail the rest; E3 exit; false-breakout exit (close back below the breakout level within 2 bars); time stop at 16 bars unless the partial was taken. `fee_rate` 0.0035 is a placeholder.

Exit orders are reduce-only; the kill switch does not block them (RISK.md).

## Profiles (`strategy/profiles.py`, `bot --profile strict|demo`)

- `strict` (default): `MomentumParams()` as above. Under the default 0.2 % taker fee (and even at 0.1 %) with recorded costs the cost gate did not pass on a single train or validation bar (`reports/v2_summary.md`, `assumed_0.002` column), so a strict mock/shadow run usually shows abstentions only.
- `demo`: an **EXERCISE profile, no edge claimed, mock/shadow only**. It exists to make decisions, risk checks, orders, fills, exits and guardian ticks visible. Changes against strict: `cost_mult`/`cost_mult_tight` 0.5/0.5, `volume_mult`/`_tight` 1.2/1.5, `atr_pct_min` 0.0005, `roc_z_min` 0.5, `er_min` 0.2, `min_score` 0.5; nothing else. The values were picked by hand so that signals occur, not selected on any validation data. The bot prints `EXERCISE PROFILE - NO EDGE CLAIMED - SHADOW/PAPER ONLY`, writes `profile` into every bot audit event and the startup run record (the healthcheck reports it), and refuses `demo` for any mode but mock/shadow and for a config with `mode: live`.
- Observed: on the synthetic breakout replay with default fees, strict places no order and demo completes a full entry/exit lifecycle (`tests/test_profiles.py`). On one local replay of recorded October 2026 1m data (BTC/ETH/SOL/XRP, first 4000 simulated minutes, default config, 1000 USDT), strict produced 0 signals and demo 2 signals and 2 round trips, both closed at a loss. That run is not part of the test suite and says nothing about edge.

## What the strategy does not claim

- No profitability, win rate or expected return. No backtest has been run.
- It may produce zero trades in a period; that is correct when filters fail.
