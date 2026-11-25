"""ArenaService: the single writer around the matching engine.

Every command (order, cancel), market snapshot and day transition runs under one asyncio lock:
engine mutation -> one Postgres transaction -> publish to SSE subscribers. Reads (leaderboard, state,
profile) come from engine memory plus a few queries.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import math
import secrets
import uuid
from collections import deque
from pathlib import Path
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import asyncpg

from .config import Settings
from .domain import events as ev
from .domain.enums import ControllerType, Liquidity, OrderStatus, OrderType, RejectCode, Side
from .domain.errors import ArenaError, MarketDataError, OrderRejected
from .domain.events import ArenaEvent
from .domain.models import Fill, OrderRequest, Position
from .house_bots import HouseBots, Intent, MarketView
from .market.calendar import CST, TradingCalendar
from .market.demo import DemoFeed, EndOfDay
from .market.feed import RedisStreamFeed, ReplayFeed
from .market.snapshot import L1Snapshot
from .matching import AShareMatchingEngine
from .matching.fees import money
from .personas import BADGE_BY_CODE, BADGES, BY_TAG
from .roast import DayStat, build_roast
from .store import PortfolioMeta, Store
from .bus import EventBus

log = logging.getLogger(__name__)
ZERO = Decimal("0")
Q8 = Decimal("0.00000001")

# Persisted to arena_events (replayable via SSE Last-Event-ID). Everything else is live-only.
PUBLIC_EVENTS = {ev.MARKET_OPEN, ev.MARKET_CLOSE, ev.ORDER_FILLED, ev.LIMIT_UP_BROKEN, ev.LIMIT_UP_SEALED,
                 ev.LIMIT_DOWN_BROKEN, ev.CONFLICT_DETECTED, "badge.awarded", "roast.daily"}
CONFLICT_WINDOW = timedelta(minutes=30)


def _dec(x: float | Decimal) -> Decimal:
    return Decimal(str(x)).quantize(Q8)


class ArenaService:
    def __init__(self, settings: Settings, pool: asyncpg.Pool) -> None:
        self.settings = settings
        self.store = Store(pool)
        self.calendar = TradingCalendar()
        self.engine = AShareMatchingEngine(self.calendar)
        self.lock = asyncio.Lock()
        self.bus = EventBus()
        self.season: dict = {}
        self.season_id: uuid.UUID | None = None
        self.meta: dict[str, PortfolioMeta] = {}
        self.balances: dict[tuple[str, str, str | None], Decimal] = {}
        self.history: dict[str, list[dict]] = {}
        self.names: dict[str, str] = {}
        self._names_pending: dict[str, str] = {}
        self.market_now: datetime | None = None
        self.day_open_px: dict[str, Decimal] = {}
        self.minute_closes: dict[str, list[Decimal]] = {}
        self._minute_of: dict[str, tuple[int, int]] = {}
        self.recent_fills: deque = deque(maxlen=1000)
        self._conflicts_seen: dict[tuple, datetime] = {}
        self._first_trade_done: set[str] = set()
        self._closed_dates: set[date] = set()
        self._last_bot_minute: tuple | None = None
        self._token_cache: dict[bytes, str] = {}
        self._summaries: dict[str, str] = {}
        self.last_seen: dict[str, tuple[datetime, str | None]] = {}
        self._last_seen_written: dict[str, datetime] = {}   # house order id -> decision summary (for conflict cards)
        self._join_hits: dict[str, deque] = {}
        self.bots: HouseBots | None = None
        self._tasks: list[asyncio.Task] = []
        self.feed = None
        self.feed_done = asyncio.Event()

    # ================================================================== lifecycle
    async def start(self) -> None:
        today = datetime.now(CST).date()
        self.season = await self.store.ensure_season(self.settings.resolved_season_slug(), today,
                                                     today + timedelta(days=120))
        self.season_id = self.season["id"]
        await self._load_portfolios()
        stale = await self.store.cancel_stale_open_orders()
        if stale:
            log.warning("canceled %d open orders left from a previous run", stale)
        self.names = await self.store.symbol_names()
        external = self.settings.resolved_external_teams()
        teams = {p.id: p.persona_tag for p in self.meta.values()
                 if p.is_official and p.persona_tag in BY_TAG and p.persona_tag not in external}
        if external:
            log.info("officially driven from outside (no rule bot): %s", ", ".join(external))
        for tag, raw in self.settings.team_tokens:
            meta = await self.store.official_portfolio(self.season_id, tag)
            if meta is None:
                log.warning("ARENA_TEAM_TOKENS: no official team %r", tag)
            elif not await self.store.token_exists(self.hash_token(raw)):
                await self.store.add_token(meta.id, raw[:8], self.hash_token(raw))
                log.info("registered pre-shared token for %s", meta.name)
        self.last_seen = await self.store.last_seen(self.season_id)
        self.bots = HouseBots(teams, self.settings.resolved_benchmark(), self.settings.resolved_dividend_symbols())
        for pid in self.meta:
            if await self.store.has_badge(pid, "first_trade"):
                self._first_trade_done.add(pid)
        if self.settings.auto_start_feed:
            await self.start_feed()

    async def _load_portfolios(self) -> None:
        metas = await self.store.portfolios(self.season_id)
        ids = [m.id for m in metas]
        self.balances = await self.store.load_balances(ids)
        positions = await self.store.load_positions(ids)
        initial = Decimal(self.season["initial_cash"])
        for m in metas:
            if m.id in self.meta:
                continue
            await self._open_engine_account(m, initial, positions.get(m.id, {}))
        self.history = await self.store.snapshot_history(ids)

    async def _open_engine_account(self, m: PortfolioMeta, initial: Decimal,
                                   positions: dict[str, tuple[int, int, Decimal]]) -> None:
        if (m.id, "cash", None) not in self.balances:
            await self.store.fund(m.id, self.season_id, initial, self.balances)
        cash = self.balances[(m.id, "cash", None)]
        realized = -self.balances.get((m.id, "realized_pnl", None), ZERO)
        fees = sum((self.balances.get((m.id, a, None), ZERO)
                    for a in ("fee_commission", "fee_stamp_duty", "fee_transfer")), ZERO)
        self.engine.restore_account(m.id, ControllerType(m.controller_type), initial, cash, positions, realized, fees)
        self.meta[m.id] = m

    async def start_feed(self) -> None:
        s = self.settings
        if s.market_feed == "demo":
            last = await self.store.last_trade_date(self.season_id)
            start = (last + timedelta(days=1)) if last else datetime.now(CST).date()
            self.feed = DemoFeed(self.calendar, start, speed=s.demo_speed)
        elif s.market_feed == "replay":
            self.feed = ReplayFeed.from_jsonl(Path(s.replay_path), speed=s.demo_speed)
        elif s.market_feed == "redis":
            import redis.asyncio as aioredis
            self.feed = RedisStreamFeed(aioredis.from_url(s.redis_url), stream=s.market_stream)
        else:
            self.feed = None
        if self.feed is not None:
            self._tasks.append(asyncio.create_task(self._run_feed(), name="feed"))
        if not s.is_simulated_clock:
            self._tasks.append(asyncio.create_task(self._run_clock(), name="clock"))

    async def stop(self) -> None:
        if self.feed is not None:
            await self.feed.close()
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    async def _run_feed(self) -> None:
        backoff = 1.0
        while True:
            try:
                async for item in self.feed.stream():
                    if isinstance(item, EndOfDay):
                        await self.close_day()
                        continue
                    await self.on_snapshot(item)
                    await self.maybe_run_bots()
                    backoff = 1.0
                if self.settings.market_feed == "replay":
                    await self.close_day()
                break
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("market feed failed; retrying in %.0fs", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
                if self.settings.market_feed != "redis":
                    break
        self.feed_done.set()

    async def _run_clock(self) -> None:
        """Live mode: close the day at 15:00:05 CST even if quotes stop arriving."""
        while True:
            await asyncio.sleep(5)
            try:
                td = self.engine.trade_date
                if self.engine.day_open and td and datetime.now(CST) >= datetime.combine(td, time(15, 0, 5), CST):
                    await self.close_day()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("clock tick failed")

    # ================================================================== clock
    def now(self) -> datetime:
        if self.settings.is_simulated_clock and self.market_now is not None:
            return self.market_now
        return datetime.now(CST)

    def market_status(self) -> dict:
        now = self.now()
        trading = self.engine.day_open and self.calendar.is_continuous_trading(now) \
            and self.calendar.trade_date_of(now) == self.engine.trade_date
        return {"trading": trading, "day_open": self.engine.day_open,
                "trade_date": None if self.engine.trade_date is None else self.engine.trade_date.isoformat(),
                "clock": now.astimezone(CST).strftime("%H:%M:%S"), "now": now.isoformat(),
                "simulated": self.settings.is_simulated_clock, "feed": self.settings.market_feed}

    # ================================================================== market data
    async def on_snapshot(self, snap: L1Snapshot) -> None:
        async with self.lock:
            d = self.calendar.trade_date_of(snap.ts)
            if d in self._closed_dates or (self.engine.trade_date and d < self.engine.trade_date):
                return
            if self.engine.trade_date != d or not self.engine.day_open:
                if self.engine.day_open:
                    await self._close_day_locked()
                await self._open_day_locked(d, snap.ts)
            if self.market_now is None or snap.ts > self.market_now:
                self.market_now = snap.ts
            self._track(snap)
            try:
                tick = self.engine.on_snapshot(snap)
            except MarketDataError as exc:
                log.warning("dropping snapshot: %s", exc)
                return
            if tick.fills or tick.events:
                await self._handle(tick.fills, tick.events)
            if self._names_pending:
                pending, self._names_pending = self._names_pending, {}
                await self.store.upsert_symbol_names(pending)

    def _track(self, snap: L1Snapshot) -> None:
        sym = snap.symbol
        name = snap.extra.get("name") if snap.extra else None
        if name and self.names.get(sym) != name:
            self.names[sym] = name
            self._names_pending[sym] = name
        t = snap.ts.astimezone(CST).time()
        if sym not in self.day_open_px and t >= time(9, 30):
            self.day_open_px[sym] = snap.last_price
        minute = (t.hour, t.minute)
        if self._minute_of.get(sym) != minute:
            self._minute_of[sym] = minute
            self.minute_closes.setdefault(sym, []).append(snap.last_price)
        else:
            self.minute_closes[sym][-1] = snap.last_price

    async def _open_day_locked(self, d: date, now: datetime) -> None:
        if await self.store.has_market_open(self.season_id, d):
            events = self.engine.resume_trading_day(d, now)
        else:
            events = self.engine.begin_trading_day(d, now)
        self.day_open_px.clear()
        self.minute_closes.clear()
        self._minute_of.clear()
        self._last_bot_minute = None
        positions = [(pid, p) for pid, a in self.engine.accounts.items() for p in a.positions.values()]
        await self._handle([], events, extra_positions=positions)
        log.info("market open %s", d)

    async def close_day(self) -> None:
        async with self.lock:
            if self.engine.day_open:
                await self._close_day_locked()

    async def _close_day_locked(self) -> None:
        d = self.engine.trade_date
        now = self.now() if not self.settings.is_simulated_clock else datetime.combine(d, time(15, 0, 1), CST)
        events = self.engine.end_trading_day(now)
        expired = [e.payload for e in events if e.type == ev.ORDER_EXPIRED]
        badges: list[tuple[str, str, str | None]] = []
        for o in expired:
            book = self.engine.book(o["symbol"])
            if (o["side"] == "buy" and o["filled_qty"] == 0 and o["queue_initial"] is not None and book
                    and o["price"] is not None and Decimal(o["price"]) == book.upper_limit):
                badges.append((o["portfolio_id"], "paile_yitian", o["order_id"]))
        trades = await self.store.day_trades(d, list(self.meta))
        for t in trades:
            book = self.engine.book(t["symbol"])
            if t["side"] == "buy" and book and t["price"] == book.lower_limit and book.last_price == book.upper_limit:
                badges.append((str(t["portfolio_id"]), "ditianban", str(t["id"])))
        await self._handle([], events, badges=badges)

        rows, stats = await self._daily_rows(d, trades, badges)
        await self.store.write_daily_snapshots(rows)
        for r in rows:
            self.history.setdefault(r["portfolio_id"], []).append(r)
        roast = build_roast(stats, d.isoformat())
        await self._emit("roast.daily", None, roast)
        self._closed_dates.add(d)
        log.info("market close %s", d)

    async def _daily_rows(self, d: date, trades, badges) -> tuple[list[dict], list[DayStat]]:
        win = await self.store.win_rates(list(self.meta))
        turnover: dict[str, Decimal] = {}
        n_trades: dict[str, int] = {}
        breaks: dict[str, int] = {}
        for t in trades:
            pid = str(t["portfolio_id"])
            turnover[pid] = turnover.get(pid, ZERO) + t["notional"]
            n_trades[pid] = n_trades.get(pid, 0) + 1
            if t["liquidity"] == "board_break":
                breaks[pid] = breaks.get(pid, 0) + 1
        lonely: dict[str, int] = {}
        for pid, code, _ in badges:
            if code == "paile_yitian":
                lonely[pid] = lonely.get(pid, 0) + 1

        bench_cum = self.benchmark_return()
        rows, stats = [], []
        cums = {}
        for pid, m in self.meta.items():
            eq, cash, mv = self.equity(pid)
            hist = [h for h in self.history.get(pid, []) if h["trade_date"] < d]
            initial = self.engine.accounts[pid].initial_cash
            prev_eq = Decimal(hist[-1]["total_equity"]) if hist else initial
            daily = float(eq / prev_eq - 1) if prev_eq else 0.0
            cum = float(eq / initial - 1)
            cums[pid] = cum
            equities = [Decimal(h["total_equity"]) for h in hist] + [eq]
            peak, mdd = initial, 0.0
            for e in equities:
                peak = max(peak, e)
                mdd = min(mdd, float(e / peak - 1))
            rets = [float(h["daily_return"]) for h in hist] + [daily]
            sharpe = None
            if len(rets) >= 5:
                mean = sum(rets) / len(rets)
                sd = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1))
                sharpe = _dec(mean / sd * math.sqrt(242)) if sd > 0 else None
            w = win.get(pid)
            rows.append({
                "portfolio_id": pid, "trade_date": d, "total_equity": money(eq), "cash": money(cash),
                "market_value": money(mv), "daily_return": _dec(daily), "cum_return": _dec(cum),
                "excess_return": _dec(cum - bench_cum), "max_drawdown": _dec(mdd), "sharpe_ratio": sharpe,
                "win_rate": None if not w or not w[1] else Decimal(w[0]) / Decimal(w[1]),
                "turnover": money(turnover.get(pid, ZERO)), "rank": None,
            })
            stats.append(DayStat(pid, m.name, daily, cum, n_trades.get(pid, 0), breaks.get(pid, 0),
                                 lonely.get(pid, 0), m.is_benchmark))
        ranked = sorted((r for r in rows if not self.meta[r["portfolio_id"]].is_benchmark),
                        key=lambda r: r["cum_return"], reverse=True)
        for i, r in enumerate(ranked, start=1):
            r["rank"] = i
        for r in rows:
            r["win_rate"] = None if r["win_rate"] is None else r["win_rate"].quantize(Decimal("0.0001"))
        return rows, stats

    # ================================================================== the write path
    async def _handle(self, fills: list[Fill], events: list[ArenaEvent], badges=(), extra_positions=(),
                      cancel_reasons: dict[str, str] | None = None) -> None:
        touched = {f.order_id for f in fills}
        for e in events:
            oid = e.payload.get("order_id")
            if oid:
                touched.add(oid)
        orders = [o for o in (self.engine.order(i) for i in touched) if o is not None]
        pos_keys = {(f.portfolio_id, f.symbol) for f in fills}
        positions = list(extra_positions) + [
            (pid, self.engine.accounts[pid].positions.get(sym) or Position(sym)) for pid, sym in pos_keys]

        public: list[tuple[str, str | None, dict]] = []
        live: list[dict] = []
        order_events = []
        for e in events:
            payload = self._enrich(e.payload)
            if e.type.startswith("order."):
                order_events.append((payload["order_id"], e.type, payload))
            if e.type in PUBLIC_EVENTS:
                public.append((e.type, payload.get("symbol"), payload | {"ts": e.ts.isoformat()}))
            else:
                live.append({"type": e.type, "data": payload | {"ts": e.ts.isoformat()}})

        badges = list(badges)
        for f in fills:
            if f.portfolio_id not in self._first_trade_done:
                self._first_trade_done.add(f.portfolio_id)
                badges.append((f.portfolio_id, "first_trade", f.id))
            if f.liquidity is Liquidity.BOARD_BREAK and f.side is Side.BUY:
                badges.append((f.portfolio_id, "zhaban_jiepan", f.id))
            public += self._detect_conflicts(f)
        for pid, code, ref in badges:
            b = BADGE_BY_CODE[code]
            public.append(("badge.awarded", None, {"portfolio_id": pid, "portfolio_name": self._name(pid),
                                                   "badge": code, "name": b.name, "desc": b.desc,
                                                   "ts": self.now().isoformat()}))

        ids = await self.store.persist(self.season_id, orders, fills, positions, order_events, public,
                                       self.balances, badges, cancel_reasons)
        for (etype, _sym, payload), eid in zip(public, ids):
            self.bus.publish({"id": eid, "type": etype, "data": payload})
        for item in live:
            self.bus.publish({"id": None, **item})

    async def _emit(self, etype: str, symbol: str | None, payload: dict) -> int:
        ids = await self.store.persist(self.season_id, [], [], [], [], [(etype, symbol, payload)], self.balances)
        self.bus.publish({"id": ids[0], "type": etype, "data": payload})
        return ids[0]

    def _name(self, pid: str) -> str:
        m = self.meta.get(pid)
        return m.name if m else pid

    def _enrich(self, payload: dict) -> dict:
        out = dict(payload)
        if "portfolio_id" in out:
            out["portfolio_name"] = self._name(out["portfolio_id"])
        if "symbol" in out:
            out["symbol_name"] = self.names.get(out["symbol"], out["symbol"])
        return out

    def _detect_conflicts(self, f: Fill) -> list[tuple[str, str | None, dict]]:
        """Team A buys X while team B sells X within 30 minutes -> one conflict per pair per window."""
        order = self.engine.order(f.order_id)
        thesis = self._thesis_of(order)
        out = []
        now = f.executed_at
        for (ts, sym, side, pid, qty, th) in reversed(self.recent_fills):
            if now - ts > CONFLICT_WINDOW:
                break
            if sym != f.symbol or pid == f.portfolio_id or side == f.side:
                continue
            buyer, seller = (f.portfolio_id, pid) if f.side is Side.BUY else (pid, f.portfolio_id)
            key = (sym, buyer, seller)
            seen = self._conflicts_seen.get(key)
            if seen and now - seen < CONFLICT_WINDOW:
                continue
            self._conflicts_seen[key] = now
            b_qty, s_qty = (f.qty, qty) if f.side is Side.BUY else (qty, f.qty)
            b_th, s_th = (thesis, th) if f.side is Side.BUY else (th, thesis)
            out.append((ev.CONFLICT_DETECTED, sym, {
                "symbol": sym, "symbol_name": self.names.get(sym, sym), "ts": now.isoformat(),
                "buyer_id": buyer, "buyer": self._name(buyer), "buy_qty": b_qty, "buy_thesis": b_th,
                "seller_id": seller, "seller": self._name(seller), "sell_qty": s_qty, "sell_thesis": s_th,
            }))
            break
        self.recent_fills.append((now, f.symbol, f.side, f.portfolio_id, f.qty, thesis))
        return out

    def _thesis_of(self, order) -> str:
        if order is None:
            return ""
        return order.thesis or self._summaries.get(order.id) or "（见辩论记录）"

    # ================================================================== commands
    async def submit_order(self, portfolio_id: str, body: dict, summary: str | None = None) -> dict:
        req = OrderRequest(
            portfolio_id=portfolio_id, symbol=body["symbol"], side=Side(body["side"]),
            order_type=OrderType(body.get("order_type", "limit")), qty=int(body["qty"]),
            idempotency_key=body["idempotency_key"],
            price=None if body.get("price") in (None, "") else Decimal(str(body["price"])),
            thesis=body.get("thesis") or None, debate_id=body.get("debate_id") or None,
        )
        async with self.lock:
            existing = await self.store.order_by_key(portfolio_id, req.idempotency_key)
            if existing is not None:
                if existing["request_hash"] != req.fingerprint():
                    raise OrderRejected(RejectCode.IDEMPOTENCY_CONFLICT,
                                        f"idempotency_key {req.idempotency_key!r} was used for a different order")
                live = self.engine.order(str(existing["id"]))
                return (live.public_view() if live else _row_view(existing)) | {"duplicate": True}
            if req.debate_id and await self.store.debate_owner(req.debate_id) != portfolio_id:
                raise OrderRejected(RejectCode.THESIS_REQUIRED, "debate_id not found for this portfolio")
            res = self.engine.submit_order(req, self.now())
            if summary:
                self._summaries[res.order.id] = summary
            reasons = {res.order.id: "ioc_remainder"} if res.order.status is OrderStatus.CANCELED else None
            await self._handle(res.fills, res.events, cancel_reasons=reasons)
            return self._enrich(res.order.public_view()) | {"duplicate": res.duplicate}

    async def cancel_order(self, portfolio_id: str, order_id: str) -> dict:
        async with self.lock:
            events = self.engine.cancel_order(portfolio_id, order_id, self.now())
            await self._handle([], events, cancel_reasons={order_id: "user"})
            return self.engine.order(order_id).public_view()

    async def submit_debate(self, portfolio_id: str, body: dict) -> dict:
        res = await self.store.create_debate(portfolio_id, body["symbol"], body["rounds"], body["final_decision"],
                                             body.get("execution_trace"), body.get("execution_trace_hash"))
        res["created_at"] = res["created_at"].isoformat()
        return res

    async def maybe_run_bots(self) -> None:
        if not (self.settings.house_bots and self.bots and self.engine.day_open and self.market_now):
            return
        t = self.market_now.astimezone(CST)
        minute = (t.date(), t.hour, t.minute)
        if minute == self._last_bot_minute:
            return
        self._last_bot_minute = minute
        mv = MarketView(self.engine.trade_date, self.market_now, dict(self.engine.books), dict(self.day_open_px),
                        {k: list(v) for k, v in self.minute_closes.items()}, self.names)
        for intent in self.bots.decide(mv, self.engine.accounts):
            await self._execute_intent(intent)

    async def _execute_intent(self, it: Intent) -> None:
        body: dict[str, Any] = {"symbol": it.symbol, "side": it.side.value, "order_type": it.order_type.value,
                                "qty": it.qty, "price": None if it.price is None else str(it.price),
                                "idempotency_key": it.key}
        try:
            if it.rounds:
                d = await self.submit_debate(it.portfolio_id, {
                    "symbol": it.symbol, "rounds": it.rounds,
                    "final_decision": {"action": it.side.value, "title": it.title, "summary": it.summary,
                                       "qty": it.qty, "price": body["price"]}})
                body["debate_id"] = d["debate_id"]
            else:
                body["thesis"] = it.summary
            await self.submit_order(it.portfolio_id, body, summary=it.summary)
        except OrderRejected as exc:
            log.info("house order rejected (%s %s): %s", self._name(it.portfolio_id), it.symbol, exc)

    # ================================================================== join / auth
    def hash_token(self, raw: str) -> bytes:
        return hmac.new(self.settings.token_pepper.encode(), raw.encode(), hashlib.sha256).digest()

    async def authenticate(self, raw: str) -> str | None:
        h = self.hash_token(raw)
        pid = self._token_cache.get(h)
        if pid is None:
            pid = await self.store.portfolio_for_token(h)
            if pid:
                self._token_cache[h] = pid
        return pid if pid in self.meta else None

    async def touch(self, pid: str, driver_name: str | None = None) -> None:
        """Record that an external driver is alive. Wall clock on purpose (liveness, not market time)."""
        now = datetime.now(CST)
        prev_seen, prev_name = self.last_seen.get(pid, (None, None))
        name = driver_name or prev_name
        self.last_seen[pid] = (now, name)
        last_write = self._last_seen_written.get(pid)
        if last_write is None or (now - last_write).total_seconds() >= 60 or name != prev_name:
            self._last_seen_written[pid] = now
            await self.store.touch(pid, now, driver_name)

    def driver(self, pid: str) -> dict | None:
        """Who drives a portfolio and whether they are online. None for humans (they come and go).
        The arena does not care what the driver is: any client with the team's token can drive it."""
        m = self.meta[pid]
        if m.controller_type == "human":
            return None
        if m.is_official and m.persona_tag not in self.settings.resolved_external_teams():
            return {"kind": "rules", "name": "规则引擎", "online": True, "last_seen": None}
        seen, name = self.last_seen.get(pid, (None, None))
        online = seen is not None and (datetime.now(CST) - seen).total_seconds() <= self.settings.online_window_s
        return {"kind": "external", "name": name, "online": online,
                "last_seen": None if seen is None else seen.isoformat()}

    def join_allowed(self, client: str) -> bool:
        q = self._join_hits.setdefault(client, deque())
        now = datetime.now(CST)
        while q and now - q[0] > timedelta(hours=1):
            q.popleft()
        if len(q) >= 10:
            return False
        q.append(now)
        return True

    async def join(self, display_name: str, controller_type: str, persona_tag: str | None,
                   brief: str | None) -> dict:
        raw = "agk_" + secrets.token_urlsafe(24)
        try:
            meta = await self.store.create_entrant(self.season_id, display_name, controller_type, persona_tag, brief,
                                                   raw[:8], self.hash_token(raw))
        except asyncpg.UniqueViolationError:
            raise ArenaError("NAME_TAKEN") from None
        async with self.lock:
            await self._open_engine_account(meta, Decimal(self.season["initial_cash"]), {})
        return {"portfolio_id": meta.id, "name": meta.name, "token": raw,
                "initial_cash": str(self.season["initial_cash"]), "season": self.season["slug"]}

    # ================================================================== reads
    def equity(self, pid: str) -> tuple[Decimal, Decimal, Decimal]:
        acct = self.engine.accounts[pid]
        mv = ZERO
        for p in acct.positions.values():
            if not p.qty:
                continue
            b = self.engine.book(p.symbol)
            mv += (b.last_price * p.qty) if b else p.cost_basis
        return acct.cash + mv, acct.cash, mv

    def benchmark_return(self) -> float:
        for pid, m in self.meta.items():
            if m.is_benchmark:
                eq, _, _ = self.equity(pid)
                return float(eq / self.engine.accounts[pid].initial_cash - 1)
        return 0.0

    def _group(self, m: PortfolioMeta) -> str:
        if m.is_benchmark:
            return "benchmark"
        if m.controller_type == "openpup_ai":
            return "official_ai" if m.is_official else "community_ai"
        return {"human": "human", "hybrid": "hybrid"}.get(m.controller_type, "bot")

    def leaderboard(self, group: str = "all") -> list[dict]:
        bench = self.benchmark_return()
        today = self.engine.trade_date
        rows = []
        for pid, m in self.meta.items():
            g = self._group(m)
            if group == "ai" and g not in ("official_ai", "community_ai", "benchmark"):
                continue
            if group == "human" and g not in ("human", "hybrid", "bot"):
                continue
            eq, _, _ = self.equity(pid)
            initial = self.engine.accounts[pid].initial_cash
            cum = float(eq / initial - 1)
            hist = [h for h in self.history.get(pid, []) if today is None or h["trade_date"] < today]
            prev = Decimal(hist[-1]["total_equity"]) if hist else initial
            spark = [float(h["cum_return"]) for h in hist[-19:]] + [cum]
            rows.append({"portfolio_id": pid, "name": m.name, "group": g, "persona_tag": m.persona_tag,
                         "cum_return": cum, "excess_return": cum - bench, "day_return": float(eq / prev - 1),
                         "driver": self.driver(pid),
                         "equity": str(money(eq)), "spark": [0.0] + spark if len(spark) == 1 else spark})
        rows.sort(key=lambda r: r["cum_return"], reverse=True)
        place = 0
        for r in rows:
            if r["group"] == "benchmark":
                r["rank"] = None
            else:
                place += 1
                r["rank"] = place
        return rows

    def symbols(self) -> list[dict]:
        out = []
        for sym, b in sorted(self.engine.books.items()):
            out.append({"symbol": sym, "name": self.names.get(sym, sym), "last": str(b.last_price),
                        "prev_close": str(b.prev_close), "chg_pct": float(b.last_price / b.prev_close - 1) * 100,
                        "upper_limit": str(b.upper_limit), "lower_limit": str(b.lower_limit),
                        "limit_up_sealed": b.limit_up_sealed, "limit_down_sealed": b.limit_down_sealed,
                        "suspended": b.suspended})
        return out

    def snapshot(self, symbols: list[str]) -> list[dict]:
        out = []
        for sym in symbols:
            b = self.engine.book(sym)
            if b is None:
                continue
            out.append({"symbol": sym, "name": self.names.get(sym, sym), "ts": b.ts.isoformat(), "seq": b.seq,
                        "bids": [[str(l.price), l.volume] for l in b.bids],
                        "asks": [[str(l.price), l.volume] for l in b.asks],
                        "last_price": str(b.last_price), "cumulative_volume": b.cumulative_volume,
                        "upper_limit": str(b.upper_limit), "lower_limit": str(b.lower_limit),
                        "prev_close": str(b.prev_close), "limit_up_sealed": b.limit_up_sealed,
                        "limit_down_sealed": b.limit_down_sealed, "suspended": b.suspended})
        return out

    def queues(self) -> list[dict]:
        out = []
        for o in self.engine.open_orders():
            b = self.engine.book(o.symbol)
            if b is None or o.price is None or o.queue_ahead is None:
                continue
            if not ((o.side is Side.BUY and o.price == b.upper_limit) or (o.side is Side.SELL and o.price == b.lower_limit)):
                continue
            out.append(self._enrich(o.public_view()) | {"sealed": b.limit_up_sealed or b.limit_down_sealed})
        return out

    async def arena_feed(self) -> dict:
        conflicts = await self.store.latest_events(self.season_id, ev.CONFLICT_DETECTED, 6)
        roast = await self.store.latest_events(self.season_id, "roast.daily", 1)
        return {"conflicts": [c["data"] for c in conflicts], "roast": roast[0]["data"] if roast else None,
                "queues": self.queues(), "market": self.market_status(),
                "season": {"slug": self.season["slug"], "name": self.season["name"],
                           "trading_days": len({h["trade_date"] for hs in self.history.values() for h in hs})
                           + (1 if self.engine.day_open else 0)}}

    def rank_info(self, pid: str) -> dict:
        board = self.leaderboard("all")
        me = next(r for r in board if r["portfolio_id"] == pid)
        ai = [r for r in board if r["group"] in ("official_ai", "community_ai")]
        above = [r for r in ai if r["cum_return"] > me["cum_return"]]
        target = min(above, key=lambda r: r["cum_return"]) if above else None
        return {"rank": me["rank"], "cum_return": me["cum_return"], "beaten_ai": len(ai) - len(above),
                "ai_total": len(ai),
                "chase": None if target is None else {"name": target["name"],
                                                     "gap": target["cum_return"] - me["cum_return"]}}

    def agent_state(self, pid: str) -> dict:
        view = self.engine.state_view(pid)
        view["portfolio_name"] = self._name(pid)
        for p in view["positions"]:
            p["name"] = self.names.get(p["symbol"], p["symbol"])
        view["open_orders"] = [self._enrich(o) for o in view["open_orders"]]
        return view | self.rank_info(pid) | {"market": self.market_status()}

    async def profile(self, pid: str) -> dict:
        m = self.meta[pid]
        board = self.leaderboard("all")
        me = next(r for r in board if r["portfolio_id"] == pid)
        hist = self.history.get(pid, [])
        eq, cash, _ = self.equity(pid)
        initial = self.engine.accounts[pid].initial_cash
        peak, mdd = initial, 0.0
        for e in [Decimal(h["total_equity"]) for h in hist] + [eq]:
            peak = max(peak, e)
            mdd = min(mdd, float(e / peak - 1))
        win = (await self.store.win_rates([pid])).get(pid)
        got, queued = await self.store.queue_stats(pid)
        counts = await self.store.badge_counts(pid)
        roast = await self.store.latest_events(self.season_id, "roast.daily", 1)
        acct = self.engine.accounts[pid]
        positions = []
        for p in acct.positions.values():
            if not p.qty:
                continue
            b = self.engine.book(p.symbol)
            last = b.last_price if b else p.cost_basis / p.qty
            cost = p.cost_basis / p.qty
            positions.append({"symbol": p.symbol, "name": self.names.get(p.symbol, p.symbol), "qty": p.qty,
                              "frozen_qty": p.frozen_qty, "cost": str(cost.quantize(Decimal("0.001"))),
                              "pnl_pct": float(last / cost - 1) * 100})
        persona = BY_TAG.get(m.persona_tag or "") if m.is_official else None
        return {
            "portfolio_id": pid, "name": m.name, "group": self._group(m), "controller_type": m.controller_type,
            "brief": m.brief, "persona_tag": m.persona_tag, "is_official": m.is_official,
            "glyph": persona.glyph if persona else (m.name[:1] or "?"), "color": persona.color if persona else "#8CC4FF",
            "rank": me["rank"], "cum_return": me["cum_return"], "excess_return": me["excess_return"],
            "max_drawdown": mdd, "win_rate": None if not win or not win[1] else win[0] / win[1],
            "closed_trades": win[1] if win else 0, "queue_success": None if not queued else got / queued,
            "queue_count": queued, "cash": str(money(cash)), "equity": str(money(eq)),
            "badges": [{"code": b.code, "name": b.name, "desc": b.desc, "glyph": b.glyph,
                        "count": counts.get(b.code, 0)} for b in BADGES],
            "positions": positions,
            "roast_line": (roast[0]["data"].get("lines", {}).get(pid) if roast else None),
            "driver": self.driver(pid),
        }

    async def timeline(self, pid: str) -> list[dict]:
        items = await self.store.timeline(pid)
        for it in items:
            it["symbol_name"] = self.names.get(it["symbol"], it["symbol"])
            live = [self.engine.order(o["order_id"]) for o in it["orders"]]
            for o, lo in zip(it["orders"], live):
                if lo is not None:
                    o.update(status=lo.status.value, filled_qty=lo.filled_qty, queue_ahead=lo.queue_ahead,
                             queue_progress_pct=lo.queue_progress_pct,
                             avg_fill_price=None if lo.avg_fill_price is None else str(lo.avg_fill_price),
                             fees_paid=str(lo.fees_paid))
        return items

    async def my_orders(self, pid: str) -> list[dict]:
        rows = await self.store.recent_orders(pid, 30)
        out = []
        for r in rows:
            live = self.engine.order(str(r["id"]))
            view = live.public_view() if live else _row_view(r)
            out.append(self._enrich(view) | {"created_at": r["created_at"].isoformat()})
        return out


def _row_view(r: asyncpg.Record) -> dict:
    return {"order_id": str(r["id"]), "portfolio_id": str(r["portfolio_id"]), "symbol": r["symbol"],
            "side": r["side"], "order_type": r["order_type"], "price": None if r["price"] is None else str(r["price"]),
            "qty": r["qty"], "filled_qty": r["filled_qty"],
            "avg_fill_price": None if r["avg_fill_price"] is None else str(r["avg_fill_price"]),
            "status": r["status"], "queue_initial": r["queue_initial"], "queue_ahead": r["queue_ahead"],
            "queue_progress_pct": None, "debate_id": None if r["debate_id"] is None else str(r["debate_id"]),
            "thesis": r["thesis"]}
