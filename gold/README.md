# Gold (wallexio): pre-registered strategy and test plan

Written on 2026-10-23, **before any gold price data was downloaded or looked at**. Nothing in this
file was changed after the results were seen, except the "Results" pointer at the bottom
(the results live in [REPORT.md](REPORT.md) and `report.json`).

Question asked by the owner: can this repo make about **3% per month** on gold over the last ~6
months, on the gold tokens listed on Wallex (PAXGUSDT, XAUTUSDT)? The answer is whatever the
numbers say, including "no" or "it just tracks buy-and-hold".

## Data (fixed in advance)

- Price series: Binance public klines **PAXGUSDT 1h**, from 2026-02-01 00:00 UTC to the last
  closed hour at download time. Binance is used because Wallex only serves ~45 days of 1m candles
  and its 1h series is truncated; the strategy trades the Wallex market, so the Binance-vs-Wallex
  price basis is measured on the overlap and reported (Wallex PAXGUSDT and XAUTUSDT candles from
  the repo's own host-pinned public client).
- Shadow replay quotes: Binance PAXGUSDT **5m** klines for the last ~31 days (a finer price path
  inside each hour, so software stops are checked every 5 minutes).
- Files are committed under `gold/data/` with a `.sha256` sidecar (the repo's candle CSV format),
  so every number in the report is reproducible offline.

## Windows (fixed in advance)

- Warm-up: 2026-02-01 to 2026-03-31 (59 days). Indicators only; **no trades**. The 50-day EMA
  needs 50 completed days, so this period is warm-up, not an in-sample fitting period: no
  parameter is fitted anywhere.
- Evaluation: **2026-04-01 00:00 UTC to the end of the data** (about 6.7 months). Per calendar
  month table (April to September complete, October partial and flagged).
- Shadow: the last 30 complete UTC days of the data.

## Strategy G1: daily trend filter + 20-day breakout + ATR trailing stop (long only, spot)

Rationale: gold trends over weeks to months and is slow on an hourly scale; a classic,
textbook trend-following rule (Donchian breakout with a trend filter and a volatility stop) is the
least "fitted" choice. Decisions are taken on 1h closes, but every indicator is on **daily** bars
built from completed UTC days only (the current, unfinished day is never used).

| Rule | Value |
|---|---|
| Daily bars | UTC-day aggregates of the 1h bars, completed days only |
| Trend filter | last 1h close > EMA(**50**) of daily closes |
| Entry trigger | last 1h close > highest daily high of the previous **20** completed days |
| Fill | next 1h bar open (the engine's next-bar-open rule) |
| Initial stop | signal close - **2.0** x ATR(14, daily, Wilder) |
| Trailing stop | highest high since entry - **3.0** x ATR(14, daily), ratchets up only, updated on every 1h close; stop touches fill intrabar at the stop (gap: at the open) |
| Take-profit / time stop | none |
| Re-entry cooldown | 24 hourly bars after any exit, plus the repo's 3-losses-in-a-row / 60-minute cooldown |
| Sizing | repo `size_position`: risk_per_trade_pct **3.0**, capped by max_position_pct **25**, max_exposure_pct **60**, max_order_value **1000** USDT, min_notional 5 (default_config.yaml) |
| Loss limits | repo engine: daily loss soft/hard 4/6 %, drawdown soft/hard 8/12 % (hard = flatten, halt for the UTC day) |
| Starting equity | 1000 USDT |

Note written in advance: with the repo's 25 % max position, a fully invested trade moves equity
by only 25 % of gold's move. 3 %/month for the strategy then needs gold to rise about 12 % in the
months it is held. This is a property of the repo's risk caps, not of gold.

## Costs (repo cost model, fixed in advance)

Every fill is a taker fill: Wallex taker fee **0.2 %** per side (documented public rate, our tier
unverified), half-spread **7.5 bps**, linear impact `0.2 x order notional / previous bar notional`
capped at 50 bps. The liquidity proxy is the Binance bar's notional volume, which **overstates
Wallex liquidity**, so impact is near zero in the base case; that is why the impact-at-cap stress
below exists. Base round trip is therefore about 0.55 %.

## Every run that will be reported (no others)

| id | what |
|---|---|
| V0 | G1 as above (the headline) |
| V1 | stops x0.8: initial 1.6 ATR, trail 2.4 ATR |
| V2 | stops x1.2: initial 2.4 ATR, trail 3.6 ATR |
| V3 | lookbacks x0.8: EMA 40, breakout 16 days |
| V4 | lookbacks x1.2: EMA 60, breakout 24 days |
| S1 | V0 with fees x2 (0.4 % per side) |
| S2 | V0 with impact always at the 50 bps cap (thin Wallex book stress) |
| S3 | V0 with the 25 % position cap and 60 % exposure cap lifted to 100 % and no max order value (spot, no leverage). Structural "what does the repo's cap cost" check, not a recommendation |
| B&H | buy PAXG at the first evaluation open, sell at the last close, same cost model; at 100 % and at 25 % of equity (the strategy's maximum position) |

V1-V4 are the +/-20 % neighbours. No variant is selected as "best": V0 is the result.

## Success criterion (fixed in advance)

The ~3 %/month target is **met** only if V0's geometric average monthly return over the
evaluation window, `(1 + total)^(30.4375 / days) - 1`, is >= 3.0 %. The number of complete months
with a return >= 3 % and the arithmetic mean of the complete months are reported as well.

## Shadow (fixed in advance)

The same G1 strategy object and exit policy run inside the repo's real `bot.loop.Bot` (risk
engine, OMS, append-only SQLite audit store, in-process guardian) with the repo's `ShadowBroker`
as the broker, fed by an offline replay of the committed Binance data (1h bars for decisions,
5m closes for quotes, a synthetic book of +/-7.5 bps around each quote). No network, no orders.
Its fills and P&L are compared with the pure backtest over the same 30 days.

## Addendum, 2026-10-23, after the data download and BEFORE any strategy run

The Wallex order-book snapshots taken with the data (`gold/data/wallex_depth.jsonl`, 5 snapshots
per market) show that the two gold markets are very different:

- **PAXGUSDT on Wallex**: median half-spread about 42 bps (worst snapshot about 94 bps), 24h volume
  about 1,500 USDT, 63 % of hours without a single trade, and its closes sit a median of about
  3.4 % below Binance PAXG. The repo's 7.5 bps half-spread is far too optimistic for this market.
- **XAUTUSDT on Wallex**: median half-spread about 0.15 bps, 24h volume about 240,000 USDT, closes
  a median of about 0.2 % below Binance PAXG. 7.5 bps is conservative here.

One cost stress is therefore added (no strategy parameter changes):

| id | what |
|---|---|
| S4 | V0 with the half-spread set to the observed Wallex PAXGUSDT median (42 bps per side) |

## Addendum 2, after the first 30-day shadow replay

The 30-day shadow window (2026-09-23 to 2026-10-23) produced **no signal at all** (neither in the
bot replay nor in the backtest of the same days), so it compares nothing. To actually exercise and
compare the fill path, a second shadow replay is added over the **whole evaluation window**
(2026-04-01 to the end of the data) with Binance 15m closes as quotes
(`gold/data/binance_PAXGUSDT_15m.csv`). Same strategy, same parameters; it is a mechanics check of
backtest vs bot path, not an extra performance claim. Both replays are reported in SHADOW.md.

## Results

See [REPORT.md](REPORT.md) (generated by `python -m wallexbot gold-backtest`) and `report.json`;
shadow replays in [SHADOW.md](SHADOW.md) (`python -m wallexbot gold-shadow`) and `shadow.json`.

Code: `src/wallexbot/gold/` (`strategy.py` G1, `backtest.py` runs and statistics, `report.py`
REPORT.md renderer, `shadow.py` replay / live feeds on the bot stack, `compare.py` shadow vs
backtest, `data.py` / `binance.py` downloads, `cli.py` commands). Tests: `tests/test_gold.py`.
