from decimal import Decimal

from wallexbot.risk.killswitch import KillSwitch
from wallexbot.store import Store
from wallexbot.types import OrderIntent, OrderType, Side

D = Decimal
ENTRY = OrderIntent("BTCUSDT", Side.BUY, OrderType.MARKET, D("0.01"), None, "s")
EXIT = OrderIntent("BTCUSDT", Side.SELL, OrderType.MARKET, D("0.01"), None, "s-x", reduce_only=True)
BAD_EXIT = OrderIntent("BTCUSDT", Side.BUY, OrderType.MARKET, D("0.01"), None, "s-b", reduce_only=True)


def test_fresh_store_is_disengaged(store):
    ks = KillSwitch(store)
    assert ks.is_engaged() is False and ks.allows(ENTRY)


def test_latest_row_wins_and_history_is_kept(store):
    ks = KillSwitch(store)
    ks.engage("drawdown", "guardian")
    assert ks.is_engaged()
    ks.disengage("reviewed", "operator")
    assert not ks.is_engaged()
    ks.engage("again")
    assert ks.is_engaged()
    assert [r["engaged"] for r in store.rows("killswitch_events")] == [1, 0, 1]


def test_engaged_blocks_entries_not_reduce_only_sells(store):
    ks = KillSwitch(store)
    ks.engage("test")
    assert ks.allows(ENTRY) is False
    assert ks.allows(EXIT) is True
    assert ks.allows(BAD_EXIT) is False  # reduce_only BUY is not an exit


class _BrokenStore:
    def latest_killswitch(self):
        raise OSError("disk gone")


class _WeirdStore:
    def latest_killswitch(self):
        class Ev:
            engaged = "maybe"
        return Ev()


def test_read_error_fails_closed():
    ks = KillSwitch(_BrokenStore())
    assert ks.is_engaged() is True
    assert ks.allows(ENTRY) is False
    assert ks.allows(EXIT) is True


def test_unexpected_value_fails_closed():
    assert KillSwitch(_WeirdStore()).is_engaged() is True


def test_closed_db_fails_closed(tmp_path):
    s = Store(tmp_path / "k.db")
    ks = KillSwitch(s)
    s.close()
    assert ks.is_engaged() is True
