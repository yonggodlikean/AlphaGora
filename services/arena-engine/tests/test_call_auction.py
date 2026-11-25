"""Opening (09:15-09:25) and closing (14:57-15:00) call auctions."""
from __future__ import annotations

import pytest

from arena_engine.domain.enums import Liquidity, OrderStatus, OrderType, RejectCode, Side
from arena_engine.domain.errors import OrderRejected
from arena_engine.market.calendar import TradingCalendar
from conftest import ALICE, BOB, D, D1, SYM, at, req


def auction(tape, hms, price=None, vol=0, ub=0, us=0, cum=0):
    info = None if price is None else {"price": price, "volume": vol, "unmatched_buy": ub, "unmatched_sell": us}
    return tape.snap(at(hms), [], [], price or "10.00", cum, call_auction=True, auction=info)


def test_opening_auction_fills_better_prices_at_the_open_and_rests_the_rest(engine, tape):
    engine.on_snapshot(auction(tape, "09:15:03", "10.10", 50_000, ub=2_000))
    better = engine.submit_order(req(qty=1000, price="10.20"), at("09:16:00"))
    below = engine.submit_order(req(portfolio=BOB, qty=1000, price="10.00"), at("09:17:00"))
    assert better.fills == [] and better.order.status is OrderStatus.NEW          # nothing trades in the auction
    engine.on_snapshot(auction(tape, "09:24:57", "10.10", 60_000, ub=1_500))
    # 09:25: opened at 10.10, 60,000 shares traded, bid1 left at 10.10 (buyers were long)
    tick = engine.on_snapshot(tape.snap(at("09:25:03"), [("10.10", 1_500), ("10.09", 800)], [("10.11", 900)],
                                        "10.10", 60_000))
    assert [(f.order_id, f.price, f.qty, f.liquidity) for f in tick.fills] == [
        (better.order.id, D("10.10"), 1000, Liquidity.AUCTION)]
    assert below.order.status is OrderStatus.NEW and below.order.queue_ahead == 0  # inside the spread: rests


def test_at_the_open_price_only_the_short_side_fills(engine, tape):
    acct = engine.account(BOB)
    pos = acct.position(SYM)
    pos.qty, pos.cost_basis = 1000, D("9000")
    engine.on_snapshot(auction(tape, "09:15:03", "10.10", 50_000))
    buy = engine.submit_order(req(qty=500, price="10.10"), at("09:16:00"))
    sell = engine.submit_order(req(portfolio=BOB, side=Side.SELL, qty=500, price="10.10"), at("09:16:00"))
    engine.on_snapshot(auction(tape, "09:24:57", "10.10", 50_000, ub=1_500))
    tick = engine.on_snapshot(tape.snap(at("09:25:03"), [("10.10", 1_500)], [("10.11", 900)], "10.10", 50_000))
    assert [f.order_id for f in tick.fills] == [sell.order.id]         # sellers were short at 10.10
    assert buy.order.status is OrderStatus.NEW and buy.order.queue_ahead == 1_500   # behind the leftover buyers


def test_one_price_limit_up_open_queues_the_auction_buyer_behind_the_seal(engine, tape):
    engine.on_snapshot(auction(tape, "09:15:03", "11.00", 10_000, ub=900_000))
    o = engine.submit_order(req(qty=1000, price="11.00"), at("09:15:30"))
    engine.on_snapshot(auction(tape, "09:24:57", "11.00", 20_000, ub=2_000_000))
    tick = engine.on_snapshot(tape.snap(at("09:25:03"), [("11.00", 2_000_000)], [], "11.00", 20_000))
    assert tick.fills == [] and o.order.queue_ahead == 2_000_000


def test_auction_fills_are_capped_at_a_tenth_of_the_real_volume(engine, tape):
    engine.on_snapshot(auction(tape, "09:15:03", "10.10", 5_000))
    o = engine.submit_order(req(qty=5_000, price="10.50"), at("09:16:00"))
    engine.on_snapshot(tape.snap(at("09:25:03"), [("10.09", 800)], [("10.11", 900)], "10.10", 5_000))
    assert o.order.filled_qty == 500 and o.order.status is OrderStatus.PARTIALLY_FILLED


def test_auction_accepts_limit_orders_only_and_cancels_stop_at_0920(engine, tape):
    engine.on_snapshot(auction(tape, "09:15:03", "10.10", 5_000))
    with pytest.raises(OrderRejected) as exc:
        engine.submit_order(req(price=None, order_type=OrderType.MARKET), at("09:16:00"))
    assert exc.value.code is RejectCode.INVALID_PRICE
    early = engine.submit_order(req(qty=100, price="10.00"), at("09:16:00"))
    late = engine.submit_order(req(qty=100, price="10.00"), at("09:16:01"))
    engine.cancel_order(ALICE, early.order.id, at("09:19:59"))
    engine.on_snapshot(auction(tape, "09:20:03", "10.10", 5_000))
    with pytest.raises(OrderRejected) as exc:
        engine.cancel_order(ALICE, late.order.id, at("09:20:01"))
    assert exc.value.code is RejectCode.CANCEL_NOT_ALLOWED


def test_orders_entered_0925_0930_meet_the_book_at_0930(engine, tape):
    engine.on_snapshot(auction(tape, "09:15:03", "10.10", 5_000))
    engine.on_snapshot(tape.snap(at("09:25:03"), [("10.09", 800)], [("10.11", 900)], "10.10", 5_000))
    o = engine.submit_order(req(qty=500, price="10.11"), at("09:27:00"))
    assert o.fills == [] and o.order.deferred
    engine.on_snapshot(tape.snap(at("09:28:00"), [("10.09", 800)], [("10.11", 900)], "10.10", 5_000))
    assert o.order.filled_qty == 0                                       # still waiting
    tick = engine.on_snapshot(tape.snap(at("09:30:00"), [("10.09", 800)], [("10.11", 900)], "10.10", 5_000))
    assert [(f.price, f.qty, f.liquidity) for f in tick.fills] == [(D("10.11"), 500, Liquidity.TAKER)]


def test_closing_auction_stops_continuous_matching_and_matches_at_1500(engine, tape):
    engine.on_snapshot(tape.snap(at("14:56:57"), [("9.99", 500)], [("10.00", 300)], "10.00", 1_000_000))
    resting = engine.submit_order(req(qty=1000, price="10.05"), at("14:56:58"))   # takes 300 on arrival
    assert resting.order.filled_qty == 300
    engine.on_snapshot(auction(tape, "14:57:03", "10.02", 80_000, us=100, cum=1_000_000))
    late = engine.submit_order(req(portfolio=BOB, qty=1000, price="10.01"), at("14:58:00"))
    assert late.fills == []
    with pytest.raises(OrderRejected) as exc:
        engine.cancel_order(BOB, late.order.id, at("14:58:30"))
    assert exc.value.code is RejectCode.CANCEL_NOT_ALLOWED
    # 15:00: closed at 10.02 with 80,000 shares in the auction; sellers left over at 10.02
    tick = engine.on_snapshot(tape.snap(at("15:00:03"), [("10.01", 400)], [("10.02", 100)], "10.02", 1_080_000))
    assert [(f.order_id, f.price, f.qty) for f in tick.fills] == [(resting.order.id, D("10.02"), 700)]
    assert late.order.filled_qty == 0                                    # 10.01 < close: expires at 15:00:05
    events = engine.end_trading_day(at("15:00:05"))
    assert late.order.status is OrderStatus.EXPIRED and events


def test_calendar_windows():
    cal = TradingCalendar()
    assert [cal.order_window(at(t)) for t in ("09:14:59", "09:15:00", "09:25:00", "09:30:00", "14:57:00", "15:00:00")] \
        == [None, "call_open", "wait", "continuous", "call_close", None]
    assert [cal.can_cancel(at(t)) for t in ("09:19:59", "09:20:00", "09:29:59", "10:00:00", "14:57:00")] \
        == [True, False, False, True, False]
    assert [cal.phase(at(t)) for t in ("09:16:00", "09:26:00", "14:58:00")] == ["auction", "open_wait", "closing_auction"]
    assert D1.weekday() < 5


# ------------------------------------------------------------------ through the service and the database
from datetime import datetime, time as dtime  # noqa: E402

from arena_engine.market.calendar import CST  # noqa: E402
from arena_engine.market.snapshot import L1Snapshot, Level  # noqa: E402
from test_service_api import DAY1, boot  # noqa: E402,F401  (fixtures pg/dsn/anyio_backend come from there)
from test_service_api import anyio_backend, dsn, pg  # noqa: E402,F401


def _snap(hms, seq, *, bids=(), asks=(), last="5.25", cum=0, call=False, info=None):
    return L1Snapshot("999002.SH", datetime.combine(DAY1, dtime.fromisoformat(hms), CST), seq,
                      tuple(Level(D(p), v) for p, v in bids), tuple(Level(D(p), v) for p, v in asks),
                      D(last), cum, D("5.72"), D("4.68"), D("5.20"), call_auction=call, auction=info)


@pytest.mark.anyio
async def test_auction_order_through_the_api_is_filled_and_persisted(dsn):
    service, client, pool = await boot(dsn, house_bots=False)
    await service.on_snapshot(_snap("09:15:03", 1, call=True, last="5.25",
                                    info={"price": "5.25", "volume": 100_000, "unmatched_buy": 0, "unmatched_sell": 0}))
    assert service.engine.day_open and service.market_status()["phase"] == "auction"
    r = await client.post("/api/v1/join", json={"display_name": "竞价玩家", "kind": "human"})
    auth = {"Authorization": f"Bearer {r.json()['token']}"}
    o = await client.post("/api/v1/agent/orders", headers=auth, json={
        "idempotency_key": "auction-0001", "symbol": "999002.SH", "side": "buy", "price": "5.30", "qty": 1000,
        "thesis": "竞价抢筹试试"})
    assert o.status_code == 201 and o.json()["status"] == "new", o.text
    await service.on_snapshot(_snap("09:25:03", 2, bids=[("5.24", 900)], asks=[("5.26", 800)], last="5.25",
                                    cum=200_000))
    mine = (await client.get("/api/v1/agent/orders", headers=auth)).json()["orders"]
    assert mine[0]["status"] == "filled" and mine[0]["avg_fill_price"] == "5.2500"
    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT liquidity::text FROM trades WHERE order_id = $1",
                                   __import__("uuid").UUID(o.json()["order_id"])) == "auction"
    await client.aclose(); await service.stop(); await pool.close()
