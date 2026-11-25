"""In-process pub/sub feeding the SSE endpoint. One engine process per arena, so no broker is needed."""
from __future__ import annotations

import asyncio


class EventBus:
    def __init__(self, maxsize: int = 1000) -> None:
        self._subs: set[asyncio.Queue] = set()
        self._maxsize = maxsize

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(self._maxsize)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self, event: dict) -> None:
        for q in list(self._subs):
            if q.full():               # slow consumer: drop its oldest event rather than block the engine
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(event)

    @property
    def subscribers(self) -> int:
        return len(self._subs)
