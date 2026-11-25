"""Corporate actions (除权除息) on held positions.

On the ex-date, before any trading, every position held at the previous close receives:
  * the cash dividend: shares × (cash per 10 shares) / 10, rounded down to the fen;
  * bonus and transfer shares (送股 / 转增): floor(shares × (shares per 10) / 10), locked until the next day
    like any new shares (红股上市日 is the following trading day); the total cost basis stays, so the per-share
    cost falls.
Simplifications, stated in docs/API.md: no dividend tax (in reality charged on sale by holding time), rights
issues (配股) are not modelled, fractional bonus shares are dropped rather than paid in cash.

Each action has a deterministic id, so a restart or a reopened day never pays twice.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_DOWN, Decimal

from .domain.models import Account
from .market.history import History

NS = uuid.UUID("6f1f3c8e-6a3e-4a54-9a51-0c2b3d1f7a11")


@dataclass(frozen=True)
class Action:
    portfolio_id: str
    symbol: str
    ex_date: date
    held: int                   # shares held at the previous close
    cash_per_10: Decimal
    shares_per_10: Decimal
    cash: Decimal               # paid
    bonus: int                  # shares added

    @property
    def id(self) -> uuid.UUID:
        return uuid.uuid5(NS, f"{self.portfolio_id}:{self.symbol}:{self.ex_date.isoformat()}")

    def describe(self) -> str:
        parts = []
        if self.cash_per_10 > 0:
            parts.append(f"10 派 {self.cash_per_10.normalize():f}")
        if self.shares_per_10 > 0:
            parts.append(f"10 送转 {self.shares_per_10.normalize():f}")
        return "，".join(parts)


def plan(accounts: dict[str, Account], history: History | None, day: date) -> list[Action]:
    if history is None:
        return []
    key = day.year * 10_000 + day.month * 100 + day.day
    out = []
    for pid, acct in accounts.items():
        for sym, pos in sorted(acct.positions.items()):
            if pos.qty <= 0:
                continue
            bars = history.bars.get(sym)
            for ex, cash10, shares10 in (bars.events if bars else ()):
                if ex != key:
                    continue
                c10, s10 = Decimal(str(cash10)), Decimal(str(shares10))
                cash = (Decimal(pos.qty) * c10 / 10).quantize(Decimal("0.01"), rounding=ROUND_DOWN)
                bonus = int(Decimal(pos.qty) * s10 / 10)
                if cash > 0 or bonus > 0:
                    out.append(Action(pid, sym, day, pos.qty, c10, s10, cash, bonus))
    return out


def apply_to_engine(accounts: dict[str, Account], a: Action) -> None:
    acct = accounts[a.portfolio_id]
    acct.cash += a.cash
    acct.realized_pnl += a.cash
    if a.bonus:
        pos = acct.positions[a.symbol]
        pos.qty += a.bonus
        pos.frozen_qty += a.bonus
