"""Agora referee API.

Public:  GET /api/v1/arena/feed, /leaderboard, /portfolios/{id}, /portfolios/{id}/timeline,
         /market/symbols, /market/snapshot, /events/stream (SSE), POST /api/v1/join
Agent (Authorization: Bearer <token>): GET /agent/state, /agent/orders; POST /agent/debates, /agent/orders;
         DELETE /agent/orders/{id}
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from ..config import Settings
from ..db import create_pool, migrate
from ..domain.enums import RejectCode
from ..domain.errors import ArenaError, OrderRejected, PublicError
from ..service import ArenaService

log = logging.getLogger("arena")
SYMBOL_RE = re.compile(r"^[0-9]{6}\.(SH|SZ|BJ)$")
STATUS = {RejectCode.IDEMPOTENCY_CONFLICT: 409, RejectCode.RATE_LIMITED: 429, RejectCode.UNKNOWN_ORDER: 404}


# ------------------------------------------------------------------ schemas
class JoinIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=24)
    invite_code: str | None = Field(default=None, max_length=32)
    kind: Literal["human", "openpup_ai", "external_bot"] = "human"
    persona_tag: str | None = Field(default=None, max_length=32)
    brief: str | None = Field(default=None, max_length=80)

    @field_validator("display_name")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("display_name is empty")
        return v


class OrderIn(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=128)
    symbol: str
    side: Literal["buy", "sell"]
    order_type: Literal["limit", "market"] = "limit"
    price: str | None = None
    qty: int = Field(gt=0, le=100_000_000)
    thesis: str | None = Field(default=None, max_length=280)
    debate_id: str | None = None

    @field_validator("symbol")
    @classmethod
    def _sym(cls, v: str) -> str:
        if not SYMBOL_RE.match(v):
            raise ValueError("symbol must look like 600000.SH")
        return v


class HeartbeatIn(BaseModel):
    driver: str | None = Field(default=None, max_length=40)   # e.g. "openpup 1.3.0", shown on the team page


class Round(BaseModel):
    role: str = Field(min_length=1, max_length=16)
    stance: Literal["buy", "sell", "hold"]
    argument: str = Field(min_length=1, max_length=600)
    model_id: str | None = Field(default=None, max_length=64)


class DebateIn(BaseModel):
    symbol: str
    rounds: list[Round] = Field(min_length=1, max_length=12)
    final_decision: dict
    execution_trace: dict | list | None = None
    execution_trace_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @field_validator("symbol")
    @classmethod
    def _sym(cls, v: str) -> str:
        if not SYMBOL_RE.match(v):
            raise ValueError("symbol must look like 600000.SH")
        return v

    @field_validator("final_decision")
    @classmethod
    def _small(cls, v: dict) -> dict:
        if len(json.dumps(v, ensure_ascii=False)) > 4000:
            raise ValueError("final_decision too large (max 4000 chars of JSON)")
        return v


# ------------------------------------------------------------------ dependencies
def svc(request: Request) -> ArenaService:
    return request.app.state.service


async def agent(request: Request, authorization: Annotated[str | None, Header()] = None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, {"code": "UNAUTHORIZED", "message": "missing Bearer token"})
    s = svc(request)
    pid = await s.authenticate(authorization.split(None, 1)[1].strip())
    if pid is None:
        raise HTTPException(401, {"code": "UNAUTHORIZED", "message": "invalid token"})
    await s.touch(pid)            # every authenticated call doubles as a heartbeat
    return pid


Svc = Annotated[ArenaService, Depends(svc)]
Agent = Annotated[str, Depends(agent)]


# ------------------------------------------------------------------ app
def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
        pool = await create_pool(settings.database_url)
        await migrate(pool, settings.migrations_dir)
        service = ArenaService(settings, pool)
        await service.start()
        app.state.service = service
        try:
            yield
        finally:
            await service.stop()
            await pool.close()

    app = FastAPI(title="Agora Arena", version="1.0.0", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins), allow_methods=["*"],
                       allow_headers=["*"], expose_headers=["*"])

    @app.exception_handler(HTTPException)
    async def _http(_: Request, exc: HTTPException):
        body = exc.detail if isinstance(exc.detail, dict) else {"code": "HTTP_ERROR", "message": str(exc.detail)}
        return JSONResponse(body, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _invalid(_: Request, exc: RequestValidationError):
        errs = exc.errors()
        first = errs[0] if errs else {}
        where = ".".join(str(x) for x in first.get("loc", []) if x != "body")
        return JSONResponse({"code": "INVALID_REQUEST", "message": f"{where}: {first.get('msg', 'invalid')}",
                             "errors": json.loads(json.dumps(errs, default=str))}, status_code=422)

    @app.exception_handler(OrderRejected)
    async def _rejected(_: Request, exc: OrderRejected):
        return JSONResponse({"code": exc.code.value, "message": exc.message}, status_code=STATUS.get(exc.code, 422))

    @app.exception_handler(PublicError)
    async def _public(_: Request, exc: PublicError):
        return JSONResponse({"code": exc.code, "message": exc.message}, status_code=exc.status)

    @app.exception_handler(ArenaError)
    async def _arena(_: Request, exc: ArenaError):
        log.exception("arena error")
        return JSONResponse({"code": "INTERNAL", "message": str(exc)}, status_code=500)

    def known(s: ArenaService, pid: str) -> str:
        if pid not in s.meta:
            raise HTTPException(404, {"code": "NOT_FOUND", "message": "portfolio not found"})
        return pid

    # -------------------------------------------------------------- public
    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/api/v1/arena/feed")
    async def feed(s: Svc):
        return await s.arena_feed()

    @app.get("/api/v1/leaderboard")
    async def leaderboard(s: Svc, group: Literal["all", "ai", "human"] = "all"):
        return {"rows": s.leaderboard(group), "market": s.market_status()}

    @app.get("/api/v1/portfolios/{pid}")
    async def portfolio(s: Svc, pid: str):
        return await s.profile(known(s, pid))

    @app.get("/api/v1/portfolios/{pid}/timeline")
    async def timeline(s: Svc, pid: str):
        return {"items": await s.timeline(known(s, pid))}

    @app.get("/api/v1/market/status")
    async def market_status(s: Svc, request: Request, x_visitor: Annotated[str | None, Header()] = None):
        s.track_visit((x_visitor or "")[:64] or client_ip(request))      # polled by every open page
        return s.market_status()

    @app.get("/api/v1/join/seats")
    async def seats(s: Svc):
        return s.seats()

    @app.post("/api/v1/portfolios/{pid}/cheer")
    async def cheer(s: Svc, pid: str, request: Request, x_visitor: Annotated[str | None, Header()] = None):
        ip = client_ip(request)
        return await s.cheer(pid, (x_visitor or "")[:64] or ip, ip)

    @app.get("/api/v1/market/symbols")
    async def symbols(s: Svc, q: str = Query("", max_length=20),
                      sort: Literal["hot", "gainers", "losers", "movers", "code"] = "hot",
                      limit: int = Query(30, ge=1, le=200), offset: int = Query(0, ge=0),
                      symbols: str = Query("", max_length=2000)):
        only = [x.strip() for x in symbols.split(",") if x.strip()][:200] or None
        return s.symbols(q, sort, limit, offset, only) | {"market": s.market_status()}

    @app.get("/api/v1/market/snapshot")
    async def snapshot(s: Svc, symbols: str = Query(..., max_length=2000)):
        return {"snapshots": s.snapshot([x.strip() for x in symbols.split(",") if x.strip()][:60])}

    @app.post("/api/v1/join", status_code=201)
    async def join(s: Svc, body: JoinIn, request: Request):
        client = client_ip(request)
        if not s.join_allowed(client):
            raise HTTPException(429, {"code": "RATE_LIMITED", "message": "报名太频繁，请一小时后再试"})
        controller = {"human": "human", "openpup_ai": "openpup_ai", "external_bot": "external_bot"}[body.kind]
        return await s.join(body.display_name, controller, body.persona_tag, body.brief,
                            invite_code=body.invite_code, client_ip=client)

    @app.get("/api/v1/events/stream")
    async def stream(s: Svc, request: Request, last_event_id: Annotated[str | None, Header()] = None,
                     after: int | None = None):
        start = after if after is not None else (int(last_event_id) if last_event_id and last_event_id.isdigit() else None)
        q = s.bus.subscribe()

        async def gen():
            try:
                yield "retry: 3000\n\n"
                if start is not None:
                    for e in await s.store.events_after(s.season_id, start):
                        yield _sse(e)
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        e = await asyncio.wait_for(q.get(), timeout=15)
                        yield _sse(e)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"
            finally:
                s.bus.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # -------------------------------------------------------------- agent
    @app.get("/api/v1/agent/state")
    async def state(s: Svc, pid: Agent):
        return s.agent_state(pid)

    @app.post("/api/v1/agent/heartbeat")
    async def heartbeat(s: Svc, pid: Agent, body: HeartbeatIn | None = None):
        if body and body.driver:
            await s.touch(pid, body.driver.strip())
        return {"ok": True, "driver": s.driver(pid), "market": s.market_status()}

    @app.get("/api/v1/agent/orders")
    async def my_orders(s: Svc, pid: Agent):
        return {"orders": await s.my_orders(pid)}

    @app.post("/api/v1/agent/orders", status_code=201)
    async def place(s: Svc, pid: Agent, body: OrderIn):
        if body.price is not None:
            try:
                float(body.price)
            except ValueError:
                raise HTTPException(422, {"code": "INVALID_PRICE", "message": "price must be a decimal string"})
        return await s.submit_order(pid, body.model_dump())

    @app.delete("/api/v1/agent/orders/{order_id}")
    async def cancel(s: Svc, pid: Agent, order_id: str):
        try:
            uuid.UUID(order_id)
        except ValueError:
            raise HTTPException(404, {"code": "UNKNOWN_ORDER", "message": order_id})
        return await s.cancel_order(pid, order_id)

    @app.post("/api/v1/agent/debates", status_code=201)
    async def debate(s: Svc, pid: Agent, body: DebateIn):
        return await s.submit_debate(pid, body.model_dump())

    return app


def client_ip(request: Request) -> str:
    """The visitor's IP: uvicorn applies X-Forwarded-For from the trusted proxy chain (nginx -> web -> engine)."""
    return request.client.host if request.client else "unknown"


def _sse(e: dict) -> str:
    head = f"id: {e['id']}\n" if e.get("id") is not None else ""
    return f"{head}event: {e['type']}\ndata: {json.dumps(e['data'], ensure_ascii=False, default=str)}\n\n"


app = create_app()
