"""Invite-only sign-up, seat caps, moderation, account status (anti-cheat), cheers and rate limits."""
from __future__ import annotations

import uuid

import pytest

from test_service_api import DAY1, boot, play  # noqa: F401  (fixtures pg/dsn/anyio_backend come from there)
from test_service_api import anyio_backend, dsn, pg  # noqa: F401
from arena_engine.market.demo import DemoMarket
from arena_engine.moderation import ContentFilter


def test_content_filter():
    f = ContentFilter()
    assert f.check("回踩20日线，量能萎缩") is None
    assert f.check("加我微 信 看牛股") is not None            # spaced-out evasion still caught
    assert f.check("想要老师带单的私聊") is not None
    assert f.check("详情 www.example.com") is not None
    assert f.check("电话13812345678") is not None
    assert f.check("USB接口概念股") is None                    # no false positive on short ascii
    assert f.check("你个傻逼") is not None


@pytest.mark.anyio
async def test_invites_and_seat_caps(dsn):
    service, client, pool = await boot(dsn, require_invite=True, seats_human=2, seats_ai=1)
    store = service.store
    human = await store.create_invites(service.season_id, "human", 1, 2, "group")
    ai = await store.create_invites(service.season_id, "ai", 1, 1, None)
    join = lambda name, kind="human", code=None: client.post(
        "/api/v1/join", json={"display_name": name, "kind": kind, "invite_code": code})

    assert (await join("无码")).json()["code"] == "INVITE_REQUIRED"
    assert (await join("加微信领牛股", code=human[0])).json()["code"] == "CONTENT_BLOCKED"   # checked before the invite
    assert (await join("乱码", code="AG-NOPE0000")).json()["code"] == "INVITE_INVALID"
    assert (await join("错类", code=ai[0])).json()["code"] == "INVITE_WRONG_KIND"
    assert (await join("甲", code=human[0].lower())).status_code == 201      # codes are case-insensitive
    assert (await join("甲", code=human[0])).json()["code"] == "NAME_TAKEN"   # and that did not burn a use
    assert (await join("乙", code=human[0])).status_code == 201
    assert (await join("丙", code=human[0])).json()["code"] == "INVITE_USED_UP"
    more = await store.create_invites(service.season_id, "human", 1, 5, None)
    assert (await join("丁", code=more[0])).json()["code"] == "SEATS_FULL"   # 2 human seats taken
    assert (await join("机器人", "external_bot", ai[0])).status_code == 201
    seats = (await client.get("/api/v1/join/seats")).json()
    assert seats["human"] == {"taken": 2, "total": 2} and seats["ai"] == {"taken": 1, "total": 1}
    # every attempt counts against the per-IP sign-up limit (10/hour): this also stops invite-code guessing
    assert (await join("戊", code=more[0])).json()["code"] == "RATE_LIMITED"
    await client.aclose(); await service.stop(); await pool.close()


@pytest.mark.anyio
async def test_suspend_ban_hide_and_rate_limits(dsn):
    service, client, pool = await boot(dsn, orders_per_min_human=3)
    day = DemoMarket(seed=11).day(DAY1)
    await play(service, day, 0, 120)
    r = await client.post("/api/v1/join", json={"display_name": "嫌疑人", "kind": "human"})
    pid, auth = r.json()["portfolio_id"], {"Authorization": f"Bearer {r.json()['token']}"}
    snap = (await client.get("/api/v1/market/snapshot", params={"symbols": "999002.SH"})).json()["snapshots"][0]
    low = f"{float(snap['bids'][0][0]) - 0.05:.2f}"
    order = lambda k, thesis="低吸一点试试": client.post("/api/v1/agent/orders", headers=auth, json={
        "idempotency_key": f"cheat-{k:06d}", "symbol": "999002.SH", "side": "buy", "price": low, "qty": 100,
        "thesis": thesis})

    blocked = await order(0, "进群看我带单")
    assert blocked.status_code == 422 and blocked.json()["code"] == "CONTENT_BLOCKED"
    first = await order(1)
    assert first.status_code == 201, first.text
    oids = [first.json()["order_id"]] + [(await order(i)).json()["order_id"] for i in (2, 3)]
    limited = await order(4)
    assert limited.status_code == 429 and limited.json()["code"] == "RATE_LIMITED"

    # hide one rationale
    await service.store.hide("order", uuid.UUID(oids[0]), "spam")
    await service.refresh_controls()
    tl = (await client.get(f"/api/v1/portfolios/{pid}/timeline")).json()["items"]
    assert any(o["thesis"] == "（该内容已被管理员隐藏）" for it in tl for o in it["orders"])

    # suspend: open orders canceled, off the board, cannot trade, can still read state
    assert any(r["portfolio_id"] == pid for r in (await client.get("/api/v1/leaderboard")).json()["rows"])
    await service.store.set_status(uuid.UUID(pid), "suspended", "刷单")
    await service.refresh_controls()
    assert all(not service.engine.order(o).status.is_open for o in oids)
    assert service.engine.account(pid).reserved_cash == 0
    assert not any(r["portfolio_id"] == pid for r in (await client.get("/api/v1/leaderboard")).json()["rows"])
    assert (await client.get("/api/v1/agent/state", headers=auth)).status_code == 200
    denied = await order(9)
    assert denied.status_code == 403 and denied.json()["code"] == "ACCOUNT_SUSPENDED"
    prof = (await client.get(f"/api/v1/portfolios/{pid}")).json()
    assert prof["status"] == "suspended" and prof["rank"] is None

    # reset-token: old token dies, new one works, same account
    raw = "agk_" + "n" * 32
    assert await service.store.reset_token(uuid.UUID(pid), raw[:8], service.hash_token(raw)) == 1
    await service.refresh_controls()
    assert (await client.get("/api/v1/agent/state", headers=auth)).status_code == 401
    auth = {"Authorization": f"Bearer {raw}"}
    st = await client.get("/api/v1/agent/state", headers=auth)
    assert st.status_code == 200 and st.json()["portfolio_id"] == pid

    # ban: token dead
    await service.store.set_status(uuid.UUID(pid), "banned", "多账号")
    await service.refresh_controls()
    assert (await client.get("/api/v1/agent/state", headers=auth)).status_code == 401
    await client.aclose(); await service.stop(); await pool.close()


@pytest.mark.anyio
async def test_cheers_and_visitors(dsn):
    service, client, pool = await boot(dsn)
    board = (await client.get("/api/v1/leaderboard")).json()["rows"]
    dog = next(r for r in board if r["name"] == "激进打板动量狗")["portfolio_id"]
    v1, v2 = {"X-Visitor": "browser-one"}, {"X-Visitor": "browser-two"}
    a = (await client.post(f"/api/v1/portfolios/{dog}/cheer", headers=v1)).json()
    b = (await client.post(f"/api/v1/portfolios/{dog}/cheer", headers=v1)).json()   # same visitor, same day
    c = (await client.post(f"/api/v1/portfolios/{dog}/cheer", headers=v2)).json()
    assert (a["new"], b["new"], c["new"]) == (True, False, True) and c["cheers"] == 2
    assert next(r for r in (await client.get("/api/v1/leaderboard")).json()["rows"]
                if r["portfolio_id"] == dog)["cheers"] == 2
    for h in (v1, v2, v1):
        await client.get("/api/v1/market/status", headers=h)
    st = (await client.get("/api/v1/market/status", headers=v2)).json()
    assert st["visitors_today"] == 2 and st["online"] == 2 and "phase" in st
    for k in service._watching:                       # both browsers went quiet a while ago
        service._watching[k] -= 120
    assert service.watching() == 0
    await service.refresh_controls()
    today, total = await service.store.visits(service._visitors_day)
    assert (today, total) == (2, 2)
    await client.aclose(); await service.stop(); await pool.close()


def test_calendar_phase():
    from datetime import date, datetime, time
    from arena_engine.market.calendar import CST, TradingCalendar
    cal = TradingCalendar(holidays=frozenset({date(2026, 10, 1)}))
    at = lambda d, h, m: datetime.combine(d, time(h, m), CST)
    wed, sat = date(2026, 11, 25), date(2026, 11, 28)
    assert [cal.phase(at(wed, h, m)) for h, m in ((8, 0), (9, 20), (9, 30), (11, 30), (12, 59), (13, 0), (15, 0))] == \
        ["pre_open", "auction", "trading", "lunch", "lunch", "trading", "closed"]
    assert cal.phase(at(sat, 10, 0)) == "holiday" and cal.phase(at(date(2026, 10, 1), 10, 0)) == "holiday"


@pytest.mark.anyio
async def test_timeline_orders_and_conflicts_page_by_trading_day(dsn):
    from test_service_api import DAY2
    service, client, pool = await boot(dsn)
    market = DemoMarket(seed=11)
    for d in (DAY1, DAY2):
        await play(service, market.day(d))
        await service.close_day()
    board = (await client.get("/api/v1/leaderboard")).json()["rows"]
    dividend = next(r for r in board if r["name"] == "激进打板动量狗")["portfolio_id"]   # trades both days
    first = (await client.get(f"/api/v1/portfolios/{dividend}/timeline", params={"days": 1})).json()
    assert len(first["days"]) == 1 and first["days"][0]["date"] == DAY2.isoformat() and first["total"] >= 1
    assert first["next_before"] == DAY2.isoformat()
    older = (await client.get(f"/api/v1/portfolios/{dividend}/timeline",
                              params={"days": 1, "before": first["next_before"]})).json()
    assert older["days"][0]["date"] == DAY1.isoformat() and older["next_before"] is None
    assert all(it["day"] == DAY1.isoformat() for it in older["days"][0]["items"])
    filled = (await client.get(f"/api/v1/portfolios/{dividend}/timeline", params={"kind": "filled"})).json()
    assert all(it["kind"] == "corporate" or any(o["filled_qty"] for o in it["orders"])
               for d in filled["days"] for it in d["items"])
    conflicts = (await client.get("/api/v1/arena/conflicts", params={"date": DAY1.isoformat()})).json()
    assert conflicts["date"] == DAY1.isoformat() and isinstance(conflicts["conflicts"], list)
    assert "conflicts_today" in (await client.get("/api/v1/arena/feed")).json()

    r = await client.post("/api/v1/join", json={"display_name": "翻页玩家", "kind": "human"})
    auth = {"Authorization": f"Bearer {r.json()['token']}"}
    today = (await client.get("/api/v1/agent/orders", headers=auth)).json()
    hist = (await client.get("/api/v1/agent/orders", headers=auth, params={"scope": "history"})).json()
    assert today["orders"] == [] and hist == {"orders": [], "days": [], "next_before": None}
    await client.aclose(); await service.stop(); await pool.close()


@pytest.mark.anyio
async def test_equity_curve_sparks_and_etag(dsn):
    service, client, pool = await boot(dsn)
    await play(service, DemoMarket(seed=11).day(DAY1))
    await service.close_day()
    async with pool.acquire() as conn:
        n = await conn.fetchval("SELECT count(DISTINCT ts) FROM equity_points")
    assert n >= 40                                     # every 5 minutes of continuous trading, plus the close
    sparks = (await client.get("/api/v1/leaderboard/sparks")).json()
    board = (await client.get("/api/v1/leaderboard")).json()
    assert sparks["unit"] == "bps" and "spark" not in board["rows"][0] and "market" not in board
    dog = next(r for r in board["rows"] if r["name"] == "激进打板动量狗")
    line = sparks["series"][dog["portfolio_id"]]
    assert 20 <= len(line) <= 60 and line[-1] == round(dog["cum_return"] * 10_000)
    for rng in ("today", "5d", "month", "season"):
        c = (await client.get(f"/api/v1/portfolios/{dog['portfolio_id']}/curve", params={"range": rng})).json()
        assert c["series"] and c["benchmark"], rng
    today = (await client.get(f"/api/v1/portfolios/{dog['portfolio_id']}/curve", params={"range": "today"})).json()
    assert today["trades"] and {"t", "side", "name", "thesis", "item"} <= set(today["trades"][0])
    # unchanged data: the second request gets a bodyless 304
    r1 = await client.get("/api/v1/leaderboard")
    r2 = await client.get("/api/v1/leaderboard", headers={"If-None-Match": r1.headers["etag"]})
    assert r1.status_code == 200 and r2.status_code == 304 and r2.content == b""
    assert r1.headers["cache-control"] == "no-cache"
    await client.aclose(); await service.stop(); await pool.close()
