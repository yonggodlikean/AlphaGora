from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _team_tokens() -> tuple[tuple[str, str], ...]:
    pairs = [(t.split(":", 1)[0].strip(), t.split(":", 1)[1].strip())
             for t in _env("ARENA_TEAM_TOKENS", "").split(",") if ":" in t and t.split(":", 1)[1].strip()]
    # Shortcut used by docker-compose: the OpenPup team container and the engine share PUP_TEAM_TOKEN.
    pup = _env("ARENA_PUP_TEAM_TOKEN").strip()
    if pup and all(tag != "macro_debate" for tag, _ in pairs):
        pairs.append(("macro_debate", pup))
    return tuple(pairs)


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL", "postgresql://agora:agora@127.0.0.1:5432/agora"))
    redis_url: str = field(default_factory=lambda: _env("REDIS_URL", "redis://127.0.0.1:6379/0"))
    # redis = live quotes from market-gateway; demo = built-in synthetic day, looping; replay = a JSONL tape;
    # manual = no feed, simulated clock (tests drive on_snapshot); none = no feed, wall clock
    market_feed: str = field(default_factory=lambda: _env("ARENA_MARKET_FEED", "demo"))
    market_stream: str = field(default_factory=lambda: _env("ARENA_MARKET_STREAM", "md:l1"))
    replay_path: str = field(default_factory=lambda: _env("ARENA_REPLAY_PATH", ""))
    demo_speed: float = field(default_factory=lambda: float(_env("ARENA_DEMO_SPEED", "30")))
    season_slug: str = field(default_factory=lambda: _env("ARENA_SEASON_SLUG", ""))
    token_pepper: str = field(default_factory=lambda: _env("ARENA_TOKEN_PEPPER", "dev-pepper-change-me"))
    benchmark_symbol: str = field(default_factory=lambda: _env("ARENA_BENCHMARK_SYMBOL", ""))
    dividend_symbols: tuple[str, ...] = field(default_factory=lambda: tuple(
        s.strip() for s in _env("ARENA_DIVIDEND_SYMBOLS", "").split(",") if s.strip()))
    house_bots: bool = field(default_factory=lambda: _env("ARENA_HOUSE_BOTS", "1") == "1")
    # Official teams driven from outside (any agent runner holding the team's token) instead of the built-in rule bots.
    external_teams: tuple[str, ...] = field(default_factory=lambda: tuple(
        s.strip() for s in _env("ARENA_EXTERNAL_TEAMS", "").split(",") if s.strip()))
    # "tag:token,tag:token": pre-shared tokens for official teams driven from outside. Registering a token for a
    # team also hands that team to its external driver (same effect as listing it in ARENA_EXTERNAL_TEAMS).
    team_tokens: tuple[tuple[str, str], ...] = field(default_factory=lambda: _team_tokens())
    online_window_s: int = field(default_factory=lambda: int(_env("ARENA_ONLINE_WINDOW", "600")))
    # Sign-up: invite codes and per-season seat caps (official teams never count)
    require_invite: bool = field(default_factory=lambda: _env("ARENA_REQUIRE_INVITE", "1") == "1")
    seats_human: int = field(default_factory=lambda: int(_env("ARENA_SEATS_HUMAN", "100")))
    seats_ai: int = field(default_factory=lambda: int(_env("ARENA_SEATS_AI", "20")))
    # Extra blocked words for names / rationales / debates (UTF-8, one per line)
    blocklist: str = field(default_factory=lambda: _env("ARENA_BLOCKLIST", ""))
    # Order rate limits per account (official house teams are exempt)
    orders_per_min_human: int = field(default_factory=lambda: int(_env("ARENA_ORDERS_PER_MIN_HUMAN", "20")))
    orders_per_day_human: int = field(default_factory=lambda: int(_env("ARENA_ORDERS_PER_DAY_HUMAN", "500")))
    orders_per_min_ai: int = field(default_factory=lambda: int(_env("ARENA_ORDERS_PER_MIN_AI", "60")))
    cors_origins: tuple[str, ...] = field(default_factory=lambda: tuple(
        s.strip() for s in _env("ARENA_CORS_ORIGINS", "*").split(",") if s.strip()))
    migrations_dir: Path = field(default_factory=lambda: Path(_env(
        "ARENA_MIGRATIONS_DIR", str(Path(__file__).resolve().parents[4] / "db" / "migrations"))))
    auto_start_feed: bool = True

    @property
    def is_simulated_clock(self) -> bool:
        """demo/replay: the market clock follows snapshot timestamps instead of the wall clock."""
        return self.market_feed in ("demo", "replay", "manual")

    def resolved_external_teams(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(self.external_teams + tuple(tag for tag, _ in self.team_tokens)))

    def resolved_season_slug(self) -> str:
        if self.season_slug:
            return self.season_slug
        return "demo" if self.market_feed == "demo" else "s1-2026w"

    def resolved_benchmark(self) -> str:
        if self.benchmark_symbol:
            return self.benchmark_symbol
        return "999300.SH" if self.market_feed == "demo" else "510300.SH"

    def resolved_dividend_symbols(self) -> tuple[str, ...]:
        if self.dividend_symbols:
            return self.dividend_symbols
        if self.market_feed == "demo":
            return ("999002.SH", "999003.SH")
        return ("601398.SH", "600900.SH", "601088.SH")
