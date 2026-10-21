"""Shared value types. All money/quantity values are Decimal; never float."""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from decimal import ROUND_DOWN, Decimal
from enum import Enum

_NS = uuid.UUID("6f1d2c1e-8a0b-4c53-9a54-1c7f0e6d2b11")


def D(value) -> Decimal:
    """Convert to Decimal via str (rejects NaN/inf)."""
    d = Decimal(str(value))
    if not d.is_finite():
        raise ValueError(f"non-finite decimal: {value!r}")
    return d


def canonical_decimal(value) -> str | None:
    """Value-canonical fixed-point text: equal numbers give equal strings, whatever their scale.

    0.01, 0.010000 and 1E-2 -> "0.01"; 100 and 1E+2 -> "100"; 0, 0.000 and -0 -> "0".
    Pure string manipulation of the exact fixed-point form: no Decimal context is involved, so
    nothing is rounded (Decimal.normalize() would round to the context precision)."""
    if value is None:
        return None
    d = value if isinstance(value, Decimal) else D(value)
    if not d.is_finite():
        raise ValueError(f"non-finite decimal: {value!r}")
    if d.is_zero():
        return "0"
    s = format(d, "f")  # exact, never exponent notation
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def round_down(value: Decimal, precision: int) -> Decimal:
    """Round toward zero to `precision` decimal places (never rounds up)."""
    return value.quantize(Decimal(1).scaleb(-precision), rounding=ROUND_DOWN)


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"
    STOP_LIMIT = "STOP_LIMIT"
    STOP_MARKET = "STOP_MARKET"


class IntentState(str, Enum):
    PROPOSED = "proposed"
    RISK_APPROVED = "risk_approved"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    UNKNOWN_PENDING = "unknown_pending"
    NEEDS_REVIEW = "needs_review"


TERMINAL_STATES = frozenset(
    {IntentState.FILLED, IntentState.CANCELED, IntentState.REJECTED, IntentState.NEEDS_REVIEW}
)


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: Decimal
    ask: Decimal
    ts: float  # unix seconds, UTC

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    @property
    def spread_bps(self) -> Decimal:
        return (self.ask - self.bid) / self.mid * 10000


@dataclass(frozen=True)
class SymbolRules:
    symbol: str
    base: str
    quote: str
    amount_precision: int
    price_precision: int
    min_notional: Decimal | None = None  # unverified on Wallex; None = unknown


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    side: Side
    type: OrderType
    quantity: Decimal
    price: Decimal | None
    signal_id: str
    reduce_only: bool = False
    stop_price: Decimal | None = None

    @property
    def intent_id(self) -> str:
        """Deterministic id: same signal + same economic shape => same id (idempotent).

        Numbers are keyed by value, not by spelling (canonical_decimal), so Decimal("0.01") and
        Decimal("0.010000") give the same id and a resubmission cannot become a second order.
        stop_price is part of the shape: two exits that differ only in their stop are different
        orders. The key is a JSON array, so no field value can collide by containing a delimiter."""
        key = json.dumps(
            [self.signal_id, self.symbol, self.side.value, self.type.value,
             canonical_decimal(self.quantity), canonical_decimal(self.price),
             canonical_decimal(self.stop_price), bool(self.reduce_only)],
            separators=(",", ":"))
        return str(uuid.uuid5(_NS, key))

    @property
    def client_id(self) -> str:
        """Wallex client_id: letters/digits/underscore, unique."""
        return "wb_" + self.intent_id.replace("-", "")

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.client_id.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Fill:
    client_id: str
    symbol: str
    side: Side
    price: Decimal
    quantity: Decimal
    fee: Decimal
    fee_asset: str
    ts: float


@dataclass
class Position:
    symbol: str
    quantity: Decimal = Decimal(0)
    avg_price: Decimal = Decimal(0)
    realized_pnl: Decimal = Decimal(0)
    stop_price: Decimal | None = None
    meta: dict = field(default_factory=dict)
