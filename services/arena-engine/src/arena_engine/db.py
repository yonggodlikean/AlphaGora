"""Connection pool and a minimal forward-only migration runner (db/migrations/*.sql, applied once each)."""
from __future__ import annotations

import logging
from pathlib import Path

import asyncpg

log = logging.getLogger(__name__)


async def create_pool(dsn: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(dsn, min_size=1, max_size=10, command_timeout=30)


async def migrate(pool: asyncpg.Pool, directory: Path) -> list[str]:
    applied: list[str] = []
    async with pool.acquire() as conn:
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        # Serialize concurrent starters.
        await conn.execute("SELECT pg_advisory_lock(727274)")
        try:
            done = {r["name"] for r in await conn.fetch("SELECT name FROM schema_migrations")}
            for path in sorted(directory.glob("*.sql")):
                if path.name in done:
                    continue
                log.info("applying migration %s", path.name)
                await conn.execute(path.read_text(encoding="utf-8"))
                await conn.execute("INSERT INTO schema_migrations (name) VALUES ($1)", path.name)
                applied.append(path.name)
        finally:
            await conn.execute("SELECT pg_advisory_unlock(727274)")
    return applied
