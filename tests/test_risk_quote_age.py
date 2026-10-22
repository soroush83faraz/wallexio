"""Entry-quote timestamp/price validation and clock-step handling in the risk engine."""
from dataclasses import replace
from decimal import Decimal

import pytest

from wallexbot.risk.engine import AccountState, RiskEngine, utc_day
from wallexbot.risk.killswitch import KillSwitch
from wallexbot.types import OrderIntent, OrderType, Position, Quote, Side

D = Decimal
NOW = 1_760_000_000.0
SYM = "BTCUSDT"
QUOTE = Quote(SYM, D("60000.00"), D("60010.00"), NOW)
ACCOUNT = AccountState(equity=D("10000"), cash_free=D("10000"), sod_equity=D("10000"),
                       sod_day=utc_day(NOW), hwm_equity=D("10000"))
HELD = {SYM: Position(SYM, D("0.010000"), D("60000"))}


@pytest.fixture
def ks(store):
    return KillSwitch(store)


@pytest.fixture
def engine(cfg, rules, ks):
    return RiskEngine(cfg, rules, ks)


def entry():
    return OrderIntent(symbol=SYM, side=Side.BUY, type=OrderType.LIMIT, quantity=D("0.010000"),
                       price=D("60010.00"), signal_id="s")


def exit_(type_=OrderType.MARKET):
    return OrderIntent(symbol=SYM, side=Side.SELL, type=type_, quantity=D("0.010000"), price=None,
                       signal_id="x", reduce_only=True)


def run(engine, quote=QUOTE, account=ACCOUNT, now=NOW, intent=None, positions=None):
    return engine.check(intent or entry(), quote, account, positions or {}, [], now)


def rejected(dec, reason):
    return dec.approved is False and reason in dec.reasons


def ok(dec):
    return dec.approved is True and dec.reasons == ()


# --------------------------------------------------------------------- timestamps
def test_future_ts_within_and_beyond_skew(engine):
    assert ok(run(engine, quote=replace(QUOTE, ts=NOW + 4.9)))
    assert ok(run(engine, quote=replace(QUOTE, ts=NOW + 5.0)))           # exact skew boundary
    assert rejected(run(engine, quote=replace(QUOTE, ts=NOW + 5.01)), "quote_future_ts")
    assert rejected(run(engine, quote=replace(QUOTE, ts=NOW + 86400)), "quote_future_ts")


def test_skew_is_configurable(cfg, rules, ks):
    strict = RiskEngine(cfg, rules, ks, max_future_skew_s=0.0)
    assert ok(run(strict, quote=replace(QUOTE, ts=NOW)))
    assert rejected(run(strict, quote=replace(QUOTE, ts=NOW + 0.001)), "quote_future_ts")
    with pytest.raises(ValueError):
        RiskEngine(cfg, rules, ks, max_future_skew_s=-1)
    with pytest.raises(ValueError):
        RiskEngine(cfg, rules, ks, max_future_skew_s=float("nan"))


def test_stale_and_exact_age_boundary(engine, cfg):
    m = cfg.max_data_age_s
    assert ok(run(engine, quote=replace(QUOTE, ts=NOW - m)))              # boundary accepted
    assert rejected(run(engine, quote=replace(QUOTE, ts=NOW - m - 0.001)), "quote_stale")
    assert rejected(run(engine, quote=replace(QUOTE, ts=NOW - 10 * m)), "quote_stale")


def test_backward_clock_step_does_not_make_frozen_quote_fresh(engine, cfg):
    # quote received at NOW, clock then steps back an hour: the quote is "from the future"
    assert rejected(run(engine, now=NOW - 3600), "quote_future_ts")


@pytest.mark.parametrize("ts", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_ts(engine, ts):
    assert rejected(run(engine, quote=replace(QUOTE, ts=ts)), "quote_bad_ts")


@pytest.mark.parametrize("now", [float("nan"), float("inf")])
def test_non_finite_clock(engine, now):
    assert rejected(run(engine, now=now), "bad_clock")


# --------------------------------------------------------------------- prices
@pytest.mark.parametrize("field,value", [
    ("bid", D("NaN")), ("ask", D("NaN")), ("bid", D("sNaN")), ("ask", D("Infinity")),
    ("bid", D("-Infinity")), ("bid", D("0")), ("ask", D("0")), ("bid", D("-1")), ("ask", D("-60010")),
])
def test_bad_prices(engine, field, value):
    assert rejected(run(engine, quote=replace(QUOTE, **{field: value})), "quote_bad_price")


def test_crossed_and_locked_book(engine):
    assert rejected(run(engine, quote=replace(QUOTE, bid=D("60010.01"))), "quote_crossed")
    locked = run(engine, quote=replace(QUOTE, bid=D("60010.00")))         # bid == ask is not crossed
    assert "quote_crossed" not in locked.reasons and "quote_bad_price" not in locked.reasons


def test_absurd_spread(engine, cfg, rules, ks):
    # ~3300 bps > default 2000 bps sanity cap: gated before any sizing math
    dec = run(engine, quote=replace(QUOTE, bid=D("43000"), ask=D("60010.00")))
    assert rejected(dec, "quote_absurd_spread") and dec.reasons == ("quote_absurd_spread",)
    # below the sanity cap but above policy: still the ordinary policy rejection
    dec = run(engine, quote=replace(QUOTE, bid=D("59000"), ask=D("60010.00")))
    assert rejected(dec, "spread_too_wide") and "quote_absurd_spread" not in dec.reasons
    with pytest.raises(ValueError):
        RiskEngine(cfg, rules, ks, max_sane_spread_bps=0)


# --------------------------------------------------------------------- cooldown
def test_cooldown_with_future_last_loss_expires(engine):
    acct = replace(ACCOUNT, consecutive_losses=3, last_loss_ts=NOW + 10 * 86400)   # bogus future ts
    assert rejected(run(engine, account=acct), "loss_cooldown")                   # full cooldown applies
    later = NOW + 3599
    q = replace(QUOTE, ts=later)
    assert rejected(run(engine, account=acct, now=later, quote=q), "loss_cooldown")
    done = NOW + 3600
    assert ok(run(engine, account=acct, now=done, quote=replace(QUOTE, ts=done)))  # expires, not at ts+1h


def test_cooldown_future_ts_restarts_on_further_backward_step(engine):
    acct = replace(ACCOUNT, consecutive_losses=3, last_loss_ts=NOW + 100)
    assert rejected(run(engine, account=acct), "loss_cooldown")
    back = NOW - 50                       # clock steps back again: elapsed never negative
    assert rejected(run(engine, account=acct, now=back, quote=replace(QUOTE, ts=back)), "loss_cooldown")
    t = back + 3600
    assert ok(run(engine, account=acct, now=t, quote=replace(QUOTE, ts=t)))


def test_cooldown_future_ts_fresh_engine_is_conservative(cfg, rules, ks):
    # without earlier observation the cooldown starts now (never shorter than cooldown_s)
    e = RiskEngine(cfg, rules, ks)
    acct = replace(ACCOUNT, consecutive_losses=3, last_loss_ts=NOW + 1)
    assert rejected(run(e, account=acct), "loss_cooldown")


def test_cooldown_normal_and_non_finite(engine):
    acct = replace(ACCOUNT, consecutive_losses=3, last_loss_ts=NOW - 3600)
    assert ok(run(engine, account=acct))                                  # exact boundary expires
    assert rejected(run(engine, account=replace(acct, last_loss_ts=NOW - 3599)), "loss_cooldown")
    assert rejected(run(engine, account=replace(acct, last_loss_ts=float("nan"))), "loss_cooldown")
    assert rejected(run(engine, account=replace(acct, last_loss_ts=None)), "loss_cooldown")


def test_sod_day_after_backward_step_across_midnight(engine):
    tomorrow = replace(ACCOUNT, sod_day=utc_day(NOW + 86400))
    assert rejected(run(engine, account=tomorrow), "sod_equity_stale")


# --------------------------------------------------------------------- exits unchanged
@pytest.mark.parametrize("quote", [
    None,
    replace(QUOTE, ts=NOW + 86400),
    replace(QUOTE, ts=NOW - 86400),
    replace(QUOTE, ts=float("nan")),
    replace(QUOTE, bid=D("NaN")),
    replace(QUOTE, bid=D("70000")),
    replace(QUOTE, bid=D("0"), ask=D("0")),
])
def test_reduce_only_exit_ignores_quote_problems(engine, quote):
    acct = replace(ACCOUNT, consecutive_losses=9, last_loss_ts=NOW + 86400)
    assert ok(run(engine, intent=exit_(), quote=quote, account=acct, positions=HELD))
    assert ok(run(engine, intent=exit_(), quote=quote, account=acct, positions=HELD, now=float("nan")))
