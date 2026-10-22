"""The one dust rule, shared by the risk engine, the bot and the guardian.

A holding is DUST (treated as flat: not a position, not sellable, not counted against
max_open_positions) when either
- its quantity, rounded down to the symbol's amount precision, is zero (below one amount step), or
- its notional at the mark is below the minimum notional, min_notional_for(rules, risk):
  the larger of the exchange's per-symbol minimum (when known) and config risk.min_notional.
Without a usable mark (None or <= 0) only the amount-step rule applies (unknown value: not dust).

Why: fees charged in the base asset leave ~1e-5 of the base after a full exit; that residue can
be neither sold (below the exchange minimum) nor ignored by one component and counted by another.

Imports nothing beyond wallexbot.types / wallexbot.config (the guardian may not import strategy).
"""
from __future__ import annotations

from decimal import Decimal

from wallexbot.config import RiskConfig
from wallexbot.types import SymbolRules, round_down


def min_notional_for(rules: SymbolRules, risk: RiskConfig) -> Decimal:
    if rules.min_notional is None:
        return risk.min_notional
    return max(rules.min_notional, risk.min_notional)


def is_dust(qty: Decimal, rules: SymbolRules, mark: Decimal | None, min_notional: Decimal) -> bool:
    q = round_down(qty, rules.amount_precision)
    if q <= 0:
        return True
    return mark is not None and mark > 0 and q * mark < min_notional
