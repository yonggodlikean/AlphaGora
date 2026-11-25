from enum import Enum


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    LIMIT = "limit"
    # 最优五档即时成交剩余撤销: walk up to 5 visible levels, cancel the remainder.
    MARKET = "market"


class OrderStatus(str, Enum):
    NEW = "new"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    EXPIRED = "expired"
    REJECTED = "rejected"

    @property
    def is_open(self) -> bool:
        return self in (OrderStatus.NEW, OrderStatus.PARTIALLY_FILLED)


class ControllerType(str, Enum):
    OPENPUP_AI = "openpup_ai"
    EXTERNAL_BOT = "external_bot"
    HUMAN = "human"
    HYBRID = "hybrid"


class Liquidity(str, Enum):
    TAKER = "taker"              # crossed the visible book on arrival
    MAKER = "maker"              # resting order crossed by a later snapshot
    QUEUE = "queue"              # resting order reached the front of its price-level queue
    BOARD_BREAK = "board_break"  # resting at a limit price when the board opened (炸板)
    AUCTION = "auction"          # matched in the opening or closing call auction (集合竞价)


class RejectCode(str, Enum):
    UNKNOWN_PORTFOLIO = "UNKNOWN_PORTFOLIO"
    THESIS_REQUIRED = "THESIS_REQUIRED"
    INVALID_QTY = "INVALID_QTY"
    INVALID_PRICE = "INVALID_PRICE"
    INVALID_LOT = "INVALID_LOT"
    PRICE_OUTSIDE_LIMITS = "PRICE_OUTSIDE_LIMITS"
    MARKET_CLOSED = "MARKET_CLOSED"
    NO_MARKET_DATA = "NO_MARKET_DATA"
    STALE_MARKET_DATA = "STALE_MARKET_DATA"
    SUSPENDED = "SUSPENDED"
    INSUFFICIENT_CASH = "INSUFFICIENT_CASH"
    INSUFFICIENT_POSITION = "INSUFFICIENT_POSITION"
    T1_LOCKED = "T1_LOCKED"
    RATE_LIMITED = "RATE_LIMITED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    UNKNOWN_ORDER = "UNKNOWN_ORDER"
    ORDER_NOT_OPEN = "ORDER_NOT_OPEN"
    CANCEL_NOT_ALLOWED = "CANCEL_NOT_ALLOWED"
