"""Market data feeds. Every feed yields validated `L1Snapshot`s in per-symbol `seq` order."""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Iterable, Sequence
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..domain.errors import MarketDataError
from .snapshot import L1Snapshot, Level

log = logging.getLogger(__name__)


@runtime_checkable
class MarketDataFeed(Protocol):
    async def subscribe(self, symbols: Sequence[str]) -> None: ...
    def stream(self) -> AsyncIterator[L1Snapshot]: ...
    async def close(self) -> None: ...


def snapshot_from_dict(d: dict) -> L1Snapshot:
    """Canonical JSON shape (also the replay file format, one object per line):
    {"symbol":"600000.SH","ts":"2026-11-20T09:30:03+08:00","seq":1,
     "bids":[["10.00",1200],...],"asks":[["10.01",300],...],"last_price":"10.00",
     "cumulative_volume":123400,"upper_limit":"11.00","lower_limit":"9.00","prev_close":"10.00"}"""
    return L1Snapshot(
        symbol=d["symbol"],
        ts=datetime.fromisoformat(d["ts"]),
        seq=int(d["seq"]),
        bids=tuple(Level(Decimal(str(p)), int(v)) for p, v in d.get("bids", [])),
        asks=tuple(Level(Decimal(str(p)), int(v)) for p, v in d.get("asks", [])),
        last_price=Decimal(str(d["last_price"])),
        cumulative_volume=int(d["cumulative_volume"]),
        upper_limit=Decimal(str(d["upper_limit"])),
        lower_limit=Decimal(str(d["lower_limit"])),
        prev_close=Decimal(str(d["prev_close"])),
        suspended=bool(d.get("suspended", False)),
        extra={"name": d["name"]} if d.get("name") else {},
    )


def snapshot_to_dict(s: L1Snapshot) -> dict:
    """Inverse of `snapshot_from_dict`."""
    d = {"symbol": s.symbol, "ts": s.ts.isoformat(), "seq": s.seq,
         "bids": [[str(l.price), l.volume] for l in s.bids], "asks": [[str(l.price), l.volume] for l in s.asks],
         "last_price": str(s.last_price), "cumulative_volume": s.cumulative_volume,
         "upper_limit": str(s.upper_limit), "lower_limit": str(s.lower_limit), "prev_close": str(s.prev_close),
         "suspended": s.suspended}
    if s.extra.get("name"):
        d["name"] = s.extra["name"]
    return d


class ReplayFeed:
    """Replays recorded snapshots (JSONL files or in-memory) for local dev, demos and deterministic tests.
    `speed=0` replays as fast as possible; `speed=1.0` honours the original 3s spacing."""

    def __init__(self, snapshots: Iterable[L1Snapshot], speed: float = 0.0) -> None:
        self._snapshots = sorted(snapshots, key=lambda s: (s.ts, s.symbol, s.seq))
        self._speed = speed
        self._symbols: set[str] | None = None

    @classmethod
    def from_jsonl(cls, path: Path, speed: float = 0.0) -> "ReplayFeed":
        """Reads a gateway tape: plain `.jsonl` or `.jsonl.gz`. A gzip tape whose writer crashed lacks its
        trailer; everything up to the last flushed batch is still returned."""
        if path.suffix == ".gz":
            import zlib
            text = zlib.decompressobj(zlib.MAX_WBITS | 16).decompress(path.read_bytes())  # no trailer needed
            lines = text.decode("utf-8", errors="replace").splitlines()
        else:
            lines = path.read_text(encoding="utf-8").splitlines()
        snaps = []
        for line in lines:
            if line.strip():
                try:
                    snaps.append(snapshot_from_dict(json.loads(line)))
                except (ValueError, KeyError, MarketDataError):
                    continue   # a torn last line after a crash
        return cls(snaps, speed)

    async def subscribe(self, symbols: Sequence[str]) -> None:
        self._symbols = set(symbols)

    async def stream(self) -> AsyncIterator[L1Snapshot]:
        prev_ts: datetime | None = None
        for snap in self._snapshots:
            if self._symbols is not None and snap.symbol not in self._symbols:
                continue
            if self._speed > 0 and prev_ts is not None:
                await asyncio.sleep(max(0.0, (snap.ts - prev_ts).total_seconds() / self._speed))
            prev_ts = snap.ts
            yield snap

    async def close(self) -> None:
        return None


class RedisStreamFeed:
    """Consumes snapshots published by `services/market-gateway` (rustdx-complete / TDX) on a Redis Stream.

    Each entry carries one canonical snapshot JSON in field `data`. `last_id` is the resume point: persist it
    and pass it back as `start_id` after a restart to continue without gaps ("$" = only new entries).
    Malformed entries are counted in `rejected` and skipped, never fatal.
    """

    def __init__(self, client, stream: str = "md:l1", start_id: str = "$", block_ms: int = 5000,
                 batch: int = 500) -> None:
        self._client = client                 # redis.asyncio.Redis (or anything with a compatible xread)
        self._stream = stream
        self.last_id = start_id
        self._block_ms = block_ms
        self._batch = batch
        self._symbols: set[str] | None = None
        self._closed = False
        self.rejected = 0

    async def subscribe(self, symbols: Sequence[str]) -> None:
        self._symbols = set(symbols)

    async def stream(self) -> AsyncIterator[L1Snapshot]:
        while not self._closed:
            resp = await self._client.xread({self._stream: self.last_id}, count=self._batch, block=self._block_ms)
            for _stream, entries in resp or []:
                for entry_id, fields in entries:
                    self.last_id = entry_id.decode() if isinstance(entry_id, bytes) else entry_id
                    raw = fields.get(b"data", fields.get("data"))
                    try:
                        snap = snapshot_from_dict(json.loads(raw))
                    except (MarketDataError, KeyError, TypeError, ValueError) as exc:
                        self.rejected += 1
                        log.warning("dropping stream entry %s: %s", self.last_id, exc)
                        continue
                    if self._symbols is not None and snap.symbol not in self._symbols:
                        continue
                    yield snap

    async def close(self) -> None:
        self._closed = True
