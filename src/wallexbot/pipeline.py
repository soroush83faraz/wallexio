"""Policy gate and the scripted mock trade used by tests and the demo.

Autonomy policy: an order is approved iff the risk engine approved it. There is no human step
per order (human approval exists only for live arming / config, outside this module).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from wallexbot.config import Config, load_config
from wallexbot.exchange.base import Balance
from wallexbot.exchange.mock_wallex import MockClock, MockWallex
from wallexbot.oms import OMS
from wallexbot.risk.engine import AccountState, RiskDecision, RiskEngine, utc_day
from wallexbot.risk.killswitch import KillSwitch
from wallexbot.store import AUDIT_TABLES, Store
from wallexbot.types import OrderIntent, OrderType, Quote, Side, SymbolRules, round_down


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
