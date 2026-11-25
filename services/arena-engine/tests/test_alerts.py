"""Alert lifecycle, health checks (gateway heartbeat, feed stall, team offline, disk) and webhook delivery."""
from __future__ import annotations

import dataclasses
import http.server
import json
import threading
from datetime import datetime, timedelta

import pytest

from test_service_api import anyio_backend, boot, dsn, pg  # noqa: F401
from arena_engine.alerts import Alerter, payload
from arena_engine.market.calendar import CST


def test_payload_formats():
    ts = datetime(2026, 11, 26, 10, 0, tzinfo=CST)
    assert payload("wecom", "标题", "说明", "critical", ts)["text"]["content"].startswith("[Agora] 🔴 标题")
    assert payload("feishu", "标题", "说明", "warning", ts)["content"]["text"].startswith("[Agora] 🟡")
    assert payload("slack", "t", "x", "ok", ts) == {"text": "[Agora] 🟢 t\nx"}
    assert payload("generic", "t", "x", "ok", ts)["source"] == "agora"
    with pytest.raises(ValueError):
        Alerter("", "telegram")


@pytest.mark.anyio
async def test_fire_once_repeat_and_recover():
    a = Alerter("", "generic", repeat=timedelta(minutes=60))
    await a.update("k", True, "行情断流", "x")
    await a.update("k", True, "行情断流", "x")            # still firing: no duplicate
    assert [m["title"] for m in a.sent] == ["行情断流"]
    a.firing["k"].last_sent -= timedelta(minutes=61)       # an hour later: reminder
    await a.update("k", True, "行情断流", "x")
    assert "已持续" in a.sent[-1]["title"]
    await a.update("k", False, "行情断流", "x")
    assert a.sent[-1]["title"] == "已恢复：行情断流" and a.sent[-1]["level"] == "ok"
    await a.update("k", False, "行情断流", "x")            # stays quiet once recovered
    assert len(a.sent) == 3


@pytest.mark.anyio
async def test_webhook_delivery():
    got = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    a = Alerter(f"http://127.0.0.1:{srv.server_port}/hook", "wecom")
    assert await a.send("测试", "ok", "ok") is True
    srv.shutdown()
    assert got == [{"msgtype": "text", "text": {"content": "[Agora] 🟢 测试\nok"}}]
    assert await Alerter("http://127.0.0.1:1/nope", "generic").send("t", "x") is False   # never raises

    class Reject(http.server.BaseHTTPRequestHandler):   # DingTalk-style: HTTP 200 + error code in the body
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"errcode": 310000, "errmsg": "keywords not in content"}).encode())

        def log_message(self, *a):
            pass

    srv2 = http.server.HTTPServer(("127.0.0.1", 0), Reject)
    threading.Thread(target=srv2.serve_forever, daemon=True).start()
    assert await Alerter(f"http://127.0.0.1:{srv2.server_port}/hook", "dingtalk").send("t", "x") is False
    srv2.shutdown()


class FakeRedis:
    def __init__(self, status: dict):
        self.status = status

    async def hgetall(self, key):
        return {k.encode(): v.encode() for k, v in self.status.items()}


@pytest.mark.anyio
async def test_health_checks(dsn):
    service, client, pool = await boot(dsn, team_tokens=(("macro_debate", "agk_alert_test_token_0001"),),
                                       alert_disk_pct=100)
    service.settings = dataclasses.replace(service.settings, market_feed="redis")
    now = datetime(2026, 11, 26, 10, 0, tzinfo=CST)
    iso = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%S.123456789+08:00")   # chrono's rfc3339: 9 fractional digits
    titles = lambda: [m["title"] for m in service.alerter.sent]

    # healthy gateway, session on, fresh quotes; the OpenPup team has never connected -> offline alert only
    service._redis = FakeRedis({"heartbeat": iso(now - timedelta(seconds=5)), "session": "1",
                                "last_publish": iso(now - timedelta(seconds=3))})
    await service.check_health(now)
    assert titles() == ["OpenPup 宏观辩论团 掉线"]

    # quotes stop during the session
    service._redis.status["last_publish"] = iso(now - timedelta(seconds=200))
    await service.check_health(now)
    assert titles()[-1] == "交易时段行情断流"

    # after the close (session 0): the stall recovers, nothing new fires
    service._redis.status["session"] = "0"
    await service.check_health(now)
    assert titles()[-1] == "已恢复：交易时段行情断流"

    # gateway dies
    service._redis.status["heartbeat"] = iso(now - timedelta(minutes=5))
    await service.check_health(now)
    assert titles()[-1] == "行情网关无响应"

    # the team's runner checks in -> recovered
    hb = await client.post("/api/v1/agent/heartbeat", headers={"Authorization": "Bearer agk_alert_test_token_0001"})
    assert hb.status_code == 200
    await service.check_health(now)
    assert "已恢复：OpenPup 宏观辩论团 掉线" in titles()

    # disk threshold
    service.settings = dataclasses.replace(service.settings, alert_disk_pct=0)
    await service.check_health(now)
    assert titles()[-1].startswith("磁盘使用率")
    await client.aclose(); await service.stop(); await pool.close()
