"""Static A-share instrument rules that are not carried by the snapshot."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

TICK = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class LotRule:
    min_qty: int
    step: int

    def is_round(self, qty: int) -> bool:
        return qty >= self.min_qty and (qty - self.min_qty) % self.step == 0


MAIN_BOARD = LotRule(min_qty=100, step=100)   # SSE/SZSE main board, ChiNext: 1 手 = 100 shares
STAR_MARKET = LotRule(min_qty=200, step=1)    # 科创板 688xxx: >= 200, then 1-share increments
BSE = LotRule(min_qty=100, step=1)            # 北交所: >= 100, then 1-share increments


def lot_rule_for(symbol: str) -> LotRule:
    code, _, exch = symbol.partition(".")
    if exch == "BJ":
        return BSE
    if exch == "SH" and code.startswith("688"):
        return STAR_MARKET
    return MAIN_BOARD


FUND_TICK = Decimal("0.001")


def is_fund(symbol: str) -> bool:
    """Exchange-traded funds (ETF/LOF): SH 5xxxxx, SZ 15xxxx/16xxxx. 0.001 tick, no stamp duty."""
    code, _, exch = symbol.partition(".")
    return (exch == "SH" and code.startswith("5")) or (exch == "SZ" and code[:2] in ("15", "16"))


def tick_for(symbol: str) -> Decimal:
    return FUND_TICK if is_fund(symbol) else TICK


def is_valid_tick(price: Decimal, tick: Decimal = TICK) -> bool:
    return price > 0 and price == price.quantize(tick)
