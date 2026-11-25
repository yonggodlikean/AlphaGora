"""Bridge between Agora market data and NautilusTrader's data model.

Live multi-tenant matching runs in `arena_engine.matching` (see docs/ARCHITECTURE.md for why). Nautilus is
used where it is strongest: a canonical instrument/data model and an offline backtest venue. The same tape
the arena replays can be fed into a Nautilus `BacktestEngine`, so bot authors can research against identical
data before entering a season.

    from arena_engine.adapters.nautilus_bridge import make_equity, to_depth10, to_trade_tick
"""
from __future__ import annotations

from decimal import Decimal

from ..market.snapshot import L1Snapshot

try:
    from nautilus_trader.model.currencies import CNY
    from nautilus_trader.model.data import BookOrder, OrderBookDepth10, QuoteTick, TradeTick
    from nautilus_trader.model.enums import AggressorSide, OrderSide
    from nautilus_trader.model.identifiers import InstrumentId, Symbol, TradeId, Venue
    from nautilus_trader.model.instruments import Equity
    from nautilus_trader.model.objects import Price, Quantity
except ImportError as exc:  # pragma: no cover - exercised only without the optional extra
    raise ImportError("install arena-engine[nautilus] to use the Nautilus bridge") from exc

from ..matching.fees import FeeSchedule
from ..matching.rules import lot_rule_for

# Agora symbol suffix -> Nautilus venue code.
VENUES = {"SH": "SSE", "SZ": "SZSE", "BJ": "BSE"}
PRICE_PRECISION = 2


def instrument_id(symbol: str) -> InstrumentId:
    code, _, exch = symbol.partition(".")
    try:
        return InstrumentId(Symbol(code), Venue(VENUES[exch]))
    except KeyError:
        raise ValueError(f"unsupported symbol {symbol!r}; expected e.g. 600000.SH / 000001.SZ / 830799.BJ") from None


def make_equity(symbol: str, fees: FeeSchedule | None = None, ts_ns: int = 0) -> Equity:
    fees = fees or FeeSchedule()
    iid = instrument_id(symbol)
    lot = lot_rule_for(symbol)
    return Equity(
        instrument_id=iid,
        raw_symbol=iid.symbol,
        currency=CNY,
        price_precision=PRICE_PRECISION,
        price_increment=Price.from_str("0.01"),
        lot_size=Quantity.from_int(lot.step),
        min_quantity=Quantity.from_int(lot.min_qty),
        ts_event=ts_ns,
        ts_init=ts_ns,
        # Nautilus fee rates are flat; min-commission and sell-only stamp duty need a custom FeeModel.
        maker_fee=fees.commission_rate,
        taker_fee=fees.commission_rate,
    )


def _ns(snap: L1Snapshot) -> int:
    return int(snap.ts.timestamp() * 1_000_000_000)


def _px(p: Decimal) -> Price:
    return Price(float(p), PRICE_PRECISION)


DEPTH = 10


def _null_order() -> BookOrder:
    return BookOrder(OrderSide.NO_ORDER_SIDE, Price(0, PRICE_PRECISION), Quantity.from_int(0), 0)


def to_depth10(snap: L1Snapshot) -> OrderBookDepth10:
    """5 visible levels padded to Nautilus' fixed depth of 10 with NULL orders (both sides must have
    equal length). Lists are built fresh because the constructor mutates its arguments."""
    bids = [BookOrder(OrderSide.BUY, _px(lv.price), Quantity.from_int(lv.volume), 0) for lv in snap.bids]
    asks = [BookOrder(OrderSide.SELL, _px(lv.price), Quantity.from_int(lv.volume), 0) for lv in snap.asks]
    bid_counts = [1] * len(bids) + [0] * (DEPTH - len(bids))
    ask_counts = [1] * len(asks) + [0] * (DEPTH - len(asks))
    bids += [_null_order() for _ in range(DEPTH - len(bids))]
    asks += [_null_order() for _ in range(DEPTH - len(asks))]
    ts = _ns(snap)
    return OrderBookDepth10(instrument_id(snap.symbol), bids, asks, bid_counts, ask_counts, 0, snap.seq, ts, ts)


def to_quote_tick(snap: L1Snapshot) -> QuoteTick | None:
    """Top of book; None when one side is empty (sealed limit-up/down), which Nautilus cannot represent."""
    if snap.bid1 is None or snap.ask1 is None:
        return None
    ts = _ns(snap)
    return QuoteTick(instrument_id(snap.symbol), _px(snap.bid1.price), _px(snap.ask1.price),
                     Quantity.from_int(snap.bid1.volume), Quantity.from_int(snap.ask1.volume), ts, ts)


def to_trade_tick(snap: L1Snapshot, delta_volume: int) -> TradeTick | None:
    """Synthesise the interval's traded volume as one trade at last_price, so Nautilus' trade-driven
    queue-position model sees the same flow the arena does. None when nothing traded."""
    if delta_volume <= 0:
        return None
    ts = _ns(snap)
    return TradeTick(instrument_id(snap.symbol), _px(snap.last_price), Quantity.from_int(delta_volume),
                     AggressorSide.NO_AGGRESSOR, TradeId(f"{snap.symbol}-{snap.seq}"), ts, ts)
