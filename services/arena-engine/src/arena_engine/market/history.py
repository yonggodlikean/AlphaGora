"""Daily history and the indicators the house teams trade on.

The market gateway publishes ~250 forward-adjusted daily bars per symbol to Redis before the open (`md:hist`,
see services/market-gateway/src/history.rs). Indicators always combine that history with today's live price as
the last, still-forming bar, so "MA5 crossed MA20" means what a trader looking at a chart at 14:50 would say.

Demo and test markets have no history, so `synthetic()` makes a deterministic one that ends at the demo's
previous close and gives every strategy something to do.
"""
from __future__ import annotations

import json
import math
import random
from array import array
from dataclasses import dataclass, field
from datetime import date


@dataclass(slots=True)
class Bars:
    dates: list[int]                   # yyyymmdd
    close: array
    high: array
    low: array
    volume: array                      # shares
    amount: array                      # yuan
    limit_up: bytes                    # 1 = closed at the limit-up price that day (raw prices)
    div_ttm: float = 0.0               # cash dividends per share over the last 12 months
    float_shares: float = 0.0

    @classmethod
    def from_json(cls, d: dict) -> "Bars":
        n = len(d["d"])
        return cls(list(d["d"]), array("d", d["c"]), array("d", d.get("h", d["c"])), array("d", d.get("l", d["c"])),
                   array("d", d.get("v", [0.0] * n)), array("d", d.get("a", [0.0] * n)),
                   bytes(d.get("lu", [0] * n)), float(d.get("div_ttm") or 0.0), float(d.get("float_shares") or 0.0))

    def before(self, day: date) -> "Bars":
        """Only bars strictly before `day` (history must never contain the day being traded)."""
        cut = day.year * 10_000 + day.month * 100 + day.day
        n = len(self.dates)
        while n and self.dates[n - 1] >= cut:
            n -= 1
        if n == len(self.dates):
            return self
        return Bars(self.dates[:n], self.close[:n], self.high[:n], self.low[:n], self.volume[:n], self.amount[:n],
                    self.limit_up[:n], self.div_ttm, self.float_shares)


@dataclass
class History:
    bars: dict[str, Bars] = field(default_factory=dict)
    members: dict[str, list[str]] = field(default_factory=dict)      # index code -> symbols
    indices: dict[str, dict] = field(default_factory=dict)           # index code -> {"d","c","up","down"}
    built: str | None = None                                          # gateway build date (yyyy-mm-dd)

    # ------------------------------------------------------------------ loading
    @classmethod
    async def from_redis(cls, client) -> "History":
        meta_raw = await client.get("md:hist:meta")
        if not meta_raw:
            return cls()
        meta = json.loads(meta_raw)
        raw = await client.hgetall("md:hist")
        bars = {}
        for k, v in raw.items():
            sym = k.decode() if isinstance(k, bytes) else k
            try:
                bars[sym] = Bars.from_json(json.loads(v))
            except (ValueError, KeyError, TypeError):
                continue
        return cls(bars, meta.get("members") or {}, meta.get("indices") or {}, meta.get("date"))

    def member_of(self, *indices: str) -> set[str]:
        return {s for code in indices for s in self.members.get(code, [])}

    # ------------------------------------------------------------------ demo / tests
    @classmethod
    def synthetic(cls, prev_close: dict[str, float], today: date, *, dividend: tuple[str, ...] = (),
                  days: int = 250, seed: int = 7) -> "History":
        """A plausible past for each symbol, ending exactly at its previous close. Shapes rotate by symbol so
        the demo has an uptrend (MA crossover), an oversold name in a long uptrend (mean reversion) and
        dividend payers with low volatility."""
        bars: dict[str, Bars] = {}
        shapes = ("uptrend_dip", "trend_up", "steady", "range", "downtrend")
        for i, (sym, last) in enumerate(sorted(prev_close.items())):
            rng = random.Random(f"{seed}:{sym}")
            shape = "steady" if sym in dividend else shapes[i % len(shapes)]
            drift, vol = {"uptrend_dip": (0.0012, 0.015), "trend_up": (0.0015, 0.018), "steady": (0.0002, 0.006),
                          "range": (0.0, 0.02), "downtrend": (-0.0012, 0.02)}[shape]
            rets = [rng.gauss(drift, vol) for _ in range(days)]
            if shape == "uptrend_dip":                     # a sharp 3-day drop at the end of a long rise
                rets[-3:] = [-0.04, -0.045, -0.035]
            if shape == "trend_up":                        # a short dip that has just turned back up
                rets[-8:-3] = [-0.012] * 5
                rets[-3:] = [0.02, 0.025, 0.03]
            closes = [1.0]
            for r in rets[1:]:
                closes.append(closes[-1] * math.exp(r))
            scale = last / closes[-1]
            closes = [round(c * scale, 2) for c in closes]
            ds, d = [], today.toordinal()
            while len(ds) < days:                          # weekdays going back from yesterday
                d -= 1
                dt = date.fromordinal(d)
                if dt.weekday() < 5:
                    ds.append(dt.year * 10_000 + dt.month * 100 + dt.day)
            ds.reverse()
            hi = [round(c * (1 + abs(rng.gauss(0, vol / 2))), 2) for c in closes]
            lo = [round(c * (1 - abs(rng.gauss(0, vol / 2))), 2) for c in closes]
            v = [float(rng.randint(5_000_000, 20_000_000)) for _ in closes]
            bars[sym] = Bars(ds, array("d", closes), array("d", hi), array("d", lo), array("d", v),
                             array("d", [c * x for c, x in zip(closes, v)]), bytes(days),
                             round(last * 0.055, 4) if sym in dividend else 0.0, 1e8)
        syms = sorted(prev_close)
        return cls(bars, {"000300": syms, "000016": syms, "000852": []}, {}, today.isoformat())

    # ------------------------------------------------------------------ keep it current
    def append_day(self, day: date, sym: str, close: float, high: float, low: float, volume: float,
                   amount: float, limit_up: bool) -> None:
        """Adds a finished trading day (at the close), so multi-day demos and a gateway that failed to refresh
        still have yesterday in the history."""
        b = self.bars.get(sym)
        dd = day.year * 10_000 + day.month * 100 + day.day
        if b is None or (b.dates and b.dates[-1] >= dd):
            return
        b.dates.append(dd)
        b.close.append(close)
        b.high.append(high)
        b.low.append(low)
        b.volume.append(volume)
        b.amount.append(amount)
        b.limit_up = b.limit_up + bytes([1 if limit_up else 0])


# ---------------------------------------------------------------------- indicators
# `c` is the close series with today's live price appended as the last element.

def sma(c, n: int, back: int = 0) -> float | None:
    """Simple moving average of the n closes ending `back` bars before the last one."""
    end = len(c) - back
    if end < n or n <= 0:
        return None
    return sum(c[end - n:end]) / n


def ret(c, n: int) -> float | None:
    """Return over the last n bars."""
    if len(c) <= n or c[-1 - n] <= 0:
        return None
    return c[-1] / c[-1 - n] - 1


def rsi(c, n: int = 2) -> float | None:
    """Wilder RSI over n periods (Connors uses n=2). 100 = only up days, 0 = only down days."""
    if len(c) < n + 1:
        return None
    gains = losses = 0.0
    for a, b in zip(c[-n - 1:-1], c[-n:]):
        d = b - a
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    if losses == 0:
        return 100.0
    return 100 - 100 / (1 + gains / losses)


def volatility(c, n: int = 60) -> float | None:
    """Annualised standard deviation of daily log returns over the last n bars."""
    if len(c) < n + 1:
        return None
    r = [math.log(b / a) for a, b in zip(c[-n - 1:-1], c[-n:]) if a > 0 and b > 0]
    if len(r) < 2:
        return None
    mean = sum(r) / len(r)
    return math.sqrt(sum((x - mean) ** 2 for x in r) / (len(r) - 1)) * math.sqrt(250)


def series(b: Bars, live: float) -> list[float]:
    return [*b.close, live]


def features(b: Bars, live: float) -> dict:
    """The numbers the arena shows for a symbol (API /market/features, OpenPup debates)."""
    c = series(b, live)
    f = {"ma5": sma(c, 5), "ma20": sma(c, 20), "ma60": sma(c, 60), "ma200": sma(c, 200),
         "ret5": ret(c, 5), "ret20": ret(c, 20), "ret60": ret(c, 60), "rsi2": rsi(c, 2), "vol60": volatility(c, 60),
         "div_yield": (b.div_ttm / live) if live > 0 and b.div_ttm > 0 else 0.0,
         "limit_up_yesterday": bool(b.limit_up and b.limit_up[-1]), "bars": len(b.close)}
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in f.items()}
