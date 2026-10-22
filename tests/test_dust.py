"""One dust rule (wallexbot/dust.py) for the risk engine, the bot and the guardian.

Regression: fees charged in the base asset leave ~1e-5 of the base after a full exit. The bot
treated that residue as flat, but RiskEngine._open_positions_limit counted any quantity > 0, so
after max_open_positions round trips no NEW symbol could ever be entered."""
from __future__ import annotations

from decimal import Decimal as D

from test_bot_helpers import ManualFeed, cheap_config

from wallexbot.bot.loop import Bot
from wallexbot.bot.main import build_stack
from wallexbot.data.candles import Candle
from wallexbot.dust import is_dust, min_notional_for
from wallexbot.exchange.mock_wallex import MockClock, MockWallex
from wallexbot.guardian.main import build_guardian
from wallexbot.risk.engine import AccountState
from wallexbot.store import Store
from wallexbot.strategy.contract import ExitDecision, Signal
from wallexbot.strategy.cooldown import CooldownTracker
from wallexbot.strategy.sizing import min_notional_for as sizing_min_notional
from wallexbot.strategy.sizing import size_position
from wallexbot.types import IntentState, OrderIntent, OrderType, Position, Quote, Side, SymbolRules

B = 900
T = 1_700_000_100 // B * B
S = IntentState


class _Engine:
    def __init__(self):
        self.on = True
        self.only: str | None = None

    def evaluate(self, sym, bars, mc):
        if not self.on or (self.only is not None and sym != self.only):
            return None
        last = bars[-1]
        return Signal(sym, last.ts, last.close, last.close, D("1"), last.close - D(2), D("0.9"))


class _Hold:
    def on_bar(self, st, bars):
        return ExitDecision("hold")

    def targets(self, st):
        return ()


def _bars(last_open):
    p = D(100)
    return [Candle(last_open - i * B, p, p + 1, p - 1, p, D(1000)) for i in range(60, -1, -1)]


def test_rule():
    r = SymbolRules("XUSDT", "X", "USDT", amount_precision=4, price_precision=2)
    assert is_dust(D("0.00009"), r, D("100"), D("5"))           # below one amount step
    assert is_dust(D("0.0400"), r, D("100"), D("5"))            # 4 USDT < 5 USDT minimum
    assert not is_dust(D("0.0500"), r, D("100"), D("5"))        # exactly the minimum
    assert not is_dust(D("0.0400"), r, None, D("5"))            # no mark: only the step rule
    assert is_dust(D("0"), r, None, D("5"))
    risk = cheap_config().risk
    with_min = SymbolRules("XUSDT", "X", "USDT", 4, 2, min_notional=risk.min_notional * 2)
    assert min_notional_for(with_min, risk) == risk.min_notional * 2
    assert sizing_min_notional is min_notional_for           # sizing re-exports the same rule


def test_risk_open_positions_ignore_dust():
    from wallexbot.risk.engine import RiskEngine
    from wallexbot.risk.killswitch import KillSwitch

    class _KS:
        def allows(self, intent):
            return True
    cfg = cheap_config()
    syms = [f"S{i:02d}USDT" for i in range(4)]
    rules = {s: SymbolRules(s, s[:3], "USDT", amount_precision=4, price_precision=2) for s in syms}
    eng = RiskEngine(cfg, rules, _KS())
    assert KillSwitch  # imported for symmetry with the other risk tests
    dust = {s: Position(s, D("0.0000120"), D("100")) for s in syms[:3]}
    acct = AccountState(D(10000), D(10000), D(10000), "2023-11-14", D(10000),
                        marks={s: D("100") for s in syms})
    now = T + 5
    q = Quote(syms[3], D("100.00"), D("100.02"), now)
    intent = OrderIntent(syms[3], Side.BUY, OrderType.LIMIT, D("1.0000"), D("100.02"), "sig_x")
    d = eng.check(intent, q, acct, dust, [], now)
    assert "max_open_positions" not in d.reasons, d.reasons
    real = {s: Position(s, D("1.0000"), D("100")) for s in syms[:3]}
    assert "max_open_positions" in eng.check(intent, q, acct, real, [], now).reasons


def test_fourth_symbol_enters_after_three_round_trips(tmp_path):
    cfg = cheap_config()
    assert cfg.risk.max_open_positions == 3
    syms = [f"S{i:02d}USDT" for i in range(4)]
    rules = {s: SymbolRules(s, s[:3], "USDT", amount_precision=7, price_precision=2) for s in syms}
    clock = MockClock(T + 5)
    broker = MockWallex(rules.values(), clock=clock, balances={"USDT": D(10000)},
                        maker_rate=cfg.maker_rate, taker_rate=cfg.taker_rate)
    feed = ManualFeed(cfg, syms)
    store = Store(tmp_path / "d.db", clock=clock)
    ks, risk, oms = build_stack(store, broker, rules, cfg, clock)
    eng = _Engine()
    bot = Bot(feed, eng, _Hold(), size_position, oms, risk, store, ks, cfg, clock)

    def price(sym, bid):
        feed.set_quote(sym, str(bid), str(bid + D("0.02")), clock())
        broker.set_price(sym, bid, bid + D("0.02"))
    for s in syms:
        feed.bars[s] = _bars(T - B)
        price(s, D("100.00"))
    bot.tick()
    held = sorted(bot.positions)
    assert len(held) == 3 and syms[3] not in held          # max_open_positions binds
    eng.on = False
    for s in held:                                          # stop out all three
        price(s, bot.positions[s].current_stop - 1)
    for _ in range(3):
        clock.advance(30)
        for s in held:
            price(s, feed.quotes[s].bid)
        bot.tick()
    assert bot.positions == {}
    ledger = store.latest_positions()
    assert all(0 < ledger[s].quantity < D("0.001") for s in held), ledger   # fee dust remains
    # the guardian agrees: nothing held (dust only), no flatten on a breach-free tick
    for s in syms:
        price(s, feed.quotes[s].bid)
    gstore = Store(tmp_path / "d.db", clock=clock)
    g = build_guardian(gstore, broker, rules, cfg, clock=clock, sleep=clock.advance)
    rep = g.tick()
    assert not ks.is_engaged(), rep.conditions
    assert not any(a.startswith("flatten") for a in rep.actions)
    assert not any(c.startswith("balance_mismatch") for c in rep.conditions)
    # a new bar with a signal on the fourth symbol only: the three dusts do not block it
    eng.on, eng.only = True, syms[3]
    bot.cooldown = CooldownTracker(B, 0, 0)
    clock.advance(3 * B)
    for s in syms:
        feed.bars[s] = _bars(T + 2 * B)
        price(s, D("100.00"))
    bot.tick()
    new = [r for r in store.intents_in_states(frozenset(S))
           if r.intent.symbol == syms[3] and r.intent.side is Side.BUY]
    assert new and new[-1].state is S.FILLED, [(r.state, r.intent.symbol) for r in new]
    reasons = [d["reasons"] for d in store.rows("risk_decisions")]
    assert not any("max_open_positions" in r for r in reasons[-4:])
