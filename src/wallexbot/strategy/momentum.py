"""S1 long-only momentum breakout entry engine. All rules must hold on the last closed bar; any
failure, missing data or non-finite value means abstain (None). Deterministic and look-ahead-safe:
only bars[-1] and earlier are read. The engine is stateless; cooldown lives in CooldownTracker.

Score (0..1): weighted mean of five margin components, each 0.5 exactly at its threshold rising
linearly to 1.0 at twice the threshold (earliness: 0.5 at the late limit, 1.0 at the breakout level):
volume 0.25, roc_z 0.20, efficiency_ratio 0.20, cost margin 0.20, earliness 0.15.
A signal that only just clears every rule therefore scores ~0.5 and is rejected by min_score=0.6."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from wallexbot.data.candles import Candle
from wallexbot.strategy.config import MomentumParams
from wallexbot.strategy.contract import MarketContext, Signal
from wallexbot.strategy.features import Features, atr_stop, compute_features, price_quantum

WEIGHTS = {"volume": 0.25, "roc_z": 0.20, "efficiency": 0.20, "cost": 0.20, "earliness": 0.15}


@dataclass(frozen=True)
class RuleResult:
    name: str
    text: str       # observed value and threshold, for the decision log
    passed: bool


@dataclass(frozen=True)
class Diagnosis:
    rules: tuple[RuleResult, ...]
    regime: str     # "favourable" | "tightened"
    score: float
    features: Features

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.rules)

    @property
    def failed(self) -> list[str]:
        return [r.name for r in self.rules if not r.passed]


def _margin(x: float, thr: float) -> float:
    """0.5 at x == thr, 1.0 at x >= 2*thr, clipped to [0, 1]."""
    if thr <= 0:
        return 1.0 if x > 0 else 0.0
    return min(1.0, max(0.0, 0.5 + 0.5 * (x / thr - 1.0)))


class MomentumSignalEngine:
    def __init__(self, params: MomentumParams = MomentumParams()):
        self.params = params

    def diagnose(self, symbol: str, bars: Sequence[Candle], market: MarketContext) -> Diagnosis | None:
        """Every rule's observed value and verdict, or None if the data is insufficient or unusable."""
        p = self.params
        f = compute_features(bars, p)
        if f is None or f.zero_volume_frac > p.max_zero_volume_frac:
            return None
        cost_bps = float(market.round_trip_cost_bps)
        favourable = f.efficiency_ratio >= p.er_min and f.ema_slope > 0
        vol_thr = p.volume_mult if favourable else p.volume_mult_tight
        cost_mult = p.cost_mult if favourable else p.cost_mult_tight
        move_bps = p.expected_move_atr * f.atr_pct * 1e4
        cost_thr = cost_mult * cost_bps
        late_limit = p.late_atr_mult * f.atr
        dist = f.close - f.donchian_high

        def r(name, ok, text):
            return RuleResult(name, text, bool(ok))

        rules = [
            r("breakout", f.close > f.donchian_high,
              f"close {f.close:.8g} > donchian{p.donchian_period}_high {f.donchian_high:.8g}"),
            r("trend", f.close > f.ema, f"close {f.close:.8g} > ema{p.ema_period} {f.ema:.8g}"),
            r("roc_accel", f.roc > f.roc_prev, f"roc{p.roc_period} {f.roc:.6f} > prev {f.roc_prev:.6f}"),
            r("roc_positive", f.roc_prev > 0, f"prev roc{p.roc_period} {f.roc_prev:.6f} > 0"),
            r("roc_z", f.roc_z >= p.roc_z_min, f"roc_z {f.roc_z:.3f} >= {p.roc_z_min:g}"),
            r("volume", f.vol_ratio >= vol_thr,
              f"volume/median{p.vol_median_window} {f.vol_ratio:.3f} >= {vol_thr:g}"),
            r("atr_band", p.atr_pct_min <= f.atr_pct <= p.atr_pct_max,
              f"atr_pct {f.atr_pct:.5f} in [{p.atr_pct_min:g}, {p.atr_pct_max:g}]"),
            r("not_late_breakout", dist <= late_limit,
              f"close-breakout {dist:.8g} <= {p.late_atr_mult:g}*atr {late_limit:.8g}"),
            r("not_late_rsi", f.rsi <= p.rsi_max, f"rsi{p.rsi_period} {f.rsi:.2f} <= {p.rsi_max:g}"),
            r("not_late_advance", f.advance_from_low <= p.max_advance_atr * f.atr,
              f"advance_from_{p.low_window}bar_low {f.advance_from_low:.8g} <= "
              f"{p.max_advance_atr:g}*atr {p.max_advance_atr * f.atr:.8g}"),
            r("cost_gate", move_bps >= cost_thr,
              f"expected_move {move_bps:.1f}bps >= {cost_mult:g}*round_trip_cost {cost_thr:.1f}bps"),
        ]
        comps = {
            "volume": _margin(f.vol_ratio, vol_thr),
            "roc_z": _margin(f.roc_z, p.roc_z_min),
            "efficiency": _margin(f.efficiency_ratio, p.er_min),
            "cost": _margin(move_bps, cost_thr),
            "earliness": min(1.0, max(0.0, 1.0 - 0.5 * dist / late_limit)) if late_limit > 0 else 0.0,
        }
        score = round(sum(WEIGHTS[k] * comps[k] for k in WEIGHTS), 4)
        rules.append(r("min_score", score >= p.min_score, f"score {score:.4f} >= {p.min_score:g}"))
        return Diagnosis(tuple(rules), "favourable" if favourable else "tightened", score, f)

    def evaluate(self, symbol: str, bars: Sequence[Candle], market: MarketContext) -> Signal | None:
        try:
            d = self.diagnose(symbol, bars, market)
            if d is None or not d.passed:
                return None
            last = bars[-1]
            breakout = max(c.high for c in bars[-self.params.donchian_period - 1:-1])
            stop = atr_stop(last.close, d.features.atr, self.params.stop_atr_mult, price_quantum(bars))
            if stop is None:
                return None
            reasons = {r.name: r.text for r in d.rules}
            reasons["regime"] = (f"{d.regime}: efficiency_ratio {d.features.efficiency_ratio:.3f} "
                                 f"(min {self.params.er_min:g}), ema_slope {d.features.ema_slope:.5f} (> 0)")
            return Signal(symbol=symbol, bar_ts=last.ts, entry_ref_price=last.close,
                          breakout_level=breakout, atr=stop[0], stop_price=stop[1],
                          score=Decimal(f"{d.score:.4f}"), reasons=reasons)
        except Exception:  # contract: never raise on bad data, abstain
            return None
