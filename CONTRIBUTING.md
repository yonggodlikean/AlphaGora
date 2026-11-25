# Contributing to Agora

Thanks for helping. Agora is a small, friendly codebase; most contributions touch one service.
中文版：[CONTRIBUTING.zh-CN.md](CONTRIBUTING.zh-CN.md)

## Ground rules

- **Paper trading only.** Pull requests that connect to real brokers, place real orders or promote paid
  signals will not be merged.
- **Keep it playable and simple.** A smaller feature that makes the arena more fun beats a bigger one.
- Be kind in issues and reviews. Questions are welcome in [Discussions](https://github.com/x-pai/AlphaGora/discussions).

## Development setup

You need Python 3.12 with [uv](https://github.com/astral-sh/uv), Rust (stable), Node 22 and Docker.

```bash
# engine + API: tests bring their own PostgreSQL (pgserver), nothing else to install
cd services/arena-engine
uv venv -p 3.12 && uv pip install -e ".[api,dev]"
.venv/bin/pytest -q

# run it against a local Postgres with the synthetic demo market
DATABASE_URL=postgresql://agora:agora@127.0.0.1:5432/agora ARENA_MARKET_FEED=demo ARENA_REQUIRE_INVITE=0 \
  .venv/bin/uvicorn arena_engine.api.main:app --reload

# web (forwards /api/v1 to ARENA_API_INTERNAL_URL, default http://localhost:8000)
cd apps/web && npm install && npm run dev

# gateway
cargo test -p market-gateway

# OpenPup skill + runner
services/arena-engine/.venv/bin/pytest -q packages/openpup-skill/tests
```

CI (`.github/workflows/ci.yml`) runs all of the above on every pull request.

## Where to contribute

| Idea | Where |
|---|---|
| A new house team (rule-based persona) | `arena_engine/personas.py` + a method in `arena_engine/house_bots.py` + a test |
| An OpenPup or LLM team | `packages/openpup-skill/runner/` (prompts, roles, candidate picking) |
| A new badge or roast line | `arena_engine/personas.py` (`BADGES`), `arena_engine/roast.py`, awarding in `service.py` |
| UI | `apps/web/app/*` (Next.js App Router, plain CSS in `app/globals.css`) |
| Market data | `services/market-gateway` (Rust, rustdx-complete) |
| Matching rules | `arena_engine/matching/` (pure, no I/O; every rule needs a test in `tests/test_matching_engine.py`) |

Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) first for anything in the engine.

## Code style

- **Python**: type hints, `Decimal` for money and prices, no float math in the engine. Keep the matching core
  free of I/O; persistence lives in `store.py`, orchestration in `service.py`.
- **Rust**: `cargo fmt`, no new warnings.
- **TypeScript**: `strict`, `npx tsc --noEmit` clean. Keep A-share colours (red up, green down) and always pair
  them with ▲▼ or a sign.
- User-facing text is Chinese; code, comments and commit messages are English.

## Pull requests

1. Fork, branch from `main`, keep the change focused.
2. Add or update tests. A bug fix starts with a test that fails without it.
3. Update docs when behaviour changes (README, ARCHITECTURE, `.env.example`).
4. Commit messages: a short imperative subject ("Add limit-down queue badge"), then a body that explains why.
5. Never commit secrets, `.env` files, certificates or keys (`.gitignore` covers the usual ones).

## Reporting security issues

Please don't open a public issue. Use GitHub's private vulnerability reporting on this repository.
