from decimal import Decimal

import pytest

from wallexbot.data.aggregate import aggregate, aggregate_to
from wallexbot.data.candles import Candle, CandleDataError
from wallexbot.exchange.wallex_public import WallexPublicClient

T0 = 124484 * 14400  # 2026-10-21 UTC; multiple of 4h so 15m/1h/4h are all aligned
assert T0 % 14400 == 0


def c(minute, o, h, l, cl, v):
    return Candle(T0 + minute * 60, Decimal(o), Decimal(h), Decimal(l), Decimal(cl), Decimal(v))


def fifteen_minutes(offset=0, skip=()):
    """15 hand-written 1m bars: open 100, then +1 each minute; high = open+2, low = open-1."""
    return [c(offset + i, str(100 + i), str(102 + i), str(99 + i), str(101 + i), "1.5") for i in range(15) if i not in skip]


def test_hand_computed_15m():
    bars = fifteen_minutes() + [c(15, "200", "201", "199", "200", "1")]  # next bucket's first minute proves bucket 0 closed
    out = aggregate(bars, 15)
    assert len(out) == 1  # second bucket has 1 of 15 minutes and is still forming
    b = out[0]
    assert b.ts == T0
    assert b.open == Decimal("100")  # first open
    assert b.high == Decimal("116")  # max high: 102 + 14
    assert b.low == Decimal("99")  # min low
    assert b.close == Decimal("115")  # last close: 101 + 14
    assert b.volume == Decimal("22.5")  # 15 * 1.5
    assert b.missing == 0


def test_partial_bucket_not_emitted():
    bars = fifteen_minutes()[:-1]  # 14 of 15 minutes, last input is minute 13
    assert aggregate(bars, 15) == []
    # last 1m bar present => the bucket has demonstrably ended
    assert len(aggregate(fifteen_minutes(), 15)) == 1


def test_now_gates_emission():
    bars = fifteen_minutes()
    assert aggregate(bars, 15, now=T0 + 15 * 60 - 1) == []
    assert len(aggregate(bars, 15, now=T0 + 15 * 60)) == 1


def test_missing_minutes_flagged_and_still_aggregated():
    bars = fifteen_minutes(skip={3, 7}) + [c(15, "1", "1", "1", "1", "0")]
    b = aggregate(bars, 15)[0]
    assert b.missing == 2
    assert b.volume == Decimal("19.5")  # 13 * 1.5


def test_trailing_missing_minute_uses_now():
    bars = fifteen_minutes(skip={14})  # final minute had no trades
    assert aggregate(bars, 15) == []  # cannot know from data alone
    out = aggregate(bars, 15, now=T0 + 900)
    assert len(out) == 1 and out[0].missing == 1 and out[0].close == Decimal("114")


def test_buckets_are_utc_aligned_and_empty_buckets_skipped():
    bars = [c(7, "1", "1", "1", "1", "1"), c(8, "2", "2", "2", "2", "1"), c(45, "3", "3", "3", "3", "1"), c(61, "4", "4", "4", "4", "1")]
    out = aggregate(bars, 15)
    assert [b.ts for b in out] == [T0, T0 + 45 * 60]  # 15..29 and 30..44 buckets empty -> absent; last still forming
    assert out[0].missing == 13 and out[0].open == Decimal("1") and out[0].close == Decimal("2")
    one_h = aggregate(bars, 60)
    assert [b.ts for b in one_h] == [T0]  # the 01:00 bucket (minute 61) is still forming


def test_input_validation():
    with pytest.raises(CandleDataError):
        aggregate([c(1, "1", "1", "1", "1", "1"), c(1, "1", "1", "1", "1", "1")], 15)
    with pytest.raises(CandleDataError):
        aggregate([c(2, "1", "1", "1", "1", "1"), c(1, "1", "1", "1", "1", "1")], 15)
    with pytest.raises(CandleDataError):
        aggregate([Candle(T0 + 5, *[Decimal(1)] * 5)], 15)
    with pytest.raises(ValueError):
        aggregate_to([], "5m")
    assert aggregate([], 15) == []


def test_aggregate_to_names():
    bars = [c(i, "1", "1", "1", "1", "1") for i in range(0, 241)]
    assert len(aggregate_to(bars, "4h")) == 1
    assert len(aggregate_to(bars, "1h")) == 4
    assert len(aggregate_to(bars, "15m")) == 16


def test_1m_to_1h_matches_native_hourly_fixture(fx, fx_bytes, fetched_at):
    """Cross-check against the exchange's own 60-minute bars. (A 15m check is impossible: the live
    API returns 1m bars for resolution=15, see test_resolution_not_honoured_by_server_fails_closed.)"""
    raw1 = fx("udf_1m_13h.json")
    raw60 = fx("udf_60_13h.json")

    def transport(fixture):
        return lambda method, url, timeout: (200, fx_bytes(fixture))

    mk = lambda f: WallexPublicClient(transport=transport(f), clock=lambda: fetched_at, sleep=lambda s: None)
    m1 = mk("udf_1m_13h.json").candles("BTCUSDT", "1", raw1["t"][0], raw1["t"][-1])
    h1 = mk("udf_60_13h.json").candles("BTCUSDT", "60", raw60["t"][0], raw60["t"][-1])
    agg = aggregate(m1, 60, now=fetched_at)
    assert agg[0].missing == 40  # fixture starts mid-hour: the leading partial bucket is flagged, not trusted
    agg = [b for b in agg if b.missing == 0]
    assert len(h1) == 12 and len(agg) == 12
    for mine, native in zip(agg, h1):
        assert mine.ts == native.ts
        assert (mine.high, mine.low, mine.close, mine.volume) == (native.high, native.low, native.close, native.volume)
        assert mine.missing == 0
        # Real API quirk: the 1m series forward-fills no-trade minutes as flat zero-volume bars, while the
        # native hourly open is the open of the first bar that actually traded. open=first-1m-bar differs
        # from native only when the hour starts with such filler bars.
        hour = [b for b in m1 if mine.ts <= b.ts < mine.ts + 3600]
        first_traded = next(b for b in hour if b.volume > 0)
        assert native.open == first_traded.open
        if hour[0].volume > 0:
            assert mine.open == native.open
