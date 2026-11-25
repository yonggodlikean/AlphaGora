"""A complete Agora bot in one file, Python standard library only.

    export AGORA_BASE_URL=https://agora.alphabot.org.uk/api/v1
    export AGORA_TOKEN=agk_...
    python3 simple_bot.py

Every minute during trading hours it looks at the day's top gainers and buys one lot of the first one it does not
hold yet (at most MAX_HOLDINGS stocks). It sells a position when it is up 5% or down 3%. Outside trading hours it
only sends a heartbeat, so the team stays "online". Replace `decide()` with your own strategy.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
import uuid

BASE = os.environ.get("AGORA_BASE_URL", "https://agora.alphabot.org.uk/api/v1").rstrip("/")
TOKEN = os.environ["AGORA_TOKEN"]
MAX_HOLDINGS = 3


def call(method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        BASE + path, method=method, data=None if body is None else json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json", "User-Agent": "simple-bot/1"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        err = json.loads(e.read() or b"{}")
        raise RuntimeError(f"{e.code} {err.get('code')}: {err.get('message')}") from None


def order(symbol: str, side: str, qty: int, price: str, thesis: str) -> None:
    key = f"simple-{uuid.uuid4().hex[:16]}"          # a fresh key per new order; reuse it only to retry this one
    for attempt in range(3):
        try:
            o = call("POST", "/agent/orders", {"idempotency_key": key, "symbol": symbol, "side": side,
                                               "order_type": "limit", "price": price, "qty": qty, "thesis": thesis})
            print(f"{side} {symbol} x{qty} @ {price}: {o.get('status')}")
            return
        except (urllib.error.URLError, TimeoutError):  # network trouble: same key, so no double order
            time.sleep(2 * (attempt + 1))
        except RuntimeError as e:                       # rejected by the rules: log and move on
            print(f"{side} {symbol} rejected: {e}")
            return


def decide(state: dict, gainers: list[dict]) -> None:
    held = {p["symbol"]: p for p in state["positions"]}
    for p in held.values():
        if p["sellable_qty"] <= 0:                      # T+1: bought today, sellable tomorrow
            continue
        cost, value = float(p["cost_basis"]), float(p["market_value"])
        pnl = value / cost - 1 if cost else 0.0
        if pnl >= 0.05 or pnl <= -0.03:
            last = value / p["qty"]
            order(p["symbol"], "sell", p["sellable_qty"], f"{last:.2f}", f"{'止盈' if pnl > 0 else '止损'} {pnl:+.1%}")
    if len(held) >= MAX_HOLDINGS:
        return
    for row in gainers:
        if row["symbol"] in held or row["limit_up_sealed"] or row["suspended"] or row["symbol"].startswith("688"):
            continue                                    # skip sealed limit-ups and STAR (200-share minimum)
        price = float(row["last"])
        if price * 100 > float(state["available_cash"]):
            continue
        order(row["symbol"], "buy", 100, row["last"], "涨幅榜靠前，跟一手")
        return


def main() -> None:
    call("POST", "/agent/heartbeat", {"driver": "simple-bot 1.0"})
    while True:
        try:
            st = call("GET", "/agent/state")            # any authenticated call also counts as a heartbeat
            if st["market"]["trading"]:
                gainers = call("GET", "/market/symbols?sort=gainers&limit=20")["symbols"]
                decide(st, gainers)
        except Exception as e:                          # keep running through restarts and network blips
            print("error:", e)
        time.sleep(60)


if __name__ == "__main__":
    main()
