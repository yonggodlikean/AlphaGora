from __future__ import annotations

from datetime import timedelta

import pytest

from arena_engine.domain import events as ev
from arena_engine.domain.enums import Liquidity, OrderStatus, OrderType, RejectCode, Side
from arena_engine.domain.errors import OrderRejected
from conftest import ALICE, BOB, D, D1, D2, SYM, Tape, at, req

BOOK_BIDS = [("9.99", 500), ("9.98", 800), ("9.97", 1000), ("9.96", 1000), ("9.95", 1000)]
BOOK_ASKS = [("10.00", 300), ("10.01", 200), ("10.02", 100), ("10.03", 1000), ("10.04", 1000)]


def types(events):
    return [e.type for e in events]


# ============================================================ required scenario 1: partial fill
def test_marketable_limit_walks_book_then_rests_and_fills_on_next_tick(engine, tape):
    engine.on_snapshot(tape.snap(at("09:30:00"), BOOK_BIDS, BOOK_ASKS, "10.00", 10_000))

    res = engine.submit_order(req(qty=1000, price="10.02"), at("09:30:01"))
    o = res.order
    assert [(f.price, f.qty, f.liquidity) for f in res.fills] == [
        (D("10.00"), 300, Liquidity.TAKER), (D("10.01"), 200, Liquidity.TAKER), (D("10.02"), 100, Liquidity.TAKER)]
    assert o.status is OrderStatus.PARTIALLY_FILLED and o.filled_qty == 600 and o.remaining == 400
    assert o.avg_fill_price == D("10.0067")
    assert o.queue_ahead == 0                                 # 10.02 is inside the spread on the bid side
    assert types(res.events) == [ev.ORDER_ACCEPTED, ev.ORDER_PARTIALLY_FILLED,
                                 ev.ORDER_PARTIALLY_FILLED, ev.ORDER_PARTIALLY_FILLED]

    acct = engine.account(ALICE)
    # notional 6004.00; commission 6004 * 0.025% = 1.50 -> 5.00 minimum, charged once per order.
    assert [f.commission for f in res.fills] == [D("5.00"), D("0.00"), D("0.00")]
    assert acct.cash == D("1000000") - D("6004.00") - D("5.00")
    assert acct.reserved_cash == D("4008.00")                # 400 x 10.02; min commission already paid
    assert acct.positions[SYM].qty == 600 and acct.positions[SYM].frozen_qty == 600

    # Next tick: sellers now at 10.02 and 700 shares traded -> remaining 400 fills passively at our price.
    tick = engine.on_snapshot(tape.snap(at("09:30:03"), BOOK_BIDS[:1], [("10.02", 1000)], "10.02", 10_700))
    assert [(f.price, f.qty, f.liquidity) for f in tick.fills] == [(D("10.02"), 400, Liquidity.MAKER)]
    assert o.status is OrderStatus.FILLED and o.fees_paid == D("5.00")
    assert acct.reserved_cash == 0
    assert acct.cash == D("1000000") - D("10012.00") - D("5.00")


def test_portfolio_cannot_take_same_visible_liquidity_twice_in_one_snapshot(engine, tape):
    engine.on_snapshot(tape.snap(at("09:30:00"), BOOK_BIDS, BOOK_ASKS, "10.00", 10_000))
    first = engine.submit_order(req(qty=300, price="10.00"), at("09:30:01"))
    second = engine.submit_order(req(qty=300, price="10.00"), at("09:30:02"))
    assert first.order.filled_qty == 300
    assert second.order.filled_qty == 0                     # ask1 already taken by ALICE in this snapshot
    # ...but tenants are isolated: BOB sees the full public book.
    bob = engine.submit_order(req(portfolio=BOB, qty=300, price="10.00"), at("09:30:02"))
    assert bob.order.filled_qty == 300


# ============================================================ required scenario 2: T+1
def test_t_plus_one_blocks_same_day_sell_and_releases_next_day(engine, tape):
    engine.on_snapshot(tape.snap(at("09:30:00"), BOOK_BIDS, [("10.00", 5000)], "10.00", 10_000))
    engine.submit_order(req(qty=1000, price="10.00"), at("09:30:01"))
    pos = engine.account(ALICE).positions[SYM]
    assert (pos.qty, pos.frozen_qty, pos.sellable_qty) == (1000, 1000, 0)

    with pytest.raises(OrderRejected) as exc:
        engine.submit_order(req(side=Side.SELL, qty=1000, price="9.99"), at("09:30:10"))
    assert exc.value.code is RejectCode.T1_LOCKED
    with pytest.raises(OrderRejected) as exc:
        engine.submit_order(req(side=Side.SELL, qty=2000, price="9.99"), at("09:30:10"))
    assert exc.value.code is RejectCode.INSUFFICIENT_POSITION

    engine.end_trading_day(at("15:00:00"))
    engine.begin_trading_day(D2, at("09:15:00", D2))
    assert engine.account(ALICE).positions[SYM].sellable_qty == 1000

    tape2 = Tape()
    engine.on_snapshot(tape2.snap(at("09:30:00", D2), [("10.20", 3000)], [("10.21", 3000)], "10.20", 5_000))
    res = engine.submit_order(req(side=Side.SELL, qty=1000, price="10.20"), at("09:30:01", D2))
    (fill,) = res.fills
    assert (fill.price, fill.qty) == (D("10.20"), 1000)
    assert fill.commission == D("5.00")                      # 10200 * 0.025% = 2.55 -> min 5
    assert fill.stamp_duty == D("5.10")                      # 10200 * 0.05%, sell side only
    acct = engine.account(ALICE)
    assert acct.realized_pnl == D("200.00")
    assert acct.cash == D("1000000") - D("10000.00") - D("5.00") + D("10200.00") - D("10.10")


def test_lot_rules(engine, tape):
    engine.on_snapshot(tape.snap(at("09:30:00"), BOOK_BIDS, BOOK_ASKS, "10.00", 10_000))
    with pytest.raises(OrderRejected) as exc:
        engine.submit_order(req(qty=150), at("09:30:01"))
    assert exc.value.code is RejectCode.INVALID_LOT


# ============================================================ required scenario 3: limit-up queue
SEALED = dict(asks=[], last="11.00")


def test_limit_up_queue_drains_and_fills(engine, tape):
    engine.on_snapshot(tape.snap(at("10:00:00"), [("11.00", 50_000), ("10.99", 2_000)], cum=1_000_000, **SEALED))

    res = engine.submit_order(req(qty=10_000, price="11.00"), at("10:00:01"))
    o = res.order
    assert res.fills == [] and o.status is OrderStatus.NEW
    assert (o.queue_initial, o.queue_ahead, o.queue_progress_pct) == (50_000, 50_000, 0.0)
    assert types(res.events)[-1] == ev.LIMIT_QUEUE_UPDATE

    t = engine.on_snapshot(tape.snap(at("10:00:03"), [("11.00", 40_000)], cum=1_020_000, **SEALED))
    assert o.queue_ahead == 30_000 and o.queue_progress_pct == 40.0
    assert t.events[-1].payload["progress_pct"] == 40.0

    engine.on_snapshot(tape.snap(at("10:00:06"), [("11.00", 20_000)], cum=1_045_000, **SEALED))
    assert o.queue_ahead == 5_000

    # No trades, but the visible seal shrank to 3,000: at most 3,000 can still be ahead of us.
    engine.on_snapshot(tape.snap(at("10:00:09"), [("11.00", 3_000)], cum=1_045_000, **SEALED))
    assert o.queue_ahead == 3_000

    # 8,000 trade: 3,000 clears the queue ahead, the next 5,000 fill us.
    t = engine.on_snapshot(tape.snap(at("10:00:12"), [("11.00", 60_000)], cum=1_053_000, **SEALED))
    assert [(f.qty, f.price, f.liquidity) for f in t.fills] == [(5_000, D("11.00"), Liquidity.QUEUE)]
    assert o.status is OrderStatus.PARTIALLY_FILLED and o.queue_ahead == 0

    t = engine.on_snapshot(tape.snap(at("10:00:15"), [("11.00", 60_000)], cum=1_065_000, **SEALED))
    assert [f.qty for f in t.fills] == [5_000]
    assert o.status is OrderStatus.FILLED and o.queue_progress_pct == 100.0
    assert engine.account(ALICE).reserved_cash == 0


def test_limit_up_board_break_fills_every_queued_tenant(engine, tape):
    engine.on_snapshot(tape.snap(at("10:00:00"), [("11.00", 50_000)], cum=1_000_000, **SEALED))
    a = engine.submit_order(req(qty=10_000, price="11.00"), at("10:00:01")).order
    b = engine.submit_order(req(portfolio=BOB, qty=20_000, price="11.00", thesis="打板! sector leader"),
                            at("10:00:02")).order
    engine.on_snapshot(tape.snap(at("10:00:03"), [("11.00", 40_000)], cum=1_010_000, **SEALED))
    assert a.queue_ahead == b.queue_ahead == 40_000

    # 炸板: sellers appear at the limit price and trades print below it.
    t = engine.on_snapshot(tape.snap(at("10:00:06"), [("10.98", 5_000)], [("11.00", 30_000), ("11.01", 0)],
                                     "10.99", 1_070_000))
    assert ev.LIMIT_UP_BROKEN in types(t.events)
    assert {(f.portfolio_id, f.qty, f.price, f.liquidity) for f in t.fills} == {
        (ALICE, 10_000, D("11.00"), Liquidity.BOARD_BREAK), (BOB, 20_000, D("11.00"), Liquidity.BOARD_BREAK)}
    assert a.status is b.status is OrderStatus.FILLED


def test_market_order_on_sealed_board_cancels_immediately(engine, tape):
    engine.on_snapshot(tape.snap(at("10:00:00"), [("11.00", 50_000)], cum=1_000_000, **SEALED))
    res = engine.submit_order(req(qty=1000, price=None, order_type=OrderType.MARKET), at("10:00:01"))
    assert res.fills == [] and res.order.status is OrderStatus.CANCELED
    assert engine.account(ALICE).reserved_cash == 0


# ============================================================ guard rails
def test_idempotent_resubmit_and_conflict(engine, tape):
    engine.on_snapshot(tape.snap(at("09:30:00"), BOOK_BIDS, BOOK_ASKS, "10.00", 10_000))
    first = engine.submit_order(req(qty=100, price="9.90", key="openpup-0001"), at("09:30:01"))
    reserved = engine.account(ALICE).reserved_cash
    again = engine.submit_order(req(qty=100, price="9.90", key="openpup-0001"), at("09:30:02"))
    assert again.duplicate and again.order is first.order and again.events == []
    assert engine.account(ALICE).reserved_cash == reserved
    # Retries are answered even after the session closes.
    assert engine.submit_order(req(qty=100, price="9.90", key="openpup-0001"), at("12:00:00")).duplicate
    with pytest.raises(OrderRejected) as exc:
        engine.submit_order(req(qty=200, price="9.90", key="openpup-0001"), at("09:30:03"))
    assert exc.value.code is RejectCode.IDEMPOTENCY_CONFLICT


@pytest.mark.parametrize("hms", ["09:14:59", "11:30:00", "12:15:00", "15:00:00"])
def test_rejects_outside_continuous_sessions(engine, tape, hms):
    engine.on_snapshot(tape.snap(at("09:30:00"), BOOK_BIDS, BOOK_ASKS, "10.00", 10_000))
    with pytest.raises(OrderRejected) as exc:
        engine.submit_order(req(), at(hms))
    assert exc.value.code is RejectCode.MARKET_CLOSED


def test_rejects_missing_thesis_price_outside_limits_and_insufficient_cash(engine, tape):
    engine.on_snapshot(tape.snap(at("09:30:00"), BOOK_BIDS, BOOK_ASKS, "10.00", 10_000))
    cases = [
        (req(thesis=None), RejectCode.THESIS_REQUIRED),
        (req(price="11.01"), RejectCode.PRICE_OUTSIDE_LIMITS),
        (req(price="10.005"), RejectCode.INVALID_PRICE),
        (req(qty=100_000, price="10.00"), RejectCode.INSUFFICIENT_CASH),   # 1,000,000 + fees > cash
        (req(symbol="000001.SZ"), RejectCode.NO_MARKET_DATA),
    ]
    for r, code in cases:
        with pytest.raises(OrderRejected) as exc:
            engine.submit_order(r, at("09:30:05"))
        assert exc.value.code is code, r
    # A debate_id satisfies the "public reasoning" rule without a thesis.
    assert engine.submit_order(req(thesis=None, debate_id="deb-1", price="9.90"), at("09:30:05")).order
    with pytest.raises(OrderRejected) as exc:
        engine.submit_order(req(price="9.90"), at("09:30:05") + timedelta(minutes=5))
    assert exc.value.code is RejectCode.STALE_MARKET_DATA


def test_cancel_rate_limit_two_per_symbol_per_minute(engine, tape):
    engine.on_snapshot(tape.snap(at("09:30:00"), BOOK_BIDS, BOOK_ASKS, "10.00", 10_000))
    orders = [engine.submit_order(req(price="9.90"), at("09:30:01")).order for _ in range(4)]
    engine.cancel_order(ALICE, orders[0].id, at("09:30:05"))
    engine.cancel_order(ALICE, orders[1].id, at("09:30:10"))
    with pytest.raises(OrderRejected) as exc:
        engine.cancel_order(ALICE, orders[2].id, at("09:30:20"))
    assert exc.value.code is RejectCode.RATE_LIMITED
    assert orders[2].status is OrderStatus.NEW
    engine.cancel_order(ALICE, orders[2].id, at("09:31:05"))  # first cancel left the window
    with pytest.raises(OrderRejected) as exc:
        engine.cancel_order(ALICE, orders[0].id, at("09:32:00"))
    assert exc.value.code is RejectCode.ORDER_NOT_OPEN


def test_close_expires_open_orders_and_releases_reservations(engine, tape):
    engine.on_snapshot(tape.snap(at("09:30:00"), BOOK_BIDS, BOOK_ASKS, "10.00", 10_000))
    o = engine.submit_order(req(qty=1000, price="9.90"), at("09:30:01")).order
    assert engine.account(ALICE).reserved_cash == D("9905.00")   # 9900 + 5 min commission
    events = engine.end_trading_day(at("15:00:00"))
    assert o.status is OrderStatus.EXPIRED
    assert types(events) == [ev.ORDER_EXPIRED, ev.MARKET_CLOSE]
    assert engine.account(ALICE).reserved_cash == 0


def test_duplicate_snapshot_is_ignored(engine, tape):
    s = tape.snap(at("09:30:00"), BOOK_BIDS, BOOK_ASKS, "10.00", 10_000)
    engine.on_snapshot(s)
    engine.submit_order(req(qty=100, price="10.00"), at("09:30:01"))
    assert engine.on_snapshot(s).fills == []
