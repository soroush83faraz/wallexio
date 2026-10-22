"""Append-only SQLite (WAL) audit store.

Invariants:
- Every audit table is append-only, enforced by triggers stored in the database schema (so they
  apply to every connection, not just this class):
    * BEFORE UPDATE / BEFORE DELETE triggers raise, except that the `state` and `updated_at`
      columns of order_intents may change (state changes are compare-and-set, checked against
      LEGAL_TRANSITIONS in Python AND by a trigger, and logged to intent_state_log);
    * BEFORE INSERT triggers raise when the new row's PRIMARY KEY or any UNIQUE key already
      exists. SQLite's REPLACE conflict resolution (INSERT OR REPLACE / REPLACE INTO / upserts)
      deletes the old row WITHOUT firing DELETE triggers when recursive_triggers is off, so
      without these an existing row could be silently rewritten. With them, a key conflict
      aborts before any conflict resolution runs, whatever the OR-clause.
    * PRAGMA recursive_triggers=ON is also set on this connection, as a second layer (REPLACE
      deletes then fire the DELETE triggers too); it is per-connection, the INSERT triggers are not.
  Not covered (by design or impossible in SQLite): appending NEW rows (that is the point of an
  audit log; e.g. a new killswitch_events row is how the switch is disengaged, and it is logged),
  and a writer with raw file access who drops the triggers or edits the file.
- Money/quantity values are Decimal, stored as TEXT (str(Decimal) round-trips exactly).
  Floats are refused at this boundary.
- Every payload / free-text column passes through redact() before it is written.
- Identifier columns (client_id, signal_id, symbol, broker event kind, error source) are NOT
  redacted (that would break idempotency and lookups); they are VALIDATED instead: client_id must
  match ^wb_[0-9a-f]{32}$, the others ^[A-Za-z0-9_.:-]{1,128}$, and must pass redact() unchanged.
  A value that fails raises StoreError before anything is written.
"""
from __future__ import annotations

import dataclasses
import json
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Iterator

from wallexbot.redact import redact
from wallexbot.types import Fill, IntentState, OrderIntent, OrderType, Position, Side

S = IntentState
LEGAL_TRANSITIONS: dict[IntentState, frozenset[IntentState]] = {
    S.PROPOSED: frozenset({S.RISK_APPROVED, S.REJECTED}),
    S.RISK_APPROVED: frozenset({S.SUBMITTED, S.UNKNOWN_PENDING, S.REJECTED}),
    S.SUBMITTED: frozenset({S.PARTIALLY_FILLED, S.FILLED, S.CANCELED, S.REJECTED,
                            S.UNKNOWN_PENDING, S.NEEDS_REVIEW}),
    S.PARTIALLY_FILLED: frozenset({S.FILLED, S.CANCELED, S.UNKNOWN_PENDING, S.NEEDS_REVIEW}),
    S.UNKNOWN_PENDING: frozenset({S.SUBMITTED, S.PARTIALLY_FILLED, S.FILLED, S.CANCELED,
                                  S.REJECTED, S.NEEDS_REVIEW}),
    S.FILLED: frozenset(),
    S.CANCELED: frozenset(),
    S.REJECTED: frozenset(),
    S.NEEDS_REVIEW: frozenset(),
}

APPEND_ONLY_TABLES = ("signals", "risk_decisions", "intent_state_log", "broker_events", "fills",
                      "positions_snapshots", "killswitch_events", "equity_sod", "equity_hwm", "errors",
                      "audit_events")
AUDIT_TABLES = ("signals", "risk_decisions", "order_intents", "intent_state_log", "broker_events",
                "fills", "positions_snapshots", "killswitch_events", "equity_sod", "equity_hwm", "errors",
                "audit_events")
POSITION_LEDGER_KEYS = ("cost_basis", "realized_at_open")
_INTENT_IMMUTABLE = ("client_id", "intent_id", "signal_id", "symbol", "side", "type", "quantity",
                     "price", "stop_price", "reduce_only", "created_at", "payload")
# Every key of every audit table that an insert can conflict on: the rowid (`id` is an alias of
# it; order_intents has an implicit rowid besides its TEXT primary key), the PRIMARY KEY and every
# UNIQUE key. The first entry is always the rowid. A BEFORE INSERT trigger per key aborts an
# insert whose key already exists, which closes the REPLACE-conflict rewrite path.
_UNIQUE_KEYS: dict[str, tuple[tuple[str, ...], ...]] = {
    **{t: (("id",),) for t in AUDIT_TABLES},
    "order_intents": (("rowid",), ("client_id",)),
    "fills": (("id",), ("client_id", "seq")),
    "equity_sod": (("id",), ("day",)),
}

CLIENT_ID_RE = re.compile(r"wb_[0-9a-f]{32}")
IDENT_RE = re.compile(r"[A-Za-z0-9_.:\-]{1,128}")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS signals(id INTEGER PRIMARY KEY, ts REAL NOT NULL, signal_id TEXT NOT NULL,
    symbol TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS risk_decisions(id INTEGER PRIMARY KEY, ts REAL NOT NULL,
    client_id TEXT NOT NULL, approved INTEGER NOT NULL, hard_breach INTEGER NOT NULL,
    reasons TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS order_intents(client_id TEXT PRIMARY KEY, intent_id TEXT NOT NULL,
    signal_id TEXT NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL, type TEXT NOT NULL,
    quantity TEXT NOT NULL, price TEXT, stop_price TEXT, reduce_only INTEGER NOT NULL,
    state TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS intent_state_log(id INTEGER PRIMARY KEY, ts REAL NOT NULL,
    client_id TEXT NOT NULL, from_state TEXT, to_state TEXT NOT NULL, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS broker_events(id INTEGER PRIMARY KEY, ts REAL NOT NULL,
    client_id TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fills(id INTEGER PRIMARY KEY, ts REAL NOT NULL, client_id TEXT NOT NULL,
    seq INTEGER NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL, price TEXT NOT NULL,
    quantity TEXT NOT NULL, quote_amount TEXT NOT NULL, fee TEXT NOT NULL, fee_asset TEXT NOT NULL,
    fee_quote TEXT NOT NULL, UNIQUE(client_id, seq));
CREATE TABLE IF NOT EXISTS positions_snapshots(id INTEGER PRIMARY KEY, ts REAL NOT NULL,
    symbol TEXT NOT NULL, quantity TEXT NOT NULL, avg_price TEXT NOT NULL, realized_pnl TEXT NOT NULL,
    stop_price TEXT, closed_pnl TEXT, cost_basis TEXT, realized_at_open TEXT, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS killswitch_events(id INTEGER PRIMARY KEY, ts REAL NOT NULL,
    engaged INTEGER NOT NULL CHECK (engaged IN (0, 1)), reason TEXT NOT NULL, actor TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS equity_sod(id INTEGER PRIMARY KEY, ts REAL NOT NULL,
    day TEXT NOT NULL UNIQUE, equity TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS equity_hwm(id INTEGER PRIMARY KEY, ts REAL NOT NULL, equity TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS errors(id INTEGER PRIMARY KEY, ts REAL NOT NULL, source TEXT NOT NULL,
    message TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit_events(id INTEGER PRIMARY KEY, ts REAL NOT NULL, actor TEXT NOT NULL,
    kind TEXT NOT NULL, payload TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS audit_events_actor_kind ON audit_events(actor, kind, id);
"""


class StoreError(Exception):
    pass


class IllegalTransition(StoreError):
    pass


class StaleState(StoreError):
    """Compare-and-set failed: the intent is no longer in the expected state."""


@dataclass(frozen=True)
class IntentRecord:
    intent: OrderIntent
    state: IntentState
    created_at: float
    updated_at: float
    seq: int = 0          # insertion order (rowid): a total order across every connection to the DB


@dataclass(frozen=True)
class FillTotals:
    count: int
    quantity: Decimal
    quote_amount: Decimal
    fee: Decimal


@dataclass(frozen=True)
class KillswitchEvent:
    engaged: bool
    reason: str
    actor: str
    ts: float


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, Decimal):
        return str(obj)
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in obj]
    if obj is None or isinstance(obj, (bool, int, str)):
        return obj
    if isinstance(obj, float):
        return repr(obj)
    return str(obj)


def _payload(obj: Any) -> str:
    """JSON-safe conversion first, then redact (so every string is seen), then serialise."""
    return json.dumps(redact(_jsonable(obj if obj is not None else {})), sort_keys=True)


def _text(s: str) -> str:
    return str(redact(str(s)))


def _dec(v: Decimal | None) -> str | None:
    if v is None:
        return None
    if not isinstance(v, Decimal) or not v.is_finite():
        raise StoreError(f"refusing non-Decimal / non-finite money value {v!r}")
    return str(v)


def _undec(s: str | None) -> Decimal | None:
    return None if s is None else Decimal(s)


def _client_id(value: Any) -> str:
    """client_id is derived from a hash (OrderIntent.client_id), so its shape is fully known."""
    if not isinstance(value, str) or CLIENT_ID_RE.fullmatch(value) is None:
        raise StoreError("refusing malformed client_id (expected wb_ + 32 lowercase hex)")
    return value


def _ident(value: Any, what: str) -> str:
    """Identifier columns are stored verbatim (not redacted), so they must be plainly harmless:
    a short token of [A-Za-z0-9_.:-] that redact() leaves unchanged (which also refuses any value
    containing a fragment of a registered secret). The offending value is never echoed."""
    if not isinstance(value, str) or IDENT_RE.fullmatch(value) is None or redact(value) != value:
        raise StoreError(f"refusing malformed {what} (expected 1-128 chars of [A-Za-z0-9_.:-], "
                         "no secret material)")
    return value


def _transition_sql() -> str:
    clauses = []
    for src, dsts in LEGAL_TRANSITIONS.items():
        if dsts:
            lst = ",".join(f"'{d.value}'" for d in sorted(dsts, key=lambda x: x.value))
            clauses.append(f"(OLD.state = '{src.value}' AND NEW.state IN ({lst}))")
    return " OR ".join(clauses)


class Store:
    def __init__(self, path: str | Path, clock=time.time) -> None:
        self.path = str(path)
        self._clock = clock
        self._depth = 0
        self._conn = sqlite3.connect(self.path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA recursive_triggers=ON")  # REPLACE deletes fire DELETE triggers
        self._init_schema()

    # ---------------------------------------------------------------- infrastructure
    def _init_schema(self) -> None:
        stmts = [_SCHEMA]
        for t in APPEND_ONLY_TABLES:
            stmts.append(f"CREATE TRIGGER IF NOT EXISTS {t}_no_update BEFORE UPDATE ON {t} "
                         f"BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;")
            stmts.append(f"CREATE TRIGGER IF NOT EXISTS {t}_no_delete BEFORE DELETE ON {t} "
                         f"BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;")
        changed = " OR ".join(f"NEW.{c} IS NOT OLD.{c}" for c in _INTENT_IMMUTABLE)
        stmts.append("CREATE TRIGGER IF NOT EXISTS order_intents_no_delete BEFORE DELETE ON order_intents "
                     "BEGIN SELECT RAISE(ABORT, 'order_intents is append-only'); END;")
        stmts.append("CREATE TRIGGER IF NOT EXISTS order_intents_immutable BEFORE UPDATE ON order_intents "
                     f"WHEN {changed} BEGIN SELECT RAISE(ABORT, 'order_intents: only state/updated_at "
                     "may change'); END;")
        stmts.append("CREATE TRIGGER IF NOT EXISTS order_intents_transition BEFORE UPDATE OF state ON "
                     f"order_intents WHEN NEW.state IS NOT OLD.state AND NOT ({_transition_sql()}) "
                     "BEGIN SELECT RAISE(ABORT, 'illegal intent state transition'); END;")
        # The implicit rowid of order_intents is not in _INTENT_IMMUTABLE; changing it with
        # UPDATE OR REPLACE could delete another intent row through a rowid conflict.
        stmts.append("CREATE TRIGGER IF NOT EXISTS order_intents_rowid_immutable BEFORE UPDATE ON "
                     "order_intents WHEN NEW.rowid IS NOT OLD.rowid BEGIN SELECT RAISE(ABORT, "
                     "'order_intents: rowid may not change'); END;")
        # REPLACE / upsert guard. BEFORE INSERT triggers run before SQLite resolves a key conflict,
        # so aborting here means the existing row is never deleted or rewritten.
        for t, keys in _UNIQUE_KEYS.items():
            for cols in keys:
                match = " AND ".join(f"{c} = NEW.{c}" for c in cols)
                stmts.append(f"CREATE TRIGGER IF NOT EXISTS {t}_no_replace_{'_'.join(cols)} BEFORE INSERT "
                             f"ON {t} WHEN EXISTS (SELECT 1 FROM {t} WHERE {match}) BEGIN SELECT "
                             f"RAISE(ABORT, '{t} is append-only: {'/'.join(cols)} already exists'); END;")
            # In a BEFORE INSERT trigger an auto-assigned rowid reads as -1, so a row stored with a
            # negative rowid could not be told apart from a fresh insert by the guard above.
            # Auto-assigned rowids are always positive: refuse explicit negative ones.
            rid = keys[0][0]
            stmts.append(f"CREATE TRIGGER IF NOT EXISTS {t}_no_negative_rowid AFTER INSERT ON {t} "
                         f"WHEN NEW.{rid} < 0 BEGIN SELECT RAISE(ABORT, '{t}: negative rowid'); END;")
        self._conn.executescript("\n".join(stmts))

    def close(self) -> None:
        self._conn.close()

    def now(self) -> float:
        return float(self._clock())

    def _ts(self, ts: float | None) -> float:
        return self.now() if ts is None else float(ts)

    @contextmanager
    def tx(self) -> Iterator[None]:
        """Atomic block. Nested calls use savepoints.

        A failing COMMIT (or RELEASE) is never left half-open: SQLite does not always roll back
        by itself (e.g. an I/O error on COMMIT), and a connection stuck in a write transaction
        would refuse every later BEGIN ('cannot start a transaction within a transaction') until
        a restart. On any COMMIT exception the transaction is rolled back (secondary errors
        ignored), the nesting depth is reset, and the original exception is re-raised, so a
        retry of the same work starts clean."""
        if self._depth == 0:
            self._conn.execute("BEGIN IMMEDIATE")
            begin, commit, rollback = None, "COMMIT", "ROLLBACK"
        else:
            sp = f"sp{self._depth}"
            self._conn.execute(f"SAVEPOINT {sp}")
            begin, commit, rollback = sp, f"RELEASE {sp}", f"ROLLBACK TO {sp}"
        self._depth += 1
        try:
            yield
        except BaseException:
            self._depth -= 1
            if self._conn.in_transaction:
                self._conn.execute(rollback)
                if begin is not None:
                    self._conn.execute(f"RELEASE {begin}")
            raise
        self._depth -= 1
        try:
            self._conn.execute(commit)
        except BaseException:
            undo = ("ROLLBACK",) if begin is None else (rollback, f"RELEASE {begin}")
            for sql in undo:
                try:
                    self._conn.execute(sql)
                except Exception:
                    pass
            if begin is None:
                self._depth = 0
            raise

    def _insert(self, table: str, row: dict[str, Any]) -> int:
        cols = ",".join(row)
        qs = ",".join("?" for _ in row)
        cur = self._conn.execute(f"INSERT INTO {table}({cols}) VALUES ({qs})", tuple(row.values()))
        return int(cur.lastrowid)

    def rows(self, table: str) -> list[dict[str, Any]]:
        if table not in AUDIT_TABLES:
            raise StoreError(f"unknown table {table!r}")
        return [dict(r) for r in self._conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]

    # ---------------------------------------------------------------- append-only writers
    def append_signal(self, signal_id: str, symbol: str, payload: dict | None = None,
                      ts: float | None = None) -> int:
        return self._insert("signals", {"ts": self._ts(ts), "signal_id": _ident(signal_id, "signal_id"),
                                        "symbol": _ident(symbol, "symbol"), "payload": _payload(payload)})

    def append_risk_decision(self, client_id: str, decision: Any, ts: float | None = None) -> int:
        """`decision` is a risk.engine.RiskDecision (approved, reasons, hard_breach)."""
        return self._insert("risk_decisions", {
            "ts": self._ts(ts), "client_id": _client_id(client_id), "approved": int(bool(decision.approved)),
            "hard_breach": int(bool(getattr(decision, "hard_breach", False))),
            "reasons": _payload(list(decision.reasons)), "payload": _payload(decision)})

    def append_broker_event(self, client_id: str, kind: str, payload: Any = None,
                            ts: float | None = None) -> int:
        return self._insert("broker_events", {"ts": self._ts(ts), "client_id": _client_id(client_id),
                                              "kind": _ident(kind, "kind"), "payload": _payload(payload)})

    def append_fill(self, fill: Fill, *, seq: int, quote_amount: Decimal, fee_quote: Decimal) -> int:
        return self._insert("fills", {
            "ts": fill.ts, "client_id": _client_id(fill.client_id), "seq": seq,
            "symbol": _ident(fill.symbol, "symbol"), "side": fill.side.value, "price": _dec(fill.price),
            "quantity": _dec(fill.quantity), "quote_amount": _dec(quote_amount), "fee": _dec(fill.fee),
            "fee_asset": _ident(fill.fee_asset, "fee_asset"),
            "fee_quote": _dec(fee_quote)})

    def append_position_snapshot(self, pos: Position, *, closed_pnl: Decimal | None = None,
                                 ts: float | None = None) -> int:
        """Ledger keys in pos.meta (POSITION_LEDGER_KEYS) are stored as Decimal TEXT columns, not in the
        redacted payload."""
        meta = {k: v for k, v in pos.meta.items() if k not in POSITION_LEDGER_KEYS}
        return self._insert("positions_snapshots", {
            "ts": self._ts(ts), "symbol": _ident(pos.symbol, "symbol"), "quantity": _dec(pos.quantity),
            "avg_price": _dec(pos.avg_price), "realized_pnl": _dec(pos.realized_pnl),
            "stop_price": _dec(pos.stop_price), "closed_pnl": _dec(closed_pnl),
            "cost_basis": _dec(pos.meta.get("cost_basis")),
            "realized_at_open": _dec(pos.meta.get("realized_at_open")), "payload": _payload(meta)})

    def append_killswitch_event(self, engaged: bool, reason: str, actor: str,
                                ts: float | None = None) -> int:
        return self._insert("killswitch_events", {"ts": self._ts(ts), "engaged": int(bool(engaged)),
                                                  "reason": _text(reason), "actor": _text(actor)})

    def append_equity_sod(self, day: str, equity: Decimal, ts: float | None = None) -> bool:
        """First write for a UTC day wins; later writes for the same day are ignored (False).

        Check-then-insert inside one IMMEDIATE transaction (not INSERT OR IGNORE: the
        no-replace trigger aborts a duplicate key whatever the OR-clause)."""
        day = _ident(day, "day")
        value = _dec(equity)
        with self.tx():
            if self._conn.execute("SELECT 1 FROM equity_sod WHERE day=?", (day,)).fetchone():
                return False
            self._insert("equity_sod", {"ts": self._ts(ts), "day": day, "equity": value})
            return True

    def append_equity_hwm(self, equity: Decimal, ts: float | None = None) -> int:
        return self._insert("equity_hwm", {"ts": self._ts(ts), "equity": _dec(equity)})

    def append_error(self, source: str, message: str, payload: Any = None,
                     ts: float | None = None) -> int:
        return self._insert("errors", {"ts": self._ts(ts), "source": _ident(source, "source"),
                                       "message": _text(message), "payload": _payload(payload)})

    def append_audit_event(self, actor: str, kind: str, payload: Any = None,
                           ts: float | None = None) -> int:
        """Generic append-only decision / health record (guardian decisions, component heartbeats)."""
        return self._insert("audit_events", {"ts": self._ts(ts), "actor": _ident(actor, "actor"),
                                             "kind": _ident(kind, "kind"), "payload": _payload(payload)})

    def heartbeat(self, component: str = "strategy", ts: float | None = None) -> int:
        """A long-running loop proves it is alive; the guardian reads latest_event_ts(component, 'heartbeat')."""
        return self.append_audit_event(component, "heartbeat", None, ts=ts)

    # ---------------------------------------------------------------- intents
    def append_intent(self, intent: OrderIntent, payload: Any = None, ts: float | None = None) -> bool:
        """Insert in PROPOSED. Returns False (and writes nothing) if client_id already exists.

        Idempotency is an explicit existence check inside one IMMEDIATE transaction followed by a
        plain INSERT; it never relies on OR IGNORE / OR REPLACE conflict semantics. Identifiers
        are validated first; an invalid one raises StoreError and nothing is written."""
        cid = _client_id(intent.client_id)
        signal_id = _ident(intent.signal_id, "signal_id")
        symbol = _ident(intent.symbol, "symbol")
        ts = self._ts(ts)
        with self.tx():
            if self._conn.execute("SELECT 1 FROM order_intents WHERE client_id=?", (cid,)).fetchone():
                return False
            self._conn.execute(
                "INSERT INTO order_intents(client_id, intent_id, signal_id, symbol, side, type,"
                " quantity, price, stop_price, reduce_only, state, created_at, updated_at, payload)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, intent.intent_id, signal_id, symbol, intent.side.value,
                 intent.type.value, _dec(intent.quantity), _dec(intent.price), _dec(intent.stop_price),
                 int(intent.reduce_only), IntentState.PROPOSED.value, ts, ts, _payload(payload)))
            self._insert("intent_state_log", {"ts": ts, "client_id": intent.client_id, "from_state": None,
                                              "to_state": IntentState.PROPOSED.value, "reason": "created"})
            return True

    def _record(self, r: sqlite3.Row) -> IntentRecord:
        intent = OrderIntent(
            symbol=r["symbol"], side=Side(r["side"]), type=OrderType(r["type"]),
            quantity=Decimal(r["quantity"]), price=_undec(r["price"]), signal_id=r["signal_id"],
            reduce_only=bool(r["reduce_only"]), stop_price=_undec(r["stop_price"]))
        if intent.client_id != r["client_id"]:
            raise StoreError(f"intent row {r['client_id']} does not reproduce its client_id")
        return IntentRecord(intent, IntentState(r["state"]), r["created_at"], r["updated_at"], int(r["seq"]))

    def get_intent(self, client_id: str) -> IntentRecord | None:
        r = self._conn.execute("SELECT rowid AS seq, * FROM order_intents WHERE client_id=?", (client_id,)).fetchone()
        return None if r is None else self._record(r)

    def intents_in_states(self, states: Iterable[IntentState]) -> list[IntentRecord]:
        vals = [s.value for s in states]
        if not vals:
            return []
        q = f"SELECT rowid AS seq, * FROM order_intents WHERE state IN ({','.join('?' for _ in vals)}) ORDER BY created_at, rowid"
        return [self._record(r) for r in self._conn.execute(q, vals)]

    def set_intent_state(self, client_id: str, new: IntentState, *, expected: IntentState | None = None,
                         reason: str = "", ts: float | None = None) -> IntentState:
        """Compare-and-set the intent state. Returns the previous state.
        Raises IllegalTransition (not in LEGAL_TRANSITIONS) or StaleState (expected mismatch)."""
        ts = self._ts(ts)
        with self.tx():
            rec = self.get_intent(client_id)
            if rec is None:
                raise StoreError(f"unknown intent {client_id}")
            old = rec.state
            if expected is not None and old is not expected:
                raise StaleState(f"{client_id}: expected {expected.value}, found {old.value}")
            if new is old:
                return old
            if new not in LEGAL_TRANSITIONS[old]:
                raise IllegalTransition(f"{client_id}: {old.value} -> {new.value}")
            cur = self._conn.execute(
                "UPDATE order_intents SET state=?, updated_at=? WHERE client_id=? AND state=?",
                (new.value, ts, client_id, old.value))
            if cur.rowcount != 1:
                raise StaleState(f"{client_id}: concurrent state change")
            self._insert("intent_state_log", {"ts": ts, "client_id": client_id, "from_state": old.value,
                                              "to_state": new.value, "reason": _text(reason)})
            return old

    # ---------------------------------------------------------------- readers
    def fill_totals(self, client_id: str) -> FillTotals:
        rows = self._conn.execute("SELECT quantity, quote_amount, fee FROM fills WHERE client_id=?",
                                  (client_id,)).fetchall()
        zero = Decimal(0)
        return FillTotals(len(rows), sum((Decimal(r[0]) for r in rows), zero),
                          sum((Decimal(r[1]) for r in rows), zero), sum((Decimal(r[2]) for r in rows), zero))

    def latest_positions(self) -> dict[str, Position]:
        q = ("SELECT p.* FROM positions_snapshots p JOIN (SELECT symbol, MAX(id) AS id FROM "
             "positions_snapshots GROUP BY symbol) m ON p.id = m.id")
        out = {}
        for r in self._conn.execute(q):
            meta = json.loads(r["payload"])
            meta.update({k: Decimal(r[k]) for k in POSITION_LEDGER_KEYS if r[k] is not None})
            out[r["symbol"]] = Position(r["symbol"], Decimal(r["quantity"]), Decimal(r["avg_price"]),
                                        Decimal(r["realized_pnl"]), _undec(r["stop_price"]), meta)
        return out

    def latest_killswitch(self) -> KillswitchEvent | None:
        r = self._conn.execute("SELECT * FROM killswitch_events ORDER BY id DESC LIMIT 1").fetchone()
        if r is None:
            return None
        if r["engaged"] not in (0, 1):
            raise StoreError(f"corrupt killswitch row {r['id']}")
        return KillswitchEvent(bool(r["engaged"]), r["reason"], r["actor"], r["ts"])

    def latest_event_ts(self, actor: str, kind: str) -> float | None:
        r = self._conn.execute("SELECT ts FROM audit_events WHERE actor=? AND kind=? ORDER BY id DESC LIMIT 1",
                               (actor, kind)).fetchone()
        return None if r is None else float(r[0])

    def audit_events(self, actor: str | None = None, kind: str | None = None) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM audit_events WHERE 1=1", []
        if actor is not None:
            q, args = q + " AND actor=?", [*args, actor]
        if kind is not None:
            q, args = q + " AND kind=?", [*args, kind]
        out = []
        for r in self._conn.execute(q + " ORDER BY id", args):
            d = dict(r)
            d["payload"] = json.loads(d["payload"])
            out.append(d)
        return out

    def sod_equity(self, day: str) -> Decimal | None:
        r = self._conn.execute("SELECT equity FROM equity_sod WHERE day=?", (day,)).fetchone()
        return None if r is None else Decimal(r[0])

    def equity_hwm(self) -> Decimal | None:
        vals = [Decimal(r[0]) for r in self._conn.execute("SELECT equity FROM equity_hwm")]
        return max(vals) if vals else None

    def consecutive_losses(self) -> tuple[int, float | None]:
        """(number of most recent closed round trips that lost money, ts of the latest loss)."""
        n, last_ts = 0, None
        for r in self._conn.execute("SELECT ts, closed_pnl FROM positions_snapshots WHERE closed_pnl "
                                    "IS NOT NULL ORDER BY id DESC"):
            if Decimal(r["closed_pnl"]) >= 0:
                break
            n += 1
            last_ts = r["ts"] if last_ts is None else last_ts
        return n, last_ts
