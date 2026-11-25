from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

CST = ZoneInfo("Asia/Shanghai")

# Continuous-auction sessions. The call auctions around them: open 09:15-09:25 (cancels until 09:20), matched at
# 09:25; 09:25-09:30 orders wait for 09:30; close 14:57-15:00 (no cancels), matched at 15:00.
CONTINUOUS_SESSIONS: tuple[tuple[time, time], ...] = (
    (time(9, 30), time(11, 30)),
    (time(13, 0), time(14, 57)),
)
OPEN_AUCTION = (time(9, 15), time(9, 25))
OPEN_WAIT = (time(9, 25), time(9, 30))
CLOSE_AUCTION = (time(14, 57), time(15, 0))
CANCEL_CUTOFF = time(9, 20)


@dataclass(slots=True)
class TradingCalendar:
    """SSE/SZSE trading calendar. `holidays` must be loaded from the exchange calendar each year
    (CSRC publishes it in December); weekends are always closed."""

    holidays: frozenset[date] = field(default_factory=frozenset)
    sessions: tuple[tuple[time, time], ...] = CONTINUOUS_SESSIONS

    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.holidays

    def is_continuous_trading(self, ts: datetime) -> bool:
        if ts.tzinfo is None:
            raise ValueError("timestamp must be tz-aware")
        local = ts.astimezone(CST)
        if not self.is_trading_day(local.date()):
            return False
        t = local.time()
        # [start, end): an order stamped exactly 11:30:00 or 15:00:00 is outside the session.
        return any(start <= t < end for start, end in self.sessions)

    def order_window(self, ts: datetime) -> str | None:
        """Which kind of order entry is open: call_open | wait | continuous | call_close, or None."""
        local = ts.astimezone(CST)
        if not self.is_trading_day(local.date()):
            return None
        t = local.time()
        if OPEN_AUCTION[0] <= t < OPEN_AUCTION[1]:
            return "call_open"
        if OPEN_WAIT[0] <= t < OPEN_WAIT[1]:
            return "wait"
        if CLOSE_AUCTION[0] <= t < CLOSE_AUCTION[1]:
            return "call_close"
        return "continuous" if self.is_continuous_trading(local) else None

    def can_cancel(self, ts: datetime) -> bool:
        """Exchanges refuse cancels 09:20-09:30 and 14:57-15:00."""
        t = ts.astimezone(CST).time()
        return not (CANCEL_CUTOFF <= t < OPEN_WAIT[1] or CLOSE_AUCTION[0] <= t < CLOSE_AUCTION[1])

    def phase(self, ts: datetime) -> str:
        """Where the wall clock is in the exchange day: holiday | pre_open | auction | open_wait | trading |
        lunch | closing_auction | closed. (Calendar only; whether quotes flow is the caller's business.)"""
        local = ts.astimezone(CST)
        if not self.is_trading_day(local.date()):
            return "holiday"
        t = local.time()
        if t < OPEN_AUCTION[0]:
            return "pre_open"
        window = self.order_window(local)
        if window == "call_open":
            return "auction"
        if window == "wait":
            return "open_wait"
        if window == "call_close":
            return "closing_auction"
        if window == "continuous":
            return "trading"
        if t < self.sessions[-1][0]:
            return "lunch"
        return "closed"

    @staticmethod
    def trade_date_of(ts: datetime) -> date:
        return ts.astimezone(CST).date()
