"""Shared value types. All money/quantity values are Decimal; never float."""
from __future__ import annotations

import hashlib
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
        """Deterministic id: same signal+shape => same id (idempotent)."""
        key = "|".join(
            [self.signal_id, self.symbol, self.side.value, self.type.value,
             str(self.quantity), str(self.price), str(self.reduce_only)]
        )
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
