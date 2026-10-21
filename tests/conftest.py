import json
from decimal import Decimal
from pathlib import Path

import pytest

from wallexbot.config import load_config
from wallexbot.exchange.mock_wallex import MockClock, MockWallex
from wallexbot.oms import OMS
from wallexbot.pipeline import auto_approve, snapshot_account
from wallexbot.risk.engine import RiskEngine
from wallexbot.risk.killswitch import KillSwitch
from wallexbot.store import Store
from wallexbot.types import SymbolRules

D = Decimal
SYM = "BTCUSDT"
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def cfg():
    return load_config(env={})


@pytest.fixture
def rules():
    return {SYM: SymbolRules(SYM, "BTC", "USDT", amount_precision=6, price_precision=2),
            "ETHUSDT": SymbolRules("ETHUSDT", "ETH", "USDT", amount_precision=4, price_precision=2)}


@pytest.fixture
def clock():
    return MockClock()


@pytest.fixture
def store(tmp_path, clock):
    s = Store(tmp_path / "audit.db", clock=clock)
    yield s
    s.close()


@pytest.fixture
def broker(rules, clock):
    b = MockWallex(rules.values(), clock=clock, balances={"USDT": D("10000")})
    b.set_price(SYM, D("60000.00"), D("60010.00"))
    return b


class Rig:
    def __init__(self, cfg, rules, clock, store, broker):
        self.cfg, self.rules, self.clock, self.store, self.broker = cfg, rules, clock, store, broker
        self.ks = KillSwitch(store)
        self.risk = RiskEngine(cfg, rules, self.ks)
        self.oms = OMS(store, broker, self.risk, self.ks, approve=auto_approve, clock=clock,
                       not_found_grace_s=30)

    def account(self):
        return snapshot_account(self.store, self.broker.balances(), "USDT",
                                {SYM: self.broker.quote(SYM)}, self.clock())

    def submit(self, intent):
        return self.oms.submit(intent, self.broker.quote(intent.symbol), self.account())


@pytest.fixture
def rig(cfg, rules, clock, store, broker):
    return Rig(cfg, rules, clock, store, broker)


@pytest.fixture
def fx():
    """Load a fixture as parsed JSON (plain floats are fine here; tests re-serialise to bytes)."""
    def load(name: str):
        return json.loads((FIXTURES / name).read_text())
    return load


@pytest.fixture
def fx_bytes():
    def load(name: str) -> bytes:
        return (FIXTURES / name).read_bytes()
    return load


@pytest.fixture
def fetched_at() -> int:
    """Unix time at which the fixtures were captured; the newest 1m/1h bars were still forming."""
    return int((FIXTURES / "FETCHED_AT.txt").read_text().strip())
