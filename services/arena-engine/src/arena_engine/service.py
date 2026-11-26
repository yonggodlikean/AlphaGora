"""ArenaService: the single writer around the matching engine.

Every command (order, cancel), market snapshot and day transition runs under one asyncio lock:
engine mutation -> one Postgres transaction -> publish to SSE subscribers. Reads (leaderboard, state,
profile) come from engine memory plus a few queries.
"""
from __future__ import annotations

import asyncio
import time as time_mod
import hashlib
import json
import hmac
import logging
import math
import re
import secrets
import shutil
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
from .domain.errors import ArenaError, MarketDataError, OrderRejected, PublicError
from .domain.events import ArenaEvent
from .domain.models import Fill, Order, OrderRequest, Position
from . import corporate
from .seasons import month_key, season_for
from .house_bots import HouseBots, Intent, MarketView, is_stock as is_stock_symbol
from .market.history import History, features as hist_features
from .market.calendar import CST, TradingCalendar
from .market.demo import DemoFeed, EndOfDay
from .market.feed import RedisStreamFeed, ReplayFeed, snapshot_from_dict, snapshot_to_dict
from .market.snapshot import L1Snapshot
from .matching.rate_limit import SlidingWindowLimiter
from .moderation import ContentFilter
from .alerts import Alerter
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
HIDDEN_TEXT = "（该内容已被管理员隐藏）"


def _dec(x: float | Decimal) -> Decimal:
    return Decimal(str(x)).quantize(Q8)


SEASON_NS = uuid.UUID("2b5d0c1e-4f7a-4c39-9d0e-7a1c3e5b9f20")


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
        self.daily: History | None = None          # daily bars for the strategies (gateway, or synthetic in demo)
        self._history_built: str | None = None
        self.held_days: dict[tuple[str, str], int] = {}
        self.minute_closes: dict[str, list[Decimal]] = {}
        self._minute_of: dict[str, tuple[int, int]] = {}
        self.recent_fills: deque = deque(maxlen=1000)
        self._conflicts_seen: dict[tuple, datetime] = {}
        self._first_trade_done: set[str] = set()
        self._closed_dates: set[date] = set()
        self._late_dates: set[date] = set()
        self._last_bot_minute: tuple | None = None
        self._token_cache: dict[bytes, str] = {}
        self._summaries: dict[str, str] = {}
        self._pending_restore: list = []
        self.filter = ContentFilter(Path(settings.blocklist) if settings.blocklist else None)
        self.status: dict[str, tuple[str, str | None]] = {}
        self.hidden: set[str] = set()
        self.cheers: dict[str, int] = {}
        self._visitors: set[str] = set()
        self._visitors_day: date | None = None
        self._visitors_saved = 0
        self._watching: dict[str, float] = {}       # visitor hash -> monotonic time of its last status poll
        self._cheer_ip: dict[str, tuple[date, int]] = {}
        self._order_day: dict[str, tuple[date, int]] = {}
        self._limiters: dict[int, SlidingWindowLimiter] = {}
        self.alerter = Alerter(settings.alert_webhook, settings.alert_format, timedelta(minutes=settings.alert_repeat_min))
        self._redis = None   # the quote stream's client (live mode), also used to read the gateway heartbeat
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
        # Live arenas rotate seasons by the calendar (seasons.py); demo, replay and a pinned slug never rotate.
        self.auto_season = not self.settings.season_slug and self.settings.market_feed == "redis"
        if self.auto_season:
            row = await self.store.open_auto_season()
            if row is None:
                spec = season_for(today)
                row = await self.store.ensure_season(spec.slug, spec.start, spec.end, spec.name)
            else:      # seeds/renames the official teams
                row = await self.store.ensure_season(row["slug"], row["start_date"], row["end_date"])
        else:
            row = await self.store.ensure_season(self.settings.resolved_season_slug(), today,
                                                 today + timedelta(days=120))
        self.names = await self.store.symbol_names()
        await self._enter_season(row)
        await self.load_marks()
        if self.settings.auto_start_feed:
            await self.start_feed()
            await self.refresh_history()
            self._tasks.append(asyncio.create_task(self._run_housekeeping(), name="housekeeping"))

    async def _enter_season(self, row: dict) -> None:
        """Everything that belongs to one season: accounts, tokens, team drivers, statuses. Also used at rollover,
        on a fresh engine."""
        self.season = dict(row)
        self.season_id = self.season["id"]
        if self.meta:
            self.engine = AShareMatchingEngine(self.calendar)
            self.meta, self.balances, self.history = {}, {}, {}
            self._token_cache, self._first_trade_done, self.held_days = {}, set(), {}
        await self._load_portfolios()
        # Open orders from a previous run are restored into their own trade date once it reopens (first snapshot),
        # or expired if the market has moved on to a new day.
        self._pending_restore = list(await self.store.load_open_orders(list(self.meta)))
        if self._pending_restore:
            log.info("%d open orders waiting to be restored", len(self._pending_restore))
        external = self.settings.resolved_external_teams()
        teams = {p.id: p.persona_tag for p in self.meta.values()
                 if p.is_official and p.persona_tag in BY_TAG and p.persona_tag not in external}
        if external:
            log.info("officially driven from outside (no rule bot): %s", ", ".join(external))
        for tag, raw in self.settings.team_tokens:
            meta = await self.store.official_portfolio(self.season_id, tag)
            if meta is None:
                log.warning("ARENA_TEAM_TOKENS: no official team %r", tag)
            elif await self.store.assign_token(meta.id, raw[:8], self.hash_token(raw)):
                log.info("pre-shared token now drives %s (season %s)", meta.name, self.season["slug"])
        self.last_seen = await self.store.last_seen(self.season_id)
        self.status = await self.store.statuses(self.season_id)
        self.hidden = await self.store.hidden_refs()
        self.cheers = await self.store.cheer_totals(list(self.meta))
        self.bots = HouseBots(teams, self.settings.resolved_benchmark(), self.settings.resolved_dividend_symbols())
        for pid in self.meta:
            if await self.store.has_badge(pid, "first_trade"):
                self._first_trade_done.add(pid)
        await self.refresh_held_days()
        log.info("season %s (%s → %s): %d teams", self.season["name"], self.season["start_date"],
                 self.season["end_date"], len(self.meta))

    # ================================================================== seasons
    async def _season_boundaries(self, d: date) -> None:
        """Called before a new trading day opens: award last month's best, and settle + roll the season over
        when `d` is past its end."""
        await self._award_month(d)
        if self.auto_season and d > self.season["end_date"]:
            await self._rollover(d)

    async def _award_month(self, d: date) -> None:
        last = max((h["trade_date"] for hs in self.history.values() for h in hs), default=None)
        if last is None or (last.year, last.month) == (d.year, d.month):
            return
        month = month_key(last)
        ref = uuid.uuid5(SEASON_NS, f"month:{self.season_id}:{month}")
        if await self.store.badge_ref_exists("month_best", ref):
            return
        best: list[tuple[float, str]] = []
        for pid, m in self.meta.items():
            if m.is_benchmark or not self.is_active(pid):
                continue
            r = self._period_return(pid, date(last.year, last.month, 1), last)
            if r is not None:
                best.append((r, pid))
        if not best:
            return
        top = max(best)[0]
        winners = [pid for r, pid in best if r == top and r > 0]
        if winners:
            await self._handle([], [], badges=[(pid, "month_best", str(ref)) for pid in winners])
            log.info("month %s best: %s (%+.2f%%)", month, ", ".join(self._name(p) for p in winners), top * 100)

    def _period_return(self, pid: str, start: date, end: date | None, live: Decimal | None = None) -> float | None:
        """Return from the last snapshot before `start` (or the initial cash) to the snapshot on/before `end`,
        or to `live` equity when given."""
        hist = self.history.get(pid, [])
        initial = self.engine.accounts[pid].initial_cash if pid in self.engine.accounts else Decimal(self.season["initial_cash"])
        before = [h for h in hist if h["trade_date"] < start]
        base = Decimal(before[-1]["total_equity"]) if before else initial
        if live is not None:
            final = live
        else:
            upto = [h for h in hist if h["trade_date"] <= end] if end else hist
            if not upto or upto[-1]["trade_date"] < start:
                return None
            final = Decimal(upto[-1]["total_equity"])
        return float(final / base - 1) if base > 0 else None

    async def _rollover(self, d: date) -> None:
        old = self.season
        snaps = await self.store.last_snapshots(old["id"])
        trades = await self.store.season_trade_counts(old["id"])
        initial = Decimal(old["initial_cash"])
        bench = next((p for p, m in self.meta.items() if m.is_benchmark), None)
        bench_cum = float(snaps[bench]["cum_return"]) if bench in snaps else 0.0
        rows = []
        for pid, m in self.meta.items():
            if self.status.get(pid, ("active",))[0] == "banned":
                continue
            s = snaps.get(pid)
            cum = float(s["cum_return"]) if s else 0.0
            rows.append({"portfolio_id": pid, "cum_return": cum, "excess_return": cum - bench_cum,
                         "final_equity": s["total_equity"] if s else initial, "trades": trades.get(pid, 0),
                         "benchmark": m.is_benchmark, "rank": None})
        ranked = sorted((r for r in rows if not r["benchmark"]), key=lambda r: -r["cum_return"])
        for i, r in enumerate(ranked, 1):
            r["rank"] = i
        ref = uuid.uuid5(SEASON_NS, f"season:{old['id']}")
        badges = []
        for r in ranked:
            if r["rank"] == 1:
                badges.append((r["portfolio_id"], "season_champion", ref))
            if r["rank"] <= 3:
                badges.append((r["portfolio_id"], "season_top3", ref))
            if r["trades"] > 0 and r["cum_return"] > bench_cum:
                badges.append((r["portfolio_id"], "season_beat_index", ref))
        if await self.store.settle_season(old["id"], rows, badges):
            champ = ranked[0] if ranked else None
            await self._emit("season.settled", None, {
                "season": old["slug"], "name": old["name"],
                "champion": None if champ is None else {"portfolio_id": champ["portfolio_id"],
                                                        "name": self._name(champ["portfolio_id"]),
                                                        "cum_return": champ["cum_return"]},
                "benchmark_return": bench_cum, "teams": len(ranked)})
        spec = season_for(d)
        new = await self.store.ensure_season(spec.slug, spec.start, spec.end, spec.name)
        moved = await self.store.carry_over(old["id"], new["id"])
        await self._enter_season(new)
        await self._emit("season.started", None, {"season": new["slug"], "name": new["name"],
                                                   "start": str(new["start_date"]), "end": str(new["end_date"]),
                                                   "carried": len(moved)})
        log.warning("season %s settled; %s started with %d carried-over teams", old["slug"], new["slug"], len(moved))

    async def season_report(self, slug: str) -> dict | None:
        row = await self.store.season_by_slug(slug)
        if row is None:
            return None
        standings = await self.store.season_standings(row["id"])
        for s in standings:
            s["portfolio_id"] = str(s.pop("portfolio_id"))
            s.pop("season_id", None)
            for k in ("cum_return", "excess_return"):
                s[k] = float(s[k])
            s["final_equity"] = str(s["final_equity"])
        return {"slug": row["slug"], "name": row["name"], "start": str(row["start_date"]),
                "end": str(row["end_date"]), "status": row["status"], "standings": standings,
                "current": row["id"] == self.season_id}

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
            # The feed blocks up to 5s per XREAD; the socket timeout must comfortably exceed that.
            client = aioredis.from_url(s.redis_url, socket_timeout=30, socket_connect_timeout=10,
                                       health_check_interval=30)
            self.feed = RedisStreamFeed(client, stream=s.market_stream, block_ms=5000)
            self._redis = client
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
        phase = self.calendar.phase(now)
        if phase == "trading" and not trading:
            phase = "waiting"            # in session by the clock, but no quotes have opened the day yet
        return {"trading": trading, "phase": phase, "day_open": self.engine.day_open,
                "trade_date": None if self.engine.trade_date is None else self.engine.trade_date.isoformat(),
                "clock": now.astimezone(CST).strftime("%H:%M:%S"), "now": now.isoformat(),
                "simulated": self.settings.is_simulated_clock, "feed": self.settings.market_feed,
                "online": self.watching(), "streams": self.bus.subscribers, "visitors_today": len(self._visitors)}

    def watching(self, window_s: float = 60.0) -> int:
        """Browsers that polled the status in the last minute. Every page polls every 5s while its tab is visible,
        so this counts people actually looking, once per browser, on any page."""
        cutoff = time_mod.monotonic() - window_s
        self._watching = {v: t for v, t in self._watching.items() if t >= cutoff}
        return len(self._watching)

    # ================================================================== market data
    async def on_snapshot(self, snap: L1Snapshot) -> None:
        async with self.lock:
            d = self.calendar.trade_date_of(snap.ts)
            if d in self._closed_dates or (self.engine.trade_date and d < self.engine.trade_date):
                return
            if (not self.settings.is_simulated_clock and (self.engine.trade_date != d or not self.engine.day_open)
                    and datetime.now(CST) > datetime.combine(d, time(15, 0, 5), CST)):
                # Live mode: never open a trading day after its close (late, replayed or test quotes would
                # otherwise create an empty "day" that opens and closes at once).
                if d not in self._late_dates:
                    self._late_dates.add(d)
                    log.warning("ignoring quotes for %s: they arrived after that day's close", d)
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
        if snap.call_auction:
            return                        # indicative prices, not trades
        t = snap.ts.astimezone(CST).time()
        if sym not in self.day_open_px and t >= time(9, 25):
            self.day_open_px[sym] = snap.last_price           # the opening auction price
        minute = (t.hour, t.minute)
        if self._minute_of.get(sym) != minute:
            self._minute_of[sym] = minute
            self.minute_closes.setdefault(sym, []).append(snap.last_price)
        else:
            self.minute_closes[sym][-1] = snap.last_price

    async def _open_day_locked(self, d: date, now: datetime) -> None:
        if not await self.store.has_market_open(self.season_id, d):
            await self._season_boundaries(d)
        resumed = await self.store.has_market_open(self.season_id, d)
        if resumed:
            events = self.engine.resume_trading_day(d, now)
        else:
            events = self.engine.begin_trading_day(d, now)
            await self._corporate_actions(d)
        await self._restore_open_orders(d if resumed else None)
        self.day_open_px.clear()
        self.minute_closes.clear()
        self._minute_of.clear()
        self._last_bot_minute = None
        await self.refresh_held_days(d)
        positions = [(pid, p) for pid, a in self.engine.accounts.items() for p in a.positions.values()]
        await self._handle([], events, extra_positions=positions)
        log.info("market open %s", d)

    async def _restore_open_orders(self, trade_date: date | None) -> None:
        """Put back the open orders of `trade_date` (restart within a session); expire anything older."""
        pending, self._pending_restore = self._pending_restore, []
        restored, expired = 0, []
        for r in pending:
            if trade_date is None or r["trade_date"] != trade_date or r["order_type"] != "limit":
                expired.append(str(r["id"]))
                continue
            order = Order(
                id=str(r["id"]), seq=0, portfolio_id=str(r["portfolio_id"]), symbol=r["symbol"], side=Side(r["side"]),
                order_type=OrderType.LIMIT, qty=r["qty"], price=r["price"], thesis=r["thesis"],
                debate_id=None if r["debate_id"] is None else str(r["debate_id"]),
                idempotency_key=r["idempotency_key"], request_hash=r["request_hash"], trade_date=r["trade_date"],
                created_at=r["created_at"], status=OrderStatus(r["status"]), filled_qty=r["filled_qty"],
                filled_notional=r["filled_notional"], commission_charged=r["commission"], fees_paid=r["fees_paid"],
                queue_initial=r["queue_initial"], queue_ahead=r["queue_ahead"],
            )
            try:
                self.engine.restore_order(order)
                restored += 1
            except (OrderRejected, ValueError) as exc:
                log.warning("could not restore order %s: %s", order.id, exc)
                expired.append(order.id)
        await self.store.expire_orders(expired, "engine_restart")
        if restored or expired:
            log.info("restart: restored %d open orders, expired %d", restored, len(expired))

    async def _run_housekeeping(self) -> None:
        while True:
            await asyncio.sleep(30)
            try:
                await self.refresh_controls()
                await self.check_health()
                await self.refresh_history()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("housekeeping failed")

    async def refresh_controls(self) -> None:
        """Picks up admin actions made with `python -m arena_engine.admin` (status, hidden content) and persists
        visitor counts. A suspended or banned account loses its open orders immediately."""
        statuses = await self.store.statuses(self.season_id)
        newly_blocked = [pid for pid, (st, _) in statuses.items()
                         if st != "active" and self.status.get(pid, ("active", None))[0] == "active"]
        self.status = statuses
        self.hidden = await self.store.hidden_refs()
        self._token_cache = {}    # re-checked against the DB on next use, so `admin reset-token` bites within 30s
        if newly_blocked:
            async with self.lock:
                events = []
                for pid in newly_blocked:
                    for o in self.engine.open_orders():
                        if o.portfolio_id == pid:
                            e = self.engine.force_cancel(o.id, self.now(), "account_" + statuses[pid][0])
                            if e:
                                events.append(e)
                    self._token_cache = {h: p for h, p in self._token_cache.items() if p != pid}
                    log.warning("%s is now %s (%s)", self._name(pid), statuses[pid][0], statuses[pid][1])
                if events:
                    await self._handle([], events, cancel_reasons={e.payload["order_id"]: e.payload.get("reason") for e in events})
        if self._visitors_day and len(self._visitors) != self._visitors_saved:
            await self.store.save_visits(self._visitors_day, len(self._visitors))
            self._visitors_saved = len(self._visitors)

    async def check_health(self, now: datetime | None = None) -> None:
        """Raises / clears alerts. Gateway liveness and the trading-session flag come from the gateway itself
        (it knows the exchange holidays), so nothing fires on a holiday or at night."""
        now = now or datetime.now(CST)
        if self.settings.market_feed == "redis" and self._redis is not None:
            try:
                raw = await self._redis.hgetall("md:status")
                status = {(k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
                          for k, v in raw.items()}
            except Exception as exc:
                status = {}
                log.warning("cannot read md:status: %s", exc)
            hb = _parse_ts(status.get("heartbeat"))
            down = hb is None or (now - hb).total_seconds() > 120
            await self.alerter.update(
                "gateway_down", down, "行情网关无响应",
                "market-gateway 超过 2 分钟没有心跳。检查：docker compose logs --tail 50 market-gateway", "critical")
            last = _parse_ts(status.get("last_publish"))
            # Only continuous trading guarantees fresh quotes every few seconds: in the call auctions, 09:25-09:30
            # and the first minute of a session, a quiet book is normal.
            settled_in = self.calendar.is_continuous_trading(now - timedelta(seconds=90))
            stalled = (not down and status.get("session") == "1" and settled_in
                       and self.calendar.is_continuous_trading(now)
                       and (last is None or (now - last).total_seconds() > 90))
            await self.alerter.update(
                "feed_stalled", stalled, "交易时段行情断流",
                f"网关在线，但超过 90 秒没有新行情（最后一次：{last.strftime('%H:%M:%S') if last else '今天还没有'}）。"
                "可能是通达信服务器不可达。", "critical")
        for pid, m in self.meta.items():
            if m.is_official and m.persona_tag in self.settings.resolved_external_teams():
                d = self.driver(pid)
                seen = d.get("last_seen") if d else None
                await self.alerter.update(
                    f"team_offline:{pid}", bool(d) and not d["online"], f"{m.name} 掉线",
                    f"驱动程序超过 {self.settings.online_window_s // 60} 分钟没有心跳"
                    f"（最后在线：{seen[11:19] if seen else '从未上线'}）。检查：docker compose logs --tail 50 pup-team")
        usage = shutil.disk_usage("/")
        pct = usage.used * 100 // usage.total
        await self.alerter.update(
            "disk_high", pct >= self.settings.alert_disk_pct, f"磁盘使用率 {pct}%",
            f"剩余 {usage.free // 2**30} GB。行情录像在 data/tape（保留 TAPE_KEEP_DAYS 天），备份在 backups/。")

    def is_active(self, pid: str) -> bool:
        return self.status.get(pid, ("active", None))[0] == "active"

    def _blocked(self, text: str | None) -> None:
        reason = self.filter.check(text)
        if reason:
            raise PublicError("CONTENT_BLOCKED", reason, 422)

    def _limiter(self, n: int) -> SlidingWindowLimiter:
        if n not in self._limiters:
            self._limiters[n] = SlidingWindowLimiter(n, timedelta(minutes=1))
        return self._limiters[n]

    def _check_order_rate(self, pid: str) -> None:
        m = self.meta[pid]
        if m.is_official:
            return
        human = m.controller_type in ("human", "hybrid")
        per_min = self.settings.orders_per_min_human if human else self.settings.orders_per_min_ai
        now = datetime.now(CST)
        lim = self._limiter(per_min)
        if not lim.allowed((pid,), now):
            raise OrderRejected(RejectCode.RATE_LIMITED, f"下单太快了：每分钟最多 {per_min} 单")
        day, n = self._order_day.get(pid, (now.date(), 0))
        if day != now.date():
            day, n = now.date(), 0
        if human and n >= self.settings.orders_per_day_human:
            raise OrderRejected(RejectCode.RATE_LIMITED, f"今天已经下了 {n} 单，明天再来")
        lim.record((pid,), now)
        self._order_day[pid] = (day, n + 1)

    def track_visit(self, visitor: str) -> None:
        today = datetime.now(CST).date()
        if self._visitors_day != today:
            self._visitors_day, self._visitors, self._visitors_saved = today, set(), 0
        key = hashlib.sha256((self.settings.token_pepper + "|v|" + visitor).encode()).hexdigest()[:20]
        self._visitors.add(key)
        self._watching[key] = time_mod.monotonic()

    async def cheer(self, pid: str, visitor: str, ip: str) -> dict:
        if pid not in self.meta or not self.is_active(pid):
            raise PublicError("NOT_FOUND", "没有这支队伍", 404)
        today = datetime.now(CST).date()
        day, n = self._cheer_ip.get(ip, (today, 0))
        if day != today:
            day, n = today, 0
        if n >= 60:
            raise PublicError("RATE_LIMITED", "今天加油次数太多了，明天再来", 429)
        key = hashlib.sha256((self.settings.token_pepper + "|c|" + visitor).encode()).hexdigest()[:24]
        added = await self.store.add_cheer(pid, today, key)
        if added:
            self._cheer_ip[ip] = (day, n + 1)
            self.cheers[pid] = self.cheers.get(pid, 0) + 1
        return {"portfolio_id": pid, "cheers": self.cheers.get(pid, 0), "cheered_today": True, "new": added}

    def seats(self) -> dict:
        human = sum(1 for p, m in self.meta.items()
                    if not m.is_official and m.controller_type in ("human", "hybrid") and self.status.get(p, ("active",))[0] != "banned")
        ai = sum(1 for p, m in self.meta.items()
                 if not m.is_official and m.controller_type not in ("human", "hybrid") and self.status.get(p, ("active",))[0] != "banned")
        return {"invite_required": self.settings.require_invite,
                "human": {"taken": human, "total": self.settings.seats_human},
                "ai": {"taken": ai, "total": self.settings.seats_ai}}

    async def load_marks(self) -> int:
        """After a restart with no day open, value positions and list symbols at the last close."""
        if self.engine.day_open:
            return 0
        snaps = []
        for d in await self.store.load_marks():
            try:
                snaps.append(snapshot_from_dict(d))
            except Exception as exc:      # one bad row must not stop the engine
                log.warning("skipping stored close for %s: %s", d.get("symbol"), exc)
        self.engine.prime_books(snaps)
        if snaps:
            log.info("loaded %d closing prices from %s", len(snaps), max(s.ts for s in snaps).date())
        return len(snaps)

    # ================================================================== daily history
    async def refresh_history(self) -> None:
        """Live: reload the gateway's daily history when it has built a new one (checked every 30s)."""
        if self.settings.market_feed != "redis" or self._redis is None:
            return
        try:
            raw = await self._redis.get("md:hist:meta")
            if not raw:
                return
            built = json.loads(raw).get("built_at")
            if built == self._history_built:
                return
            h = await History.from_redis(self._redis)
            self.daily, self._history_built = h, built
            log.info("daily history %s: %d symbols", h.built, len(h.bars))
        except Exception:
            log.exception("could not load daily history")

    def _ensure_synthetic_history(self) -> None:
        books = self.engine.books
        if not books or (self.daily is not None and all(s in self.daily.bars for s in books)):
            return
        missing = {s: float(b.prev_close) for s, b in books.items()
                   if self.daily is None or s not in self.daily.bars}
        h = History.synthetic(missing, self.engine.trade_date, dividend=self.settings.resolved_dividend_symbols())
        if self.daily is None:
            self.daily = h
        else:
            self.daily.bars.update(h.bars)
            for k, v in h.members.items():
                self.daily.members.setdefault(k, [])
                self.daily.members[k] = sorted(set(self.daily.members[k]) | set(v))

    def _append_history_day(self, d: date) -> None:
        if self.daily is None:
            return
        for sym, b in self.engine.books.items():
            closes = [float(x) for x in self.minute_closes.get(sym, [])] or [float(b.last_price)]
            self.daily.append_day(d, sym, float(b.last_price), max(closes), min(closes),
                                    float(b.cumulative_volume), float(b.cumulative_volume * b.last_price),
                                    b.limit_up_sealed)

    async def _corporate_actions(self, d: date) -> None:
        """Ex-date: pay dividends and bonus shares to positions held at the previous close (before any trading)."""
        if self.settings.market_feed == "redis" and self._redis is not None:
            await self.refresh_history()            # make sure this morning's ex-rights list is loaded
        actions = corporate.plan(self.engine.accounts, self.daily, d)
        if not actions:
            return
        after = {}
        for a in actions:
            p = self.engine.accounts[a.portfolio_id].positions[a.symbol]
            after[(a.portfolio_id, a.symbol)] = (p.qty + a.bonus, p.frozen_qty + a.bonus, p.cost_basis)
        applied = await self.store.apply_corporate_actions(actions, after, self.balances)
        for a in applied:
            corporate.apply_to_engine(self.engine.accounts, a)
            await self._emit("corporate.action", a.symbol, {
                "portfolio_id": a.portfolio_id, "portfolio": self._name(a.portfolio_id), "symbol": a.symbol,
                "name": self.names.get(a.symbol, a.symbol), "ex_date": d.isoformat(), "held": a.held,
                "plan": a.describe(), "cash": str(a.cash), "bonus_shares": a.bonus})
        if applied:
            log.info("corporate actions on %s: %d applied", d, len(applied))

    async def refresh_held_days(self, today: date | None = None) -> None:
        """Trading days each house position has been held (bought yesterday = 1)."""
        today = today or self.engine.trade_date or datetime.now(CST).date()
        held = {}
        for (pid, sym), entry in (await self.store.entry_dates(list(self.meta))).items():
            n, d = 0, entry
            while d < today and n < 60:
                d += timedelta(days=1)
                n += self.calendar.is_trading_day(d)
            held[(pid, sym)] = n
        self.held_days = held

    def symbol_features(self, symbols: list[str]) -> dict[str, dict]:
        out = {}
        if self.daily is None:
            return out
        day = self.engine.trade_date or datetime.now(CST).date() + timedelta(days=1)
        for sym in symbols:
            bars, b = self.daily.bars.get(sym), self.engine.book(sym)
            if bars is None:
                continue
            hist = bars.before(day) if self.engine.day_open else bars
            live = float(b.last_price) if b and self.engine.day_open else (hist.close[-1] if hist.close else 0.0)
            if not self.engine.day_open and hist.close:     # after the close, the last bar is today
                hist = type(hist)(hist.dates[:-1], hist.close[:-1], hist.high[:-1], hist.low[:-1],
                                  hist.volume[:-1], hist.amount[:-1], hist.limit_up[:-1], hist.div_ttm,
                                  hist.float_shares, hist.events)
            if hist.close and live > 0:
                out[sym] = hist_features(hist, live)
        return out

    def breadth(self) -> dict:
        """Market-wide mood from the arena's own books, plus the index ETFs as proxies for the indices."""
        up = down = flat = lu = ld = 0
        for sym, b in self.engine.books.items():
            if not is_stock_symbol(sym) or b.suspended:
                continue
            c = b.last_price - b.prev_close
            up += c > 0
            down += c < 0
            flat += c == 0
            lu += b.limit_up_sealed
            ld += b.limit_down_sealed
        proxies = {}
        for name, sym in (("沪深300", "510300.SH"), ("上证50", "510050.SH"), ("中证1000", "512100.SH")):
            b = self.engine.book(sym)
            if b is not None:
                proxies[name] = {"symbol": sym, "chg_pct": round(float(b.last_price / b.prev_close - 1) * 100, 2)}
        return {"up": up, "down": down, "flat": flat, "limit_up": lu, "limit_down": ld, "indices": proxies,
                "market": self.market_status()}

    async def close_day(self) -> None:
        async with self.lock:
            if self.engine.day_open:
                await self._close_day_locked()

    async def _close_day_locked(self) -> None:
        d = self.engine.trade_date
        now = self.now() if not self.settings.is_simulated_clock else datetime.combine(d, time(15, 0, 1), CST)
        self._append_history_day(d)
        events = self.engine.end_trading_day(now)
        try:
            await self.store.save_marks(d, [(sym, json.dumps(snapshot_to_dict(b), ensure_ascii=False))
                                            for sym, b in self.engine.books.items()])
        except Exception:                 # nice to have: never block the close on it
            log.exception("could not save closing prices")
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
        for (ts, sym, side, pid, qty, th, oid) in reversed(self.recent_fills):
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
            b_oid, s_oid = (f.order_id, oid) if f.side is Side.BUY else (oid, f.order_id)
            out.append((ev.CONFLICT_DETECTED, sym, {
                "symbol": sym, "symbol_name": self.names.get(sym, sym), "ts": now.isoformat(),
                "buyer_id": buyer, "buyer": self._name(buyer), "buy_qty": b_qty, "buy_thesis": b_th,
                "seller_id": seller, "seller": self._name(seller), "sell_qty": s_qty, "sell_thesis": s_th,
                "buy_order_id": b_oid, "sell_order_id": s_oid,
            }))
            break
        self.recent_fills.append((now, f.symbol, f.side, f.portfolio_id, f.qty, thesis, f.order_id))
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
        if not self.is_active(portfolio_id):
            raise PublicError("ACCOUNT_SUSPENDED", "这个账号已被暂停交易：" + (self.status[portfolio_id][1] or "违反擂台规则"), 403)
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
            self._blocked(req.thesis)
            self._check_order_rate(portfolio_id)
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
        if not self.meta[portfolio_id].is_official:
            if not self.is_active(portfolio_id):
                raise PublicError("ACCOUNT_SUSPENDED", "这个账号已被暂停交易", 403)
            for r in body["rounds"]:
                self._blocked(r.get("argument"))
                self._blocked(r.get("role"))
            fd = body.get("final_decision") or {}
            self._blocked(str(fd.get("title") or "") + " " + str(fd.get("summary") or ""))
        res = await self.store.create_debate(portfolio_id, body["symbol"], body["rounds"], body["final_decision"],
                                             body.get("execution_trace"), body.get("execution_trace_hash"),
                                             created_at=self.now())       # market time (the simulated clock in demos)
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
        if self.settings.market_feed != "redis":
            self._ensure_synthetic_history()
        mv = MarketView(self.engine.trade_date, self.market_now, dict(self.engine.books), dict(self.day_open_px),
                        {k: list(v) for k, v in self.minute_closes.items()}, self.names, self.daily,
                        self.held_days)
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
        if pid not in self.meta or self.status.get(pid, ("active",))[0] == "banned":
            return None
        return pid

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
                   brief: str | None, invite_code: str | None = None, client_ip: str | None = None) -> dict:
        for text in (display_name, brief, persona_tag):
            self._blocked(text)
        raw = "agk_" + secrets.token_urlsafe(24)
        human = controller_type in ("human", "hybrid")
        fingerprint = None if not client_ip else hashlib.sha256(
            (self.settings.token_pepper + "|ip|" + client_ip).encode()).hexdigest()[:24]
        try:
            meta = await self.store.create_entrant(
                self.season_id, display_name, controller_type, persona_tag, brief, raw[:8], self.hash_token(raw),
                invite_code=invite_code, require_invite=self.settings.require_invite,
                seats=self.settings.seats_human if human else self.settings.seats_ai, fingerprint=fingerprint)
        except asyncpg.UniqueViolationError:
            raise PublicError("NAME_TAKEN", "这个名字已经有人用了", 409) from None
        self.status[meta.id] = ("active", None)
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

    def leaderboard(self, group: str = "all", period: str = "season") -> list[dict]:
        """period: season (cumulative since the season started) or month (since the last close before the 1st)."""
        bench = self.benchmark_return()
        today = self.engine.trade_date
        month_start = (today or datetime.now(CST).date()).replace(day=1)
        rows = []
        for pid, m in self.meta.items():
            if not self.is_active(pid):
                continue
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
            month = self._period_return(pid, month_start, None, live=eq)
            rows.append({"portfolio_id": pid, "name": m.name, "group": g, "persona_tag": m.persona_tag,
                         "cum_return": cum, "excess_return": cum - bench, "day_return": float(eq / prev - 1),
                         "month_return": month,
                         "driver": self.driver(pid), "cheers": self.cheers.get(pid, 0),
                         "equity": str(money(eq)), "spark": [0.0] + spark if len(spark) == 1 else spark})
        key = "month_return" if period == "month" else "cum_return"
        rows.sort(key=lambda r: r[key] if r[key] is not None else -1e9, reverse=True)
        place = 0
        for r in rows:
            if r["group"] == "benchmark":
                r["rank"] = None
            else:
                place += 1
                r["rank"] = place
        return rows

    def _symbol_row(self, sym: str, b: L1Snapshot) -> dict:
        return {"symbol": sym, "name": self.names.get(sym, sym), "last": str(b.last_price),
                "prev_close": str(b.prev_close), "chg_pct": float(b.last_price / b.prev_close - 1) * 100,
                "upper_limit": str(b.upper_limit), "lower_limit": str(b.lower_limit),
                "limit_up_sealed": b.limit_up_sealed, "limit_down_sealed": b.limit_down_sealed,
                "suspended": b.suspended, "call_auction": b.call_auction}

    def symbols(self, q: str = "", sort: str = "hot", limit: int = 30, offset: int = 0,
                only: list[str] | None = None) -> dict:
        """The tradable universe, searched and sorted server-side (it can be ~1,300 symbols).

        sort: hot (sealed limit-up first, then biggest gainers), gainers, losers, movers (|change|), code,
        trend (above the 20-day line, strongest 20-day return first; needs daily history)."""
        books = self.engine.books
        if only:
            rows = [self._symbol_row(s, books[s]) for s in only if s in books]
            return {"total": len(rows), "symbols": rows}
        needle = q.strip().upper()
        rows = []
        for sym, b in books.items():
            if needle:
                name = self.names.get(sym, "")
                if needle not in sym and needle not in name.upper():
                    continue
            rows.append(self._symbol_row(sym, b))
        if sort == "trend":
            feats = self.symbol_features([r["symbol"] for r in rows if is_stock_symbol(r["symbol"])])
            rows = [r | {"ret20": feats[r["symbol"]]["ret20"]} for r in rows
                    if (f := feats.get(r["symbol"])) and f["ret20"] is not None and f["ma20"]
                    and float(r["last"]) > f["ma20"]]
            rows.sort(key=lambda r: -r["ret20"])
            return {"total": len(rows), "symbols": rows[offset:offset + limit]}
        keys = {
            "hot": lambda r: (not r["limit_up_sealed"], -r["chg_pct"]),
            "gainers": lambda r: -r["chg_pct"],
            "losers": lambda r: r["chg_pct"],
            "movers": lambda r: -abs(r["chg_pct"]),
            "code": lambda r: r["symbol"],
        }
        rows.sort(key=keys.get(sort, keys["hot"]))
        return {"total": len(rows), "sealed_up": sum(1 for r in rows if r["limit_up_sealed"]),
                "symbols": rows[offset:offset + limit]}

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
                        "limit_down_sealed": b.limit_down_sealed, "suspended": b.suspended,
                        "call_auction": b.call_auction, "auction": b.auction})
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
        conflicts = []
        for c in await self.store.latest_events(self.season_id, ev.CONFLICT_DETECTED, 12):
            d = c["data"]
            if not (self.is_active(d.get("buyer_id", "")) and self.is_active(d.get("seller_id", ""))):
                continue
            if f"order:{d.get('buy_order_id')}" in self.hidden:
                d["buy_thesis"] = HIDDEN_TEXT
            if f"order:{d.get('sell_order_id')}" in self.hidden:
                d["sell_thesis"] = HIDDEN_TEXT
            conflicts.append(d)
        roast = await self.store.latest_events(self.season_id, "roast.daily", 1)
        today = await self.store.conflicts_on(self.season_id, self.engine.trade_date or datetime.now(CST).date())
        return {"conflicts": conflicts[:6], "conflicts_today": len(today[0]), "roast": roast[0]["data"] if roast else None,
                "queues": self.queues(), "market": self.market_status(),
                "season": {"slug": self.season["slug"], "name": self.season["name"],
                           "start": str(self.season["start_date"]), "end": str(self.season["end_date"]),
                           "trading_days": len({h["trade_date"] for hs in self.history.values() for h in hs})
                           + (1 if self.engine.day_open else 0)}}

    def rank_info(self, pid: str) -> dict:
        board = self.leaderboard("all")
        me = next((r for r in board if r["portfolio_id"] == pid), None)
        if me is None:   # suspended / banned: off the board
            return {"rank": None, "cum_return": 0.0, "beaten_ai": 0, "ai_total": 0, "chase": None}
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
        me = next((r for r in board if r["portfolio_id"] == pid), None)
        if me is None:
            eq0, _, _ = self.equity(pid)
            cum0 = float(eq0 / self.engine.accounts[pid].initial_cash - 1)
            me = {"rank": None, "cum_return": cum0, "excess_return": cum0 - self.benchmark_return()}
        hist = self.history.get(pid, [])
        eq, cash, _ = self.equity(pid)
        initial = self.engine.accounts[pid].initial_cash
        peak, mdd = initial, 0.0
        for e in [Decimal(h["total_equity"]) for h in hist] + [eq]:
            peak = max(peak, e)
            mdd = min(mdd, float(e / peak - 1))
        win = (await self.store.win_rates([pid])).get(pid)
        got, queued = await self.store.queue_stats(pid)
        counts = dict(await self.store.badge_counts(pid))
        career = await self.store.career(pid)
        for c in career:                                 # badges earned in earlier seasons count too
            for code in c.get("badges") or []:
                counts[code] = counts.get(code, 0) + 1
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
            "cheers": self.cheers.get(pid, 0),
            "status": self.status.get(pid, ("active", None))[0],
            "season": {"slug": self.season["slug"], "name": self.season["name"]},
            "career": [{"season": c["slug"], "name": c["name"], "rank": c["rank"],
                        "cum_return": None if c["cum_return"] is None else float(c["cum_return"]),
                        "excess_return": None if c["excess_return"] is None else float(c["excess_return"]),
                        "trades": c["trades"]} for c in career],
        }

    async def timeline(self, pid: str, before: date | None = None, days: int = 5, kind: str = "all") -> dict:
        """A team's decisions grouped by trading day, `days` at a time, newest first. `before` pages back;
        kind: all | filled (something traded) | conflict (多空分歧)."""
        day_list, more, total = await self.store.timeline_days(pid, before, days, kind)
        if not day_list:
            return {"days": [], "items": [], "next_before": None, "total": total}
        items = await self._enrich_timeline(await self.store.timeline(pid, min(day_list), max(day_list)))
        if kind == "filled":
            items = [it for it in items if it["kind"] == "corporate" or any(o["filled_qty"] > 0 for o in it["orders"])]
        elif kind == "conflict":
            items = [it for it in items if it.get("has_conflict")]
        fills = await self.store.day_fills(pid, day_list)
        rets = await self.store.day_returns(pid, day_list)
        if self.engine.day_open and self.engine.trade_date in day_list and pid in self.engine.accounts:
            row = next((r for r in self.leaderboard("all") if r["portfolio_id"] == pid), None)
            if row:
                rets[self.engine.trade_date] = row["day_return"]
        out = []
        for d in day_list:
            its = [it for it in items if it["day"] == d.isoformat()]
            out.append({"date": d.isoformat(), "weekday": "一二三四五六日"[d.weekday()],
                        "debates": sum(1 for it in its if it["kind"] == "debate"), "fills": fills.get(d, 0),
                        "day_return": rets.get(d), "items": its})
        return {"days": out, "items": items, "next_before": day_list[-1].isoformat() if more else None, "total": total}

    async def _enrich_timeline(self, items: list[dict]) -> list[dict]:
        for it in items:
            it["symbol_name"] = self.names.get(it["symbol"], it["symbol"])
            live = [self.engine.order(o["order_id"]) for o in it["orders"]]
            for o, lo in zip(it["orders"], live):
                if lo is not None:
                    o.update(status=lo.status.value, filled_qty=lo.filled_qty, queue_ahead=lo.queue_ahead,
                             queue_progress_pct=lo.queue_progress_pct,
                             avg_fill_price=None if lo.avg_fill_price is None else str(lo.avg_fill_price),
                             fees_paid=str(lo.fees_paid))
                if f"order:{o['order_id']}" in self.hidden:
                    o["thesis"] = HIDDEN_TEXT
            if it["kind"] == "debate" and f"debate:{it['id']}" in self.hidden:
                it["rounds"] = [r | {"argument": HIDDEN_TEXT} for r in it["rounds"]]
                it["final_decision"] = {**it["final_decision"], "title": "（已隐藏）", "summary": HIDDEN_TEXT}
        return items

    async def my_orders(self, pid: str, scope: str = "today", before: date | None = None, days: int = 5) -> dict:
        """scope=today: the current trading day, open orders first. scope=history: earlier days, `days` at a
        time, grouped by trade date."""
        today = self.engine.trade_date or datetime.now(CST).date()

        def view(r) -> dict:
            live = self.engine.order(str(r["id"]))
            v = live.public_view() if live else _row_view(r)
            return self._enrich(v) | {"created_at": r["created_at"].isoformat(), "trade_date": r["trade_date"].isoformat()}

        if scope == "today":
            rows = [view(r) for r in await self.store.orders_between(pid, today, today)]
            rows.sort(key=lambda o: o["status"] not in ("new", "partially_filled"))   # still cancellable first
            return {"orders": rows, "days": [], "next_before": None}
        day_list, more = await self.store.order_days(pid, before or today, days)
        if not day_list:
            return {"orders": [], "days": [], "next_before": None}
        rows = [view(r) for r in await self.store.orders_between(pid, min(day_list), max(day_list))]
        return {"orders": rows,
                "days": [{"date": d.isoformat(), "weekday": "一二三四五六日"[d.weekday()],
                          "orders": [o for o in rows if o["trade_date"] == d.isoformat()]} for d in day_list],
                "next_before": day_list[-1].isoformat() if more else None}

    async def conflicts_on(self, day: date | None = None) -> dict:
        day = day or self.engine.trade_date or datetime.now(CST).date()
        cards, prev = await self.store.conflicts_on(self.season_id, day)
        out = []
        for d in cards:
            if not (self.is_active(d.get("buyer_id", "")) and self.is_active(d.get("seller_id", ""))):
                continue
            if f"order:{d.get('buy_order_id')}" in self.hidden:
                d["buy_thesis"] = HIDDEN_TEXT
            if f"order:{d.get('sell_order_id')}" in self.hidden:
                d["sell_thesis"] = HIDDEN_TEXT
            out.append(d)
        return {"date": day.isoformat(), "weekday": "一二三四五六日"[day.weekday()], "conflicts": out,
                "prev": prev.isoformat() if prev else None}


def _row_view(r: asyncpg.Record) -> dict:
    return {"order_id": str(r["id"]), "portfolio_id": str(r["portfolio_id"]), "symbol": r["symbol"],
            "side": r["side"], "order_type": r["order_type"], "price": None if r["price"] is None else str(r["price"]),
            "qty": r["qty"], "filled_qty": r["filled_qty"],
            "avg_fill_price": None if r["avg_fill_price"] is None else str(r["avg_fill_price"]),
            "status": r["status"], "queue_initial": r["queue_initial"], "queue_ahead": r["queue_ahead"],
            "queue_progress_pct": None, "debate_id": None if r["debate_id"] is None else str(r["debate_id"]),
            "thesis": r["thesis"]}


def _parse_ts(value: str | None) -> datetime | None:
    """RFC 3339 from the gateway (chrono writes up to 9 fractional digits; Python takes 6)."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", value))
    except ValueError:
        return None
