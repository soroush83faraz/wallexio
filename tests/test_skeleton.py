from decimal import Decimal

import pytest

from wallexbot.config import ConfigError, load_config
from wallexbot.types import D, OrderIntent, OrderType, Side, round_down


def test_round_down_never_rounds_up():
    assert round_down(Decimal("0.123999"), 3) == Decimal("0.123")


def test_intent_id_deterministic():
    a = OrderIntent("BTCUSDT", Side.BUY, OrderType.LIMIT, D("0.1"), D("100"), "sig1")
    b = OrderIntent("BTCUSDT", Side.BUY, OrderType.LIMIT, D("0.1"), D("100"), "sig1")
    assert a.client_id == b.client_id and a.client_id.replace("_", "").isalnum()


def test_live_requires_arming(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(open("config/default.yaml").read().replace("mode: mock", "mode: live"))
    with pytest.raises(ConfigError):
        load_config(p, env={})


def test_default_loads():
    assert load_config(env={}).mode == "mock"
