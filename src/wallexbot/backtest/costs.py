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
net P&L == frictionless P&L - fees - spread_cost - impact_cost, to the last digit. The money
products (notional, fee, spread_cost, impact_cost) are computed in `EXACT`, a 1000-digit context
that TRAPS Inexact: they are products/sums of finite decimals, so any rounding would be a bug and
raises instead of silently breaking the identity. Only the impact ratio (a division) and the price
quantization round, and they happen before, in the caller's context.

Conservative choices / unverified Wallex facts:
- taker_rate 0.0035 is a PLACEHOLDER (config `fees.taker_rate`), not a verified Wallex fee.
- Real Wallex may charge the fee in the received asset (base on buys). We charge the same rate in
  quote on the notional; the value is equivalent but quantities received may differ slightly.
- Take-profit levels are also charged as taker fills with spread + impact (a resting limit would
  usually pay less); maker_rate is carried for reporting only.
- half_spread default 7.5 bps = half of the 15 bps max spread the risk engine allows.

Cost sources (`cost_source`; COST_MODEL_VERSION is part of the engine's inputs hash):
- "proxy" (the default, the model above): impact from the previous bar's notional volume. Candle
  volume semantics on Wallex are unverified, and for 1000 USDT orders the proxy sits at its cap.
- "recorded": built by `CostModel.from_calibration` from live order-book snapshots
  (`backtest/calibrate.py`). half_spread_bps is a quantile (default p90) of the recorded half
  spread; impact is a piecewise-linear curve over the ORDER notional (quote) through the calibrated
  notionals, flat below the first point, extrapolated with the last segment's slope (floored at 0)
  above the last one, and capped at `impact_cap_bps` (100 for recorded). `bar_notional` is ignored.
  The curve is non-decreasing, so impact stays monotone in quantity (the engine's two-pass sizing
  relies on that). See docs/COSTS.md.
- `CostBook` maps symbols to cost models (recorded where calibrated, proxy elsewhere); the engine
  accepts it wherever it accepts a CostModel. A plain CostModel applies to every symbol.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import (ROUND_CEILING, ROUND_HALF_EVEN, Context, Decimal, DivisionByZero, Inexact, InvalidOperation,
                     Overflow, localcontext)
from typing import Any, Mapping

from wallexbot.strategy.contract import MarketContext
from wallexbot.types import round_down

_BPS = Decimal(10000)
_ZERO = Decimal(0)
COST_MODEL_VERSION = "cm-0.2"   # 0.2: cost_source 'recorded' (calibrated impact curve) and CostBook
COST_SOURCES = ("proxy", "recorded")
RECORDED_IMPACT_CAP_BPS = Decimal(100)
# Exact money arithmetic (same convention as the OMS ledger): +, -, * of finite decimals never
# round at 1000 digits for any real magnitude span, and the Inexact trap turns a rounding into an
# exception. Never divide or quantize inside it.
EXACT = Context(prec=1000, rounding=ROUND_HALF_EVEN, traps=[InvalidOperation, DivisionByZero, Overflow, Inexact])


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
    # --- used by cost_source "recorded" only; the defaults leave the proxy model unchanged
    cost_source: str = "proxy"
    impact_curve: tuple[tuple[Decimal, Decimal], ...] = ()   # ((order notional in quote, impact bps), ...)
    reference_notional: Decimal = Decimal(1000)  # order size behind the signal engine's cost estimate
    calibration_id: str = ""                     # sha256 of the calibration it came from (provenance)

    def __post_init__(self):
        if min(self.taker_rate, self.maker_rate, self.half_spread_bps, self.impact_k, self.impact_cap_bps) < 0:
            raise ValueError("cost parameters must be >= 0")
        if self.taker_rate >= 1 or self.half_spread_bps + self.impact_cap_bps >= _BPS:
            raise ValueError("cost parameters too large")
        if self.cost_source not in COST_SOURCES:
            raise ValueError(f"cost_source must be one of {COST_SOURCES}")
        if self.cost_source == "recorded":
            if not self.impact_curve:
                raise ValueError("a recorded cost model needs an impact curve")
            if self.reference_notional <= 0:
                raise ValueError("reference_notional must be positive")
        if self.impact_curve and (self.impact_curve[0][0] <= 0 or self.impact_curve[0][1] < 0):
            raise ValueError("impact_curve points need a positive notional and bps >= 0")
        for (x0, y0), (x1, y1) in zip(self.impact_curve, self.impact_curve[1:]):
            if not x1 > x0 or y1 < y0:
                raise ValueError("impact_curve notionals must increase and its bps must not decrease")

    # ------------------------------------------------------------------ construction
    @classmethod
    def from_calibration(cls, cal: Any, symbol: str, quantile: str = "p90", *,
                         taker_rate: Decimal = Decimal("0.0035"), maker_rate: Decimal | None = None,
                         impact_cap_bps: Decimal = RECORDED_IMPACT_CAP_BPS,
                         reference_notional: Decimal = Decimal(1000)) -> "CostModel":
        """A recorded-cost model for `symbol` from a `calibrate.CostCalibration` (or its JSON dict).

        half_spread_bps = the `quantile` of the recorded half spread. The impact at each calibrated
        notional N is the worse side (BUY or SELL) of
            max(q(VWAP slippage beyond the touch at N), q(VWAP slippage vs mid at N) - q(half spread), 0)
        so that half spread + impact >= q(slippage vs mid) at every calibrated point. A notional whose
        quantile is undefined on either side (insufficient visible depth) is dropped and the curve is
        extrapolated from the remaining points. Raises KeyError when the symbol has no usable data."""
        if quantile not in ("median", "p90"):
            raise ValueError("quantile must be 'median' or 'p90'")
        data: Mapping[str, Any] = cal.data if hasattr(cal, "data") else cal
        entry = data["symbols"].get(symbol)
        if entry is None or not entry.get("usable"):
            raise KeyError(f"no usable cost calibration for {symbol}")
        hs = Decimal(entry["half_spread_bps"][quantile])
        points: list[tuple[Decimal, Decimal]] = []
        for n in data["notionals_quote"]:
            sides = []
            for side in ("BUY", "SELL"):
                touch = entry["impact_beyond_touch_bps"][side][n][quantile]
                mid = entry["slippage_vs_mid_bps"][side][n][quantile]
                if touch is None or mid is None:
                    break
                sides.append(max(Decimal(touch), Decimal(mid) - hs, _ZERO))
            if len(sides) != 2:
                continue
            y = max(sides)
            if points and y < points[-1][1]:
                y = points[-1][1]   # never decreasing (quantiles of monotone book walks already are)
            points.append((Decimal(n), y))
        if not points:
            raise KeyError(f"cost calibration for {symbol} has no notional with enough visible depth")
        return cls(taker_rate=taker_rate, maker_rate=taker_rate if maker_rate is None else maker_rate,
                   half_spread_bps=hs, impact_k=_ZERO, impact_cap_bps=impact_cap_bps, cost_source="recorded",
                   impact_curve=tuple(points), reference_notional=reference_notional,
                   calibration_id=str(data.get("calibration_sha256", "")))

    def with_fee(self, taker_rate: Decimal, maker_rate: Decimal | None = None) -> "CostModel":
        return replace(self, taker_rate=taker_rate, maker_rate=taker_rate if maker_rate is None else maker_rate)

    def for_symbol(self, symbol: str) -> "CostModel":
        """A single CostModel applies to every symbol (CostBook overrides this)."""
        return self

    # ------------------------------------------------------------------ pricing
    @property
    def half_spread(self) -> Decimal:
        return self.half_spread_bps / _BPS

    def curve_bps(self, order_notional: Decimal) -> tuple[Decimal, str]:
        """Recorded impact (bps, before the cap) for an order of `order_notional` quote, and where it
        came from: 'below_range' (flat at the first point), 'interpolated' or 'extrapolated'."""
        pts = self.impact_curve
        if not pts:
            raise ValueError("no impact curve (proxy cost model)")
        if order_notional <= pts[0][0]:
            return pts[0][1], ("below_range" if order_notional < pts[0][0] else "interpolated")
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            if order_notional <= x1:
                return y0 + (y1 - y0) * (order_notional - x0) / (x1 - x0), "interpolated"
        xn, yn = pts[-1]
        if len(pts) >= 2:
            xa, ya = pts[-2]
            slope = max((yn - ya) / (xn - xa), _ZERO)
        else:
            slope = yn / xn   # a single point: the line through the origin
        return yn + slope * (order_notional - xn), "extrapolated"

    def impact_status(self, order_notional: Decimal) -> str:
        """'proxy' | 'below_range' | 'interpolated' | 'extrapolated' | 'capped' (for reports)."""
        if self.cost_source != "recorded":
            return "proxy"
        bps, where = self.curve_bps(order_notional)
        return "capped" if bps > self.impact_cap_bps else where

    def impact_fraction(self, order_notional: Decimal, bar_notional: Decimal | None) -> Decimal:
        cap = self.impact_cap_bps / _BPS
        if self.cost_source == "recorded":
            bps, _ = self.curve_bps(order_notional)
            return min(max(bps, _ZERO) / _BPS, cap)
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
        with localcontext(EXACT):
            notional = qty * price
            spread_cost = qty * ref_price * hs
            impact_cost = abs(notional - qty * ref_price) - spread_cost
            fee = notional * self.taker_rate
        return FillCost(price, notional, fee, spread_cost, impact_cost)

    def exit_cost_bps(self, order_notional: Decimal | None = None, bar_notional: Decimal | None = None) -> Decimal:
        """Estimated price give-up of a taker SELL, in bps of the reference price: half spread plus
        impact (the cap when the order or liquidity is unknown). Use it to derive
        `ExitParams.breakeven_buffer_bps` from the same cost model the backtest charges."""
        if order_notional is None:
            return self.half_spread_bps + self.impact_cap_bps
        return self.half_spread_bps + self.impact_fraction(order_notional, bar_notional) * _BPS

    def market_context(self) -> MarketContext:
        """What a signal engine is told about costs: the full spread (both half spreads of a round
        trip) and a slippage estimate. Proxy: the worst-case impact cap, once (unchanged).
        Recorded: the curve's impact at `reference_notional`, paid on BOTH legs (entry and exit)."""
        if self.cost_source == "recorded":
            slip = 2 * self.impact_fraction(self.reference_notional, None) * _BPS
        else:
            slip = self.impact_cap_bps
        return MarketContext(spread_bps=2 * self.half_spread_bps, est_slippage_bps=slip,
                             taker_fee_rate=self.taker_rate)


@dataclass(frozen=True)
class CostBook:
    """Per-symbol cost models plus a default for symbols not listed. All models share one fee
    scenario (the engine's sizing reads `taker_rate`)."""
    default: CostModel
    per_symbol: tuple[tuple[str, CostModel], ...] = ()

    def __post_init__(self):
        names = [s for s, _ in self.per_symbol]
        if names != sorted(set(names)):
            raise ValueError("CostBook symbols must be unique and sorted")
        if any((m.taker_rate, m.maker_rate) != (self.default.taker_rate, self.default.maker_rate)
               for _, m in self.per_symbol):
            raise ValueError("every model of a CostBook must use the same fee rates")

    @classmethod
    def of(cls, default: CostModel, models: Mapping[str, CostModel]) -> "CostBook":
        return cls(default, tuple(sorted(models.items())))

    def for_symbol(self, symbol: str) -> CostModel:
        for s, m in self.per_symbol:
            if s == symbol:
                return m
        return self.default

    @property
    def taker_rate(self) -> Decimal:
        return self.default.taker_rate

    @property
    def maker_rate(self) -> Decimal:
        return self.default.maker_rate

    def with_fee(self, taker_rate: Decimal, maker_rate: Decimal | None = None) -> "CostBook":
        return CostBook(self.default.with_fee(taker_rate, maker_rate),
                        tuple((s, m.with_fee(taker_rate, maker_rate)) for s, m in self.per_symbol))
