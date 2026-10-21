"""Order management system. OMS.submit() is the ONLY code path that calls broker.submit_order.

Invariants:
- At most one broker submit per client_id, ever. An intent that already exists in the store is
  never sent again; an intent found in RISK_APPROVED (possible crash mid-submit) is moved to
  UNKNOWN_PENDING and reconciled, never resent.
- Any ambiguous broker outcome (timeout, unexpected error, duplicate client_id) => UNKNOWN_PENDING;
  only reconcile() resolves it, by looking the order up by client_id and in open orders.
- Fills are derived as deltas of the broker's cumulative fill totals, so re-reading the same
  order never double-counts. Fill rows, position snapshot and state change commit atomically.
- Positions are long-only; fees in the base asset reduce quantity, any fee is valued in the quote
  currency; the exact cost basis (fees included) drives realized P&L, which is net of fees.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable

from wallexbot.exchange.base import BrokerOrder, OrderRejected, OrderStatus, RateLimited
from wallexbot.risk.engine import AccountState, RiskDecision
from wallexbot.store import Store
from wallexbot.types import TERMINAL_STATES, Fill, IntentState, OrderIntent, Position, Quote, Side

S = IntentState
OPEN_INTENT_STATES = frozenset({S.RISK_APPROVED, S.SUBMITTED, S.PARTIALLY_FILLED, S.UNKNOWN_PENDING})
_RECONCILE_STATES = frozenset({S.SUBMITTED, S.PARTIALLY_FILLED, S.UNKNOWN_PENDING})
_ZERO = Decimal(0)


class LedgerError(Exception):
    pass


@dataclass(frozen=True)
class SubmitResult:
    client_id: str
    state: IntentState
    sent: bool                         # True iff broker.submit_order was called by this call
    decision: RiskDecision | None = None


def fee_in_quote(fee: Decimal, fee_asset: str, price: Decimal, base: str, quote: str) -> Decimal:
    if fee == 0 or fee_asset == quote:
        return fee
    if fee_asset == base:
        return fee * price
    raise LedgerError(f"cannot value fee asset {fee_asset!r} in {quote}")


def apply_fill(pos: Position, fill: Fill, quote_amount: Decimal, base: str,
               quote: str) -> tuple[Position, Decimal | None]:
    """Return (new position, closed round-trip P&L if this fill closed the position).

    The exact quote-currency cost basis is carried in pos.meta["cost_basis"]; avg_price is derived
    from it. A full close releases the whole basis, so a round trip's P&L equals its cash flows."""
    fee_q = fee_in_quote(fill.fee, fill.fee_asset, fill.price, base, quote)
    base_fee = fill.fee if fill.fee_asset == base else _ZERO
    other_fee = _ZERO if fill.fee_asset == base else fee_q  # quote-denominated cost of a non-base fee
    meta = dict(pos.meta)
    cost = meta.get("cost_basis", pos.avg_price * pos.quantity)
    if fill.side is Side.BUY:
        new_qty = pos.quantity + fill.quantity - base_fee
        if new_qty <= 0:
            raise LedgerError("buy fill leaves non-positive position")
        if pos.quantity == 0:
            meta["realized_at_open"] = pos.realized_pnl
            cost = _ZERO
        cost += quote_amount + other_fee
        meta["cost_basis"] = cost
        return Position(pos.symbol, new_qty, cost / new_qty, pos.realized_pnl, pos.stop_price, meta), None
    base_out = fill.quantity + base_fee
    if base_out > pos.quantity:
        raise LedgerError(f"sell of {base_out} exceeds position {pos.quantity}")
    new_qty = pos.quantity - base_out
    released = cost if new_qty == 0 else cost * base_out / pos.quantity
    realized = pos.realized_pnl + (quote_amount - other_fee) - released
    if new_qty == 0:
        closed = realized - meta.pop("realized_at_open", _ZERO)
        meta.pop("cost_basis", None)
        return Position(pos.symbol, _ZERO, _ZERO, realized, None, meta), closed
    meta["cost_basis"] = cost - released
    return Position(pos.symbol, new_qty, pos.avg_price, realized, pos.stop_price, meta), None


def _default_approve(decision: RiskDecision) -> bool:
    return decision.approved is True


class OMS:
    def __init__(self, store: Store, broker, risk, killswitch, *,
                 approve: Callable[[RiskDecision], bool] = _default_approve,
                 clock: Callable[[], float] = time.time, not_found_grace_s: float = 30.0) -> None:
        self.store = store
        self.broker = broker
        self.risk = risk
        self.killswitch = killswitch
        self.approve = approve
        self.clock = clock
        self.not_found_grace_s = not_found_grace_s

    # ------------------------------------------------------------------ views
    def positions(self) -> dict[str, Position]:
        return self.store.latest_positions()

    def open_intents(self) -> list[OrderIntent]:
        """Intents that may hold a live exchange order (ambiguous ones included)."""
        return [r.intent for r in self.store.intents_in_states(OPEN_INTENT_STATES)]

    # ------------------------------------------------------------------ submit
    def submit(self, intent: OrderIntent, quote: Quote | None, account: AccountState,
               now: float | None = None) -> SubmitResult:
        now = self._now(now)
        cid = intent.client_id
        self.store.append_intent(intent, payload={"intent": intent}, ts=now)
        rec = self.store.get_intent(cid)
        if rec.state is not S.PROPOSED:
            if rec.state is S.RISK_APPROVED:  # a previous run may have died mid-submit: never resend
                self._cas(cid, S.RISK_APPROVED, S.UNKNOWN_PENDING, "resubmit_seen_risk_approved", now)
            return SubmitResult(cid, self._state(cid), sent=False)

        try:
            decision = self.risk.check(intent, quote, account, self.positions(), self.open_intents(), now)
        except Exception as e:
            decision = RiskDecision(False, (f"risk_error:{type(e).__name__}",))
        self.store.append_risk_decision(cid, decision, ts=now)
        try:
            approved = self.approve(decision) is True
        except Exception:
            approved = False
        if not approved:
            self._cas(cid, S.PROPOSED, S.REJECTED, "risk:" + ",".join(decision.reasons), now)
            return SubmitResult(cid, self._state(cid), sent=False, decision=decision)
        if not self._cas(cid, S.PROPOSED, S.RISK_APPROVED, "risk_approved", now):
            return SubmitResult(cid, self._state(cid), sent=False, decision=decision)  # lost a race
        if not self._ks_allows(intent):  # last-moment gate
            self._cas(cid, S.RISK_APPROVED, S.REJECTED, "kill_switch", now)
            return SubmitResult(cid, self._state(cid), sent=False, decision=decision)

        self.store.append_broker_event(cid, "submit_attempt", {"intent": intent}, ts=now)
        try:
            order = self.broker.submit_order(
                client_id=cid, symbol=intent.symbol, side=intent.side, type=intent.type,
                quantity=intent.quantity, price=intent.price, stop_price=intent.stop_price)
        except (RateLimited, OrderRejected) as e:  # definitely not created
            self.store.append_broker_event(cid, "submit_rejected", {"error": repr(e)}, ts=now)
            self._cas(cid, S.RISK_APPROVED, S.REJECTED, f"broker:{type(e).__name__}", now)
            return SubmitResult(cid, self._state(cid), sent=True, decision=decision)
        except Exception as e:  # timeout / duplicate client_id / anything else: ambiguous
            self.store.append_broker_event(cid, "submit_ambiguous", {"error": repr(e)}, ts=now)
            self._cas(cid, S.RISK_APPROVED, S.UNKNOWN_PENDING, f"broker:{type(e).__name__}", now)
            return SubmitResult(cid, self._state(cid), sent=True, decision=decision)
        self.store.append_broker_event(cid, "submit_ack", order, ts=now)
        self._cas(cid, S.RISK_APPROVED, S.SUBMITTED, "broker_ack", now)
        self._apply(cid, order, now)
        return SubmitResult(cid, self._state(cid), sent=True, decision=decision)

    # ------------------------------------------------------------------ reconcile / recover
    def reconcile(self, now: float | None = None) -> dict[str, IntentState]:
        """Resolve every SUBMITTED / PARTIALLY_FILLED / UNKNOWN_PENDING intent against the broker."""
        now = self._now(now)
        pending = self.store.intents_in_states(_RECONCILE_STATES)
        if not pending:
            return {}
        try:
            open_by_id: dict[str, BrokerOrder] | None = {o.client_id: o for o in self.broker.open_orders()}
        except Exception as e:
            self.store.append_error("oms.reconcile", f"open_orders failed: {e!r}", ts=now)
            open_by_id = None
        out: dict[str, IntentState] = {}
        for rec in pending:
            cid = rec.intent.client_id
            try:
                order = self.broker.get_order(cid)
            except Exception as e:
                self.store.append_error("oms.reconcile", f"get_order failed: {e!r}", {"client_id": cid}, ts=now)
                out[cid] = rec.state
                continue
            if order is None and open_by_id is not None:
                order = open_by_id.get(cid)
            if order is not None:
                self.store.append_broker_event(cid, "reconcile_found", order, ts=now)
                self._apply(cid, order, now)
            elif open_by_id is None:
                pass  # absence not confirmed by both lookups: stay as is
            elif rec.state is S.UNKNOWN_PENDING:
                if now - rec.updated_at >= self.not_found_grace_s:
                    self.store.append_broker_event(cid, "reconcile_not_found", None, ts=now)
                    self._cas(cid, S.UNKNOWN_PENDING, S.REJECTED, "not_found_at_exchange", now)
            else:  # the exchange acknowledged this order earlier and now has no record of it
                self._needs_review(cid, "acknowledged_order_missing", now)
            out[cid] = self._state(cid)
        return out

    def recover(self, now: float | None = None) -> dict[str, IntentState]:
        """After a restart: settle intents interrupted mid-flow, then reconcile everything open."""
        now = self._now(now)
        for rec in self.store.intents_in_states({S.PROPOSED}):
            # PROPOSED never reached the broker (submit happens only after RISK_APPROVED).
            self._cas(rec.intent.client_id, S.PROPOSED, S.REJECTED, "abandoned_before_risk", now)
        for rec in self.store.intents_in_states({S.RISK_APPROVED}):
            self._cas(rec.intent.client_id, S.RISK_APPROVED, S.UNKNOWN_PENDING, "recover_mid_submit", now)
        return self.reconcile(now)

    def cancel_stale(self, max_age_s: float, now: float | None = None) -> list[str]:
        """Cancel resting ENTRY orders older than max_age_s. Returns client_ids canceled."""
        now = self._now(now)
        done = []
        for rec in self.store.intents_in_states({S.SUBMITTED, S.PARTIALLY_FILLED}):
            if rec.intent.reduce_only or now - rec.created_at <= max_age_s:
                continue
            cid = rec.intent.client_id
            try:
                order = self.broker.cancel_order(cid)
            except Exception as e:
                self.store.append_error("oms.cancel_stale", f"cancel failed: {e!r}", {"client_id": cid}, ts=now)
                continue
            self.store.append_broker_event(cid, "cancel", order, ts=now)
            if order is not None:
                self._apply(cid, order, now)
            if self._state(cid) is S.CANCELED:
                done.append(cid)
        return done

    # ------------------------------------------------------------------ internals
    def _now(self, now: float | None) -> float:
        return float(self.clock()) if now is None else float(now)

    def _state(self, cid: str) -> IntentState:
        return self.store.get_intent(cid).state

    def _ks_allows(self, intent: OrderIntent) -> bool:
        try:
            return self.killswitch.allows(intent) is True
        except Exception:
            return False

    def _cas(self, cid: str, expected: IntentState, new: IntentState, reason: str, now: float) -> bool:
        try:
            self.store.set_intent_state(cid, new, expected=expected, reason=reason, ts=now)
            return True
        except Exception as e:
            self.store.append_error("oms.state", repr(e), {"client_id": cid}, ts=now)
            return False

    def _needs_review(self, cid: str, reason: str, now: float) -> None:
        st = self._state(cid)
        if st not in TERMINAL_STATES:
            self._cas(cid, st, S.NEEDS_REVIEW, reason, now)

    def _apply(self, cid: str, order: BrokerOrder, now: float) -> None:
        """Record fill deltas, update the position and move the intent state, atomically."""
        try:
            with self.store.tx():
                rec = self.store.get_intent(cid)
                if rec.state in TERMINAL_STATES:
                    return
                self._check_identity(rec.intent, order)
                self._record_fill_delta(rec.intent, order, now)
                target = self._target_state(rec.intent, order)
                if target is not rec.state:
                    self.store.set_intent_state(cid, target, expected=rec.state,
                                                reason=f"broker:{order.status.value}", ts=now)
        except Exception as e:
            self.store.append_error("oms.apply", repr(e), {"client_id": cid, "order": order}, ts=now)
            self._needs_review(cid, f"apply_failed:{type(e).__name__}", now)

    @staticmethod
    def _check_identity(intent: OrderIntent, order: BrokerOrder) -> None:
        if (order.client_id, order.symbol, order.side, order.quantity) != \
                (intent.client_id, intent.symbol, intent.side, intent.quantity):
            raise LedgerError("broker order does not match intent")

    @staticmethod
    def _target_state(intent: OrderIntent, order: BrokerOrder) -> IntentState:
        st = order.status
        if st in (OrderStatus.NEW, OrderStatus.PENDING_TRIGGER):
            return S.PARTIALLY_FILLED if order.filled_qty > 0 else S.SUBMITTED
        if st is OrderStatus.PARTIALLY_FILLED:
            return S.PARTIALLY_FILLED
        if st is OrderStatus.FILLED:
            if order.filled_qty != intent.quantity:
                raise LedgerError("FILLED but filled_qty != quantity")
            return S.FILLED
        if st is OrderStatus.CANCELED:
            return S.CANCELED
        if st is OrderStatus.REJECTED:
            if order.filled_qty != 0:
                raise LedgerError("REJECTED order with fills")
            return S.REJECTED
        raise LedgerError(f"unknown broker status {st!r}")

    def _record_fill_delta(self, intent: OrderIntent, order: BrokerOrder, now: float) -> None:
        tot = self.store.fill_totals(intent.client_id)
        dq = order.filled_qty - tot.quantity
        dquote = order.filled_quote - tot.quote_amount
        dfee = order.fee - tot.fee
        if dq < 0 or dquote < 0 or dfee < 0 or order.filled_qty > intent.quantity:
            raise LedgerError("broker fill totals went backwards or exceed order quantity")
        if dq == 0:
            if dquote != 0 or dfee != 0:
                raise LedgerError("fill amounts changed without quantity")
            return
        rules = self.risk.rules.get(intent.symbol)
        if rules is None:
            raise LedgerError(f"no symbol rules for {intent.symbol}")
        price = dquote / dq
        fill = Fill(intent.client_id, intent.symbol, intent.side, price, dq, dfee, order.fee_asset, now)
        fee_q = fee_in_quote(dfee, order.fee_asset, price, rules.base, rules.quote)
        pos = self.positions().get(intent.symbol, Position(intent.symbol))
        new_pos, closed = apply_fill(pos, fill, dquote, rules.base, rules.quote)
        self.store.append_fill(fill, seq=tot.count + 1, quote_amount=dquote, fee_quote=fee_q)
        self.store.append_position_snapshot(new_pos, closed_pnl=closed, ts=now)
