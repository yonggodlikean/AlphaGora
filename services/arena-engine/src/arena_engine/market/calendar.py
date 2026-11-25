from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

CST = ZoneInfo("Asia/Shanghai")

# Continuous-auction sessions. Call auctions (09:15-09:25 open, 14:57-15:00 SZ/SH close) are V2.
CONTINUOUS_SESSIONS: tuple[tuple[time, time], ...] = (
    (time(9, 30), time(11, 30)),
    (time(13, 0), time(15, 0)),
)


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

    def phase(self, ts: datetime) -> str:
        """Where the wall clock is in the exchange day: holiday | pre_open | auction | trading | lunch | closed.
        (Calendar only; whether quotes are actually flowing is the caller's business.)"""
        local = ts.astimezone(CST)
        if not self.is_trading_day(local.date()):
            return "holiday"
        t = local.time()
        if t < time(9, 15):
            return "pre_open"
        if t < self.sessions[0][0]:
            return "auction"
        if self.is_continuous_trading(local):
            return "trading"
        if t < self.sessions[-1][0]:
            return "lunch"
        return "closed"

    @staticmethod
    def trade_date_of(ts: datetime) -> date:
        return ts.astimezone(CST).date()
