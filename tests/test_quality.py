from decimal import Decimal

import pytest

from wallexbot.data.candles import Candle, CandleDataError, read_candles_csv, sidecar_path, write_candles_csv
from wallexbot.data.quality import check_candles, require_strictly_increasing

T0 = 1_792_560_000


def c(i, vol="1", ts=None):
    p = Decimal(100 + i)
    return Candle(T0 + i * 60 if ts is None else ts, p, p + 1, p - 1, p, Decimal(vol))


def test_clean_series():
    r = check_candles([c(i) for i in range(10)])
    assert r.ok and r.n == 10 and r.missing_total == 0 and r.coverage == 1.0
    assert (r.first_ts, r.last_ts) == (T0, T0 + 9 * 60)


def test_gap_detection():
    r = check_candles([c(0), c(1), c(5), c(6), c(10)])
    assert r.gaps == ((T0 + 2 * 60, 3), (T0 + 7 * 60, 3))
    assert r.missing_total == 6 and not r.ok and r.strictly_increasing
    assert r.coverage == pytest.approx(5 / 11)


def test_duplicates_and_out_of_order():
    r = check_candles([c(0), c(1), c(1), c(3), c(2), c(4)])
    assert r.duplicates == (T0 + 60,)
    assert r.out_of_order == (T0 + 120,)
    assert not r.strictly_increasing and not r.ok
    with pytest.raises(CandleDataError):
        require_strictly_increasing([c(0), c(1), c(1)])
    require_strictly_increasing([c(0), c(1), c(2)])


def test_zero_volume_runs_reported_not_fatal():
    bars = [c(0), c(1, "0"), c(2, "0"), c(3, "0"), c(4, "0"), c(5, "0"), c(6), c(7, "0"), c(8, "0"), c(9, "0")]
    r = check_candles(bars, zero_run_min=3)
    assert r.zero_volume_runs == ((T0 + 60, 5), (T0 + 7 * 60, 3))
    assert r.ok  # still a clean grid
    assert check_candles(bars, zero_run_min=6).zero_volume_runs == ()
    # a run at the very end is reported too
    assert check_candles([c(0), c(1, "0"), c(2, "0")], zero_run_min=2).zero_volume_runs == ((T0 + 60, 2),)


def test_misaligned_and_other_step():
    r = check_candles([c(0), c(1, ts=T0 + 61)])
    assert r.misaligned == (T0 + 61,) and not r.ok
    r15 = check_candles([c(0, ts=T0), c(0, ts=T0 + 900), c(0, ts=T0 + 2700)], step_s=900)
    assert r15.gaps == ((T0 + 1800, 1),)


def test_empty():
    r = check_candles([])
    assert r.n == 0 and r.ok and r.first_ts is None and r.coverage == 1.0


# ---- CSV cache with sha256 sidecar -------------------------------------------
def test_csv_roundtrip_exact_decimals(tmp_path):
    bars = [Candle(T0, Decimal("84448.50"), Decimal("84500"), Decimal("84448.49"), Decimal("84448.49"), Decimal("0.00358376"), 0),
            Candle(T0 + 60, Decimal("1E-7"), Decimal("2E-7"), Decimal("1E-7"), Decimal("2E-7"), Decimal("1E+3"), 2)]
    p = tmp_path / "sub" / "BTCUSDT_1m.csv"
    digest = write_candles_csv(p, bars)
    assert sidecar_path(p).read_text().strip() == digest and len(digest) == 64
    assert read_candles_csv(p) == bars
    assert "E" not in p.read_text()  # plain notation


def test_csv_tamper_and_missing_sidecar_detected(tmp_path):
    p = tmp_path / "x.csv"
    write_candles_csv(p, [c(0), c(1)])
    p.write_text(p.read_text().replace(str(T0), str(T0 + 1), 1))
    with pytest.raises(CandleDataError):
        read_candles_csv(p)
    sidecar_path(p).unlink()
    with pytest.raises(CandleDataError):
        read_candles_csv(p)
    assert read_candles_csv(p, verify=False)[0].ts == T0 + 1


def test_candle_rejects_inconsistent_ohlc():
    with pytest.raises(CandleDataError):
        Candle(T0, Decimal(10), Decimal(9), Decimal(8), Decimal(9), Decimal(1))
    with pytest.raises(CandleDataError):
        Candle(T0, Decimal(10), Decimal(11), Decimal(9), Decimal(10), Decimal(-1))
