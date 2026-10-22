# Execution costs: calibration from recorded order books

Status: **calibrated from a small, single-period sample (low confidence).** The numbers are in
`reports/cost_calibration.{json,md}`; the backtest runs that use them are `reports/v2_*`. Nothing here
is a verified Wallex fact.

## Why

The first exploratory backtest (`reports/backtest_grid.md`, pre-fix engine `bt-0.1`) priced every fill
with a 7.5 bps half spread plus a market-impact term `0.2 x order notional / previous bar notional`,
capped at 50 bps. For 1000 USDT orders that proxy sat at its cap almost everywhere, because 15m candle
volume on Wallex is small (and its semantics are unverified). The same cap (50 bps) was also fed to the
S1 cost gate. The proxy is therefore not a measurement of anything.

## Method (`backtest/calibrate.py`)

Input: the recorder's `depth_YYYYMMDD.jsonl` (`data/recorder.py`): one row per symbol per cycle with the
top 10 bid and ask levels (`[price, qty]` strings, qty in base units), sampled every ~15-35 s.

For every valid snapshot (both sides non-empty, best ask > best bid):

- `mid = (best_bid + best_ask) / 2`, `spread_bps = (ask - bid) / mid x 1e4`, half spread = spread / 2.
- For each order notional N in {250, 500, 1000, 2500} USDT:
  - BUY: walk the asks from the best price, spending N quote; SELL: walk the bids, receiving N quote.
    `VWAP = N / base filled`.
  - `slippage_vs_mid_bps = |VWAP - mid| / mid x 1e4` (what a marketable order pays relative to mid);
  - `impact_beyond_touch_bps = |VWAP - touch| / mid x 1e4` (the part beyond the best level), so per
    snapshot `slippage_vs_mid = half_spread + impact_beyond_touch` exactly.
  - If N exceeds the quote value of the 10 visible levels the walk is undefined: the snapshot is counted
    as **insufficient depth** and ranks as +infinity in the quantiles (a quantile that lands on such a
    snapshot is reported as `null` / "insufficient", never as an optimistic number).
- Per symbol: median and p90 of each quantity (upper nearest-rank: the ceil(q x n)-th smallest value,
  no interpolation), visible depth per side, sample count, first/last time, covered hours, number of
  sessions (gaps > 120 s split sessions), UTC hours covered, and a confidence note.
- Snapshots are sorted by time and de-duplicated on (symbol, ts); a torn last line (the recorder may be
  writing) is skipped and flagged. Output values are Decimal rounded UP to 0.01 bps. The JSON carries the
  sha256 of every input file and a `calibration_sha256` of its own content, and no wall-clock time, so
  the same input bytes always give the same file.
- A symbol is **usable** with >= 20 valid snapshots and at least one notional whose p90 is defined on
  both sides. Unusable symbols fall back to the proxy model in the backtest and are labelled as such.

## Cost model (`backtest/costs.py`, `COST_MODEL_VERSION` cm-0.2)

`CostModel.from_calibration(cal, symbol, quantile="p90")` builds a `cost_source="recorded"` model:

- `half_spread_bps` = p90 of the recorded half spread.
- Impact curve over the ORDER notional: at each calibrated N, the worse of BUY and SELL of
  `max(p90 impact beyond touch, p90 slippage vs mid - p90 half spread, 0)`. So half spread + impact is
  never below the p90 slippage vs mid at a calibrated point, on either side. Notionals whose p90 is
  undefined (insufficient depth) are dropped.
- Between points: linear interpolation. Below the first point (250 USDT): flat at the first value (not
  zero). Above the last defined point: extrapolated with the last segment's slope (floored at 0; a single
  point extrapolates along the line through the origin). Capped at 100 bps; fills priced at the cap or by
  extrapolation are counted per run (`fill_impact_status`: below_range / interpolated / extrapolated /
  capped / proxy).
- The curve is non-decreasing, so impact is monotone in quantity (the engine's two-pass sizing needs it).
- The S1 cost gate (`market_context`) sees the full recorded spread plus the curve's impact at 1000 USDT
  on BOTH legs (entry and exit). The proxy model still reports its 50 bps cap once, as before.
- `CostBook` holds one model per symbol (recorded where calibrated, proxy elsewhere); the engine prices
  each fill and builds each symbol's market context from that symbol's model. `COST_MODEL_VERSION` is
  part of the engine's inputs hash; `ENGINE_VERSION` (bt-0.2) is unchanged because fill semantics are.
- Recorded and proxy runs differ only through the cost term: with zero costs both produce identical
  trades, fills and equity; with costs, entries/exits and per-unit frictionless P&L are the same for
  cost-independent strategies, and `net = gross - fees - spread - impact` holds exactly
  (`tests/test_cost_calibration.py`).

## Assumptions and limitations

- **Sample size.** At calibration time the recorder had about one hour of usable data in two or three
  short sessions (late evening and early morning UTC), not the "~3 hours" expected: the recorder was
  down for several hours in between. One session of a few hours is NOT representative of all hours,
  weekdays, volatility regimes or news events; spreads and depth are known to vary strongly with all of
  them. The p90 of a quiet window can be below the median of a volatile one.
- **p90 choice.** Every fill in the backtest is charged the p90 half spread and the p90 impact: a
  deliberately conservative, constant cost. Real costs vary per fill; the median is lower (both are in the
  calibration file). `quantile="median"` exists for sensitivity checks only.
- **Measured spreads are wide.** In the recorded sample the median BTCUSDT quoted spread was about
  19 bps (p90 ~24), ETHUSDT ~38 bps, PEPEUSDT ~64 bps: far wider than the ~5 bps sometimes quoted
  for BTCUSDT and wider than the old 15 bps (7.5 half spread) assumption. Thin books also make the
  BUY/SELL sides asymmetric (e.g. little BTC bid depth); the model charges the worse side to both.
- **Top 10 levels only.** Hidden or deeper liquidity, refills between snapshots, queue position,
  latency, partial fills and our own impact on later snapshots are not modelled. Orders bigger than the
  visible depth are extrapolated (flagged).
- **Fee.** The grid's fee scenarios are 0.0035 (conservative; the former placeholder), 0.002 (the documented public
  USDT-pair rate, now the config default) and 0.001 (offer / top tier); our account's
  real fee is unverified (public figures below). It is charged in quote on the notional, whereas Wallex
  charges it per fill in `feeAsset`; the docs' trade example (a BUY on USDTTMN) shows it in the received
  asset ([socket-private-channel](https://developers.wallex.ir/docs/socket-private-channel), 2026-10-22).
- **Candle volume is unreliable** as a liquidity measure (forward-filled zero-volume minutes, unverified
  units); the recorded model does not use it. The proxy model still does, for uncalibrated symbols.
- **Timing.** The books were recorded after the end of the 45-day candle history (after the reserved
  test segment). They carry no price-path information about train/validation/test, only cost levels;
  cost levels in September-October may have been different.
- Exit parameters are unchanged (`ExitParams.breakeven_buffer_bps` stays 20 bps): with recorded costs an
  E4 "breakeven" stop can still realise a small loss on symbols whose exit cost is above 20 bps.

## Fee: public sources (retrieved 2026-10-22)

None of these is the fee of OUR account: Wallex fees are tier-dependent, and only `GET /v1/account/fee`
(read key; `python -m wallexbot doctor` prints the highest maker/taker) returns ours. Persian sources, our
translation. The config default `fees.taker_rate` / `fees.maker_rate` is 0.002 (the help-centre figure below; it was
0.0035, an unverified planning assumption, until 2026-10-22).

| Source | Date | What it says |
|---|---|---|
| Help centre, [commissions-calculate](https://wallex.ir/help/knowledge-base/commissions-calculate/) | undated | Toman-market fee by user level (last 3 months' volume); pairs such as BTC-USDT "fixed 0.2 %", independent of level. |
| Blog, ["spot fees at Wallex reach 0.1 %"](https://wallex.ir/blog/%da%a9%d8%a7%d8%b1%d9%85%d8%b2%d8%af-%d9%85%d8%b9%d8%a7%d9%85%d9%84%d8%a7%d8%aa-%d8%a7%d8%b3%d9%be%d8%a7%d8%aa-%d8%af%d8%b1-%d9%88%d8%a7%d9%84%da%a9%d8%b3-%d8%a8%d9%87-%db%b0%d9%ab%db%b1%d9%aa-%d9%85/) | 2025-08-20 | New spot structure for Toman AND USDT markets, rates set by the last 30 days' volume (margin excluded); a 30-day 0.1 % offer for every user who activates it; otherwise 0.1 % only at high volume (e.g. over 20 billion toman a month). No tier table. |
| API docs, [basic-services Swagger](https://developers.wallex.ir/assets/files/basic-services-89626420b5f06e4d7a2dc9206cdeb192.json) `FeeItem` | — | Example `"makerFeeRate": "0.00200000"`, `"takerFeeRate": "0.00200000"` for BTCUSDT: an example, not a schedule. |
| Fee page, [wallex.ir/commission](https://wallex.ir/commission) | — | "Fees are a percentage of the total trade amount ... log in to view". Its table is loaded from `api.wallex.ir/v2/structured-fee`, not fetched (only developers.wallex.ir and wallex.ir were in scope). |
| Markets API `is_zero_fee` | 2026-10-21 | false for all six USDT markets in `tests/fixtures/markets.json`. Past zero-fee offers (DOGE/SHIB in 2023, selling DOGS in 2024) were time-limited promotions. |

Not found publicly: the current maker/taker table per tier for USDT markets, any fee-token discount, and
the competition's fee rules (login-gated). The two public numbers conflict in time: the undated "0.2 %
flat for USDT pairs" may predate the 2025-08 tiering.

**Scenario note `public_usdt_0.002` (now the config default):** the grid's existing `assumed_0.002` row equals
the help-centre figure and the docs' example; `assumed_0.001` equals the 0.1 % offer or top tier. The default
`fees.taker_rate` / `fees.maker_rate` is 0.002; it is still not our verified rate. Read `GET /v1/account/fee`
(`python -m wallexbot doctor`) before trusting it, and set the config to the account's highest rate if it differs.

## Refreshing the calibration

```
python -m wallexbot calibrate-costs [--recorder-dir data/recorder] [--symbols ...] \
    [--notionals 250 500 1000 2500] [--out reports/cost_calibration.json]
python -m wallexbot backtest --cost-source recorded --bar-minutes 15 60 --report-prefix v2_
```

`calibrate-costs` copies `depth_*.jsonl` to a temporary directory first (the recorder may be writing),
calibrates the copies and writes `cost_calibration.json` plus a Markdown rendering. Because the live files
keep growing, the input sha256 values identify exactly which bytes were used; re-running later gives a
different calibration. Refresh after the recorder has covered several days including every UTC hour
(the per-symbol `confidence` only becomes "medium" with all 24 UTC hours over >= 7 days of coverage), and
re-run the backtest afterwards. `backtest` uses `reports/cost_calibration.json` by default when it exists
(`--cost-source proxy` forces the old model).
