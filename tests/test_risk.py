from dataclasses import replace
from decimal import Decimal

import pytest

from wallexbot.risk.engine import AccountState, RiskEngine, utc_day
from wallexbot.risk.killswitch import KillSwitch
from wallexbot.types import OrderIntent, OrderType, Position, Quote, Side, SymbolRules

D = Decimal
NOW = 1_760_000_000.0
SYM = "BTCUSDT"


@pytest.fixture
def ks(store):
    return KillSwitch(store)


@pytest.fixture
def engine(cfg, rules, ks):
    return RiskEngine(cfg, rules, ks)


def entry(**kw):
    base = dict(symbol=SYM, side=Side.BUY, type=OrderType.LIMIT, quantity=D("0.010000"),
                price=D("60010.00"), signal_id="s")
    base.update(kw)
    return OrderIntent(**base)


def exit_(qty="0.010000", **kw):
    base = dict(symbol=SYM, side=Side.SELL, type=OrderType.MARKET, quantity=D(qty), price=None,
                signal_id="x", reduce_only=True)
    base.update(kw)
    return OrderIntent(**base)


QUOTE = Quote(SYM, D("60000.00"), D("60010.00"), NOW)
ACCOUNT = AccountState(equity=D("10000"), cash_free=D("10000"), sod_equity=D("10000"),
                       sod_day=utc_day(NOW), hwm_equity=D("10000"))


def run(engine, intent=None, quote=QUOTE, account=ACCOUNT, positions=None, open_orders=(), now=NOW):
    return engine.check(intent or entry(), quote, account, positions or {}, list(open_orders), now)


def assert_reject(dec, reason):
    assert dec.approved is False and reason in dec.reasons, dec


def assert_ok(dec):
    assert dec.approved is True and dec.reasons == (), dec


def test_baseline_approved(engine):
    assert_ok(run(engine))


def test_kill_switch(engine, ks):
    assert_ok(run(engine))
    ks.engage("t")
    assert_reject(run(engine), "kill_switch_engaged")


def test_stale_quote(engine, cfg):
    assert_ok(run(engine, quote=replace(QUOTE, ts=NOW - cfg.max_data_age_s)))
    assert_reject(run(engine, quote=replace(QUOTE, ts=NOW - cfg.max_data_age_s - 1)), "stale_quote")
    assert_reject(run(engine, quote=replace(QUOTE, ts=NOW + 60)), "quote_from_future")
    assert_reject(run(engine, quote=None), "no_quote")
    assert_reject(run(engine, quote=replace(QUOTE, symbol="ETHUSDT")), "quote_symbol_mismatch")
    assert_reject(run(engine, quote=replace(QUOTE, bid=D("60020"))), "bad_quote")


def test_unknown_symbol(engine):
    q = replace(QUOTE, symbol="XRPUSDT")
    assert_reject(run(engine, intent=entry(symbol="XRPUSDT"), quote=q), "unknown_symbol")


def test_spread(engine):
    assert_ok(run(engine, quote=replace(QUOTE, bid=D("59930.00"))))   # ~11.7 bps
    assert_reject(run(engine, quote=replace(QUOTE, bid=D("59900.00"))), "spread_too_wide")  # ~18 bps


def test_quantity_precision_never_rounded(engine):
    assert_ok(run(engine, intent=entry(quantity=D("0.010000"))))
    assert_reject(run(engine, intent=entry(quantity=D("0.0100001"))), "quantity_precision")
    assert_reject(run(engine, intent=entry(quantity=D("0"))), "non_positive_quantity")


def test_price_precision(engine):
    assert_ok(run(engine, intent=entry(price=D("60010.10"))))
    assert_reject(run(engine, intent=entry(price=D("60010.001"))), "price_precision")
    assert_reject(run(engine, intent=entry(price=None)), "missing_price")
    assert_reject(run(engine, intent=entry(type=OrderType.STOP_MARKET, price=None, stop_price=None)),
                  "missing_stop_price")
    assert_reject(run(engine, intent=entry(type=OrderType.STOP_LIMIT, stop_price=D("60000.005"))),
                  "stop_price_precision")


def test_min_notional(engine, cfg, rules, ks):
    assert_ok(run(engine, intent=entry(quantity=D("0.000100"))))       # ~6 USDT >= 5
    assert_reject(run(engine, intent=entry(quantity=D("0.000050"))), "below_min_notional")
    strict = dict(rules)
    strict[SYM] = replace(rules[SYM], min_notional=D("10"))           # exchange rule overrides config
    assert_reject(run(RiskEngine(cfg, strict, ks), intent=entry(quantity=D("0.000100"))), "below_min_notional")


def test_max_order_value(engine):
    assert_ok(run(engine, intent=entry(quantity=D("0.016000"))))        # 960
    assert_reject(run(engine, intent=entry(quantity=D("0.017000"))), "above_max_order_value")


def test_market_order_uses_ask_plus_slippage(engine):
    # 0.0166 * 60010 = 996.17 (<1000) but with 20 bps slippage buffer = 998.16; 0.01666 -> 1001.8
    assert_ok(run(engine, intent=entry(type=OrderType.MARKET, price=None, quantity=D("0.016600"))))
    assert_reject(run(engine, intent=entry(type=OrderType.MARKET, price=None, quantity=D("0.016660"))),
                  "above_max_order_value")


def test_max_position_pct(engine):
    held = lambda q: {SYM: Position(SYM, D(q), D("60000"))}
    assert_ok(run(engine, positions=held("0.030")))     # 1800 + 600 = 24%
    assert_reject(run(engine, positions=held("0.032")), "above_max_position_pct")  # 1920 + 600 = 25.2%


def test_max_exposure_pct(engine):
    def pos(eth_qty):
        return {"ETHUSDT": Position("ETHUSDT", D(eth_qty), D("2000"))}
    acct = replace(ACCOUNT, marks={"ETHUSDT": D("2000")})
    assert_ok(run(engine, positions=pos("2.6"), account=acct))       # 5200 + 600 = 58%
    assert_reject(run(engine, positions=pos("2.8"), account=acct), "above_max_exposure_pct")  # 62%
    # pending BUY orders count as exposure too
    pending = [OrderIntent("ETHUSDT", Side.BUY, OrderType.LIMIT, D("0.5"), D("2000"), "p")]
    assert_reject(run(engine, positions=pos("2.6"), account=acct, open_orders=pending), "above_max_exposure_pct")


def test_max_open_positions(engine):
    small = lambda s: Position(s, D("0.0001"), D("100"))
    three = {s: small(s) for s in ("AUSDT", "BUSDT", "CUSDT")}
    two = {s: small(s) for s in ("AUSDT", "BUSDT")}
    assert_ok(run(engine, positions=two))
    assert_reject(run(engine, positions=three), "max_open_positions")
    assert_ok(run(engine, positions={**two, SYM: small(SYM)}))  # adding to an existing position


def test_cash_includes_fee_reserve(engine):
    assert_ok(run(engine, account=replace(ACCOUNT, cash_free=D("603"))))   # 600.10 * 1.0035 = 602.20
    assert_reject(run(engine, account=replace(ACCOUNT, cash_free=D("602"))), "insufficient_cash")


def test_one_open_order_per_symbol(engine):
    other = OrderIntent("ETHUSDT", Side.BUY, OrderType.LIMIT, D("0.01"), D("2000"), "o")
    same = OrderIntent(SYM, Side.BUY, OrderType.LIMIT, D("0.001"), D("59000"), "o")
    assert_ok(run(engine, open_orders=[other]))
    assert_reject(run(engine, open_orders=[same]), "open_order_exists")


def test_sod_must_be_today_utc(engine):
    assert_reject(run(engine, account=replace(ACCOUNT, sod_day="2000-01-01")), "sod_equity_stale")
    # crossing UTC midnight makes yesterday's SOD stale
    midnight = (NOW // 86400 + 1) * 86400
    assert_reject(run(engine, now=midnight, quote=replace(QUOTE, ts=midnight)), "sod_equity_stale")


def test_daily_loss_soft_and_hard(engine):
    soft = run(engine, account=replace(ACCOUNT, equity=D("9590")))  # -4.1%
    assert_reject(soft, "daily_loss_soft")
    assert soft.hard_breach is False
    hard = run(engine, account=replace(ACCOUNT, equity=D("9390")))  # -6.1%
    assert_reject(hard, "daily_loss_hard")
    assert hard.hard_breach is True
    assert_ok(run(engine, account=replace(ACCOUNT, equity=D("9700"))))


def test_drawdown_soft_and_hard(engine):
    soft = run(engine, account=replace(ACCOUNT, hwm_equity=D("11000")))   # -9.1%
    assert_reject(soft, "drawdown_soft")
    assert not soft.hard_breach
    hard = run(engine, account=replace(ACCOUNT, hwm_equity=D("11500")))   # -13%
    assert_reject(hard, "drawdown_hard")
    assert hard.hard_breach
    assert_ok(run(engine, account=replace(ACCOUNT, hwm_equity=D("10500"))))


def test_hard_breach_flag_set_even_when_gated_earlier(engine):
    dec = run(engine, quote=None, account=replace(ACCOUNT, equity=D("9000")))
    assert dec.approved is False and dec.hard_breach is True


def test_consecutive_loss_cooldown(engine):
    acct = replace(ACCOUNT, consecutive_losses=3, last_loss_ts=NOW - 100)
    assert_reject(run(engine, account=acct), "loss_cooldown")
    assert_ok(run(engine, account=replace(acct, last_loss_ts=NOW - 3601)))
    assert_ok(run(engine, account=replace(acct, consecutive_losses=2)))


def test_long_only_plain_sell_rejected(engine):
    assert_reject(run(engine, intent=entry(side=Side.SELL)), "sell_must_be_reduce_only")


class _Boom:
    def __getattr__(self, name):
        raise RuntimeError("boom")


class _RaisingKS:
    def allows(self, intent):
        raise RuntimeError("db down")


def test_exceptions_reject(engine, cfg, rules):
    assert run(engine, account=None).approved is False
    assert run(engine, account=_Boom()).approved is False
    assert run(engine, positions=_Boom()).approved is False
    dec = run(RiskEngine(cfg, rules, _RaisingKS()))
    assert dec.approved is False and any("kill_switch" in r for r in dec.reasons)
    weird = replace(QUOTE, ask=D("0"), bid=D("0"))  # division by zero inside spread
    assert run(engine, quote=weird).approved is False


def test_exception_in_reduce_only_rejects(engine):
    assert engine.check(exit_(), QUOTE, ACCOUNT, _Boom(), [], NOW).approved is False


# ----------------------------------------------------------------------------- reduce-only exits
HELD = {SYM: Position(SYM, D("0.010000"), D("60000"))}


def test_reduce_only_passes_under_kill_switch_breach_and_stale_data(engine, ks):
    ks.engage("t")
    bad_acct = replace(ACCOUNT, equity=D("5000"), sod_day="1999-01-01", consecutive_losses=9, last_loss_ts=NOW)
    dec = run(engine, intent=exit_(), quote=None, account=bad_acct, positions=HELD)
    assert_ok(dec)
    assert dec.hard_breach is True  # still reported to the guardian
    # a broken account object never blocks an exit
    assert_ok(run(engine, intent=exit_(), account=None, positions=HELD))


def test_reduce_only_still_needs_precision(engine):
    assert_reject(run(engine, intent=exit_("0.0100001"), positions={SYM: Position(SYM, D("1"), D("1"))}),
                  "quantity_precision")
    assert_reject(run(engine, intent=exit_(type=OrderType.LIMIT, price=D("1.001")), positions=HELD),
                  "price_precision")


def test_reduce_only_cannot_exceed_uncommitted_position(engine):
    assert_ok(run(engine, intent=exit_("0.010000"), positions=HELD))
    assert_reject(run(engine, intent=exit_("0.010001"), positions=HELD), "exceeds_position")
    assert_reject(run(engine, intent=exit_("0.000001")), "exceeds_position")  # nothing held
    resting_stop = OrderIntent(SYM, Side.SELL, OrderType.STOP_MARKET, D("0.006"), None, "st",
                               reduce_only=True, stop_price=D("59000"))
    assert_ok(run(engine, intent=exit_("0.004000"), positions=HELD, open_orders=[resting_stop]))
    assert_reject(run(engine, intent=exit_("0.005000"), positions=HELD, open_orders=[resting_stop]),
                  "exceeds_position")


def test_reduce_only_buy_and_unknown_symbol_rejected(engine):
    assert_reject(run(engine, intent=exit_(side=Side.BUY), positions=HELD), "reduce_only_must_be_sell")
    assert_reject(run(engine, intent=exit_(symbol="XRPUSDT")), "unknown_symbol")


def test_utc_day_boundary():
    assert utc_day(86400 * 3 - 0.001) == "1970-01-03"
    assert utc_day(86400 * 3) == "1970-01-04"


def test_rules_type(rules):
    assert isinstance(rules[SYM], SymbolRules)
