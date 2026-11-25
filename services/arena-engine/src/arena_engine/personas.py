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
            "多头、空头、风控轮流发言，主席按多数拍板，风控反对就不买。", "辩", "#F5C451"),
    Persona("dividend_conservative", "极度保守红利派",
            "只买红利股，单只不超过 10%，当天涨超 1% 绝不追。", "息", "#7FB2FF"),
    Persona("limit_up_momentum", "激进打板动量狗",
            "专排封死涨停的票，第二天开盘就卖。成败全看排队进度条。", "狗", "#FF7AC6"),
    Persona("contrarian", "逆向抄底反指王",
            "谁跌超 5% 就买谁，赚 3% 就跑。", "反", "#B98CFF"),
    Persona("ma_crossover", "纯均线死板机器",
            "5 分钟均线上穿 20 分钟均线就买，下穿就卖。没有感情，只有规则。", "线", "#4FD1C5"),
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
)

BADGE_BY_CODE = {b.code: b for b in BADGES}
