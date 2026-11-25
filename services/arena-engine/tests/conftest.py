from __future__ import annotations

import itertools
from datetime import date, datetime, time
from decimal import Decimal

import pytest

from arena_engine.domain.enums import ControllerType, OrderType, Side
from arena_engine.domain.models import OrderRequest
from arena_engine.market.calendar import CST, TradingCalendar
from arena_engine.market.snapshot import L1Snapshot, Level
from arena_engine.matching import AShareMatchingEngine

D1 = date(2026, 11, 20)   # Friday
D2 = date(2026, 11, 23)   # Monday (T+1 across a weekend)
SYM = "600000.SH"
ALICE = "pf-alice"
BOB = "pf-bob"


def at(hms: str, day: date = D1) -> datetime:
    return datetime.combine(day, time.fromisoformat(hms), tzinfo=CST)


def D(x) -> Decimal:
    return Decimal(str(x))


class Tape:
    """Builds consecutive snapshots for one symbol with auto-incrementing seq and 3s spacing."""

    def __init__(self, symbol=SYM, upper="11.00", lower="9.00", prev_close="10.00"):
        self.symbol, self.upper, self.lower, self.prev_close = symbol, D(upper), D(lower), D(prev_close)
        self._seq = itertools.count(1)

    def snap(self, ts: datetime, bids, asks, last, cum, **kw) -> L1Snapshot:
        return L1Snapshot(
            symbol=self.symbol, ts=ts, seq=next(self._seq),
            bids=tuple(Level(D(p), v) for p, v in bids), asks=tuple(Level(D(p), v) for p, v in asks),
            last_price=D(last), cumulative_volume=cum, upper_limit=self.upper, lower_limit=self.lower,
            prev_close=self.prev_close, **kw,
        )


def req(portfolio=ALICE, side=Side.BUY, qty=100, price="10.00", key=None, symbol=SYM,
        order_type=OrderType.LIMIT, thesis="Test thesis: mean reversion", debate_id=None) -> OrderRequest:
    return OrderRequest(
        portfolio_id=portfolio, symbol=symbol, side=side, order_type=order_type, qty=qty,
        idempotency_key=key or f"key-{next(_keys):06d}",
        price=None if price is None else D(price), thesis=thesis, debate_id=debate_id,
    )


_keys = itertools.count(1)


@pytest.fixture
def engine() -> AShareMatchingEngine:
    ids = itertools.count(1)
    eng = AShareMatchingEngine(TradingCalendar(), id_factory=lambda: f"id-{next(ids):04d}")
    eng.open_account(ALICE, ControllerType.OPENPUP_AI, D("1000000"))
    eng.open_account(BOB, ControllerType.HUMAN, D("1000000"))
    eng.begin_trading_day(D1, at("09:15:00"))
    return eng


@pytest.fixture
def tape() -> Tape:
    return Tape()
