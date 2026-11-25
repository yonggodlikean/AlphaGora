"""PostgreSQL persistence. The engine is the source of truth while running; this module writes every change
it makes in one transaction per command/tick and rebuilds the engine on startup."""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import asyncpg

from .domain.models import Fill, Order, Position
from .ledger.journal import Journal, fill_journal, funding_journal
from .personas import PERSONAS

ZERO = Decimal("0")
HOUSE_PARTICIPANT = "00000000-0000-4000-8000-000000000001"


@dataclass
class PortfolioMeta:
    id: str
    name: str
    controller_type: str
    is_official: bool
    is_benchmark: bool
    persona_tag: str | None
    brief: str | None
    created_at: datetime


def _json(x: Any) -> str:
    return json.dumps(x, ensure_ascii=False, default=str)


class Store:
    def __init__(self, pool: asyncpg.Pool) -> None:
        self.pool = pool

    # ------------------------------------------------------------------ season & portfolios
    async def ensure_season(self, slug: str, start: date, end: date) -> dict:
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "INSERT INTO participants (id, display_name, is_house) VALUES ($1, 'Agora House', true) "
                "ON CONFLICT (id) DO NOTHING", uuid.UUID(HOUSE_PARTICIPANT))
            row = await conn.fetchrow("SELECT * FROM seasons WHERE slug = $1", slug)
            if row is None:
                row = await conn.fetchrow(
                    "INSERT INTO seasons (slug, name, start_date, end_date, status) VALUES ($1, $2, $3, $4, 'live') "
                    "RETURNING *", slug, "演示赛季" if slug == "demo" else slug, start, end)
            for p in PERSONAS:
                # Keep official team names in sync with personas.py (a rename must not create a second team).
                await conn.execute(
                    "UPDATE portfolios SET name = $3 WHERE season_id = $1 AND persona_tag = $2 AND is_official "
                    "AND name <> $3 AND NOT EXISTS (SELECT 1 FROM portfolios WHERE season_id = $1 AND name = $3)",
                    row["id"], p.tag, p.name)
                await conn.execute(
                    "INSERT INTO portfolios (season_id, participant_id, name, persona_tag, controller_type, "
                    "is_official, is_benchmark, brief) VALUES ($1, $2, $3, $4, 'openpup_ai', true, $5, $6) "
                    "ON CONFLICT (season_id, name) DO NOTHING",
                    row["id"], uuid.UUID(HOUSE_PARTICIPANT), p.name, p.tag, p.is_benchmark, p.brief)
            return dict(row)

    async def portfolios(self, season_id: uuid.UUID) -> list[PortfolioMeta]:
        rows = await self.pool.fetch(
            "SELECT id, name, controller_type::text, is_official, is_benchmark, persona_tag, brief, created_at "
            "FROM portfolios WHERE season_id = $1 ORDER BY created_at, name", season_id)
        return [PortfolioMeta(str(r["id"]), r["name"], r["controller_type"], r["is_official"], r["is_benchmark"],
                              r["persona_tag"], r["brief"], r["created_at"]) for r in rows]

    async def create_entrant(self, season_id: uuid.UUID, display_name: str, controller_type: str,
                             persona_tag: str | None, brief: str | None,
                             token_prefix: str, token_hash: bytes) -> PortfolioMeta:
        async with self.pool.acquire() as conn, conn.transaction():
            participant = await conn.fetchval(
                "INSERT INTO participants (display_name) VALUES ($1) RETURNING id", display_name)
            r = await conn.fetchrow(
                "INSERT INTO portfolios (season_id, participant_id, name, persona_tag, controller_type, brief) "
                "VALUES ($1, $2, $3, $4, $5::controller_type, $6) "
                "RETURNING id, name, controller_type::text, is_official, is_benchmark, persona_tag, brief, created_at",
                season_id, participant, display_name, persona_tag, controller_type, brief)
            await conn.execute(
                "INSERT INTO api_tokens (portfolio_id, token_prefix, token_hash) VALUES ($1, $2, $3)",
                r["id"], token_prefix, token_hash)
        return PortfolioMeta(str(r["id"]), r["name"], r["controller_type"], r["is_official"], r["is_benchmark"],
                             r["persona_tag"], r["brief"], r["created_at"])

    async def official_portfolio(self, season_id: uuid.UUID, persona_tag: str) -> PortfolioMeta | None:
        r = await self.pool.fetchrow(
            "SELECT id, name, controller_type::text, is_official, is_benchmark, persona_tag, brief, created_at "
            "FROM portfolios WHERE season_id = $1 AND persona_tag = $2 AND is_official", season_id, persona_tag)
        return None if r is None else PortfolioMeta(str(r["id"]), r["name"], r["controller_type"], r["is_official"],
                                                    r["is_benchmark"], r["persona_tag"], r["brief"], r["created_at"])

    async def add_token(self, portfolio_id: str, token_prefix: str, token_hash: bytes) -> None:
        await self.pool.execute(
            "INSERT INTO api_tokens (portfolio_id, token_prefix, token_hash) VALUES ($1, $2, $3)",
            uuid.UUID(portfolio_id), token_prefix, token_hash)

    async def token_exists(self, token_hash: bytes) -> bool:
        return bool(await self.pool.fetchval("SELECT 1 FROM api_tokens WHERE token_hash = $1", token_hash))

    async def last_seen(self, season_id: uuid.UUID) -> dict[str, tuple[datetime, str | None]]:
        rows = await self.pool.fetch(
            "SELECT id, last_seen_at, driver_name FROM portfolios WHERE season_id = $1 AND last_seen_at IS NOT NULL",
            season_id)
        return {str(r["id"]): (r["last_seen_at"], r["driver_name"]) for r in rows}

    async def touch(self, portfolio_id: str, ts: datetime, driver_name: str | None) -> None:
        await self.pool.execute(
            "UPDATE portfolios SET last_seen_at = $2, driver_name = COALESCE($3, driver_name) WHERE id = $1",
            uuid.UUID(portfolio_id), ts, driver_name)

    async def portfolio_for_token(self, token_hash: bytes) -> str | None:
        pid = await self.pool.fetchval(
            "SELECT portfolio_id FROM api_tokens WHERE token_hash = $1 AND revoked_at IS NULL", token_hash)
        return None if pid is None else str(pid)

    # ------------------------------------------------------------------ rebuild
    async def load_balances(self, portfolio_ids: list[str]) -> dict[tuple[str, str, str | None], Decimal]:
        rows = await self.pool.fetch(
            "SELECT DISTINCT ON (portfolio_id, account, symbol) portfolio_id, account::text, symbol, balance_after "
            "FROM ledger_entries WHERE portfolio_id = ANY($1::uuid[]) "
            "ORDER BY portfolio_id, account, symbol, id DESC", [uuid.UUID(p) for p in portfolio_ids])
        return {(str(r["portfolio_id"]), r["account"], r["symbol"]): r["balance_after"] for r in rows}

    async def load_positions(self, portfolio_ids: list[str]) -> dict[str, dict[str, tuple[int, int, Decimal]]]:
        rows = await self.pool.fetch(
            "SELECT portfolio_id, symbol, qty, frozen_qty, cost_basis FROM positions "
            "WHERE portfolio_id = ANY($1::uuid[]) AND qty > 0", [uuid.UUID(p) for p in portfolio_ids])
        out: dict[str, dict[str, tuple[int, int, Decimal]]] = {}
        for r in rows:
            out.setdefault(str(r["portfolio_id"]), {})[r["symbol"]] = (r["qty"], r["frozen_qty"], r["cost_basis"])
        return out

    async def load_open_orders(self, portfolio_ids: list[str]) -> list[asyncpg.Record]:
        """Open orders left by a previous run, with what they had filled (to rebuild fees and reservations)."""
        return await self.pool.fetch(
            """SELECT o.*, COALESCE(sum(t.notional), 0) AS filled_notional, COALESCE(sum(t.commission), 0) AS commission
               FROM orders o LEFT JOIN trades t ON t.order_id = o.id
               WHERE o.status IN ('new', 'partially_filled') AND o.portfolio_id = ANY($1::uuid[])
               GROUP BY o.id ORDER BY o.created_at""", [uuid.UUID(p) for p in portfolio_ids])

    async def expire_orders(self, order_ids: list[str], reason: str) -> None:
        if not order_ids:
            return
        ids = [uuid.UUID(i) for i in order_ids]
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.execute(
                "UPDATE orders SET status = 'expired', reject_reason = $2 WHERE id = ANY($1::uuid[])", ids, reason)
            await conn.executemany(
                "INSERT INTO order_events (order_id, event_type, payload) VALUES ($1, 'order.expired', $2::jsonb)",
                [(i, _json({"reason": reason})) for i in ids])

    async def has_market_open(self, season_id: uuid.UUID, trade_date: date) -> bool:
        return bool(await self.pool.fetchval(
            "SELECT 1 FROM arena_events WHERE season_id = $1 AND event_type = 'market.open' "
            "AND payload->>'trade_date' = $2 LIMIT 1", season_id, trade_date.isoformat()))

    async def last_trade_date(self, season_id: uuid.UUID) -> date | None:
        v = await self.pool.fetchval(
            "SELECT max((payload->>'trade_date')::date) FROM arena_events "
            "WHERE season_id = $1 AND event_type = 'market.open'", season_id)
        return v

    async def snapshot_history(self, portfolio_ids: list[str], limit: int = 30) -> dict[str, list[dict]]:
        rows = await self.pool.fetch(
            "SELECT * FROM (SELECT d.*, row_number() OVER (PARTITION BY portfolio_id ORDER BY trade_date DESC) rn "
            "FROM daily_snapshots d WHERE portfolio_id = ANY($1::uuid[])) t WHERE rn <= $2 "
            "ORDER BY portfolio_id, trade_date", [uuid.UUID(p) for p in portfolio_ids], limit)
        out: dict[str, list[dict]] = {}
        for r in rows:
            out.setdefault(str(r["portfolio_id"]), []).append(dict(r))
        return out

    async def symbol_names(self) -> dict[str, str]:
        return {r["symbol"]: r["name"] for r in await self.pool.fetch("SELECT symbol, name FROM symbols")}

    async def upsert_symbol_names(self, names: dict[str, str]) -> None:
        if names:
            await self.pool.executemany(
                "INSERT INTO symbols (symbol, name) VALUES ($1, $2) ON CONFLICT (symbol) "
                "DO UPDATE SET name = EXCLUDED.name, updated_at = now()", list(names.items()))

    # ------------------------------------------------------------------ orders / debates lookups
    async def order_by_key(self, portfolio_id: str, key: str) -> asyncpg.Record | None:
        return await self.pool.fetchrow(
            "SELECT * FROM orders WHERE portfolio_id = $1 AND idempotency_key = $2", uuid.UUID(portfolio_id), key)

    async def debate_owner(self, debate_id: str) -> str | None:
        try:
            did = uuid.UUID(debate_id)
        except ValueError:
            return None
        v = await self.pool.fetchval("SELECT portfolio_id FROM debates WHERE id = $1", did)
        return None if v is None else str(v)

    async def create_debate(self, portfolio_id: str, symbol: str, rounds: list[dict], final_decision: dict,
                            execution_trace: Any | None, trace_hash: str | None) -> dict:
        r = await self.pool.fetchrow(
            "INSERT INTO debates (portfolio_id, symbol, rounds, final_decision, execution_trace, execution_trace_hash) "
            "VALUES ($1, $2, $3::jsonb, $4::jsonb, $5::jsonb, $6) RETURNING id, has_conflict, created_at",
            uuid.UUID(portfolio_id), symbol, _json(rounds), _json(final_decision),
            None if execution_trace is None else _json(execution_trace), trace_hash)
        return {"debate_id": str(r["id"]), "has_conflict": r["has_conflict"], "created_at": r["created_at"]}

    async def recent_orders(self, portfolio_id: str, limit: int = 30) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            "SELECT * FROM orders WHERE portfolio_id = $1 ORDER BY created_at DESC LIMIT $2",
            uuid.UUID(portfolio_id), limit)

    # ------------------------------------------------------------------ the write path
    async def persist(
        self,
        season_id: uuid.UUID,
        orders: list[Order],
        fills: list[Fill],
        positions: list[tuple[str, Position]],
        order_events: list[tuple[str, str, dict]],
        arena_events: list[tuple[str, str | None, dict]],
        balances: dict[tuple[str, str, str | None], Decimal],
        badges: list[tuple[str, str, str | None]] = (),
        cancel_reasons: dict[str, str] | None = None,
    ) -> list[int]:
        """Writes one engine result atomically. Returns the arena_events ids (for SSE Last-Event-ID)."""
        cancel_reasons = cancel_reasons or {}
        async with self.pool.acquire() as conn, conn.transaction():
            for o in orders:
                await conn.execute(
                    """INSERT INTO orders (id, portfolio_id, debate_id, idempotency_key, request_hash, thesis, side,
                         symbol, order_type, price, qty, filled_qty, avg_fill_price, fees_paid, queue_initial,
                         queue_ahead, status, reject_reason, trade_date, created_at)
                       VALUES ($1,$2,$3,$4,$5,$6,$7::order_side,$8,$9::order_type,$10,$11,$12,$13,$14,$15,$16,
                               $17::order_status,$18,$19,$20)
                       ON CONFLICT (id) DO UPDATE SET filled_qty = EXCLUDED.filled_qty,
                         avg_fill_price = EXCLUDED.avg_fill_price, fees_paid = EXCLUDED.fees_paid,
                         queue_initial = EXCLUDED.queue_initial, queue_ahead = EXCLUDED.queue_ahead,
                         status = EXCLUDED.status, reject_reason = COALESCE(EXCLUDED.reject_reason, orders.reject_reason)""",
                    uuid.UUID(o.id), uuid.UUID(o.portfolio_id), uuid.UUID(o.debate_id) if o.debate_id else None,
                    o.idempotency_key, o.request_hash, o.thesis, o.side.value, o.symbol, o.order_type.value, o.price,
                    o.qty, o.filled_qty, o.avg_fill_price, o.fees_paid, o.queue_initial, o.queue_ahead,
                    o.status.value, cancel_reasons.get(o.id), o.trade_date, o.created_at)

            for f in fills:
                await conn.execute(
                    """INSERT INTO trades (id, order_id, portfolio_id, debate_id, symbol, side, price, qty, notional,
                         commission, stamp_duty, transfer_fee, liquidity, snapshot_seq, trade_date, executed_at)
                       VALUES ($1,$2,$3,$4,$5,$6::order_side,$7,$8,$9,$10,$11,$12,$13::fill_liquidity,$14,$15,$16)""",
                    uuid.UUID(f.id), uuid.UUID(f.order_id), uuid.UUID(f.portfolio_id),
                    uuid.UUID(f.debate_id) if f.debate_id else None, f.symbol, f.side.value, f.price, f.qty,
                    f.notional, f.commission, f.stamp_duty, f.transfer_fee, f.liquidity.value, f.snapshot_seq,
                    f.trade_date, f.executed_at)
                await self._journal(conn, fill_journal(f), balances)

            for pid, pos in positions:
                await conn.execute(
                    """INSERT INTO positions (portfolio_id, symbol, qty, frozen_qty, cost_basis) VALUES ($1,$2,$3,$4,$5)
                       ON CONFLICT (portfolio_id, symbol) DO UPDATE SET qty = EXCLUDED.qty,
                         frozen_qty = EXCLUDED.frozen_qty, cost_basis = EXCLUDED.cost_basis""",
                    uuid.UUID(pid), pos.symbol, pos.qty, pos.frozen_qty, pos.cost_basis)

            if order_events:
                await conn.executemany(
                    "INSERT INTO order_events (order_id, event_type, payload) VALUES ($1, $2, $3::jsonb)",
                    [(uuid.UUID(oid), t, _json(p)) for oid, t, p in order_events])

            for pid, code, ref in badges:
                await conn.execute(
                    "INSERT INTO portfolio_badges (portfolio_id, badge_code, ref_id) VALUES ($1, $2, $3)",
                    uuid.UUID(pid), code, uuid.UUID(ref) if ref else None)

            ids = []
            for etype, symbol, payload in arena_events:
                ids.append(await conn.fetchval(
                    "INSERT INTO arena_events (season_id, event_type, symbol, payload) VALUES ($1,$2,$3,$4::jsonb) "
                    "RETURNING id", season_id, etype, symbol, _json(payload)))
            return ids

    async def fund(self, portfolio_id: str, season_id: uuid.UUID, amount: Decimal,
                   balances: dict[tuple[str, str, str | None], Decimal]) -> bool:
        """Season funding, exactly once per portfolio (journal id = uuid5(season, portfolio))."""
        jid = uuid.uuid5(season_id, portfolio_id)
        async with self.pool.acquire() as conn, conn.transaction():
            if await conn.fetchval("SELECT 1 FROM ledger_journals WHERE id = $1", jid):
                return False
            await self._journal(conn, funding_journal(portfolio_id, str(season_id), amount), balances,
                                journal_id=jid, ref_id=jid)
            return True

    async def _journal(self, conn: asyncpg.Connection, j: Journal,
                       balances: dict[tuple[str, str, str | None], Decimal],
                       journal_id: uuid.UUID | None = None, ref_id: uuid.UUID | None = None) -> None:
        jid = journal_id or uuid.uuid4()
        await conn.execute(
            "INSERT INTO ledger_journals (id, portfolio_id, entry_type, ref_type, ref_id) "
            "VALUES ($1, $2, $3::ledger_entry_type, $4, $5)",
            jid, uuid.UUID(j.portfolio_id), j.entry_type, j.ref_type, ref_id or uuid.UUID(j.ref_id))
        for p in j.postings:
            key = (j.portfolio_id, p.account, p.symbol)
            bal = balances.get(key, ZERO) + p.amount
            balances[key] = bal
            await conn.execute(
                "INSERT INTO ledger_entries (journal_id, portfolio_id, account, symbol, amount, qty_delta, balance_after) "
                "VALUES ($1, $2, $3::ledger_account, $4, $5, $6, $7)",
                jid, uuid.UUID(j.portfolio_id), p.account, p.symbol, p.amount, p.qty_delta, bal)

    # ------------------------------------------------------------------ daily close
    async def write_daily_snapshots(self, rows: list[dict]) -> None:
        await self.pool.executemany(
            """INSERT INTO daily_snapshots (portfolio_id, trade_date, total_equity, cash, market_value, daily_return,
                 cum_return, excess_return, max_drawdown, sharpe_ratio, win_rate, turnover, rank)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
               ON CONFLICT (portfolio_id, trade_date) DO UPDATE SET total_equity = EXCLUDED.total_equity,
                 cash = EXCLUDED.cash, market_value = EXCLUDED.market_value, daily_return = EXCLUDED.daily_return,
                 cum_return = EXCLUDED.cum_return, excess_return = EXCLUDED.excess_return,
                 max_drawdown = EXCLUDED.max_drawdown, sharpe_ratio = EXCLUDED.sharpe_ratio,
                 win_rate = EXCLUDED.win_rate, turnover = EXCLUDED.turnover, rank = EXCLUDED.rank""",
            [(uuid.UUID(r["portfolio_id"]), r["trade_date"], r["total_equity"], r["cash"], r["market_value"],
              r["daily_return"], r["cum_return"], r["excess_return"], r["max_drawdown"], r["sharpe_ratio"],
              r["win_rate"], r["turnover"], r["rank"]) for r in rows])

    async def day_trades(self, trade_date: date, portfolio_ids: list[str]) -> list[asyncpg.Record]:
        return await self.pool.fetch(
            "SELECT t.*, o.price AS limit_price FROM trades t JOIN orders o ON o.id = t.order_id "
            "WHERE t.trade_date = $1 AND t.portfolio_id = ANY($2::uuid[])",
            trade_date, [uuid.UUID(p) for p in portfolio_ids])

    async def win_rates(self, portfolio_ids: list[str]) -> dict[str, tuple[int, int]]:
        """(winning sells, total sells), using the cost released into the ledger for each sell fill."""
        rows = await self.pool.fetch(
            """SELECT t.portfolio_id, count(*) AS n,
                      count(*) FILTER (WHERE t.notional + e.amount > 0) AS wins
               FROM trades t
               JOIN ledger_journals j ON j.ref_type = 'trade' AND j.ref_id = t.id
               JOIN ledger_entries e ON e.journal_id = j.id AND e.account = 'position'
               WHERE t.side = 'sell' AND t.portfolio_id = ANY($1::uuid[])
               GROUP BY t.portfolio_id""", [uuid.UUID(p) for p in portfolio_ids])
        return {str(r["portfolio_id"]): (r["wins"], r["n"]) for r in rows}

    async def queue_stats(self, portfolio_id: str) -> tuple[int, int]:
        r = await self.pool.fetchrow(
            "SELECT count(*) FILTER (WHERE queue_initial IS NOT NULL AND status <> 'rejected') AS queued, "
            "count(*) FILTER (WHERE queue_initial IS NOT NULL AND filled_qty > 0) AS got "
            "FROM orders WHERE portfolio_id = $1 AND order_type = 'limit'", uuid.UUID(portfolio_id))
        return r["got"], r["queued"]

    async def badge_counts(self, portfolio_id: str) -> dict[str, int]:
        rows = await self.pool.fetch(
            "SELECT badge_code, count(*) n FROM portfolio_badges WHERE portfolio_id = $1 GROUP BY badge_code",
            uuid.UUID(portfolio_id))
        return {r["badge_code"]: r["n"] for r in rows}

    async def has_badge(self, portfolio_id: str, code: str) -> bool:
        return bool(await self.pool.fetchval(
            "SELECT 1 FROM portfolio_badges WHERE portfolio_id = $1 AND badge_code = $2 LIMIT 1",
            uuid.UUID(portfolio_id), code))

    # ------------------------------------------------------------------ feeds for the UI
    async def latest_events(self, season_id: uuid.UUID, event_type: str, limit: int) -> list[dict]:
        rows = await self.pool.fetch(
            "SELECT id, event_type, symbol, payload, created_at FROM arena_events "
            "WHERE season_id = $1 AND event_type = $2 ORDER BY id DESC LIMIT $3", season_id, event_type, limit)
        return [{"id": r["id"], "type": r["event_type"], "symbol": r["symbol"],
                 "data": json.loads(r["payload"]), "created_at": r["created_at"].isoformat()} for r in rows]

    async def events_after(self, season_id: uuid.UUID, after_id: int, limit: int = 500) -> list[dict]:
        rows = await self.pool.fetch(
            "SELECT id, event_type, payload FROM arena_events WHERE season_id = $1 AND id > $2 ORDER BY id LIMIT $3",
            season_id, after_id, limit)
        return [{"id": r["id"], "type": r["event_type"], "data": json.loads(r["payload"])} for r in rows]

    async def timeline(self, portfolio_id: str, limit: int = 40) -> list[dict]:
        pid = uuid.UUID(portfolio_id)
        debates = await self.pool.fetch(
            "SELECT id, symbol, rounds, final_decision, has_conflict, execution_trace_hash, created_at "
            "FROM debates WHERE portfolio_id = $1 ORDER BY created_at DESC LIMIT $2", pid, limit)
        orders = await self.pool.fetch(
            """SELECT o.*, (SELECT string_agg(DISTINCT t.liquidity::text, ',') FROM trades t WHERE t.order_id = o.id)
                          AS liquidities
               FROM orders o WHERE o.portfolio_id = $1 ORDER BY o.created_at DESC LIMIT $2""", pid, limit * 2)
        by_debate: dict[str, list[asyncpg.Record]] = {}
        loose: list[asyncpg.Record] = []
        for o in orders:
            if o["debate_id"]:
                by_debate.setdefault(str(o["debate_id"]), []).append(o)
            else:
                loose.append(o)
        items = []
        for d in debates:
            items.append({"kind": "debate", "id": str(d["id"]), "symbol": d["symbol"],
                          "created_at": d["created_at"], "rounds": json.loads(d["rounds"]),
                          "final_decision": json.loads(d["final_decision"]), "has_conflict": d["has_conflict"],
                          "trace_hash": d["execution_trace_hash"],
                          "orders": [_order_row(o) for o in by_debate.get(str(d["id"]), [])]})
        for o in loose:
            items.append({"kind": "order", "id": str(o["id"]), "symbol": o["symbol"], "created_at": o["created_at"],
                          "orders": [_order_row(o)]})
        items.sort(key=lambda x: x["created_at"], reverse=True)
        for it in items:
            it["created_at"] = it["created_at"].isoformat()
        return items[:limit]


def _order_row(o: asyncpg.Record) -> dict:
    return {
        "order_id": str(o["id"]), "side": o["side"], "order_type": o["order_type"],
        "price": None if o["price"] is None else str(o["price"]), "qty": o["qty"], "filled_qty": o["filled_qty"],
        "avg_fill_price": None if o["avg_fill_price"] is None else str(o["avg_fill_price"]),
        "fees_paid": str(o["fees_paid"]), "status": o["status"], "thesis": o["thesis"],
        "queue_initial": o["queue_initial"], "liquidity": (o["liquidities"] or "").split(",") if o.get("liquidities") else [],
        "reason": o["reject_reason"], "created_at": o["created_at"].isoformat(),
    }
