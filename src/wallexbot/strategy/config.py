"""Parameters of the S1 momentum strategy. Every value below is UNVALIDATED: the plan fixes only the
structure (Donchian 20, EMA50, volume 1.8x, cost gate 3x, 8-bar cooldown, ...). The numeric defaults
here are placeholders to be chosen on train/validation data only, never on the test set."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MomentumParams:
    # Breakout and trend. ema_period is a BAR count of the series the engine is fed: 50 x 15m bars
    # (~12.5h) only approximates the plan's 1h-derived EMA50; use ema_period=200 on 15m bars to
    # match 50 hourly bars.
    donchian_period: int = 20          # close must exceed the highest high of the PRIOR 20 bars
    ema_period: int = 50
    ema_slope_bars: int = 4            # EMA slope = ema[-1] / ema[-1-k] - 1 (regime test)
    # Momentum acceleration
    roc_period: int = 6
    roc_z_window: int = 96             # z-score of ROC over this many trailing bars
    roc_z_min: float = 1.0
    # Volume expansion relative to the median of the PRIOR vol_median_window bars
    vol_median_window: int = 48
    volume_mult: float = 1.8
    volume_mult_tight: float = 2.2     # used when the regime is unfavourable
    max_zero_volume_frac: float = 0.2  # abstain if more of the median window has zero volume
    # Volatility band (ATR / close). UNVALIDATED defaults.
    atr_period: int = 14
    atr_pct_min: float = 0.002
    atr_pct_max: float = 0.03
    # Not-late filters
    late_atr_mult: float = 1.0         # close <= breakout + late_atr_mult * ATR
    rsi_period: int = 14
    rsi_max: float = 85.0
    low_window: int = 24
    max_advance_atr: float = 5.0       # close - min(low, last low_window bars) <= this * ATR
    # Cost gate: expected_move_atr * ATR (in bps of close) >= cost_mult * round-trip cost bps
    expected_move_atr: float = 1.5
    cost_mult: float = 3.0
    cost_mult_tight: float = 4.0       # used when the regime is unfavourable
    # Regime: favourable iff efficiency_ratio >= er_min AND EMA slope > 0
    er_period: int = 20
    er_min: float = 0.3
    # Decision
    min_score: float = 0.6
    stop_atr_mult: float = 1.5
    # Data requirements
    min_bars: int = 120                # fewer bars than this -> abstain
    history_bars: int = 300            # only the trailing bars are used (bounded cost per evaluate)
    # Cooldown (consumed by CooldownTracker, not by the engine)
    cooldown_bars: int = 8
    cooldown_bars_profitable: int = 2  # after a profitable exit with the trend intact

    @property
    def required_bars(self) -> int:
        return max(self.roc_z_window + self.roc_period, self.vol_median_window + 1,
                   self.ema_period + self.ema_slope_bars, self.donchian_period + 1,
                   self.atr_period + 1, self.rsi_period + 1, self.er_period + 1, self.low_window)

    def __post_init__(self):
        if self.min_bars < self.required_bars:
            raise ValueError(f"min_bars {self.min_bars} < required_bars {self.required_bars}")
        if self.history_bars < self.min_bars:
            raise ValueError("history_bars must be >= min_bars")
