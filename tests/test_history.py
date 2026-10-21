from decimal import Decimal

import pytest

from wallexbot.data.candles import Candle, CandleDataError
from wallexbot.data.history import RequestBudget, fetch_history, load_history
from wallexbot.exchange.wallex_public import WallexHTTPError

NOW = 1_700_000_000 // 60 * 60 + 30  # mid-bar "now"
LAST_CLOSED = NOW // 60 * 60 - 60


def bar(ts, vol="1"):
    return Candle(ts, Decimal("10"), Decimal("11"), Decimal("9"), Decimal("10"), Decimal(vol))


class FakeClient:
    """Mimics WallexPublicClient.candles: closed bars in [start, end] only, [] for no_data."""

    def __init__(self, floor, last=LAST_CLOSED, holes=(), fail_windows=0):
        self.floor, self.last, self.holes = floor, last, set(holes)
        self.calls = []
        self.fail = fail_windows

    def candles(self, symbol, resolution, start, end):
        assert resolution == "1"
        self.calls.append((start, end))
        if self.fail:
            self.fail -= 1
            raise WallexHTTPError("boom")
        assert (end - start) // 60 + 1 <= 600
        return [bar(t) for t in range(start, end + 1, 60)
                if self.floor <= t <= self.last and t not in self.holes]


def run(client, a, b, tmp_path, **kw):
    kw.setdefault("chunk_sleep_s", 0)
    kw.setdefault("sleep", lambda s: None)
    kw.setdefault("clock", lambda: NOW)
    return fetch_history(client, "BTCUSDT", a, b, tmp_path, **kw)


def test_chunking_boundaries_backwards(tmp_path):
    a = LAST_CLOSED - 1499 * 60  # 1500 bars -> 3 chunks of 600/600/300
    c = FakeClient(floor=0)
    r = run(c, a, LAST_CLOSED, tmp_path)
    assert c.calls == [(LAST_CLOSED - 599 * 60, LAST_CLOSED), (LAST_CLOSED - 1199 * 60, LAST_CLOSED - 600 * 60),
                       (a, LAST_CLOSED - 1200 * 60)]
    assert r.rows == 1500 and r.requests == 3 and r.reached_start and r.aborted is None
    assert r.gaps == 0 and r.coverage == 1.0 and r.window_coverage == 1.0
    assert (r.first_ts, r.last_ts) == (a, LAST_CLOSED)


def test_end_clamped_to_last_closed_bar(tmp_path):
    c = FakeClient(floor=0)
    r = run(c, LAST_CLOSED - 600, NOW + 10_000, tmp_path)
    assert r.requested_end == LAST_CLOSED and r.last_ts == LAST_CLOSED


def test_resume_fetches_only_missing_range(tmp_path):
    a = LAST_CLOSED - 1199 * 60
    mid = LAST_CLOSED - 600 * 60
    run(FakeClient(floor=0), mid, LAST_CLOSED, tmp_path)  # newest 601 bars
    c2 = FakeClient(floor=0)
    r = run(c2, a, LAST_CLOSED, tmp_path)
    assert c2.calls == [(a, mid - 60)]
    assert r.rows == 1200 and r.requests == 1
    assert [x.ts for x in load_history(tmp_path, "BTCUSDT", a, LAST_CLOSED)] == list(range(a, LAST_CLOSED + 1, 60))
    # third run: nothing to do
    c3 = FakeClient(floor=0)
    assert run(c3, a, LAST_CLOSED, tmp_path).requests == 0 and c3.calls == []


def test_resume_extends_forward_when_time_passes(tmp_path):
    a = LAST_CLOSED - 599 * 60
    run(FakeClient(floor=0), a, LAST_CLOSED, tmp_path)
    later = NOW + 180
    c = FakeClient(floor=0, last=later // 60 * 60 - 60)
    r = run(c, a, later, tmp_path, clock=lambda: later)
    assert c.calls == [(LAST_CLOSED + 60, later // 60 * 60 - 60)]
    assert r.rows == 603


def test_no_data_before_start_stops_and_reports_floor(tmp_path):
    floor = LAST_CLOSED - 700 * 60
    c = FakeClient(floor=floor)
    r = run(c, LAST_CLOSED - 5000 * 60, LAST_CLOSED, tmp_path)
    assert len(c.calls) == 3  # 600 + 101 bars + one empty chunk, then stop (not 9 chunks)
    assert r.history_floor_ts == floor and r.first_ts == floor and r.rows == 701 and r.reached_start
    # re-run does not re-request the pre-listing region
    c2 = FakeClient(floor=floor)
    assert run(c2, LAST_CLOSED - 5000 * 60, LAST_CLOSED, tmp_path).requests == 0


def test_checksum_mismatch_detected(tmp_path):
    a = LAST_CLOSED - 99 * 60
    r = run(FakeClient(floor=0), a, LAST_CLOSED, tmp_path)
    f = tmp_path / "BTCUSDT" / sorted(r.files)[0]
    f.write_text(f.read_text().replace("\n10,", "\n12,", 1) if "\n10," in f.read_text() else f.read_text() + "x")
    with pytest.raises(CandleDataError):
        load_history(tmp_path, "BTCUSDT", a, LAST_CLOSED)
    with pytest.raises(CandleDataError):  # resuming also refuses corrupted data
        run(FakeClient(floor=0), a, LAST_CLOSED, tmp_path)


def test_quality_report_gaps_and_zero_runs(tmp_path):
    a = LAST_CLOSED - 99 * 60
    holes = [a + 10 * 60, a + 11 * 60, a + 50 * 60]

    class ZeroVol(FakeClient):
        def candles(self, *args):
            rows = super().candles(*args)
            return [bar(c.ts, "0") if a + 70 * 60 <= c.ts < a + 80 * 60 else c for c in rows]

    r = run(ZeroVol(floor=0, holes=holes), a, LAST_CLOSED, tmp_path)
    assert r.rows == 97 and r.gaps == 2 and r.missing_bars == 3
    assert r.zero_volume_runs == 1
    assert r.coverage == pytest.approx(97 / 100) and r.window_coverage == pytest.approx(0.97)
    assert r.quality.gaps == ((a + 10 * 60, 2), (a + 50 * 60, 1))
    # real gaps are remembered as answered, not re-requested
    c2 = FakeClient(floor=0)
    assert run(c2, a, LAST_CLOSED, tmp_path).requests == 0


def test_monthly_files_with_sidecars(tmp_path):
    start = 1_706_745_600 - 100 * 60  # 100 min before 2024-02-01 00:00
    c = FakeClient(floor=0, last=start + 300 * 60)
    r = run(c, start, start + 199 * 60, tmp_path, clock=lambda: start + 10**6)
    names = sorted(r.files)
    assert names == ["BTCUSDT_1m_2024-01.csv", "BTCUSDT_1m_2024-02.csv"]
    assert all((tmp_path / "BTCUSDT" / (n + ".sha256")).exists() for n in names)
    assert len(load_history(tmp_path, "BTCUSDT", start, start + 199 * 60)) == 200


def test_abort_after_consecutive_failures_and_resume(tmp_path):
    a = LAST_CLOSED - 5999 * 60
    c = FakeClient(floor=0, fail_windows=100)
    r = run(c, a, LAST_CLOSED, tmp_path, max_consecutive_failures=2, chunk_retries=1)
    assert r.aborted and "consecutive" in r.aborted and not r.reached_start
    assert len(c.calls) == 4  # 2 chunks x (1 try + 1 retry)
    r2 = run(FakeClient(floor=0), a, LAST_CLOSED, tmp_path)  # resume completes it
    assert r2.reached_start and r2.rows == 6000 and r2.aborted is None


def test_request_budget_enforced_across_calls(tmp_path):
    b = RequestBudget(limit=2)
    c = FakeClient(floor=0)
    r = run(c, LAST_CLOSED - 2999 * 60, LAST_CLOSED, tmp_path, budget=b)
    assert len(c.calls) == 2 and b.used == 2 and r.aborted and "budget" in r.aborted
    assert r.rows == 1200
