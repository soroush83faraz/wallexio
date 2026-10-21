import sqlite3
from decimal import Decimal

import pytest

import wallexbot.store as store_mod
from wallexbot.risk.engine import RiskDecision
from wallexbot.store import (APPEND_ONLY_TABLES, LEGAL_TRANSITIONS, IllegalTransition, StaleState, Store,
                             StoreError)
from wallexbot.types import TERMINAL_STATES, Fill, IntentState, OrderIntent, OrderType, Position, Side

D = Decimal
S = IntentState


def _intent(qty="0.010", signal="sig1"):
    return OrderIntent("BTCUSDT", Side.BUY, OrderType.LIMIT, D(qty), D("60010.00"), signal)


def _populate(store):
    i = _intent()
    store.append_intent(i)
    store.append_signal("sig1", "BTCUSDT", {"a": 1})
    store.append_risk_decision(i.client_id, RiskDecision(True, ()))
    store.set_intent_state(i.client_id, S.RISK_APPROVED)
    store.append_broker_event(i.client_id, "submit_ack", {"x": D("1")})
    store.append_fill(Fill(i.client_id, "BTCUSDT", Side.BUY, D("1"), D("1"), D("0"), "BTC", 1.0),
                      seq=1, quote_amount=D("1"), fee_quote=D("0"))
    store.append_position_snapshot(Position("BTCUSDT", D("1"), D("1")))
    store.append_killswitch_event(True, "test", "pytest")
    store.append_equity_sod("2025-01-01", D("100"))
    store.append_equity_hwm(D("100"))
    store.append_error("test", "boom")
    return i


def test_wal_mode(store):
    assert store._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


@pytest.mark.parametrize("table", APPEND_ONLY_TABLES)
def test_append_only_tables_reject_update_and_delete(store, table):
    _populate(store)
    assert store._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] >= 1
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store._conn.execute(f"UPDATE {table} SET ts = ts + 1")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        store._conn.execute(f"DELETE FROM {table}")


def test_order_intents_only_state_and_updated_at_mutable(store):
    i = _populate(store)
    with pytest.raises(sqlite3.DatabaseError):
        store._conn.execute("UPDATE order_intents SET quantity='99' WHERE client_id=?", (i.client_id,))
    with pytest.raises(sqlite3.DatabaseError):
        store._conn.execute("DELETE FROM order_intents")
    store._conn.execute("UPDATE order_intents SET updated_at=updated_at+1 WHERE client_id=?", (i.client_id,))
    # the trigger itself rejects illegal transitions even when Python is bypassed
    with pytest.raises(sqlite3.DatabaseError, match="illegal"):
        store._conn.execute("UPDATE order_intents SET state='filled' WHERE client_id=?", (i.client_id,))
    store._conn.execute("UPDATE order_intents SET state='submitted' WHERE client_id=?", (i.client_id,))
    assert store.get_intent(i.client_id).state is S.SUBMITTED


def test_append_intent_is_idempotent_and_roundtrips_decimal_text(store):
    i = _intent(qty="0.010")
    assert store.append_intent(i) is True
    assert store.append_intent(i) is False
    rec = store.get_intent(i.client_id)
    assert rec.state is S.PROPOSED
    assert str(rec.intent.quantity) == "0.010" and rec.intent.client_id == i.client_id
    assert len(store.rows("order_intents")) == 1


def test_set_intent_state_legal_path_and_log(store):
    i = _intent()
    store.append_intent(i)
    for st in (S.RISK_APPROVED, S.SUBMITTED, S.PARTIALLY_FILLED, S.FILLED):
        store.set_intent_state(i.client_id, st, reason="r")
    log = [(r["from_state"], r["to_state"]) for r in store.rows("intent_state_log")]
    assert log == [(None, "proposed"), ("proposed", "risk_approved"), ("risk_approved", "submitted"),
                   ("submitted", "partially_filled"), ("partially_filled", "filled")]


@pytest.mark.parametrize("src,dst", [(S.PROPOSED, S.SUBMITTED), (S.PROPOSED, S.FILLED),
                                     (S.RISK_APPROVED, S.FILLED), (S.PARTIALLY_FILLED, S.SUBMITTED)])
def test_illegal_transitions_rejected(store, src, dst):
    i = _intent()
    store.append_intent(i)
    path = {S.PROPOSED: [], S.RISK_APPROVED: [S.RISK_APPROVED],
            S.PARTIALLY_FILLED: [S.RISK_APPROVED, S.SUBMITTED, S.PARTIALLY_FILLED]}[src]
    for st in path:
        store.set_intent_state(i.client_id, st)
    with pytest.raises(IllegalTransition):
        store.set_intent_state(i.client_id, dst)
    assert store.get_intent(i.client_id).state is src


def test_terminal_states_have_no_exits():
    for t in TERMINAL_STATES:
        assert LEGAL_TRANSITIONS[t] == frozenset()
    assert set(LEGAL_TRANSITIONS) == set(IntentState)


def test_compare_and_set(store):
    i = _intent()
    store.append_intent(i)
    with pytest.raises(StaleState):
        store.set_intent_state(i.client_id, S.SUBMITTED, expected=S.RISK_APPROVED)
    store.set_intent_state(i.client_id, S.RISK_APPROVED, expected=S.PROPOSED)
    with pytest.raises(StaleState):
        store.set_intent_state(i.client_id, S.REJECTED, expected=S.PROPOSED)


def test_redaction_applied_to_every_payload(store, monkeypatch):
    calls = []

    def fake_redact(obj):
        calls.append(obj)
        if isinstance(obj, str):
            return obj.replace("SECRET", "***")
        if isinstance(obj, dict):
            return {k: fake_redact(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [fake_redact(v) for v in obj]
        return obj

    monkeypatch.setattr(store_mod, "redact", fake_redact)
    i = _intent()
    store.append_signal("s", "BTCUSDT", {"api_key": "SECRET-123", "nested": [{"t": "xSECRETx"}]})
    store.append_intent(i, payload={"hdr": "SECRET"})
    store.append_broker_event(i.client_id, "submit_attempt", {"auth": "Bearer SECRET"})
    store.append_risk_decision(i.client_id, RiskDecision(False, ("SECRET-reason",)))
    store.append_error("x", "failed with SECRET", {"k": "SECRET"})
    store.append_killswitch_event(True, "SECRET reason", "SECRET actor")
    store.set_intent_state(i.client_id, S.REJECTED, reason="SECRET")
    assert calls, "redact() was never called"
    dump = repr([store.rows(t) for t in store_mod.AUDIT_TABLES])
    assert "SECRET" not in dump


def test_refuses_float_money(store):
    f = Fill("c", "BTCUSDT", Side.BUY, 1.5, D("1"), D("0"), "BTC", 1.0)
    with pytest.raises(StoreError):
        store.append_fill(f, seq=1, quote_amount=D("1"), fee_quote=D("0"))
    with pytest.raises(StoreError):
        store.append_equity_hwm(D("NaN"))


def test_fill_seq_unique_per_client_id(store):
    f = Fill("c", "BTCUSDT", Side.BUY, D("1"), D("1"), D("0"), "BTC", 1.0)
    store.append_fill(f, seq=1, quote_amount=D("1"), fee_quote=D("0"))
    with pytest.raises(sqlite3.IntegrityError):
        store.append_fill(f, seq=1, quote_amount=D("1"), fee_quote=D("0"))
    t = store.fill_totals("c")
    assert (t.count, t.quantity, t.quote_amount) == (1, D("1"), D("1"))


def test_tx_rolls_back_atomically(store):
    with pytest.raises(RuntimeError):
        with store.tx():
            store.append_signal("s", "BTCUSDT", {})
            with store.tx():
                store.append_error("x", "y")
            raise RuntimeError
    assert store.rows("signals") == [] and store.rows("errors") == []


def test_positions_sod_hwm_losses(store):
    store.append_position_snapshot(Position("BTCUSDT", D("1"), D("100")))
    store.append_position_snapshot(Position("BTCUSDT", D("0"), D("0"), D("-5")), closed_pnl=D("-5"), ts=10.0)
    store.append_position_snapshot(Position("ETHUSDT", D("2"), D("50"), meta={"k": "v"}))
    pos = store.latest_positions()
    assert pos["BTCUSDT"].quantity == 0 and pos["BTCUSDT"].realized_pnl == D("-5")
    assert pos["ETHUSDT"].meta == {"k": "v"}
    assert store.consecutive_losses() == (1, 10.0)
    store.append_position_snapshot(Position("ETHUSDT", D("0"), D("0"), D("-1")), closed_pnl=D("-1"), ts=20.0)
    assert store.consecutive_losses() == (2, 20.0)
    store.append_position_snapshot(Position("ETHUSDT", D("0"), D("0"), D("2")), closed_pnl=D("3"), ts=30.0)
    assert store.consecutive_losses() == (0, None)
    assert store.append_equity_sod("2025-01-01", D("100")) is True
    assert store.append_equity_sod("2025-01-01", D("50")) is False
    assert store.sod_equity("2025-01-01") == D("100") and store.sod_equity("2025-01-02") is None
    for v in ("9", "100", "20"):  # TEXT max would pick "9"
        store.append_equity_hwm(D(v))
    assert store.equity_hwm() == D("100")


def test_survives_reopen(tmp_path):
    p = tmp_path / "a.db"
    s = Store(p)
    i = _intent()
    s.append_intent(i)
    s.close()
    s2 = Store(p)
    assert s2.get_intent(i.client_id).state is S.PROPOSED
    with pytest.raises(sqlite3.DatabaseError):
        s2._conn.execute("DELETE FROM intent_state_log")
    s2.close()
