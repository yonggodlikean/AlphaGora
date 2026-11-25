"""Operator commands (run inside the engine container: docker compose exec arena-engine python -m arena_engine.admin …).

    invites create --kind human --count 20 [--uses 1] [--note "WeChat group A"]
    invites list | invites revoke AG-XXXXXXXX
    seats                                   seats taken / total per group
    suspend <name|id> --reason "…"          cannot trade, off the leaderboard; reversible with `restore`
    ban <name|id> --reason "…"              token revoked, open orders canceled, off the leaderboard
    restore <name|id>
    hide order|debate <id> --reason "…"     replace a rationale / debate with "hidden" everywhere; `unhide` undoes
    review                                  sign-ups that share a source (possible multi-accounting)
    reset-token <name|id>                   leaked token: revoke it and print a new one (account and record kept)
    issue-token <persona>                   token for an official team driven by an external agent
    list-teams
    alert-test                              send a test message to ARENA_ALERT_WEBHOOK

The running engine applies status and moderation changes within 30 seconds.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import secrets
import sys
import uuid
from datetime import datetime, timedelta

from .config import Settings
from .db import create_pool, migrate
from .market.calendar import CST
from .personas import BY_TAG
from .seasons import season_for
from .store import Store


async def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="python -m arena_engine.admin")
    sub = ap.add_subparsers(dest="cmd", required=True)

    inv = sub.add_parser("invites", help="invite codes")
    inv_sub = inv.add_subparsers(dest="action", required=True)
    c = inv_sub.add_parser("create")
    c.add_argument("--kind", choices=["human", "ai"], required=True)
    c.add_argument("--count", type=int, default=1)
    c.add_argument("--uses", type=int, default=1, help="sign-ups per code (e.g. 30 for a group code)")
    c.add_argument("--note", default=None)
    inv_sub.add_parser("list")
    r = inv_sub.add_parser("revoke")
    r.add_argument("code")

    sub.add_parser("seats")
    for name in ("suspend", "ban"):
        p = sub.add_parser(name)
        p.add_argument("who", help="portfolio name or id")
        p.add_argument("--reason", required=True)
    p = sub.add_parser("restore")
    p.add_argument("who")
    for name in ("hide", "unhide"):
        p = sub.add_parser(name)
        p.add_argument("kind", choices=["order", "debate"])
        p.add_argument("id")
        if name == "hide":
            p.add_argument("--reason", default=None)
    sub.add_parser("review", help="sign-ups sharing a source")
    p = sub.add_parser("reset-token", help="revoke a player's token(s) and print a new one")
    p.add_argument("who", help="portfolio name or id")
    it = sub.add_parser("issue-token", help="token for an official team")
    it.add_argument("persona", choices=sorted(BY_TAG))
    sub.add_parser("list-teams")
    sub.add_parser("alert-test", help="send a test message to ARENA_ALERT_WEBHOOK")
    args = ap.parse_args(argv)

    settings = Settings()
    if args.cmd == "alert-test":
        from .alerts import Alerter
        if not settings.alert_webhook:
            print("ARENA_ALERT_WEBHOOK is not set", file=sys.stderr)
            return 1
        ok = await Alerter(settings.alert_webhook, settings.alert_format).send(
            "告警测试", "如果你看到这条消息，Agora 的告警 webhook 已配置成功。", "ok")
        print("sent" if ok else "failed (see the log line above)")
        return 0 if ok else 1
    pool = await create_pool(settings.database_url)
    try:
        await migrate(pool, settings.migrations_dir)
        store = Store(pool)
        today = datetime.now(CST).date()
        season = None
        if not settings.season_slug and settings.market_feed == "redis":     # rotating seasons: the open one
            season = await store.open_auto_season()
            if season is None:
                spec = season_for(today)
                season = await store.ensure_season(spec.slug, spec.start, spec.end, spec.name)
        if season is None:
            season = await store.ensure_season(settings.resolved_season_slug(), today, today + timedelta(days=120))
        sid = season["id"]

        if args.cmd == "invites":
            if args.action == "create":
                if args.count < 1 or args.count > 500 or args.uses < 1:
                    print("count must be 1-500 and uses >= 1", file=sys.stderr)
                    return 2
                for code in await store.create_invites(sid, args.kind, args.count, args.uses, args.note):
                    print(code)
                print(f"# {args.count} {args.kind} invite(s), {args.uses} use(s) each, season {season['slug']}",
                      file=sys.stderr)
            elif args.action == "list":
                for row in await store.list_invites(sid):
                    state = "revoked" if row["revoked_at"] else ("used up" if row["used"] >= row["max_uses"] else "open")
                    print(f"{row['code']}  {row['kind']:<5} {row['used']}/{row['max_uses']}  {state:<8} {row['note'] or ''}")
            else:
                ok = await store.revoke_invite(args.code)
                print("revoked" if ok else "no such open invite")
            return 0

        if args.cmd == "seats":
            taken = await store.seats_taken(sid)
            print(f"human {taken['human']}/{settings.seats_human} · ai {taken['ai']}/{settings.seats_ai} "
                  f"· invites {'required' if settings.require_invite else 'off'} · season {season['slug']}")
            return 0

        if args.cmd in ("suspend", "ban", "restore"):
            row = await store.find_portfolio(sid, args.who)
            if row is None:
                print(f"no portfolio {args.who!r} in season {season['slug']}", file=sys.stderr)
                return 1
            if row["is_official"]:
                print("official teams cannot be suspended or banned", file=sys.stderr)
                return 1
            status = {"suspend": "suspended", "ban": "banned", "restore": "active"}[args.cmd]
            if args.cmd == "restore" and row["status"] == "banned":
                print("banned accounts stay banned (their token is revoked); create a new invite instead", file=sys.stderr)
                return 1
            await store.set_status(row["id"], status, getattr(args, "reason", None))
            print(f"{row['name']}: {row['status']} -> {status} (the engine applies it within 30s)")
            return 0

        if args.cmd == "reset-token":
            row = await store.find_portfolio(sid, args.who)
            if row is None:
                print(f"no portfolio {args.who!r} in season {season['slug']}", file=sys.stderr)
                return 1
            if row["is_official"]:
                print("official teams use the pre-shared PUP_TEAM_TOKEN / issue-token instead", file=sys.stderr)
                return 1
            if row["status"] == "banned":
                print("banned accounts stay banned; create a new invite instead", file=sys.stderr)
                return 1
            raw = "agk_" + secrets.token_urlsafe(24)
            digest = hmac.new(settings.token_pepper.encode(), raw.encode(), hashlib.sha256).digest()
            n = await store.reset_token(row["id"], raw[:8], digest)
            print(f"# {row['name']}: {n} old token(s) revoked; they stop working within 30s. Send the player:",
                  file=sys.stderr)
            print(raw)
            return 0

        if args.cmd in ("hide", "unhide"):
            ref = uuid.UUID(args.id)
            if args.cmd == "hide":
                await store.hide(args.kind, ref, args.reason)
            else:
                await store.unhide(args.kind, ref)
            print(f"{args.cmd} {args.kind} {ref} (applies within 30s)")
            return 0

        if args.cmd == "review":
            rows = await store.shared_sources(sid)
            if not rows:
                print("no shared sign-up sources")
            for row in rows:
                print(f"{row['n']} accounts from one source: {', '.join(row['names'])}")
            return 0

        if args.cmd == "list-teams":
            for p in await store.portfolios(sid):
                print(f"{p.id}  {p.controller_type:<12} {'official' if p.is_official else '':<8} {p.persona_tag or '':<22} {p.name}")
            return 0

        meta = await store.official_portfolio(sid, args.persona)
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
