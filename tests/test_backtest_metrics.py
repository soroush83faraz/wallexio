import math
from decimal import Decimal

import pytest

from wallexbot.backtest.costs import CostModel
from wallexbot.backtest.engine import BacktestResult, EquityPoint, SimFill, Trade
from wallexbot.backtest.metrics import buy_and_hold, compute_metrics, max_drawdown, periods_per_year
from wallexbot.data.candles import Candle

D = Decimal


def trade(net, r, gross, fees="0", spread="0", impact="0", eod=False, sym="AAAUSDT"):
    return Trade(symbol=sym, signal_id="s", signal_ts=0, entry_ts=900, entry_price=D(100), qty=D(1),
                 initial_stop=D(90), risk_per_unit=D(10), exit_ts=1800, exit_reason="x", exits=(), gross_pnl=D(gross),
                 fees=D(fees), spread_cost=D(spread), impact_cost=D(impact), net_pnl=D(net), r_multiple=D(r),
                 bars_held=1, end_of_data=eod)


def fill(notional):
    return SimFill(0, "AAAUSDT", "BUY", "entry", "", D(1), D(1), D(1), D(notional), D(0), D(0), D(0))


def result(equities, trades=(), fills=(), pos=None, interval=900):
    pos = pos or [0] * len(equities)
    curve = tuple(EquityPoint(i * interval, D(e), D(e) - D(p), D(p)) for i, (e, p) in enumerate(zip(equities, pos)))
    return BacktestResult(tuple(trades), curve, tuple(fills), (), D(100), D(equities[-1]),
                          {"bar_interval_s": interval, "symbols": ["AAAUSDT"]}, "ih", "rh")


def test_known_equity_curve():
    # initial 100 -> 110 -> 99 -> 120: net +20%, max drawdown (110 - 99) / 110 = 10%
    m = compute_metrics(result(["110", "99", "120"], pos=["50", "0", "60"]))
    assert m["net_return"] == pytest.approx(0.2)
    assert m["max_drawdown"] == pytest.approx(0.1)
    assert m["bars"] == 3
    assert m["periods_per_year"] == 35040.0                 # 365 * 96, not 252
    rets = [0.1, -0.1, 120 / 99 - 1]
    mean = sum(rets) / 3
    sd = math.sqrt(sum((x - mean) ** 2 for x in rets) / 2)
    assert m["sharpe"] == pytest.approx(mean / sd * math.sqrt(35040))
    assert m["annualized_return"] is None                    # 1.2 ** 11680 overflows -> undefined, not inf
    assert m["exposure_time"] == pytest.approx(2 / 3)
    assert m["avg_exposure"] == pytest.approx((50 / 110 + 0 + 60 / 120) / 3)


def test_annualisation_365_day_year():
    assert periods_per_year(900) == 35040 and periods_per_year(3600) == 8760
    # one day of hourly bars, +1% total: (1.01) ** 365 - 1
    m = compute_metrics(result(["100"] * 23 + ["101"], interval=3600))
    assert m["annualized_return"] == pytest.approx(1.01 ** 365 - 1)


def test_trade_statistics_and_cost_shares():
    trades = [trade("30", "1.5", "40", fees="4", spread="3", impact="3"),
              trade("-10", "-0.5", "-4", fees="2", spread="2", impact="2", eod=True),
              trade("0", "0", "0")]
    m = compute_metrics(result(["120"], trades, fills=[fill("500"), fill("700")]))
    assert (m["trade_count"], m["wins"], m["losses"]) == (3, 1, 1)
    assert m["win_rate"] == pytest.approx(1 / 3)
    assert (m["avg_win"], m["avg_loss"]) == (30.0, -10.0)
    assert m["expectancy_quote"] == pytest.approx(20 / 3)
    assert m["expectancy_r"] == pytest.approx(1 / 3)
    assert m["profit_factor"] == pytest.approx(3.0)
    assert m["gross_pnl"] == 36.0
    assert m["fee_share_of_gross"] == pytest.approx(6 / 36)
    assert m["spread_share_of_gross"] == pytest.approx(5 / 36)
    assert m["slippage_share_of_gross"] == pytest.approx(5 / 36)
    assert m["turnover"] == pytest.approx(12.0)               # 1200 traded / 100 initial
    assert m["cost_bps_of_traded_notional"] == pytest.approx(16 / 1200 * 1e4)
    assert m["end_of_data_trades"] == 1


def test_undefined_values_are_none_and_regime_hook():
    m = compute_metrics(result(["100"], [trade("5", "0.5", "5")]), regime_of=lambda t: "bull")
    assert m["profit_factor"] is None and m["sharpe"] is None
    assert m["by_regime"]["bull"]["trade_count"] == 1
    empty = compute_metrics(result(["100"]))
    assert empty["win_rate"] is None and empty["expectancy_r"] is None and empty["fee_share_of_gross"] is None


def test_max_drawdown_and_buy_and_hold():
    assert max_drawdown([100, 120, 90, 130, 65]) == pytest.approx(0.5)
    bars = [Candle(0, D(100), D(100), D(100), D(100), D(10)), Candle(900, D(110), D(110), D(110), D(110), D(10))]
    zero = CostModel(taker_rate=D(0), maker_rate=D(0), half_spread_bps=D(0), impact_k=D(0), impact_cap_bps=D(0))
    assert buy_and_hold(bars, D(1000), zero)["net_return"] == pytest.approx(0.1)
    costly = buy_and_hold(bars, D(1000), CostModel())
    assert costly["net_return"] < 0.1
