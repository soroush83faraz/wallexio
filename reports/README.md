# reports/

- `backtest_grid.*`, `backtest_top_{train,validation}.*`: the first exploratory run, made with the
  PRE-FIX engine (`bt-0.1`) and the placeholder volume-proxy cost model (impact mostly at its 50 bps cap).
  Kept for the record only; superseded by the `v2_*` files. Do not quote their numbers.
- `cost_calibration.{json,md}`: spread / VWAP-slippage calibration from recorded order books (about one
  hour of snapshots, 3 short sessions, UTC 22-23h and 04-05h; LOW confidence). Method and limitations:
  `docs/COSTS.md`.
- `v2_backtest_grid_{15m,60m}.{json,md}`: engine `bt-0.2`, cost model `cm-0.2` with per-symbol recorded
  (p90) costs, S0/S1 x E1-E4 x fee {0.0035 placeholder, 0.002 / 0.001 ASSUMED}, TRAIN and VALIDATION
  only (the test segment was not evaluated and the TestSetGuard was not consumed).
  `v2_backtest_top_*`: full reports of the runner's S1 top candidate (E4: S1 took no trades).
  `v2_summary.md`: best/worst exit per strategy, evidence count, buy-and-hold, S1 cost gate.
  Reproduce: `python -m wallexbot backtest --cost-source recorded --bar-minutes 15 60 --report-prefix v2_`.

## Which fee column is realistic (2026-10-22)

The `v2_*` files are historical and unchanged: they still call 0.0035 the "primary"/placeholder fee and
rank best/worst exits at it. The config default is now 0.002 (`fees.taker_rate`, the flat 0.2 % for USDT
pairs on Wallex's public, undated help page; still not our account's verified rate), so the realistic
scenario is now the `assumed_0.002` column of `v2_summary.md`. In that column: S0 with the 15m train-best
exit (E4) returns -8.84% on train (60 trades) and -8.17% on validation (54 trades); S0 with the 60m
train-best exit (E2) returns -3.15% on train (33 trades) and -4.65% on validation (18 trades, n<30); the
evidence count is 0/8 positive on train and 0/8 on validation for both bar sizes; S1 takes 0 trades, and
its cost gate passes on 0 bars for every symbol, with round trips from 77 bps (BTCUSDT) to 326 bps
(PEPEUSDT); equal-weight buy-and-hold returns 5.69% / -7.89% (15m train / validation) and 5.76% / -8.22%
(60m). The verdict below holds unchanged at 0.002.

## Verdict (v2, recorded costs)

There is no evidence of a net edge. Not one of the 48 strategy/exit/fee/bar-size configurations is
positive on train or on validation, at any fee scenario, including the optimistic 0.001 assumption.
S0 loses on every run. On 15m every run ends at or near the -8% soft-drawdown limit, after which
entries are blocked (43-104 trades per run, win rate 2-36%, profit factor <= 0.40, expectancy -0.7 to
-1.9 R). On 1h it loses 2.5-8.2% on train and 4.3-5.4% on validation, where it takes fewer than 30
trades. The cost-free P&L of the trades S0 took is small on train (-35 to +523 USDT on 10000) and
negative on validation in every run (-47 to -209 USDT). So the losses are not only a cost artefact,
and the realistic costs (about 57-107 bps per round trip for BTC, more for the other symbols) are far
larger than any gross edge S0 shows. S1 takes zero trades: its cost gate does not
pass on a single train or validation bar for any symbol at any fee. Even at the 0.001 fee, the cheapest
symbol's round trip (BTCUSDT, 57 bps) needs a 15m ATR of about 1.1% of price. The S1 abstention is
therefore the cost gate working as designed, not evidence for or against S1's signal. Caveats that
could change the cost side, though not the gross picture: the calibration covers about an hour; the
p90 is charged on every fill; the Wallex fee is unverified; and all of this is one ~45-day regime.
