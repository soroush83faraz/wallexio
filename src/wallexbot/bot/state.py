"""Persisted position-management state of the trading loop.

The stop of an open position must survive a restart (the known flaw of the earlier design was that
it lived only in memory). Every change is APPENDED to the store's append-only `audit_events` table
(actor "bot"); readers replay the rows in id order and the latest row wins. Nothing is updated in
place, so the history of every stop move is kept for the decision log.

Event kinds (payload keys are plain JSON; Decimals as strings):
- position_state   full TrackedPosition (latest per symbol wins)
- position_closed  {symbol, key, reason, pnl}: drops the symbol's state if `key` matches
- entry_plan       EntryPlan, written BEFORE the entry intent is handed to the OMS. A signal_id
                   that has a plan is never planned again (bar reprocessing / restart idempotency).
- entry_resolved   {signal_id, outcome}: the plan's intent reached a terminal state
- cooldown_state   CooldownTracker.to_dict() (latest wins)
- bar_done         {bars: {symbol: bar_ts}}: last closed 15m bar handled per symbol
Decision rows (abstain, signal, entry_*, exit_*, advisor_verdict, ...) use the same actor; they are
for the decision log only and are never read back as state.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

from wallexbot.strategy.contract import PositionState
from wallexbot.types import IntentState

ACTOR = "bot"
K_POSITION = "position_state"
K_CLOSED = "position_closed"
K_PLAN = "entry_plan"
K_PLAN_DONE = "entry_resolved"
K_COOLDOWN = "cooldown_state"
K_BAR = "bar_done"
EXIT_PREFIX = "x"                        # exit intents: signal_id = x:<position key>:<n>
_ALL_STATES = frozenset(IntentState)


def _d(v: Any) -> Decimal | None:
    return None if v is None else Decimal(str(v))


def _plain(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return str(obj)
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return _plain(dataclasses.asdict(obj))
    if isinstance(obj, dict):
        return {str(k): _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    return obj


def exit_signal_id(key: str, n: int) -> str:
    return f"{EXIT_PREFIX}:{key}:{n}"


def exit_prefix(key: str) -> str:
    return f"{EXIT_PREFIX}:{key}:"


@dataclass(frozen=True)
class ExitRequest:
    """Sell down to `target_qty` (0 = flat). Kept until the ledger reaches it; attempts placed since
    `since` decide LIMIT (first try) vs MARKET (after a LIMIT attempt ended unfilled)."""
    reason: str
    target_qty: Decimal
    since: float
    stop_hit: bool = False

    @staticmethod
    def from_payload(p: Mapping[str, Any] | None) -> "ExitRequest | None":
        if not p:
            return None
        return ExitRequest(p["reason"], Decimal(p["target_qty"]), float(p["since"]), bool(p.get("stop_hit", False)))


@dataclass(frozen=True)
class TrackedPosition:
    symbol: str
    key: str                              # entry signal_id, or orph_<symbol>_<ts> for an orphan
    origin: str                           # "signal" | "orphan"
    entry_bar_ts: int                     # open time of the bar during which the entry filled
    entry_price: Decimal
    initial_stop: Decimal
    current_stop: Decimal
    breakout_level: Decimal
    highest_high_since_entry: Decimal
    bars_held: int = 0
    last_bar_ts: int | None = None        # last closed bar folded into this state
    partial_taken: bool = False
    needs_review: bool = False
    review_reason: str = ""
    atr: Decimal | None = None
    realized_at_open: Decimal = Decimal(0)   # ledger realized P&L when the position was adopted
    exit_request: ExitRequest | None = None
    opened_ts: float = 0.0

    def contract_state(self, quantity: Decimal) -> PositionState:
        return PositionState(symbol=self.symbol, entry_price=self.entry_price, entry_bar_ts=self.entry_bar_ts,
                             quantity=quantity, initial_stop=self.initial_stop, breakout_level=self.breakout_level,
                             highest_high_since_entry=self.highest_high_since_entry, bars_held=self.bars_held,
                             partial_taken=self.partial_taken, current_stop=self.current_stop)

    def to_payload(self) -> dict:
        return _plain(self)

    @staticmethod
    def from_payload(p: Mapping[str, Any]) -> "TrackedPosition":
        return TrackedPosition(
            symbol=p["symbol"], key=p["key"], origin=p["origin"], entry_bar_ts=int(p["entry_bar_ts"]),
            entry_price=Decimal(p["entry_price"]), initial_stop=Decimal(p["initial_stop"]),
            current_stop=Decimal(p["current_stop"]), breakout_level=Decimal(p["breakout_level"]),
            highest_high_since_entry=Decimal(p["highest_high_since_entry"]), bars_held=int(p["bars_held"]),
            last_bar_ts=None if p.get("last_bar_ts") is None else int(p["last_bar_ts"]),
            partial_taken=bool(p.get("partial_taken", False)), needs_review=bool(p.get("needs_review", False)),
            review_reason=str(p.get("review_reason", "")), atr=_d(p.get("atr")),
            realized_at_open=Decimal(p.get("realized_at_open", "0")),
            exit_request=ExitRequest.from_payload(p.get("exit_request")), opened_ts=float(p.get("opened_ts", 0.0)))


@dataclass(frozen=True)
class EntryPlan:
    signal_id: str
    symbol: str
    client_id: str
    signal_bar_ts: int
    entry_bar_ts: int
    entry_ref_price: Decimal
    stop_price: Decimal
    breakout_level: Decimal
    atr: Decimal
    score: Decimal
    ts: float

    def to_payload(self) -> dict:
        return _plain(self)

    @staticmethod
    def from_payload(p: Mapping[str, Any]) -> "EntryPlan":
        return EntryPlan(p["signal_id"], p["symbol"], p["client_id"], int(p["signal_bar_ts"]),
                         int(p["entry_bar_ts"]), Decimal(p["entry_ref_price"]), Decimal(p["stop_price"]),
                         Decimal(p["breakout_level"]), Decimal(p["atr"]), Decimal(p["score"]), float(p["ts"]))


@dataclass
class LoadedState:
    positions: dict[str, TrackedPosition]
    open_plans: dict[str, EntryPlan]          # signal_id -> plan not yet resolved
    planned: set[str]                         # every signal_id ever planned
    cooldown: dict | None
    bars_done: dict[str, int]


class BotState:
    """Append-only persistence of the bot's state through Store.append_audit_event."""

    def __init__(self, store) -> None:
        self.store = store

    def _append(self, kind: str, payload: Any, now: float) -> None:
        self.store.append_audit_event(ACTOR, kind, _plain(payload), ts=now)

    # ---- writers
    def save_position(self, tp: TrackedPosition, now: float) -> None:
        self._append(K_POSITION, tp.to_payload(), now)

    def close_position(self, tp: TrackedPosition, reason: str, pnl: Decimal | None, now: float) -> None:
        self._append(K_CLOSED, {"symbol": tp.symbol, "key": tp.key, "reason": reason, "pnl": pnl,
                                "signal_id": tp.key}, now)

    def add_plan(self, plan: EntryPlan, now: float) -> None:
        self._append(K_PLAN, plan.to_payload(), now)

    def resolve_plan(self, plan: EntryPlan, outcome: str, now: float) -> None:
        self._append(K_PLAN_DONE, {"signal_id": plan.signal_id, "symbol": plan.symbol,
                                   "client_id": plan.client_id, "outcome": outcome}, now)

    def save_cooldown(self, data: dict, now: float) -> None:
        self._append(K_COOLDOWN, data, now)

    def mark_bars(self, bars: Mapping[str, int], now: float) -> None:
        self._append(K_BAR, {"bars": dict(bars)}, now)

    def decision(self, kind: str, payload: Any, now: float) -> None:
        self._append(kind, payload, now)

    # ---- reader
    def load(self) -> LoadedState:
        positions: dict[str, TrackedPosition] = {}
        plans: dict[str, EntryPlan] = {}
        planned: set[str] = set()
        cooldown: dict | None = None
        bars: dict[str, int] = {}
        for row in self.store.audit_events(actor=ACTOR):
            kind, p = row["kind"], row["payload"]
            if kind == K_POSITION:
                tp = TrackedPosition.from_payload(p)
                positions[tp.symbol] = tp
            elif kind == K_CLOSED:
                cur = positions.get(p["symbol"])
                if cur is not None and cur.key == p["key"]:
                    del positions[p["symbol"]]
            elif kind == K_PLAN:
                plan = EntryPlan.from_payload(p)
                plans[plan.signal_id] = plan
                planned.add(plan.signal_id)
            elif kind == K_PLAN_DONE:
                plans.pop(p["signal_id"], None)
            elif kind == K_COOLDOWN:
                cooldown = p
            elif kind == K_BAR:
                bars.update({s: int(t) for s, t in p["bars"].items()})
        return LoadedState(positions, plans, planned, cooldown, bars)


def decision_log(store, signal_id: str) -> dict[str, list]:
    """Everything recorded about one trade, rebuilt from the store: the signal rows, the bot's
    decision/state events, the entry and exit intents (exit signal_ids are x:<signal_id>:<n>),
    their state transitions, risk decisions, broker events and fills."""
    prefix = exit_prefix(signal_id)
    intents = [r for r in store.intents_in_states(_ALL_STATES)
               if r.intent.signal_id == signal_id or r.intent.signal_id.startswith(prefix)]
    cids = {r.intent.client_id for r in intents}

    def mine(p: Any) -> bool:
        return isinstance(p, dict) and (p.get("signal_id") == signal_id or p.get("key") == signal_id
                                        or p.get("client_id") in cids)

    return {
        "signals": [r for r in store.rows("signals") if r["signal_id"] == signal_id],
        "events": [e for e in store.audit_events(actor=ACTOR) if mine(e["payload"])],
        "intents": intents,
        "transitions": [r for r in store.rows("intent_state_log") if r["client_id"] in cids],
        "risk_decisions": [r for r in store.rows("risk_decisions") if r["client_id"] in cids],
        "broker_events": [r for r in store.rows("broker_events") if r["client_id"] in cids],
        "fills": [r for r in store.rows("fills") if r["client_id"] in cids],
    }
