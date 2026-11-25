from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

from ..domain.enums import Side

CENT = Decimal("0.01")
ZERO = Decimal("0")


def money(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    """A-share retail fee schedule. Commission is charged per ORDER on its cumulative filled notional
    (brokers aggregate same-day fills of one order), so the 5 CNY minimum is paid once per order, not per fill.
    Transfer fee (过户费, 0.001% both sides since 2022) defaults to 0 to match the V1 spec; set 0.00001 for realism."""

    commission_rate: Decimal = Decimal("0.00025")
    commission_min: Decimal = Decimal("5.00")
    stamp_duty_rate: Decimal = Decimal("0.0005")     # sell side only (since 2023-08-28)
    transfer_fee_rate: Decimal = Decimal("0")

    def commission_due(self, cumulative_notional: Decimal) -> Decimal:
        """Total commission owed by an order once `cumulative_notional` has filled."""
        if cumulative_notional <= 0:
            return ZERO
        return max(self.commission_min, money(cumulative_notional * self.commission_rate))

    def stamp_duty(self, side: Side, notional: Decimal) -> Decimal:
        return money(notional * self.stamp_duty_rate) if side is Side.SELL else ZERO

    def transfer_fee(self, notional: Decimal) -> Decimal:
        return money(notional * self.transfer_fee_rate)

    def transfer_fee_upper_bound(self, notional: Decimal, n_fills_upper_bound: int = 1) -> Decimal:
        """Rounding-safe bound used for cash reservation (per-fill rounding can add up to 0.005 each)."""
        if self.transfer_fee_rate == 0:
            return ZERO
        return (notional * self.transfer_fee_rate).quantize(CENT, rounding=ROUND_CEILING) + CENT * n_fills_upper_bound
