"""Shared strategy contract. Indicators may use float internally; every price/qty that leaves the
strategy layer is Decimal. Bars are CLOSED candles, oldest first; strategies must never look past
`bars[-1]` (look-ahead is tested with `check_no_lookahead`)."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Mapping, Protocol, Sequence

from wallexbot.data.candles import Candle

STRATEGY_VERSION = "s1-0.1"


@dataclass(frozen=True)
class MarketContext:
    """Execution-cost inputs at decision time (from live depth, or recorded/assumed in backtests)."""
    spread_bps: Decimal
    est_slippage_bps: Decimal
    taker_fee_rate: Decimal  # per side, e.g. Decimal("0.0035")

    @property
    def round_trip_cost_bps(self) -> Decimal:
        return 2 * self.taker_fee_rate * 10000 + self.spread_bps + self.est_slippage_bps


@dataclass(frozen=True)
class Signal:
    symbol: str
    bar_ts: int                 # open time of the signal bar (closed)
    entry_ref_price: Decimal    # close of the signal bar
    breakout_level: Decimal
    atr: Decimal
    stop_price: Decimal         # initial hard stop (entry_ref - stop_atr_mult * ATR)
    score: Decimal              # 0..1, deterministic
    reasons: Mapping[str, str] = field(default_factory=dict)  # rule -> value, for the decision log
    strategy_version: str = STRATEGY_VERSION

    @property
    def signal_id(self) -> str:
        key = f"{self.symbol}|{self.bar_ts}|{self.strategy_version}"
        return "sig_" + hashlib.sha256(key.encode()).hexdigest()[:24]


class SignalEngine(Protocol):
    def evaluate(self, symbol: str, bars: Sequence[Candle], market: MarketContext) -> Signal | None: ...


@dataclass(frozen=True)
class PositionState:
    """What an exit policy may know. Updated by the caller (backtest or bot) after each bar."""
    symbol: str
    entry_price: Decimal
    entry_bar_ts: int
    quantity: Decimal
    initial_stop: Decimal
    breakout_level: Decimal
    highest_high_since_entry: Decimal
    bars_held: int
    partial_taken: bool = False
    current_stop: Decimal | None = None


@dataclass(frozen=True)
class ExitDecision:
    action: str                  # "hold" | "exit_all" | "exit_partial" | "update_stop"
    fraction: Decimal = Decimal(0)   # for exit_partial (0 < f < 1)
    new_stop: Decimal | None = None  # for update_stop
    reason: str = ""


class ExitPolicy(Protocol):
    def on_bar(self, state: PositionState, bars: Sequence[Candle]) -> ExitDecision: ...
