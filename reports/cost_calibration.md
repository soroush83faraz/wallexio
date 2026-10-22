# Cost calibration from recorded order books

**LOW CONFIDENCE. These numbers describe only the recorded snapshots: a few hours of one or two sessions, top 10 levels, ~15-35 s sampling. Spreads and depth vary by hour of day, weekday, volatility regime and news; a quiet-hours p90 can be far below a volatile-hours median. Hidden or deeper liquidity, queue dynamics, latency and our own market impact on subsequent snapshots are not captured. Refresh after several days covering all UTC hours before relying on them.**

- calibration sha256: `3ed8c7993ededa96f526037a1db264269f84d0adad7a8c925326749d845e6cdc` (version cal-1)
- inputs (copied snapshot of the live recorder files; sha256 of the bytes read):
  - `depth_20261021.jsonl` sha256 `3cccd60f98fe118a94f2786af1203097348f5b6d23058d7d73eda6f0c5a5c54e`: 742 rows, 0 bad lines, torn last line: False
  - `depth_20261022.jsonl` sha256 `547041daba4cd2de6ca167466c177f844d7751f0c10709a570a8c86420160780`: 294 rows, 0 bad lines, torn last line: False

Method: VWAP of a marketable order of N quote walking the recorded levels, vs mid, bps; VWAP vs best ask (BUY) / best bid (SELL), in bps of mid; slippage_vs_mid = half_spread + impact_beyond_touch per snapshot. Quantiles: upper nearest-rank over valid snapshots (each snapshot weighs the same); snapshots whose visible depth is below N rank as +inf, so such a quantile is null.

## Headline (order notional 1000 USDT)

| symbol | usable | samples | covered h | UTC hours | spread median / p90 (bps) | BUY slip vs mid median / p90 | SELL slip vs mid median / p90 | visible depth ask / bid median (USDT) |
|---|---|---|---|---|---|---|---|---|
| BTCUSDT | yes | 148 | 1.19 | 04-05,22-23 | 17.84 / 23.36 | 9.02 / 11.73 | 10.08 / 16.89 | 58746.33 / 1802.56 |
| ETHUSDT | yes | 148 | 1.19 | 04-05,22-23 | 26.34 / 53.24 | 16.45 / 29.81 | 20.29 / 42.68 | 37717.51 / 11375.70 |
| PEPEUSDT | yes | 148 | 1.19 | 04-05,22-23 | 44.55 / 85.84 | 32.16 / 53.99 | 108.64 / 149.14 | 38130.06 / 3587.19 |
| SOLUSDT | yes | 148 | 1.19 | 04-05,22-23 | 18.67 / 31.29 | 28.74 / 36.16 | 12.55 / 19.41 | 21638.21 / 15427.69 |
| USOONUSDT | yes | 148 | 1.19 | 04-05,22-23 | 34.94 / 50.81 | 26.64 / 30.28 | 22.42 / 37.96 | 7933.11 / 9027.71 |
| XAUTUSDT | yes | 148 | 1.19 | 04-05,22-23 | 3.61 / 39.51 | 2.50 / 21.57 | 3.20 / 27.77 | 7592.28 / 7273.64 |
| XRPUSDT | yes | 148 | 1.19 | 04-05,22-23 | 16.60 / 32.28 | 12.31 / 24.60 | 17.29 / 33.68 | 8682.74 / 8447.22 |

## Slippage vs mid by notional (bps; median / p90; 'insufficient' = visible 10 levels too thin)

| symbol | side | 250 | 500 | 1000 | 2500 |
|---|---|---|---|---|---|
| BTCUSDT | BUY | 8.92 / 11.68 | 8.95 / 11.70 | 9.02 / 11.73 | 9.14 / 11.79 (1/148 short) |
| BTCUSDT | SELL | 8.94 / 11.91 | 9.11 / 12.26 | 10.08 / 16.89 | insufficient / insufficient (92/148 short) |
| ETHUSDT | BUY | 15.44 / 27.92 | 16.27 / 29.03 | 16.45 / 29.81 | 17.15 / 31.88 |
| ETHUSDT | SELL | 13.17 / 26.71 | 13.40 / 31.22 | 20.29 / 42.68 (1/148 short) | 41.37 / 75.16 (7/148 short) |
| PEPEUSDT | BUY | 32.16 / 51.93 | 32.16 / 53.19 | 32.16 / 53.99 | 32.16 / 72.82 |
| PEPEUSDT | SELL | 32.23 / 124.52 | 33.45 / 129.95 | 108.64 / 149.14 | 206.96 / 261.91 (11/148 short) |
| SOLUSDT | BUY | 10.15 / 17.07 | 13.16 / 26.12 | 28.74 / 36.16 | 42.27 / 50.31 |
| SOLUSDT | SELL | 10.24 / 16.11 | 10.81 / 16.35 | 12.55 / 19.41 | 14.51 / 21.32 (2/148 short) |
| USOONUSDT | BUY | 24.53 / 27.60 | 25.72 / 29.32 | 26.64 / 30.28 | 30.32 / 39.72 |
| USOONUSDT | SELL | 21.99 / 27.31 | 21.99 / 34.75 | 22.42 / 37.96 | 25.41 / 43.13 |
| XAUTUSDT | BUY | 1.81 / 20.18 | 2.14 / 20.84 | 2.50 / 21.57 | 4.21 / 35.13 (13/148 short) |
| XAUTUSDT | SELL | 1.97 / 19.88 | 2.33 / 20.04 | 3.20 / 27.77 | 3.99 / insufficient (23/148 short) |
| XRPUSDT | BUY | 9.86 / 16.19 | 10.78 / 19.91 | 12.31 / 24.60 | 18.45 / 38.52 (1/148 short) |
| XRPUSDT | SELL | 10.15 / 20.28 | 11.79 / 22.57 | 17.29 / 33.68 (1/148 short) | 24.41 / 57.32 (6/148 short) |

## Per-symbol confidence

- BTCUSDT: 148 snapshots over 1.2 covered hours in 3 session(s), UTC hours 04-05,22-23; NOT representative of other hours, days or volatility regimes.
- ETHUSDT: 148 snapshots over 1.2 covered hours in 3 session(s), UTC hours 04-05,22-23; NOT representative of other hours, days or volatility regimes.
- PEPEUSDT: 148 snapshots over 1.2 covered hours in 3 session(s), UTC hours 04-05,22-23; NOT representative of other hours, days or volatility regimes.
- SOLUSDT: 148 snapshots over 1.2 covered hours in 3 session(s), UTC hours 04-05,22-23; NOT representative of other hours, days or volatility regimes.
- USOONUSDT: 148 snapshots over 1.2 covered hours in 3 session(s), UTC hours 04-05,22-23; NOT representative of other hours, days or volatility regimes.
- XAUTUSDT: 148 snapshots over 1.2 covered hours in 3 session(s), UTC hours 04-05,22-23; NOT representative of other hours, days or volatility regimes.
- XRPUSDT: 148 snapshots over 1.2 covered hours in 3 session(s), UTC hours 04-05,22-23; NOT representative of other hours, days or volatility regimes.
