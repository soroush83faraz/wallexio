"""Gold add-on (src/wallexbot/gold): data fetch/parse, G1 strategy (incl. no look-ahead), the
report pipeline, the shadow replay through the real bot path, and the --live smoke test. No test
touches the network: every transport is a fake."""
from __future__ import annotations

import calendar
import json
import math
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from wallexbot import cli
from wallexbot.backtest.engine import BacktestParams, run_backtest
from wallexbot.data.candles import Candle, write_candles_csv
from wallexbot.exchange.wallex_public import WallexPublicClient
from wallexbot.gold import backtest as gb
from wallexbot.gold import binance
from wallexbot.gold import compare as gc
from wallexbot.gold import data as gdata
from wallexbot.gold import shadow as gs
from wallexbot.gold.report import render_markdown
from wallexbot.gold.strategy import G1Engine, G1Params, G1Trail, daily_bars
from wallexbot.strategy.contract import MarketContext, PositionState
from wallexbot.strategy.lookahead import check_no_lookahead
from wallexbot.types import SymbolRules

D = Decimal
H = 3600
REPO = Path(__file__).resolve().parents[1]
GOLD_DIR = REPO / "gold"
RULES = SymbolRules("PAXGUSDT", "PAXG", "USDT", amount_precision=6, price_precision=2)
MC = MarketContext(spread_bps=D(15), est_slippage_bps=D(0), taker_fee_rate=D("0.002"))
START = calendar.timegm((2026, 1, 1, 0, 0, 0))


def synth_hours(n_days: int, start: int = START, *, rally_from_day: int = 100, rally_days: int = 15,
                fall_days: int = 10) -> list[Candle]:
    """Deterministic gold-like hourly series: a gentle wave, then a rally (breakouts), then a fall."""
    out, px = [], 4000.0
    for i in range(n_days * 24):
        day = i // 24
        drift = 0.0
        if rally_from_day <= day < rally_from_day + rally_days:
            drift = 0.0012
        elif rally_from_day + rally_days <= day < rally_from_day + rally_days + fall_days:
            drift = -0.0015
        nxt = px * (1 + drift + 0.002 * math.sin(i / 7.0))
        o, c = D(f"{px:.2f}"), D(f"{nxt:.2f}")
        hi = max(o, c) + D("1.50")
        lo = min(o, c) - D("1.50")
        out.append(Candle(start + i * H, o, hi, lo, c, D("10")))
        px = nxt
    return out


# ------------------------------------------------------------------ strategy
def test_daily_bars_use_completed_days_only():
    bars = synth_hours(5, START + 5 * H)            # starts at 05:00 of day d0: d0 is truncated
    d0 = START // 86400
    # last bar 07:00 of day d0+3: d0 dropped (truncated), d0+3 not finished -> only d0+1, d0+2
    db = daily_bars(bars[: 24 * 3 + 3])
    assert db.day == (d0 + 1, d0 + 2)
    assert db.high[0] == max(c.high for c in bars if c.ts // 86400 == d0 + 1)
    assert db.close[1] == [c for c in bars if c.ts // 86400 == d0 + 2][-1].close
    # a day is used only once its last hour has closed
    window = bars[: 19 + 24]                         # ends with the 23:00 bar of day d0+1
    assert daily_bars(window).day == (d0 + 1,)
    assert daily_bars(window[:-1]).day == ()         # one hour earlier: d0+1 not finished yet


def test_g1_has_no_lookahead_and_the_check_is_not_vacuous():
    bars = synth_hours(140)
    eng = G1Engine()
    signal_cuts = [t for t in range(24 * 100, len(bars), 7) if eng.evaluate("PAXGUSDT", bars[:t], MC) is not None]
    assert signal_cuts, "synthetic rally must produce signals"
    cuts = signal_cuts[:10] + list(range(24 * 60, len(bars) - 1, 97))
    assert check_no_lookahead(eng, bars, MC, sample_points=cuts, seed=3) >= 1


def test_g1_decision_ignores_the_unfinished_day_and_future_bars():
    bars = synth_hours(140)
    eng = G1Engine()
    t = next(t for t in range(24 * 100, len(bars)) if eng.evaluate("PAXGUSDT", bars[:t], MC) is not None)
    sig = eng.evaluate("PAXGUSDT", bars[:t], MC)
    # rewriting every LATER bar (same timestamps, wild prices) cannot change the decision at t
    wild = bars[:t] + [replace(c, open=c.open * 3, high=c.high * 3, low=c.low * 3, close=c.close * 3)
                       for c in bars[t:]]
    assert eng.evaluate("PAXGUSDT", wild[:t], MC) == sig
    assert sig.bar_ts == bars[t - 1].ts and sig.stop_price < sig.entry_ref_price


def test_backtest_entries_do_not_depend_on_later_data():
    """No look-ahead end to end: the engine run on a truncated series makes exactly the same entry
    decisions before the cut as the run on the full series."""
    bars = synth_hours(140)
    p = G1Params()
    cut = bars[24 * 108].ts

    def entries(series):
        res = run_backtest({"PAXGUSDT": series}, G1Engine(p), G1Trail(p), gb.Variant("V0", "").cost_model(),
                           gb.repo_risk(), {"PAXGUSDT": RULES}, D(1000),
                           BacktestParams(bar_interval_s=H, trade_start_ts=bars[24 * 90].ts,
                                          lookback_bars=p.lookback_bars))
        return [(f.ts, f.price, f.qty) for f in res.fills if f.kind == "entry" and f.ts < cut]

    full = entries(bars)
    assert full, "the window before the cut must contain an entry"
    assert entries([c for c in bars if c.ts < cut]) == full


def test_trail_only_raises_the_stop():
    bars = synth_hours(140)
    trail = G1Trail()
    st = PositionState("PAXGUSDT", D(4000), bars[-1].ts, D("0.05"), D(3900), D(3990), D(5000), 10,
                       current_stop=D(3900))
    d = trail.on_bar(st, bars)
    assert d.action == "update_stop" and d.new_stop > D(3900)
    assert trail.on_bar(replace(st, current_stop=D(4999)), bars).action == "hold"


def test_params_reject_a_too_short_lookback():
    with pytest.raises(ValueError):
        G1Params(lookback_bars=24 * 30)


# ------------------------------------------------------------------ binance
def _klines(start: int, n: int, step: int = H) -> list:
    return [[(start + i * step) * 1000, "4000.10000000", "4001.00000000", "3999.00000000", "4000.50000000",
             "12.50000000", (start + (i + 1) * step) * 1000 - 1] for i in range(n)]


def test_binance_paginates_drops_the_forming_bar_and_pins_the_host():
    start, calls = START, []

    def transport(url, timeout):
        calls.append(url)
        assert url.startswith("https://api.binance.com/api/v3/klines?")
        q = dict(p.split("=") for p in url.split("?")[1].split("&"))
        s = int(q["startTime"]) // 1000
        n = min(1000, (start + 2500 * H - s) // H)
        return 200, json.dumps(_klines(s, n)).encode()

    now = start + 2400 * H + 10          # bar 2400 is still forming (2399 closed 10 s ago)
    bars = binance.fetch_klines("PAXGUSDT", "1h", start, start + 2499 * H, transport=transport,
                                clock=lambda: now, sleep=lambda s: None)
    assert len(bars) == 2400 and bars[-1].ts == start + 2399 * H
    assert len(calls) == 3
    assert bars[0].close == D("4000.5") and bars[0].volume == D("12.5")
    with pytest.raises(binance.BinanceDataError):
        binance.check_url("https://evil.example/api/v3/klines")
    with pytest.raises(binance.BinanceDataError):
        binance.parse_klines([[START * 1000 + 5, "1", "1", "1", "1", "1"]], H)


# ------------------------------------------------------------------ data reports
def test_basis_and_depth_reports():
    b = [Candle(START + i * H, D(100), D(101), D(99), D(100), D(1)) for i in range(4)]
    w = [Candle(START + i * H, D(99), D(99), D(99), D(99), D(1 if i % 2 else 0)) for i in range(4)]
    rep = gdata.basis_report(b, w)
    assert rep["traded_hours"]["n"] == 2 and rep["traded_hours"]["median_bps"] == pytest.approx(-100)
    assert rep["zero_volume_hour_share"] == 0.5
    snap = {"symbol": "X", "ts": 1, "asks": [["101", "10"]], "bids": [["99", "10"]]}
    dep = gdata.depth_report([snap], notionals=(250, 5000))["X"]
    assert dep["median_half_spread_bps"] == pytest.approx(100)
    assert dep["vwap_cost_vs_mid_bps"]["250"]["BUY"] == pytest.approx(100)
    assert dep["vwap_cost_vs_mid_bps"]["5000"]["BUY"] is None     # deeper than the visible book


SYNTH_START = calendar.timegm((2026, 2, 8, 0, 0, 0))     # 52 warm-up days before the 2026-04-01 evaluation


def write_synthetic_data(d: Path, n_days: int = 82) -> list[Candle]:
    """A gold/data-shaped directory over synthetic data (evaluation window starts 2026-04-01)."""
    bars = synth_hours(n_days, SYNTH_START, rally_from_day=56)
    write_candles_csv(d / gdata.F_BINANCE_1H, bars)
    m5_start = bars[-24 * 6].ts
    m5 = [Candle(c.ts + k * 300, c.close, c.close + D("0.5"), c.close - D("0.5"), c.close, D(1))
          for c in bars if c.ts >= m5_start for k in range(12)]
    write_candles_csv(d / gdata.F_BINANCE_5M, m5)
    write_candles_csv(d / gdata.F_BINANCE_15M, [c for c in m5 if c.ts % 900 == 0])
    for sym in gdata.GOLD_MARKETS:
        write_candles_csv(d / gdata.wallex_file(sym), [replace(c, close=c.close, volume=D(1)) for c in bars[-500:]])
    (d / gdata.F_MARKETS).write_text(json.dumps({"markets": {"PAXGUSDT": {
        "symbol": "PAXGUSDT", "base_asset": "PAXG", "quote_asset": "USDT", "amount_precision": 6,
        "price_precision": 2, "quote_volume_24h": "1500", "volume_24h": "0.4"}}}))
    snaps = [{"symbol": s, "ts": 1, "asks": [["4010", "1"]], "bids": [["3990", "1"]]} for s in gdata.GOLD_MARKETS]
    (d / gdata.F_DEPTH).write_text("\n".join(json.dumps(s) for s in snaps) + "\n")
    (d / gdata.F_META).write_text(json.dumps({"fetched_at_utc": "synthetic"}))
    return bars


def test_report_markdown_comes_from_the_json(tmp_path):
    """One report over synthetic data: the markdown is a pure rendering of the JSON, the pre-registered
    runs are all there, monthly returns chain to the total, stresses cost more. (Determinism across
    processes is checked on the committed data below.)"""
    write_synthetic_data(tmp_path / "data")
    rep1 = gb.build_report(tmp_path / "data")
    jp, mp = gb.write_report(rep1, tmp_path / "out")
    assert gb.dumps(rep1) == jp.read_text(encoding="utf-8")
    loaded = json.loads(jp.read_text(encoding="utf-8"))
    md = mp.read_text(encoding="utf-8")
    assert md == render_markdown(loaded)
    assert set(loaded["variants"]) == {"V0", "V1", "V2", "V3", "V4", "S1", "S2", "S3", "S4"}
    v0 = loaded["variants"]["V0"]
    assert f"{v0['total_return'] * 100:+.2f}%" in md and f"{v0['max_drawdown'] * 100:.2f}%" in md
    assert ("NOT MET" in md) == (not v0["target"]["met"])
    # monthly returns chain to the total return
    prod = 1.0
    for r in v0["monthly"]:
        prod *= 1 + r["return"]
    assert prod - 1 == pytest.approx(v0["total_return"], abs=1e-6)
    assert v0["trades"] >= 1
    v = loaded["variants"]
    assert v["S1"]["fees"] > v["V0"]["fees"] and v["S2"]["impact_cost"] > v["V0"]["impact_cost"]
    assert v["S4"]["spread_cost"] > v["V0"]["spread_cost"]


# ------------------------------------------------------------------ committed artifacts
@pytest.mark.skipif(not (GOLD_DIR / "report.json").is_file(), reason="gold report not generated")
def test_committed_report_markdown_matches_its_json():
    rep = json.loads((GOLD_DIR / "report.json").read_text(encoding="utf-8"))
    assert (GOLD_DIR / "REPORT.md").read_text(encoding="utf-8") == render_markdown(rep)


@pytest.mark.skipif(not (GOLD_DIR / "data" / gdata.F_BINANCE_1H).is_file(), reason="gold data not present")
def test_committed_data_reproduces_the_v0_headline():
    """Checksums verify on load, and V0 rerun on the committed CSVs equals report.json."""
    d = gdata.load(GOLD_DIR / "data")
    rep = json.loads((GOLD_DIR / "report.json").read_text(encoding="utf-8"))
    res = gb.run_variant(d.h1, gdata.symbol_rules(d), gb.variants(D("42"))[0])
    s = json.loads(gb.dumps(gb.summarize(res, gb.EVAL_START_TS, with_trades=True)))
    v0 = rep["variants"]["V0"]
    for k in ("total_return", "max_drawdown", "trades", "final_equity", "monthly", "result_hash", "trade_list"):
        assert s[k] == v0[k], k


@pytest.mark.skipif(not (GOLD_DIR / "shadow.json").is_file(), reason="gold shadow report not generated")
def test_committed_shadow_markdown_matches_its_json():
    rep = json.loads((GOLD_DIR / "shadow.json").read_text(encoding="utf-8"))
    assert (GOLD_DIR / "SHADOW.md").read_text(encoding="utf-8") == gc.render_shadow(rep)


# ------------------------------------------------------------------ shadow (real bot path, offline)
def test_shadow_replay_trades_through_the_bot_path(tmp_path):
    bars = synth_hours(140, rally_from_day=120)
    rally = calendar.timegm((2026, 5, 1, 0, 0, 0))
    m5 = [Candle(c.ts + k * 300, c.close, c.close, c.close, c.close, D(1)) for c in bars if c.ts >= rally - 86400
          for k in range(12)]
    data = gdata.GoldData(tuple(bars), tuple(m5), (), {}, {"PAXGUSDT": {
        "symbol": "PAXGUSDT", "base_asset": "PAXG", "quote_asset": "USDT", "amount_precision": 6,
        "price_precision": 2}}, (), {})
    db, journal = tmp_path / "s.db", tmp_path / "j.jsonl"
    out = gs.run_replay(data, db, journal, start_ts=rally, end_ts=rally + 6 * 86400)
    fills = gs.journal_fills(journal)
    assert out["ticks_with_errors"] == 0 and not out["kill_switch_engaged"]
    assert any(f["side"] == "BUY" for f in fills), out
    assert out["decisions"].get("signal", 0) >= 1 and out["decisions"].get("entry_submitted", 0) >= 1
    trips = gc.shadow_trips(fills, gdata.symbol_rules(data))
    assert trips and trips[0]["entry_price"] > 0


def test_synthetic_book_spread_never_exceeds_the_risk_limit():
    for px in (D("4000.00"), D("4123.45"), D("3999.99")):
        bids, asks = gs.synthetic_book(px, 2, D("7.5"))
        mid = (bids[0][0] + asks[0][0]) / 2
        assert (asks[0][0] - bids[0][0]) / mid * 10000 <= 15
        assert bids[0][0] < px < asks[0][0]


def test_replay_feed_only_shows_closed_bars_and_quotes(cfg):
    bars = synth_hours(3)
    m5 = [Candle(c.ts + k * 300, c.open, c.high, c.low, c.close, D(1)) for c in bars for k in range(12)]
    feed = gs.GoldReplayFeed(bars, m5, {"PAXGUSDT": RULES}, cfg, symbol="PAXGUSDT")
    t = bars[10].ts + 1800                               # half way through hour 10
    feed.refresh(t)
    assert feed.closed_bars("PAXGUSDT", 100)[-1].ts == bars[9].ts
    q = feed.quote("PAXGUSDT")
    assert q.ts == t and q.bid < m5[10 * 12 + 5].close < q.ask   # last CLOSED 5m candle: index 10*12+5


# ------------------------------------------------------------------ live (smoke, offline fakes)
def test_live_shadow_smoke_with_fake_transports(tmp_path):
    hist = synth_hours(140)
    now = [hist[-1].ts + H + 30.0]
    depth = {"result": {"ask": [{"price": "4012.5", "quantity": "1.0", "sum": "4012.5"}],
                        "bid": [{"price": "4011.5", "quantity": "1.0", "sum": "4011.5"}]}, "success": True}
    wallex_urls, binance_urls = [], []

    def wallex_transport(method, url, timeout):
        wallex_urls.append(url)
        assert method == "GET" and url.startswith("https://api.wallex.ir/v1/depth?symbol=PAXGUSDT")
        return 200, json.dumps(depth).encode()

    def binance_transport(url, timeout):
        binance_urls.append(url)
        q = dict(p.split("=") for p in url.split("?")[1].split("&"))
        s, e = int(q["startTime"]) // 1000, int(q["endTime"]) // 1000
        rows = [[c.ts * 1000, str(c.open), str(c.high), str(c.low), str(c.close), str(c.volume)]
                for c in hist if s <= c.ts <= e][:1000]
        return 200, json.dumps(rows).encode()

    def sleep(s):
        now[0] += s

    client = WallexPublicClient(transport=wallex_transport, clock=lambda: now[0], sleep=sleep)
    logs = []
    out = gs.run_live(tmp_path / "live.db", tmp_path / "live.jsonl", symbol="PAXGUSDT", max_ticks=3,
                      client=client, binance_transport=binance_transport, clock=lambda: now[0], sleep=sleep,
                      rules=RULES, log=logs.append, guardian=False)
    assert out["ticks"] == 3 and out["ticks_with_errors"] == 0
    assert wallex_urls and binance_urls and len(logs) == 3
    assert "bid=4011.5" in logs[0]
    assert out["decisions"].get("bar_done", 0) >= 1      # the bot evaluated the newest closed 1h bar


def test_cli_registers_the_gold_commands():
    names = set(cli.build_parser()._subparsers._group_actions[0].choices)
    assert {"gold-fetch", "gold-backtest", "gold-shadow"} <= names
    assert cli.main(["gold-backtest", "--arm-live"]) == 2
