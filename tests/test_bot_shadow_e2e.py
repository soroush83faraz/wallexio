"""Real ShadowBroker + ReplayFeed, offline, end to end: signal -> sizing -> risk -> OMS -> ShadowBroker -> exit.

The shadow broker simulates fills from the feed's own cached depth (no extra requests); orders become
eligible after its latency and are matched by pre_tick() (feed refresh + broker.step) before each tick."""
import json
from decimal import Decimal

import pytest
from test_bot_helpers import ENTRY_TS, RULES, SIGNAL_BAR, STEP, SYM, T0, WINNER, cheap_config, series

from wallexbot.bot import main as bot_main
from wallexbot.bot.feed import ReplayFeed, explode_bars
from wallexbot.exchange.mock_wallex import MockClock
from wallexbot.exchange.shadow import ShadowBroker, shadow_report
from wallexbot.store import Store
from wallexbot.types import IntentState, Side

D = Decimal
END = T0 + (SIGNAL_BAR + 1 + len(WINNER)) * STEP + 60


def _rig(tmp_path, journal=None):
    cfg = cheap_config()
    clock = MockClock(T0 + 121 * STEP)
    feed = ReplayFeed({SYM: explode_bars(series(WINNER))}, RULES, cfg)
    source = bot_main.fresh_depth_source(feed, clock)
    broker = bot_main.make_broker("shadow", config=cfg, clock=clock, rules=RULES, book_source=source,
                                  balances={"USDT": D("1000")}, journal_path=journal)
    store = Store(tmp_path / "shadow.db", clock=clock)
    ks, risk, oms = bot_main.build_stack(store, broker, RULES, cfg, clock)
    bot = bot_main.build_bot(feed, oms, risk, store, ks, cfg, clock)
    return cfg, clock, feed, broker, store, bot


def _run(clock, feed, broker, bot, end):
    reps = []
    while clock() < end:
        bot_main.pre_tick(feed, broker, clock)
        reps.append(bot.tick())
        clock.advance(60.0)
    return reps


def test_shadow_round_trip_over_replayed_book(tmp_path):
    journal = tmp_path / "shadow_journal.jsonl"
    cfg, clock, feed, broker, store, bot = _rig(tmp_path, journal)
    assert isinstance(broker, ShadowBroker)
    reps = _run(clock, feed, broker, bot, END)
    assert not any(r.errors for r in reps)
    intents = store.intents_in_states(frozenset(IntentState))
    entries = [r for r in intents if r.intent.side is Side.BUY]
    exits = [r for r in intents if r.intent.side is Side.SELL]
    assert len(entries) == 1 and entries[0].state is IntentState.FILLED
    assert entries[0].created_at == ENTRY_TS
    assert exits and all(r.state is IntentState.FILLED for r in exits)
    assert broker.submit_calls == len(intents)                       # one broker order per intent, via the OMS
    pos = store.latest_positions()[SYM]
    assert pos.quantity < D("0.0001") and pos.realized_pnl != 0
    # the simulated account moved: USDT was spent and recovered, the base asset is (almost) flat
    bal = broker.balances()
    assert bal["USDT"].free > 0 and bal["USDT"].free != D("1000")
    # journal: every simulated event is appended locally as JSONL and summarised by shadow_report
    lines = [json.loads(x) for x in journal.read_text().splitlines()]
    assert lines and shadow_report(journal)
    store.close()


def test_stale_or_missing_book_moves_nothing(tmp_path):
    cfg, clock, feed, broker, store, bot = _rig(tmp_path)
    src = bot_main.fresh_depth_source(feed, clock, max_age_s=5.0)
    with pytest.raises(LookupError):
        src(SYM)                                                      # nothing cached before the first refresh
    feed.refresh(clock())
    assert src(SYM).ts <= clock()
    clock.advance(6.0)
    with pytest.raises(LookupError, match="stale"):
        src(SYM)
    store.close()


def test_pre_tick_is_a_noop_for_brokers_without_step(tmp_path):
    class Plain:
        pass

    class Feed:
        def refresh(self, now):
            raise AssertionError("must not refresh")

    bot_main.pre_tick(Feed(), Plain(), MockClock())
