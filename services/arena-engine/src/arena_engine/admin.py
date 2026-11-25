"""Operator commands.

    python -m arena_engine.admin issue-token macro_debate     # token for an official team driven by an external agent
    python -m arena_engine.admin list-teams
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import secrets
import sys
from datetime import datetime, timedelta

from .config import Settings
from .db import create_pool, migrate
from .market.calendar import CST
from .personas import BY_TAG
from .store import Store


async def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="python -m arena_engine.admin")
    sub = ap.add_subparsers(dest="cmd", required=True)
    it = sub.add_parser("issue-token", help="issue an API token for an official team")
    it.add_argument("persona", choices=sorted(BY_TAG))
    sub.add_parser("list-teams", help="list portfolios in the current season")
    args = ap.parse_args(argv)

    settings = Settings()
    pool = await create_pool(settings.database_url)
    try:
        await migrate(pool, settings.migrations_dir)
        store = Store(pool)
        today = datetime.now(CST).date()
        season = await store.ensure_season(settings.resolved_season_slug(), today, today + timedelta(days=120))
        if args.cmd == "list-teams":
            for p in await store.portfolios(season["id"]):
                print(f"{p.id}  {p.controller_type:<12} {'official' if p.is_official else '':<8} {p.persona_tag or '':<22} {p.name}")
            return 0
        meta = await store.official_portfolio(season["id"], args.persona)
        if meta is None:
            print(f"no official team {args.persona!r} in season {season['slug']}", file=sys.stderr)
            return 1
        raw = "agk_" + secrets.token_urlsafe(24)
        digest = hmac.new(settings.token_pepper.encode(), raw.encode(), hashlib.sha256).digest()
        await store.add_token(meta.id, raw[:8], digest)
        print(f"# {meta.name} · season {season['slug']}", file=sys.stderr)
        print(f"# start the engine with ARENA_EXTERNAL_TEAMS={args.persona} so the rule bot stops trading it", file=sys.stderr)
        print(raw)
        return 0
    finally:
        await pool.close()


def main() -> None:
    raise SystemExit(asyncio.run(_main(sys.argv[1:])))


if __name__ == "__main__":
    main()
