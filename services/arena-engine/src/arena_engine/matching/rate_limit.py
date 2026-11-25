from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta


class SlidingWindowLimiter:
    """Deterministic sliding-window limiter driven by the caller's clock (market time), not wall time,
    so replays reproduce identical accept/reject decisions. The API layer mirrors this in Redis when the
    engine is sharded across processes."""

    def __init__(self, max_actions: int, window: timedelta) -> None:
        if max_actions < 1:
            raise ValueError("max_actions must be >= 1")
        self._max = max_actions
        self._window = window
        self._hits: dict[tuple, deque[datetime]] = {}

    def _prune(self, key: tuple, now: datetime) -> deque[datetime]:
        q = self._hits.setdefault(key, deque())
        while q and now - q[0] >= self._window:
            q.popleft()
        return q

    def allowed(self, key: tuple, now: datetime) -> bool:
        return len(self._prune(key, now)) < self._max

    def record(self, key: tuple, now: datetime) -> None:
        self._prune(key, now).append(now)

    def reset(self) -> None:
        self._hits.clear()
