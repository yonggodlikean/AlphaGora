"""Daily roast (毒舌复盘), built from the day's facts with templates. No LLM needed; the facts are real."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass
class DayStat:
    portfolio_id: str
    name: str
    day_ret: float          # fraction
    cum_ret: float
    trades: int
    board_breaks: int       # fills tagged board_break
    lonely_queues: int      # limit-up queues that expired with zero fill
    is_benchmark: bool


def _pick(options: list[str], salt: str) -> str:
    h = int(hashlib.sha256(salt.encode()).hexdigest(), 16)
    return options[h % len(options)]


def _p(x: float) -> str:
    return f"{x * 100:+.2f}%"


def build_roast(stats: list[DayStat], trade_date: str) -> dict:
    teams = [s for s in stats if not s.is_benchmark]
    bench = next((s for s in stats if s.is_benchmark), None)
    lines: dict[str, str] = {}
    headline: list[str] = []
    if not teams:
        return {"trade_date": trade_date, "text": "今天擂台上一个人都没有。", "lines": {}}

    best = max(teams, key=lambda s: s.day_ret)
    worst = min(teams, key=lambda s: s.day_ret)
    breakers = sorted((s for s in teams if s.board_breaks), key=lambda s: -s.board_breaks)
    lonely = sorted((s for s in teams if s.lonely_queues), key=lambda s: -s.lonely_queues)
    idle = [s for s in teams if s.trades == 0]

    if breakers:
        s = breakers[0]
        headline.append(f"{s.name}今天炸板接盘 {s.board_breaks} 次，喜提「炸板接盘侠」。")
    if lonely:
        s = lonely[0]
        headline.append(f"{s.name}排了一整天涨停，一股没排到，解锁「排了一天寂寞」。")
    if best.day_ret > 0:
        headline.append(_pick([f"{best.name}今天 {_p(best.day_ret)}，可以嚣张到明天开盘。",
                               f"今日最佳 {best.name}（{_p(best.day_ret)}），建议截图保存，明天可能就没了。"],
                              trade_date + best.portfolio_id))
    if worst.day_ret < 0 and worst is not best:
        headline.append(_pick([f"{worst.name}今天 {_p(worst.day_ret)}，主打一个稳定输出（亏损）。",
                               f"{worst.name}今天 {_p(worst.day_ret)}，策略名建议改成「割肉」系列。"],
                              trade_date + worst.portfolio_id))
    if bench is not None:
        beaten = sum(1 for s in teams if s.cum_ret < bench.cum_ret)
        if beaten:
            headline.append(f"沪深300 躺平基准什么都没做，累计跑赢了 {beaten} 支队伍。")
    if idle and len(headline) < 3:
        headline.append(f"{idle[0].name}今天一笔没动，也算一种实力。")

    for s in stats:
        if s.is_benchmark:
            lines[s.portfolio_id] = "今天依然什么都没做，累计 " + _p(s.cum_ret) + "。"
        elif s.board_breaks:
            lines[s.portfolio_id] = f"排队排到的那一刻正好炸板，今天 {_p(s.day_ret)}。"
        elif s.lonely_queues:
            lines[s.portfolio_id] = "排了一整天，一股没排到。明天继续排？"
        elif s.trades == 0:
            lines[s.portfolio_id] = "今天一笔没动。" + ("躺赢。" if s.day_ret > 0 else "躺输。" if s.day_ret < 0 else "")
        elif s.day_ret >= 0.02:
            lines[s.portfolio_id] = _pick([f"今天 {_p(s.day_ret)}，膨胀警告。", f"今天 {_p(s.day_ret)}，运气也是实力的一部分。"],
                                          trade_date + s.portfolio_id)
        elif s.day_ret <= -0.02:
            lines[s.portfolio_id] = _pick([f"今天 {_p(s.day_ret)}，建议复盘时把灯关上。", f"今天 {_p(s.day_ret)}，问题不大，就是钱没了。"],
                                          trade_date + s.portfolio_id)
        else:
            lines[s.portfolio_id] = f"今天 {_p(s.day_ret)}，不温不火，像极了上班。"

    return {"trade_date": trade_date, "text": "".join(headline[:3]) or "今天风平浪静，没什么好吐槽的。",
            "lines": lines}
