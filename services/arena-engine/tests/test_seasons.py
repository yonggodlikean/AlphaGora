"""Season calendar, monthly board and the season rollover (settle, badges, carry-over, fresh cash)."""
import uuid
from datetime import date
from decimal import Decimal

import pytest

from arena_engine.market.demo import DemoMarket
from arena_engine.seasons import season_for
from test_service_api import DAY1, boot, play  # noqa: F401
from test_service_api import anyio_backend, dsn, pg  # noqa: F401


def test_season_calendar():
    assert season_for(date(2026, 11, 25)).slug == "s1-2026w"                    # launch week belongs to S1
    s1 = season_for(date(2027, 2, 26))
    assert (s1.number, s1.start, s1.end) == (1, date(2026, 11, 23), date(2027, 2, 28))
    s2 = season_for(date(2027, 3, 1))
    assert (s2.slug, s2.name, s2.end) == ("s2-2027sp", "Season 2 · 2027 春季赛", date(2027, 5, 31))
    assert season_for(date(2028, 1, 5)).slug == "s5-2027w" and season_for(date(2028, 2, 29)).end == date(2028, 2, 29)


@pytest.mark.anyio
async def test_month_board_and_rollover(dsn):
    service, client, pool = await boot(dsn)
    market = DemoMarket(seed=11)
    r = await client.post("/api/v1/join", json={"display_name": "跨季玩家", "kind": "human"})
    old_pid, auth = r.json()["portfolio_id"], {"Authorization": f"Bearer {r.json()['token']}"}
    await play(service, market.day(DAY1), 0, 130)
    snap = (await client.get("/api/v1/market/snapshot", params={"symbols": "999002.SH"})).json()["snapshots"][0]
    o = await client.post("/api/v1/agent/orders", headers=auth, json={
        "idempotency_key": "season-0001", "symbol": "999002.SH", "side": "buy", "price": snap["asks"][0][0],
        "qty": 1000, "thesis": "赛季末上车"})
    assert o.status_code == 201
    await play(service, market.day(DAY1), 130)
    await service.close_day()

    month = (await client.get("/api/v1/leaderboard", params={"period": "month"})).json()
    assert month["period"] == "month" and all("month_return" in row for row in month["rows"])
    old_season, old_slug = service.season_id, service.season["slug"]
    spare = (await service.store.create_invites(old_season, "human", 1, 1, "spare"))[0]
    service.auto_season = True
    service.season["end_date"] = date(2026, 11, 30)              # S0 ends with November

    dec1 = date(2026, 12, 1)
    await play(service, market.day(dec1), 0, 10)                  # opens 12-01: award November, settle, roll over
    assert service.season["slug"] == "s1-2026w" and service.season_id != old_season
    async with pool.acquire() as conn:
        status = await conn.fetchval("SELECT status FROM seasons WHERE id = $1", old_season)
        ranks = [r["rank"] for r in await conn.fetch(
            "SELECT rank FROM season_results WHERE season_id = $1 ORDER BY rank NULLS LAST", old_season)]
        champs = await conn.fetchval("SELECT count(*) FROM portfolio_badges WHERE badge_code = 'season_champion'")
        months = await conn.fetchval("SELECT count(*) FROM portfolio_badges WHERE badge_code = 'month_best'")
    assert status == "settled" and ranks[:3] == [1, 2, 3] and ranks[-1] is None and champs == 1
    assert months <= 1 + len(ranks)                               # November's best (ties share it)

    # the human team is carried over: same token, fresh cash, no positions, last season in its career
    st = (await client.get("/api/v1/agent/state", headers=auth)).json()
    assert st["portfolio_id"] != old_pid and Decimal(st["total_equity"]) == Decimal("1000000") and st["positions"] == []
    prof = (await client.get(f"/api/v1/portfolios/{st['portfolio_id']}")).json()
    assert prof["season"]["slug"] == "s1-2026w" and prof["career"][0]["season"] == old_slug
    assert prof["career"][0]["rank"] is not None
    report = (await client.get(f"/api/v1/seasons/{old_slug}")).json()
    assert report["status"] == "settled" and report["standings"][0]["rank"] == 1
    seasons = (await client.get("/api/v1/seasons")).json()["seasons"]
    assert [x["slug"] for x in seasons if x["current"]] == ["s1-2026w"]
    # official teams exist again in the new season and the board works
    board = (await client.get("/api/v1/leaderboard")).json()
    assert board["season"]["slug"] == "s1-2026w" and len(board["rows"]) >= 7
    ev = [e for e in await service.store.events_after(service.season_id, 0) if e["type"] == "season.started"]
    assert ev and ev[0]["data"]["carried"] == 1
    assert uuid.UUID(st["portfolio_id"])
    async with pool.acquire() as conn:      # an unused invite moved to the new season
        assert await conn.fetchval("SELECT season_id FROM invites WHERE code = $1", spare) == service.season_id
    await client.aclose(); await service.stop(); await pool.close()
