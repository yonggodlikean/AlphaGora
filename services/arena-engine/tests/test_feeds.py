import asyncio
import json
from pathlib import Path

from arena_engine.market.feed import RedisStreamFeed, ReplayFeed

# A real snapshot as produced by market-gateway from a live TDX quote (600000.SH, 2026-11-20 close).
LIVE = {"symbol": "600000.SH", "ts": "2026-11-20T14:59:57.010+08:00", "seq": 53997010,
        "bids": [["10.86", 49300], ["10.85", 40200], ["10.83", 2500], ["10.81", 1700], ["10.80", 23900]],
        "asks": [["10.87", 143900], ["10.88", 597800], ["10.89", 341700], ["10.90", 836500], ["10.91", 139700]],
        "last_price": "10.87", "cumulative_volume": 100866700, "upper_limit": "11.77", "lower_limit": "9.63",
        "prev_close": "10.70", "suspended": False}


class FakeRedis:
    def __init__(self, entries):
        self.entries = entries
        self.calls = []

    async def xread(self, streams, count, block):
        self.calls.append(dict(streams))
        out, self.entries = self.entries, []
        if not out:
            await asyncio.sleep(0)
            return []
        return [(b"md:l1", out)]


def collect(feed, n):
    async def run():
        got = []
        async for s in feed.stream():
            got.append(s)
            if len(got) == n:
                await feed.close()
                break
        return got
    return asyncio.run(run())


def test_redis_stream_feed_parses_skips_bad_entries_and_tracks_resume_id():
    other = dict(LIVE, symbol="000001.SZ", seq=1)
    crossed = dict(LIVE, seq=2, asks=[["10.85", 100]])
    fake = FakeRedis([
        (b"1-0", {b"data": json.dumps(LIVE).encode()}),
        (b"2-0", {b"data": b"not json"}),
        (b"3-0", {b"data": json.dumps(crossed).encode()}),
        (b"4-0", {b"data": json.dumps(other).encode()}),
    ])
    feed = RedisStreamFeed(fake, start_id="0-0")
    asyncio.run(feed.subscribe(["600000.SH", "000001.SZ"]))
    snaps = collect(feed, 2)
    assert [s.symbol for s in snaps] == ["600000.SH", "000001.SZ"]
    assert snaps[0].upper_limit == snaps[0].prev_close * 11 / 10 and snaps[0].bid1.volume == 49300
    assert feed.rejected == 2 and feed.last_id == "4-0"
    assert fake.calls[0] == {"md:l1": "0-0"}


def test_replay_feed_reads_gateway_tape(tmp_path: Path):
    tape = tmp_path / "2026-11-20.jsonl"
    tape.write_text(json.dumps(LIVE) + "\n" + json.dumps(dict(LIVE, seq=LIVE["seq"] + 3000)) + "\n")
    snaps = collect(ReplayFeed.from_jsonl(tape), 2)
    assert [s.seq for s in snaps] == [53997010, 54000010]


def test_replay_feed_reads_gzip_tape_even_without_trailer(tmp_path: Path):
    import zlib
    body = "\n".join(json.dumps(dict(LIVE, seq=LIVE["seq"] + i * 3000)) for i in range(3)) + "\n"
    comp = zlib.compressobj(6, zlib.DEFLATED, zlib.MAX_WBITS | 16)
    data = comp.compress(body.encode()) + comp.flush(zlib.Z_SYNC_FLUSH)   # what a crashed writer leaves behind
    tape = tmp_path / "2026-11-26.jsonl.gz"
    tape.write_bytes(data)
    assert [s.seq for s in collect(ReplayFeed.from_jsonl(tape), 3)] == [53997010, 54000010, 54003010]
