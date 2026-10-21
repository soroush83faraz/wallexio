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
    store.append_audit_event("pytest", "test", {"k": "v"})
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
    # valid client_id, so the refusal below is about the float price and nothing else
    f = Fill(_intent().client_id, "BTCUSDT", Side.BUY, 1.5, D("1"), D("0"), "BTC", 1.0)
    with pytest.raises(StoreError, match="non-Decimal"):
        store.append_fill(f, seq=1, quote_amount=D("1"), fee_quote=D("0"))
    with pytest.raises(StoreError):
        store.append_equity_hwm(D("NaN"))


def test_fill_seq_unique_per_client_id(store):
    cid = _intent().client_id  # was the literal "c", which the Store now refuses as a client_id
    f = Fill(cid, "BTCUSDT", Side.BUY, D("1"), D("1"), D("0"), "BTC", 1.0)
    store.append_fill(f, seq=1, quote_amount=D("1"), fee_quote=D("0"))
    with pytest.raises(sqlite3.IntegrityError):
        store.append_fill(f, seq=1, quote_amount=D("1"), fee_quote=D("0"))
    t = store.fill_totals(cid)
    assert (t.count, t.quantity, t.quote_amount) == (1, D("1"), D("1"))


# ------------------------------------------------------------------ REPLACE-conflict bypass
def _snapshot(store):
    return {t: store.rows(t) for t in store_mod.AUDIT_TABLES}


def _replace_sql(store, table, verb):
    """A REPLACE statement that re-inserts the first row of `table` with altered content, keeping
    every key column so it conflicts with the existing row."""
    row = store.rows(table)[0]
    if table == "order_intents":
        row["state"] = "filled"           # e.g. fake a terminal state
    elif table == "killswitch_events":
        row["engaged"] = 0                # e.g. disengage the kill switch
    else:
        row["ts"] = row["ts"] + 1000.0
    cols = ",".join(row)
    qs = ",".join("?" for _ in row)
    return f"{verb} {table}({cols}) VALUES ({qs})", tuple(row.values())


@pytest.mark.parametrize("verb", ["INSERT OR REPLACE INTO", "REPLACE INTO", "INSERT OR IGNORE INTO",
                                  "INSERT INTO"])
@pytest.mark.parametrize("table", store_mod.AUDIT_TABLES)
def test_key_conflicting_insert_rejected_on_every_audit_table(store, table, verb):
    _populate(store)
    before = _snapshot(store)
    sql, args = _replace_sql(store, table, verb)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._conn.execute(sql, args)
    assert _snapshot(store) == before


@pytest.mark.parametrize("table,cols,vals", [
    ("fills", "(ts, client_id, seq, symbol, side, price, quantity, quote_amount, fee, fee_asset, "
              "fee_quote)", "(9.0, :cid, 1, 'BTCUSDT', 'SELL', '9', '9', '9', '0', 'BTC', '0')"),
    ("equity_sod", "(ts, day, equity)", "(9.0, '2025-01-01', '1')"),
])
def test_replace_on_unique_non_pk_key_rejected(store, table, cols, vals):
    """REPLACE through a UNIQUE (non-primary) key, with a fresh rowid, is refused as well."""
    i = _populate(store)
    before = _snapshot(store)
    for verb in ("INSERT OR REPLACE INTO", "REPLACE INTO"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            store._conn.execute(f"{verb} {table}{cols} VALUES {vals}", {"cid": i.client_id})
    assert _snapshot(store) == before


def test_order_intents_rowid_replace_and_rowid_update_rejected(store):
    """order_intents has an implicit rowid besides its TEXT key: neither a REPLACE through it nor
    an UPDATE OR REPLACE that moves it onto another row may remove an intent."""
    a = _populate(store)
    b = _intent(qty="0.020", signal="sig2")
    store.append_intent(b)
    before = _snapshot(store)
    rowid_a = store._conn.execute("SELECT rowid FROM order_intents WHERE client_id=?",
                                  (a.client_id,)).fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._conn.execute(
            "INSERT OR REPLACE INTO order_intents(rowid, client_id, intent_id, signal_id, symbol, side, "
            "type, quantity, reduce_only, state, created_at, updated_at, payload) "
            "VALUES (?, 'wb_00000000000000000000000000000000', 'x', 'x', 'BTCUSDT', 'BUY', 'LIMIT', '1', "
            "0, 'filled', 0, 0, '{}')", (rowid_a,))
    with pytest.raises(sqlite3.IntegrityError, match="rowid"):
        store._conn.execute("UPDATE OR REPLACE order_intents SET rowid=? WHERE client_id=?",
                            (rowid_a, b.client_id))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._conn.execute("INSERT INTO order_intents(client_id, intent_id, signal_id, symbol, side, type, "
                            "quantity, reduce_only, state, created_at, updated_at, payload) VALUES "
                            "(?, 'x', 'x', 'BTCUSDT', 'BUY', 'LIMIT', '1', 0, 'proposed', 0, 0, '{}') "
                            "ON CONFLICT(client_id) DO UPDATE SET state='filled'", (a.client_id,))
    assert _snapshot(store) == before


def test_killswitch_cannot_be_disengaged_by_rewriting_history(store):
    store.append_killswitch_event(True, "drawdown", "risk")
    with pytest.raises(sqlite3.IntegrityError):
        store._conn.execute("REPLACE INTO killswitch_events(id, ts, engaged, reason, actor) "
                            "VALUES (1, 0, 0, 'x', 'x')")
    with pytest.raises(sqlite3.IntegrityError, match="negative rowid"):
        store._conn.execute("INSERT INTO killswitch_events(id, ts, engaged, reason, actor) "
                            "VALUES (-1, 0, 0, 'x', 'x')")
    assert store.latest_killswitch().engaged is True


def test_replace_guard_is_in_the_schema_not_the_connection(tmp_path):
    """A second, plain sqlite3 connection (recursive_triggers OFF) is refused too."""
    p = tmp_path / "a.db"
    s = Store(p)
    s.append_killswitch_event(True, "drawdown", "risk")
    s.close()
    raw = sqlite3.connect(p)
    assert raw.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
    with pytest.raises(sqlite3.IntegrityError):
        raw.execute("INSERT OR REPLACE INTO killswitch_events(id, ts, engaged, reason, actor) "
                    "VALUES (1, 0, 0, 'x', 'x')")
    raw.close()
    s2 = Store(p)
    assert s2.latest_killswitch().engaged is True
    s2.close()


def test_append_intent_false_on_existing_client_id_without_replace_semantics(store):
    i = _intent()
    assert store.append_intent(i) is True
    before = _snapshot(store)
    assert store.append_intent(i, payload={"different": "payload"}) is False
    assert store.append_intent(_intent(qty="0.0100000")) is False   # same value, other scale
    assert _snapshot(store) == before


def test_normal_appends_still_work_after_guard(store):
    _populate(store)
    row_id = store.append_error("test", "again")
    assert row_id > 1 and len(store.rows("errors")) == 2


# ------------------------------------------------------------------ identifier validation
BAD_IDENTS = ["api_key=SECRETVALUE", "Bearer abcdef", "a b", "", "x" * 129, "line\nbreak", "ok\n",
              "a/b", "a+b==", "naïve", "semi;colon", "quote'", None, 123]


@pytest.mark.parametrize("bad", BAD_IDENTS)
def test_identifier_columns_refuse_odd_values_and_write_nothing(store, bad):
    i = _intent()
    good_cid = i.client_id
    calls = [
        lambda: store.append_signal(bad, "BTCUSDT", {}),
        lambda: store.append_signal("sig", bad, {}),
        lambda: store.append_broker_event(good_cid, bad, {}),
        lambda: store.append_error(bad, "boom"),
        lambda: store.append_intent(OrderIntent("BTCUSDT", Side.BUY, OrderType.LIMIT, D("1"), D("1"), bad)),
        lambda: store.append_fill(Fill(good_cid, "BTCUSDT", Side.BUY, D("1"), D("1"), D("0"), bad, 1.0),
                                  seq=1, quote_amount=D("1"), fee_quote=D("0")),
        lambda: store.append_position_snapshot(Position(bad, D("1"), D("1"))),
    ]
    before = _snapshot(store)
    for call in calls:
        with pytest.raises(StoreError, match="malformed"):
            call()
    assert _snapshot(store) == before


BAD_CIDS = ["c", "wb_" + "A" * 32, "wb_" + "0" * 31, "wb_" + "0" * 33, "wb_" + "0" * 32 + "\n",
            "WB_" + "0" * 32, "api_key=SECRET", None]


@pytest.mark.parametrize("bad", BAD_CIDS)
def test_client_id_columns_refuse_malformed_and_write_nothing(store, bad):
    calls = [
        lambda: store.append_broker_event(bad, "submit_ack", {}),
        lambda: store.append_risk_decision(bad, RiskDecision(True, ())),
        lambda: store.append_fill(Fill(bad, "BTCUSDT", Side.BUY, D("1"), D("1"), D("0"), "BTC", 1.0),
                                  seq=1, quote_amount=D("1"), fee_quote=D("0")),
    ]
    before = _snapshot(store)
    for call in calls:
        with pytest.raises(StoreError, match="malformed client_id"):
            call()
    assert _snapshot(store) == before


def test_identifier_carrying_registered_secret_fragment_refused(store, monkeypatch):
    """A pattern-valid identifier that contains secret material (redact() would change it) is
    refused rather than stored verbatim."""
    monkeypatch.setattr(store_mod, "redact", lambda v: v.replace("hunter2key", "[REDACTED]")
                        if isinstance(v, str) else v)
    with pytest.raises(StoreError, match="malformed signal_id"):
        store.append_signal("sig-hunter2key-1", "BTCUSDT", {})
    with pytest.raises(StoreError, match="malformed source"):
        store.append_error("src.hunter2key", "boom")
    assert store.rows("signals") == [] and store.rows("errors") == []


def test_valid_identifiers_accepted(store):
    i = _intent(signal="scripted-breakout-1.exit:v2")
    assert store.append_intent(i) is True
    store.append_signal("scripted-breakout-1.exit:v2", "BTCUSDT", {})
    store.append_broker_event(i.client_id, "submit_ack", {})
    store.append_error("oms.reconcile", "boom")
    assert len(store.rows("signals")) == len(store.rows("broker_events")) == len(store.rows("errors")) == 1


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
