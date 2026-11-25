from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from ..domain.errors import MarketDataError

MAX_LEVELS = 5


@dataclass(frozen=True, slots=True)
class Level:
    price: Decimal
    volume: int  # shares


@dataclass(frozen=True, slots=True)
class L1Snapshot:
    """A-share 3-second L1 5-level snapshot (Level-1 行情快照). All volumes are in SHARES.

    `bids` best-first (descending price), `asks` best-first (ascending price). Empty/zero levels are dropped,
    so a sealed limit-up board is simply `asks == ()` with `bids[0].price == upper_limit`.
    """

    symbol: str
    ts: datetime                      # exchange time, tz-aware
    seq: int                          # strictly increasing per symbol (feed sequence)
    bids: tuple[Level, ...]
    asks: tuple[Level, ...]
    last_price: Decimal
    cumulative_volume: int            # day cumulative traded shares
    upper_limit: Decimal
    lower_limit: Decimal
    prev_close: Decimal
    suspended: bool = False
    extra: dict = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        if self.ts.tzinfo is None:
            raise MarketDataError(f"{self.symbol}: snapshot ts must be tz-aware")
        object.__setattr__(self, "bids", tuple(lv for lv in self.bids if lv.volume > 0))
        object.__setattr__(self, "asks", tuple(lv for lv in self.asks if lv.volume > 0))
        if len(self.bids) > MAX_LEVELS or len(self.asks) > MAX_LEVELS:
            raise MarketDataError(f"{self.symbol}: more than {MAX_LEVELS} levels")
        if any(lv.volume < 0 for lv in (*self.bids, *self.asks)):
            raise MarketDataError(f"{self.symbol}: negative level volume")
        if any(nxt.price >= prv.price for nxt, prv in zip(self.bids[1:], self.bids)):
            raise MarketDataError(f"{self.symbol}: bids not strictly descending")
        if any(nxt.price <= prv.price for prv, nxt in zip(self.asks, self.asks[1:])):
            raise MarketDataError(f"{self.symbol}: asks not strictly ascending")
        if self.bids and self.asks and self.bids[0].price >= self.asks[0].price:
            raise MarketDataError(f"{self.symbol}: crossed book {self.bids[0].price} >= {self.asks[0].price}")
        if not (self.lower_limit < self.upper_limit):
            raise MarketDataError(f"{self.symbol}: invalid price limits")
        for lv in (*self.bids, *self.asks):
            if not (self.lower_limit <= lv.price <= self.upper_limit):
                raise MarketDataError(f"{self.symbol}: level {lv.price} outside limits")
        if self.cumulative_volume < 0:
            raise MarketDataError(f"{self.symbol}: negative cumulative volume")

    # -- convenience -------------------------------------------------------------------------------
    @property
    def bid1(self) -> Level | None:
        return self.bids[0] if self.bids else None

    @property
    def ask1(self) -> Level | None:
        return self.asks[0] if self.asks else None

    @property
    def limit_up_sealed(self) -> bool:
        """封板: best bid sits on the upper limit and nobody is offering."""
        return bool(self.bids) and self.bids[0].price == self.upper_limit and not self.asks

    @property
    def limit_down_sealed(self) -> bool:
        return bool(self.asks) and self.asks[0].price == self.lower_limit and not self.bids

    def level_volume(self, side_is_bid: bool, price: Decimal) -> int | None:
        """Visible volume at `price` on one side; 0 if the price is inside the spread / better than best;
        None if it is deeper than the 5 visible levels (unknown)."""
        levels = self.bids if side_is_bid else self.asks
        for lv in levels:
            if lv.price == price:
                return lv.volume
        if not levels:
            return 0
        best, worst = levels[0].price, levels[-1].price
        if side_is_bid:
            if price > best:
                return 0
            if price > worst:          # between visible levels -> empty level
                return 0
            return None if len(levels) == MAX_LEVELS else 0
        if price < best:
            return 0
        if price < worst:
            return 0
        return None if len(levels) == MAX_LEVELS else 0
