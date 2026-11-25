"""Content checks for everything shown publicly: names, briefs, trade rationales, debate text.

Built-in rules target what hurts a paper-trading arena most: ads and contact details, paid stock tips
(this arena never carries paid signals), and obvious abuse. Operators add their own words with
ARENA_BLOCKLIST (a UTF-8 file, one word per line, # comments allowed).
"""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path

PATTERNS = [
    (re.compile(r"(https?://|www\.|\.(com|cn|net|org|top|xyz|io)\b)"), "不能包含网址"),
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "不能包含手机号"),
    (re.compile(r"(微信|vx|v信|wx|qq|扣扣|加群|进群|私聊|私信|联系我|电报|telegram|tg群)"), "不能包含联系方式或引流"),
    (re.compile(r"(荐股|带单|收费群|会员群|内幕消息|稳赚|保本|翻倍股|牛股推荐|老师带|跟单群)"), "不能荐股、带单或宣传收费服务"),
]

BUILTIN_WORDS = ["傻逼", "傻b", "操你", "妈的", "尼玛", "去死", "智障", "脑残", "代开发票", "赌博", "博彩"]


def _normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", text).lower()
    return re.sub(r"[\s\W_]+", "", t, flags=re.UNICODE)


class ContentFilter:
    def __init__(self, blocklist: Path | None = None) -> None:
        words = list(BUILTIN_WORDS)
        if blocklist and blocklist.exists():
            for line in blocklist.read_text(encoding="utf-8").splitlines():
                line = line.split("#", 1)[0].strip()
                if line:
                    words.append(line)
        self.words = sorted({_normalize(w) for w in words if _normalize(w)}, key=len, reverse=True)

    def check(self, text: str | None) -> str | None:
        """Returns a user-facing reason when `text` is not allowed, else None."""
        if not text:
            return None
        raw = unicodedata.normalize("NFKC", text).lower()
        for pattern, reason in PATTERNS:
            if pattern.search(raw) or pattern.search(_normalize(text)):
                return reason
        flat = _normalize(text)
        for w in self.words:
            if w in flat:
                return "包含不允许的词语"
        return None
