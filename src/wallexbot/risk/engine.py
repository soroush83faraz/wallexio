"""Fail-closed pre-trade risk engine.

Entries (BUY, not reduce-only) run every check below, in order, and are approved only if none
fails. Reduce-only SELLs (exits) skip entry limits, the kill switch and loss breaches; they still
need known symbol rules, exact precision, and must not sell more than is held and uncommitted.
Any exception inside check() => rejected. The trading day is the UTC calendar day (24/7 market).

Clock / timestamp handling (fail closed; a backward clock step or bad data must never make old
data look fresh):
- Entry quotes: `quote.ts` must be finite and at most `max_future_skew_s` after `now`
  (`quote_future_ts` otherwise), and at most `config.max_data_age_s` before it (`quote_stale`
  otherwise; the boundary age itself is accepted). A quote inside the future skew is as fresh as
  of `now` (age clamped to 0), matching guardian/monitor.py. Prices must be finite and > 0
  (`quote_bad_price`), not crossed (bid > ask: `quote_crossed`) and the spread must be below
  `max_sane_spread_bps` (`quote_absurd_spread`; the configured `max_spread_bps` policy check
  `spread_too_wide` still applies on top).
- Loss cooldown: a `last_loss_ts` after `now` (clock stepped back, or bad data) is replaced by the
  earliest `now` at which this engine saw it, so the cooldown still runs a full `cooldown_s` but
  always expires; it can neither go negative nor last until the clock catches up with a bogus ts.
  A non-finite `last_loss_ts` keeps the cooldown engaged (unknown => fail closed).
- SOD day: `sod_day` must equal the UTC day of `now`; a clock stepped back across midnight
  therefore rejects entries (`sod_equity_stale`) until the account snapshot is rebuilt.
Reduce-only exits never look at the quote or the clock.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Mapping, Sequence

from wallexbot.config import Config
from wallexbot.types import OrderIntent, OrderType, Position, Quote, Side, SymbolRules, round_down

_HUNDRED = Decimal(100)
_BPS = Decimal(10000)


def utc_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


@dataclass(frozen=True)
class AccountState:
    equity: Decimal            # total account value in quote currency
    cash_free: Decimal         # free quote balance
    sod_equity: Decimal        # equity at the start of sod_day (UTC)
    sod_day: str               # YYYY-MM-DD (UTC) that sod_equity belongs to
    hwm_equity: Decimal        # equity high-water mark
    consecutive_losses: int = 0
    last_loss_ts: float | None = None
    marks: Mapping[str, Decimal] = field(default_factory=dict)  # symbol -> mark price


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reasons: tuple[str, ...] = ()
    hard_breach: bool = False   # hard daily-loss/drawdown breached: guardian should flatten


@dataclass(frozen=True)
class Breaches:
    soft: tuple[str, ...]
    hard: tuple[str, ...]


class _Stop(Exception):
    """Internal: a gating check failed; later checks cannot run meaningfully."""


class RiskEngine:
    def __init__(self, config: Config, rules: Mapping[str, SymbolRules], killswitch,
                 *, cooldown_s: float = 3600.0, max_future_skew_s: float = 5.0,
                 max_sane_spread_bps: Decimal | int | str = 2000) -> None:
        if not (math.isfinite(cooldown_s) and cooldown_s >= 0):
            raise ValueError("cooldown_s must be finite and >= 0")
        if not (math.isfinite(max_future_skew_s) and max_future_skew_s >= 0):
            raise ValueError("max_future_skew_s must be finite and >= 0")
        sane = Decimal(str(max_sane_spread_bps))
        if not (sane.is_finite() and sane > 0):
            raise ValueError("max_sane_spread_bps must be finite and > 0")
        self.config = config
        self.rules = dict(rules)
        self.killswitch = killswitch
        self.cooldown_s = cooldown_s
        self.max_future_skew_s = max_future_skew_s
        self.max_sane_spread_bps = sane
        # (future last_loss_ts value, earliest `now` it was seen at): see _cooldown
        self._future_loss: tuple[float, float] | None = None

    # ------------------------------------------------------------------ public
    def check(self, intent: OrderIntent, quote: Quote | None, account: AccountState,
              positions: Mapping[str, Position], open_orders: Sequence[Any], now: float) -> RiskDecision:
        try:
            if intent.reduce_only:
                return self._check_exit(intent, account, positions, open_orders)
            return self._check_entry(intent, quote, account, positions, open_orders, now)
        except Exception as e:  # fail closed
            return RiskDecision(False, (f"risk_error:{type(e).__name__}",))

    def breaches(self, account: AccountState) -> Breaches:
        """Soft/hard daily-loss and drawdown breaches (percent of SOD equity / HWM)."""
        r = self.config.risk
        if account.sod_equity <= 0 or account.hwm_equity <= 0:
            raise ValueError("non-positive reference equity")
        daily = (account.sod_equity - account.equity) / account.sod_equity * _HUNDRED
        dd = (account.hwm_equity - account.equity) / account.hwm_equity * _HUNDRED
        soft, hard = [], []
        if daily >= r.daily_loss_hard_pct:
            hard.append("daily_loss_hard")
        elif daily >= r.daily_loss_soft_pct:
            soft.append("daily_loss_soft")
        if dd >= r.drawdown_hard_pct:
            hard.append("drawdown_hard")
        elif dd >= r.drawdown_soft_pct:
            soft.append("drawdown_soft")
        return Breaches(tuple(soft), tuple(hard))

    # ------------------------------------------------------------------ exits
    def _check_exit(self, intent, account, positions, open_orders) -> RiskDecision:
        reasons: list[str] = []
        hard = False
        try:  # informational only: a broken account must never block an exit
            hard = bool(self.breaches(account).hard)
        except Exception:
            pass
        if intent.side is not Side.SELL:
            return RiskDecision(False, ("reduce_only_must_be_sell",), hard)
        rules = self.rules.get(intent.symbol)
        if rules is None:
            return RiskDecision(False, ("unknown_symbol",), hard)
        reasons += self._precision(intent, rules)
        held = positions.get(intent.symbol, Position(intent.symbol)).quantity
        committed = sum((o.quantity for o in open_orders
                         if o.symbol == intent.symbol and o.side is Side.SELL), Decimal(0))
        if intent.quantity > held - committed:
            reasons.append("exceeds_position")
        return RiskDecision(not reasons, tuple(reasons), hard)

    # ------------------------------------------------------------------ entries
    def _check_entry(self, intent, quote, account, positions, open_orders, now) -> RiskDecision:
        cfg, r = self.config, self.config.risk
        reasons: list[str] = []
        hard = False

        def run(name: str, fn: Callable[[], list[str] | str | None], gate: bool = False) -> None:
            try:
                res = fn()
            except Exception as e:
                res = f"check_error:{name}:{type(e).__name__}"
            if isinstance(res, str):
                res = [res]
            if res:
                reasons.extend(res)
                if gate:
                    raise _Stop

        try:  # computed independently of the gates below so the guardian always sees it
            hard = bool(self.breaches(account).hard)
        except Exception:
            hard = False  # the loss_limits check below then rejects the entry
        rules = self.rules.get(intent.symbol)
        try:
            run("kill_switch", lambda: None if self.killswitch.allows(intent) else "kill_switch_engaged")
            run("long_only", lambda: None if intent.side is Side.BUY else "sell_must_be_reduce_only",
                gate=True)
            run("quote", lambda: self._quote_problem(intent, quote, now), gate=True)
            run("symbol", lambda: None if rules is not None else "unknown_symbol", gate=True)
            run("spread", lambda: "spread_too_wide" if quote.spread_bps > cfg.max_spread_bps else None)
            run("precision", lambda: self._precision(intent, rules), gate=True)
            notional = intent.quantity * self._entry_price(intent, quote)
            min_notional = rules.min_notional if rules.min_notional is not None else r.min_notional
            run("min_notional", lambda: "below_min_notional" if notional < min_notional else None)
            run("max_order_value", lambda: "above_max_order_value" if notional > r.max_order_value else None)
            run("equity", lambda: None if account.equity > 0 else "non_positive_equity", gate=True)
            run("max_position", lambda: self._position_limit(intent, quote, account, positions, notional))
            run("max_exposure", lambda: self._exposure_limit(intent, quote, account, positions,
                                                             open_orders, notional))
            run("max_open_positions", lambda: self._open_positions_limit(intent, positions, open_orders))
            fee_rate = max(cfg.taker_rate, cfg.maker_rate)
            run("cash", lambda: "insufficient_cash"
                if notional * (1 + fee_rate) > account.cash_free else None)
            run("open_order", lambda: "open_order_exists"
                if any(o.symbol == intent.symbol for o in open_orders) else None)
            run("sod_day", lambda: None if account.sod_day == utc_day(now) else "sod_equity_stale")
            run("loss_limits", lambda: [*(b := self.breaches(account)).hard, *b.soft])
            run("cooldown", lambda: self._cooldown(account, now))
        except _Stop:
            pass
        return RiskDecision(not reasons, tuple(reasons), hard)

    # ------------------------------------------------------------------ helpers
    def _quote_problem(self, intent: OrderIntent, quote: Quote | None, now: float) -> str | None:
        """Entry-quote validity. Every comparison is written so NaN fails closed."""
        if quote is None:
            return "no_quote"
        if quote.symbol != intent.symbol:
            return "quote_symbol_mismatch"
        now = float(now)
        if not math.isfinite(now):
            return "bad_clock"
        try:
            ts = float(quote.ts)
        except (TypeError, ValueError):
            return "quote_bad_ts"
        if not math.isfinite(ts):
            return "quote_bad_ts"
        if not ts <= now + self.max_future_skew_s:
            return "quote_future_ts"
        age = max(0.0, now - ts)   # inside the skew: fresh as of now
        if not age <= self.config.max_data_age_s:
            return "quote_stale"
        try:
            bid, ask = Decimal(str(quote.bid)), Decimal(str(quote.ask))
        except Exception:
            return "quote_bad_price"
        if not (bid.is_finite() and ask.is_finite() and bid > 0 and ask > 0):
            return "quote_bad_price"
        if bid > ask:
            return "quote_crossed"
        if not (ask - bid) / ((bid + ask) / 2) * _BPS <= self.max_sane_spread_bps:
            return "quote_absurd_spread"
        return None

    @staticmethod
    def _precision(intent: OrderIntent, rules: SymbolRules) -> list[str]:
        out = []
        q = intent.quantity
        if q <= 0:
            out.append("non_positive_quantity")
        elif round_down(q, rules.amount_precision) != q:
            out.append("quantity_precision")
        needs_price = intent.type in (OrderType.LIMIT, OrderType.STOP_LIMIT)
        needs_stop = intent.type in (OrderType.STOP_LIMIT, OrderType.STOP_MARKET)
        for name, val, needed in (("price", intent.price, needs_price),
                                  ("stop_price", intent.stop_price, needs_stop)):
            if val is None:
                if needed:
                    out.append(f"missing_{name}")
            elif val <= 0:
                out.append(f"non_positive_{name}")
            elif round_down(val, rules.price_precision) != val:
                out.append(f"{name}_precision")
        return out

    def _entry_price(self, intent: OrderIntent, quote: Quote) -> Decimal:
        """Conservative per-unit price for sizing checks."""
        slip = 1 + self.config.max_slippage_bps / _BPS
        if intent.type is OrderType.LIMIT:
            return intent.price
        if intent.type is OrderType.STOP_LIMIT:
            return intent.price
        if intent.type is OrderType.STOP_MARKET:
            return max(intent.stop_price, quote.ask) * slip
        return quote.ask * slip

    def _mark(self, symbol: str, quote: Quote, account: AccountState, pos: Position) -> Decimal:
        if symbol == quote.symbol:
            return quote.mid
        return account.marks.get(symbol, pos.avg_price)

    def _position_limit(self, intent, quote, account, positions, notional) -> str | None:
        pos = positions.get(intent.symbol)
        existing = pos.quantity * quote.mid if pos else Decimal(0)
        pct = (existing + notional) / account.equity * _HUNDRED
        return "above_max_position_pct" if pct > self.config.risk.max_position_pct else None

    def _exposure_limit(self, intent, quote, account, positions, open_orders, notional) -> str | None:
        held = sum((p.quantity * self._mark(s, quote, account, p) for s, p in positions.items()
                    if p.quantity > 0), Decimal(0))
        pending = Decimal(0)
        for o in open_orders:
            if o.side is Side.BUY:
                px = o.price if o.price is not None else self._mark(
                    o.symbol, quote, account, positions.get(o.symbol, Position(o.symbol)))
                pending += o.quantity * px
        pct = (held + pending + notional) / account.equity * _HUNDRED
        return "above_max_exposure_pct" if pct > self.config.risk.max_exposure_pct else None

    def _open_positions_limit(self, intent, positions, open_orders) -> str | None:
        held = {s for s, p in positions.items() if p.quantity > 0}
        held |= {o.symbol for o in open_orders if o.side is Side.BUY}
        if intent.symbol not in held and len(held) >= self.config.risk.max_open_positions:
            return "max_open_positions"
        return None

    def _cooldown(self, account: AccountState, now: float) -> str | None:
        """Cooldown after N consecutive losses, `cooldown_s` from the latest loss.

        A last_loss_ts after `now` (backward clock step / bad data) is clamped to the earliest `now`
        this engine has seen it at: the loss cannot have happened after it was first observed, so the
        full cooldown still runs from then, but it always expires (no negative elapsed time, no
        waiting for the clock to reach a bogus future ts). Unknown/non-finite ts: stay in cooldown."""
        if account.consecutive_losses < self.config.risk.consecutive_loss_cooldown:
            return None
        if account.last_loss_ts is None:
            return "loss_cooldown"
        now = float(now)
        ts = float(account.last_loss_ts)
        if not (math.isfinite(ts) and math.isfinite(now)):
            return "loss_cooldown"
        if ts > now:
            prev = self._future_loss
            seen = min(prev[1], now) if prev is not None and prev[0] == ts else now
            self._future_loss = (ts, seen)
            ts = seen
        elif self._future_loss is not None and self._future_loss[0] == ts:
            ts = min(ts, self._future_loss[1])   # clock caught up: keep the earlier start
        if not now - ts >= self.cooldown_s:
            return "loss_cooldown"
        return None
