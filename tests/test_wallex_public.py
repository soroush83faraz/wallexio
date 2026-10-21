import json
import math
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest

from wallexbot.exchange.wallex_public import (
    WallexDataError, WallexHTTPError, WallexHostError, WallexPublicClient,
)


class FakeTransport:
    """Routes by URL path; `routes` values are bytes, or a list of responses consumed in order."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []  # (method, url)

    def __call__(self, method, url, timeout):
        assert timeout and timeout > 0
        self.calls.append((method, url))
        r = self.routes[urlsplit(url).path]
        if isinstance(r, list):
            r = r.pop(0) if len(r) > 1 else r[0]
        if isinstance(r, Exception):
            raise r
        status, body = r if isinstance(r, tuple) else (200, r)
        return status, body


def client(routes, clock=1792604388, **kw):
    t = FakeTransport(routes)
    c = WallexPublicClient(transport=t, clock=lambda: clock, sleep=lambda s: None, **kw)
    return c, t


def jb(obj) -> bytes:
    return json.dumps(obj).encode()


# ---- markets ---------------------------------------------------------------
def test_markets_filters_spot_and_parses_precisions(fx_bytes):
    c, _ = client({"/hector/web/v1/markets": fx_bytes("markets.json")})
    rules = {r.symbol: r for r in c.markets()}
    assert "CHRTMN" not in rules and "COMPTMN" not in rules  # is_spot false
    btc = rules["BTCUSDT"]
    assert (btc.base, btc.quote, btc.amount_precision, btc.price_precision) == ("BTC", "USDT", 8, 2)
    assert btc.min_notional is None


def test_rank_universe_by_quote_volume(fx_bytes):
    c, _ = client({"/hector/web/v1/markets": fx_bytes("markets.json")})
    top = c.rank_universe("USDT", 3)
    assert [r.symbol for r in top] == ["BTCUSDT", "ETHUSDT", "XRPUSDT"]
    all_usdt = [r.symbol for r in c.rank_universe("USDT", 99)]
    assert "CGPTUSDT" not in all_usdt  # no 24h volume -> not rankable
    assert c.rank_universe("TMN", 1)[0].symbol == "PEPETMN"


def test_markets_missing_precision_fails_closed(fx):
    d = fx("markets.json")
    del next(m for m in d["result"]["markets"] if m["symbol"] == "BTCUSDT")["amount_precision"]
    c, _ = client({"/hector/web/v1/markets": jb(d)})
    with pytest.raises(WallexDataError):
        c.markets()


def test_markets_unsuccessful_or_missing_result_fails_closed(fx):
    for bad in ({"success": False, "result": {}}, {"success": True}, [], {"success": True, "result": {"markets": []}}):
        c, _ = client({"/hector/web/v1/markets": jb(bad)})
        with pytest.raises(WallexDataError):
            c.markets()


def test_rank_universe_bad_volume_fails_closed(fx):
    d = fx("markets.json")
    next(m for m in d["result"]["markets"] if m["symbol"] == "BTCUSDT")["quote_volume_24h"] = "lots"
    c, _ = client({"/hector/web/v1/markets": jb(d)})
    with pytest.raises(WallexDataError):
        c.rank_universe("USDT", 3)


# ---- candles ---------------------------------------------------------------
def test_candles_parse_decimal_and_drop_forming(fx_bytes, fx, fetched_at):
    raw = fx("udf_1m_13h.json")
    c, t = client({"/v1/udf/history": fx_bytes("udf_1m_13h.json")}, clock=fetched_at)
    out = c.candles("BTCUSDT", "1", raw["t"][0], raw["t"][-1])
    assert len(out) == len(raw["t"]) - 1  # newest (still forming) bar dropped
    assert out[-1].ts == raw["t"][-2]
    assert all(out[i].ts < out[i + 1].ts for i in range(len(out) - 1))
    first = out[0]
    assert first.open == Decimal(raw["o"][0]) and first.volume == Decimal(raw["v"][0])
    assert all(isinstance(x, Decimal) for x in (first.open, first.high, first.low, first.close, first.volume))
    q = parse_qs(urlsplit(t.calls[0][1]).query)
    assert q["symbol"] == ["BTCUSDT"] and q["resolution"] == ["1"]


def test_candle_closes_exactly_at_boundary(fx_bytes, fx):
    raw = fx("udf_1m_13h.json")
    last = raw["t"][-1]
    c, _ = client({"/v1/udf/history": fx_bytes("udf_1m_13h.json")}, clock=last + 60)  # bar just closed
    assert c.candles("BTCUSDT", "1", raw["t"][0], last)[-1].ts == last
    c, _ = client({"/v1/udf/history": fx_bytes("udf_1m_13h.json")}, clock=last + 59)
    assert c.candles("BTCUSDT", "1", raw["t"][0], last)[-1].ts == raw["t"][-2]


def test_candles_native_hourly(fx_bytes, fx, fetched_at):
    raw = fx("udf_60_13h.json")
    c, _ = client({"/v1/udf/history": fx_bytes("udf_60_13h.json")}, clock=fetched_at)
    out = c.candles("BTCUSDT", "60", raw["t"][0], raw["t"][-1])
    assert len(out) == 12 and out[-1].ts == raw["t"][-2]


def test_resolution_not_honoured_by_server_fails_closed(fx_bytes, fetched_at):
    # Real API quirk: resolution=15 returns 1m bars. Must never be passed off as 15m.
    c, _ = client({"/v1/udf/history": fx_bytes("udf_1m_13h.json")}, clock=fetched_at)
    with pytest.raises(WallexDataError):
        c.candles("BTCUSDT", "15", 1792557600, 1792604340)


def test_unsupported_resolution_and_bad_symbol(fx_bytes):
    c, t = client({"/v1/udf/history": fx_bytes("udf_1m_13h.json")})
    with pytest.raises(ValueError):
        c.candles("BTCUSDT", "5", 0, 100)  # there is no 5m
    with pytest.raises(ValueError):
        c.candles("BTC/USDT&x=1", "1", 0, 100)
    assert t.calls == []


def test_long_range_is_chunked_and_merged():
    two_days = 2 * 86400
    start = 1_700_000_040
    seen = []

    def bars(lo, hi):
        ts = [x for x in range(lo + (-lo) % 60, hi + 1, 60)]
        return {"s": "ok", "t": ts, "o": ["1"] * len(ts), "h": ["2"] * len(ts), "l": ["1"] * len(ts),
                "c": ["2"] * len(ts), "v": ["3"] * len(ts)}

    class T(FakeTransport):
        def __call__(self, method, url, timeout):
            q = {k: int(v[0]) for k, v in parse_qs(urlsplit(url).query).items() if k in ("from", "to")}
            seen.append((q["from"], q["to"]))
            return 200, jb(bars(q["from"], q["to"]))

    t = T({})
    c = WallexPublicClient(transport=t, clock=lambda: start + 10 * 86400, sleep=lambda s: None)
    out = c.candles("BTCUSDT", "1", start, start + two_days)
    assert len(seen) == math.ceil((two_days + 1) / (600 * 60))
    assert all(hi - lo + 1 <= 600 * 60 for lo, hi in seen)
    assert len(out) == len({x.ts for x in out}) == len(range(start + 0, start + two_days + 1, 60))


def test_candle_mutations_fail_closed(fx):
    base = fx("udf_1m_13h.json")
    cases = {}
    m = json.loads(json.dumps(base)); m["c"][3] = "abc"; cases["non-numeric"] = m
    m = json.loads(json.dumps(base)); m["h"] = m["h"][:-1]; cases["length"] = m
    m = json.loads(json.dumps(base)); del m["v"]; cases["missing v"] = m
    m = json.loads(json.dumps(base)); m["t"][5] = m["t"][4]; cases["duplicate ts"] = m
    m = json.loads(json.dumps(base)); m["l"][2] = str(Decimal(base["h"][2]) + 1); cases["low above high"] = m
    m = json.loads(json.dumps(base)); m["o"][1] = None; cases["null"] = m
    m = json.loads(json.dumps(base)); m["s"] = "error"; m["errmsg"] = "boom"; cases["error"] = m
    for name, payload in cases.items():
        c, _ = client({"/v1/udf/history": jb(payload)}, clock=base["t"][-1] + 10_000)
        with pytest.raises(WallexDataError):
            c.candles("BTCUSDT", "1", base["t"][0], base["t"][-1])


def test_no_data_is_empty(fetched_at):
    c, _ = client({"/v1/udf/history": jb({"s": "no_data"})}, clock=fetched_at)
    assert c.candles("BTCUSDT", "1", fetched_at - 600, fetched_at) == []


# ---- depth / trades ----------------------------------------------------------
def test_depth_parse_and_quote(fx_bytes, fx):
    c, _ = client({"/v1/depth": fx_bytes("depth_BTCUSDT.json")}, clock=1792604388)
    d = c.depth("BTCUSDT")
    assert len(d.asks) == len(d.bids) == 50
    assert all(isinstance(p, Decimal) and isinstance(q, Decimal) for p, q in d.asks + d.bids)
    assert d.asks[0][0] == min(p for p, _ in d.asks) and d.bids[0][0] == max(p for p, _ in d.bids)
    q = d.quote
    assert (q.bid, q.ask, q.ts) == (d.bids[0][0], d.asks[0][0], 1792604388)
    assert q.ask > q.bid and q.spread_bps > 0
    raw = fx("depth_BTCUSDT.json")["result"]
    assert q.ask == Decimal(str(min(r["price"] for r in raw["ask"])))  # exact, no float artefacts


def test_depth_mutations_fail_closed(fx):
    base = fx("depth_BTCUSDT.json")
    cases = []
    m = json.loads(json.dumps(base)); del m["result"]["ask"][0]["price"]; cases.append(m)
    m = json.loads(json.dumps(base)); m["result"]["bid"][0]["quantity"] = "x"; cases.append(m)
    m = json.loads(json.dumps(base)); m["result"]["bid"] = []; cases.append(m)
    m = json.loads(json.dumps(base)); del m["result"]["bid"]; cases.append(m)
    m = json.loads(json.dumps(base)); m["result"]["bid"][0]["price"] = m["result"]["ask"][0]["price"] + 1; cases.append(m)
    for payload in cases:
        c, _ = client({"/v1/depth": jb(payload)})
        with pytest.raises(WallexDataError):
            c.depth("BTCUSDT")


def test_trades_parse_oldest_first(fx_bytes):
    c, _ = client({"/v1/trades": fx_bytes("trades_BTCUSDT.json")})
    ts = c.trades("BTCUSDT")
    assert len(ts) == 50
    assert [t.ts for t in ts] == sorted(t.ts for t in ts)
    t = ts[-1]
    assert isinstance(t.price, Decimal) and isinstance(t.quantity, Decimal) and isinstance(t.is_buy, bool)


def test_trades_mutations_fail_closed(fx):
    base = fx("trades_BTCUSDT.json")
    for field, value in (("price", "NaN"), ("quantity", None), ("timestamp", "yesterday"), ("isBuyOrder", "yes")):
        m = json.loads(json.dumps(base))
        m["result"]["latestTrades"][4][field] = value
        c, _ = client({"/v1/trades": jb(m)})
        with pytest.raises(WallexDataError):
            c.trades("BTCUSDT")
    m = json.loads(json.dumps(base)); del m["result"]["latestTrades"][0]["timestamp"]
    c, _ = client({"/v1/trades": jb(m)})
    with pytest.raises(WallexDataError):
        c.trades("BTCUSDT")


# ---- transport safety --------------------------------------------------------
@pytest.mark.parametrize("url", [
    "https://evil.example.com/v1/depth?symbol=BTCUSDT",
    "http://api.wallex.ir/v1/depth?symbol=BTCUSDT",
    "https://api.wallex.ir.evil.com/v1/depth",
    "https://api.wallex.ir@evil.com/v1/depth",
    "https://api.wallex.ir:8443/v1/depth",
    "ftp://api.wallex.ir/v1/depth",
])
def test_wrong_host_rejected_before_any_request(url):
    c, t = client({})
    with pytest.raises(WallexHostError):
        c.get_json(url)
    assert t.calls == []


@pytest.mark.parametrize("method", ["POST", "DELETE", "PUT", "get"])
def test_only_get_is_possible(method):
    c, t = client({})
    with pytest.raises(WallexHostError):
        c._request(method, "https://api.wallex.ir/v1/depth?symbol=BTCUSDT")
    assert t.calls == []


def test_retries_5xx_then_succeeds(fx_bytes):
    c, t = client({"/v1/depth": [(500, b""), (503, b""), fx_bytes("depth_BTCUSDT.json")]})
    assert len(c.depth("BTCUSDT").asks) == 50
    assert [m for m, _ in t.calls] == ["GET"] * 3


def test_retries_timeouts_and_429(fx_bytes):
    c, t = client({"/v1/depth": [TimeoutError("t"), (429, b""), OSError("reset"), fx_bytes("depth_BTCUSDT.json")]})
    assert len(c.depth("BTCUSDT").bids) == 50
    assert len(t.calls) == 4


def test_retries_are_bounded():
    c, t = client({"/v1/depth": (500, b"")}, max_retries=2)
    with pytest.raises(WallexHTTPError):
        c.depth("BTCUSDT")
    assert len(t.calls) == 3


def test_4xx_not_retried():
    c, t = client({"/v1/depth": (404, b"nope")})
    with pytest.raises(WallexHTTPError):
        c.depth("BTCUSDT")
    assert len(t.calls) == 1


def test_invalid_json_is_data_error():
    c, _ = client({"/v1/depth": b"<html>"})
    with pytest.raises(WallexDataError):
        c.depth("BTCUSDT")


def test_rate_limiter_spaces_requests(fx_bytes):
    now = [100.0]
    sleeps = []
    t = FakeTransport({"/v1/depth": fx_bytes("depth_BTCUSDT.json")})
    c = WallexPublicClient(transport=t, clock=lambda: now[0], sleep=sleeps.append, min_interval_s=0.5)
    c.depth("BTCUSDT")
    c.depth("BTCUSDT")  # clock did not advance
    assert sleeps == [0.5]
