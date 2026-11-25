"""The six official teams. Seeded into every season by `Store.ensure_season`."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Persona:
    tag: str
    name: str
    brief: str
    glyph: str        # one character for the avatar
    color: str        # avatar accent
    is_benchmark: bool = False


PERSONAS: tuple[Persona, ...] = (
    Persona("macro_debate", "OpenPup 宏观辩论团",
            "多头、空头、风控看着大盘和日线轮流发言，主席按多数拍板；持仓定期复盘，风控反对就不买。", "辩", "#F5C451"),
    Persona("dividend_conservative", "极度保守红利派",
            "红利低波：沪深300 和上证50 里股息率前 30、波动最小的 10 只，等权持有，每月调仓；当天涨超 1% 不追。", "息", "#7FB2FF"),
    Persona("limit_up_momentum", "激进打板动量狗",
            "只打首板：11 点前封死、封单够厚、不是一字板才排队。明天不连板就走，最多拿 3 天。", "狗", "#FF7AC6"),
    Persona("contrarian", "逆向抄底反指王",
            "超跌反弹：长期趋势还在（200 日线上方），短线跌透（RSI(2) 极低）才在尾盘抄；站上 5 日线或赚 5% 就走。", "反", "#B98CFF"),
    Persona("ma_crossover", "纯均线死板机器",
            "沪深300 成分股，日线 5 日线上穿 20 日线（且 20 日线向上）就买，下穿就卖，每天 14:50 决定。没有感情，只有规则。", "线", "#4FD1C5"),
    Persona("benchmark", "沪深300 躺平基准",
            "第一天全仓买入沪深300ETF，然后什么都不做。其他队伍都得跑赢它。", "躺", "#9AA1AE",
            is_benchmark=True),
)

BY_TAG = {p.tag: p for p in PERSONAS}


@dataclass(frozen=True)
class Badge:
    code: str
    name: str
    desc: str
    glyph: str


BADGES: tuple[Badge, ...] = (
    Badge("first_trade", "首单", "第一笔成交", "首"),
    Badge("zhaban_jiepan", "炸板接盘侠", "排队成交的那一刻，板炸了", "炸"),
    Badge("paile_yitian", "排了一天寂寞", "排了一整天涨停，一股没排到", "寂"),
    Badge("ditianban", "地天板战神", "跌停价买入，当天涨停收盘", "天"),
    Badge("month_best", "月度最佳", "当月收益第一", "月"),
    Badge("season_champion", "赛季冠军", "赛季收益第一", "冠"),
    Badge("season_top3", "赛季前三", "赛季收益前三", "三"),
    Badge("season_beat_index", "跑赢沪深300", "赛季收益跑赢沪深300 躺平基准", "赢"),
)

BADGE_BY_CODE = {b.code: b for b in BADGES}
