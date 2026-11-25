"""Operational alerts to a configurable webhook.

Each alert has a key. It is sent when it starts firing, repeated every `repeat` while it keeps firing, and a
"recovered" message is sent when it clears. Messages start with "[Agora]" so keyword-protected bots (DingTalk,
WeCom, Feishu) accept them.

    ARENA_ALERT_WEBHOOK=https://...      empty = alerts only go to the log
    ARENA_ALERT_FORMAT=generic|wecom|feishu|dingtalk|slack
"""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta

from .market.calendar import CST

log = logging.getLogger(__name__)
FORMATS = ("generic", "wecom", "feishu", "dingtalk", "slack")


def payload(fmt: str, title: str, text: str, level: str, ts: datetime) -> dict:
    line = f"[Agora] {'🔴' if level == 'critical' else '🟡' if level == 'warning' else '🟢'} {title}\n{text}"
    if fmt in ("wecom", "dingtalk"):
        return {"msgtype": "text", "text": {"content": line}}
    if fmt == "feishu":
        return {"msg_type": "text", "content": {"text": line}}
    if fmt == "slack":
        return {"text": line}
    return {"source": "agora", "level": level, "title": title, "text": text, "ts": ts.isoformat()}


@dataclass
class _State:
    since: datetime
    last_sent: datetime


class Alerter:
    def __init__(self, url: str, fmt: str = "generic", repeat: timedelta = timedelta(hours=1)) -> None:
        if fmt not in FORMATS:
            raise ValueError(f"ARENA_ALERT_FORMAT must be one of {FORMATS}")
        self.url, self.fmt, self.repeat = url, fmt, repeat
        self.firing: dict[str, _State] = {}
        self.sent: list[dict] = []          # what went out (tests, and the log)

    def _post(self, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode()
        req = urllib.request.Request(self.url, data=data, method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": "agora-alerts/1"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()

    async def send(self, title: str, text: str, level: str = "warning") -> bool:
        now = datetime.now(CST)
        body = payload(self.fmt, title, text, level, now)
        self.sent.append(body)
        log.warning("ALERT %s: %s | %s", level, title, text.replace("\n", " "))
        if not self.url:
            return False
        try:
            await asyncio.to_thread(self._post, body)
            return True
        except Exception as exc:  # never let alerting take the engine down
            log.error("alert webhook failed: %s", exc)
            return False

    async def update(self, key: str, firing: bool, title: str, text: str, level: str = "warning") -> None:
        now = datetime.now(CST)
        st = self.firing.get(key)
        if firing:
            if st is None:
                self.firing[key] = _State(now, now)
                await self.send(title, text, level)
            elif now - st.last_sent >= self.repeat:
                st.last_sent = now
                mins = int((now - st.since).total_seconds() // 60)
                await self.send(f"{title}（已持续 {mins} 分钟）", text, level)
        elif st is not None:
            del self.firing[key]
            mins = int((now - st.since).total_seconds() // 60)
            await self.send(f"已恢复：{title}", f"持续了约 {mins} 分钟。", "ok")
