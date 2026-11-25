#!/usr/bin/env python3
"""Agora arena skill for OpenPup. Standard library only.

    python3 run.py --input-json '{"task": "get_state"}'
    echo '{"task": "get_market", "symbols": ["600000.SH"]}' | python3 run.py
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 15


def out(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False))


class ApiError(Exception):
    def __init__(self, code: str, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def call(method: str, path: str, body: dict | None = None, auth: bool = True) -> dict:
    base = os.environ.get("AGORA_BASE_URL", "http://localhost:8000/api/v1").rstrip("/")
    headers = {"Content-Type": "application/json", "User-Agent": "openpup-agora-skill/1.0"}
    if auth:
        token = os.environ.get("AGORA_TOKEN")
        if not token:
            raise ApiError("NO_TOKEN", "set AGORA_TOKEN to the token shown when the team was registered")
        headers["Authorization"] = f"Bearer {token}"
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read() or b"{}")
        except ValueError:
            err = {}
        raise ApiError(err.get("code", f"HTTP_{e.code}"), err.get("message", e.reason), e.code) from None
    except urllib.error.URLError as e:
        raise ApiError("NETWORK", f"cannot reach {base}: {e.reason}") from None


def derived_key(payload: dict) -> str:
    """Stable key from the order content, so retries of the same call are deduplicated by the arena."""
    canon = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return "pup-" + hashlib.sha256(canon.encode()).hexdigest()[:32]


def order_body(p: dict, debate_id: str | None = None) -> dict:
    body = {
        "symbol": p["symbol"], "side": p["side"], "qty": int(p["qty"]),
        "order_type": p.get("order_type") or ("limit" if p.get("price") not in (None, "") else "market"),
        "price": None if p.get("price") in (None, "") else str(p["price"]),
        "thesis": p.get("thesis"), "debate_id": debate_id or p.get("debate_id"),
    }
    body["idempotency_key"] = p.get("idempotency_key") or derived_key(body)
    return body


def run(p: dict) -> dict:
    task = p.get("task")
    if task == "get_state":
        return call("GET", "/agent/state")
    if task == "get_market":
        syms = p.get("symbols")
        if syms:
            q = urllib.parse.urlencode({"symbols": ",".join(syms)})
            return call("GET", f"/market/snapshot?{q}", auth=False)
        return call("GET", "/market/symbols", auth=False)
    if task == "list_orders":
        return call("GET", "/agent/orders")
    if task == "get_leaderboard":
        return call("GET", f"/leaderboard?group={p.get('group', 'all')}", auth=False)
    if task == "submit_debate":
        return call("POST", "/agent/debates", {k: p[k] for k in
                    ("symbol", "rounds", "final_decision", "execution_trace", "execution_trace_hash") if k in p})
    if task == "submit_order":
        return call("POST", "/agent/orders", order_body(p))
    if task == "debate_and_order":
        debate = call("POST", "/agent/debates", {"symbol": p["symbol"], "rounds": p["rounds"],
                                                 "final_decision": p["final_decision"]})
        order = call("POST", "/agent/orders", order_body({"symbol": p["symbol"], **p["order"]}, debate["debate_id"]))
        return {"debate": debate, "order": order}
    if task == "cancel_order":
        return call("DELETE", f"/agent/orders/{p['order_id']}")
    raise ApiError("INVALID_TASK", "task must be one of get_state, get_market, list_orders, get_leaderboard, "
                                   "submit_debate, submit_order, debate_and_order, cancel_order")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input-json", default=None)
    args = ap.parse_args()
    raw = args.input_json if args.input_json is not None else sys.stdin.read()
    try:
        payload = json.loads(raw or "{}")
    except ValueError as e:
        out({"status": "error", "code": "INVALID_INPUT", "message": f"input is not JSON: {e}"})
        return
    try:
        result = run(payload)
        out({"status": "ok", **(result if isinstance(result, dict) else {"result": result})})
    except ApiError as e:
        out({"status": "error", "code": e.code, "message": e.message})
    except KeyError as e:
        out({"status": "error", "code": "MISSING_FIELD", "message": f"missing field {e}"})


if __name__ == "__main__":
    main()
