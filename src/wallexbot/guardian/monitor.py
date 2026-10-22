"""Guardian: the independent emergency layer (docs/RISK.md).

It runs with its own Store connection and reads the BROKER as the source of truth (balances, open
orders, quotes). In this build (mock/shadow) it runs inside the bot process against the bot's own
broker object (guardian/main.py InProcessGuardian); a separate guardian process is only meaningful
against a real exchange (live), which this build does not have. It imports nothing from wallexbot.strategy,
wallexbot.backtest or wallexbot.ai (enforced by tests/test_guardian_isolation.py), and it never
touches the broker's order entry: exits go through OMS.submit inside flatten.py.

One tick:
1. oms.reconcile() so the ledger reflects every known order (ambiguous ones included).
2. Read balances, open orders and a quote per watched symbol (held, ordered, or in the universe).
3. Equity = quote-currency balance (free + locked) + base holdings (free + locked) x bid.
   Start-of-day equity is the first snapshot per UTC day (store.append_equity_sod: first write
   wins), the high-water mark is appended when exceeded. Both are written only from fresh,
   fully marked data, so a stale feed cannot set them.
4. Conditions:
   Exposure (what is "held") ignores dust under the shared rule (wallexbot/dust.py).
   - soft breach (RiskEngine.breaches)                       -> kill switch (entries off)
   - hard breach                                             -> flatten_all(hard=True)
   - exchange base balance != OMS ledger beyond tolerance,
     on `mismatch_confirm_ticks` consecutive ticks            -> kill switch + needs_review
     (exchange wins: nothing in the ledger is "fixed"; flatten sizes from exchange balances)
   - feed staleness is evaluated ONLY for symbols with a non-dust position or a live order; with
     nothing held or ordered the guardian never acts on feed age (a universe-only symbol's stale or
     missing quote is the bot's concern: it refuses entries on it). For those symbols:
     no fresh quote for > max_data_age_s                     -> condition stale_feed:<symbols>, no
       kill switch (entries on a stale quote are already refused by the bot and the risk engine)
     no fresh quote for > stale_action_after_s (120 s), held -> per config: hold, or flatten
     (a quote dated > max_quote_future_skew_s after the clock is invalid, never fresh; freshness
     is clamped to now, so a bogus future timestamp or a backward clock step cannot mask a
     frozen feed)
   - strategy heartbeat older than heartbeat_timeout_s       -> kill switch, TRANSIENT ('auto:')
   - breach evaluation error                                 -> kill switch (fail closed)
   Kill switch release. Every engagement stays until an operator disengages it, EXCEPT the
   guardian's own transient one (reason 'auto:...', actor 'guardian', still the latest row): the
   guardian releases that after auto_release_clear_ticks (3) consecutive clear ticks spanning at
   least auto_release_clear_s (60 s) of wall-clock AND monotonic time, as an audited
   'release_kill_switch' event. The release is a compare-and-set in one IMMEDIATE store
   transaction: only the row id the clear streak observed, still the latest, is released. A latching
   condition seen while only a transient engagement holds the switch writes a new, latching row
   on top (KillSwitch.engage_unless_held), so it can never be auto-released.
5. Every decision is appended to audit_events with actor='guardian' (and kill-switch rows carry
   actor='guardian').
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Iterable, Sequence

from wallexbot.dust import is_dust, min_notional_for
from wallexbot.guardian.flatten import ACTOR, FlattenConfig, FlattenResult, flatten_all
from wallexbot.oms import OPEN_INTENT_STATES
from wallexbot.risk.engine import AccountState, utc_day
from wallexbot.risk.killswitch import AUTO_PREFIX
from wallexbot.types import IntentState, Side

_ZERO = Decimal(0)
_HUNDRED = Decimal(100)
_IN_FLIGHT = frozenset({IntentState.PROPOSED, IntentState.RISK_APPROVED})
# Kill-switch conditions. LATCHING ones stay engaged until an operator disengages the switch;
# TRANSIENT ones are engaged with an 'auto:' reason that the guardian itself releases once clear.
LATCHING_CONDITIONS = frozenset({"balance_mismatch", "breach_eval_error", "daily_loss_soft", "drawdown_soft",
                                 "daily_loss_hard", "drawdown_hard"})
TRANSIENT_CONDITIONS = frozenset({"heartbeat_lost"})
log = logging.getLogger("wallexbot.guardian")


@dataclass(frozen=True)
class GuardianConfig:
    quote_currency: str | None = None          # None: risk.config.quote_currency
    max_data_age_s: float | None = None         # None: risk.config.max_data_age_s (30 s)
    stale_action_after_s: float = 120.0
    stale_action: str = "hold"                  # "hold" | "flatten"
    # A quote stamped more than this far AFTER the guardian's clock (bad data, ms-vs-s, a clock
    # step) is invalid: not fresh, not used for marking. Freshness never exceeds "now".
    max_quote_future_skew_s: float = 5.0
    heartbeat_component: str = "strategy"
    heartbeat_timeout_s: float = 60.0
    # Balance mismatch tolerance per base asset: max(abs_units x smallest amount step,
    # rel_pct % of the ledger quantity). Fees are booked exactly by the OMS, so any real
    # difference is a missed fill, a manual trade or a ledger fault.
    mismatch_abs_units: int = 1
    mismatch_rel_pct: Decimal = Decimal("0.1")
    mismatch_confirm_ticks: int = 2             # transient races (fill seen before ack) self-clear
    in_flight_grace_s: float = 30.0             # a mid-submit intent younger than this skips the check
    # Auto-release of the guardian's own TRANSIENT ('auto:') kill-switch engagement: the condition
    # must be clear for this many consecutive ticks AND at least this many seconds.
    auto_release_clear_ticks: int = 3
    auto_release_clear_s: float = 60.0
    flatten: FlattenConfig = field(default_factory=FlattenConfig)

    def __post_init__(self) -> None:
        if self.stale_action not in ("hold", "flatten"):
            raise ValueError(f"stale_action must be 'hold' or 'flatten', not {self.stale_action!r}")
        if not self.max_quote_future_skew_s >= 0:
            raise ValueError("max_quote_future_skew_s must be >= 0")
        if self.auto_release_clear_ticks < 1 or not self.auto_release_clear_s >= 60:
            raise ValueError("auto_release_clear_ticks must be >= 1 and auto_release_clear_s >= 60")


@dataclass
class TickReport:
    ts: float
    equity: Decimal | None = None
    conditions: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    flatten: FlattenResult | None = None


class Guardian:
    def __init__(self, store, broker, risk, killswitch, oms, config: GuardianConfig | None = None,
                 clock: Callable[[], float] = time.time, *, sleep: Callable[[float], Any] = time.sleep,
                 depth_fn: Callable[[str], Sequence] | None = None,
                 universe: Callable[[], Iterable[str]] | None = None,
                 monotonic: Callable[[], float] | None = None) -> None:
        """`universe`: symbols to quote and watch besides every symbol held (ledger or exchange
        balance). None = every symbol in the risk rules. The in-process guardian passes the bot's
        trading universe, so it does not quote hundreds of markets the bot never trades.
        `monotonic`: the clock the auto-release window must ALSO have elapsed on (default
        time.monotonic; a simulated run passes its simulated clock)."""
        self.store, self.broker, self.risk, self.ks, self.oms = store, broker, risk, killswitch, oms
        self.universe = universe
        self.cfg = config or GuardianConfig()
        self.clock, self.sleep, self.depth_fn = clock, sleep, depth_fn
        self.monotonic = monotonic or time.monotonic
        self.quote_ccy = self.cfg.quote_currency or risk.config.quote_currency
        self.max_age = float(self.cfg.max_data_age_s if self.cfg.max_data_age_s is not None
                             else risk.config.max_data_age_s)
        self.started_at = float(clock())
        self._last_fresh: dict[str, float] = {}
        self._future_logged: set[str] = set()
        self._quote_err_logged: set[str] = set()
        self._mismatch_streak: dict[str, int] = {}
        self._reviewed: frozenset[str] = frozenset()
        self._last_logged: tuple | None = None
        self._clear_row: int | None = None
        self._clear_ticks = 0
        self._clear_since: float | None = None
        self._clear_mono: float | None = None

    # ------------------------------------------------------------------ public
    def tick(self) -> TickReport:
        now = float(self.clock())
        rep = TickReport(now)
        self._reconcile(now)
        balances = self.broker.balances()          # a failed read fails the tick (the loop counts it)
        try:
            open_orders = self.broker.open_orders()
        except Exception as e:  # breach handling must not depend on this read
            self._error("guardian.monitor", f"open_orders failed: {e!r}", now)
            rep.conditions.append("open_orders_unavailable")
            open_orders = []
        positions = self.oms.positions()
        rules = self.risk.rules
        ordered = {o.symbol for o in open_orders if o.symbol in rules} | \
            {r.intent.symbol for r in self.store.intents_in_states(OPEN_INTENT_STATES) if r.intent.symbol in rules}
        syms = sorted(set(self._watch_symbols(balances, positions, now)) | ordered)
        quotes, future_dated = self._quotes(now, syms, quiet=self._universe_only(syms, balances, positions))
        if future_dated:
            rep.conditions.append("future_quote_ts:" + ",".join(future_dated))

        # Feed staleness only matters where we are exposed: a non-dust position or a live order.
        # Universe-only symbols are the bot's concern (it refuses entries on a stale/missing quote).
        held = sorted(s for s in syms if not self._dust(s, rules[s], balances, positions, quotes))
        ages = {s: now - self._last_fresh.get(s, self.started_at) for s in sorted(set(held) | ordered)}
        feed_age = max(ages.values(), default=None)
        held_age = max((ages[s] for s in held), default=None)
        stale = [s for s, a in ages.items() if a > self.max_age]
        if stale:
            rep.conditions.append("stale_feed:" + ",".join(stale))

        equity, unmarked = self._equity(balances, quotes, positions)
        rep.equity = equity
        day = utc_day(now)
        if not stale and not unmarked:
            self.store.append_equity_sod(day, equity, ts=now)
            hwm = self.store.equity_hwm()
            if hwm is None or equity > hwm:
                self.store.append_equity_hwm(equity, ts=now)
        elif unmarked:
            rep.conditions.append("unmarked:" + ",".join(unmarked))
        sod, hwm = self.store.sod_equity(day), self.store.equity_hwm()
        hard: tuple[str, ...] = ()
        cash = balances.get(self.quote_ccy)
        if sod is not None and hwm is not None:
            account = AccountState(equity=equity, cash_free=cash.free if cash else _ZERO, sod_equity=sod,
                                   sod_day=day, hwm_equity=hwm,
                                   marks={s: q.bid for s, q in quotes.items()})
            try:
                b = self.risk.breaches(account)
                rep.conditions += list(b.soft) + list(b.hard)
                hard = tuple(b.hard)
            except Exception as e:
                rep.conditions.append(f"breach_eval_error:{type(e).__name__}")
        else:
            account = None
            rep.conditions.append("no_reference_equity")

        mismatched = self._mismatches(balances, positions, now)
        if mismatched:
            rep.conditions.append("balance_mismatch:" + ",".join(sorted(mismatched)))
            new = frozenset(mismatched) - self._reviewed
            if new:
                self._needs_review("balance_mismatch", {a: mismatched[a] for a in sorted(new)}, now)
                rep.actions.append("needs_review")
        self._reviewed = frozenset(mismatched)

        hb = self.store.latest_event_ts(self.cfg.heartbeat_component, "heartbeat")
        if now - (hb if hb is not None else self.started_at) > self.cfg.heartbeat_timeout_s:
            rep.conditions.append("heartbeat_lost")

        latching = [c for c in rep.conditions if c.split(":")[0] in LATCHING_CONDITIONS]
        transient = [c for c in rep.conditions if c.split(":")[0] in TRANSIENT_CONDITIONS]
        if latching:
            if self.ks.engage_unless_held("guardian: " + ",".join(latching + transient), actor=ACTOR, ts=now):
                rep.actions.append("engage_kill_switch")
                self._audit("engage_kill_switch", {"reasons": latching + transient, "auto_release": False}, now)
        elif transient and not self.ks.is_engaged():
            self.ks.engage(AUTO_PREFIX + ",".join(transient), actor=ACTOR, ts=now)
            rep.actions.append("engage_kill_switch")
            self._audit("engage_kill_switch", {"reasons": transient, "auto_release": True}, now)
        self._auto_release(now, rep, bool(latching or transient))

        exposure = bool(held) or any(o.side is Side.BUY for o in open_orders)
        if hard and exposure:
            rep.flatten = self._flatten("hard", True, ",".join(hard), account)
            rep.actions.append("flatten:hard")
        elif held_age is not None and held_age > self.cfg.stale_action_after_s:
            if self.cfg.stale_action == "flatten":
                rep.flatten = self._flatten("stale", False, f"feed_age={int(held_age)}s", account)
                rep.actions.append("flatten:stale")
            else:
                rep.conditions.append("stale_hold")

        key = (tuple(rep.conditions), tuple(rep.actions))
        if rep.actions or key != self._last_logged:
            self._audit("tick", {"equity": equity, "sod": sod, "hwm": hwm,
                                 "feed_age_s": None if feed_age is None else round(feed_age, 3),
                                 "conditions": rep.conditions, "actions": rep.actions,
                                 "flat": None if rep.flatten is None else rep.flatten.flat,
                                 "needs_review": None if rep.flatten is None else rep.flatten.needs_review}, now)
            self._last_logged = key
        return rep

    # ------------------------------------------------------------------ pieces
    def _auto_release(self, now: float, rep: TickReport, condition_present: bool) -> None:
        """Release the kill switch ONLY when its latest row is the guardian's own transient
        ('auto:') engagement and no freezing condition has been seen for auto_release_clear_ticks
        consecutive ticks spanning at least auto_release_clear_s of BOTH wall-clock and monotonic
        time (a forward wall-clock jump cannot shorten the window; a backward step restarts it).
        The clear streak belongs to one observed row id: a different latest row restarts it. The
        release is a compare-and-set in one store transaction (KillSwitch.release_transient): if
        the latest row is no longer the observed one (an operator / bot row written meanwhile by
        any connection), nothing is written and the streak resets. Audited."""
        ev = None if condition_present else self.ks.latest_transient()
        if ev is None:
            self._reset_clear()
            return
        mono = float(self.monotonic())
        if (self._clear_row != ev.id or self._clear_since is None or not self._clear_since <= now
                or not self._clear_mono <= mono):          # new row / first clear tick / clock stepped back
            self._clear_row, self._clear_ticks, self._clear_since, self._clear_mono = ev.id, 0, now, mono
        self._clear_ticks += 1
        clear_s, clear_mono_s = now - self._clear_since, mono - self._clear_mono
        floor = self.cfg.auto_release_clear_s
        if self._clear_ticks < self.cfg.auto_release_clear_ticks or clear_s < floor or clear_mono_s < floor:
            return
        released = ev.reason
        if not self.ks.release_transient(ev.id, f"guardian auto-release: '{released}' clear for "
                                         f"{self._clear_ticks} ticks / {int(clear_s)}s", actor=ACTOR, ts=now):
            self._audit("release_kill_switch_refused", {"observed": released, "observed_id": ev.id,
                                                        "why": "latest kill-switch row changed"}, now)
            log.warning("guardian: auto-release refused: the latest kill-switch row is no longer %r", released)
            self._reset_clear()
            return
        rep.actions.append("release_kill_switch")
        self._audit("release_kill_switch", {"released": released, "clear_ticks": self._clear_ticks,
                                            "clear_s": round(clear_s, 3), "clear_mono_s": round(clear_mono_s, 3)},
                    now)
        log.warning("guardian: kill switch auto-released (%s clear for %d ticks / %ds)", released,
                    self._clear_ticks, int(clear_s))
        self._reset_clear()

    def _reset_clear(self) -> None:
        self._clear_row, self._clear_ticks, self._clear_since, self._clear_mono = None, 0, None, None

    def _flatten(self, cause: str, hard: bool, reason: str, account: AccountState | None) -> FlattenResult:
        return flatten_all(self.store, self.broker, self.oms, self.ks, cause=cause, hard=hard,
                           config=self.cfg.flatten, clock=self.clock, sleep=self.sleep, account=account,
                           reason=reason, depth_fn=self.depth_fn)

    def _watch_symbols(self, balances, positions, now: float) -> list[str]:
        rules = self.risk.rules
        if self.universe is None:
            return sorted(rules)
        out = {s for s, r in rules.items() if self._total(balances, r.base) > 0 or self._qty(positions, s) > 0}
        try:
            out |= {s for s in self.universe() if s in rules}
        except Exception as e:   # held symbols are still watched
            self._error("guardian.monitor", f"universe failed: {e!r}", now)
        return sorted(out)

    def _universe_only(self, syms, balances, positions) -> frozenset[str]:
        """Watched only because the bot may trade them (nothing held): a missing quote there is
        not an error row (it still counts toward stale_feed when nothing is held)."""
        if self.universe is None:
            return frozenset()
        rules = self.risk.rules
        return frozenset(s for s in syms
                         if self._total(balances, rules[s].base) <= 0 and self._qty(positions, s) <= 0)

    def _quotes(self, now: float, symbols: Iterable[str] | None = None,
                quiet: frozenset[str] = frozenset()) -> tuple[dict[str, Any], list[str]]:
        """Quotes usable for marking, and the symbols whose quote was dated in the future.

        Freshness is clamped to `now`: a quote up to `max_quote_future_skew_s` ahead counts as
        fresh as of now; one further ahead is invalid (not fresh, not used to mark). Values kept
        from earlier ticks are clamped too, so a backward clock step cannot leave a symbol "fresh"
        in the future and hide a frozen feed."""
        out, future = {}, []
        for sym in list(self._last_fresh):
            self._last_fresh[sym] = min(self._last_fresh[sym], now)
        for sym in (self.risk.rules if symbols is None else symbols):
            try:
                q = self.broker.quote(sym)
            except Exception as e:   # logged once per symbol until a quote succeeds again
                if sym not in self._quote_err_logged and sym not in quiet:
                    self._quote_err_logged.add(sym)
                    self._error("guardian.monitor", f"quote {sym} failed: {e!r}", now)
                continue
            self._quote_err_logged.discard(sym)
            ts = float(q.ts)
            if not ts <= now + self.cfg.max_quote_future_skew_s:   # also rejects NaN
                future.append(sym)
                if sym not in self._future_logged:
                    self._future_logged.add(sym)
                    self._error("guardian.monitor", f"quote {sym} dated {ts} is ahead of the clock {now}; "
                                "treated as missing", now)
                continue
            self._future_logged.discard(sym)
            out[sym] = q
            if q.bid > 0 and q.ask >= q.bid:
                self._last_fresh[sym] = max(self._last_fresh.get(sym, float("-inf")), min(ts, now))
        return out, future

    def _equity(self, balances, quotes, positions) -> tuple[Decimal, list[str]]:
        cash = balances.get(self.quote_ccy)
        equity = (cash.free + cash.locked) if cash is not None else _ZERO
        unmarked: list[str] = []
        marked_assets: set[str] = set()
        for sym, r in self.risk.rules.items():
            if r.quote != self.quote_ccy or r.base in marked_assets:
                continue
            qty = self._total(balances, r.base)
            if qty == 0:
                continue
            q = quotes.get(sym)
            if q is None:
                pos = positions.get(sym)
                if pos is None or pos.avg_price <= 0:
                    unmarked.append(r.base)
                    continue
                mark = pos.avg_price  # degraded; SOD/HWM are not written from it
                unmarked.append(r.base)
            else:
                mark = q.bid
            marked_assets.add(r.base)
            equity += qty * mark
        return equity, unmarked

    def _mismatches(self, balances, positions, now: float) -> dict[str, dict[str, Decimal]]:
        by_asset: dict[str, list[str]] = {}
        for sym, r in self.risk.rules.items():
            by_asset.setdefault(r.base, []).append(sym)
        # Only a RECENT mid-submit intent explains a lag; a stuck one must not mask a mismatch.
        in_flight = {rec.intent.symbol for rec in self.store.intents_in_states(_IN_FLIGHT)
                     if now - rec.updated_at <= self.cfg.in_flight_grace_s}
        out = {}
        for asset, syms in sorted(by_asset.items()):
            if in_flight & set(syms):  # an order is mid-submit: the ledger may lag the exchange
                self._mismatch_streak[asset] = 0
                continue
            ledger = sum((self._qty(positions, s) for s in syms), _ZERO)
            exch = self._total(balances, asset)
            step = Decimal(1).scaleb(-max(self.risk.rules[s].amount_precision for s in syms))
            tol = max(step * self.cfg.mismatch_abs_units, ledger * self.cfg.mismatch_rel_pct / _HUNDRED)
            if abs(exch - ledger) > tol:
                self._mismatch_streak[asset] = self._mismatch_streak.get(asset, 0) + 1
            else:
                self._mismatch_streak[asset] = 0
            if self._mismatch_streak[asset] >= self.cfg.mismatch_confirm_ticks:
                out[asset] = {"exchange": exch, "ledger": ledger, "tolerance": tol}
        return out

    def _needs_review(self, what: str, detail: Any, now: float) -> None:
        payload = {"what": what, "detail": detail}
        self._audit("needs_review", payload, now)
        self._error("guardian.needs_review", f"operator review needed: {what}", now, payload)
        log.error("guardian: operator review needed: %s %s", what, detail)

    def _reconcile(self, now: float) -> None:
        try:
            self.oms.reconcile(now=now)
        except Exception as e:
            self._error("guardian.monitor", f"reconcile failed: {e!r}", now)

    def _dust(self, sym: str, rules, balances, positions, quotes) -> bool:
        """Flat for exposure purposes: exchange total AND ledger quantity are dust under the shared
        rule (wallexbot/dust.py), marked at the bid (else the ledger's average price)."""
        pos = positions.get(sym)
        q = quotes.get(sym)
        mark = q.bid if q is not None else (pos.avg_price if pos is not None else None)
        floor = min_notional_for(rules, self.risk.config.risk)
        return all(is_dust(qty, rules, mark, floor)
                   for qty in (self._total(balances, rules.base), self._qty(positions, sym)))

    @staticmethod
    def _total(balances, asset: str) -> Decimal:
        b = balances.get(asset)
        return _ZERO if b is None else b.free + b.locked

    @staticmethod
    def _qty(positions, sym: str) -> Decimal:
        p = positions.get(sym)
        return _ZERO if p is None else p.quantity

    def _audit(self, kind: str, payload: Any, now: float) -> None:
        self.store.append_audit_event(ACTOR, kind, payload, ts=now)

    def _error(self, source: str, message: str, now: float, payload: Any = None) -> None:
        try:
            self.store.append_error(source, message, payload, ts=now)
        except Exception as e:
            log.error("guardian error write failed: %r (%s)", e, message)

