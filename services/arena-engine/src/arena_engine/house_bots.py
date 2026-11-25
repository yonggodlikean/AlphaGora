"""Official house teams. Deterministic rules, run once per market minute. Each action is published as a
"debate" whose rounds are the rule checks that fired, tagged model_id="rules-v1" so nobody mistakes them for
LLM output. Community OpenPup teams post real multi-agent debates through the API instead."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import ROUND_DOWN, Decimal

from .domain.enums import OrderType, Side
from .domain.models import Account
from .market.calendar import CST
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


@dataclass
class MarketView:
    trade_date: date
    now: datetime
    books: dict[str, L1Snapshot]
    day_open: dict[str, Decimal]
    minute_closes: dict[str, list[Decimal]]
    names: dict[str, str]


def _r(role: str, stance: str, argument: str) -> dict:
    return {"role": role, "stance": stance, "argument": argument, "model_id": MODEL}


def _pct(x: Decimal | float) -> str:
    return f"{float(x) * 100:+.2f}%"


@dataclass
class HouseBots:
    teams: dict[str, str]                 # portfolio_id -> persona tag
    benchmark_symbol: str
    dividend_symbols: tuple[str, ...]
    _counter: dict[tuple, int] = field(default_factory=lambda: defaultdict(int))
    _acted: set[tuple] = field(default_factory=set)
    _ma_rel: dict[tuple[str, str], str] = field(default_factory=dict)
    _day: date | None = None

    # -------------------------------------------------------------- helpers
    def _reset(self, d: date) -> None:
        if self._day != d:
            self._day = d
            self._counter.clear()
            self._acted.clear()

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

    # -------------------------------------------------------------- entry point
    def decide(self, mv: MarketView, accounts: dict[str, Account]) -> list[Intent]:
        self._reset(mv.trade_date)
        if not (time(9, 31) <= mv.now.astimezone(CST).time() < time(14, 57)):
            return []
        out: list[Intent] = []
        for pid, tag in self.teams.items():
            acct = accounts.get(pid)
            if acct is None:
                continue
            fn = getattr(self, f"_{tag}", None)
            if fn is not None:
                for it in fn(pid, tag, acct, mv)[:2]:   # at most two actions per team per minute
                    it.key = self._key(tag, mv.trade_date, it.symbol, it.side)
                    out.append(it)
        return out

    def _tradeable(self, mv: MarketView) -> list[L1Snapshot]:
        return [b for s, b in sorted(mv.books.items()) if s != self.benchmark_symbol and not b.suspended]

    def _sellable(self, acct: Account, sym: str) -> int:
        p = acct.positions.get(sym)
        return p.sellable_qty if p else 0

    # -------------------------------------------------------------- personas
    def _macro_debate(self, pid: str, tag: str, acct: Account, mv: MarketView) -> list[Intent]:
        eq = self.equity(acct, mv.books)
        out: list[Intent] = []
        for b in self._tradeable(mv):
            sym, name = b.symbol, mv.names.get(b.symbol, b.symbol)
            chg = self.chg(b)
            opened = mv.day_open.get(sym, b.last_price)
            held = acct.positions.get(sym)
            if held and held.qty and self._sellable(acct, sym) > 0:
                gain = b.last_price * held.qty / held.cost_basis - 1 if held.cost_basis else ZERO
                bear = chg < Decimal("-0.03")
                take = gain > Decimal("0.08")
                if (bear or take) and (tag, sym, "sell") not in self._acted:
                    self._acted.add((tag, sym, "sell"))
                    rounds = [
                        _r("多头", "hold", f"持仓浮盈 {_pct(gain)}，趋势还在。" if not bear else "跌得有点多，但还没破位。"),
                        _r("空头", "sell", f"今日 {_pct(chg)}，先走为敬。" if bear else f"浮盈 {_pct(gain)} 已经够了，落袋。"),
                        _r("风控", "sell", "触发止损线 -3%。" if bear else "触发止盈线 +8%。"),
                        _r("主席", "sell", "2:1，卖出全部可卖仓位。"),
                    ]
                    out.append(Intent(pid, sym, Side.SELL, OrderType.LIMIT, self._sellable(acct, sym),
                                      b.bid1.price if b.bid1 else b.lower_limit,
                                      f"{name}：止盈还是止损？", "2:1 通过，卖出可卖仓位。", rounds))
                continue
            if held and held.qty:
                continue
            bull = Decimal("0.005") < chg < Decimal("0.05") and b.last_price > opened
            bear_ok = not (chg > Decimal("0.06") or b.last_price < opened)
            risk_ok = acct.available_cash / eq > Decimal("0.3") and sum(1 for p in acct.positions.values() if p.qty) < 4
            votes = sum((bull, bear_ok, risk_ok))
            if not bull or votes < 2 or (tag, sym, "buy") in self._acted or b.ask1 is None:
                continue
            qty = self.lots_for(self._budget(acct, eq, "0.15"), b.ask1.price)
            if qty == 0:
                continue
            self._acted.add((tag, sym, "buy"))
            rounds = [
                _r("多头", "buy", f"今日 {_pct(chg)}，站上开盘价，资金在流入。"),
                _r("空头", "hold" if bear_ok else "sell", "涨幅还不算过热，不反对。" if bear_ok else "已经涨太多 / 跌破开盘价，不建议追。"),
                _r("风控", "buy" if risk_ok else "hold", "现金充足，仓位 15% 可以。" if risk_ok else "现金或持仓数已到上限。"),
                _r("主席", "buy", f"{votes}:{3 - votes} 通过，买入 {qty // 100} 手。"),
            ]
            out.append(Intent(pid, sym, Side.BUY, OrderType.LIMIT, qty, b.ask1.price,
                              f"{name}：追不追？", f"{votes}:{3 - votes} 通过，按卖一价买入 15% 仓位。", rounds))
        return out

    def _dividend_conservative(self, pid: str, tag: str, acct: Account, mv: MarketView) -> list[Intent]:
        eq = self.equity(acct, mv.books)
        out: list[Intent] = []
        for sym in self.dividend_symbols:
            b = mv.books.get(sym)
            if b is None or b.ask1 is None or (tag, sym, "buy") in self._acted:
                continue
            held = acct.positions.get(sym)
            held_value = b.last_price * held.qty if held else ZERO
            chg = self.chg(b)
            if held_value >= eq * Decimal("0.09") or chg > Decimal("0.01"):
                continue
            qty = self.lots_for(min(eq * Decimal("0.10") - held_value, acct.available_cash * Decimal("0.98")), b.ask1.price)
            if qty == 0:
                continue
            self._acted.add((tag, sym, "buy"))
            name = mv.names.get(sym, sym)
            rounds = [
                _r("纪律", "buy", f"今日 {_pct(chg)}，没超过 +1%，不算追高。"),
                _r("仓位", "buy", f"单只上限 10%，现在只有 {float(held_value / eq) * 100:.1f}%。"),
            ]
            out.append(Intent(pid, sym, Side.BUY, OrderType.LIMIT, qty, b.ask1.price,
                              f"{name}：补到 10%", "符合红利纪律，补仓到 10%。", rounds))
        return out

    def _limit_up_momentum(self, pid: str, tag: str, acct: Account, mv: MarketView) -> list[Intent]:
        eq = self.equity(acct, mv.books)
        out: list[Intent] = []
        for sym, p in sorted(acct.positions.items()):
            b = mv.books.get(sym)
            n = self._sellable(acct, sym)
            if b and n > 0 and b.bid1 and (tag, sym, "sell") not in self._acted:
                self._acted.add((tag, sym, "sell"))
                name = mv.names.get(sym, sym)
                out.append(Intent(pid, sym, Side.SELL, OrderType.MARKET, n, None,
                                  f"{name}：次日开盘卖", "打板第二天，不管涨跌，开盘就卖。",
                                  [_r("规则", "sell", f"昨天打板买入，今天 {_pct(self.chg(b))}，按纪律卖出。")]))
        holding = sum(1 for p in acct.positions.values() if p.qty)
        for b in self._tradeable(mv):
            sym = b.symbol
            if not b.limit_up_sealed or (tag, sym, "buy") in self._acted or holding >= 3:
                continue
            if acct.positions.get(sym) and acct.positions[sym].qty:
                continue
            qty = self.lots_for(self._budget(acct, eq, "0.15"), b.upper_limit)
            if qty == 0:
                continue
            self._acted.add((tag, sym, "buy"))
            holding += 1
            seal_lots = b.bids[0].volume // 100
            weak = seal_lots < 50_000
            name = mv.names.get(sym, sym)
            rounds = [
                _r("多头", "buy", f"封死涨停，封单 {seal_lots:,} 手，必须排。"),
                _r("空头", "sell" if weak else "hold",
                   "封单不到 5 万手，随时可能炸，排到就是接盘。" if weak else "封单够厚，不反对。"),
                _r("风控", "buy", "仓位 15%，排不到就算了。"),
                _r("主席", "buy", "按人设执行：打。"),
            ]
            out.append(Intent(pid, sym, Side.BUY, OrderType.LIMIT, qty, b.upper_limit,
                              f"{name}：打不打这个板？", f"涨停价排队 {qty // 100} 手。", rounds))
        return out

    def _contrarian(self, pid: str, tag: str, acct: Account, mv: MarketView) -> list[Intent]:
        eq = self.equity(acct, mv.books)
        out: list[Intent] = []
        for b in self._tradeable(mv):
            sym, name = b.symbol, mv.names.get(b.symbol, b.symbol)
            held = acct.positions.get(sym)
            n = self._sellable(acct, sym)
            if held and held.qty:
                cost = held.cost_basis / held.qty
                if n > 0 and b.bid1 and (b.last_price >= cost * Decimal("1.03") or b.last_price <= cost * Decimal("0.92")) \
                        and (tag, sym, "sell") not in self._acted:
                    self._acted.add((tag, sym, "sell"))
                    win = b.last_price >= cost
                    out.append(Intent(pid, sym, Side.SELL, OrderType.LIMIT, n, b.bid1.price,
                                      f"{name}：{'赚 3% 就跑' if win else '认错止损'}",
                                      "反弹到 +3%，跑。" if win else "跌破 -8%，认错。",
                                      [_r("规则", "sell", f"相对成本 {_pct(b.last_price / cost - 1)}。")]))
                continue
            chg = self.chg(b)
            if chg > Decimal("-0.05") or b.limit_down_sealed or b.ask1 is None or (tag, sym, "buy") in self._acted:
                continue
            qty = self.lots_for(self._budget(acct, eq, "0.10"), b.ask1.price)
            if qty == 0:
                continue
            self._acted.add((tag, sym, "buy"))
            out.append(Intent(pid, sym, Side.BUY, OrderType.LIMIT, qty, b.ask1.price,
                              f"{name}：跌了 {_pct(chg)}，抄？", "别人恐惧我贪婪，买 10%。",
                              [_r("反指", "buy", f"今日 {_pct(chg)}，恐慌盘出来了。"),
                               _r("风控", "hold", "单只 10%，跌破 -8% 止损。")]))
        return out

    def _ma_crossover(self, pid: str, tag: str, acct: Account, mv: MarketView) -> list[Intent]:
        eq = self.equity(acct, mv.books)
        out: list[Intent] = []
        for b in self._tradeable(mv):
            sym = b.symbol
            closes = mv.minute_closes.get(sym, [])
            if len(closes) < 20:
                continue
            ma5 = sum(closes[-5:]) / 5
            ma20 = sum(closes[-20:]) / 20
            rel = "above" if ma5 > ma20 else "below"
            prev = self._ma_rel.get((pid, sym))
            self._ma_rel[(pid, sym)] = rel
            if prev is None or prev == rel:
                continue
            name = mv.names.get(sym, sym)
            held = acct.positions.get(sym)
            arg = f"MA5 {ma5:.3f} {'上穿' if rel == 'above' else '下穿'} MA20 {ma20:.3f}。"
            if rel == "above" and not (held and held.qty) and b.ask1:
                qty = self.lots_for(self._budget(acct, eq, "0.10"), b.ask1.price)
                if qty:
                    out.append(Intent(pid, sym, Side.BUY, OrderType.LIMIT, qty, b.ask1.price,
                                      f"{name}：金叉", "金叉买入 10%。", [_r("规则", "buy", arg)]))
            elif rel == "below" and self._sellable(acct, sym) > 0 and b.bid1:
                out.append(Intent(pid, sym, Side.SELL, OrderType.LIMIT, self._sellable(acct, sym), b.bid1.price,
                                  f"{name}：死叉", "死叉全部卖出。", [_r("规则", "sell", arg)]))
        return out

    def _benchmark(self, pid: str, tag: str, acct: Account, mv: MarketView) -> list[Intent]:
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
