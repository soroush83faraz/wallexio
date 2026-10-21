"""Broker protocol and exchange-level value types shared by MockWallex and the live client."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Protocol

from wallexbot.types import OrderType, Quote, Side


class BrokerError(Exception):
    """Base class for broker failures."""


class BrokerTimeout(BrokerError):
    """Ambiguous: the request may or may not have been accepted. Never resubmit."""


class RateLimited(BrokerError):
    """HTTP 429: the request was refused before processing; no order was created."""


class OrderRejected(BrokerError):
    """Definite rejection: no order was created."""


class DuplicateClientId(BrokerError):
    """An order with this client_id already exists; the new order was NOT created."""


class OrderStatus(str, Enum):
    NEW = "NEW"                        # resting in the book
    PENDING_TRIGGER = "PENDING_TRIGGER"  # stop order waiting for stop_price; not in the book
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"


OPEN_STATUSES = frozenset({OrderStatus.NEW, OrderStatus.PENDING_TRIGGER, OrderStatus.PARTIALLY_FILLED})


@dataclass(frozen=True)
class BrokerOrder:
    """Exchange view of one order. Fill amounts are CUMULATIVE; the OMS derives deltas."""
    client_id: str
    symbol: str
    side: Side
    type: OrderType
    quantity: Decimal
    price: Decimal | None
    stop_price: Decimal | None
    status: OrderStatus
    filled_qty: Decimal
    filled_quote: Decimal  # sum(price * qty) over all fills, before fees
    fee: Decimal           # cumulative fee, denominated in fee_asset
    fee_asset: str
    created_ts: float
    updated_ts: float


@dataclass(frozen=True)
class Balance:
    free: Decimal
    locked: Decimal

    @property
    def total(self) -> Decimal:
        return self.free + self.locked


@dataclass(frozen=True)
class FeeRates:
    maker: Decimal
    taker: Decimal


class Broker(Protocol):
    def submit_order(self, *, client_id: str, symbol: str, side: Side, type: OrderType,
                     quantity: Decimal, price: Decimal | None = None,
                     stop_price: Decimal | None = None) -> BrokerOrder: ...

    def cancel_order(self, client_id: str) -> BrokerOrder | None: ...

    def get_order(self, client_id: str) -> BrokerOrder | None:
        """None means the exchange has no order with this client_id."""
        ...

    def open_orders(self, symbol: str | None = None) -> list[BrokerOrder]: ...

    def balances(self) -> dict[str, Balance]: ...

    def fee_rates(self) -> FeeRates: ...

    def quote(self, symbol: str) -> Quote: ...
