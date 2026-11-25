"""Season calendar: four seasons a year, about three months each, plus a monthly board inside every season.

    winter  Dec-Feb   s{n}-{year}w    (year = the December's year)
    spring  Mar-May   s{n}-{year}sp
    summer  Jun-Aug   s{n}-{year}su
    autumn  Sep-Nov   s{n}-{year}au

Season 1 is the 2026 winter season; it started early, on launch day (2026-11-23), so every date before
2026-12-01 belongs to it too. A season ends after its last calendar day; the first trading day past it settles
the season (final ranks, badges) and starts the next one with fresh cash.
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date

FIRST_YEAR, FIRST_MONTH = 2026, 12           # Season 1 = 2026 winter
S1_START = date(2026, 11, 23)
QUARTERS = ((12, "w", "冬季赛"), (3, "sp", "春季赛"), (6, "su", "夏季赛"), (9, "au", "秋季赛"))
AUTO_SLUG = re.compile(r"^s\d+-\d{4}(w|sp|su|au)$")


@dataclass(frozen=True)
class SeasonSpec:
    number: int
    slug: str
    name: str
    start: date
    end: date


def season_for(d: date) -> SeasonSpec:
    if d < date(FIRST_YEAR, FIRST_MONTH, 1):
        d = date(FIRST_YEAR, FIRST_MONTH, 1)
    # first month of the quarter containing d (Dec, Mar, Jun, Sep)
    start_month = ((d.month % 12) // 3) * 3 or 12
    start_year = d.year - 1 if start_month == 12 and d.month < 12 else d.year
    code, title = next((c, t) for m, c, t in QUARTERS if m == start_month)
    number = ((start_year * 12 + start_month) - (FIRST_YEAR * 12 + FIRST_MONTH)) // 3 + 1
    end_month = (start_month + 2 - 1) % 12 + 1
    end_year = start_year + (1 if end_month < start_month else 0)
    end = date(end_year, end_month, calendar.monthrange(end_year, end_month)[1])
    start = S1_START if number == 1 else date(start_year, start_month, 1)
    return SeasonSpec(number, f"s{number}-{start_year}{code}", f"Season {number} · {start_year} {title}", start, end)


def is_auto(slug: str) -> bool:
    return bool(AUTO_SLUG.match(slug))


def month_key(d: date) -> str:
    return f"{d.year}-{d.month:02d}"
