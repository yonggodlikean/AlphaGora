"""Synthetic market for demos and local play: one generated trading day per calendar trading day, looping
forever. Every day a different symbol plays the limit-up "dragon" (seal, queue, maybe break) and another one
crashes, so the queue bars, board breaks and conflicts all happen without a live feed. Codes 998xxx/999xxx are
deliberately not real securities."""
from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal

from .calendar import CST, TradingCalendar
from .snapshot import L1Snapshot, Level

TICK = Decimal("0.01")

DEMO_SYMBOLS: tuple[tuple[str, str, float], ...] = (
    ("999001.SH", "演示龙头", 11.70),
    ("999002.SH", "演示银行", 5.20),
    ("999003.SH", "演示电力", 23.80),
    ("998005.SZ", "演示科技", 36.50),
    ("998006.SZ", "演示医药", 18.40),
    ("998007.SZ", "演示消费", 52.10),
    ("999300.SH", "演示300ETF", 4.12),
)
STEADY = {"999002.SH", "999003.SH"}
INDEX = "999300.SH"
ROTATING = ("999001.SH", "998005.SZ", "998006.SZ", "998007.SZ")


@dataclass(frozen=True)
class EndOfDay:
    trade_date: date


def _q(x: float) -> Decimal:
    return Decimal(str(x)).quantize(TICK, rounding=ROUND_HALF_UP)


def session_times(step_s: int) -> list[time]:
    out = [time(9, 25)]
    for start, end in ((time(9, 30), time(11, 30)), (time(13, 0), time(15, 0))):
        t = datetime.combine(date(2000, 1, 1), start)
        stop = datetime.combine(date(2000, 1, 1), end)
        while t <= stop:
            out.append(t.time())
            t += timedelta(seconds=step_s)
    return out


class DemoMarket:
    def __init__(self, seed: int = 7, step_s: int = 15) -> None:
        self.seed = seed
        self.step_s = step_s
        self.prev_close = {s: Decimal(str(p)).quantize(TICK) for s, _, p in DEMO_SYMBOLS}
        self.names = {s: n for s, n, _ in DEMO_SYMBOLS}
        self.day_index = 0

    def roles_for(self, day_index: int) -> dict[str, str]:
        roles = {s: "steady" for s in STEADY}
        roles[INDEX] = "index"
        k = day_index % len(ROTATING)
        rot = ROTATING[k:] + ROTATING[:k]
        for sym, role in zip(rot, ("dragon", "crash", "trend", "volatile")):
            roles[sym] = role
        return roles

    def day(self, d: date) -> list[list[L1Snapshot]]:
        """All snapshots for one day, grouped by timestamp."""
        rng = random.Random(f"{self.seed}-{d.isoformat()}")
        roles = self.roles_for(self.day_index)
        self.day_index += 1
        times = session_times(self.step_s)
        per_symbol = {s: self._path(s, roles[s], times, d, rng) for s, _, _ in DEMO_SYMBOLS}
        for s, snaps in per_symbol.items():
            self.prev_close[s] = snaps[-1].last_price
        return [[per_symbol[s][i] for s, _, _ in DEMO_SYMBOLS] for i in range(len(times))]

    def _path(self, sym: str, role: str, times: list[time], d: date, rng: random.Random) -> list[L1Snapshot]:
        prev = self.prev_close[sym]
        upper = (prev * Decimal("1.1")).quantize(TICK, rounding=ROUND_HALF_UP)
        lower = (prev * Decimal("0.9")).quantize(TICK, rounding=ROUND_HALF_UP)
        p = float(prev) * (1 + rng.uniform(-0.01, 0.015))
        sigma = {"steady": 0.0007, "index": 0.0004, "trend": 0.0010, "volatile": 0.0022,
                 "crash": 0.0012, "dragon": 0.0012}[role]
        drift = {"steady": 0.0, "index": rng.uniform(-0.00002, 0.00003), "trend": 0.00008,
                 "volatile": 0.0, "crash": -0.00016, "dragon": 0.0}[role]
        n = len(times)
        seal_at = rng.randint(40, 140) if role == "dragon" else n + 1
        break_at = rng.randint(seal_at + 120, n - 60) if role == "dragon" and rng.random() < 0.65 else n + 1
        break_len = rng.randint(6, 30)
        reseal = rng.random() < 0.5
        queue = rng.randint(20_000, 80_000) * 100      # shares sealed on bid1 at the limit (2-8M)
        cum = rng.randint(2000, 9000) * 100
        out: list[L1Snapshot] = []
        lo_cap, hi_cap = float(lower) * 1.002, float(upper) * 0.995

        for i, t in enumerate(times):
            ts = datetime.combine(d, t, tzinfo=CST)
            traded = rng.randint(50, 2500) * 100 if i else 0
            sealed = False
            if role == "dragon" and i < seal_at:
                target = float(prev) + (float(upper) - float(prev)) * (i / seal_at) ** 0.8
                p = target * (1 + rng.gauss(0, 0.002))
                p = min(p, float(upper) - 0.02)
            elif role == "dragon" and (i < break_at or (reseal and i >= break_at + break_len)):
                sealed = True
                # Roughly as many new buyers join the back as trades eat the front: a queue at the back
                # takes minutes of market time to reach the front, and the board may break first.
                traded = rng.randint(300, 3000) * 100
                queue = max(200_000, queue - traded + rng.randint(0, 3200) * 100)
                p = float(upper)
            elif role == "dragon":
                traded = rng.randint(4000, 12000) * 100
                if i < break_at + break_len:
                    p = float(upper) - rng.randint(1, 15) * 0.01
                else:
                    p *= 1 + rng.gauss(-0.0003, 0.0015)
                p = min(p, float(upper) - 0.01)
            else:
                p *= 1 + drift + rng.gauss(0, sigma)
                p = max(lo_cap, min(hi_cap, p))
            cum += traded
            last = upper if sealed else _q(p)

            if sealed:
                bids = [Level(upper, queue)] + [Level(upper - TICK * k, rng.randint(5, 300) * 100) for k in range(1, 5)]
                asks: list[Level] = []
            else:
                bid1 = min(last, upper - TICK) if last >= upper else last
                bids = [Level(bid1 - TICK * k, rng.randint(10, 600) * 100) for k in range(5)]
                asks = [Level(bid1 + TICK * (k + 1), rng.randint(10, 600) * 100) for k in range(5)]
                if role == "dragon" and break_at <= i < break_at + break_len:
                    asks[0] = Level(asks[0].price, rng.randint(2000, 8000) * 100)  # sellers dumping at the top
                bids = [lv for lv in bids if lv.price >= lower]
                asks = [lv for lv in asks if lv.price <= upper]
            out.append(L1Snapshot(
                symbol=sym, ts=ts, seq=i + 1, bids=tuple(bids), asks=tuple(asks), last_price=last,
                cumulative_volume=cum, upper_limit=upper, lower_limit=lower, prev_close=prev,
                extra={"name": self.names[sym]},
            ))
        return out


class DemoFeed:
    """Yields snapshots (grouped by time, sleeping step/speed between groups) and an EndOfDay after each day."""

    def __init__(self, calendar: TradingCalendar, start: date, speed: float = 30.0, step_s: int = 15,
                 seed: int = 7) -> None:
        self.calendar = calendar
        self.start = start
        self.speed = speed
        self.market = DemoMarket(seed=seed, step_s=step_s)
        self._closed = False
        self.max_days: int | None = None

    async def subscribe(self, symbols: Sequence[str]) -> None:
        return None

    async def stream(self) -> AsyncIterator[L1Snapshot | EndOfDay]:
        d = self.start
        days = 0
        while not self._closed:
            while not self.calendar.is_trading_day(d):
                d += timedelta(days=1)
            for group in self.market.day(d):
                if self._closed:
                    return
                for snap in group:
                    yield snap
                if self.speed > 0:
                    await asyncio.sleep(self.market.step_s / self.speed)
                else:
                    await asyncio.sleep(0)
            yield EndOfDay(d)
            days += 1
            if self.max_days is not None and days >= self.max_days:
                return
            d += timedelta(days=1)

    async def close(self) -> None:
        self._closed = True
