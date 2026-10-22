# Backtest grid (train + validation only) - 15m bars, recorded costs

**EXPLORATORY, UNVALIDATED results: in-sample (train) and validation only, placeholder/assumed costs, ~45 days of one market regime. Not evidence of profitability.**

- git HEAD: `6111708d3dac542f497ad863bd40a69615b5a497` ; inputs hash: `7ac6c21ede31622070f33c94624a707bc8c13059d43abe4c8164db94aef996fb`
- symbols: BTCUSDT, ETHUSDT, PEPEUSDT, SOLUSDT, USOONUSDT, XAUTUSDT, XRPUSDT
- split: 50% train / 25% validation / remainder (~25%) test, by 15m bar time; embargo 300 bars between segments; 300-bar warm-up from the embargo.
  - train: 2026-09-06T21:15:00Z .. 2026-09-26T06:30:00Z (1857 bars)
  - validation: 2026-09-29T09:45:00Z .. 2026-10-09T01:30:00Z (928 bars)
  - test: 2026-10-12T04:45:00Z .. 2026-10-21T21:00:00Z (930 bars) (RESERVED, not evaluated)
- 15m bars per run (all symbols): train 12998 scored + 0 warm-up; validation 6496 scored + 2100 warm-up

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
| S0 | E1 | primary_0.0035 | -8.23% | 8.23% | 57 | 26.32% | 0.127 | -1.61 | -20.6 | 448/12267 | 397.5 | 251.6 | 319.0 | 145.1 |
| S0 | E2 | primary_0.0035 | -8.37% | 8.37% | 54 | 14.81% | 0.0995 | -1.63 | -18.3 | 444/12468 | 376.4 | 246.1 | 324.2 | 109.4 |
| S0 | E3 | primary_0.0035 | -8.37% | 8.37% | 49 | 4.08% | 0.0366 | -1.77 | -24.8 | 497/12840 | 341.3 | 226.0 | 290.8 | 21.0 |
| S0 | E4 | primary_0.0035 | -8.13% | 8.13% | 47 | 6.38% | 0.0269 | -1.77 | -28.2 | 502/12866 | 327.3 | 218.4 | 286.6 | 19.2 |
| S0 | E1 | assumed_0.002 | -8.55% | 8.55% | 79 | 31.65% | 0.265 | -1.07 | -16.7 | 373/11484 | 314.9 | 361.8 | 511.1 | 332.6 |
| S0 | E2 | assumed_0.002 | -8.61% | 8.61% | 74 | 22.97% | 0.178 | -1.18 | -16.4 | 381/11821 | 294.9 | 328.0 | 438.4 | 200.1 |
| S0 | E3 | assumed_0.002 | -8.11% | 8.11% | 58 | 5.17% | 0.048 | -1.35 | -24 | 485/12802 | 230.8 | 262.1 | 346.8 | 28.9 |
| S0 | E4 | assumed_0.002 | -8.84% | 8.84% | 60 | 6.67% | 0.036 | -1.38 | -28.9 | 493/12832 | 238.7 | 270.6 | 355.4 | -19.2 |
| S0 | E1 | assumed_0.001 | -8.15% | 8.32% | 104 | 35.58% | 0.397 | -0.727 | -12.8 | 297/10789 | 207.4 | 471.8 | 658.4 | 522.9 |
| S0 | E2 | assumed_0.001 | -8.10% | 8.10% | 80 | 25.00% | 0.231 | -0.977 | -15.4 | 360/11690 | 159.3 | 356.4 | 492.5 | 198.6 |
| S0 | E3 | assumed_0.001 | -8.26% | 8.26% | 64 | 7.81% | 0.0559 | -1.16 | -24.4 | 484/12793 | 127.3 | 292.2 | 388.8 | -17.2 |
| S0 | E4 | assumed_0.001 | -8.33% | 8.33% | 63 | 6.35% | 0.047 | -1.18 | -28 | 492/12828 | 125.3 | 289.1 | 383.3 | -35.0 |
| S1 | E1 | primary_0.0035 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a | 0/12998 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E2 | primary_0.0035 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a | 0/12998 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E3 | primary_0.0035 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a | 0/12998 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E4 | primary_0.0035 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a | 0/12998 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E1 | assumed_0.002 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a | 0/12998 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E2 | assumed_0.002 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a | 0/12998 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E3 | assumed_0.002 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a | 0/12998 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E4 | assumed_0.002 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a | 0/12998 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E1 | assumed_0.001 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a | 0/12998 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E2 | assumed_0.001 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a | 0/12998 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E3 | assumed_0.001 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a | 0/12998 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E4 | assumed_0.001 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a | 0/12998 | 0.0 | 0.0 | 0.0 | 0.0 |

Cost shares of |gross P&L| (fee / spread / impact), abstentions, how fills were priced, events:

| strategy | exit | fee | fee share | spread share | impact share | abstentions | fill pricing | events |
|---|---|---|---|---|---|---|---|---|
| S0 | E1 | primary_0.0035 | 2.74 | 1.73 | 2.2 | 11819 | capped=12, extrapolated=2, interpolated=100 | entry_rejected:cooldown=28, entry_rejected:max_open_positions=16, signal_blocked:drawdown_soft=347 |
| S0 | E2 | primary_0.0035 | 3.44 | 2.25 | 2.96 | 12024 | capped=14, extrapolated=2, interpolated=92 | entry_rejected:cooldown=21, entry_rejected:max_open_positions=16, signal_blocked:drawdown_soft=353 |
| S0 | E3 | primary_0.0035 | 16.3 | 10.8 | 13.9 | 12343 | capped=14, interpolated=84 | entry_rejected:cooldown=43, entry_rejected:max_open_positions=6, signal_blocked:drawdown_soft=399 |
| S0 | E4 | primary_0.0035 | 17.1 | 11.4 | 15 | 12364 | capped=15, interpolated=82 | entry_rejected:cooldown=44, entry_rejected:max_open_positions=5, signal_blocked:drawdown_soft=406 |
| S0 | E1 | assumed_0.002 | 0.947 | 1.09 | 1.54 | 11111 | capped=24, extrapolated=4, interpolated=130 | entry_rejected:cooldown=37, entry_rejected:max_open_positions=63, signal_blocked:drawdown_soft=194 |
| S0 | E2 | assumed_0.002 | 1.47 | 1.64 | 2.19 | 11440 | capped=18, extrapolated=4, interpolated=126 | entry_rejected:cooldown=26, entry_rejected:max_open_positions=35, signal_blocked:drawdown_soft=246 |
| S0 | E3 | assumed_0.002 | 7.98 | 9.06 | 12 | 12317 | capped=16, extrapolated=1, interpolated=99 | entry_rejected:cooldown=57, entry_rejected:max_open_positions=8, signal_blocked:drawdown_soft=362 |
| S0 | E4 | assumed_0.002 | 12.4 | 14.1 | 18.5 | 12339 | capped=17, interpolated=107 | entry_rejected:cooldown=70, entry_rejected:max_open_positions=6, signal_blocked:drawdown_soft=357 |
| S0 | E1 | assumed_0.001 | 0.397 | 0.902 | 1.26 | 10492 | capped=30, extrapolated=7, interpolated=171 | entry_rejected:cooldown=48, entry_rejected:max_open_positions=106, signal_blocked:drawdown_soft=39 |
| S0 | E2 | assumed_0.001 | 0.802 | 1.79 | 2.48 | 11330 | capped=22, extrapolated=5, interpolated=133 | entry_rejected:cooldown=31, entry_rejected:max_open_positions=55, signal_blocked:drawdown_soft=194 |
| S0 | E3 | assumed_0.001 | 7.39 | 17 | 22.6 | 12309 | capped=18, extrapolated=1, interpolated=109 | entry_rejected:cooldown=59, entry_rejected:max_open_positions=8, signal_blocked:drawdown_soft=353 |
| S0 | E4 | assumed_0.001 | 3.58 | 8.25 | 10.9 | 12336 | capped=19, interpolated=111 | entry_rejected:cooldown=70, entry_rejected:max_open_positions=6, signal_blocked:drawdown_soft=353 |
| S1 | E1 | primary_0.0035 | n/a | n/a | n/a | 12998 | - | - |
| S1 | E2 | primary_0.0035 | n/a | n/a | n/a | 12998 | - | - |
| S1 | E3 | primary_0.0035 | n/a | n/a | n/a | 12998 | - | - |
| S1 | E4 | primary_0.0035 | n/a | n/a | n/a | 12998 | - | - |
| S1 | E1 | assumed_0.002 | n/a | n/a | n/a | 12998 | - | - |
| S1 | E2 | assumed_0.002 | n/a | n/a | n/a | 12998 | - | - |
| S1 | E3 | assumed_0.002 | n/a | n/a | n/a | 12998 | - | - |
| S1 | E4 | assumed_0.002 | n/a | n/a | n/a | 12998 | - | - |
| S1 | E1 | assumed_0.001 | n/a | n/a | n/a | 12998 | - | - |
| S1 | E2 | assumed_0.001 | n/a | n/a | n/a | 12998 | - | - |
| S1 | E3 | assumed_0.001 | n/a | n/a | n/a | 12998 | - | - |
| S1 | E4 | assumed_0.001 | n/a | n/a | n/a | 12998 | - | - |

## Validation results

| strategy | exit | fee | net return | max DD | trades | win rate | PF | exp (R) | Sharpe | signals/evals | fees | spread | impact | gross P&L |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| S0 | E1 | primary_0.0035 | -8.18% | 8.18% | 43 | 16.28% | 0.0291 | -1.89 | -31.2 | 158/5602 | 299.2 | 186.0 | 245.7 | -87.2 |
| S0 | E2 | primary_0.0035 | -8.04% | 8.14% | 47 | 10.64% | 0.0311 | -1.91 | -32.9 | 157/5788 | 327.3 | 197.0 | 232.5 | -47.2 |
| S0 | E3 | primary_0.0035 | -8.09% | 8.09% | 44 | 2.27% | 0.00657 | -1.85 | -36.2 | 206/6358 | 306.2 | 184.3 | 250.9 | -67.9 |
| S0 | E4 | primary_0.0035 | -8.16% | 8.16% | 44 | 2.27% | 0.00122 | -1.88 | -37.6 | 211/6380 | 306.2 | 184.3 | 250.2 | -75.5 |
| S0 | E1 | assumed_0.002 | -8.28% | 8.30% | 50 | 14.00% | 0.0522 | -1.6 | -32.2 | 147/5514 | 198.7 | 219.8 | 290.1 | -119.6 |
| S0 | E2 | assumed_0.002 | -8.14% | 8.28% | 55 | 10.91% | 0.0479 | -1.62 | -34.3 | 143/5626 | 218.8 | 234.8 | 278.6 | -81.6 |
| S0 | E3 | assumed_0.002 | -8.34% | 8.34% | 56 | 1.79% | 0.00994 | -1.46 | -39.6 | 192/6314 | 222.8 | 230.0 | 312.2 | -69.0 |
| S0 | E4 | assumed_0.002 | -8.17% | 8.17% | 54 | 3.70% | 0.00519 | -1.47 | -39.5 | 198/6342 | 214.8 | 224.4 | 304.9 | -72.7 |
| S0 | E1 | assumed_0.001 | -8.05% | 8.10% | 56 | 12.50% | 0.069 | -1.38 | -31.7 | 136/5472 | 111.3 | 246.1 | 310.6 | -136.6 |
| S0 | E2 | assumed_0.001 | -7.60% | 7.76% | 60 | 13.33% | 0.0683 | -1.38 | -32.4 | 125/5570 | 119.4 | 258.7 | 297.8 | -83.8 |
| S0 | E3 | assumed_0.001 | -8.04% | 8.04% | 61 | 3.28% | 0.0144 | -1.25 | -39.8 | 193/6306 | 121.3 | 253.7 | 343.4 | -85.5 |
| S0 | E4 | assumed_0.001 | -8.05% | 8.05% | 60 | 3.33% | 0.0102 | -1.24 | -41.6 | 196/6326 | 119.3 | 251.3 | 340.9 | -93.3 |
| S1 | E1 | primary_0.0035 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a | 0/6496 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E2 | primary_0.0035 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a | 0/6496 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E3 | primary_0.0035 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a | 0/6496 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E4 | primary_0.0035 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a | 0/6496 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E1 | assumed_0.002 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a | 0/6496 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E2 | assumed_0.002 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a | 0/6496 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E3 | assumed_0.002 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a | 0/6496 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E4 | assumed_0.002 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a | 0/6496 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E1 | assumed_0.001 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a | 0/6496 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E2 | assumed_0.001 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a | 0/6496 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E3 | assumed_0.001 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a | 0/6496 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E4 | assumed_0.001 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a | 0/6496 | 0.0 | 0.0 | 0.0 | 0.0 |

Cost shares of |gross P&L| (fee / spread / impact), abstentions, how fills were priced, events:

| strategy | exit | fee | fee share | spread share | impact share | abstentions | fill pricing | events |
|---|---|---|---|---|---|---|---|---|
| S0 | E1 | primary_0.0035 | 3.43 | 2.13 | 2.82 | 5444 | capped=10, extrapolated=2, interpolated=74 | entry_rejected:cooldown=21, entry_rejected:max_open_positions=44, signal_blocked:drawdown_soft=50 |
| S0 | E2 | primary_0.0035 | 6.93 | 4.17 | 4.93 | 5631 | capped=8, extrapolated=3, interpolated=83 | entry_rejected:cooldown=25, entry_rejected:max_open_positions=32, signal_blocked:drawdown_soft=53 |
| S0 | E3 | primary_0.0035 | 4.51 | 2.72 | 3.7 | 6152 | capped=10, extrapolated=1, interpolated=77 | entry_rejected:cooldown=45, entry_rejected:max_open_positions=7, signal_blocked:drawdown_soft=110 |
| S0 | E4 | primary_0.0035 | 4.05 | 2.44 | 3.31 | 6169 | capped=10, interpolated=79 | entry_rejected:cooldown=51, entry_rejected:max_open_positions=6, signal_blocked:drawdown_soft=110 |
| S0 | E1 | assumed_0.002 | 1.66 | 1.84 | 2.43 | 5367 | capped=12, extrapolated=2, interpolated=86 | entry_rejected:cooldown=26, entry_rejected:max_open_positions=44, signal_blocked:drawdown_soft=27 |
| S0 | E2 | assumed_0.002 | 2.68 | 2.88 | 3.41 | 5483 | capped=10, extrapolated=3, interpolated=97 | entry_rejected:cooldown=29, entry_rejected:max_open_positions=32, signal_blocked:drawdown_soft=27 |
| S0 | E3 | assumed_0.002 | 3.23 | 3.33 | 4.52 | 6122 | capped=12, extrapolated=2, interpolated=98 | entry_rejected:cooldown=65, entry_rejected:max_open_positions=8, signal_blocked:drawdown_soft=63 |
| S0 | E4 | assumed_0.002 | 2.95 | 3.09 | 4.19 | 6144 | capped=12, interpolated=98 | entry_rejected:cooldown=64, entry_rejected:max_open_positions=6, signal_blocked:drawdown_soft=74 |
| S0 | E1 | assumed_0.001 | 0.815 | 1.8 | 2.27 | 5336 | capped=12, extrapolated=2, interpolated=98 | entry_rejected:cooldown=32, entry_rejected:max_open_positions=44, signal_blocked:drawdown_soft=4 |
| S0 | E2 | assumed_0.001 | 1.42 | 3.09 | 3.55 | 5445 | capped=10, extrapolated=3, interpolated=107 | entry_dropped:end_of_data=1, entry_rejected:cooldown=30, entry_rejected:max_open_positions=34 |
| S0 | E3 | assumed_0.001 | 1.42 | 2.97 | 4.02 | 6113 | capped=14, extrapolated=2, interpolated=106 | entry_rejected:cooldown=69, entry_rejected:max_open_positions=8, signal_blocked:drawdown_soft=55 |
| S0 | E4 | assumed_0.001 | 1.28 | 2.69 | 3.65 | 6130 | capped=14, interpolated=108 | entry_rejected:cooldown=73, entry_rejected:max_open_positions=7, signal_blocked:drawdown_soft=56 |
| S1 | E1 | primary_0.0035 | n/a | n/a | n/a | 6496 | - | - |
| S1 | E2 | primary_0.0035 | n/a | n/a | n/a | 6496 | - | - |
| S1 | E3 | primary_0.0035 | n/a | n/a | n/a | 6496 | - | - |
| S1 | E4 | primary_0.0035 | n/a | n/a | n/a | 6496 | - | - |
| S1 | E1 | assumed_0.002 | n/a | n/a | n/a | 6496 | - | - |
| S1 | E2 | assumed_0.002 | n/a | n/a | n/a | 6496 | - | - |
| S1 | E3 | assumed_0.002 | n/a | n/a | n/a | 6496 | - | - |
| S1 | E4 | assumed_0.002 | n/a | n/a | n/a | 6496 | - | - |
| S1 | E1 | assumed_0.001 | n/a | n/a | n/a | 6496 | - | - |
| S1 | E2 | assumed_0.001 | n/a | n/a | n/a | 6496 | - | - |
| S1 | E3 | assumed_0.001 | n/a | n/a | n/a | 6496 | - | - |
| S1 | E4 | assumed_0.001 | n/a | n/a | n/a | 6496 | - | - |

## Buy-and-hold benchmark (equal weight over the universe, same costs)

Each symbol is bought with the whole 10000 USDT at the segment's first open and sold at its last close; with recorded costs a 10000 USDT order is beyond the calibrated notionals (extrapolated or capped impact).

| split | fee | mean net return | frictionless return | worst per-symbol max DD |
|---|---|---|---|---|
| train | primary_0.0035 | 5.38% | 8.62% | 19.96% |
| train | assumed_0.002 | 5.69% | 8.62% | 19.96% |
| train | assumed_0.001 | 5.91% | 8.62% | 19.96% |
| validation | primary_0.0035 | -8.16% | -5.36% | 22.65% |
| validation | assumed_0.002 | -7.89% | -5.36% | 22.65% |
| validation | assumed_0.001 | -7.70% | -5.36% | 22.65% |

## S1 cost-gate feasibility

fraction of 15m bars (train+validation segments only, features available) where the S1 cost gate passes, with each symbol's own cost model; regime-dependent threshold (3x favourable, 4x tightened).

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
| BTCUSDT | 2785 | 2341 | 0.20% | 0.34% | 0.56% | 0.00% (0) | 0.00% (0) | 0.00% (0) | 8 |
| ETHUSDT | 2784 | 1380 | 0.30% | 0.56% | 1.26% | 0.00% (0) | 0.00% (0) | 0.00% (0) | 2 |
| PEPEUSDT | 2785 | 1655 | 0.52% | 1.04% | 2.33% | 0.00% (0) | 0.00% (0) | 0.00% (0) | 2 |
| SOLUSDT | 2785 | 1704 | 0.29% | 0.50% | 0.88% | 0.00% (0) | 0.00% (0) | 0.00% (0) | 1 |
| USOONUSDT | 2785 | 1082 | 0.41% | 0.73% | 1.23% | 0.00% (0) | 0.00% (0) | 0.00% (0) | 6 |
| XAUTUSDT | 2785 | 609 | 0.13% | 0.20% | 0.30% | 0.00% (0) | 0.00% (0) | 0.00% (0) | 0 |
| XRPUSDT | 2785 | 1475 | 0.40% | 0.67% | 1.17% | 0.00% (0) | 0.00% (0) | 0.00% (0) | 5 |

## Top candidate: S1+E4 at primary_0.0035

best S1 validation net return at the primary fee among variants with trades; ties/none -> pre-registered E4.

- sensitivity on train (+/-0.2): base expectancy (R) n/a, same-sign fraction n/a over 22 neighbours
- sensitivity on validation (+/-0.2): base expectancy (R) n/a, same-sign fraction n/a over 22 neighbours

## Data quality (15m bars)

| symbol | bars | bars with missing minutes | zero-volume bars | rules |
|---|---|---|---|---|
| BTCUSDT | 4314 | 8 | 394 | fixture: amount 8, price 2 |
| ETHUSDT | 4303 | 10 | 997 | fixture: amount 5, price 2 |
| XRPUSDT | 4313 | 9 | 1040 | fixture: amount 1, price 5 |
| XAUTUSDT | 4308 | 9 | 1400 | derived: amount 5, price 2 |
| USOONUSDT | 4304 | 12 | 1211 | derived: amount 4, price 2 |
| PEPEUSDT | 4315 | 9 | 975 | derived: amount 0, price 8 |
| SOLUSDT | 4312 | 12 | 887 | fixture: amount 5, price 2 |

## Limitations

EXPLORATORY, UNVALIDATED results: in-sample (train) and validation only, placeholder/assumed costs, ~45 days of one market regime. Not evidence of profitability. The universe was ranked by CURRENT 24h volume (selection bias). Window ~45 days. Costs: per-symbol p90 half spread and a p90 impact curve calibrated from a few hours of recorded order books (one or two sessions, top 10 levels; NOT representative of all hours or regimes; see docs/COSTS.md); symbols without a usable calibration use the proxy model and are labelled. Fills at next-bar open/level touch; take-profits also pay taker costs. Fees are placeholders/assumptions charged in quote (Wallex fee unverified). The S1 cost gate sees the recorded full spread plus the round-trip impact at 1000 USDT. Warm-up bars before each segment are excluded from metrics. Missing-minute bars are kept and counted. No parameters were tuned.
