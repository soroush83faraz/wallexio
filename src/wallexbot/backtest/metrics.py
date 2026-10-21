"""Performance statistics from a BacktestResult. Floats are used only here, for statistics; the
inputs (trades, fills, equity curve) are exact Decimal.

Annualisation assumes a 24/7 market and a 365-day year: periods_per_year = 365*86400 / bar
interval (35040 for 15m bars), never 252 trading days. Sharpe uses a zero risk-free rate.
Undefined values (no trades, no losses, zero gross P&L, ...) are None, never inf/NaN.
"""
from __future__ import annotations

import math
from decimal import Decimal
from typing import Callable, Mapping, Sequence

from wallexbot.backtest.costs import CostModel
from wallexbot.backtest.engine import BacktestResult, Trade
from wallexbot.data.candles import Candle
from wallexbot.types import round_down

YEAR_S = 365 * 86400


def periods_per_year(bar_interval_s: int) -> float:
    return YEAR_S / bar_interval_s


def max_drawdown(values: Sequence[float]) -> float:
    """Largest peak-to-trough decline as a fraction of the peak (0.1 = -10%)."""
    peak, worst = -math.inf, 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            worst = max(worst, (peak - v) / peak)
    return worst


def _ratio(a: float, b: float) -> float | None:
    return None if b == 0 else a / b


def _mean(xs: Sequence[float]) -> float | None:
    return sum(xs) / len(xs) if xs else None


def _annualize(net_return: float, n_periods: int, ppy: float) -> float | None:
    """Compound a total return over n_periods bars to a 365-day year; None if undefined or it
    overflows a float (tiny samples extrapolate absurdly; read it with the bar count)."""
    if n_periods <= 0 or net_return <= -1:
        return None
    try:
        return math.exp(math.log1p(net_return) * ppy / n_periods) - 1
    except OverflowError:
        return None


def trade_stats(trades: Sequence[Trade]) -> dict:
    pnl = [float(t.net_pnl) for t in trades]
    rs = [float(t.r_multiple) for t in trades]
    wins = [p for p in pnl if p > 0]
    losses = [p for p in pnl if p < 0]
    return {
        "trade_count": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": _ratio(len(wins), len(trades)),
        "avg_win": _mean(wins),
        "avg_loss": _mean(losses),
        "expectancy_quote": _mean(pnl),
        "expectancy_r": _mean(rs),
        "profit_factor": _ratio(sum(wins), -sum(losses)) if losses else None,
        "net_pnl": sum(pnl),
    }


def compute_metrics(result: BacktestResult, *, regime_of: Callable[[Trade], str] | None = None) -> dict:
    """All headline numbers, computed only from `result`."""
    interval = int(result.stats_inputs["bar_interval_s"])
    init = float(result.initial_equity)
    eq = [init] + [float(p.equity) for p in result.equity_curve]
    rets = [eq[i] / eq[i - 1] - 1 for i in range(1, len(eq)) if eq[i - 1] > 0]
    ppy = periods_per_year(interval)
    n = len(rets)
    net_return = eq[-1] / init - 1
    mean_r = _mean(rets)
    vol = math.sqrt(sum((r - mean_r) ** 2 for r in rets) / (n - 1)) if n > 1 else None
    trades = result.trades
    gross = float(sum((t.gross_pnl for t in trades), Decimal(0)))
    fees = float(sum((t.fees for t in trades), Decimal(0)))
    spread = float(sum((t.spread_cost for t in trades), Decimal(0)))
    impact = float(sum((t.impact_cost for t in trades), Decimal(0)))
    traded = float(sum((f.notional for f in result.fills), Decimal(0)))
    curve = result.equity_curve
    out = {
        "initial_equity": init,
        "final_equity": eq[-1],
        "net_return": net_return,
        "max_drawdown": max_drawdown(eq),
        "bars": n,
        "bar_interval_s": interval,
        "periods_per_year": ppy,
        "annualized_return": _annualize(net_return, n, ppy),
        "annualized_volatility": vol * math.sqrt(ppy) if vol is not None else None,
        "sharpe": (mean_r / vol) * math.sqrt(ppy) if vol else None,
        **trade_stats(trades),
        "gross_pnl": gross,
        "fees": fees,
        "spread_cost": spread,
        "impact_cost": impact,
        "fee_share_of_gross": _ratio(fees, abs(gross)),
        "spread_share_of_gross": _ratio(spread, abs(gross)),
        "slippage_share_of_gross": _ratio(impact, abs(gross)),
        "cost_bps_of_traded_notional": _ratio((fees + spread + impact) * 1e4, traded),
        "traded_notional": traded,
        "turnover": traded / init,
        "exposure_time": _ratio(sum(1 for p in curve if p.position_value > 0), len(curve)),
        "avg_exposure": _mean([float(p.position_value / p.equity) for p in curve if p.equity > 0]),
        "end_of_data_trades": sum(1 for t in trades if t.end_of_data),
    }
    if regime_of is not None:
        out["by_regime"] = regime_breakdown(trades, regime_of)
    return out


def regime_breakdown(trades: Sequence[Trade], regime_of: Callable[[Trade], str]) -> dict:
    groups: dict[str, list[Trade]] = {}
    for t in trades:
        groups.setdefault(str(regime_of(t)), []).append(t)
    return {k: trade_stats(groups[k]) for k in sorted(groups)}


def buy_and_hold(bars: Sequence[Candle], initial_equity: Decimal, cost_model: CostModel,
                 amount_precision: int = 8) -> dict:
    """Buy at the first bar's OPEN, hold, sell at the last CLOSE, both with full costs (no
    liquidity proxy before the first bar, so the buy pays the impact cap)."""
    if not bars:
        return {"net_return": None, "max_drawdown": None}
    first, last = bars[0], bars[-1]
    unit = cost_model.fill("BUY", first.open, Decimal(1), None)
    qty = round_down(initial_equity / (unit.price * (1 + cost_model.taker_rate)), amount_precision)
    # impact makes the real price higher than the 1-unit estimate: step down until it fits
    while qty > 0 and (c := cost_model.fill("BUY", first.open, qty, None)).notional + c.fee > initial_equity:
        qty -= Decimal(1).scaleb(-amount_precision)
    if qty <= 0:
        return {"net_return": None, "max_drawdown": None}
    buy = cost_model.fill("BUY", first.open, qty, None)
    cash = initial_equity - buy.notional - buy.fee
    prev = bars[-2] if len(bars) > 1 else None
    sell = cost_model.fill("SELL", last.close, qty, prev.volume * prev.close if prev else None)
    final = cash + sell.notional - sell.fee
    curve = [float(initial_equity)] + [float(cash + qty * b.close) for b in bars[:-1]] + [float(final)]
    return {"net_return": float(final / initial_equity - 1), "max_drawdown": max_drawdown(curve),
            "final_equity": float(final)}


def equal_weight_buy_and_hold(bars_by_symbol: Mapping[str, Sequence[Candle]], initial_equity: Decimal,
                              cost_model: CostModel) -> dict:
    per = {s: buy_and_hold(bars_by_symbol[s], initial_equity, cost_model) for s in sorted(bars_by_symbol)}
    rets = [v["net_return"] for v in per.values() if v["net_return"] is not None]
    return {"net_return": _mean(rets), "per_symbol": per}
