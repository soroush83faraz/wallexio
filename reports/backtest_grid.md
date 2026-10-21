# Backtest grid (train + validation only)

**EXPLORATORY, UNVALIDATED results: in-sample (train) and validation only, placeholder/assumed costs, ~45 days of one market regime. Not evidence of profitability.**

- git HEAD: `eb023f54d1f162919b9f37663274f8598befbfda` ; inputs hash: `a2c1559704d6b86ddf825b3f5d2cc5c1b47737f58a451ff194e160be08eb92b2`
- symbols: BTCUSDT, ETHUSDT, PEPEUSDT, SOLUSDT, USOONUSDT, XAUTUSDT, XRPUSDT
- split: 50% train / 25% validation / remainder (~25%) test, by 15m bar time; embargo 300 bars between segments; 300-bar warm-up from the embargo.
  - train: 2026-09-06T21:15:00Z .. 2026-09-26T06:30:00Z (1857 bars)
  - validation: 2026-09-29T09:45:00Z .. 2026-10-09T01:30:00Z (928 bars)
  - test: 2026-10-12T04:45:00Z .. 2026-10-21T21:00:00Z (930 bars) (RESERVED, not evaluated)

Reading notes: a max drawdown near 8% is the RiskConfig soft-drawdown limit (`drawdown_soft_pct` 8): new entries are blocked after it, which is why many losing runs end at about -8%. Impact cost is mostly at the model's 50 bps cap because the liquidity proxy (previous 15m bar notional) is small next to 1000 USDT orders. Metrics exclude warm-up bars; 'bars' in the per-run reports counts the scored segment, the header's bar count includes warm-up.

Fee scenarios:
- `primary_0.0035` taker 0.0035: PLACEHOLDER taker fee (config fees.taker_rate), unverified
- `assumed_0.002` taker 0.002: ASSUMED fee scenario - real fee unverified
- `assumed_0.001` taker 0.001: ASSUMED fee scenario - real fee unverified

## Train results

| strategy | exit | fee | net return | max DD | trades | win rate | PF | exp (R) | Sharpe | signals/evals | fees | spread | impact | gross P&L |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| S0 | E1 | primary_0.0035 | -8.10% | 8.10% | 47 | 21.28% | 0.101 | -1.56 | -21.2 | 455/12177 | 327.3 | 70.1 | 483.1 | 70.1 |
| S0 | E2 | primary_0.0035 | -8.15% | 8.15% | 50 | 12.00% | 0.123 | -1.54 | -18.1 | 445/12474 | 348.4 | 74.6 | 512.4 | 120.4 |
| S0 | E3 | primary_0.0035 | -8.10% | 8.10% | 45 | 6.67% | 0.0486 | -1.7 | -25.5 | 501/12855 | 313.3 | 67.1 | 455.6 | 26.1 |
| S0 | E4 | primary_0.0035 | -8.17% | 8.17% | 45 | 6.67% | 0.037 | -1.71 | -30.1 | 502/12868 | 313.2 | 67.1 | 454.9 | 17.9 |
| S0 | E1 | assumed_0.002 | -8.14% | 8.14% | 54 | 20.37% | 0.132 | -1.38 | -22 | 448/12095 | 214.8 | 80.5 | 555.5 | 36.6 |
| S0 | E2 | assumed_0.002 | -8.31% | 8.31% | 58 | 13.79% | 0.139 | -1.36 | -19.1 | 436/12359 | 230.8 | 86.5 | 594.7 | 81.4 |
| S0 | E3 | assumed_0.002 | -8.00% | 8.00% | 52 | 5.77% | 0.0593 | -1.44 | -25.5 | 497/12832 | 206.8 | 77.6 | 525.3 | 9.3 |
| S0 | E4 | assumed_0.002 | -8.06% | 8.06% | 52 | 5.77% | 0.0479 | -1.43 | -30.8 | 501/12856 | 206.8 | 77.5 | 524.5 | 3.2 |
| S0 | E1 | assumed_0.001 | -8.22% | 8.22% | 72 | 26.39% | 0.254 | -1.01 | -17.6 | 373/11316 | 143.3 | 107.5 | 746.3 | 175.2 |
| S0 | E2 | assumed_0.001 | -8.44% | 8.44% | 77 | 25.97% | 0.218 | -1 | -16.8 | 366/11833 | 153.3 | 115.0 | 797.8 | 222.5 |
| S0 | E3 | assumed_0.001 | -8.06% | 8.06% | 60 | 6.67% | 0.0673 | -1.2 | -25.4 | 484/12797 | 119.3 | 89.5 | 610.3 | 13.3 |
| S0 | E4 | assumed_0.001 | -8.42% | 8.42% | 60 | 6.67% | 0.0535 | -1.23 | -31 | 493/12832 | 119.3 | 89.5 | 609.4 | -24.0 |
| S1 | E1 | primary_0.0035 | 0.00% | 0.00% | 0 | n/a | n/a | n/a | n/a | 0/12998 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E2 | primary_0.0035 | 0.00% | 0.00% | 0 | n/a | n/a | n/a | n/a | 0/12998 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E3 | primary_0.0035 | 0.00% | 0.00% | 0 | n/a | n/a | n/a | n/a | 0/12998 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E4 | primary_0.0035 | 0.00% | 0.00% | 0 | n/a | n/a | n/a | n/a | 0/12998 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E1 | assumed_0.002 | 0.00% | 0.00% | 0 | n/a | n/a | n/a | n/a | 0/12998 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E2 | assumed_0.002 | 0.00% | 0.00% | 0 | n/a | n/a | n/a | n/a | 0/12998 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E3 | assumed_0.002 | 0.00% | 0.00% | 0 | n/a | n/a | n/a | n/a | 0/12998 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E4 | assumed_0.002 | 0.00% | 0.00% | 0 | n/a | n/a | n/a | n/a | 0/12998 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E1 | assumed_0.001 | 0.00% | 0.00% | 0 | n/a | n/a | n/a | n/a | 0/12998 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E2 | assumed_0.001 | 0.00% | 0.00% | 0 | n/a | n/a | n/a | n/a | 0/12998 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E3 | assumed_0.001 | 0.00% | 0.00% | 0 | n/a | n/a | n/a | n/a | 0/12998 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E4 | assumed_0.001 | 0.00% | 0.00% | 0 | n/a | n/a | n/a | n/a | 0/12998 | 0.0 | 0.0 | 0.0 | 0.0 |

Cost shares of |gross P&L| (fee / spread / impact) and abstentions:

| strategy | exit | fee | fee share | spread share | impact share | abstentions | events |
|---|---|---|---|---|---|---|---|
| S0 | E1 | primary_0.0035 | 4.67 | 1 | 6.89 | 11722 | entry_rejected:cooldown=23, entry_rejected:max_open_positions=28, signal_blocked:drawdown_soft=357 |
| S0 | E2 | primary_0.0035 | 2.89 | 0.62 | 4.26 | 12029 | entry_rejected:cooldown=22, entry_rejected:max_open_positions=16, signal_blocked:drawdown_soft=357 |
| S0 | E3 | primary_0.0035 | 12 | 2.58 | 17.5 | 12354 | entry_rejected:cooldown=41, entry_rejected:max_open_positions=5, signal_blocked:drawdown_soft=410 |
| S0 | E4 | primary_0.0035 | 17.5 | 3.74 | 25.4 | 12366 | entry_rejected:cooldown=42, entry_rejected:max_open_positions=5, signal_blocked:drawdown_soft=410 |
| S0 | E1 | assumed_0.002 | 5.86 | 2.2 | 15.2 | 11647 | entry_rejected:cooldown=25, entry_rejected:max_open_positions=28, signal_blocked:drawdown_soft=341 |
| S0 | E2 | assumed_0.002 | 2.84 | 1.06 | 7.31 | 11923 | entry_rejected:cooldown=21, entry_rejected:max_open_positions=16, signal_blocked:drawdown_soft=341 |
| S0 | E3 | assumed_0.002 | 22.3 | 8.37 | 56.7 | 12335 | entry_rejected:cooldown=49, entry_rejected:max_open_positions=7, signal_blocked:drawdown_soft=389 |
| S0 | E4 | assumed_0.002 | 65.3 | 24.5 | 166 | 12355 | entry_rejected:cooldown=54, entry_rejected:max_open_positions=6, signal_blocked:drawdown_soft=389 |
| S0 | E1 | assumed_0.001 | 0.818 | 0.614 | 4.26 | 10943 | entry_rejected:cooldown=33, entry_rejected:max_open_positions=74, signal_blocked:drawdown_soft=194 |
| S0 | E2 | assumed_0.001 | 0.689 | 0.517 | 3.59 | 11467 | entry_rejected:cooldown=23, entry_rejected:max_open_positions=53, signal_blocked:drawdown_soft=213 |
| S0 | E3 | assumed_0.001 | 8.97 | 6.73 | 45.9 | 12313 | entry_rejected:cooldown=59, entry_rejected:max_open_positions=8, signal_blocked:drawdown_soft=357 |
| S0 | E4 | assumed_0.001 | 4.97 | 3.73 | 25.4 | 12339 | entry_rejected:cooldown=70, entry_rejected:max_open_positions=6, signal_blocked:drawdown_soft=357 |
| S1 | E1 | primary_0.0035 | n/a | n/a | n/a | 12998 | - |
| S1 | E2 | primary_0.0035 | n/a | n/a | n/a | 12998 | - |
| S1 | E3 | primary_0.0035 | n/a | n/a | n/a | 12998 | - |
| S1 | E4 | primary_0.0035 | n/a | n/a | n/a | 12998 | - |
| S1 | E1 | assumed_0.002 | n/a | n/a | n/a | 12998 | - |
| S1 | E2 | assumed_0.002 | n/a | n/a | n/a | 12998 | - |
| S1 | E3 | assumed_0.002 | n/a | n/a | n/a | 12998 | - |
| S1 | E4 | assumed_0.002 | n/a | n/a | n/a | 12998 | - |
| S1 | E1 | assumed_0.001 | n/a | n/a | n/a | 12998 | - |
| S1 | E2 | assumed_0.001 | n/a | n/a | n/a | 12998 | - |
| S1 | E3 | assumed_0.001 | n/a | n/a | n/a | 12998 | - |
| S1 | E4 | assumed_0.001 | n/a | n/a | n/a | 12998 | - |

## Validation results

| strategy | exit | fee | net return | max DD | trades | win rate | PF | exp (R) | Sharpe | signals/evals | fees | spread | impact | gross P&L |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| S0 | E1 | primary_0.0035 | -8.12% | 8.12% | 40 | 10.00% | 0.0504 | -1.95 | -33.4 | 143/5285 | 278.1 | 59.6 | 406.7 | -67.5 |
| S0 | E2 | primary_0.0035 | -8.29% | 8.33% | 44 | 9.09% | 0.0442 | -1.83 | -33.3 | 150/5643 | 306.2 | 65.6 | 439.8 | -17.7 |
| S0 | E3 | primary_0.0035 | -8.01% | 8.01% | 40 | 0.00% | 0 | -1.73 | -37.7 | 208/6370 | 278.2 | 59.6 | 395.7 | -67.4 |
| S0 | E4 | primary_0.0035 | -8.09% | 8.09% | 41 | 0.00% | 0 | -1.78 | -40 | 211/6384 | 285.2 | 61.1 | 408.7 | -54.2 |
| S0 | E1 | assumed_0.002 | -7.59% | 7.59% | 44 | 9.09% | 0.0678 | -1.67 | -32.2 | 134/5247 | 174.8 | 65.6 | 444.0 | -74.1 |
| S0 | E2 | assumed_0.002 | -8.19% | 8.26% | 50 | 10.00% | 0.0588 | -1.6 | -34.3 | 140/5558 | 198.8 | 74.5 | 502.7 | -43.0 |
| S0 | E3 | assumed_0.002 | -8.06% | 8.06% | 48 | 2.08% | 0.00203 | -1.54 | -41.5 | 204/6347 | 190.8 | 71.5 | 474.5 | -69.3 |
| S0 | E4 | assumed_0.002 | -8.14% | 8.14% | 49 | 0.00% | 0 | -1.55 | -44.2 | 204/6355 | 194.8 | 73.0 | 487.4 | -59.3 |
| S0 | E1 | assumed_0.001 | -6.71% | 6.76% | 44 | 9.09% | 0.0861 | -1.48 | -29.7 | 134/5247 | 87.4 | 65.6 | 444.0 | -74.1 |
| S0 | E2 | assumed_0.001 | -7.92% | 8.02% | 54 | 12.96% | 0.0729 | -1.44 | -33.7 | 134/5530 | 107.3 | 80.5 | 539.9 | -64.0 |
| S0 | E3 | assumed_0.001 | -8.20% | 8.20% | 56 | 1.79% | 0.00442 | -1.34 | -44.1 | 192/6314 | 111.3 | 83.5 | 556.1 | -69.4 |
| S0 | E4 | assumed_0.001 | -8.03% | 8.03% | 55 | 1.82% | 0.00179 | -1.34 | -44.9 | 196/6335 | 109.3 | 82.0 | 544.2 | -67.3 |
| S1 | E1 | primary_0.0035 | 0.00% | 0.00% | 0 | n/a | n/a | n/a | n/a | 0/6496 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E2 | primary_0.0035 | 0.00% | 0.00% | 0 | n/a | n/a | n/a | n/a | 0/6496 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E3 | primary_0.0035 | 0.00% | 0.00% | 0 | n/a | n/a | n/a | n/a | 0/6496 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E4 | primary_0.0035 | 0.00% | 0.00% | 0 | n/a | n/a | n/a | n/a | 0/6496 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E1 | assumed_0.002 | 0.00% | 0.00% | 0 | n/a | n/a | n/a | n/a | 0/6496 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E2 | assumed_0.002 | 0.00% | 0.00% | 0 | n/a | n/a | n/a | n/a | 0/6496 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E3 | assumed_0.002 | 0.00% | 0.00% | 0 | n/a | n/a | n/a | n/a | 0/6496 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E4 | assumed_0.002 | 0.00% | 0.00% | 0 | n/a | n/a | n/a | n/a | 0/6496 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E1 | assumed_0.001 | 0.00% | 0.00% | 0 | n/a | n/a | n/a | n/a | 0/6496 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E2 | assumed_0.001 | 0.00% | 0.00% | 0 | n/a | n/a | n/a | n/a | 0/6496 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E3 | assumed_0.001 | 0.00% | 0.00% | 0 | n/a | n/a | n/a | n/a | 0/6496 | 0.0 | 0.0 | 0.0 | 0.0 |
| S1 | E4 | assumed_0.001 | 0.00% | 0.00% | 0 | n/a | n/a | n/a | n/a | 0/6496 | 0.0 | 0.0 | 0.0 | 0.0 |

Cost shares of |gross P&L| (fee / spread / impact) and abstentions:

| strategy | exit | fee | fee share | spread share | impact share | abstentions | events |
|---|---|---|---|---|---|---|---|
| S0 | E1 | primary_0.0035 | 4.12 | 0.884 | 6.03 | 5142 | entry_rejected:cooldown=23, entry_rejected:max_open_positions=63, signal_blocked:drawdown_soft=17 |
| S0 | E2 | primary_0.0035 | 17.3 | 3.7 | 24.8 | 5493 | entry_rejected:cooldown=21, entry_rejected:max_open_positions=44, signal_blocked:drawdown_soft=41 |
| S0 | E3 | primary_0.0035 | 4.13 | 0.884 | 5.87 | 6162 | entry_rejected:cooldown=40, entry_rejected:max_open_positions=7, signal_blocked:drawdown_soft=121 |
| S0 | E4 | primary_0.0035 | 5.26 | 1.13 | 7.54 | 6173 | entry_rejected:cooldown=47, entry_rejected:max_open_positions=6, signal_blocked:drawdown_soft=117 |
| S0 | E1 | assumed_0.002 | 2.36 | 0.885 | 5.99 | 5113 | entry_dropped:end_of_data=1, entry_rejected:cooldown=26, entry_rejected:max_open_positions=63 |
| S0 | E2 | assumed_0.002 | 4.62 | 1.73 | 11.7 | 5418 | entry_rejected:cooldown=22, entry_rejected:max_open_positions=44, signal_blocked:drawdown_soft=24 |
| S0 | E3 | assumed_0.002 | 2.75 | 1.03 | 6.85 | 6143 | entry_rejected:cooldown=51, entry_rejected:max_open_positions=7, signal_blocked:drawdown_soft=98 |
| S0 | E4 | assumed_0.002 | 3.28 | 1.23 | 8.22 | 6151 | entry_rejected:cooldown=61, entry_rejected:max_open_positions=6, signal_blocked:drawdown_soft=88 |
| S0 | E1 | assumed_0.001 | 1.18 | 0.885 | 5.99 | 5113 | entry_dropped:end_of_data=1, entry_rejected:cooldown=26, entry_rejected:max_open_positions=63 |
| S0 | E2 | assumed_0.001 | 1.68 | 1.26 | 8.44 | 5396 | entry_rejected:cooldown=27, entry_rejected:max_open_positions=44, signal_blocked:drawdown_soft=9 |
| S0 | E3 | assumed_0.001 | 1.6 | 1.2 | 8.01 | 6122 | entry_rejected:cooldown=65, entry_rejected:max_open_positions=8, signal_blocked:drawdown_soft=63 |
| S0 | E4 | assumed_0.001 | 1.62 | 1.22 | 8.09 | 6139 | entry_rejected:cooldown=69, entry_rejected:max_open_positions=7, signal_blocked:drawdown_soft=65 |
| S1 | E1 | primary_0.0035 | n/a | n/a | n/a | 6496 | - |
| S1 | E2 | primary_0.0035 | n/a | n/a | n/a | 6496 | - |
| S1 | E3 | primary_0.0035 | n/a | n/a | n/a | 6496 | - |
| S1 | E4 | primary_0.0035 | n/a | n/a | n/a | 6496 | - |
| S1 | E1 | assumed_0.002 | n/a | n/a | n/a | 6496 | - |
| S1 | E2 | assumed_0.002 | n/a | n/a | n/a | 6496 | - |
| S1 | E3 | assumed_0.002 | n/a | n/a | n/a | 6496 | - |
| S1 | E4 | assumed_0.002 | n/a | n/a | n/a | 6496 | - |
| S1 | E1 | assumed_0.001 | n/a | n/a | n/a | 6496 | - |
| S1 | E2 | assumed_0.001 | n/a | n/a | n/a | 6496 | - |
| S1 | E3 | assumed_0.001 | n/a | n/a | n/a | 6496 | - |
| S1 | E4 | assumed_0.001 | n/a | n/a | n/a | 6496 | - |

## Buy-and-hold benchmark (equal weight over the universe, same costs)

| split | fee | mean net return | worst per-symbol max DD |
|---|---|---|---|
| train | primary_0.0035 | 6.63% | 19.96% |
| train | assumed_0.002 | 6.95% | 19.96% |
| train | assumed_0.001 | 7.16% | 19.96% |
| validation | primary_0.0035 | -7.09% | 22.65% |
| validation | assumed_0.002 | -6.81% | 22.65% |
| validation | assumed_0.001 | -6.63% | 22.65% |

## S1 cost-gate feasibility

fraction of 15m bars (train+validation segments only, features available) where the S1 cost gate passes; regime-dependent threshold (3x favourable, 4x tightened).

ATR/close needed for the gate to pass (S1 atr band is [0.002, 0.03]):

| fee | round-trip cost (bps) | ATR% needed (favourable) | ATR% needed (tightened) |
|---|---|---|---|
| primary_0.0035 | 135 | 2.70% | 3.60% |
| assumed_0.002 | 105 | 2.10% | 2.80% |
| assumed_0.001 | 85 | 1.70% | 2.27% |

Fraction of usable-feature bars where the cost gate passes (bars without usable features, e.g. too many zero-volume bars or a zero median volume, make S1 abstain regardless):

| symbol | bars in train+val | usable-feature bars | median ATR% | p90 ATR% | max ATR% | primary_0.0035 | assumed_0.002 | assumed_0.001 | other rules pass (no cost gate) |
|---|---|---|---|---|---|---|---|---|---|
| BTCUSDT | 2785 | 2341 | 0.20% | 0.34% | 0.56% | 0.00% (0) | 0.00% (0) | 0.00% (0) | 8 |
| ETHUSDT | 2784 | 1380 | 0.30% | 0.56% | 1.26% | 0.00% (0) | 0.00% (0) | 0.00% (0) | 2 |
| PEPEUSDT | 2785 | 1655 | 0.52% | 1.04% | 2.33% | 0.00% (0) | 0.30% (5) | 1.15% (19) | 2 |
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

EXPLORATORY, UNVALIDATED results: in-sample (train) and validation only, placeholder/assumed costs, ~45 days of one market regime. Not evidence of profitability. The universe was ranked by CURRENT 24h volume (selection bias). Window ~45 days. Engine limitations: fills at next-bar open/level touch with half-spread 7.5 bps and an impact proxy; fees are placeholders/assumptions; the cost gate is fed the engine's worst-case slippage cap (50 bps) plus the full spread, so the round-trip cost it sees is much larger than realised costs. Warm-up bars before each segment are excluded from metrics. Missing-minute bars are kept and counted. No parameters were tuned.
