from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class ArenaEvent:
    """Domain event emitted by the engine. The API layer persists these to `arena_events` / `order_events`
    and fans them out over Redis pub/sub -> SSE. Payload values are JSON-safe (Decimals as strings)."""

    type: str
    ts: datetime
    payload: dict[str, Any] = field(default_factory=dict)


# Event type names (stable public contract for SSE consumers).
MARKET_OPEN = "market.open"
MARKET_CLOSE = "market.close"
ORDER_ACCEPTED = "order.accepted"
ORDER_PARTIALLY_FILLED = "order.partially_filled"
ORDER_FILLED = "order.filled"
ORDER_CANCELED = "order.canceled"
ORDER_EXPIRED = "order.expired"
LIMIT_QUEUE_UPDATE = "limit_up.queue"       # per-order progress bar tick (also used for limit-down sells)
LIMIT_UP_SEALED = "limit_up.sealed"
LIMIT_UP_BROKEN = "limit_up.broken"
LIMIT_DOWN_SEALED = "limit_down.sealed"
LIMIT_DOWN_BROKEN = "limit_down.broken"
CONFLICT_DETECTED = "conflict.detected"
