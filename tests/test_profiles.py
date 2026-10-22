"""--profile strict|demo. `demo` is an EXERCISE profile for mock/shadow only: it makes the pipeline
produce a full lifecycle under realistic costs, where strict S1 abstains; it is labelled
everywhere (banner, every bot audit event, healthcheck) and refused on any live path."""
from __future__ import annotations

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from test_bot_helpers import SYM, WINNER, Rig, cheap_config, series

from wallexbot import cli
from wallexbot.bot import main as bot_main
from wallexbot.bot.feed import explode_bars
from wallexbot.bot.loop import Bot
from wallexbot.config import load_config
from wallexbot.data.candles import write_candles_csv
from wallexbot.store import Store
from wallexbot.strategy.config import MomentumParams
from wallexbot.strategy.profiles import (DEMO_PARAMS, EXERCISE_BANNER, ProfileError, assert_profile_allowed,
                                         momentum_params)

ROOT = Path(__file__).resolve().parents[1]


def _replay_dir(tmp_path):
    data = tmp_path / "history"
    write_candles_csv(data / SYM / f"{SYM}_1m_2023-11.csv", explode_bars(series(WINNER)))
    return data


def _healthcheck():
    spec = importlib.util.spec_from_file_location("deploy_healthcheck_p", ROOT / "deploy" / "healthcheck.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_demo_full_lifecycle_and_strict_abstains_under_realistic_costs(tmp_path):
    cfg = load_config(env={})                      # default fees: 0.2 % taker (public USDT-pair rate)
    assert cfg.taker_rate == Decimal("0.002") and cfg.maker_rate == Decimal("0.002")
    data = _replay_dir(tmp_path)
    strict = bot_main.run_replay(tmp_path / "s.db", data, cfg, profile="strict")
    assert strict["intents"] == 0 and strict["signals"] == 0 and strict["profile"] == "strict"
    demo = bot_main.run_replay(tmp_path / "d.db", data, cfg, profile="demo")
    assert demo["profile"] == "demo" and demo["signals"] >= 1
    assert demo["intents_by_state"].get("filled", 0) >= 2 and demo["tracked"] == []   # entered and closed
    s = Store(tmp_path / "d.db")
    try:
        kinds = {e["kind"] for e in s.audit_events(actor="bot")}
        assert {"signal", "entry_submitted", "position_opened", "exit_submitted", "position_closed"} <= kinds
        states = [r["to_state"] for r in s.rows("intent_state_log")]
        assert {"proposed", "risk_approved", "submitted", "filled"} <= set(states)
        assert all(d["approved"] == 1 for d in s.rows("risk_decisions"))
        # every bot audit event with a payload says which profile produced it
        evs = [e for e in s.audit_events(actor="bot") if isinstance(e["payload"], dict)]
        assert evs and all(e["payload"].get("profile") == "demo" for e in evs)
    finally:
        s.close()


def test_demo_params_are_only_relaxations():
    strict = MomentumParams()
    assert momentum_params("strict", "mock") == strict
    assert DEMO_PARAMS.cost_mult == 0.5 and DEMO_PARAMS.cost_mult < strict.cost_mult
    assert DEMO_PARAMS.volume_mult < strict.volume_mult and DEMO_PARAMS.atr_pct_min < strict.atr_pct_min
    assert DEMO_PARAMS.min_score <= strict.min_score and DEMO_PARAMS.stop_atr_mult == strict.stop_atr_mult


@pytest.mark.parametrize("mode,config_mode", [("live", None), ("live", "live"), ("mock", "live"),
                                              ("shadow", "live"), ("paper", None), ("None", None)])
def test_demo_is_refused_for_any_live_path(mode, config_mode):
    with pytest.raises(ProfileError):
        assert_profile_allowed("demo", mode, config_mode)
    with pytest.raises(ProfileError):
        momentum_params("demo", mode, config_mode)


def test_bot_refuses_demo_without_an_explicit_mock_or_shadow_run(tmp_path):
    rig = Rig(tmp_path)
    args = (rig.feed, rig.bot.engine, rig.bot.exit_policy, rig.bot.sizing, rig.oms, rig.risk, rig.store, rig.ks,
            rig.cfg, rig.clock)
    with pytest.raises(ProfileError):
        Bot(*args, profile="demo")                              # no run mode given
    with pytest.raises(ProfileError):
        Bot(*args, profile="demo", run_info={"mode": "live"})
    assert Bot(*args, profile="demo", run_info={"mode": "shadow"}).profile == "demo"
    with pytest.raises(ProfileError):
        assert_profile_allowed("bogus", "mock")
    rig.close()


def test_cli_banner_and_refusals(tmp_path, capsys):
    data = _replay_dir(tmp_path)
    assert bot_main.main(["--db", str(tmp_path / "r.db"), "--mode", "mock", "--replay-from", str(data),
                          "--max-ticks", "3", "--profile", "demo"]) == 0
    out = capsys.readouterr().out
    assert EXERCISE_BANNER == "EXERCISE PROFILE - NO EDGE CLAIMED - SHADOW/PAPER ONLY"
    assert EXERCISE_BANNER in out and "profile: demo" in out
    assert bot_main.main(["--db", str(tmp_path / "s.db"), "--mode", "mock", "--replay-from", str(data),
                          "--max-ticks", "3"]) == 0
    assert EXERCISE_BANNER not in capsys.readouterr().out
    assert bot_main.main(["--db", str(tmp_path / "l.db"), "--mode", "live", "--profile", "demo"]) == 2
    assert cli.main(["bot", "--db", str(tmp_path / "l.db"), "--mode", "live", "--profile", "demo"]) == 2
    assert cli.main(["bot", "--db", str(tmp_path / "l.db"), "--mode", "mock", "--profile", "demo",
                     "--arm-live"]) == 2
    with pytest.raises(SystemExit):
        bot_main.main(["--db", str(tmp_path / "x.db"), "--mode", "mock", "--profile", "aggressive"])
    assert not (tmp_path / "l.db").exists()


def test_healthcheck_reports_the_profile(tmp_path):
    hc = _healthcheck()
    data = _replay_dir(tmp_path)
    db = tmp_path / "r.db"
    assert bot_main.main(["--db", str(db), "--mode", "mock", "--replay-from", str(data), "--max-ticks", "3",
                          "--profile", "demo"]) == 0
    s = Store(db)
    now = s.audit_events("strategy", "heartbeat")[-1]["ts"]
    s.close()
    ns = hc.argparse.Namespace(db=str(db), max_heartbeat_age_s=120.0, alert_window_min=15.0,
                               allow_killswitch=False, recorder_dir=None, max_recorder_age_s=300.0)
    code, st = hc.run(ns, now=now)
    assert code == 0 and st["profile"] == "demo" and st["exercise_profile"] is True
    assert st["guardian_enabled"] is True and st["guardian_heartbeat_age_s"] is not None


def test_cheap_config_strict_still_trades(tmp_path):
    """Control: strict is unchanged (with the permissive test fee it trades the synthetic breakout)."""
    out = bot_main.run_replay(tmp_path / "c.db", _replay_dir(tmp_path), cheap_config(), profile="strict")
    assert out["intents_by_state"] == {"filled": 3}
