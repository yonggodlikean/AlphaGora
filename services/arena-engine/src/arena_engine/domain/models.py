from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from .enums import ControllerType, Liquidity, OrderStatus, OrderType, Side

ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class OrderRequest:
    portfolio_id: str
    symbol: str
    side: Side
    order_type: OrderType
    qty: int
    idempotency_key: str
    price: Decimal | None = None
    thesis: str | None = None
    debate_id: str | None = None

    def fingerprint(self) -> str:
        """Canonical hash of the economic content. Same idempotency key + different fingerprint => conflict."""
        canonical = {
            "symbol": self.symbol,
            "side": self.side.value,
            "order_type": self.order_type.value,
            "qty": self.qty,
            "price": None if self.price is None else str(self.price.normalize()),
            "thesis": self.thesis,
            "debate_id": self.debate_id,
        }
        return hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@dataclass(slots=True)
class Order:
    id: str
    seq: int                          # engine-wide arrival sequence -> deterministic FIFO
    portfolio_id: str
    symbol: str
    side: Side
    order_type: OrderType
    qty: int
    price: Decimal | None
    thesis: str | None
    debate_id: str | None
    idempotency_key: str
    request_hash: str
    trade_date: date
    created_at: datetime
    status: OrderStatus = OrderStatus.NEW
    filled_qty: int = 0
    filled_notional: Decimal = ZERO
    commission_charged: Decimal = ZERO
    fees_paid: Decimal = ZERO
    # Price-level queue model. None = our price is deeper than the visible 5 levels (queue unknown):
    # the order can then only fill by being crossed or traded through, until the level becomes visible.
    queue_initial: int | None = None
    queue_ahead: int | None = None
    reserve_price: Decimal = ZERO     # buy orders: worst-case price (limit price, or upper limit for market)
    reserved_cash: Decimal = ZERO     # buy orders: cash held for remaining qty + worst-case fees
    reserved_qty: int = 0             # sell orders: shares held for remaining qty
    deferred: bool = False            # entered 09:25-09:30: handled as a new arrival at 09:30

    @property
    def remaining(self) -> int:
        return self.qty - self.filled_qty

    @property
    def avg_fill_price(self) -> Decimal | None:
        if self.filled_qty == 0:
            return None
        return (self.filled_notional / self.filled_qty).quantize(Decimal("0.0001"))

    @property
    def queue_progress_pct(self) -> float | None:
        if self.queue_ahead is None or self.queue_initial is None:
            return None
        if self.queue_initial == 0:
            return 100.0
        return round(100.0 * (self.queue_initial - self.queue_ahead) / self.queue_initial, 2)

    def public_view(self) -> dict:
        return {
            "order_id": self.id,
            "portfolio_id": self.portfolio_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "order_type": self.order_type.value,
            "price": None if self.price is None else str(self.price),
            "qty": self.qty,
            "filled_qty": self.filled_qty,
            "avg_fill_price": None if self.avg_fill_price is None else str(self.avg_fill_price),
            "status": self.status.value,
            "queue_initial": self.queue_initial,
            "queue_ahead": self.queue_ahead,
            "queue_progress_pct": self.queue_progress_pct,
            "debate_id": self.debate_id,
            "thesis": self.thesis,
        }


@dataclass(frozen=True, slots=True)
class Fill:
    id: str
    order_id: str
    portfolio_id: str
    debate_id: str | None
    symbol: str
    side: Side
    price: Decimal
    qty: int
    notional: Decimal
    commission: Decimal
    stamp_duty: Decimal
    transfer_fee: Decimal
    liquidity: Liquidity
    snapshot_seq: int
    trade_date: date
    executed_at: datetime
    cost_released: Decimal = ZERO     # sells only: average-cost basis removed from the position

    @property
    def total_fees(self) -> Decimal:
        return self.commission + self.stamp_duty + self.transfer_fee

    @property
    def cash_delta(self) -> Decimal:
        if self.side is Side.BUY:
            return -(self.notional + self.total_fees)
        return self.notional - self.total_fees


@dataclass(slots=True)
class Position:
    symbol: str
    qty: int = 0
    frozen_qty: int = 0       # bought on the current trade date (T+1 lock)
    reserved_qty: int = 0     # held by open sell orders
    cost_basis: Decimal = ZERO

    @property
    def sellable_qty(self) -> int:
        return self.qty - self.frozen_qty - self.reserved_qty


@dataclass(slots=True)
class Account:
    portfolio_id: str
    controller_type: ControllerType
    initial_cash: Decimal
    cash: Decimal
    reserved_cash: Decimal = ZERO
    realized_pnl: Decimal = ZERO
    fees_paid: Decimal = ZERO
    positions: dict[str, Position] = field(default_factory=dict)

    @property
    def available_cash(self) -> Decimal:
        return self.cash - self.reserved_cash

    def position(self, symbol: str) -> Position:
        pos = self.positions.get(symbol)
        if pos is None:
            pos = self.positions[symbol] = Position(symbol)
        return pos
