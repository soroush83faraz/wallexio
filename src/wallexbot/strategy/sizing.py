"""Risk-based position sizing for long entries. Decimal only; quantity is never rounded up.

    qty = equity * risk_pct / 100 / (entry - stop)

`risk_pct` is `risk.risk_per_trade_pct` (a percentage, e.g. 1.0), halved when `score < 0.7`.
The quantity is then clamped by every notional cap below and rounded DOWN to the symbol's
`amount_precision`:

    max_position    equity * max_position_pct / 100
    max_order_value risk.max_order_value
    cash            cash_free / (1 + fee_rate)            (fee reserve)
    depth           depth_notional / min_depth_multiple   (only when depth_notional is given)
    exposure        exposure_headroom                     (optional, caller-computed)

The result is 0 (with a reason) when the stop is not below entry, when an input is invalid, or
when the notional is below min_notional. min_notional is the LARGER of `rules.min_notional` and
`risk.min_notional` (conservative: both are unverified on Wallex).

Divisions run in a floor-rounding context and the rounded quantity is re-checked against every
cap with exact arithmetic (stepping down one precision unit if needed), so Decimal context
rounding can never push the quantity over a cap or the risk budget.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal, localcontext

from wallexbot.config import RiskConfig
from wallexbot.types import SymbolRules, round_down

LOW_SCORE = Decimal("0.7")
_HUNDRED = Decimal(100)
_ZERO = Decimal(0)


@dataclass(frozen=True)
class SizingResult:
    qty: Decimal
    notional: Decimal
    reasons: tuple[str, ...]
    risk_budget: Decimal = _ZERO   # quote currency the trade may lose at the stop (before costs)
    binding: str = ""              # which limit bound the quantity ("risk" if none of the caps)

    @property
    def ok(self) -> bool:
        return self.qty > 0


def min_notional_for(rules: SymbolRules, risk: RiskConfig) -> Decimal:
    if rules.min_notional is None:
        return risk.min_notional
    return max(rules.min_notional, risk.min_notional)


def size_position(equity: Decimal, entry: Decimal, stop: Decimal, rules: SymbolRules,
                  risk: RiskConfig, cash_free: Decimal, fee_rate: Decimal,
                  depth_notional: Decimal | None = None, score: Decimal | None = None, *,
                  min_depth_multiple: Decimal = Decimal(5),
                  exposure_headroom: Decimal | None = None) -> SizingResult:
    reasons: list[str] = []

    def zero(why: str, budget: Decimal = _ZERO, binding: str = "") -> SizingResult:
        return SizingResult(_ZERO, _ZERO, (*reasons, why), budget, binding)

    if equity <= 0:
        return zero("non_positive_equity")
    if entry <= 0:
        return zero("non_positive_entry")
    if stop <= 0:
        return zero("non_positive_stop")
    if stop >= entry:
        return zero("stop_not_below_entry")
    if fee_rate < 0 or min_depth_multiple <= 0:
        return zero("invalid_cost_inputs")

    with localcontext() as ctx:
        ctx.prec = 60
        ctx.rounding = ROUND_FLOOR
        risk_pct = risk.risk_per_trade_pct
        if score is not None and score < LOW_SCORE:
            risk_pct = risk_pct / 2
            reasons.append("low_score_half_risk")
        budget = equity * risk_pct / _HUNDRED
        per_unit = entry - stop
        # notional caps (quote currency); the risk budget is a separate per-unit cap
        caps: dict[str, Decimal] = {
            "max_position": equity * risk.max_position_pct / _HUNDRED,
            "max_order_value": risk.max_order_value,
            "cash": max(cash_free, _ZERO) / (1 + fee_rate),
        }
        if depth_notional is not None:
            caps["depth"] = max(depth_notional, _ZERO) / min_depth_multiple
        if exposure_headroom is not None:
            caps["exposure"] = max(exposure_headroom, _ZERO)

        qty_limits = {"risk": budget / per_unit}
        qty_limits.update({k: v / entry for k, v in caps.items()})
        binding = min(qty_limits, key=lambda k: qty_limits[k])  # first minimum wins: deterministic
        qty = round_down(qty_limits[binding], rules.amount_precision)

        step = Decimal(1).scaleb(-rules.amount_precision)

        def violates(q: Decimal) -> bool:
            n = q * entry
            return (q * per_unit > budget
                    or n > caps["max_position"] or n > caps["max_order_value"]
                    or n * (1 + fee_rate) > max(cash_free, _ZERO)
                    or ("depth" in caps and n * min_depth_multiple > depth_notional)
                    or ("exposure" in caps and n > caps["exposure"]))

        while qty > 0 and violates(qty):  # guards against context rounding only; rarely loops
            qty -= step
        if qty < 0:
            qty = _ZERO

        if binding != "risk":
            reasons.append(f"capped_by:{binding}")
        if qty <= 0:
            return zero("zero_after_caps_and_precision", budget, binding)
        notional = qty * entry
        if notional < min_notional_for(rules, risk):
            return zero("below_min_notional", budget, binding)
        return SizingResult(qty, notional, tuple(reasons), budget, binding)
