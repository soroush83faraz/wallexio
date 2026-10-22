import sqlite3
from decimal import Decimal

import pytest

from wallexbot.pipeline import auto_approve, run_scripted_trade
from wallexbot.risk.engine import RiskDecision
from wallexbot.store import Store
from wallexbot.types import IntentState

D = Decimal


def test_auto_approve_is_strict():
    assert auto_approve(RiskDecision(True)) is True
    assert auto_approve(RiskDecision(False, ("x",))) is False
    assert auto_approve(None) is False
    assert auto_approve(type("Fake", (), {"approved": True})()) is False


def test_scripted_trade_end_to_end(tmp_path):
    db = tmp_path / "demo.db"
    out = run_scripted_trade(str(db))
    assert out["entry"].state is IntentState.FILLED and out["exit"].state is IntentState.FILLED
    # default fee 0.2 %: buy 0.01 @ 60010 (fee 0.00002 BTC), sell 0.00998 @ 61200 (fee 0.2% in USDT)
    proceeds = D("0.00998") * D("61200.00")
    assert out["realized_pnl"] == proceeds * (1 - D("0.002")) - D("600.10")
    a = out["audit"]
    assert len(a["signals"]) == 1 and len(a["risk_decisions"]) == 2 and len(a["fills"]) == 2
    ids = {r["client_id"] for r in a["order_intents"]}
    assert {r["client_id"] for r in a["fills"]} == ids == {r["client_id"] for r in a["risk_decisions"]}
    assert [r["signal_id"] for r in a["order_intents"]] == ["scripted-breakout-1", "scripted-breakout-1-exit"]
    assert D(a["positions_snapshots"][-1]["quantity"]) == 0
    assert D(a["positions_snapshots"][-1]["closed_pnl"]) == out["realized_pnl"]
    assert a["errors"] == []
    # the audit DB on disk is intact and still append-only
    s = Store(db)
    assert len(s.rows("fills")) == 2
    with pytest.raises(sqlite3.DatabaseError):
        s._conn.execute("DELETE FROM fills")
    s.close()
