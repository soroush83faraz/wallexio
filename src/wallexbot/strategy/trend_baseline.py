"""S0 reference strategy, used only as a benchmark: plain trend following with a Donchian trigger.
Entry: close > SMA50, SMA20 > SMA50 and close > highest high of the prior 20 bars. Stop is
close - 2.5*ATR. No volume, cost or regime logic, so it is not comparable on selectivity: only on
what the S1 filters add."""
from __future__ import annotations

import math
from decimal import Decimal
from typing import Sequence

from wallexbot.data.candles import Candle
from wallexbot.strategy import indicators as ind
from wallexbot.strategy.contract import MarketContext, Signal
from wallexbot.strategy.features import atr_stop, price_quantum, window_series


class TrendBaselineEngine:
    def __init__(self, fast: int = 20, slow: int = 50, donchian: int = 20, atr_period: int = 14,
                 stop_atr_mult: float = 2.5, history_bars: int = 300):
        self.fast, self.slow, self.donchian = fast, slow, donchian
        self.atr_period, self.stop_atr_mult, self.history_bars = atr_period, stop_atr_mult, history_bars

    def evaluate(self, symbol: str, bars: Sequence[Candle], market: MarketContext) -> Signal | None:
        try:
            if len(bars) < max(self.slow, self.donchian + 1, self.atr_period + 1):
                return None
            s = window_series(bars, self.history_bars)
            if s is None:
                return None
            close = float(s.close[-1])
            sma_fast = float(ind.sma(s.close, self.fast)[-1])
            sma_slow = float(ind.sma(s.close, self.slow)[-1])
            high_n = float(ind.donchian_high(s.high, self.donchian)[-1])
            atr = float(ind.atr(s.high, s.low, s.close, self.atr_period)[-1])
            if not all(math.isfinite(v) for v in (sma_fast, sma_slow, high_n, atr)) or atr <= 0:
                return None
            if not (close > sma_slow and sma_fast > sma_slow and close > high_n):
                return None
            last = bars[-1]
            stop = atr_stop(last.close, atr, self.stop_atr_mult, price_quantum(bars))
            if stop is None:
                return None
            reasons = {
                "trend": f"close {close:.8g} > sma{self.slow} {sma_slow:.8g}",
                "sma_cross": f"sma{self.fast} {sma_fast:.8g} > sma{self.slow} {sma_slow:.8g}",
                "breakout": f"close {close:.8g} > donchian{self.donchian}_high {high_n:.8g}",
            }
            return Signal(symbol=symbol, bar_ts=last.ts, entry_ref_price=last.close,
                          breakout_level=max(c.high for c in bars[-self.donchian - 1:-1]),
                          atr=stop[0], stop_price=stop[1], score=Decimal(1), reasons=reasons,
                          strategy_version="s0-0.1")
        except Exception:  # contract: never raise on bad data, abstain
            return None
