"""Backtest engine tests. Every expected number is computed by hand in the comments."""
import random
from dataclasses import replace
from decimal import Decimal

import pytest

from wallexbot.backtest.costs import CostModel
from wallexbot.backtest.engine import BacktestParams, BarsView, loss_streak_cooldown, run_backtest
from wallexbot.config import RiskConfig
from wallexbot.data.candles import Candle
from wallexbot.strategy.contract import ExitDecision, Signal
from wallexbot.strategy.exits import Hybrid
from wallexbot.types import SymbolRules

D = Decimal
A, B = "AAAUSDT", "BBBUSDT"
RULES = {s: SymbolRules(s, s[:3], "USDT", amount_precision=4, price_precision=4) for s in (A, B)}
ZERO = CostModel(taker_rate=D(0), maker_rate=D(0), half_spread_bps=D(0), impact_k=D(0), impact_cap_bps=D(0))
HOLD = ExitDecision("hold")


def risk(**kw):
    base = dict(risk_per_trade_pct=D("1"), max_position_pct=D("100"), max_exposure_pct=D("100"),
                max_open_positions=3, max_order_value=D("1000000"), daily_loss_soft_pct=D("50"),
                daily_loss_hard_pct=D("60"), drawdown_soft_pct=D("50"), drawdown_hard_pct=D("60"),
                consecutive_loss_cooldown=3, min_notional=D("1"))
    base.update(kw)
    return RiskConfig(**base)


def bar(i, o, h, l, cl, v="1000"):
    return Candle(i * 900, D(str(o)), D(str(h)), D(str(l)), D(str(cl)), D(str(v)))


def flat(i, p=100, v="1000"):
    return bar(i, p, p, p, p, v)


class Scripted:
    """Fake SignalEngine: emits a signal when the last CLOSED bar is in the plan."""

    def __init__(self, plan):
        self.plan = plan            # {(symbol, bar_index): (stop, score[, breakout])}
        self.calls = []

    def evaluate(self, symbol, bars, market):
        last = bars[-1]
        self.calls.append((symbol, last.ts, len(bars)))
        spec = self.plan.get((symbol, last.ts // 900))
        if spec is None:
            return None
        stop, score, *rest = spec
        brk = D(str(rest[0])) if rest else last.close
        return Signal(symbol, last.ts, last.close, brk, D(1), D(str(stop)), D(str(score)))


class Policy:
    """Fake ExitPolicy: fixed targets and a per-bar decision function."""

    def __init__(self, targets=None, decide=None):
        self._targets, self._decide, self.seen = targets, decide, []

    def targets(self, state):
        return self._targets(state) if self._targets else ()

    def on_bar(self, state, bars):
        self.seen.append((state, bars[-1].ts))
        return self._decide(state, bars) if self._decide else HOLD


def run(bars, signals, policy=None, cost=ZERO, rk=None, equity="10000", params=None, **kw):
    bbs = bars if isinstance(bars, dict) else {A: bars}
    return run_backtest(bbs, signals, policy or Policy(), cost, rk or risk(), RULES, D(equity),
                        params or BacktestParams(), **kw)


BASE = [bar(0, 100, 101, 99, 100), bar(1, 100, 101, 99, 100), bar(2, 102, 103, 101, 102)]


# ------------------------------------------------------------------ entry timing / no look-ahead
def test_entry_fills_at_next_bar_open_never_on_signal_bar():
    sig = Scripted({(A, 1): (92, 1)})
    res = run(BASE + [bar(3, 102, 103, 101, 102)], sig)
    entry = res.fills[0]
    # signal on bar 1 (closed); entry at bar 2 OPEN = 102; risk 100 / (102 - 92) = 10 units
    assert (entry.ts, entry.ref_price, entry.price, entry.qty, entry.kind) == (1800, D(102), D(102), D(10), "entry")
    t = res.trades[0]
    assert t.signal_ts == 900 and t.entry_ts == 1800 and t.end_of_data
    assert (t.exit_reason, t.exits[0].price, t.net_pnl) == ("end_of_data", D(102), D(0))  # sold at last close


def test_strategy_only_sees_closed_bars():
    sig = Scripted({})
    bars = [flat(i) for i in range(6)]
    run(bars, sig)
    assert sig.calls == [(A, i * 900, i + 1) for i in range(6)]


def test_bars_view_cannot_reach_the_future():
    data = [flat(i) for i in range(5)]
    v = BarsView(data, 3)
    assert len(v) == 3 and v[-1] is data[2] and list(v) == data[:3] and v[1:] == data[1:3]
    with pytest.raises(IndexError):
        v[3]
    with pytest.raises(IndexError):
        v[-4]

    class Peeker(Scripted):
        def evaluate(self, symbol, bars, market):
            with pytest.raises(IndexError):
                bars[len(bars)]
            return super().evaluate(symbol, bars, market)

    run(data, Peeker({}))


def _random_walk(n, seed, sym_vol="5000"):
    rng = random.Random(seed)
    out, p = [], D(100)
    for i in range(n):
        o = p
        cl = max(D("1"), (o * D(str(1 + rng.gauss(0.0005, 0.01)))).quantize(D("0.0001")))
        h = max(o, cl) * D(str(1 + abs(rng.gauss(0, 0.004))))
        lo = min(o, cl) * D(str(1 - abs(rng.gauss(0, 0.004))))
        out.append(Candle(i * 900, o, h.quantize(D("0.0001")), lo.quantize(D("0.0001")), cl,
                          D(str(rng.randint(1000, 9000)))))
        p = cl
    return out


class Breakout:
    """Tiny deterministic breakout engine used only for integration tests."""

    def evaluate(self, symbol, bars, market):
        if len(bars) < 21:
            return None
        last = bars[-1]
        hh = max(b.high for b in bars[len(bars) - 21:len(bars) - 1])
        if last.close <= hh:
            return None
        lo = min(b.low for b in bars[len(bars) - 10:])
        return Signal(symbol, last.ts, last.close, hh, D(1), min(lo, last.close * D("0.98")), D("0.8"))


def test_prefix_invariance_no_lookahead():
    """Decisions up to bar k must not change when bars after k are removed or altered."""
    data = {A: _random_walk(600, 1), B: _random_walk(600, 2)}
    full = run(data, Breakout(), Hybrid(), cost=CostModel())
    cut = 400
    trunc = run({s: bs[:cut] for s, bs in data.items()}, Breakout(), Hybrid(), cost=CostModel())
    altered = {s: bs[:cut] + [replace(b, open=b.open * 3, high=b.high * 3, low=b.low * 3, close=b.close * 3)
                              for b in bs[cut:]] for s, bs in data.items()}
    alt = run(altered, Breakout(), Hybrid(), cost=CostModel())
    before = lambda r: [f for f in r.fills if f.ts < (cut - 1) * 900 and f.kind != "end_of_data"]
    assert len(before(full)) > 4
    assert before(full) == before(trunc) == before(alt)
    assert full.equity_curve[:cut - 1] == trunc.equity_curve[:cut - 1] == alt.equity_curve[:cut - 1]
    for t in full.trades:
        assert t.entry_ts == t.signal_ts + 900


# ------------------------------------------------------------------ stops / targets
def test_gap_through_stop_fills_at_open():
    res = run(BASE + [bar(3, 90, 91, 89, 90)], Scripted({(A, 1): (92, 1)}))
    t = res.trades[0]
    # stop 92, bar opens at 90 -> filled at 90 (worse than the stop): (90 - 102) * 10 = -120
    assert (t.exit_reason, t.exits[0].price, t.exits[0].ts, t.net_pnl) == ("stop_gap", D(90), 2700, D(-120))
    assert t.r_multiple == D("-1.2")
    assert res.final_equity == D(9880)


def test_same_bar_stop_and_target_books_the_stop():
    tp = Policy(targets=lambda s: ((D(112), D(1), "tp"),))
    res = run(BASE + [bar(3, 102, 113, 91, 100)], Scripted({(A, 1): (92, 1)}), tp)
    t = res.trades[0]
    assert (t.exit_reason, t.exits[0].price, t.net_pnl) == ("stop_ambiguous_stop_first", D(92), D(-100))
    # same bar without touching the stop -> target at 112: +100
    res = run(BASE + [bar(3, 102, 113, 95, 100)], Scripted({(A, 1): (92, 1)}), tp)
    assert (res.trades[0].exit_reason, res.trades[0].net_pnl) == ("tp", D(100))


def test_gap_above_target_fills_at_target_not_open():
    tp = Policy(targets=lambda s: ((D(112), D(1), "tp"),))
    res = run(BASE + [bar(3, 120, 121, 119, 120)], Scripted({(A, 1): (92, 1)}), tp)
    assert res.trades[0].exits[0].price == D(112)


def test_partial_target_then_breakeven_stop():
    tp = Policy(targets=lambda s: () if s.partial_taken else ((D(112), D("0.5"), "half"),),
                decide=lambda s, b: ExitDecision("update_stop", new_stop=s.entry_price) if s.partial_taken else HOLD)
    bars = BASE + [bar(3, 103, 113, 101, 110),   # half (5 units) at 112: +50; low 101 > stop 92
                   bar(4, 110, 111, 101, 105)]   # breakeven stop 102 (set at bar 3 close) hit -> 5 at 102
    res = run(bars, Scripted({(A, 1): (92, 1)}), tp)
    t = res.trades[0]
    assert [(x.ts, x.reason, x.qty, x.price) for x in t.exits] == [(2700, "half", D(5), D(112)),
                                                                   (3600, "stop", D(5), D(102))]
    assert t.net_pnl == D(50) and t.qty == D(10)
    # the policy saw partial_taken only after the partial fill
    assert [s.partial_taken for s, _ in tp.seen] == [False, True]


def test_fees_spread_impact_exact_round_trip():
    cm = CostModel(taker_rate=D("0.001"), maker_rate=D("0.001"), half_spread_bps=D("10"), impact_k=D("1"),
                   impact_cap_bps=D("50"))
    rules = {A: SymbolRules(A, "AAA", "USDT", amount_precision=0, price_precision=4)}
    exit_next = Policy(decide=lambda s, b: ExitDecision("exit_all", reason="go"))
    bars = [bar(0, 100, 100, 100, 100, 9000), bar(1, 100, 100, 100, 100, 9000),
            bar(2, 100, 111, 99, 110, 9000), bar(3, 110, 110, 110, 110)]
    res = run_backtest({A: bars}, Scripted({(A, 1): (90, 1)}), exit_next, cm,
                       risk(risk_per_trade_pct=D(50), max_order_value=D(1000)), rules, D(10000))
    buy, sell = res.fills
    # size: 1000 / 100.1 -> 9 units. Liquidity proxy = bar1 9000 * 100 = 900000; 900/900000 = 0.1% -> impact 10 bps
    assert (buy.qty, buy.price, buy.notional, buy.fee) == (D(9), D("100.2"), D("901.8"), D("0.9018"))
    assert (buy.spread_cost, buy.impact_cost) == (D("0.9"), D("0.9"))
    # exit at bar3 open 110; proxy = bar2 9000 * 110 = 990000; 990/990000 = 0.1% -> 110 * 0.998 = 109.78
    assert (sell.ts, sell.qty, sell.price, sell.notional, sell.fee) == (2700, D(9), D("109.78"), D("988.02"),
                                                                       D("0.98802"))
    t = res.trades[0]
    assert t.gross_pnl == D(90)                                      # 9 * (110 - 100)
    assert (t.fees, t.spread_cost, t.impact_cost) == (D("1.88982"), D("1.89"), D("1.89"))
    assert t.net_pnl == D("84.33018") == t.gross_pnl - t.fees - t.spread_cost - t.impact_cost
    assert res.final_equity == D("10084.33018")
    assert t.r_multiple == D("0.918629411765")                      # 84.33018 / (9 * (100.2 - 90))


def test_time_stop_exits_at_next_open():
    res = run(BASE + [bar(3, 102, 103, 101, 102), bar(4, 104, 105, 103, 104), bar(5, 104, 104, 104, 104)],
              Scripted({(A, 1): (92, 1)}), params=BacktestParams(time_stop_bars=2))
    t = res.trades[0]
    # held through closes of bars 2 and 3 -> exit at bar 4 open (104): +20
    assert (t.exit_reason, t.exit_ts, t.exits[0].price, t.net_pnl, t.end_of_data) == (
        "engine_time_stop", 3600, D(104), D(20), False)


def test_warmup_and_lookback_window():
    bars = [flat(i) for i in range(8)]
    sig = Scripted({(A, 1): (92, 1), (A, 4): (92, 1)})
    res = run(bars, sig, params=BacktestParams(trade_start_ts=3 * 900, lookback_bars=2))
    assert [f.ts for f in res.fills if f.kind == "entry"] == [5 * 900]     # bar-1 signal is warm-up only
    assert {n for _, _, n in sig.calls} == {1, 2}                         # at most 2 bars visible


def test_data_gap_drops_pending_entry():
    bars = [flat(0), flat(1), flat(3), flat(4)]       # bar 2 missing
    res = run(bars, Scripted({(A, 1): (92, 1)}))
    assert res.fills == () and any(e.kind == "entry_dropped" and e.detail == "data_gap" for e in res.events)


# ------------------------------------------------------------------ risk breaches
def _day_bars(n, p=100):
    return [flat(i, p) for i in range(n)]


def test_hard_daily_loss_flattens_next_open_and_halts_for_the_day():
    rk = risk(risk_per_trade_pct=D(50), daily_loss_soft_pct=D(4), daily_loss_hard_pct=D(6))
    bars = _day_bars(100)
    bars[2] = bar(2, 100, 100, 89, 90)          # entry 100 units at 100 (5000 / (100 - 50)); close 90: -10%
    bars[3] = bar(3, 91, 91, 91, 91)
    for i in range(4, 100):
        bars[i] = flat(i, 91)
    sig = Scripted({(A, 1): (50, 1), (A, 5): (80, 1), (A, 97): (81, 1)})
    res = run(bars, sig, rk=rk)
    first = res.trades[0]
    assert (first.exit_reason, first.exit_ts, first.exits[0].price, first.net_pnl) == (
        "hard_breach_flatten", 2700, D(91), D(-900))
    assert any(e.kind == "signal_blocked" and e.ts == 4500 for e in res.events)     # same day: blocked
    # next UTC day (bar 96 = 86400): start-of-day equity 9100, so day loss resets -> bar 97 signal enters at bar 98
    assert len(res.trades) == 2 and res.trades[1].entry_ts == 98 * 900
    assert res.equity_curve[2].equity == D(9000)


def test_soft_breach_blocks_entries_without_flattening():
    rk = risk(risk_per_trade_pct=D(50), daily_loss_soft_pct=D(4), daily_loss_hard_pct=D(6))
    a = _day_bars(8)
    a[2] = bar(2, 100, 100, 94, 95)              # -5% day loss at the close: soft only
    for i in range(3, 8):
        a[i] = flat(i, 95)
    b = _day_bars(8)
    res = run({A: a, B: b}, Scripted({(A, 1): (50, 1), (B, 3): (90, 1)}), rk=rk)
    assert len(res.trades) == 1 and res.trades[0].end_of_data       # A held to the end, not flattened
    assert any(e.kind == "signal_blocked" and e.symbol == B for e in res.events)


# ------------------------------------------------------------------ caps
def test_max_open_positions_prefers_higher_score():
    bars = {A: _day_bars(4), B: _day_bars(4)}
    res = run(bars, Scripted({(A, 1): (90, "0.8"), (B, 1): (90, "0.9")}), rk=risk(max_open_positions=1))
    assert [f.symbol for f in res.fills if f.kind == "entry"] == [B]
    assert any(e.symbol == A and e.detail == "max_open_positions" for e in res.events)


def test_exposure_and_position_caps():
    rk = risk(risk_per_trade_pct=D(50), max_position_pct=D(20), max_exposure_pct=D(30))
    bars = {A: _day_bars(4), B: _day_bars(4)}
    res = run(bars, Scripted({(A, 1): (90, "0.9"), (B, 1): (90, "0.8")}), rk=rk)
    q = {f.symbol: f.qty for f in res.fills if f.kind == "entry"}
    # A: 20% of 10000 = 2000 -> 20 units. B: exposure headroom 3000 - 2000 = 1000 -> 10 units
    assert q == {A: D(20), B: D(10)}


def test_cash_cap_with_fee_reserve_and_cash_never_negative():
    cm = CostModel(taker_rate=D("0.01"), maker_rate=D("0.01"), half_spread_bps=D(0), impact_k=D(0),
                   impact_cap_bps=D(0))
    res = run(_day_bars(4), Scripted({(A, 1): (90, 1)}), cost=cm, rk=risk(risk_per_trade_pct=D(100)))
    e = res.fills[0]
    # 10000 / 1.01 / 100 = 99.00990... -> 99.0099; cost 9900.99 + 99.0099 fee = 9999.9999
    assert e.qty == D("99.0099") and e.notional + e.fee == D("9999.9999")
    assert all(p.cash >= 0 for p in res.equity_curve)


def test_cooldown_hook_blocks_entries():
    bars = BASE + [bar(3, 90, 91, 89, 90)] + [flat(i, 90) for i in range(4, 8)]
    sig = Scripted({(A, 1): (92, 1), (A, 4): (80, 1)})
    res = run(bars, sig, cooldown=loss_streak_cooldown(1, 3600))
    assert len(res.trades) == 1 and any(e.detail == "cooldown" for e in res.events)
    assert len(run(bars, Scripted({(A, 1): (92, 1), (A, 4): (80, 1)})).trades) == 2


# ------------------------------------------------------------------ determinism
def test_determinism_and_hash_stability():
    data = {A: _random_walk(300, 7), B: _random_walk(300, 8)}
    r1 = run(data, Breakout(), Hybrid(), cost=CostModel())
    r2 = run(data, Breakout(), Hybrid(), cost=CostModel())
    assert r1 == r2 and r1.result_hash == r2.result_hash and r1.inputs_hash == r2.inputs_hash
    assert r1.compute_result_hash() == r1.result_hash
    data[A] = data[A][:-1] + [replace(data[A][-1], volume=data[A][-1].volume + 1)]
    r3 = run(data, Breakout(), Hybrid(), cost=CostModel())
    assert r3.inputs_hash != r1.inputs_hash and r3.result_hash != r1.result_hash
    assert run(data, Breakout(), Hybrid(), cost=CostModel(), params=BacktestParams(time_stop_bars=5)).inputs_hash \
        != r3.inputs_hash


def test_integration_invariants_with_e4_and_costs():
    data = {A: _random_walk(800, 11), B: _random_walk(800, 12)}
    rk = risk(max_position_pct=D(25), max_exposure_pct=D(40), max_open_positions=1, max_order_value=D(1000))
    res = run(data, Breakout(), Hybrid(), cost=CostModel(), rk=rk)
    assert res.trades
    held, peak = set(), 0
    for f in sorted(res.fills, key=lambda f: (f.ts, f.side != "SELL")):
        if f.kind == "entry":
            held.add(f.symbol)
            assert f.notional <= D(1000)
        peak = max(peak, len(held))
        if f.side == "SELL" and any(t.exits[-1] is f for t in res.trades):
            held.discard(f.symbol)
    assert peak <= 1
    for t in res.trades:
        assert t.net_pnl == t.gross_pnl - t.fees - t.spread_cost - t.impact_cost
        assert sum(x.qty for x in t.exits) == t.qty
        assert t.entry_ts > t.signal_ts
    assert all(p.cash >= 0 for p in res.equity_curve)
    assert res.final_equity == res.initial_equity + sum(t.net_pnl for t in res.trades)
