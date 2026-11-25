from __future__ import annotations

from .enums import RejectCode


class ArenaError(Exception):
    """Base class for all engine errors."""


class OrderRejected(ArenaError):
    """A command was refused. No state was changed. Maps to HTTP 4xx (409 for idempotency, 429 for rate limits)."""

    def __init__(self, code: RejectCode, message: str) -> None:
        super().__init__(f"{code.value}: {message}")
        self.code = code
        self.message = message


class MarketDataError(ArenaError):
    """A snapshot failed validation (crossed book, bad ordering, ...). The snapshot is dropped."""


class InvariantViolation(ArenaError):
    """Internal accounting invariant broken. Should never happen; the engine halts the affected portfolio."""


class PublicError(ArenaError):
    """A request refused for a reason the user should see (sign-up, moderation, account status)."""

    def __init__(self, code: str, message: str, status: int = 422) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status
