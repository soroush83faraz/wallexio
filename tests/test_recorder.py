import json
from decimal import Decimal

from wallexbot.data.recorder import Recorder
from wallexbot.exchange.wallex_public import Depth, Trade

DAY1 = 1_792_540_800 + 3600  # 2026-10-21 01:00 UTC
DAY2 = DAY1 + 86400


def depth(sym, ts):
    asks = tuple((Decimal(101 + i), Decimal("1.5")) for i in range(20))
    bids = tuple((Decimal(100 - i), Decimal("2")) for i in range(20))
    return Depth(sym, asks, bids, ts)


def trade(sym, ts, price="100.5", qty="0.1", buy=True):
    return Trade(sym, Decimal(price), Decimal(qty), Decimal(price) * Decimal(qty), buy, ts)


class FakeClient:
    def __init__(self):
        self.trade_pages = {}  # symbol -> list of pages (consumed in order, last repeats)
        self.fail_depth = set()
        self.fail_trades = set()
        self.depth_calls = []

    def depth(self, symbol):
        self.depth_calls.append(symbol)
        if symbol in self.fail_depth:
            raise RuntimeError("boom")
        return depth(symbol, 1.0)

    def trades(self, symbol):
        if symbol in self.fail_trades:
            raise TimeoutError("slow")
        pages = self.trade_pages.get(symbol, [[]])
        return pages.pop(0) if len(pages) > 1 else pages[0]


def lines(p):
    return [json.loads(x) for x in p.read_text().splitlines()]


def test_run_once_writes_depth_and_trades(tmp_path):
    cl = FakeClient()
    cl.trade_pages["BTCUSDT"] = [[trade("BTCUSDT", DAY1 - 5), trade("BTCUSDT", DAY1 - 3, buy=False)]]
    rec = Recorder(cl, ["BTCUSDT", "ETHUSDT"], tmp_path, depth_levels=5, clock=lambda: DAY1)
    stats = rec.run_once()
    d = lines(tmp_path / "depth_20261021.jsonl")
    assert [r["symbol"] for r in d] == ["BTCUSDT", "ETHUSDT"]
    assert len(d[0]["bids"]) == len(d[0]["asks"]) == 5 and d[0]["asks"][0] == ["101", "1.5"]  # strings, not floats
    t = lines(tmp_path / "trades_20261021.jsonl")
    assert [(r["price"], r["is_buy"]) for r in t] == [("100.5", True), ("100.5", False)]
    assert stats["depth_ok"] == 2 and stats["trades_new"] == 2 and stats["depth_err"] == 0


def test_trades_deduped_across_polls_and_restart(tmp_path):
    cl = FakeClient()
    a, b, c3 = trade("BTCUSDT", DAY1 - 9), trade("BTCUSDT", DAY1 - 8), trade("BTCUSDT", DAY1 - 7)
    cl.trade_pages["BTCUSDT"] = [[a, b], [a, b, c3], [a, b, c3]]
    rec = Recorder(cl, ["BTCUSDT"], tmp_path, clock=lambda: DAY1)
    rec.run_once(); rec.run_once(); rec.run_once()
    assert len(lines(tmp_path / "trades_20261021.jsonl")) == 3
    # a fresh process re-reading the same window must not duplicate
    rec2 = Recorder(cl, ["BTCUSDT"], tmp_path, clock=lambda: DAY1)
    rec2.run_once()
    assert len(lines(tmp_path / "trades_20261021.jsonl")) == 3
    assert rec2.stats["trades_new"] == 0


def test_daily_rotation(tmp_path):
    now = [DAY1]
    cl = FakeClient()
    cl.trade_pages["BTCUSDT"] = [[trade("BTCUSDT", DAY1 - 1)], [trade("BTCUSDT", DAY1 - 1), trade("BTCUSDT", DAY2 - 1)]]
    rec = Recorder(cl, ["BTCUSDT"], tmp_path, clock=lambda: now[0])
    rec.run_once()
    now[0] = DAY2
    rec.run_once()
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["depth_20261021.jsonl", "depth_20261022.jsonl", "trades_20261021.jsonl", "trades_20261022.jsonl"]
    assert len(lines(tmp_path / "trades_20261022.jsonl")) == 1  # only the new trade


def test_single_failure_never_stops_the_cycle(tmp_path):
    cl = FakeClient()
    cl.fail_depth = {"BTCUSDT"}
    cl.fail_trades = {"ETHUSDT"}
    cl.trade_pages["BTCUSDT"] = [[trade("BTCUSDT", DAY1 - 1)]]
    rec = Recorder(cl, ["BTCUSDT", "ETHUSDT"], tmp_path, clock=lambda: DAY1)
    stats = rec.run_once()
    assert stats["depth_err"] == 1 and stats["trades_err"] == 1
    assert stats["depth_ok"] == 1 and stats["trades_ok"] == 1
    assert len(lines(tmp_path / "trades_20261021.jsonl")) == 1  # BTC trades still recorded
    assert [r["symbol"] for r in lines(tmp_path / "depth_20261021.jsonl")] == ["ETHUSDT"]


def test_possible_gap_counted_when_full_page_has_no_overlap(tmp_path):
    cl = FakeClient()
    p1 = [trade("BTCUSDT", DAY1 - 1000 + i) for i in range(50)]
    p2 = [trade("BTCUSDT", DAY1 - 500 + i) for i in range(50)]
    cl.trade_pages["BTCUSDT"] = [p1, p2]
    rec = Recorder(cl, ["BTCUSDT"], tmp_path, clock=lambda: DAY1)
    rec.run_once(); rec.run_once()
    assert rec.stats["possible_trade_gaps"] == 1 and rec.stats["trades_new"] == 100


def test_run_forever_stops_gracefully(tmp_path):
    cl = FakeClient()
    rec = Recorder(cl, ["BTCUSDT"], tmp_path, interval_s=0.01, clock=lambda: DAY1)
    orig = cl.depth

    def depth_then_stop(sym):
        r = orig(sym)
        if len(cl.depth_calls) >= 3:
            rec.stop()
        return r

    cl.depth = depth_then_stop
    rec.run_forever()
    assert len(cl.depth_calls) == 3


def test_run_forever_survives_total_failure(tmp_path):
    cl = FakeClient()
    cl.fail_depth = cl.fail_trades = {"BTCUSDT"}
    rec = Recorder(cl, ["BTCUSDT"], tmp_path, interval_s=0.001, clock=lambda: DAY1)
    rec.run_forever(max_cycles=3)
    assert rec.stats["depth_err"] == 3 and rec.stats["trades_err"] == 3
