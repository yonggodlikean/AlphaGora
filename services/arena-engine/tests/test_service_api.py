"""End-to-end: real PostgreSQL (pgserver), the HTTP API, house bots and a full synthetic trading day."""
from __future__ import annotations

import tempfile
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

pgserver = pytest.importorskip("pgserver")
httpx = pytest.importorskip("httpx")

from arena_engine.api.main import create_app  # noqa: E402
from arena_engine.config import Settings  # noqa: E402
from arena_engine.db import create_pool, migrate  # noqa: E402
from arena_engine.market.demo import DemoMarket  # noqa: E402
from arena_engine.service import ArenaService  # noqa: E402

DAY1 = date(2026, 11, 26)   # Thursday
DAY2 = date(2026, 11, 27)   # Friday
MIGRATIONS = Path(__file__).resolve().parents[3] / "db" / "migrations"


@pytest.fixture(scope="session")
def pg():
    srv = pgserver.get_server(tempfile.mkdtemp(prefix="agora-pg-"), cleanup_mode="stop")
    yield srv


@pytest.fixture
def dsn(pg):
    name = "t_" + uuid.uuid4().hex[:10]
    pg.psql(f"CREATE DATABASE {name};")
    return pg.get_uri(name)


@pytest.fixture
def anyio_backend():
    return "asyncio"


def settings(dsn: str) -> Settings:
    return Settings(database_url=dsn, market_feed="manual", season_slug="test", benchmark_symbol="999300.SH",
                    dividend_symbols=("999002.SH", "999003.SH"), migrations_dir=MIGRATIONS, auto_start_feed=False)


async def boot(dsn: str) -> tuple[ArenaService, httpx.AsyncClient, object]:
    st = settings(dsn)
    pool = await create_pool(dsn)
    await migrate(pool, MIGRATIONS)
    service = ArenaService(st, pool)
    await service.start()
    app = create_app(st)
    app.state.service = service
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://arena")
    return service, client, pool


async def play(service: ArenaService, groups, start: int = 0, stop: int | None = None) -> None:
    for group in groups[start:stop]:
        for snap in group:
            await service.on_snapshot(snap)
        await service.maybe_run_bots()


@pytest.mark.anyio
async def test_full_day_join_trade_close_and_restart(dsn):
    service, client, pool = await boot(dsn)
    market = DemoMarket(seed=11)
    day1 = market.day(DAY1)

    # --- join as a human
    r = await client.post("/api/v1/join", json={"display_name": "散户老王", "kind": "human"})
    assert r.status_code == 201, r.text
    token = r.json()["token"]
    pid = r.json()["portfolio_id"]
    auth = {"Authorization": f"Bearer {token}"}
    assert (await client.post("/api/v1/join", json={"display_name": "散户老王"})).status_code == 409
    assert (await client.get("/api/v1/agent/state")).status_code == 401

    # --- morning session up to ~10:00
    await play(service, day1, 0, 130)
    syms = (await client.get("/api/v1/market/symbols")).json()["symbols"]
    assert {s["symbol"] for s in syms} >= {"999001.SH", "999300.SH"} and syms[0]["name"].startswith("演示")
    bank = next(s for s in syms if s["symbol"] == "999002.SH")
    snap = (await client.get("/api/v1/market/snapshot", params={"symbols": "999002.SH"})).json()["snapshots"][0]
    ask1 = snap["asks"][0][0]

    order = {"idempotency_key": "human-0001", "symbol": "999002.SH", "side": "buy", "order_type": "limit",
             "price": ask1, "qty": 1000, "thesis": "银行股低估，先买一点"}
    r = await client.post("/api/v1/agent/orders", json=order, headers=auth)
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "filled" and r.json()["portfolio_name"] == "散户老王"
    again = await client.post("/api/v1/agent/orders", json=order, headers=auth)
    assert again.status_code == 201 and again.json()["duplicate"] is True
    clash = await client.post("/api/v1/agent/orders", json=order | {"qty": 2000}, headers=auth)
    assert clash.status_code == 409 and clash.json()["code"] == "IDEMPOTENCY_CONFLICT"
    t1 = await client.post("/api/v1/agent/orders", headers=auth, json={
        "idempotency_key": "human-0002", "symbol": "999002.SH", "side": "sell", "price": bank["last"],
        "qty": 1000, "thesis": "想马上卖掉试试"})
    assert t1.status_code == 422 and t1.json()["code"] == "T1_LOCKED"
    nothesis = await client.post("/api/v1/agent/orders", headers=auth, json={
        "idempotency_key": "human-0003", "symbol": "999002.SH", "side": "buy", "price": ask1, "qty": 100})
    assert nothesis.status_code == 422 and nothesis.json()["code"] == "THESIS_REQUIRED"

    state = (await client.get("/api/v1/agent/state", headers=auth)).json()
    assert state["positions"][0]["frozen_qty"] == 1000 and state["rank"] >= 1 and "beaten_ai" in state

    # --- an OpenPup-style agent posts a debate then orders with it
    r = await client.post("/api/v1/join", json={"display_name": "社区 OpenPup 队", "kind": "openpup_ai"})
    bot_auth = {"Authorization": f"Bearer {r.json()['token']}"}
    d = await client.post("/api/v1/agent/debates", headers=bot_auth, json={
        "symbol": "999003.SH", "final_decision": {"action": "buy", "summary": "2:1 买入"},
        "rounds": [{"role": "bull", "stance": "buy", "argument": "趋势向上"},
                   {"role": "bear", "stance": "sell", "argument": "估值偏高"}]})
    assert d.status_code == 201 and d.json()["has_conflict"] is True
    snap3 = (await client.get("/api/v1/market/snapshot", params={"symbols": "999003.SH"})).json()["snapshots"][0]
    r = await client.post("/api/v1/agent/orders", headers=bot_auth, json={
        "idempotency_key": "pup-000001", "symbol": "999003.SH", "side": "buy", "price": snap3["asks"][0][0],
        "qty": 500, "debate_id": d.json()["debate_id"]})
    assert r.status_code == 201, r.text
    stolen = await client.post("/api/v1/agent/orders", headers=auth, json={
        "idempotency_key": "human-0004", "symbol": "999003.SH", "side": "buy", "price": snap3["asks"][0][0],
        "qty": 100, "debate_id": d.json()["debate_id"]})
    assert stolen.status_code == 422     # someone else's debate

    # --- rest of the day, then close
    await play(service, day1, 130)
    await service.close_day()

    board = (await client.get("/api/v1/leaderboard")).json()["rows"]
    assert len(board) == 8 and [r["rank"] for r in board if r["rank"]] == list(range(1, 8))
    bench = next(r for r in board if r["group"] == "benchmark")
    assert bench["rank"] is None and abs(bench["cum_return"]) < 0.2
    feed = (await client.get("/api/v1/arena/feed")).json()
    assert feed["roast"] and feed["roast"]["text"] and pid in feed["roast"]["lines"]

    async with pool.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM daily_snapshots") == 8
        house_debates = await conn.fetchval(
            "SELECT count(*) FROM debates d JOIN portfolios p ON p.id = d.portfolio_id WHERE p.is_official")
        house_fills = await conn.fetchval(
            "SELECT count(*) FROM trades t JOIN portfolios p ON p.id = t.portfolio_id WHERE p.is_official")
        bench_qty = await conn.fetchval(
            "SELECT qty FROM positions WHERE symbol = '999300.SH' AND portfolio_id = $1",
            uuid.UUID(bench["portfolio_id"]))
        unbalanced = await conn.fetchval(
            "SELECT count(*) FROM (SELECT journal_id FROM ledger_entries GROUP BY journal_id HAVING sum(amount) <> 0) x")
        badges = await conn.fetchval("SELECT count(*) FROM portfolio_badges WHERE badge_code = 'first_trade'")
    assert house_debates > 0 and house_fills > 0
    assert bench_qty and bench_qty > 200_000       # ~all cash in the benchmark ETF
    assert unbalanced == 0 and badges >= 2

    dog = next(r for r in board if r["name"] == "激进打板动量狗")
    tl = (await client.get(f"/api/v1/portfolios/{dog['portfolio_id']}/timeline")).json()["items"]
    assert tl and tl[0]["kind"] == "debate" and tl[0]["rounds"][0]["model_id"] == "rules-v1"
    prof = (await client.get(f"/api/v1/portfolios/{pid}")).json()
    assert prof["positions"][0]["frozen_qty"] == 1000 and any(b["count"] for b in prof["badges"])

    cash_before = service.engine.account(pid).cash
    await client.aclose()
    await service.stop()
    await pool.close()

    # --- restart on the same database: state survives, T+1 settles on the next day
    service2, client2, pool2 = await boot(dsn)
    assert service2.engine.account(pid).cash == cash_before
    r = await client2.post("/api/v1/agent/orders", json=order, headers=auth)
    assert r.json()["duplicate"] is True             # idempotency survives restarts (DB-backed)
    day2 = market.day(DAY2)
    await play(service2, day2, 0, 30)
    snap = (await client2.get("/api/v1/market/snapshot", params={"symbols": "999002.SH"})).json()["snapshots"][0]
    sell = await client2.post("/api/v1/agent/orders", headers=auth, json={
        "idempotency_key": "human-0005", "symbol": "999002.SH", "side": "sell", "price": snap["bids"][0][0],
        "qty": 1000, "thesis": "T+1 到了，落袋"})
    assert sell.status_code == 201 and sell.json()["status"] == "filled", sell.text
    assert service2.engine.account(pid).positions["999002.SH"].qty == 0
    await client2.aclose()
    await service2.stop()
    await pool2.close()


@pytest.mark.anyio
async def test_sse_replays_persisted_events(dsn):
    service, client, pool = await boot(dsn)
    await play(service, DemoMarket(seed=3).day(DAY1), 0, 40)
    events = await service.store.events_after(service.season_id, 0)
    assert events and events[0]["type"] == "market.open"
    q = service.bus.subscribe()
    await play(service, DemoMarket(seed=3).day(DAY1)[40:200])
    assert not q.empty()
    service.bus.unsubscribe(q)
    await client.aclose()
    await service.stop()
    await pool.close()


@pytest.mark.anyio
async def test_pre_shared_team_token_hands_team_to_external_driver(dsn):
    import dataclasses

    token = "agk_test_preshared_token_0001"
    st = dataclasses.replace(settings(dsn), team_tokens=(("macro_debate", token),))
    pool = await create_pool(dsn)
    await migrate(pool, MIGRATIONS)
    service = ArenaService(st, pool)
    await service.start()
    app = create_app(st)
    app.state.service = service
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://arena")

    def row(rows, name):
        return next(r for r in rows if r["name"] == name)

    board = (await client.get("/api/v1/leaderboard")).json()["rows"]
    assert row(board, "OpenPup 宏观辩论团")["driver"] == {"kind": "external", "name": None, "online": False, "last_seen": None}
    assert row(board, "激进打板动量狗")["driver"]["kind"] == "rules"

    hb = await client.post("/api/v1/agent/heartbeat", headers={"Authorization": f"Bearer {token}"},
                           json={"driver": "openpup 1.3.0"})
    assert hb.status_code == 200 and hb.json()["driver"]["online"] is True
    board = (await client.get("/api/v1/leaderboard")).json()["rows"]
    macro = row(board, "OpenPup 宏观辩论团")
    assert macro["driver"]["online"] is True
    prof = (await client.get(f"/api/v1/portfolios/{macro['portfolio_id']}")).json()
    assert prof["driver"]["kind"] == "external" and prof["driver"]["name"] == "openpup 1.3.0"

    await play(service, DemoMarket(seed=11).day(DAY1), 0, 400)
    async with pool.acquire() as conn:
        macro_debates = await conn.fetchval("SELECT count(*) FROM debates WHERE portfolio_id = $1",
                                            uuid.UUID(macro["portfolio_id"]))
        other_debates = await conn.fetchval("SELECT count(*) FROM debates")
        seen = await conn.fetchval("SELECT last_seen_at FROM portfolios WHERE id = $1", uuid.UUID(macro["portfolio_id"]))
    assert macro_debates == 0 and other_debates > 0      # the rule bot no longer trades the external team
    assert seen is not None

    await client.aclose()
    await service.stop()
    await pool.close()

    # restart: the token is already registered, not duplicated
    service2, client2, pool2 = await boot(dsn)
    async with pool2.acquire() as conn:
        assert await conn.fetchval("SELECT count(*) FROM api_tokens") == 1
    await client2.aclose()
    await service2.stop()
    await pool2.close()
