from decimal import Decimal

import pytest

from wallexbot.config import DEFAULT_PATH, ConfigError, load_config
from wallexbot.types import D, OrderIntent, OrderType, Side, round_down


def test_round_down_never_rounds_up():
    assert round_down(Decimal("0.123999"), 3) == Decimal("0.123")


def test_intent_id_deterministic():
    a = OrderIntent("BTCUSDT", Side.BUY, OrderType.LIMIT, D("0.1"), D("100"), "sig1")
    b = OrderIntent("BTCUSDT", Side.BUY, OrderType.LIMIT, D("0.1"), D("100"), "sig1")
    assert a.client_id == b.client_id and a.client_id.replace("_", "").isalnum()


def test_live_requires_arming(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(DEFAULT_PATH.read_text().replace("mode: mock", "mode: live"))
    with pytest.raises(ConfigError):
        load_config(p, env={})


def test_default_loads():
    assert load_config(env={}).mode == "mock"


def test_default_config_is_a_package_resource():
    from importlib import resources
    res = resources.files("wallexbot").joinpath("default_config.yaml")
    assert res.is_file() and "mode:" in res.read_text()
    assert DEFAULT_PATH.is_file() and DEFAULT_PATH.name == "default_config.yaml"


def test_env_var_overrides_default_and_is_fail_closed(tmp_path, monkeypatch):
    base = DEFAULT_PATH.read_text()
    shadow = tmp_path / "s.yaml"
    shadow.write_text(base.replace("mode: mock", "mode: shadow"))
    monkeypatch.setenv("WALLEXBOT_CONFIG", str(shadow))
    assert load_config(env={}).mode == "shadow"
    assert load_config(DEFAULT_PATH, env={}).mode == "mock"          # an explicit path wins
    live = tmp_path / "l.yaml"
    live.write_text(base.replace("mode: mock", "mode: live"))
    monkeypatch.setenv("WALLEXBOT_CONFIG", str(live))
    with pytest.raises(ConfigError):                                    # live rules unchanged
        load_config(env={})
    with pytest.raises(ConfigError):
        load_config(env={"WALLEX_ARM_LIVE": "YES_I_UNDERSTAND"})        # still needs cli_armed + trade key
    monkeypatch.setenv("WALLEXBOT_CONFIG", str(tmp_path / "missing.yaml"))
    with pytest.raises(ConfigError):                                    # no silent fallback to the default
        load_config(env={})
    monkeypatch.setenv("WALLEXBOT_CONFIG", "")
    assert load_config(env={}).mode == "mock"                           # empty = unset (env_file templates)
