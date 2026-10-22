"""Cost calibration from recorded books, the recorded CostModel (interpolation / extrapolation / cap),
CostBook, and the engine invariant that recorded vs proxy costs change results only through the cost term."""
import json
from decimal import ROUND_CEILING, Decimal
from fractions import Fraction

import pytest

from wallexbot.backtest import calibrate
from wallexbot.backtest.calibrate import (MIN_SAMPLES, CostCalibration, calibrate_costs, nearest_rank,
                                          read_depth_file, walk_book)
from wallexbot.backtest.costs import COST_MODEL_VERSION, CostBook, CostModel
from wallexbot.backtest.engine import BacktestParams, run_backtest
from wallexbot.config import RiskConfig
from wallexbot.data.candles import Candle
from wallexbot.strategy.contract import Signal
from wallexbot.strategy.exits import MomentumFailure
from wallexbot.types import SymbolRules

D = Decimal
BOOK = {"bids": [["99", "1"], ["98", "10"]], "asks": [["101", "1"], ["102", "10"]]}   # mid 100, spread 200 bps
T0 = 1_792_622_400


def up(x) -> str:
    return str(D(x).quantize(D("0.01"), rounding=ROUND_CEILING))


def write_depth(path, rows, torn_tail: str | None = None):
    text = "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows)
    if torn_tail is not None:
        text += torn_tail
    path.write_text(text)
    return path


def book_rows(symbol="AAAUSDT", n=MIN_SAMPLES + 5, book=BOOK, t0=T0, step=30):
    return [{"ts": t0 + step * i + 0.25, "symbol": symbol, **book} for i in range(n)]


# ---------------------------------------------------------------- book walking, hand-computed
def test_walk_book_hand_computed():
    asks = [(D(101), D(1)), (D(102), D(10))]
    # 101 quote buys 1 unit at 101; the remaining 149 quote buys 149/102 at 102
    assert walk_book(asks, D(250)) == D(250) / (1 + D(149) / D(102))
    assert walk_book(asks, D(101)) == D(101)
    assert walk_book(asks, D(1122)) is None       # visible value 101 + 1020 = 1121 < 1122


def test_calibration_numbers_hand_computed(tmp_path):
    f = write_depth(tmp_path / "depth_20261021.jsonl", book_rows())
    cal = calibrate_costs([f], ["AAAUSDT"], notionals=(250, 1000, 2500))
    e = cal.data["symbols"]["AAAUSDT"]
    assert e["samples"] == MIN_SAMPLES + 5 and e["usable"] is True
    assert e["spread_bps"]["median"] == e["spread_bps"]["p90"] == "200.00"
    assert e["half_spread_bps"]["p90"] == "100.00"
    vwap_buy = D(250) * 102 / 251                 # = 250 / (1 + 149/102)
    vwap_sell = D(250) * 98 / 249                 # = 250 / (1 + 151/98)
    buy, sell = e["slippage_vs_mid_bps"]["BUY"]["250"], e["slippage_vs_mid_bps"]["SELL"]["250"]
    assert buy["p90"] == buy["median"] == up((vwap_buy - 100) / 100 * 10000)          # 159.3625... -> 159.37
    assert sell["p90"] == up((100 - vwap_sell) / 100 * 10000)                          # 160.6425... -> 160.65
    assert e["impact_beyond_touch_bps"]["BUY"]["250"]["p90"] == up((vwap_buy - 101) / 100 * 10000)
    assert e["impact_beyond_touch_bps"]["SELL"]["250"]["p90"] == up((99 - vwap_sell) / 100 * 10000)
    assert (buy["p90"], sell["p90"]) == ("159.37", "160.65")
    # 1000 quote fits (1121 visible on the ask side, 99 + 980 = 1079 on the bid side); 2500 does not
    assert e["slippage_vs_mid_bps"]["BUY"]["1000"]["insufficient_depth"] is False
    short = e["slippage_vs_mid_bps"]["SELL"]["2500"]
    assert short["insufficient_depth"] is True and short["p90"] is None and short["median"] is None
    assert short["insufficient_depth_samples"] == MIN_SAMPLES + 5
    assert e["notionals_with_p90"] == ["250", "1000"]
    assert e["visible_depth_quote"]["BUY"]["median"] == "1121.00"
    assert e["confidence"] == "low" and "NOT representative" in e["confidence_note"]
    assert "LOW CONFIDENCE" in cal.data["confidence_note"]
    assert cal.data["inputs"][0]["sha256"] and cal.data["inputs"][0]["file"] == "depth_20261021.jsonl"


def test_torn_last_line_crossed_books_duplicates_and_other_symbols(tmp_path):
    rows = book_rows(n=3)
    rows.append(dict(rows[0]))                                          # duplicate ts -> ignored
    rows.append({"ts": T0 + 999, "symbol": "AAAUSDT", "bids": [["101", "1"]], "asks": [["100", "1"]]})  # crossed
    rows.append({"ts": T0 + 1000, "symbol": "AAAUSDT", "bids": [], "asks": [["100", "1"]]})            # one side
    rows.append({"ts": T0 + 1001, "symbol": "ZZZUSDT", **BOOK})        # not requested
    f = write_depth(tmp_path / "depth_x.jsonl", rows, torn_tail='{"ts":17926,"symbol":"AAAUSDT","bi')
    got, info = read_depth_file(f)
    assert info["torn_last_line"] is True and info["bad_lines"] == 0 and info["rows"] == len(rows)
    cal = calibrate_costs([f], ["AAAUSDT"])
    e = cal.data["symbols"]["AAAUSDT"]
    assert e["samples"] == 3 and e["invalid_snapshots"] == 2
    assert e["usable"] is False and "valid snapshots" in e["reason"]
    # a complete last line without its newline is also not trusted (the writer may be mid-line)
    g = tmp_path / "depth_y.jsonl"
    g.write_text(json.dumps(book_rows(n=1)[0]))
    assert read_depth_file(g)[1]["rows"] == 0


def test_nearest_rank_quantiles_rank_missing_depth_as_infinite():
    xs = [D(i) for i in range(1, 11)]
    assert nearest_rank(xs, Fraction(9, 10)) == D(9) and nearest_rank(xs, Fraction(1, 2)) == D(5)
    assert nearest_rank(xs[:9] + [None], Fraction(9, 10)) == D(9)       # 1 of 10 short: p90 still defined
    assert nearest_rank(xs[:8] + [None, None], Fraction(9, 10)) is None  # 2 of 10 short: p90 undefined
    assert nearest_rank([], Fraction(1, 2)) is None


def test_calibration_is_deterministic_and_file_order_independent(tmp_path):
    a = write_depth(tmp_path / "depth_20261021.jsonl", book_rows(n=30))
    other = {"bids": [["99.5", "3"], ["99", "50"]], "asks": [["100.5", "2"], ["101", "40"]]}
    b = write_depth(tmp_path / "depth_20261022.jsonl", book_rows(n=30, book=other, t0=T0 + 86400))
    one = calibrate_costs([a, b], ["AAAUSDT", "BBBUSDT"])
    two = calibrate_costs([b, a], ["BBBUSDT", "AAAUSDT"])
    assert one.to_json() == two.to_json() and one.sha256 == two.sha256
    assert one.data["symbols"]["AAAUSDT"]["sessions"] == 2
    assert one.data["symbols"]["BBBUSDT"] == {"samples": 0, "invalid_snapshots": 0, "usable": False,
                                              "reason": "no valid snapshots", "confidence": "none"}
    p = one.save(tmp_path / "cal.json")
    assert CostCalibration.load(p).data == one.data
    a.write_text(a.read_text() + json.dumps(book_rows(n=1, t0=T0 + 5000)[0]) + "\n")
    assert calibrate_costs([a, b], ["AAAUSDT"]).sha256 != calibrate_costs([b], ["AAAUSDT"]).sha256


# ---------------------------------------------------------------- recorded CostModel
CURVE = ((D(250), D(2)), (D(500), D(4)), (D(1000), D(10)))
REC = CostModel(taker_rate=D("0.001"), maker_rate=D("0.001"), half_spread_bps=D(5), impact_k=D(0),
                impact_cap_bps=D(100), cost_source="recorded", impact_curve=CURVE)


def test_curve_interpolation_extrapolation_floor_and_cap():
    assert REC.curve_bps(D(100)) == (D(2), "below_range")              # flat below the first point
    assert REC.curve_bps(D(250)) == (D(2), "interpolated")
    assert REC.curve_bps(D(750)) == (D(7), "interpolated")
    assert REC.curve_bps(D(2000)) == (D(22), "extrapolated")           # last slope 6 bps / 500 USDT
    assert REC.impact_fraction(D(2000), D(1)) == D("0.0022")           # bar notional is ignored
    assert REC.impact_fraction(D(100000), None) == D("0.01")           # capped at 100 bps
    assert REC.impact_status(D(100000)) == "capped" and REC.impact_status(D(2000)) == "extrapolated"
    flat = CostModel(cost_source="recorded", impact_curve=((D(250), D(3)), (D(1000), D(3))))
    assert flat.curve_bps(D(5000)) == (D(3), "extrapolated")           # slope 0 stays flat
    single = CostModel(cost_source="recorded", impact_curve=((D(500), D(5)),))
    assert single.curve_bps(D(1000)) == (D(10), "extrapolated")        # one point: line through the origin
    prev = D(0)
    for n in range(1, 40000, 97):                                     # monotone in order size
        cur = REC.impact_fraction(D(n), None)
        assert cur >= prev
        prev = cur
    assert CostModel().impact_status(D(1000)) == "proxy"


def test_recorded_model_validation():
    with pytest.raises(ValueError):
        CostModel(cost_source="recorded")                                       # no curve
    with pytest.raises(ValueError):
        CostModel(cost_source="recorded", impact_curve=((D(500), D(5)), (D(250), D(6))))  # not increasing
    with pytest.raises(ValueError):
        CostModel(cost_source="recorded", impact_curve=((D(250), D(5)), (D(500), D(4))))  # decreasing bps
    with pytest.raises(ValueError):
        CostModel(cost_source="volume")


def test_recorded_fill_and_market_context():
    f = REC.fill("BUY", D(100), D(5), None)            # 500 USDT order: 5 bps spread + 4 bps impact
    assert f.price == D("100.09") and f.spread_cost == D("0.25") and f.impact_cost == D("0.20")
    s = REC.fill("SELL", D(100), D(5), D(10 ** 9))
    assert s.price == D("99.91")
    mc = REC.market_context()                          # impact at the 1000 USDT reference, both legs
    assert (mc.spread_bps, mc.est_slippage_bps) == (D(10), D(20))
    assert mc.round_trip_cost_bps == D(20) + D(10) + D(20)
    assert CostModel().market_context().est_slippage_bps == D(50)      # proxy unchanged


def test_from_calibration_is_conservative_and_drops_thin_notionals(tmp_path):
    f = write_depth(tmp_path / "depth_20261021.jsonl", book_rows())
    cal = calibrate_costs([f], ["AAAUSDT", "BBBUSDT"], notionals=(250, 1000, 2500))
    m = CostModel.from_calibration(cal, "AAAUSDT", taker_rate=D("0.002"))
    e = cal.data["symbols"]["AAAUSDT"]
    assert m.cost_source == "recorded" and m.half_spread_bps == D("100.00") and m.taker_rate == D("0.002")
    assert [n for n, _ in m.impact_curve] == [D(250), D(1000)]         # 2500: insufficient depth -> dropped
    assert m.calibration_id == cal.sha256 and m.impact_cap_bps == D(100)
    for n, imp in m.impact_curve:                                       # hs + impact >= p90 slippage vs mid
        for side in ("BUY", "SELL"):
            assert m.half_spread_bps + imp >= D(e["slippage_vs_mid_bps"][side][str(n)]["p90"])
            assert imp >= D(e["impact_beyond_touch_bps"][side][str(n)]["p90"])
    assert m.curve_bps(D(2500))[1] == "extrapolated"
    assert m.impact_status(D(2500)) == "capped"                        # this thin book extrapolates past 100 bps
    with pytest.raises(KeyError):
        CostModel.from_calibration(cal, "BBBUSDT")
    with pytest.raises(ValueError):
        CostModel.from_calibration(cal, "AAAUSDT", quantile="p99")
    med = CostModel.from_calibration(cal.data, "AAAUSDT", "median")   # a plain dict works too
    assert med.half_spread_bps == D("100.00")


def test_cost_book():
    book = CostBook.of(CostModel(), {"AAAUSDT": REC.with_fee(D("0.0035"))})
    assert book.for_symbol("AAAUSDT").cost_source == "recorded" and book.for_symbol("ZZZ") is book.default
    assert book.taker_rate == D("0.0035")
    cheap = book.with_fee(D("0.001"))
    assert cheap.for_symbol("AAAUSDT").taker_rate == cheap.default.taker_rate == D("0.001")
    with pytest.raises(ValueError):
        CostBook.of(CostModel(), {"AAAUSDT": REC})                        # fee scenario mismatch
    assert REC.for_symbol("anything") is REC
    assert COST_MODEL_VERSION == "cm-0.2"


# ---------------------------------------------------------------- engine: only the cost term differs
A = "AAAUSDT"
RULES = {A: SymbolRules(A, "AAA", "USDT", 4, 2), "BBBUSDT": SymbolRules("BBBUSDT", "BBB", "USDT", 4, 2)}
RISK = RiskConfig(risk_per_trade_pct=D(1), max_position_pct=D(25), max_exposure_pct=D(60), max_open_positions=3,
                  max_order_value=D(1000), daily_loss_soft_pct=D(50), daily_loss_hard_pct=D(60),
                  drawdown_soft_pct=D(80), drawdown_hard_pct=D(90), consecutive_loss_cooldown=3, min_notional=D(5))


def wave_bars(n=400, start=0, base=100):
    out = []
    px = D(base)
    for i in range(n):
        step = D("0.6") if (i // 25) % 2 == 0 else D("-0.5")
        o, c = px, px + step
        out.append(Candle(start + 900 * i, o, max(o, c) + D("0.2"), min(o, c) - D("0.2"), c, D(50 + i % 7), 0))
        px = c
    return out


class EveryTenth:
    """Market-independent signal: every 10th bar, stop 2% below the close."""
    def __init__(self):
        self.seen = {}

    def evaluate(self, symbol, bars, market):
        self.seen.setdefault(symbol, set()).add(market)
        last = bars[-1]
        if (last.ts // 900) % 10:
            return None
        return Signal(symbol, last.ts, last.close, last.close, D(1), (last.close * D("0.98")).quantize(D("0.01")),
                      D(1))


def run(cost, bars=None, se=None):
    return run_backtest(bars or {A: wave_bars()}, se or EveryTenth(), MomentumFailure(), cost, RISK, RULES,
                        D(10000), BacktestParams(bar_interval_s=900, lookback_bars=400))


ZERO_PROXY = CostModel(taker_rate=D(0), maker_rate=D(0), half_spread_bps=D(0), impact_k=D(0), impact_cap_bps=D(0))
ZERO_REC = CostModel(taker_rate=D(0), maker_rate=D(0), half_spread_bps=D(0), impact_k=D(0), impact_cap_bps=D(0),
                     cost_source="recorded", impact_curve=((D(1000), D(0)),))


def test_zero_cost_recorded_and_proxy_runs_are_identical_except_the_inputs_hash():
    p, r = run(ZERO_PROXY), run(ZERO_REC)
    assert p.trades and p.trades == r.trades and p.fills == r.fills and p.equity_curve == r.equity_curve
    assert p.events == r.events and p.final_equity == r.final_equity
    assert p.inputs_hash != r.inputs_hash      # the cost model is part of the inputs


def test_recorded_vs_proxy_differ_only_through_costs_and_identity_is_exact():
    proxy, rec = run(CostModel()), run(REC.with_fee(D("0.0035")))
    assert proxy.trades and len(proxy.trades) == len(rec.trades)
    for a, b in zip(proxy.trades, rec.trades):
        # same decisions: entry/exit times, reasons and reference prices (the policy and signals ignore costs)
        assert (a.entry_ts, a.exit_ts, a.exit_reason, a.initial_stop) == (b.entry_ts, b.exit_ts, b.exit_reason,
                                                                         b.initial_stop)
        assert [x.ref_price for x in a.exits] == [x.ref_price for x in b.exits]
        # per-unit frictionless P&L is identical; only the cost term differs
        assert a.gross_pnl / a.qty == b.gross_pnl / b.qty
        for t in (a, b):
            assert t.net_pnl == t.gross_pnl - t.fees - t.spread_cost - t.impact_cost   # exact, to the digit
    assert sum(t.impact_cost for t in proxy.trades) != sum(t.impact_cost for t in rec.trades)


def test_engine_uses_each_symbols_cost_model_and_market_context():
    bars = {A: wave_bars(), "BBBUSDT": wave_bars(base=50)}
    book = CostBook.of(CostModel(), {A: REC.with_fee(D("0.0035"))})
    se = EveryTenth()
    res = run(book, bars, se)
    assert se.seen[A] == {book.for_symbol(A).market_context()}
    assert se.seen["BBBUSDT"] == {CostModel().market_context()}
    rec_fills = [f for f in res.fills if f.symbol == A]
    assert rec_fills and {f.symbol for f in res.fills} == {A, "BBBUSDT"}
    for f in rec_fills:   # the recorded symbol's fills are priced by its own model (bar volume irrelevant)
        assert f.price == book.for_symbol(A).fill(f.side, f.ref_price, f.qty, None, 2).price


def test_calibrate_module_has_no_wall_clock():
    src = open(calibrate.__file__).read()
    assert "time.time" not in src and "datetime.now" not in src
