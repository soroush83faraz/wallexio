#!/usr/bin/env python3
"""Pre-deploy smoke test: one forced round trip through the REAL shadow stack on the LIVE public book.

    python deploy/smoke_shadow_fill.py SCRATCH_DIR [--balance 1000] [--hold-s 45] [--max-wall-s 1500]

SMOKE TEST - forced test signal - SHADOW ONLY - NOT A STRATEGY RESULT.

What is real: PublicFeed (public GETs to api.wallex.ir only), the bot's own universe filter
(spread / depth / zero-volume), Bot, sizing, RiskEngine, OMS, ShadowBroker (simulated fills against the
live book), the in-process guardian, the audit store, the E4 exit policy and its software stop.
What is forced (test only): the ENTRY DECISION. `ForcedSignalEngine` replaces MomentumSignalEngine at
exactly the place `build_bot` puts it (Bot's second constructor argument) and emits ONE Signal for
the most liquid symbol that passed the bot's universe filter, on the first closed 15m bar. The EXIT
is the real E4 policy wrapped by `ForcedExitPolicy`, which adds one extra take-profit level after
`--hold-s` seconds so the round trip completes in minutes instead of hours; E4's own bar rules, the
software stop and its targets still apply unchanged, and the exit still goes through bot -> OMS ->
ShadowBroker. No production module is modified.

Nothing here can reach a live order path: ShadowBroker is asserted, any mode but shadow is refused,
no credential may be present, and this file contains no order-entry call (it lives outside src/, so
tests/test_single_order_path_ast.py does not scan it; it must stay that way: it never submits).

Exit codes: 0 every invariant held, 1 an invariant failed (or the wait ran out), 2 refused.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import signal
import sqlite3
import sys
import threading
import time
from argparse import Namespace
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from wallexbot.bot.loop import Bot, simple_atr
from wallexbot.bot.main import (BotSetupError, StopFlag, _guarded_sleep, build_stack, fresh_depth_source,
                                interval_problem, make_broker, make_guardian, pre_tick, summarize)
from wallexbot.bot.feed import BAR_S, TRANSIENT_FEED_ERRORS
from wallexbot.config import Config, ConfigError, load_config
from wallexbot.data.candles import Candle
from wallexbot.dust import is_dust, min_notional_for
from wallexbot.exchange.shadow import ShadowBroker
from wallexbot.store import Store
from wallexbot.strategy.contract import MarketContext, PositionState, Signal
from wallexbot.strategy.exits import ExitParams, TakeProfit, make_policy
from wallexbot.strategy.sizing import size_position
from wallexbot.types import IntentState, Side, SymbolRules, TERMINAL_STATES, round_down

BANNER = "SMOKE TEST - forced test signal - SHADOW ONLY - NOT A STRATEGY RESULT"
ENGINE_NAME = "smoke_forced"
PREFERRED_SYMBOL = "BTCUSDT"
STOP_ATR_MULT = Decimal("1.5")
ATR_PERIOD = 14
MIN_WARM_BARS = ATR_PERIOD + 2
SECRET_ENV = re.compile(r"(?i)^WALLEX_(API_KEY|ARM_LIVE|SECRET)|^WALLEX_.*(KEY|SECRET|TOKEN)")
SECRET_TEXT = re.compile(r"(?i)bearer\s+[a-z0-9._~+/=-]{20,}|sk-ant-|-----BEGIN [A-Z ]*PRIVATE KEY|"
                         r"(?:api[_-]?key|secret|token|passw(?:or)?d)[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9_\-]{16,}")
_ZERO = Decimal(0)
_BPS = Decimal(10000)


class SmokeRefused(RuntimeError):
    pass


# ---------------------------------------------------------------------------- test-only strategy pieces
class ForcedSignalEngine:
    """TEST ONLY. Implements strategy.contract.SignalEngine. Emits exactly one valid Signal for
    `symbol` on the first bar it is asked about (the first closed 15m bar after start), never again.

    entry_ref_price is the live mid when `ref_price` is given (the bar close can be up to 15 minutes
    old and the bot refuses entries whose ask ran > 0.5 ATR above it), else the bar close. The stop
    is entry - 1.5 * ATR(14) of the last bars, rounded down to the price precision: the same
    construction the real strategy uses. The reasons say plainly that this signal is forced."""

    name = ENGINE_NAME
    version = "smoke_forced-1"

    def __init__(self, symbol: str, rules: SymbolRules, ref_price: Callable[[str], Decimal | None] | None = None):
        self.symbol, self.rules, self.ref_price = symbol, rules, ref_price
        self.fired = False
        self.calls: list[str] = []

    def evaluate(self, symbol: str, bars: Sequence[Candle], market: MarketContext) -> Signal | None:
        self.calls.append(symbol)
        if self.fired or symbol != self.symbol:
            return None
        atr = simple_atr(bars, ATR_PERIOD)
        if atr is None or atr <= 0:
            raise ValueError("not enough closed bars for an ATR")   # engine_error: surfaced, never silent
        last = bars[-1]
        entry = (self.ref_price(symbol) if self.ref_price is not None else None) or last.close
        stop = round_down(entry - STOP_ATR_MULT * atr, self.rules.price_precision)
        if not 0 < stop < entry:
            raise ValueError("no valid stop below the entry")
        self.fired = True
        return Signal(symbol, last.ts, entry, entry - atr, atr, stop, Decimal("0.5"),
                      {"engine": ENGINE_NAME, "forced": "true", "note": "TEST SIGNAL, NOT A STRATEGY RESULT"},
                      strategy_version=self.version)


class ForcedExitPolicy:
    """TEST ONLY. Wraps the real exit policy (E4): on_bar and targets are delegated unchanged. In
    addition, `hold_s` seconds after the bot first asks for the position's targets (the poll after the
    fill is adopted), targets() also returns a take-profit at price ~0 for the whole position, which
    the bot's own _protect stage turns into an exit request -> OMS -> ShadowBroker."""

    name = "E4+forced_after_hold"

    def __init__(self, inner, clock: Callable[[], float], hold_s: float):
        self.inner, self.clock, self.hold_s = inner, clock, float(hold_s)
        self.first_seen: dict[str, float] = {}

    def on_bar(self, state: PositionState, bars: Sequence[Candle]):
        return self.inner.on_bar(state, bars)

    def targets(self, state: PositionState) -> tuple[TakeProfit, ...]:
        now = float(self.clock())
        t0 = self.first_seen.setdefault(state.symbol, now)
        real = tuple(self.inner.targets(state))
        if now - t0 >= self.hold_s:
            return (TakeProfit(Decimal("0.00000001"), Decimal(1), "smoke_forced_exit"), *real)
        return real


# ---------------------------------------------------------------------------- invariants
@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class SmokeResult:
    checks: list[Check] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    report: dict[str, Any] = field(default_factory=dict)
    symbol: str | None = None
    ticks: int = 0
    wall_s: float = 0.0
    stopped_because: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks)

    def add(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append(Check(name, bool(ok), detail))
        return bool(ok)


def load_healthcheck():
    spec = importlib.util.spec_from_file_location("deploy_healthcheck", Path(__file__).with_name("healthcheck.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _cash_flows(fills: Sequence[Mapping[str, Any]], quote: str) -> Decimal:
    """Signed quote-currency cash flows of fill rows, quote-asset fees included (exact)."""
    total = _ZERO
    for f in fills:
        amount, fee = Decimal(f["quote_amount"]), Decimal(f["fee"])
        fee_q = fee if f["fee_asset"] == quote else _ZERO
        total += (amount - fee_q) if f["side"] == "SELL" else -(amount + fee_q)
    return total


def read_journal(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def fill_report(journal: Sequence[Mapping[str, Any]], decisions: Mapping[str, Any]) -> dict[str, Any]:
    """Per order: fill VWAP vs the mid at decision (and the quote the signal saw), slippage in bps,
    fee. Everything comes from the shadow journal, which records each level taken."""
    orders: dict[str, dict[str, Any]] = {}
    for rec in journal:
        if rec["event"] == "accept":
            orders[rec["client_id"]] = {"side": rec["side"], "type": rec["type"], "limit": rec.get("price"),
                                        "mid_at_decision": rec["mid_at_decision"], "qty": _ZERO, "quote": _ZERO,
                                        "fee": _ZERO, "fee_asset": None, "levels": 0, "liquidity": set()}
        elif rec["event"] == "fill" and rec["client_id"] in orders:
            o = orders[rec["client_id"]]
            o["qty"] += Decimal(rec["quantity"])
            o["quote"] += Decimal(rec["quote"])
            o["fee"] += Decimal(rec["fee"])
            o["fee_asset"] = rec["fee_asset"]
            o["levels"] += 1
            o["liquidity"].add(rec["liquidity"])
    out = {}
    for cid, o in orders.items():
        if o["qty"] <= 0:
            continue
        vwap, mid = o["quote"] / o["qty"], Decimal(o["mid_at_decision"])
        sign = 1 if o["side"] == "BUY" else -1
        out[cid] = {"side": o["side"], "type": o["type"], "qty": str(o["qty"]), "levels": o["levels"],
                    "fill_vwap": str(vwap.quantize(Decimal("0.0001"))), "mid_at_decision": str(mid),
                    "slippage_bps_vs_mid": str(((vwap - mid) / mid * _BPS * sign).quantize(Decimal("0.01"))),
                    "fee": f"{o['fee']} {o['fee_asset']}", "liquidity": sorted(o["liquidity"])}
    out["_signal_market"] = decisions.get("market")
    return out


def check_invariants(res: SmokeResult, store: Store, broker, cfg: Config, rules: Mapping[str, SymbolRules],
                     symbol: str, balance: Decimal, db: Path, journal: Path, guardian, healthcheck,
                     now: float, stdout_text: str = "") -> None:
    quote, base = cfg.quote_currency, rules[symbol].base
    intents = store.intents_in_states(frozenset(IntentState))
    entries = [r for r in intents if r.intent.side is Side.BUY]
    exits = [r for r in intents if r.intent.side is Side.SELL]
    res.add("exactly one entry intent, terminal FILLED",
            len(entries) == 1 and entries[0].state is IntentState.FILLED,
            f"{[(r.intent.signal_id[:12], r.state.value) for r in entries]}")
    res.add("exactly one exit intent, terminal FILLED",
            len(exits) == 1 and exits[0].state is IntentState.FILLED,
            f"{[(r.intent.signal_id[:14], r.state.value) for r in exits]}")
    res.add("no non-terminal intent left", all(r.state in TERMINAL_STATES for r in intents),
            f"{sorted({r.state.value for r in intents})}")
    res.add("single order path: one broker order per intent (via the OMS)", broker.submit_calls == len(intents),
            f"broker submits {broker.submit_calls}, intents {len(intents)}")

    fills = store.rows("fills")
    pos = store.latest_positions().get(symbol)
    cash = _cash_flows(fills, quote)
    # A BUY fee is paid in the base asset, so a sub-step dust quantity can stay in the ledger after the exit
    # (the bot's is_dust rule treats it as flat). Its cost basis is still unreleased: realized P&L is exactly
    # the cash flows plus that remaining basis (and equals the cash flows when the ledger is exactly flat).
    res.add("position flat up to dust (the bot's own is_dust rule)",
            pos is not None and is_dust(pos.quantity, rules[symbol], pos.avg_price or Decimal(1),
                                        min_notional_for(rules[symbol], cfg.risk)),
            f"ledger quantity {None if pos is None else pos.quantity}")
    dust_basis = _ZERO if pos is None else Decimal(str(pos.meta.get("cost_basis", 0)))
    res.add("realized P&L == exact quote cash flows incl. fees (+ unreleased dust cost basis)",
            pos is not None and pos.realized_pnl == cash + dust_basis,
            f"realized {None if pos is None else pos.realized_pnl}, cash flows {cash}, dust {None if pos is None else pos.quantity} "
            f"{base} with cost basis {dust_basis}")
    bal = broker.balances()
    q_total = bal[quote].free + bal[quote].locked if quote in bal else _ZERO
    b_total = bal[base].free + bal[base].locked if base in bal else _ZERO
    res.add("ledger == shadow balances (quote: start + cash flows; base: ledger quantity)",
            q_total == balance + cash and pos is not None and b_total == pos.quantity,
            f"{quote} shadow {q_total} vs {balance + cash}; {base} shadow {b_total} vs ledger "
            f"{None if pos is None else pos.quantity}")
    ks = store.latest_killswitch()
    ks_rows = store.rows("killswitch_events")
    res.add("kill switch never engaged", not ks_rows and (ks is None or not ks.engaged),
            f"{len(ks_rows)} kill switch event(s)" + ("" if ks is None else f", latest: {ks.reason}"))
    res.add("guardian in-process ran with 0 failures",
            guardian is not None and guardian.ticks > 0 and guardian.failed_ticks == 0,
            "disabled" if guardian is None else f"ticks {guardian.ticks}, failed {guardian.failed_ticks}")
    res.add("no bot errors recorded", not store.rows("errors"),
            f"{[(e['source'], e['message'][:60]) for e in store.rows('errors')[:3]]}")

    engine_ok = all(e["payload"].get("profile") == ENGINE_NAME for e in store.audit_events(actor="bot"))
    started = [e for e in store.audit_events(actor="bot", kind="startup")]
    res.add(f"audit events carry profile/engine {ENGINE_NAME!r}",
            bool(started) and engine_ok and started[-1]["payload"]["run"].get("engine") == ENGINE_NAME,
            f"{len(store.audit_events(actor='bot'))} bot events")

    args = Namespace(db=str(db), max_heartbeat_age_s=120.0, alert_window_min=15.0, allow_killswitch=False,
                     max_feed_outage_s=900.0, recorder_dir=None, max_recorder_age_s=300.0)
    code, status = healthcheck.run(args, now=now)
    res.add("healthcheck ok", code == 0 and status.get("ok") is True,
            f"problems {status.get('problems')}, warnings {status.get('warnings')}")

    hay = [db.read_bytes().decode("utf-8", "ignore"), stdout_text,
           journal.read_text(encoding="utf-8") if journal.exists() else ""]
    leaks = [i for i, t in enumerate(hay) if SECRET_TEXT.search(t)]
    res.add("no secrets in the database, journal or output", not leaks and not _secret_env(),
            f"matches in {['db', 'output', 'journal'][leaks[0]] if leaks else 'none'}")


def _secret_env(env: Mapping[str, str] | None = None) -> list[str]:
    return sorted(k for k, v in (os.environ if env is None else env).items() if v and SECRET_ENV.search(k))


# ---------------------------------------------------------------------------- the run
def pick_symbol(universe: Sequence[str]) -> str:
    """BTCUSDT if the bot's own universe filter kept it, else the most liquid symbol that passed (the
    universe is ranked by 24h quote volume)."""
    if not universe:
        raise SmokeRefused("the bot's universe filter kept no symbol (spread / depth / volume): nothing to test on")
    return PREFERRED_SYMBOL if PREFERRED_SYMBOL in universe else universe[0]


def warm_up(feed, clock, sleep, *, max_s: float = 240.0, poll_s: float = 3.0) -> str:
    """Refresh the feed until its universe exists and the chosen symbol has enough closed bars for the
    ATR. The bot treats a bar it evaluated as done, so it must not see the first bar half loaded."""
    t0 = float(clock())
    last = ""
    while float(clock()) - t0 <= max_s:
        try:
            feed.refresh(float(clock()))
        except TRANSIENT_FEED_ERRORS as e:
            last = f"{type(e).__name__}: {e}"
        syms = feed.symbols()
        if syms:
            sym = pick_symbol(syms)
            if len(feed.closed_bars(sym, 60)) >= MIN_WARM_BARS and feed.quote(sym) is not None:
                return sym
        sleep(poll_s)
    raise SmokeRefused(f"feed not ready after {max_s:.0f}s (universe {feed.symbols()}, last error {last or 'none'})")


def run_smoke(workdir: str | Path, cfg: Config, feed, rules: Mapping[str, SymbolRules], *,
              clock: Callable[[], float] = time.time, sleep: Callable[[float], Any] | None = None,
              balance: Decimal = Decimal("1000"), interval_s: float = 12.0, hold_s: float = 45.0,
              max_wall_s: float = 1500.0, guardian_interval_s: float = 15.0, stop: StopFlag | None = None,
              out: Callable[[str], Any] = print) -> SmokeResult:
    work = Path(workdir)
    work.mkdir(parents=True, exist_ok=True)
    db, journal = work / "smoke.db", work / "shadow_journal.jsonl"
    if db.exists() or journal.exists():
        raise SmokeRefused(f"{work} already holds a smoke database / journal; use an empty scratch directory")
    problem = interval_problem(interval_s, cfg)
    if problem is not None:
        raise SmokeRefused(problem)
    if cfg.mode != "shadow" and cfg.mode != "mock":      # default_config.yaml says mock; live is never allowed
        raise SmokeRefused(f"config mode {cfg.mode!r}: only shadow runs are allowed")
    stop = stop or StopFlag()
    sleep = sleep or stop.wait
    captured: list[str] = []

    def say(msg: str) -> None:
        captured.append(msg)
        out(msg)

    say(f"*** {BANNER} ***")
    res = SmokeResult()
    symbol = warm_up(feed, clock, sleep)
    res.symbol = symbol
    say(f"universe kept by the bot's own filter: {feed.symbols()}; forced signal on {symbol}")
    broker = make_broker("shadow", config=cfg, clock=clock, rules=rules, balances={cfg.quote_currency: balance},
                         journal_path=journal, book_source=fresh_depth_source(feed, clock))
    assert type(broker) is ShadowBroker, f"refusing: broker is {type(broker).__name__}, not ShadowBroker"
    store = Store(db, clock=clock)
    gd = None
    t_start = float(clock())
    try:
        ks, risk, oms = build_stack(store, broker, rules, cfg, clock)
        assert type(oms.broker) is ShadowBroker
        engine = ForcedSignalEngine(symbol, rules[symbol],
                                    lambda s: None if feed.quote(s) is None else (feed.quote(s).bid + feed.quote(s).ask) / 2)
        policy = ForcedExitPolicy(make_policy("E4", ExitParams(fee_rate=cfg.taker_rate)), clock, hold_s)
        run_info = {"mode": "shadow", "profile": ENGINE_NAME, "engine": ENGINE_NAME, "forced_test_signal": True,
                    "guardian": {"enabled": True, "in_process": True, "interval_s": guardian_interval_s,
                                 "stale_action": "hold"}}
        # The engine goes exactly where MomentumSignalEngine goes in bot.main.build_bot: argument 2 of Bot.
        # Bot refuses unknown profiles, so it is built as "strict" and relabelled before its first tick:
        # every audit row then says profile == smoke_forced.
        bot = Bot(feed, engine, policy, size_position, oms, risk, store, ks, cfg, clock, None, profile="strict",
                  run_info=run_info)
        bot.profile = bot.state.profile = bot.run_info["profile"] = ENGINE_NAME
        gd = make_guardian(True, db, broker, rules, cfg, clock, sleep, guardian_interval_s,
                           lambda: set(feed.symbols()) | set(bot.positions),
                           monotonic=None if clock is time.time else clock)
        store.append_audit_event("smoke", "banner", {"banner": BANNER, "engine": ENGINE_NAME, "symbol": symbol,
                                                     "hold_s": hold_s, "balance": balance})
        say(f"smoke: engine={ENGINE_NAME} exit policy={policy.name} hold_s={hold_s:g} interval_s={interval_s:g} "
            f"balance={balance} {cfg.quote_currency} taker_rate={cfg.taker_rate} db={db}")
        ticks = 0
        while True:
            pre_tick(feed, broker, clock)
            rep = bot.tick()
            gd.tick()
            ticks += 1
            if rep.actions or rep.errors:
                say(f"tick {ticks} actions={rep.actions} errors={rep.errors}")
            res.stopped_because = _stop_reason(bot, store, symbol, float(clock()) - t_start, max_wall_s, stop, rep)
            if res.stopped_because:
                break
            _guarded_sleep(sleep, interval_s, gd, stop)
        res.ticks, res.wall_s = ticks, float(clock()) - t_start
        res.summary = summarize(store, bot, ticks, gd)
        say(f"loop ended: {res.stopped_because} after {ticks} ticks, {res.wall_s:.0f}s")
        decisions = _signal_decision(store)
        res.report = fill_report(read_journal(journal), decisions)
        _print_report(say, res, store, symbol)
        res.add("round trip completed in time", res.stopped_because == "round_trip_complete", res.stopped_because)
        check_invariants(res, store, broker, cfg, rules, symbol, balance, db, journal, gd, load_healthcheck(),
                         float(clock()), "\n".join(captured))
        store.append_audit_event("smoke", "result", {"ok": res.ok, "checks": [(c.name, c.ok) for c in res.checks]})
    finally:
        if gd is not None:
            gd.close()
        store.close()
    for c in res.checks:
        say(f"  [{'PASS' if c.ok else 'FAIL'}] {c.name}" + ("" if c.ok else f"  -> {c.detail}"))
    say("SMOKE RESULT: " + ("PASS" if res.ok else "FAIL") + "  (" + BANNER + ")")
    return res


def _stop_reason(bot: Bot, store: Store, symbol: str, elapsed: float, max_wall_s: float, stop: StopFlag, rep) -> str:
    intents = store.intents_in_states(frozenset(IntentState))
    buys = [r for r in intents if r.intent.side is Side.BUY]
    sells = [r for r in intents if r.intent.side is Side.SELL]
    if stop.is_set():
        return "interrupted"
    if bot.ks.is_engaged():
        return "kill_switch_engaged"
    if buys and buys[0].state in TERMINAL_STATES and buys[0].state is not IntentState.FILLED:
        return f"entry_{buys[0].state.value}"
    if not buys and symbol in bot.bars_done and not bot.planned:
        why = [e["payload"].get("reason") for e in store.audit_events(actor="bot")
               if e["kind"] in ("entry_skipped", "abstain") and e["payload"].get("symbol") == symbol][-1:]
        return f"no_entry_planned:{why[0] if why else 'unknown'}"
    if buys and sells and all(r.state in TERMINAL_STATES for r in buys + sells) and symbol not in bot.positions \
            and not bot.plans:
        return "round_trip_complete"
    if elapsed >= max_wall_s:
        return "timeout"
    return ""


def _signal_decision(store: Store) -> dict[str, Any]:
    sigs = store.audit_events(actor="bot", kind="signal")
    return sigs[-1]["payload"] if sigs else {}


def _print_report(say, res: SmokeResult, store: Store, symbol: str) -> None:
    say("---- fills vs the live public book (from the shadow journal) ----")
    mk = res.report.get("_signal_market")
    if mk:
        say(f"signal-time market context: {mk}")
    for cid, r in res.report.items():
        if cid.startswith("_"):
            continue
        say(f"{r['side']:4} {r['type']:6} qty {r['qty']} @ VWAP {r['fill_vwap']} over {r['levels']} level(s) | "
            f"mid at decision {r['mid_at_decision']} | slippage vs mid {r['slippage_bps_vs_mid']} bps | "
            f"fee {r['fee']} | {','.join(r['liquidity'])}")
    pos = store.latest_positions().get(symbol)
    fills = store.rows("fills")
    fees = sum((Decimal(f["fee_quote"]) for f in fills), _ZERO)
    if pos is not None:
        say(f"realized P&L {pos.realized_pnl} (net of fees {fees} quote-equivalent) over {len(fills)} fill(s); "
            f"ledger quantity {pos.quantity}")
    say("-------------------------------------------------------------------")


# ---------------------------------------------------------------------------- CLI
def refuse(msg: str) -> int:
    print(f"refused: {msg}", file=sys.stderr)
    return 2


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=BANNER)
    p.add_argument("scratch_dir", help="EMPTY scratch directory for the throwaway database and shadow journal")
    p.add_argument("--mode", default="shadow", help="shadow only; anything else is refused")
    p.add_argument("--balance", default="1000", help="starting shadow balance in USDT (1000)")
    p.add_argument("--hold-s", type=float, default=45.0, help="seconds to hold before the forced exit level (45)")
    p.add_argument("--interval-s", type=float, default=12.0, help="wall seconds between ticks (12, max 15)")
    p.add_argument("--max-wall-s", type=float, default=1500.0, help="give up after this many seconds (1500)")
    p.add_argument("--config", default=None)
    args = p.parse_args(argv)
    if args.mode != "shadow":
        return refuse(f"mode {args.mode!r}: the smoke test runs in shadow mode only")
    leaked = _secret_env()
    if leaked:
        return refuse(f"credential-like environment variables are set ({', '.join(leaked)}); unset them: "
                      "this test needs none and must never run next to any")
    try:
        cfg = load_config(args.config, env={})
        balance = Decimal(args.balance)
    except (ConfigError, OSError, ArithmeticError, ValueError, KeyError) as e:
        return refuse(f"bad configuration: {e}")
    if not 0 < args.max_wall_s <= 3600 or args.hold_s < 0 or balance <= 0:
        return refuse("--max-wall-s must be in (0, 3600], --hold-s >= 0, --balance > 0")
    from wallexbot.bot.feed import PublicFeed
    from wallexbot.exchange.wallex_public import WallexPublicClient
    stop = StopFlag()
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
    try:
        client = WallexPublicClient()
        rules = {r.symbol: r for r in client.markets() if r.quote == cfg.quote_currency}
        res = run_smoke(args.scratch_dir, cfg, PublicFeed(client, cfg), rules, balance=balance,
                        interval_s=args.interval_s, hold_s=args.hold_s, max_wall_s=args.max_wall_s, stop=stop)
    except (SmokeRefused, BotSetupError) as e:
        return refuse(str(e))
    return 0 if res.ok else 1


if __name__ == "__main__":
    sys.exit(main())
