"""Policy gate and the scripted mock trade used by tests and the demo.

Autonomy policy: an order is approved iff the risk engine approved it. There is no human step
per order (human approval exists only for live arming / config, outside this module).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping

from wallexbot.config import Config, load_config
from wallexbot.exchange.base import Balance
from wallexbot.exchange.mock_wallex import MockClock, MockWallex
from wallexbot.oms import OMS, SubmitResult
from wallexbot.risk.engine import AccountState, RiskDecision, RiskEngine, utc_day
from wallexbot.risk.killswitch import KillSwitch
from wallexbot.store import AUDIT_TABLES, Store
from wallexbot.types import IntentState, OrderIntent, OrderType, Quote, Side, SymbolRules, round_down


def auto_approve(decision: RiskDecision) -> bool:
    return isinstance(decision, RiskDecision) and decision.approved is True


def snapshot_account(store: Store, balances: dict[str, Balance], quote_ccy: str,
                     quotes: dict[str, Quote], now: float) -> AccountState:
    """Build the risk engine's account view; records SOD equity (first per UTC day) and HWM."""
    cash = balances.get(quote_ccy, Balance(Decimal(0), Decimal(0)))
    positions = store.latest_positions()
    marks = {s: q.bid for s, q in quotes.items()}
    equity = cash.total + sum((p.quantity * marks.get(s, p.avg_price) for s, p in positions.items()),
                              Decimal(0))
    day = utc_day(now)
    store.append_equity_sod(day, equity, ts=now)
    hwm = store.equity_hwm()
    if hwm is None or equity > hwm:
        store.append_equity_hwm(equity, ts=now)
        hwm = equity
    losses, last_loss = store.consecutive_losses()
    return AccountState(equity=equity, cash_free=cash.free, sod_equity=store.sod_equity(day), sod_day=day,
                        hwm_equity=hwm, consecutive_losses=losses, last_loss_ts=last_loss, marks=marks)


def route_to_oms(oms: OMS, intent: OrderIntent, quote: Quote | None, account: AccountState,
                 now: float | None = None) -> SubmitResult:
    """The trading loop's single door to the order path. bot/ may not call OMS.submit itself (the
    single-order-path AST guard only allows calls from oms.py / pipeline.py and one reviewed guardian
    scope), so every bot order passes through here: risk check, approval policy, kill-switch gate,
    idempotent client_id and audit all happen inside OMS.submit."""
    return oms.submit(intent, quote, account, now)


def cancel_open_intent(oms: OMS, client_id: str, now: float | None = None) -> IntentState | None:
    """Cancel one resting order at the broker (e.g. a timed-out reduce-only LIMIT exit before it is
    escalated to MARKET) and let OMS.reconcile apply the outcome. Never sends an order. Returns the
    intent state afterwards (None if unknown). A failed cancel is recorded and left to reconcile."""
    now = float(oms.clock()) if now is None else float(now)
    if oms.store.get_intent(client_id) is None:
        return None
    try:
        order = oms.broker.cancel_order(client_id)
        oms.store.append_broker_event(client_id, "cancel", order, ts=now)
    except Exception as e:
        oms.store.append_error("pipeline.cancel", f"cancel failed: {e!r}", {"client_id": client_id}, ts=now)
    oms.reconcile(now)
    return oms.store.get_intent(client_id).state


def build_paper_broker(cfg: Config, clock, rules: Mapping[str, SymbolRules],
                       balances: dict[str, Decimal] | None = None):
    """Paper broker (MockWallex) over arbitrary symbols for the bot's mock / replay runs. Its book is
    driven by the caller (set_book); bot/ may not import the broker modules itself."""
    return MockWallex(rules.values(), clock=clock,
                      balances=dict(balances) if balances is not None else {cfg.quote_currency: Decimal("10000")},
                      maker_rate=cfg.maker_rate, taker_rate=cfg.taker_rate)


def build_mock_broker(cfg: Config, clock, balances: dict[str, Decimal] | None = None):
    """Demo wiring for processes outside the order path (the guardian CLI) that may not import the
    broker modules themselves. Returns (MockWallex with a BTC book, {symbol: SymbolRules})."""
    sym = f"BTC{cfg.quote_currency}"
    rules = {sym: SymbolRules(sym, "BTC", cfg.quote_currency, amount_precision=6, price_precision=2)}
    broker = MockWallex(rules.values(), clock=clock,
                        balances=balances if balances is not None else {cfg.quote_currency: Decimal("10000")},
                        maker_rate=cfg.maker_rate, taker_rate=cfg.taker_rate)
    broker.set_price(sym, Decimal("60000.00"), Decimal("60010.00"))
    return broker, rules


def run_scripted_trade(db_path: str = ":memory:", config: Config | None = None) -> dict[str, Any]:
    """signal -> risk -> OMS -> MockWallex fill -> P&L, for one round trip on BTCUSDT.

    Returns {"entry", "exit": SubmitResult, "realized_pnl": Decimal, "audit": {table: rows}}.
    """
    cfg = config or load_config(env={})
    clock = MockClock()
    sym = f"BTC{cfg.quote_currency}"
    rules = {sym: SymbolRules(sym, "BTC", cfg.quote_currency, amount_precision=6, price_precision=2)}
    broker = MockWallex(rules.values(), clock=clock, balances={cfg.quote_currency: Decimal("10000")},
                        maker_rate=cfg.maker_rate, taker_rate=cfg.taker_rate)
    store = Store(db_path, clock=clock)
    ks = KillSwitch(store)
    oms = OMS(store, broker, RiskEngine(cfg, rules, ks), ks, approve=auto_approve, clock=clock)

    def account() -> AccountState:
        return snapshot_account(store, broker.balances(), cfg.quote_currency, {sym: broker.quote(sym)}, clock())

    broker.set_price(sym, Decimal("60000.00"), Decimal("60010.00"))
    signal_id = "scripted-breakout-1"
    store.append_signal(signal_id, sym, {"strategy": "scripted", "why": "demo breakout above range"})
    entry = oms.submit(OrderIntent(sym, Side.BUY, OrderType.MARKET, Decimal("0.010000"), None, signal_id),
                       broker.quote(sym), account())

    clock.advance(600)
    broker.set_price(sym, Decimal("61200.00"), Decimal("61210.00"))
    held = round_down(store.latest_positions()[sym].quantity, rules[sym].amount_precision)
    exit_ = oms.submit(OrderIntent(sym, Side.SELL, OrderType.MARKET, held, None, signal_id + "-exit",
                                   reduce_only=True), broker.quote(sym), account())
    pnl = store.latest_positions()[sym].realized_pnl
    audit = {t: store.rows(t) for t in AUDIT_TABLES}
    store.close()
    return {"entry": entry, "exit": exit_, "realized_pnl": pnl, "audit": audit}
