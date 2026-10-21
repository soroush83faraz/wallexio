"""Execution-cost model for the backtest. Decimal only.

Every fill is a taker fill at a reference price (next-bar open, a triggered stop/target level,
or the last close at end of data), moved against us by:

    half_spread = half_spread_bps / 10000
    impact      = min(impact_k * order_notional / bar_notional, impact_cap_bps / 10000)
    BUY  price  = ref * (1 + half_spread + impact)   rounded UP to price_precision
    SELL price  = ref * (1 - half_spread - impact)   rounded DOWN to price_precision
    fee         = qty * price * taker_rate            charged in QUOTE

`bar_notional` is a liquidity proxy supplied by the engine: base volume * close of the last bar
that CLOSED before the fill (never the fill bar itself). A missing or zero proxy charges the cap.

Attribution is exact: spread_cost = qty * ref * half_spread; impact_cost = |qty * (price - ref)|
- spread_cost (so it also absorbs price rounding). Hence for a round trip
net P&L == frictionless P&L - fees - spread_cost - impact_cost, to the last digit.

Conservative choices / unverified Wallex facts:
- taker_rate 0.0035 is a PLACEHOLDER (config `fees.taker_rate`), not a verified Wallex fee.
- Real Wallex may charge the fee in the received asset (base on buys). We charge the same rate in
  quote on the notional; the value is equivalent but quantities received may differ slightly.
- Take-profit levels are also charged as taker fills with spread + impact (a resting limit would
  usually pay less); maker_rate is carried for reporting only.
- half_spread default 7.5 bps = half of the 15 bps max spread the risk engine allows.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal

from wallexbot.strategy.contract import MarketContext
from wallexbot.types import round_down

_BPS = Decimal(10000)
_ZERO = Decimal(0)


def round_up(value: Decimal, precision: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-precision), rounding=ROUND_CEILING)


@dataclass(frozen=True)
class FillCost:
    price: Decimal
    notional: Decimal      # qty * price
    fee: Decimal           # quote
    spread_cost: Decimal   # quote, >= 0
    impact_cost: Decimal   # quote, >= 0 (includes price rounding)


@dataclass(frozen=True)
class CostModel:
    taker_rate: Decimal = Decimal("0.0035")     # placeholder, UNVERIFIED on Wallex
    maker_rate: Decimal = Decimal("0.0035")     # placeholder, UNVERIFIED; not used for fills
    half_spread_bps: Decimal = Decimal("7.5")
    impact_k: Decimal = Decimal("0.2")
    impact_cap_bps: Decimal = Decimal("50")

    def __post_init__(self):
        if min(self.taker_rate, self.maker_rate, self.half_spread_bps, self.impact_k, self.impact_cap_bps) < 0:
            raise ValueError("cost parameters must be >= 0")
        if self.taker_rate >= 1 or self.half_spread_bps + self.impact_cap_bps >= _BPS:
            raise ValueError("cost parameters too large")

    @property
    def half_spread(self) -> Decimal:
        return self.half_spread_bps / _BPS

    def impact_fraction(self, order_notional: Decimal, bar_notional: Decimal | None) -> Decimal:
        cap = self.impact_cap_bps / _BPS
        if bar_notional is None or bar_notional <= 0:
            return cap
        return min(self.impact_k * order_notional / bar_notional, cap)

    def fill(self, side: str, ref_price: Decimal, qty: Decimal, bar_notional: Decimal | None,
             price_precision: int | None = None) -> FillCost:
        if side not in ("BUY", "SELL"):
            raise ValueError(f"bad side {side!r}")
        if ref_price <= 0 or qty <= 0:
            raise ValueError("ref_price and qty must be positive")
        hs = self.half_spread
        impact = self.impact_fraction(qty * ref_price, bar_notional)
        if side == "BUY":
            price = ref_price * (1 + hs + impact)
            if price_precision is not None:
                price = round_up(price, price_precision)
        else:
            price = ref_price * (1 - hs - impact)
            if price_precision is not None:
                price = round_down(price, price_precision)
        if price <= 0:
            raise ValueError("non-positive fill price after costs")
        notional = qty * price
        spread_cost = qty * ref_price * hs
        impact_cost = abs(notional - qty * ref_price) - spread_cost
        return FillCost(price, notional, notional * self.taker_rate, spread_cost, impact_cost)

    def market_context(self) -> MarketContext:
        """What a signal engine is told about costs: full spread and the worst-case impact."""
        return MarketContext(spread_bps=2 * self.half_spread_bps, est_slippage_bps=self.impact_cap_bps,
                             taker_fee_rate=self.taker_rate)
