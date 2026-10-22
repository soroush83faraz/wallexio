# Backtest grid (train + validation only) - 60m bars, recorded costs

**EXPLORATORY, UNVALIDATED results: in-sample (train) and validation only, placeholder/assumed costs, ~45 days of one market regime. Not evidence of profitability.**

- git HEAD: `6111708d3dac542f497ad863bd40a69615b5a497` ; inputs hash: `bb5ca8bff364f7fa561265320b2c3ffbcc3f2913267d0e3b47b7db6916d8e021`
- symbols: BTCUSDT, ETHUSDT, PEPEUSDT, SOLUSDT, USOONUSDT, XAUTUSDT, XRPUSDT
- split: 50% train / 25% validation / remainder (~25%) test, by 15m bar time; embargo 300 bars between segments; 300-bar warm-up from the embargo.
  - train: 2026-09-06T21:15:00Z .. 2026-09-26T06:30:00Z (1857 bars)
  - validation: 2026-09-29T09:45:00Z .. 2026-10-09T01:30:00Z (928 bars)
  - test: 2026-10-12T04:45:00Z .. 2026-10-21T21:00:00Z (930 bars) (RESERVED, not evaluated)
- 60m bars per run (all symbols): train 3241 scored + 0 warm-up; validation 1617 scored + 525 warm-up

Reading notes: a max drawdown near 8% is the RiskConfig soft-drawdown limit (`drawdown_soft_pct` 8): new entries are blocked after it, which is why many losing runs end at about -8%. Costs are the per-symbol recorded model (see the cost table below); impact is NOT at a proxy cap. Metrics exclude warm-up bars; 'bars' in the per-run reports counts the scored segment, the header's bar count is 15m bars. Trade counts below 30 are flagged `(n<30)`: too few for any statistical statement.

Fee scenarios:
- `primary_0.0035` taker 0.0035: PLACEHOLDER taker fee (config fees.taker_rate), unverified
- `assumed_0.002` taker 0.002: ASSUMED fee scenario - real fee unverified
- `assumed_0.001` taker 0.001: ASSUMED fee scenario - real fee unverified

## Cost model per symbol

Calibration `cost_calibration.json` sha256 `3ed8c7993ededa96f526037a1db264269f84d0adad7a8c925326749d845e6cdc`, quantile p90. **LOW CONFIDENCE. These numbers describe only the recorded snapshots: a few hours of one or two sessions, top 10 levels, ~15-35 s sampling. Spreads and depth vary by hour of day, weekday, volatility regime and news; a quiet-hours p90 can be far below a volatile-hours median. Hidden or deeper liquidity, queue dynamics, latency and our own market impact on subsequent snapshots are not captured. Refresh after several days covering all UTC hours before relying on them.**

| symbol | source | half spread (bps) | impact at 1000 USDT (bps) | impact curve (USDT: bps) | cap | round trip @primary_0.0035 (bps) | round trip @assumed_0.002 (bps) | round trip @assumed_0.001 (bps) |
|---|---|---|---|---|---|---|---|---|
| BTCUSDT | recorded (p90) | 11.68 | 6.67 (interpolated) | 250: 0.23, 500: 0.58, 1000: 6.67 | 100 | 106.7 | 76.7 | 56.7 |
| ETHUSDT | recorded (p90) | 26.62 | 19.90 (interpolated) | 250: 1.30, 500: 5.66, 1000: 19.90, 2500: 48.54 | 100 | 163.0 | 133.0 | 113.0 |
| PEPEUSDT | recorded (p90) | 42.92 | 100.00 (capped) | 250: 102.78, 500: 105.63, 1000: 114.19, 2500: 228.95 | 100 | 355.8 | 325.8 | 305.8 |
| SOLUSDT | recorded (p90) | 15.65 | 26.84 (interpolated) | 250: 4.79, 500: 17.94, 1000: 26.84, 2500: 42.94 | 100 | 155.0 | 125.0 | 105.0 |
| USOONUSDT | recorded (p90) | 25.41 | 19.99 (interpolated) | 250: 8.79, 500: 16.26, 1000: 19.99, 2500: 25.58 | 100 | 160.8 | 130.8 | 110.8 |
| XAUTUSDT | recorded (p90) | 19.76 | 9.02 (interpolated) | 250: 0.70, 500: 1.20, 1000: 9.02 | 100 | 127.6 | 97.6 | 77.6 |
| XRPUSDT | recorded (p90) | 16.14 | 24.55 (interpolated) | 250: 9.98, 500: 16.97, 1000: 24.55, 2500: 44.15 | 100 | 151.4 | 121.4 | 101.4 |

## Train results

| strategy | exit | fee | net return | max DD | trades | win rate | PF | exp (R) | Sharpe | signals/evals | fees | spread | impact | gross P&L |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| S0 | E1 | primary_0.0035 | -6.25% | 6.28% | 36 | 36.11% | 0.334 | -0.597 | -11.7 | 75/2650 | 250.7 | 171.7 | 248.5 | 46.2 |
| S0 | E2 | primary_0.0035 | -3.86% | 3.89% | 32 | 37.50% | 0.521 | -0.365 | -5.79 | 64/2801 | 223.4 | 155.2 | 230.7 | 223.0 |
| S0 | E3 | primary_0.0035 | -8.17% | 8.17% | 47 | 12.77% | 0.117 | -0.642 | -21.4 | 99/3053 | 327.3 | 207.7 | 305.3 | 23.5 |
| S0 | E4 | primary_0.0035 | -7.49% | 7.49% | 51 | 27.45% | 0.148 | -0.567 | -20.6 | 91/3066 | 355.6 | 233.1 | 347.5 | 187.7 |
| S0 | E1 | assumed_0.002 | -5.17% | 5.30% | 36 | 36.11% | 0.405 | -0.458 | -9.91 | 75/2650 | 143.2 | 171.7 | 248.5 | 46.2 |
| S0 | E2 | assumed_0.002 | -3.15% | 3.32% | 33 | 39.39% | 0.592 | -0.236 | -4.8 | 64/2790 | 131.6 | 158.3 | 236.0 | 211.1 |
| S0 | E3 | assumed_0.002 | -6.77% | 6.77% | 47 | 12.77% | 0.157 | -0.496 | -18.7 | 99/3053 | 187.0 | 207.7 | 305.3 | 23.5 |
| S0 | E4 | assumed_0.002 | -5.73% | 5.73% | 50 | 28.00% | 0.229 | -0.408 | -16.7 | 91/3065 | 199.2 | 227.9 | 343.6 | 197.9 |
| S0 | E1 | assumed_0.001 | -4.46% | 4.64% | 36 | 36.11% | 0.46 | -0.365 | -8.63 | 75/2650 | 71.6 | 171.7 | 248.5 | 46.2 |
| S0 | E2 | assumed_0.001 | -2.49% | 3.09% | 33 | 39.39% | 0.66 | -0.141 | -3.84 | 64/2790 | 65.8 | 158.3 | 236.0 | 211.1 |
| S0 | E3 | assumed_0.001 | -5.83% | 5.83% | 47 | 12.77% | 0.192 | -0.398 | -16.7 | 99/3053 | 93.5 | 207.7 | 305.3 | 23.5 |
| S0 | E4 | assumed_0.001 | -4.74% | 4.74% | 50 | 28.00% | 0.293 | -0.31 | -14.3 | 91/3065 | 99.6 | 227.9 | 343.6 | 196.8 |
| S1 | E1 | primary_0.0035 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a | 0/3241 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E2 | primary_0.0035 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a | 0/3241 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E3 | primary_0.0035 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a | 0/3241 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E4 | primary_0.0035 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a | 0/3241 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E1 | assumed_0.002 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a | 0/3241 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E2 | assumed_0.002 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a | 0/3241 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E3 | assumed_0.002 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a | 0/3241 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E4 | assumed_0.002 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a | 0/3241 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E1 | assumed_0.001 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a | 0/3241 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E2 | assumed_0.001 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a | 0/3241 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E3 | assumed_0.001 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a | 0/3241 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E4 | assumed_0.001 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a | 0/3241 | 0.0 | 0.0 | 0.0 | 0.0 |

Cost shares of |gross P&L| (fee / spread / impact), abstentions, how fills were priced, events:

| strategy | exit | fee | fee share | spread share | impact share | abstentions | fill pricing | events |
|---|---|---|---|---|---|---|---|---|
| S0 | E1 | primary_0.0035 | 5.43 | 3.72 | 5.38 | 2575 | capped=12, extrapolated=3, interpolated=57 | entry_rejected:cooldown=8, entry_rejected:max_open_positions=31 |
| S0 | E2 | primary_0.0035 | 1 | 0.696 | 1.03 | 2737 | capped=12, extrapolated=3, interpolated=49 | entry_rejected:cooldown=1, entry_rejected:max_open_positions=31 |
| S0 | E3 | primary_0.0035 | 13.9 | 8.85 | 13 | 2954 | capped=14, extrapolated=4, interpolated=76 | entry_rejected:cooldown=34, entry_rejected:max_open_positions=18 |
| S0 | E4 | primary_0.0035 | 1.89 | 1.24 | 1.85 | 2975 | capped=19, extrapolated=1, interpolated=95 | entry_rejected:cooldown=21, entry_rejected:max_open_positions=19 |
| S0 | E1 | assumed_0.002 | 3.1 | 3.72 | 5.38 | 2575 | capped=12, extrapolated=3, interpolated=57 | entry_rejected:cooldown=8, entry_rejected:max_open_positions=31 |
| S0 | E2 | assumed_0.002 | 0.624 | 0.75 | 1.12 | 2726 | capped=12, extrapolated=3, interpolated=51 | entry_rejected:max_open_positions=31 |
| S0 | E3 | assumed_0.002 | 7.97 | 8.85 | 13 | 2954 | capped=14, extrapolated=4, interpolated=76 | entry_rejected:cooldown=34, entry_rejected:max_open_positions=18 |
| S0 | E4 | assumed_0.002 | 1.01 | 1.15 | 1.74 | 2974 | capped=19, extrapolated=1, interpolated=93 | entry_rejected:cooldown=21, entry_rejected:max_open_positions=20 |
| S0 | E1 | assumed_0.001 | 1.55 | 3.72 | 5.38 | 2575 | capped=12, extrapolated=3, interpolated=57 | entry_rejected:cooldown=8, entry_rejected:max_open_positions=31 |
| S0 | E2 | assumed_0.001 | 0.312 | 0.75 | 1.12 | 2726 | capped=12, extrapolated=3, interpolated=51 | entry_rejected:max_open_positions=31 |
| S0 | E3 | assumed_0.001 | 3.98 | 8.85 | 13 | 2954 | capped=14, extrapolated=4, interpolated=76 | entry_rejected:cooldown=34, entry_rejected:max_open_positions=18 |
| S0 | E4 | assumed_0.001 | 0.506 | 1.16 | 1.75 | 2974 | capped=19, extrapolated=1, interpolated=93 | entry_rejected:cooldown=21, entry_rejected:max_open_positions=20 |
| S1 | E1 | primary_0.0035 | n/a | n/a | n/a | 3241 | - | - |
| S1 | E2 | primary_0.0035 | n/a | n/a | n/a | 3241 | - | - |
| S1 | E3 | primary_0.0035 | n/a | n/a | n/a | 3241 | - | - |
| S1 | E4 | primary_0.0035 | n/a | n/a | n/a | 3241 | - | - |
| S1 | E1 | assumed_0.002 | n/a | n/a | n/a | 3241 | - | - |
| S1 | E2 | assumed_0.002 | n/a | n/a | n/a | 3241 | - | - |
| S1 | E3 | assumed_0.002 | n/a | n/a | n/a | 3241 | - | - |
| S1 | E4 | assumed_0.002 | n/a | n/a | n/a | 3241 | - | - |
| S1 | E1 | assumed_0.001 | n/a | n/a | n/a | 3241 | - | - |
| S1 | E2 | assumed_0.001 | n/a | n/a | n/a | 3241 | - | - |
| S1 | E3 | assumed_0.001 | n/a | n/a | n/a | 3241 | - | - |
| S1 | E4 | assumed_0.001 | n/a | n/a | n/a | 3241 | - | - |

## Validation results

| strategy | exit | fee | net return | max DD | trades | win rate | PF | exp (R) | Sharpe | signals/evals | fees | spread | impact | gross P&L |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| S0 | E1 | primary_0.0035 | -5.42% | 5.42% | 19 (n<30) | 10.53% | 0.0272 | -1.46 | -22.8 | 31/1505 | 131.6 | 84.3 | 117.5 | -208.8 |
| S0 | E2 | primary_0.0035 | -5.19% | 5.19% | 18 (n<30) | 11.11% | 0.0103 | -1.38 | -21.8 | 29/1524 | 124.6 | 80.4 | 115.8 | -198.1 |
| S0 | E3 | primary_0.0035 | -5.38% | 5.38% | 21 (n<30) | 0.00% | 0 | -1.32 | -23.9 | 32/1564 | 145.6 | 94.8 | 125.6 | -172.1 |
| S0 | E4 | primary_0.0035 | -5.29% | 5.29% | 21 (n<30) | 0.00% | 0 | -1.26 | -25.5 | 37/1574 | 145.7 | 94.8 | 125.0 | -163.0 |
| S0 | E1 | assumed_0.002 | -4.86% | 4.86% | 19 (n<30) | 10.53% | 0.0418 | -1.26 | -21.7 | 31/1505 | 75.2 | 84.3 | 117.5 | -208.8 |
| S0 | E2 | assumed_0.002 | -4.65% | 4.65% | 18 (n<30) | 11.11% | 0.0239 | -1.19 | -20.8 | 29/1524 | 71.2 | 80.4 | 115.8 | -198.1 |
| S0 | E3 | assumed_0.002 | -4.76% | 4.76% | 21 (n<30) | 0.00% | 0 | -1.12 | -22.7 | 32/1564 | 83.2 | 94.8 | 125.6 | -172.1 |
| S0 | E4 | assumed_0.002 | -4.64% | 4.64% | 21 (n<30) | 4.76% | 0.00812 | -1.06 | -24.3 | 34/1571 | 83.2 | 94.8 | 125.0 | -161.4 |
| S0 | E1 | assumed_0.001 | -4.48% | 4.48% | 19 (n<30) | 10.53% | 0.0533 | -1.13 | -20.8 | 31/1505 | 37.6 | 84.3 | 117.5 | -208.8 |
| S0 | E2 | assumed_0.001 | -4.30% | 4.30% | 18 (n<30) | 11.11% | 0.0347 | -1.06 | -20.1 | 29/1524 | 35.6 | 80.4 | 115.8 | -198.1 |
| S0 | E3 | assumed_0.001 | -4.34% | 4.34% | 21 (n<30) | 0.00% | 0 | -0.992 | -21.9 | 32/1564 | 41.6 | 94.8 | 125.6 | -172.1 |
| S0 | E4 | assumed_0.001 | -4.23% | 4.23% | 21 (n<30) | 4.76% | 0.0136 | -0.927 | -23.3 | 34/1571 | 41.6 | 94.8 | 125.0 | -161.4 |
| S1 | E1 | primary_0.0035 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a | 0/1617 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E2 | primary_0.0035 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a | 0/1617 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E3 | primary_0.0035 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a | 0/1617 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E4 | primary_0.0035 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a | 0/1617 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E1 | assumed_0.002 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a | 0/1617 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E2 | assumed_0.002 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a | 0/1617 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E3 | assumed_0.002 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a | 0/1617 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E4 | assumed_0.002 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a | 0/1617 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E1 | assumed_0.001 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a | 0/1617 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E2 | assumed_0.001 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a | 0/1617 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E3 | assumed_0.001 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a | 0/1617 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E4 | assumed_0.001 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a | 0/1617 | 0.0 | 0.0 | 0.0 | 0.0 |

Cost shares of |gross P&L| (fee / spread / impact), abstentions, how fills were priced, events:

| strategy | exit | fee | fee share | spread share | impact share | abstentions | fill pricing | events |
|---|---|---|---|---|---|---|---|---|
| S0 | E1 | primary_0.0035 | 0.63 | 0.404 | 0.563 | 1474 | capped=6, extrapolated=2, interpolated=30 | entry_rejected:cooldown=7, entry_rejected:max_open_positions=5 |
| S0 | E2 | primary_0.0035 | 0.629 | 0.406 | 0.585 | 1495 | capped=6, extrapolated=2, interpolated=28 | entry_rejected:cooldown=5, entry_rejected:max_open_positions=6 |
| S0 | E3 | primary_0.0035 | 0.846 | 0.551 | 0.73 | 1532 | capped=6, extrapolated=1, interpolated=35 | entry_rejected:cooldown=9, entry_rejected:max_open_positions=2 |
| S0 | E4 | primary_0.0035 | 0.893 | 0.581 | 0.767 | 1537 | capped=6, interpolated=37 | entry_rejected:cooldown=14, entry_rejected:max_open_positions=2 |
| S0 | E1 | assumed_0.002 | 0.36 | 0.404 | 0.563 | 1474 | capped=6, extrapolated=2, interpolated=30 | entry_rejected:cooldown=7, entry_rejected:max_open_positions=5 |
| S0 | E2 | assumed_0.002 | 0.36 | 0.406 | 0.585 | 1495 | capped=6, extrapolated=2, interpolated=28 | entry_rejected:cooldown=5, entry_rejected:max_open_positions=6 |
| S0 | E3 | assumed_0.002 | 0.483 | 0.551 | 0.73 | 1532 | capped=6, extrapolated=1, interpolated=35 | entry_rejected:cooldown=9, entry_rejected:max_open_positions=2 |
| S0 | E4 | assumed_0.002 | 0.516 | 0.587 | 0.775 | 1537 | capped=6, interpolated=37 | entry_rejected:cooldown=11, entry_rejected:max_open_positions=2 |
| S0 | E1 | assumed_0.001 | 0.18 | 0.404 | 0.563 | 1474 | capped=6, extrapolated=2, interpolated=30 | entry_rejected:cooldown=7, entry_rejected:max_open_positions=5 |
| S0 | E2 | assumed_0.001 | 0.18 | 0.406 | 0.585 | 1495 | capped=6, extrapolated=2, interpolated=28 | entry_rejected:cooldown=5, entry_rejected:max_open_positions=6 |
| S0 | E3 | assumed_0.001 | 0.242 | 0.551 | 0.73 | 1532 | capped=6, extrapolated=1, interpolated=35 | entry_rejected:cooldown=9, entry_rejected:max_open_positions=2 |
| S0 | E4 | assumed_0.001 | 0.258 | 0.587 | 0.775 | 1537 | capped=6, interpolated=37 | entry_rejected:cooldown=11, entry_rejected:max_open_positions=2 |
| S1 | E1 | primary_0.0035 | n/a | n/a | n/a | 1617 | - | - |
| S1 | E2 | primary_0.0035 | n/a | n/a | n/a | 1617 | - | - |
| S1 | E3 | primary_0.0035 | n/a | n/a | n/a | 1617 | - | - |
| S1 | E4 | primary_0.0035 | n/a | n/a | n/a | 1617 | - | - |
| S1 | E1 | assumed_0.002 | n/a | n/a | n/a | 1617 | - | - |
| S1 | E2 | assumed_0.002 | n/a | n/a | n/a | 1617 | - | - |
| S1 | E3 | assumed_0.002 | n/a | n/a | n/a | 1617 | - | - |
| S1 | E4 | assumed_0.002 | n/a | n/a | n/a | 1617 | - | - |
| S1 | E1 | assumed_0.001 | n/a | n/a | n/a | 1617 | - | - |
| S1 | E2 | assumed_0.001 | n/a | n/a | n/a | 1617 | - | - |
| S1 | E3 | assumed_0.001 | n/a | n/a | n/a | 1617 | - | - |
| S1 | E4 | assumed_0.001 | n/a | n/a | n/a | 1617 | - | - |

## Buy-and-hold benchmark (equal weight over the universe, same costs)

Each symbol is bought with the whole 10000 USDT at the segment's first open and sold at its last close; with recorded costs a 10000 USDT order is beyond the calibrated notionals (extrapolated or capped impact).

| split | fee | mean net return | frictionless return | worst per-symbol max DD |
|---|---|---|---|---|
| train | primary_0.0035 | 5.44% | 8.69% | 18.89% |
| train | assumed_0.002 | 5.76% | 8.69% | 18.89% |
| train | assumed_0.001 | 5.97% | 8.69% | 18.89% |
| validation | primary_0.0035 | -8.50% | -5.70% | 22.65% |
| validation | assumed_0.002 | -8.22% | -5.70% | 22.65% |
| validation | assumed_0.001 | -8.04% | -5.70% | 22.65% |

## S1 cost-gate feasibility

fraction of 60m bars (train+validation segments only, features available) where the S1 cost gate passes, with each symbol's own cost model; regime-dependent threshold (3x favourable, 4x tightened).

Proxy-model reference: ATR/close needed for the gate to pass (S1 atr band is [0.002, 0.03]):

| fee | round-trip cost (bps) | ATR% needed (favourable) | ATR% needed (tightened) |
|---|---|---|---|
| primary_0.0035 | 135 | 2.70% | 3.60% |
| assumed_0.002 | 105 | 2.10% | 2.80% |
| assumed_0.001 | 85 | 1.70% | 2.27% |

Per symbol, with its own cost model: round-trip cost the gate sees (bps) / ATR% needed (favourable regime):

| symbol | cost source | primary_0.0035 | assumed_0.002 | assumed_0.001 |
|---|---|---|---|---|
| BTCUSDT | recorded | 107 / 2.13% | 77 / 1.53% | 57 / 1.13% |
| ETHUSDT | recorded | 163 / 3.26% | 133 / 2.66% | 113 / 2.26% |
| PEPEUSDT | recorded | 356 / 7.12% | 326 / 6.52% | 306 / 6.12% |
| SOLUSDT | recorded | 155 / 3.10% | 125 / 2.50% | 105 / 2.10% |
| USOONUSDT | recorded | 161 / 3.22% | 131 / 2.62% | 111 / 2.22% |
| XAUTUSDT | recorded | 128 / 2.55% | 98 / 1.95% | 78 / 1.55% |
| XRPUSDT | recorded | 151 / 3.03% | 121 / 2.43% | 101 / 2.03% |

Fraction of usable-feature bars where the cost gate passes (bars without usable features, e.g. too many zero-volume bars or a zero median volume, make S1 abstain regardless):

| symbol | bars in train+val | usable-feature bars | median ATR% | p90 ATR% | max ATR% | primary_0.0035 | assumed_0.002 | assumed_0.001 | other rules pass (no cost gate) |
|---|---|---|---|---|---|---|---|---|---|
| BTCUSDT | 696 | 578 | 0.47% | 0.62% | 0.80% | 0.00% (0) | 0.00% (0) | 0.00% (0) | 4 |
| ETHUSDT | 696 | 578 | 0.67% | 0.94% | 1.30% | 0.00% (0) | 0.00% (0) | 0.00% (0) | 6 |
| PEPEUSDT | 696 | 578 | 1.20% | 1.97% | 3.32% | 0.00% (0) | 0.00% (0) | 0.00% (0) | 0 |
| SOLUSDT | 696 | 578 | 0.72% | 0.96% | 1.18% | 0.00% (0) | 0.00% (0) | 0.00% (0) | 5 |
| USOONUSDT | 696 | 578 | 0.79% | 1.21% | 1.75% | 0.00% (0) | 0.00% (0) | 0.00% (0) | 3 |
| XAUTUSDT | 696 | 578 | 0.25% | 0.35% | 0.45% | 0.00% (0) | 0.00% (0) | 0.00% (0) | 3 |
| XRPUSDT | 696 | 578 | 0.89% | 1.30% | 1.94% | 0.00% (0) | 0.00% (0) | 0.00% (0) | 1 |

## Top candidate: S1+E4 at primary_0.0035

best S1 validation net return at the primary fee among variants with trades; ties/none -> pre-registered E4.

- sensitivity on train (+/-0.2): base expectancy (R) n/a, same-sign fraction n/a over 22 neighbours
- sensitivity on validation (+/-0.2): base expectancy (R) n/a, same-sign fraction n/a over 22 neighbours

## Data quality (15m bars)

| symbol | bars | bars with missing minutes | zero-volume bars | rules |
|---|---|---|---|---|
| BTCUSDT | 4314 | 8 | 394 | fixture: amount 8, price 2; 1h: 1079 bars, 7 with missing minutes |
| ETHUSDT | 4303 | 10 | 997 | fixture: amount 5, price 2; 1h: 1077 bars, 9 with missing minutes |
| XRPUSDT | 4313 | 9 | 1040 | fixture: amount 1, price 5; 1h: 1079 bars, 8 with missing minutes |
| XAUTUSDT | 4308 | 9 | 1400 | derived: amount 5, price 2; 1h: 1078 bars, 10 with missing minutes |
| USOONUSDT | 4304 | 12 | 1211 | derived: amount 4, price 2; 1h: 1077 bars, 11 with missing minutes |
| PEPEUSDT | 4315 | 9 | 975 | derived: amount 0, price 8; 1h: 1079 bars, 8 with missing minutes |
| SOLUSDT | 4312 | 12 | 887 | fixture: amount 5, price 2; 1h: 1079 bars, 11 with missing minutes |

## Limitations

EXPLORATORY, UNVALIDATED results: in-sample (train) and validation only, placeholder/assumed costs, ~45 days of one market regime. Not evidence of profitability. The universe was ranked by CURRENT 24h volume (selection bias). Window ~45 days. Costs: per-symbol p90 half spread and a p90 impact curve calibrated from a few hours of recorded order books (one or two sessions, top 10 levels; NOT representative of all hours or regimes; see docs/COSTS.md); symbols without a usable calibration use the proxy model and are labelled. Fills at next-bar open/level touch; take-profits also pay taker costs. Fees are placeholders/assumptions charged in quote (Wallex fee unverified). The S1 cost gate sees the recorded full spread plus the round-trip impact at 1000 USDT. 60m bars over ~45 days give few bars per segment (S0/S1 need 50-120 bars of history first) and therefore few trades and low statistical power. Warm-up bars before each segment are excluded from metrics. Missing-minute bars are kept and counted. No parameters were tuned.
