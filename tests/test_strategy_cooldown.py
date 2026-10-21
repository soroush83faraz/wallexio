import json
from decimal import Decimal

import pytest

from wallexbot.strategy.cooldown import CooldownTracker

BAR = 900
T = 1_700_000_000
L = Decimal("101.69")


def bars_later(n):
    return T + n * BAR


def test_no_history_allows():
    assert CooldownTracker(BAR).allows("BTCUSDT", T)


def test_default_cooldown_is_8_bars():
    c = CooldownTracker(BAR)
    c.record_exit("BTCUSDT", T, profitable=False)
    assert not c.allows("BTCUSDT", bars_later(7))
    assert c.allows("BTCUSDT", bars_later(8))


def test_short_cooldown_only_when_profitable_and_trend_intact():
    c = CooldownTracker(BAR)
    c.record_exit("A", T, profitable=True, trend_intact=True)
    c.record_exit("B", T, profitable=True, trend_intact=False)
    c.record_exit("C", T, profitable=False, trend_intact=True)
    assert not c.allows("A", bars_later(1)) and c.allows("A", bars_later(2))
    assert not c.allows("B", bars_later(7)) and c.allows("B", bars_later(8))
    assert not c.allows("C", bars_later(7)) and c.allows("C", bars_later(8))


def test_stop_out_blocks_same_breakout_leg_until_a_new_level():
    c = CooldownTracker(BAR)
    c.record_exit("A", T, profitable=False, stopped_out=True, breakout_level=L)
    assert not c.allows("A", bars_later(8), breakout_level=L)       # same leg, cooldown over
    assert not c.allows("A", bars_later(500), breakout_level=L)
    assert c.allows("A", bars_later(8), breakout_level=Decimal("103.00"))
    assert not c.allows("A", bars_later(3), breakout_level=Decimal("103.00"))  # still cooling down


def test_stop_out_ignores_profitable_short_cooldown():
    c = CooldownTracker(BAR)
    c.record_exit("A", T, profitable=True, trend_intact=True, stopped_out=True, breakout_level=L)
    assert not c.allows("A", bars_later(2), breakout_level=Decimal("103"))


def test_symbols_are_independent_and_latest_exit_wins():
    c = CooldownTracker(BAR)
    c.record_exit("A", T, profitable=False)
    assert c.allows("B", T)
    c.record_exit("A", bars_later(20), profitable=False)
    assert not c.allows("A", bars_later(21))


def test_roundtrip_through_json_dict():
    c = CooldownTracker(BAR, cooldown_bars=6, cooldown_bars_profitable=1)
    c.record_exit("A", T, profitable=False, stopped_out=True, breakout_level=L)
    c.record_exit("B", T, profitable=True, trend_intact=True)
    d = json.loads(json.dumps(c.to_dict()))
    r = CooldownTracker.from_dict(d)
    assert r.to_dict() == c.to_dict()
    assert not r.allows("A", bars_later(500), breakout_level=L)
    assert r.allows("B", bars_later(1))


def test_bad_bar_seconds():
    with pytest.raises(ValueError):
        CooldownTracker(0)
