"""Official house teams. Deterministic, run once per market minute. Each strategy is a recognisable real one,
sized and capped like a small fund would, ranked by signal strength (never by ticker order), and published as a
"debate" whose rounds are the checks that fired, with the actual numbers, tagged model_id="rules-v1" so nobody
mistakes them for LLM output.

    红利派   dividend low-volatility (in the spirit of CSI 红利低波): CSI 300 ∪ SSE 50, top 30 by trailing
             dividend yield, then the 10 least volatile; equal weight, rebalanced on the month's first trading day.
    动量狗   first-board limit-up (首板打板): only a stock's first limit-up, sealed before 11:00 with a thick
             seal, never a one-price board; next day it stays only while the board holds, out by day 3.
    反指王   short-term mean reversion (Connors RSI(2)): above the 200-day line but RSI(2) < 10 with -3% in two
             days, or -10% in three,
             decided at 14:45; out on a close above MA5, +5%, 5 days, or -8%.
    均线机器 daily MA5/MA20 crossover on CSI 300 with a rising MA20, decided at 14:50.
    辩论团   (rule fallback when OpenPup is not driving it) trend + not-overheated + risk vote.
    基准     buys the CSI 300 ETF on day one and holds.

Strategies need daily history (`market.history`); without it they stand aside, except the dividend team, which
falls back to its configured list.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import ROUND_DOWN, Decimal

from .domain.enums import OrderType, Side
from .domain.models import Account
from .market.calendar import CST
from .market.history import History, rsi, ret, sma, volatility
from .market.snapshot import L1Snapshot

MODEL = "rules-v1"
ZERO = Decimal("0")


@dataclass
class Intent:
    portfolio_id: str
    symbol: str
    side: Side
    order_type: OrderType
    qty: int
    price: Decimal | None
    title: str
    summary: str
    rounds: list[dict] | None = None     # None -> plain thesis, no debate
    key: str = ""
    cancel_order_id: str | None = None   # set: cancel this open order instead of placing one


@dataclass
class MarketView:
    trade_date: date
    now: datetime
    books: dict[str, L1Snapshot]
    day_open: dict[str, Decimal]
    minute_closes: dict[str, list[Decimal]]
    names: dict[str, str]
    history: History | None = None
    held_days: dict[tuple[str, str], int] = field(default_factory=dict)   # (portfolio, symbol) -> trading days held
    pending_buys: dict[str, dict[str, str]] = field(default_factory=dict)  # portfolio -> {symbol: open buy order id}


def _r(role: str, stance: str, argument: str) -> dict:
    return {"role": role, "stance": stance, "argument": argument, "model_id": MODEL}


def _pct(x: Decimal | float | None) -> str:
    return "—" if x is None else f"{float(x) * 100:+.2f}%"


def is_stock(sym: str) -> bool:
    """Everything the feed carries except funds (SH 5xxxxx, SZ 15/16xxxx). Demo codes (99xxxx) count as stocks."""
    code, _, ex = sym.partition(".")
    return not ((ex == "SH" and code.startswith("5")) or (ex == "SZ" and code[:2] in ("15", "16")))


@dataclass
class HouseBots:
    teams: dict[str, str]                 # portfolio_id -> persona tag
    benchmark_symbol: str
    dividend_symbols: tuple[str, ...]     # fallback list when there is no dividend history
    _counter: dict[tuple, int] = field(default_factory=lambda: defaultdict(int))
    _acted: set[tuple] = field(default_factory=set)
    _day: date | None = None
    _closes: dict[str, list[float]] = field(default_factory=dict)     # today's history slice per symbol
    _first_seal: dict[str, time] = field(default_factory=dict)
    _div_target: list[str] | None = None
    _div_info: dict[str, tuple[float, float]] = field(default_factory=dict)
    _pending: dict[str, dict[str, str]] = field(default_factory=dict)

    # -------------------------------------------------------------- helpers
    def _reset(self, d: date) -> None:
        if self._day != d:
            self._day = d
            self._counter.clear()
            self._acted.clear()
            self._closes.clear()
            self._first_seal.clear()
            self._div_target = None
            self._div_info = {}

    def _key(self, tag: str, d: date, sym: str, side: Side) -> str:
        k = (tag, sym, side.value)
        self._counter[k] += 1
        return f"house:{tag}:{d.isoformat()}:{sym}:{side.value}:{self._counter[k]}"

    @staticmethod
    def equity(acct: Account, books: dict[str, L1Snapshot]) -> Decimal:
        eq = acct.cash
        for p in acct.positions.values():
            b = books.get(p.symbol)
            eq += (b.last_price if b else (p.cost_basis / p.qty if p.qty else ZERO)) * p.qty
        return eq

    @staticmethod
    def lots_for(value: Decimal, price: Decimal) -> int:
        if price <= 0 or value <= 0:
            return 0
        return int((value / (price * 100)).to_integral_value(rounding=ROUND_DOWN)) * 100

    @staticmethod
    def chg(b: L1Snapshot) -> Decimal:
        return b.last_price / b.prev_close - 1

    def _budget(self, acct: Account, eq: Decimal, frac: str) -> Decimal:
        return min(eq * Decimal(frac), acct.available_cash * Decimal("0.98"))

    def _sellable(self, acct: Account, sym: str) -> int:
        p = acct.positions.get(sym)
        return p.sellable_qty if p else 0

    def _owned(self, acct: Account, sym: str) -> bool:
        """Held, or already waiting on a buy order: never bought twice."""
        p = acct.positions.get(sym)
        return bool(p and p.qty) or sym in self._pending.get(acct.portfolio_id, {})

    @staticmethod
    def _holdings(acct: Account) -> int:
        """Only filled positions count toward a team's cap; a queued bid may never fill."""
        return sum(1 for p in acct.positions.values() if p.qty)

    def _series(self, mv: MarketView, sym: str, b: L1Snapshot) -> list[float] | None:
        """Daily closes before today plus today's live price as the forming bar."""
        if mv.history is None:
            return None
        if sym not in self._closes:
            bars = mv.history.bars.get(sym)
            self._closes[sym] = list(bars.before(mv.trade_date).close[-201:]) if bars else []
        hist = self._closes[sym]
        return [*hist, float(b.last_price)] if hist else None

    def _name(self, mv: MarketView, sym: str) -> str:
        return mv.names.get(sym, sym)

    def _st(self, mv: MarketView, sym: str) -> bool:
        return "ST" in mv.names.get(sym, "").upper()

    def _candidates(self, mv: MarketView, pool: set[str] | None = None) -> list[L1Snapshot]:
        return [b for s, b in mv.books.items()
                if (pool is None or s in pool) and s != self.benchmark_symbol and is_stock(s)
                and not b.suspended and not self._st(mv, s)]

    # -------------------------------------------------------------- entry point
    def decide(self, mv: MarketView, accounts: dict[str, Account]) -> list[Intent]:
        self._reset(mv.trade_date)
        self._pending = {pid: dict(orders) for pid, orders in mv.pending_buys.items()}
        now = mv.now.astimezone(CST).time()
        for s, b in mv.books.items():              # when each board first sealed today (minute resolution)
            if b.limit_up_sealed and s not in self._first_seal:
                self._first_seal[s] = now
        if not (time(9, 31) <= now < time(14, 57)):
            return []
        out: list[Intent] = []
        for pid, tag in self.teams.items():
            acct = accounts.get(pid)
            if acct is None:
                continue
            fn = getattr(self, f"_{tag}", None)
            if fn is not None:
                intents = fn(pid, tag, acct, mv, now)
                out += [it for it in intents if it.cancel_order_id]          # cancels are not rationed
                intents = [it for it in intents if not it.cancel_order_id]
                for it in intents[2:]:      # at most two actions per team per minute; the rest try next minute
                    self._acted.discard((tag, it.symbol, it.side.value))
                for it in intents[:2]:
                    it.key = self._key(tag, mv.trade_date, it.symbol, it.side)
                    out.append(it)
        return out

    # -------------------------------------------------------------- 红利低波
    def _dividend_targets(self, mv: MarketView) -> list[tuple[str, float, float]]:
        """[(symbol, dividend yield, annual volatility)] — the month's target list, computed once a day."""
        h = mv.history
        rows = []
        if h is not None:
            for sym in h.member_of("000300", "000016"):
                bars = h.bars.get(sym)
                b = mv.books.get(sym)
                if bars is None or b is None or not is_stock(sym) or self._st(mv, sym) or bars.div_ttm <= 0:
                    continue
                c = self._series(mv, sym, b)
                vol = volatility(c, 60) if c else None
                if vol is None:
                    continue
                rows.append((sym, bars.div_ttm / float(b.last_price), vol))
        if len(rows) < 10:                         # no usable history: the configured list, equal weight
            return [(s, 0.0, 0.0) for s in self.dividend_symbols if s in mv.books]
        top_yield = sorted(rows, key=lambda r: -r[1])[:30]
        return sorted(top_yield, key=lambda r: r[2])[:10]

    def _dividend_conservative(self, pid: str, tag: str, acct: Account, mv: MarketView, now: time) -> list[Intent]:
        if self._div_target is None:
            rows = self._dividend_targets(mv)
            if not rows:
                return []                  # no quotes for the list yet: try again next minute, don't cache it
            self._div_target = [s for s, _, _ in rows]
            self._div_info = {s: (y, v) for s, y, v in rows}
        info, target = self._div_info, self._div_target
        eq = self.equity(acct, mv.books)
        out: list[Intent] = []
        # Rebalance on the month's first trading day: sell what fell out of the list.
        h = mv.history
        last_bar = None
        if h is not None:
            any_bars = next(iter(h.bars.values()), None)
            if any_bars is not None:
                prev = any_bars.before(mv.trade_date).dates
                last_bar = prev[-1] if prev else None
        first_of_month = last_bar is not None and (last_bar // 100) % 100 != mv.trade_date.month
        if first_of_month:
            for sym, p in sorted(acct.positions.items()):
                b = mv.books.get(sym)
                n = self._sellable(acct, sym)
                if sym in target or n <= 0 or b is None or b.bid1 is None or (tag, sym, "sell") in self._acted:
                    continue
                self._acted.add((tag, sym, "sell"))
                out.append(Intent(pid, sym, Side.SELL, OrderType.LIMIT, n, b.bid1.price,
                                  f"{self._name(mv, sym)}：月度调仓卖出", "不在本月红利低波名单里，卖出。",
                                  [_r("调仓", "sell", "月初调仓：股息率或波动率排名已掉出前 10。")]))
        held = self._holdings(acct)
        for sym in target:
            b = mv.books.get(sym)
            if b is None or b.ask1 is None or (tag, sym, "buy") in self._acted:
                continue
            pos = acct.positions.get(sym)
            held_value = b.last_price * pos.qty if pos else ZERO
            chg = self.chg(b)
            if held_value >= eq * Decimal("0.09") or chg > Decimal("0.01"):
                continue
            if not (pos and pos.qty) and held >= 10:
                continue
            qty = self.lots_for(min(eq * Decimal("0.10") - held_value, acct.available_cash * Decimal("0.98")),
                                b.ask1.price)
            if qty == 0:
                continue
            self._acted.add((tag, sym, "buy"))
            y, v = info.get(sym, (0.0, 0.0))
            rounds = [_r("股息", "buy", f"近 12 个月股息率 {y * 100:.2f}%，在沪深300/上证50 里排前 30。" if y
                         else "在红利名单里。"),
                      _r("波动", "buy", f"60 日年化波动 {v * 100:.1f}%，是高股息里最稳的 10 只之一。" if v
                         else "低波动红利股。"),
                      _r("纪律", "buy", f"今日 {_pct(chg)}，没超过 +1%，不算追高；补到 10%。")]
            out.append(Intent(pid, sym, Side.BUY, OrderType.LIMIT, qty, b.ask1.price,
                              f"{self._name(mv, sym)}：补到 10%", "红利低波名单，等权补仓到 10%。", rounds))
        return out

    # -------------------------------------------------------------- 首板打板
    def _limit_up_momentum(self, pid: str, tag: str, acct: Account, mv: MarketView, now: time) -> list[Intent]:
        eq = self.equity(acct, mv.books)
        out: list[Intent] = []
        for sym, p in sorted(acct.positions.items()):
            b = mv.books.get(sym)
            n = self._sellable(acct, sym)
            if b is None or n <= 0 or (tag, sym, "sell") in self._acted:
                continue
            days = mv.held_days.get((pid, sym), 1)
            if b.limit_up_sealed and not (days >= 2 and now >= time(14, 50)):
                continue                           # the board holds: keep riding it
            if b.bid1 is None:
                continue                           # sealed limit-down: nobody to sell to
            self._acted.add((tag, sym, "sell"))
            why = (f"持有第 {days} 天，到期清仓。" if b.limit_up_sealed
                   else f"今天没封住涨停（{_pct(self.chg(b))}），不连板就走。")
            out.append(Intent(pid, sym, Side.SELL, OrderType.LIMIT, n, b.bid1.price,
                              f"{self._name(mv, sym)}：不板就走", why, [_r("纪律", "sell", why)]))
        holding = self._holdings(acct)
        if holding >= 3:
            # the cap is met with filled boards: stop queuing for more
            for sym, oid in sorted(self._pending.get(pid, {}).items()):
                if not (acct.positions.get(sym) and acct.positions[sym].qty):
                    out.append(Intent(pid, sym, Side.BUY, OrderType.LIMIT, 0, None, f"{self._name(mv, sym)}：撤单",
                                      "已经排到 3 个板，撤掉其余排队。", cancel_order_id=oid))
            return out
        if now >= time(11, 0) or mv.history is None:
            return out
        picks = []
        for b in self._candidates(mv):
            sym = b.symbol
            if not b.limit_up_sealed or (tag, sym, "buy") in self._acted or self._owned(acct, sym):
                continue
            bars = mv.history.bars.get(sym)
            opened = mv.day_open.get(sym)
            if bars is None or opened is None or opened >= b.upper_limit:
                continue                           # no history, or a one-price board nobody can get into
            hist = bars.before(mv.trade_date)
            if not hist.limit_up or hist.limit_up[-1]:
                continue                           # not the first board
            seal_amt = float(b.bids[0].volume * b.upper_limit)
            float_cap = hist.float_shares * float(b.upper_limit)
            ratio = seal_amt / float_cap if float_cap > 0 else 0.0
            if seal_amt < 1e8 and ratio < 0.02:
                continue
            picks.append((ratio, seal_amt, b))
        for ratio, seal_amt, b in sorted(picks, key=lambda x: (-x[0], -x[1])):
            if holding >= 3:
                break
            qty = self.lots_for(self._budget(acct, eq, "0.15"), b.upper_limit)
            if qty == 0:
                continue
            sym = b.symbol
            self._acted.add((tag, sym, "buy"))
            holding += 1
            sealed_at = self._first_seal.get(sym, now).strftime("%H:%M")
            rounds = [
                _r("多头", "buy", f"首板，{sealed_at} 封死，封单 {seal_amt / 1e8:.2f} 亿元"
                                  + (f"，占流通市值 {ratio * 100:.1f}%。" if ratio else "。")),
                _r("空头", "hold", "封单够厚、不是一字板，排得进去；炸板就认。"),
                _r("风控", "buy", "仓位 15%，最多同时 3 只；明天不连板就走。"),
                _r("主席", "buy", "按人设执行：打。"),
            ]
            out.append(Intent(pid, sym, Side.BUY, OrderType.LIMIT, qty, b.upper_limit,
                              f"{self._name(mv, sym)}：打首板", f"涨停价排队 {qty // 100} 手。", rounds))
        return out

    # -------------------------------------------------------------- RSI(2) 均值回归
    def _contrarian(self, pid: str, tag: str, acct: Account, mv: MarketView, now: time) -> list[Intent]:
        eq = self.equity(acct, mv.books)
        out: list[Intent] = []
        late = now >= time(14, 45)
        for sym, p in sorted(acct.positions.items()):
            b = mv.books.get(sym)
            n = self._sellable(acct, sym)
            if b is None or n <= 0 or b.bid1 is None or not p.qty or (tag, sym, "sell") in self._acted:
                continue
            cost = p.cost_basis / p.qty
            gain = b.last_price / cost - 1
            c = self._series(mv, sym, b)
            ma5 = sma(c, 5) if c else None
            days = mv.held_days.get((pid, sym), 1)
            why = None
            if gain <= Decimal("-0.08"):
                why = f"相对成本 {_pct(gain)}，跌破 -8% 止损。"
            elif late and gain >= Decimal("0.05"):
                why = f"相对成本 {_pct(gain)}，反弹到 +5%，走。"
            elif late and ma5 is not None and float(b.last_price) > ma5:
                why = f"收盘前站上 5 日线（{ma5:.2f}），反弹兑现。"
            elif late and days >= 5:
                why = f"拿了 {days} 天还没弹起来，时间止损。"
            if why:
                self._acted.add((tag, sym, "sell"))
                out.append(Intent(pid, sym, Side.SELL, OrderType.LIMIT, n, b.bid1.price,
                                  f"{self._name(mv, sym)}：{'认错' if gain < 0 else '兑现'}", why,
                                  [_r("纪律", "sell", why)]))
        if not late or mv.history is None:
            return out
        picks = []
        for b in self._candidates(mv):
            sym = b.symbol
            if (b.limit_down_sealed or b.ask1 is None or (tag, sym, "buy") in self._acted
                    or self._owned(acct, sym)):
                continue
            c = self._series(mv, sym, b)
            if c is None or len(c) < 201:
                continue
            ma200 = sma(c, 200)
            r2, d2, r3 = rsi(c, 2), ret(c, 2), ret(c, 3)
            if ma200 is None or c[-1] <= ma200 or r2 is None or d2 is None or r3 is None:
                continue
            # RSI(2) alone fires on any two red days of a sleepy stock: require a real drop as well.
            if (r2 < 10 and d2 <= -0.03) or r3 <= -0.10:
                picks.append((r2, r3, c[-1] / ma200 - 1, b))
        holding = self._holdings(acct)
        for r2, r3, above, b in sorted(picks, key=lambda x: (x[0], x[1])):
            if holding >= 5:
                break
            qty = self.lots_for(self._budget(acct, eq, "0.10"), b.ask1.price)
            if qty == 0:
                continue
            self._acted.add((tag, b.symbol, "buy"))
            holding += 1
            out.append(Intent(pid, b.symbol, Side.BUY, OrderType.LIMIT, qty, b.ask1.price,
                              f"{self._name(mv, b.symbol)}：超跌，抄？", "长期趋势还在，短线跌透，买 10%。",
                              [_r("反指", "buy", f"RSI(2) {r2:.1f}，3 天 {_pct(r3)}，恐慌盘出来了。"),
                               _r("趋势", "buy", f"仍在 200 日线上方 {above * 100:.1f}%，长期没坏。"),
                               _r("风控", "hold", "单只 10%，最多 5 只；站上 5 日线或 +5% 就走，-8% 止损，最多拿 5 天。")]))
        return out

    # -------------------------------------------------------------- 日线双均线
    def _ma_crossover(self, pid: str, tag: str, acct: Account, mv: MarketView, now: time) -> list[Intent]:
        if now < time(14, 50) or mv.history is None:
            return []
        eq = self.equity(acct, mv.books)
        out: list[Intent] = []
        for sym, p in sorted(acct.positions.items()):
            b = mv.books.get(sym)
            n = self._sellable(acct, sym)
            if b is None or n <= 0 or b.bid1 is None or (tag, sym, "sell") in self._acted:
                continue
            c = self._series(mv, sym, b)
            ma5, ma20 = (sma(c, 5), sma(c, 20)) if c else (None, None)
            if ma5 is None or ma20 is None or ma5 >= ma20:
                continue
            self._acted.add((tag, sym, "sell"))
            arg = f"MA5 {ma5:.2f} 跌到 MA20 {ma20:.2f} 下方，死叉。"
            out.append(Intent(pid, sym, Side.SELL, OrderType.LIMIT, n, b.bid1.price,
                              f"{self._name(mv, sym)}：死叉", "死叉，全部卖出。", [_r("规则", "sell", arg)]))
        picks = []
        for b in self._candidates(mv, mv.history.member_of("000300")):
            sym = b.symbol
            if b.ask1 is None or (tag, sym, "buy") in self._acted or self._owned(acct, sym):
                continue
            c = self._series(mv, sym, b)
            if c is None or len(c) < 22:
                continue
            ma5, ma20, ma5_y, ma20_y = sma(c, 5), sma(c, 20), sma(c, 5, 1), sma(c, 20, 1)
            if ma5_y <= ma20_y and ma5 > ma20 and ma20 > ma20_y:
                picks.append((ret(c, 20) or 0.0, ma5, ma20, b))
        holding = self._holdings(acct)
        for r20, ma5, ma20, b in sorted(picks, key=lambda x: -x[0]):
            if holding >= 10:
                break
            qty = self.lots_for(self._budget(acct, eq, "0.10"), b.ask1.price)
            if qty == 0:
                continue
            self._acted.add((tag, b.symbol, "buy"))
            holding += 1
            arg = f"MA5 {ma5:.2f} 上穿 MA20 {ma20:.2f}，MA20 向上；20 日 {_pct(r20)}。"
            out.append(Intent(pid, b.symbol, Side.BUY, OrderType.LIMIT, qty, b.ask1.price,
                              f"{self._name(mv, b.symbol)}：金叉", "日线金叉，买入 10%。", [_r("规则", "buy", arg)]))
        return out

    # -------------------------------------------------------------- 辩论团（规则版）
    def _macro_debate(self, pid: str, tag: str, acct: Account, mv: MarketView, now: time) -> list[Intent]:
        if mv.history is None:
            return []
        eq = self.equity(acct, mv.books)
        out: list[Intent] = []
        for sym, p in sorted(acct.positions.items()):
            b = mv.books.get(sym)
            n = self._sellable(acct, sym)
            if b is None or n <= 0 or b.bid1 is None or not p.qty or (tag, sym, "sell") in self._acted:
                continue
            gain = b.last_price * p.qty / p.cost_basis - 1 if p.cost_basis else ZERO
            c = self._series(mv, sym, b)
            ma20 = sma(c, 20) if c else None
            broke = ma20 is not None and float(b.last_price) < ma20 * 0.98
            if not (broke or gain > Decimal("0.15") or gain < Decimal("-0.08")):
                continue
            self._acted.add((tag, sym, "sell"))
            why = ("跌破 20 日线，趋势坏了。" if broke else f"浮盈 {_pct(gain)}，落袋。" if gain > 0
                   else f"浮亏 {_pct(gain)}，止损。")
            out.append(Intent(pid, sym, Side.SELL, OrderType.LIMIT, n, b.bid1.price,
                              f"{self._name(mv, sym)}：走不走？", why,
                              [_r("多头", "hold", "再给点时间。"), _r("空头", "sell", why), _r("风控", "sell", why),
                               _r("主席", "sell", "2:1，卖出可卖仓位。")]))
        risk_ok = acct.available_cash / eq > Decimal("0.3") and self._holdings(acct) < 4 if eq > 0 else False
        if not risk_ok:
            return out
        picks = []
        for b in self._candidates(mv):
            sym = b.symbol
            if b.ask1 is None or (tag, sym, "buy") in self._acted or self._owned(acct, sym):
                continue
            c = self._series(mv, sym, b)
            if c is None or len(c) < 61:
                continue
            ma20, ma60, r20, r5 = sma(c, 20), sma(c, 60), ret(c, 20), ret(c, 5)
            chg = self.chg(b)
            bull = c[-1] > ma20 > ma60 and (r20 or 0) > 0.05
            calm = chg < Decimal("0.06") and (r5 or 0) < 0.15
            if bull and calm:
                picks.append((r20, ma20, ma60, chg, b))
        for r20, ma20, ma60, chg, b in sorted(picks, key=lambda x: -x[0])[:1]:
            qty = self.lots_for(self._budget(acct, eq, "0.15"), b.ask1.price)
            if qty == 0:
                continue
            self._acted.add((tag, b.symbol, "buy"))
            rounds = [
                _r("多头", "buy", f"站上 20 日线和 60 日线，20 日 {_pct(r20)}，趋势最强之一。"),
                _r("空头", "hold", f"今日 {_pct(chg)}，还不算过热，不反对。"),
                _r("风控", "buy", "现金充足，仓位 15% 可以。"),
                _r("主席", "buy", f"3:0 通过，买入 {qty // 100} 手。"),
            ]
            out.append(Intent(pid, b.symbol, Side.BUY, OrderType.LIMIT, qty, b.ask1.price,
                              f"{self._name(mv, b.symbol)}：跟不跟趋势？", "3:0 通过，按卖一价买入 15% 仓位。", rounds))
        return out

    # -------------------------------------------------------------- 基准
    def _benchmark(self, pid: str, tag: str, acct: Account, mv: MarketView, now: time) -> list[Intent]:
        b = mv.books.get(self.benchmark_symbol)
        if b is None or b.ask1 is None:
            return []
        eq = self.equity(acct, mv.books)
        if acct.available_cash < eq * Decimal("0.02"):
            return []
        # Market buys reserve at the upper limit, so size against it.
        qty = self.lots_for(acct.available_cash * Decimal("0.995") - 10, b.upper_limit * Decimal("1.0003"))
        if qty == 0:
            return []
        return [Intent(pid, self.benchmark_symbol, Side.BUY, OrderType.MARKET, qty, None,
                       "全仓买入基准", "躺平：把现金全部换成沪深300ETF，然后什么都不做。")]
