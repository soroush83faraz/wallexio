"""OMS.submit under store faults: the risk transaction is all-or-nothing and a PROPOSED is resumable.

Verifier fault1.py: `_cas` swallowed an OperationalError raised by set_intent_state INSIDE the risk
transaction, so an approved risk_decisions row committed while the intent stayed PROPOSED, and a
retry wrote a second decision row. lock.py: `database is locked` after append_intent left a
PROPOSED that only a resubmission of the same intent can move on.
"""
from __future__ import annotations

import sqlite3
import threading
from collections import Counter
from decimal import Decimal as D

import pytest

from wallexbot.store import Store
from wallexbot.types import IntentState, OrderIntent, OrderType, Side

S = IntentState
SYM = "BTCUSDT"


def _buy(sig="F1"):
    return OrderIntent(SYM, Side.BUY, OrderType.MARKET, D("0.010000"), None, sig)


def _count_sends(rig):
    sent = Counter()
    orig = rig.broker.submit_order

    def counting(**kw):
        sent[kw["client_id"]] += 1
        return orig(**kw)
    rig.broker.submit_order = counting
    return sent


def _decisions(store, cid):
    return [r for r in store.rows("risk_decisions") if r["client_id"] == cid]


@pytest.mark.parametrize("approved", [True, False])
def test_store_error_inside_risk_tx_rolls_back_decision_and_propagates(rig, monkeypatch, approved):
    sent = _count_sends(rig)
    intent = _buy() if approved else OrderIntent(SYM, Side.BUY, OrderType.MARKET, D("50"), None, "F2")
    real = rig.store.set_intent_state

    def boom(*a, **k):
        raise sqlite3.OperationalError("disk I/O error")
    monkeypatch.setattr(rig.store, "set_intent_state", boom)
    with pytest.raises(sqlite3.OperationalError):
        rig.submit(intent)
    assert rig.store.get_intent(intent.client_id).state is S.PROPOSED
    assert _decisions(rig.store, intent.client_id) == []          # no decision without its transition
    assert not rig.store._conn.in_transaction and rig.store._depth == 0
    assert any(e["source"] == "oms.submit" for e in rig.store.rows("errors"))
    assert not sent

    monkeypatch.setattr(rig.store, "set_intent_state", real)
    res = rig.submit(intent)                                       # resumed: same intent, same client_id
    rows = _decisions(rig.store, intent.client_id)
    assert len(rows) == 1 and bool(rows[0]["approved"]) is approved
    if approved:
        assert res.state is S.FILLED and sent == {intent.client_id: 1}
    else:
        assert res.state is S.REJECTED and not sent
    assert rig.submit(intent).sent is False and len(_decisions(rig.store, intent.client_id)) == 1


def test_error_on_the_decision_row_also_rolls_back(rig, monkeypatch):
    intent = _buy("F3")

    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(rig.store, "append_risk_decision", boom)
    with pytest.raises(sqlite3.OperationalError):
        rig.submit(intent)
    assert rig.store.get_intent(intent.client_id).state is S.PROPOSED
    monkeypatch.undo()
    assert rig.submit(intent).state is S.FILLED


def test_resubmitting_a_proposed_intent_resumes_it(rig):
    """A PROPOSED left by an earlier caller (nothing sent) proceeds on the next submit()."""
    sent = _count_sends(rig)
    intent = _buy("F4")
    assert rig.store.append_intent(intent)
    res = rig.submit(intent)
    assert res.state is S.FILLED and res.sent and sent == {intent.client_id: 1}


def test_every_store_connection_waits_on_a_busy_database(tmp_path):
    s = Store(tmp_path / "a.db")
    try:
        assert s._conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        s.close()


def test_lock_between_append_and_risk_tx_leaves_a_resumable_proposed(rig, monkeypatch):
    """lock.py case 2, shortened: another connection holds the write lock past the busy timeout
    right after append_intent; the risk transaction fails, nothing is decided, a retry succeeds."""
    sent = _count_sends(rig)
    rig.store._conn.execute("PRAGMA busy_timeout=50")
    other = sqlite3.connect(rig.store.path, isolation_level=None, check_same_thread=False)
    intent = _buy("F5")
    real = rig.store.append_intent

    def append_then_lock(*a, **k):
        out = real(*a, **k)
        other.execute("BEGIN IMMEDIATE")
        return out
    monkeypatch.setattr(rig.store, "append_intent", append_then_lock)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        rig.submit(intent)
    other.execute("COMMIT")
    monkeypatch.undo()
    assert rig.store.get_intent(intent.client_id).state is S.PROPOSED and not sent
    assert rig.submit(intent).state is S.FILLED and sent == {intent.client_id: 1}
    assert len(_decisions(rig.store, intent.client_id)) == 1
    other.close()


def test_interleaved_resumes_send_once(rig):
    """Two connections resume the same PROPOSED intent: the second one, attempted while the first
    holds the risk transaction, fails on the write lock (nothing written); retried afterwards it
    finds the intent past PROPOSED and sends nothing."""
    from wallexbot.oms import OMS
    from wallexbot.pipeline import auto_approve
    from wallexbot.risk.engine import RiskEngine
    from wallexbot.risk.killswitch import KillSwitch
    sent = _count_sends(rig)
    intent = _buy("F6")
    rig.store.append_intent(intent)
    other = Store(rig.store.path, clock=rig.clock)
    other._conn.execute("PRAGMA busy_timeout=0")
    ks2 = KillSwitch(other)
    oms2 = OMS(other, rig.broker, RiskEngine(rig.cfg, rig.rules, ks2), ks2, approve=auto_approve, clock=rig.clock)
    acct, q = rig.account(), rig.broker.quote(SYM)
    seen = []
    real = rig.risk.check

    def check_then_race(*a, **k):
        try:
            oms2.submit(intent, q, acct)
        except sqlite3.OperationalError as e:
            seen.append(str(e))
        return real(*a, **k)
    rig.risk.check = check_then_race
    assert rig.oms.submit(intent, q, acct).sent
    rig.risk.check = real
    assert seen and "locked" in seen[0]
    assert oms2.submit(intent, q, acct).sent is False
    assert sent == {intent.client_id: 1} and len(_decisions(rig.store, intent.client_id)) == 1
    other.close()
