"""Multi-tenant A-share paper-trading matching engine.

Execution model ("shadow book")
-------------------------------
Every portfolio trades against the *public* L1 5-level snapshot independently. Tenants never consume each
other's liquidity, so one large bot cannot starve the arena, and every portfolio's result is reproducible from
the market tape alone. To stop a portfolio from manufacturing liquidity, each (portfolio, symbol) gets a budget
per snapshot: visible depth per level can be taken once, and passive fills share the snapshot's traded volume.

Fill rules
----------
* Arrival (taker): walk the opposite side up to the limit price (market = best-5 IOC), fill at level prices.
* Resting order at price P (buy shown; sell is symmetric), only when volume traded since the last snapshot:
    - crossed (ask1 <= P) or traded through (last < P)  -> fill at P, capped by traded volume.
      At the upper limit this is a board break (炸板) and is tagged BOARD_BREAK.
    - traded at P (last == P)  -> queue_ahead -= delta_volume; volume beyond our queue position fills us.
    - queue_ahead is then capped by the visible volume at P (orders ahead of us may cancel).
  On a sealed limit-up (asks empty, bid1 == upper), this is exactly the 打板排队 model:
  queue_ahead starts at bid1_vol, drains by delta_volume, fills when it crosses zero or the board breaks.

The engine is a single-writer, synchronous state machine with no I/O and no wall clock: callers pass market
time, and the API layer persists the returned fills/events in one transaction (see docs/ARCHITECTURE.md).
"""
from __future__ import annotations

import itertools
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from ..domain import events as ev
from ..domain.enums import ControllerType, Liquidity, OrderStatus, OrderType, RejectCode, Side
from ..domain.errors import InvariantViolation, MarketDataError, OrderRejected
from ..domain.events import ArenaEvent
from ..domain.models import ZERO, Account, Fill, Order, OrderRequest
from ..market.calendar import TradingCalendar
from ..market.snapshot import L1Snapshot
from .fees import FeeSchedule, money
from .rate_limit import SlidingWindowLimiter
from .rules import is_fund, is_valid_tick, lot_rule_for, tick_for


@dataclass(frozen=True, slots=True)
class EngineConfig:
    fees: FeeSchedule = field(default_factory=FeeSchedule)
    max_snapshot_age: timedelta = timedelta(seconds=30)
    cancel_actions_per_window: int = 2
    cancel_window: timedelta = timedelta(minutes=1)
    thesis_min_len: int = 4
    thesis_max_len: int = 280


@dataclass(slots=True)
class SubmitResult:
    order: Order
    fills: list[Fill]
    events: list[ArenaEvent]
    duplicate: bool = False     # True => idempotent replay of an earlier submit; nothing changed


@dataclass(slots=True)
class TickResult:
    fills: list[Fill] = field(default_factory=list)
    events: list[ArenaEvent] = field(default_factory=list)


@dataclass(slots=True)
class _Budget:
    """Liquidity one portfolio may still take from one snapshot of one symbol."""

    seq: int
    taken_levels: dict[tuple[bool, Decimal], int] = field(default_factory=dict)  # (is_bid_side, price) -> qty
    taken_volume: int = 0                                                        # of the traded delta volume


def _s(x: Decimal | None) -> str | None:
    return None if x is None else str(x)


class AShareMatchingEngine:
    def __init__(
        self,
        calendar: TradingCalendar,
        config: EngineConfig | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.calendar = calendar
        self.config = config or EngineConfig()
        self._new_id = id_factory or (lambda: str(uuid.uuid4()))
        self._seq = itertools.count(1)
        self._accounts: dict[str, Account] = {}
        self._orders: dict[str, Order] = {}
        self._idempotency: dict[tuple[str, str], str] = {}
        self._open: dict[str, dict[str, Order]] = {}          # symbol -> {order_id: order}, arrival order
        self._books: dict[str, L1Snapshot] = {}
        self._budgets: dict[tuple[str, str], _Budget] = {}
        self._cancel_limiter = SlidingWindowLimiter(self.config.cancel_actions_per_window, self.config.cancel_window)
        self._trade_date: date | None = None
        self._day_open = False

    # ================================================================== accounts & lifecycle
    def open_account(self, portfolio_id: str, controller_type: ControllerType, initial_cash: Decimal) -> Account:
        if portfolio_id in self._accounts:
            raise ValueError(f"account {portfolio_id} already exists")
        if initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        acct = Account(portfolio_id, controller_type, initial_cash=initial_cash, cash=money(initial_cash))
        self._accounts[portfolio_id] = acct
        return acct

    def restore_account(
        self,
        portfolio_id: str,
        controller_type: ControllerType,
        initial_cash: Decimal,
        cash: Decimal,
        positions: dict[str, tuple[int, int, Decimal]],
        realized_pnl: Decimal = ZERO,
        fees_paid: Decimal = ZERO,
    ) -> Account:
        """Rebuild an account from persisted state. `positions`: symbol -> (qty, frozen_qty, cost_basis)."""
        acct = self.open_account(portfolio_id, controller_type, initial_cash)
        acct.cash = cash
        acct.realized_pnl = realized_pnl
        acct.fees_paid = fees_paid
        for sym, (qty, frozen, cost) in positions.items():
            if qty > 0:
                pos = acct.position(sym)
                pos.qty, pos.frozen_qty, pos.cost_basis = qty, frozen, cost
        self._check_invariants(acct)
        return acct

    def restore_order(self, order: Order) -> None:
        """Re-open a persisted open limit order after a restart within the same trade date.

        Cash (buys) or shares (sells) are reserved again exactly as on submit. Raises OrderRejected if the
        account can no longer cover it; nothing is changed in that case and the caller expires the order.
        """
        if not order.status.is_open or order.remaining <= 0 or order.order_type is not OrderType.LIMIT:
            raise ValueError(f"order {order.id} is not an open limit order")
        if order.trade_date != self._trade_date or not self._day_open:
            raise ValueError("orders can only be restored into their own open trade date")
        acct = self.account(order.portfolio_id)
        if order.side is Side.BUY:
            order.reserve_price = order.price  # type: ignore[assignment]
            order.reserved_cash = ZERO
            needed = self._buy_reservation_amount(order.reserve_price, order.remaining,
                                                  order.filled_notional, order.commission_charged)
            if needed > acct.available_cash:
                raise OrderRejected(RejectCode.INSUFFICIENT_CASH, f"needs {needed}, available {acct.available_cash}")
            self._update_buy_reservation(order, acct)
        else:
            pos = acct.positions.get(order.symbol)
            if pos is None or pos.sellable_qty < order.remaining:
                raise OrderRejected(RejectCode.INSUFFICIENT_POSITION, f"cannot re-reserve {order.remaining} shares")
            pos.reserved_qty += order.remaining
            order.reserved_qty = order.remaining
        self._check_invariants(acct)
        order.seq = next(self._seq)
        self._orders[order.id] = order
        self._idempotency[(order.portfolio_id, order.idempotency_key)] = order.id
        self._open.setdefault(order.symbol, {})[order.id] = order

    def resume_trading_day(self, trade_date: date, now: datetime) -> list[ArenaEvent]:
        """Re-open a trading day after a restart WITHOUT T+1 settlement (today's buys stay frozen)."""
        if not self.calendar.is_trading_day(trade_date):
            raise ValueError(f"{trade_date} is not a trading day")
        self._trade_date = trade_date
        self._day_open = True
        return [ArenaEvent(ev.MARKET_OPEN, now, {"trade_date": trade_date.isoformat(), "resumed": True})]

    @property
    def day_open(self) -> bool:
        return self._day_open

    @property
    def accounts(self) -> dict[str, Account]:
        return self._accounts

    def book(self, symbol: str) -> L1Snapshot | None:
        return self._books.get(symbol)

    @property
    def books(self) -> dict[str, L1Snapshot]:
        return self._books

    def prime_books(self, snaps: list[L1Snapshot]) -> None:
        """Last known prices while no day is open (after a restart outside the session): they value positions and
        fill the symbol list. The next day's first snapshot clears them like any other book."""
        if self._day_open:
            return
        for s in snaps:
            self._books.setdefault(s.symbol, s)

    def open_orders(self, symbol: str | None = None) -> list[Order]:
        books = [self._open.get(symbol, {})] if symbol else list(self._open.values())
        return sorted((o for b in books for o in b.values()), key=lambda o: o.seq)

    def account(self, portfolio_id: str) -> Account:
        try:
            return self._accounts[portfolio_id]
        except KeyError:
            raise OrderRejected(RejectCode.UNKNOWN_PORTFOLIO, portfolio_id) from None

    def order(self, order_id: str) -> Order | None:
        return self._orders.get(order_id)

    @property
    def trade_date(self) -> date | None:
        return self._trade_date

    def begin_trading_day(self, trade_date: date, now: datetime) -> list[ArenaEvent]:
        """Pre-open: settle T+1 (yesterday's buys become sellable) and reset per-day state."""
        if not self.calendar.is_trading_day(trade_date):
            raise ValueError(f"{trade_date} is not a trading day")
        if self._trade_date is not None and trade_date <= self._trade_date:
            raise ValueError(f"trade date must advance ({self._trade_date} -> {trade_date})")
        events: list[ArenaEvent] = []
        if self._day_open:                       # scheduler missed the close: expire leftovers first
            events += self.end_trading_day(now)
        for acct in self._accounts.values():
            for sym in [s for s, p in acct.positions.items() if p.qty == 0 and p.reserved_qty == 0]:
                del acct.positions[sym]
            for pos in acct.positions.values():
                pos.frozen_qty = 0               # T+1 settlement
            self._check_invariants(acct)
        self._books.clear()
        self._budgets.clear()
        self._cancel_limiter.reset()
        self._trade_date = trade_date
        self._day_open = True
        events.append(ArenaEvent(ev.MARKET_OPEN, now, {"trade_date": trade_date.isoformat()}))
        return events

    def end_trading_day(self, now: datetime) -> list[ArenaEvent]:
        """Close: A-share orders are day orders, so everything still open expires and reservations release."""
        events: list[ArenaEvent] = []
        for book in self._open.values():
            for order in list(book.values()):
                events.append(self._terminate(order, OrderStatus.EXPIRED, now))
        self._day_open = False
        events.append(ArenaEvent(ev.MARKET_CLOSE, now, {"trade_date": _s_date(self._trade_date)}))
        return events

    # ================================================================== commands
    def submit_order(self, req: OrderRequest, now: datetime) -> SubmitResult:
        # 1. Idempotency first: a retry must return the original outcome even if the market has since closed.
        fingerprint = req.fingerprint()
        existing_id = self._idempotency.get((req.portfolio_id, req.idempotency_key))
        if existing_id is not None:
            existing = self._orders[existing_id]
            if existing.request_hash != fingerprint:
                raise OrderRejected(RejectCode.IDEMPOTENCY_CONFLICT,
                                    f"idempotency_key {req.idempotency_key!r} was used for a different order")
            return SubmitResult(existing, [], [], duplicate=True)

        acct = self.account(req.portfolio_id)
        snap = self._validate(req, acct, now)

        order = Order(
            id=self._new_id(), seq=next(self._seq), portfolio_id=req.portfolio_id, symbol=req.symbol,
            side=req.side, order_type=req.order_type, qty=req.qty, price=req.price,
            thesis=(req.thesis.strip() if req.thesis else None), debate_id=req.debate_id,
            idempotency_key=req.idempotency_key, request_hash=fingerprint,
            trade_date=self._trade_date, created_at=now,  # type: ignore[arg-type]
        )

        # 2. Reserve resources (validated above, so this cannot fail).
        if order.side is Side.BUY:
            order.reserve_price = order.price if order.price is not None else snap.upper_limit
            self._update_buy_reservation(order, acct)
        else:
            pos = acct.position(order.symbol)
            pos.reserved_qty += order.qty
            order.reserved_qty = order.qty
        self._check_invariants(acct)

        self._orders[order.id] = order
        self._idempotency[(req.portfolio_id, req.idempotency_key)] = order.id
        self._open.setdefault(order.symbol, {})[order.id] = order
        result = SubmitResult(order, [], [ArenaEvent(ev.ORDER_ACCEPTED, now, order.public_view())])

        # 3. Take visible liquidity.
        self._match_on_arrival(order, snap, now, result)

        # 4. Market orders never rest (最优五档即时成交剩余撤销). Limit orders join the price-level queue.
        if order.status.is_open:
            if order.order_type is OrderType.MARKET:
                result.events.append(self._terminate(order, OrderStatus.CANCELED, now, reason="ioc_remainder"))
            else:
                self._init_queue(order, snap)
                if self._at_limit_price(order, snap):
                    result.events.append(self._queue_event(order, snap, now))
        return result

    def cancel_order(self, portfolio_id: str, order_id: str, now: datetime) -> list[ArenaEvent]:
        order = self._orders.get(order_id)
        if order is None or order.portfolio_id != portfolio_id:
            raise OrderRejected(RejectCode.UNKNOWN_ORDER, order_id)
        if not order.status.is_open:
            raise OrderRejected(RejectCode.ORDER_NOT_OPEN, f"order is {order.status.value}")
        key = (portfolio_id, order.symbol)
        if not self._cancel_limiter.allowed(key, now):
            raise OrderRejected(
                RejectCode.RATE_LIMITED,
                f"max {self.config.cancel_actions_per_window} cancel/replace per {order.symbol} per "
                f"{int(self.config.cancel_window.total_seconds())}s",
            )
        self._cancel_limiter.record(key, now)
        return [self._terminate(order, OrderStatus.CANCELED, now, reason="user")]

    def force_cancel(self, order_id: str, now: datetime, reason: str) -> ArenaEvent | None:
        """Operator cancel (e.g. an account was banned): no rate limit, no ownership check."""
        order = self._orders.get(order_id)
        if order is None or not order.status.is_open:
            return None
        return self._terminate(order, OrderStatus.CANCELED, now, reason=reason)

    # ================================================================== market data
    def on_snapshot(self, snap: L1Snapshot) -> TickResult:
        """Apply one snapshot. Duplicate / out-of-order frames are ignored (idempotent tick processing)."""
        result = TickResult()
        if not self._day_open or self.calendar.trade_date_of(snap.ts) != self._trade_date:
            raise MarketDataError(f"{snap.symbol}: snapshot {snap.ts.isoformat()} outside open trade date "
                                  f"{_s_date(self._trade_date)}")
        prev = self._books.get(snap.symbol)
        if prev is not None and snap.seq <= prev.seq:
            return result
        delta = 0
        if prev is not None:
            # A shrinking cumulative volume is a feed glitch; treat as no trades rather than reversing queues.
            delta = max(0, snap.cumulative_volume - prev.cumulative_volume)
        self._books[snap.symbol] = snap
        result.events += self._board_transitions(prev, snap)

        for order in list(self._open.get(snap.symbol, {}).values()):
            if order.order_type is OrderType.LIMIT:
                self._match_resting(order, snap, delta, result)
        return result

    # ================================================================== validation
    def _validate(self, req: OrderRequest, acct: Account, now: datetime) -> L1Snapshot:
        cfg = self.config
        thesis = (req.thesis or "").strip()
        if not thesis and not req.debate_id:
            raise OrderRejected(RejectCode.THESIS_REQUIRED, "every order needs a public thesis or a debate_id")
        if thesis and not (cfg.thesis_min_len <= len(thesis) <= cfg.thesis_max_len):
            raise OrderRejected(RejectCode.THESIS_REQUIRED,
                                f"thesis must be {cfg.thesis_min_len}-{cfg.thesis_max_len} characters")
        if not isinstance(req.qty, int) or isinstance(req.qty, bool) or req.qty <= 0:
            raise OrderRejected(RejectCode.INVALID_QTY, "qty must be a positive integer number of shares")
        if req.order_type is OrderType.LIMIT:
            tick = tick_for(req.symbol)
            if req.price is None or not is_valid_tick(req.price, tick):
                raise OrderRejected(RejectCode.INVALID_PRICE, f"limit price must be > 0 on a {tick} tick")
        elif req.price is not None:
            raise OrderRejected(RejectCode.INVALID_PRICE, "market orders must not carry a price")

        if not (self._day_open and self.calendar.trade_date_of(now) == self._trade_date
                and self.calendar.is_continuous_trading(now)):
            raise OrderRejected(RejectCode.MARKET_CLOSED, "orders accepted 09:30-11:30 and 13:00-15:00 CST")

        snap = self._books.get(req.symbol)
        if snap is None:
            raise OrderRejected(RejectCode.NO_MARKET_DATA, req.symbol)
        if now - snap.ts > cfg.max_snapshot_age:
            raise OrderRejected(RejectCode.STALE_MARKET_DATA, f"last snapshot {snap.ts.isoformat()}")
        if snap.suspended:
            raise OrderRejected(RejectCode.SUSPENDED, req.symbol)
        if req.price is not None and not (snap.lower_limit <= req.price <= snap.upper_limit):
            raise OrderRejected(RejectCode.PRICE_OUTSIDE_LIMITS,
                                f"{req.price} outside [{snap.lower_limit}, {snap.upper_limit}]")

        lot = lot_rule_for(req.symbol)
        if req.side is Side.BUY:
            if not lot.is_round(req.qty):
                raise OrderRejected(RejectCode.INVALID_LOT,
                                    f"buy qty must be >= {lot.min_qty} in steps of {lot.step}")
            reserve_price = req.price if req.price is not None else snap.upper_limit
            needed = self._buy_reservation_amount(reserve_price, req.qty, ZERO, ZERO)
            if needed > acct.available_cash:
                raise OrderRejected(RejectCode.INSUFFICIENT_CASH,
                                    f"needs {needed} incl. fees, available {acct.available_cash}")
        else:
            pos = acct.positions.get(req.symbol)
            sellable = pos.sellable_qty if pos else 0
            if req.qty > sellable:
                unfrozen_would_cover = pos is not None and req.qty <= pos.qty - pos.reserved_qty
                if unfrozen_would_cover:
                    raise OrderRejected(RejectCode.T1_LOCKED,
                                        f"{pos.frozen_qty} shares bought today are locked until T+1")
                raise OrderRejected(RejectCode.INSUFFICIENT_POSITION, f"sellable {sellable}")
            # Odd lots (零股) may only be sold together with the rest of the available position.
            if not lot.is_round(req.qty) and req.qty != sellable:
                raise OrderRejected(RejectCode.INVALID_LOT,
                                    f"sell qty must be round lots, or exactly the sellable {sellable} to close")
        return snap

    # ================================================================== matching
    def _budget(self, portfolio_id: str, snap: L1Snapshot) -> _Budget:
        key = (portfolio_id, snap.symbol)
        b = self._budgets.get(key)
        if b is None or b.seq != snap.seq:
            b = self._budgets[key] = _Budget(snap.seq)
        return b

    def _match_on_arrival(self, order: Order, snap: L1Snapshot, now: datetime, out: SubmitResult) -> None:
        budget = self._budget(order.portfolio_id, snap)
        take_bids = order.side is Side.SELL
        levels = snap.bids if take_bids else snap.asks
        for lv in levels:
            if order.remaining == 0:
                break
            if order.price is not None:
                if (not take_bids and lv.price > order.price) or (take_bids and lv.price < order.price):
                    break
            avail = lv.volume - budget.taken_levels.get((take_bids, lv.price), 0)
            qty = min(order.remaining, avail)
            if qty <= 0:
                continue
            budget.taken_levels[(take_bids, lv.price)] = budget.taken_levels.get((take_bids, lv.price), 0) + qty
            fill, event = self._apply_fill(order, lv.price, qty, Liquidity.TAKER, snap, now)
            out.fills.append(fill)
            out.events.append(event)

    def _init_queue(self, order: Order, snap: L1Snapshot) -> None:
        vol = snap.level_volume(side_is_bid=order.side is Side.BUY, price=order.price)  # type: ignore[arg-type]
        order.queue_initial = order.queue_ahead = vol

    def _match_resting(self, order: Order, snap: L1Snapshot, delta: int, out: TickResult) -> None:
        P = order.price
        assert P is not None
        is_buy = order.side is Side.BUY
        before = order.queue_ahead
        budget = self._budget(order.portfolio_id, snap)
        volume_left = max(0, delta - budget.taken_volume)

        if is_buy:
            crossed = snap.ask1 is not None and snap.ask1.price <= P
            through = snap.last_price < P
        else:
            crossed = snap.bid1 is not None and snap.bid1.price >= P
            through = snap.last_price > P
        at_level = snap.last_price == P
        at_limit = P == (snap.upper_limit if is_buy else snap.lower_limit)

        fill_qty, liquidity = 0, Liquidity.QUEUE
        if delta > 0 and (crossed or through):
            # The level we were queued on traded away (or the board broke): we are filled up to traded volume.
            fill_qty = min(order.remaining, volume_left)
            liquidity = Liquidity.BOARD_BREAK if at_limit else Liquidity.MAKER
            order.queue_ahead = 0
        elif delta > 0 and at_level and order.queue_ahead is not None:
            order.queue_ahead -= delta
            if order.queue_ahead < 0:
                fill_qty = min(order.remaining, -order.queue_ahead, volume_left)
                order.queue_ahead = 0

        if order.queue_ahead is None:
            # Our price was deeper than 5 levels; start queueing once it becomes visible.
            vis = snap.level_volume(side_is_bid=is_buy, price=P)
            if vis is not None:
                order.queue_initial = order.queue_ahead = vis
        else:
            vis = snap.level_volume(side_is_bid=is_buy, price=P)
            if vis is not None and vis < order.queue_ahead:
                order.queue_ahead = vis          # orders ahead of us cancelled

        if fill_qty > 0:
            budget.taken_volume += fill_qty
            fill, event = self._apply_fill(order, P, fill_qty, liquidity, snap, snap.ts)
            out.fills.append(fill)
            out.events.append(event)
        if order.status.is_open and at_limit and order.queue_ahead != before:
            out.events.append(self._queue_event(order, snap, snap.ts))

    # ================================================================== accounting
    def _buy_reservation_amount(self, reserve_price: Decimal, remaining: int,
                                filled_notional: Decimal, commission_charged: Decimal) -> Decimal:
        fees = self.config.fees
        remaining_notional = money(reserve_price * remaining)
        commission_left = fees.commission_due(filled_notional + remaining_notional) - commission_charged
        return remaining_notional + max(ZERO, commission_left) + fees.transfer_fee_upper_bound(
            remaining_notional, n_fills_upper_bound=remaining // 100 + 1)

    def _update_buy_reservation(self, order: Order, acct: Account) -> None:
        new = ZERO
        if order.status.is_open and order.remaining > 0:
            new = self._buy_reservation_amount(order.reserve_price, order.remaining,
                                               order.filled_notional, order.commission_charged)
        acct.reserved_cash += new - order.reserved_cash
        order.reserved_cash = new

    def _apply_fill(self, order: Order, price: Decimal, qty: int, liquidity: Liquidity,
                    snap: L1Snapshot, now: datetime) -> tuple[Fill, ArenaEvent]:
        if not (0 < qty <= order.remaining):
            raise InvariantViolation(f"fill qty {qty} vs remaining {order.remaining}")
        fees = self.config.fees
        acct = self._accounts[order.portfolio_id]
        pos = acct.position(order.symbol)

        notional = money(price * qty)
        order.filled_qty += qty
        order.filled_notional += notional
        commission_total = fees.commission_due(order.filled_notional)
        commission = commission_total - order.commission_charged
        order.commission_charged = commission_total
        stamp = ZERO if is_fund(order.symbol) else fees.stamp_duty(order.side, notional)
        transfer = fees.transfer_fee(notional)
        total_fees = commission + stamp + transfer

        cost_released = ZERO
        if order.side is Side.BUY:
            acct.cash -= notional + total_fees
            pos.qty += qty
            pos.frozen_qty += qty                       # T+1 lock
            pos.cost_basis += notional
        else:
            cost_released = pos.cost_basis if qty == pos.qty else money(pos.cost_basis * qty / pos.qty)
            acct.cash += notional - total_fees
            pos.qty -= qty
            pos.reserved_qty -= qty
            order.reserved_qty -= qty
            pos.cost_basis -= cost_released
            acct.realized_pnl += notional - cost_released
        acct.fees_paid += total_fees
        order.fees_paid += total_fees

        if order.remaining == 0:
            order.status = OrderStatus.FILLED
            order.queue_ahead = 0 if order.queue_ahead is not None else None
            self._open.get(order.symbol, {}).pop(order.id, None)
        else:
            order.status = OrderStatus.PARTIALLY_FILLED
        if order.side is Side.BUY:
            self._update_buy_reservation(order, acct)
        self._check_invariants(acct)

        fill = Fill(
            id=self._new_id(), order_id=order.id, portfolio_id=order.portfolio_id, debate_id=order.debate_id,
            symbol=order.symbol, side=order.side, price=price, qty=qty, notional=notional,
            commission=commission, stamp_duty=stamp, transfer_fee=transfer, liquidity=liquidity,
            snapshot_seq=snap.seq, trade_date=order.trade_date, executed_at=now, cost_released=cost_released,
        )
        etype = ev.ORDER_FILLED if order.status is OrderStatus.FILLED else ev.ORDER_PARTIALLY_FILLED
        payload = order.public_view() | {
            "fill_id": fill.id, "fill_price": str(price), "fill_qty": qty, "liquidity": liquidity.value,
            "commission": str(commission), "stamp_duty": str(stamp), "transfer_fee": str(transfer),
        }
        return fill, ArenaEvent(etype, now, payload)

    def _terminate(self, order: Order, status: OrderStatus, now: datetime, reason: str | None = None) -> ArenaEvent:
        acct = self._accounts[order.portfolio_id]
        order.status = status
        if order.side is Side.BUY:
            self._update_buy_reservation(order, acct)       # releases everything for a closed order
        else:
            acct.position(order.symbol).reserved_qty -= order.reserved_qty
            order.reserved_qty = 0
        self._open.get(order.symbol, {}).pop(order.id, None)
        self._check_invariants(acct)
        etype = ev.ORDER_EXPIRED if status is OrderStatus.EXPIRED else ev.ORDER_CANCELED
        return ArenaEvent(etype, now, order.public_view() | {"reason": reason})

    @staticmethod
    def _check_invariants(acct: Account) -> None:
        if acct.cash < 0 or acct.reserved_cash < 0 or acct.reserved_cash > acct.cash:
            raise InvariantViolation(f"{acct.portfolio_id}: cash={acct.cash} reserved={acct.reserved_cash}")
        for p in acct.positions.values():
            if min(p.qty, p.frozen_qty, p.reserved_qty) < 0 or p.frozen_qty + p.reserved_qty > p.qty:
                raise InvariantViolation(f"{acct.portfolio_id}/{p.symbol}: {p}")

    # ================================================================== show layer helpers
    @staticmethod
    def _at_limit_price(order: Order, snap: L1Snapshot) -> bool:
        return order.price == (snap.upper_limit if order.side is Side.BUY else snap.lower_limit)

    @staticmethod
    def _queue_event(order: Order, snap: L1Snapshot, now: datetime) -> ArenaEvent:
        return ArenaEvent(ev.LIMIT_QUEUE_UPDATE, now, {
            "order_id": order.id, "portfolio_id": order.portfolio_id, "symbol": order.symbol,
            "side": order.side.value, "limit_price": str(order.price), "remaining": order.remaining,
            "queue_initial": order.queue_initial, "queue_ahead": order.queue_ahead,
            "progress_pct": order.queue_progress_pct, "sealed": snap.limit_up_sealed or snap.limit_down_sealed,
        })

    def _board_transitions(self, prev: L1Snapshot | None, snap: L1Snapshot) -> list[ArenaEvent]:
        if prev is None:
            return []
        out: list[ArenaEvent] = []
        base = {"symbol": snap.symbol, "last_price": str(snap.last_price), "seq": snap.seq}
        if prev.limit_up_sealed and not snap.limit_up_sealed:
            out.append(ArenaEvent(ev.LIMIT_UP_BROKEN, snap.ts, base | {
                "upper_limit": str(snap.upper_limit), "ask1": _level(snap.ask1)}))
        elif snap.limit_up_sealed and not prev.limit_up_sealed:
            out.append(ArenaEvent(ev.LIMIT_UP_SEALED, snap.ts, base | {
                "upper_limit": str(snap.upper_limit), "seal_volume": snap.bids[0].volume}))
        if prev.limit_down_sealed and not snap.limit_down_sealed:
            out.append(ArenaEvent(ev.LIMIT_DOWN_BROKEN, snap.ts, base | {
                "lower_limit": str(snap.lower_limit), "bid1": _level(snap.bid1)}))
        elif snap.limit_down_sealed and not prev.limit_down_sealed:
            out.append(ArenaEvent(ev.LIMIT_DOWN_SEALED, snap.ts, base | {
                "lower_limit": str(snap.lower_limit), "seal_volume": snap.asks[0].volume}))
        return out

    def state_view(self, portfolio_id: str) -> dict:
        """Shape returned by GET /api/v1/agent/state."""
        acct = self.account(portfolio_id)
        positions = []
        market_value = ZERO
        for p in acct.positions.values():
            if p.qty == 0:
                continue
            book = self._books.get(p.symbol)
            mv = money(book.last_price * p.qty) if book else p.cost_basis
            market_value += mv
            positions.append({"symbol": p.symbol, "qty": p.qty, "sellable_qty": p.sellable_qty,
                              "frozen_qty": p.frozen_qty, "reserved_qty": p.reserved_qty,
                              "cost_basis": str(p.cost_basis), "market_value": str(mv)})
        open_orders = [o.public_view() for book in self._open.values() for o in book.values()
                       if o.portfolio_id == portfolio_id]
        return {
            "portfolio_id": portfolio_id, "trade_date": _s_date(self._trade_date),
            "cash": str(acct.cash), "available_cash": str(acct.available_cash),
            "reserved_cash": str(acct.reserved_cash), "market_value": str(market_value),
            "total_equity": str(acct.cash + market_value), "realized_pnl": str(acct.realized_pnl),
            "fees_paid": str(acct.fees_paid), "positions": positions, "open_orders": open_orders,
        }


def _level(lv) -> list | None:
    return None if lv is None else [str(lv.price), lv.volume]


def _s_date(d: date | None) -> str | None:
    return None if d is None else d.isoformat()
