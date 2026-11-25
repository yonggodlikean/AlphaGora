"""Turns engine facts into balanced double-entry journals (`ledger_journals` + `ledger_entries`).
Sign convention: debit positive; every journal sums to zero (also enforced by a deferred DB trigger)."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..domain.enums import Side
from ..domain.models import ZERO, Fill


@dataclass(frozen=True, slots=True)
class Posting:
    account: str            # matches the ledger_account enum
    amount: Decimal
    symbol: str | None = None
    qty_delta: int = 0


@dataclass(frozen=True, slots=True)
class Journal:
    entry_type: str         # matches the ledger_entry_type enum
    ref_type: str
    ref_id: str
    portfolio_id: str
    postings: tuple[Posting, ...]

    def __post_init__(self) -> None:
        total = sum((p.amount for p in self.postings), ZERO)
        if total != 0:
            raise ValueError(f"unbalanced journal {self.ref_type}:{self.ref_id} ({total})")


def funding_journal(portfolio_id: str, season_id: str, amount: Decimal) -> Journal:
    return Journal("season_funding", "season", season_id, portfolio_id, (
        Posting("cash", amount),
        Posting("capital", -amount),
    ))


def fill_journal(fill: Fill) -> Journal:
    fee_postings = tuple(
        Posting(account, amt)
        for account, amt in (("fee_commission", fill.commission), ("fee_stamp_duty", fill.stamp_duty),
                             ("fee_transfer", fill.transfer_fee))
        if amt != 0
    )
    if fill.side is Side.BUY:
        postings = (
            Posting("position", fill.notional, fill.symbol, fill.qty),
            Posting("cash", fill.cash_delta),
            *fee_postings,
        )
        return Journal("buy_fill", "trade", fill.id, fill.portfolio_id, postings)
    postings = (
        Posting("cash", fill.cash_delta),
        Posting("position", -fill.cost_released, fill.symbol, -fill.qty),
        *fee_postings,
        Posting("realized_pnl", -(fill.notional - fill.cost_released)),
    )
    return Journal("sell_fill", "trade", fill.id, fill.portfolio_id, postings)
