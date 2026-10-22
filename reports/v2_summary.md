# Backtest v2 summary (recorded costs)

**EXPLORATORY, UNVALIDATED results: in-sample (train) and validation only, placeholder/assumed costs, ~45 days of one market regime. Not evidence of profitability.**

Best / worst exit = highest / lowest TRAIN net return at the primary fee (0.0035, placeholder); the validation numbers of that exit are therefore out-of-sample for the choice. Test segment NOT evaluated. `(n<30)` marks trade counts too small for any statistical statement.

- 15m bars, recorded costs: inputs hash `7ac6c21ede31622070f33c94624a707bc8c13059d43abe4c8164db94aef996fb`, git `6111708d3dac542f497ad863bd40a69615b5a497`
- 60m bars, recorded costs: inputs hash `bb5ca8bff364f7fa561265320b2c3ffbcc3f2913267d0e3b47b7db6916d8e021`, git `6111708d3dac542f497ad863bd40a69615b5a497`

| bars | strategy | exit | split | fee | net return | max DD | trades | win rate | PF | exp (R) | fee / spread / impact share of gross |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 15m | S0 | E4 (best) | train | primary_0.0035 | -8.13% | 8.13% | 47 | 6.38% | 0.0269 | -1.77 | 17 / 11 / 15 |
| 15m | S0 | E4 (best) | train | assumed_0.002 | -8.84% | 8.84% | 60 | 6.67% | 0.036 | -1.38 | 12 / 14 / 19 |
| 15m | S0 | E4 (best) | train | assumed_0.001 | -8.33% | 8.33% | 63 | 6.35% | 0.047 | -1.18 | 3.6 / 8.3 / 11 |
| 15m | S0 | E4 (best) | validation | primary_0.0035 | -8.16% | 8.16% | 44 | 2.27% | 0.00122 | -1.88 | 4.1 / 2.4 / 3.3 |
| 15m | S0 | E4 (best) | validation | assumed_0.002 | -8.17% | 8.17% | 54 | 3.70% | 0.00519 | -1.47 | 3 / 3.1 / 4.2 |
| 15m | S0 | E4 (best) | validation | assumed_0.001 | -8.05% | 8.05% | 60 | 3.33% | 0.0102 | -1.24 | 1.3 / 2.7 / 3.7 |
| 15m | S0 | E2 (worst) | train | primary_0.0035 | -8.37% | 8.37% | 54 | 14.81% | 0.0995 | -1.63 | 3.4 / 2.2 / 3 |
| 15m | S0 | E2 (worst) | train | assumed_0.002 | -8.61% | 8.61% | 74 | 22.97% | 0.178 | -1.18 | 1.5 / 1.6 / 2.2 |
| 15m | S0 | E2 (worst) | train | assumed_0.001 | -8.10% | 8.10% | 80 | 25.00% | 0.231 | -0.977 | 0.8 / 1.8 / 2.5 |
| 15m | S0 | E2 (worst) | validation | primary_0.0035 | -8.04% | 8.14% | 47 | 10.64% | 0.0311 | -1.91 | 6.9 / 4.2 / 4.9 |
| 15m | S0 | E2 (worst) | validation | assumed_0.002 | -8.14% | 8.28% | 55 | 10.91% | 0.0479 | -1.62 | 2.7 / 2.9 / 3.4 |
| 15m | S0 | E2 (worst) | validation | assumed_0.001 | -7.60% | 7.76% | 60 | 13.33% | 0.0683 | -1.38 | 1.4 / 3.1 / 3.6 |
| 15m | S1 | E1 (all exits tied) | train | primary_0.0035 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a / n/a / n/a |
| 15m | S1 | E1 (all exits tied) | train | assumed_0.002 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a / n/a / n/a |
| 15m | S1 | E1 (all exits tied) | train | assumed_0.001 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a / n/a / n/a |
| 15m | S1 | E1 (all exits tied) | validation | primary_0.0035 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a / n/a / n/a |
| 15m | S1 | E1 (all exits tied) | validation | assumed_0.002 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a / n/a / n/a |
| 15m | S1 | E1 (all exits tied) | validation | assumed_0.001 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a / n/a / n/a |
| 15m | S1 | E4 (all exits tied) | train | primary_0.0035 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a / n/a / n/a |
| 15m | S1 | E4 (all exits tied) | train | assumed_0.002 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a / n/a / n/a |
| 15m | S1 | E4 (all exits tied) | train | assumed_0.001 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a / n/a / n/a |
| 15m | S1 | E4 (all exits tied) | validation | primary_0.0035 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a / n/a / n/a |
| 15m | S1 | E4 (all exits tied) | validation | assumed_0.002 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a / n/a / n/a |
| 15m | S1 | E4 (all exits tied) | validation | assumed_0.001 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a / n/a / n/a |
| 60m | S0 | E2 (best) | train | primary_0.0035 | -3.86% | 3.89% | 32 | 37.50% | 0.521 | -0.365 | 1 / 0.7 / 1 |
| 60m | S0 | E2 (best) | train | assumed_0.002 | -3.15% | 3.32% | 33 | 39.39% | 0.592 | -0.236 | 0.62 / 0.75 / 1.1 |
| 60m | S0 | E2 (best) | train | assumed_0.001 | -2.49% | 3.09% | 33 | 39.39% | 0.66 | -0.141 | 0.31 / 0.75 / 1.1 |
| 60m | S0 | E2 (best) | validation | primary_0.0035 | -5.19% | 5.19% | 18 (n<30) | 11.11% | 0.0103 | -1.38 | 0.63 / 0.41 / 0.58 |
| 60m | S0 | E2 (best) | validation | assumed_0.002 | -4.65% | 4.65% | 18 (n<30) | 11.11% | 0.0239 | -1.19 | 0.36 / 0.41 / 0.58 |
| 60m | S0 | E2 (best) | validation | assumed_0.001 | -4.30% | 4.30% | 18 (n<30) | 11.11% | 0.0347 | -1.06 | 0.18 / 0.41 / 0.58 |
| 60m | S0 | E3 (worst) | train | primary_0.0035 | -8.17% | 8.17% | 47 | 12.77% | 0.117 | -0.642 | 14 / 8.8 / 13 |
| 60m | S0 | E3 (worst) | train | assumed_0.002 | -6.77% | 6.77% | 47 | 12.77% | 0.157 | -0.496 | 8 / 8.8 / 13 |
| 60m | S0 | E3 (worst) | train | assumed_0.001 | -5.83% | 5.83% | 47 | 12.77% | 0.192 | -0.398 | 4 / 8.8 / 13 |
| 60m | S0 | E3 (worst) | validation | primary_0.0035 | -5.38% | 5.38% | 21 (n<30) | 0.00% | 0 | -1.32 | 0.85 / 0.55 / 0.73 |
| 60m | S0 | E3 (worst) | validation | assumed_0.002 | -4.76% | 4.76% | 21 (n<30) | 0.00% | 0 | -1.12 | 0.48 / 0.55 / 0.73 |
| 60m | S0 | E3 (worst) | validation | assumed_0.001 | -4.34% | 4.34% | 21 (n<30) | 0.00% | 0 | -0.992 | 0.24 / 0.55 / 0.73 |
| 60m | S1 | E1 (all exits tied) | train | primary_0.0035 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a / n/a / n/a |
| 60m | S1 | E1 (all exits tied) | train | assumed_0.002 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a / n/a / n/a |
| 60m | S1 | E1 (all exits tied) | train | assumed_0.001 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a / n/a / n/a |
| 60m | S1 | E1 (all exits tied) | validation | primary_0.0035 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a / n/a / n/a |
| 60m | S1 | E1 (all exits tied) | validation | assumed_0.002 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a / n/a / n/a |
| 60m | S1 | E1 (all exits tied) | validation | assumed_0.001 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a / n/a / n/a |
| 60m | S1 | E4 (all exits tied) | train | primary_0.0035 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a / n/a / n/a |
| 60m | S1 | E4 (all exits tied) | train | assumed_0.002 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a / n/a / n/a |
| 60m | S1 | E4 (all exits tied) | train | assumed_0.001 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a / n/a / n/a |
| 60m | S1 | E4 (all exits tied) | validation | primary_0.0035 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a / n/a / n/a |
| 60m | S1 | E4 (all exits tied) | validation | assumed_0.002 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a / n/a / n/a |
| 60m | S1 | E4 (all exits tied) | validation | assumed_0.001 | 0.00% | 0.00% | 0 (n<30) | n/a | n/a | n/a | n/a / n/a / n/a |

## Evidence count (all exits)

Configurations (strategy x exit) with a positive net return on BOTH train and validation, and how many of those have >= 30 trades in each split:

| bars | fee | positive on train | positive on validation | positive on both | ... with n >= 30 in both |
|---|---|---|---|---|---|
| 15m | primary_0.0035 | 0/8 | 0/8 | 0  | 0 |
| 15m | assumed_0.002 | 0/8 | 0/8 | 0  | 0 |
| 15m | assumed_0.001 | 0/8 | 0/8 | 0  | 0 |
| 60m | primary_0.0035 | 0/8 | 0/8 | 0  | 0 |
| 60m | assumed_0.002 | 0/8 | 0/8 | 0  | 0 |
| 60m | assumed_0.001 | 0/8 | 0/8 | 0  | 0 |

## Buy-and-hold (equal weight, same costs)

| bars | split | fee | mean net return | frictionless |
|---|---|---|---|---|
| 15m | train | primary_0.0035 | 5.38% | 8.62% |
| 15m | train | assumed_0.002 | 5.69% | 8.62% |
| 15m | train | assumed_0.001 | 5.91% | 8.62% |
| 15m | validation | primary_0.0035 | -8.16% | -5.36% |
| 15m | validation | assumed_0.002 | -7.89% | -5.36% |
| 15m | validation | assumed_0.001 | -7.70% | -5.36% |
| 60m | train | primary_0.0035 | 5.44% | 8.69% |
| 60m | train | assumed_0.002 | 5.76% | 8.69% |
| 60m | train | assumed_0.001 | 5.97% | 8.69% |
| 60m | validation | primary_0.0035 | -8.50% | -5.70% |
| 60m | validation | assumed_0.002 | -8.22% | -5.70% |
| 60m | validation | assumed_0.001 | -8.04% | -5.70% |

## S1 cost gate under the run's costs (bars where the gate passes / usable-feature bars)

| bars | symbol | cost source | primary_0.0035: pass (RT bps) | assumed_0.002: pass (RT bps) | assumed_0.001: pass (RT bps) | all other rules pass |
|---|---|---|---|---|---|---|
| 15m | BTCUSDT | recorded | 0/2341 (107) | 0/2341 (77) | 0/2341 (57) | 8 |
| 15m | ETHUSDT | recorded | 0/1380 (163) | 0/1380 (133) | 0/1380 (113) | 2 |
| 15m | PEPEUSDT | recorded | 0/1655 (356) | 0/1655 (326) | 0/1655 (306) | 2 |
| 15m | SOLUSDT | recorded | 0/1704 (155) | 0/1704 (125) | 0/1704 (105) | 1 |
| 15m | USOONUSDT | recorded | 0/1082 (161) | 0/1082 (131) | 0/1082 (111) | 6 |
| 15m | XAUTUSDT | recorded | 0/609 (128) | 0/609 (98) | 0/609 (78) | 0 |
| 15m | XRPUSDT | recorded | 0/1475 (151) | 0/1475 (121) | 0/1475 (101) | 5 |
| 60m | BTCUSDT | recorded | 0/578 (107) | 0/578 (77) | 0/578 (57) | 4 |
| 60m | ETHUSDT | recorded | 0/578 (163) | 0/578 (133) | 0/578 (113) | 6 |
| 60m | PEPEUSDT | recorded | 0/578 (356) | 0/578 (326) | 0/578 (306) | 0 |
| 60m | SOLUSDT | recorded | 0/578 (155) | 0/578 (125) | 0/578 (105) | 5 |
| 60m | USOONUSDT | recorded | 0/578 (161) | 0/578 (131) | 0/578 (111) | 3 |
| 60m | XAUTUSDT | recorded | 0/578 (128) | 0/578 (98) | 0/578 (78) | 3 |
| 60m | XRPUSDT | recorded | 0/578 (151) | 0/578 (121) | 0/578 (101) | 1 |

## Limitations

- 15m: EXPLORATORY, UNVALIDATED results: in-sample (train) and validation only, placeholder/assumed costs, ~45 days of one market regime. Not evidence of profitability. The universe was ranked by CURRENT 24h volume (selection bias). Window ~45 days. Costs: per-symbol p90 half spread and a p90 impact curve calibrated from a few hours of recorded order books (one or two sessions, top 10 levels; NOT representative of all hours or regimes; see docs/COSTS.md); symbols without a usable calibration use the proxy model and are labelled. Fills at next-bar open/level touch; take-profits also pay taker costs. Fees are placeholders/assumptions charged in quote (Wallex fee unverified). The S1 cost gate sees the recorded full spread plus the round-trip impact at 1000 USDT. Warm-up bars before each segment are excluded from metrics. Missing-minute bars are kept and counted. No parameters were tuned.
- 60m: EXPLORATORY, UNVALIDATED results: in-sample (train) and validation only, placeholder/assumed costs, ~45 days of one market regime. Not evidence of profitability. The universe was ranked by CURRENT 24h volume (selection bias). Window ~45 days. Costs: per-symbol p90 half spread and a p90 impact curve calibrated from a few hours of recorded order books (one or two sessions, top 10 levels; NOT representative of all hours or regimes; see docs/COSTS.md); symbols without a usable calibration use the proxy model and are labelled. Fills at next-bar open/level touch; take-profits also pay taker costs. Fees are placeholders/assumptions charged in quote (Wallex fee unverified). The S1 cost gate sees the recorded full spread plus the round-trip impact at 1000 USDT. 60m bars over ~45 days give few bars per segment (S0/S1 need 50-120 bars of history first) and therefore few trades and low statistical power. Warm-up bars before each segment are excluded from metrics. Missing-minute bars are kept and counted. No parameters were tuned.
