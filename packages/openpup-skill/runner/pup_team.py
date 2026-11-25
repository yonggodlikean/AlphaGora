#!/usr/bin/env python3
"""Run an Agora team on a local OpenPup: bull, bear and risk pups debate in turn, a chair pup decides, and the
debate plus the order go to the arena through the agora-arena skill. Standard library only.

    export AGORA_BASE_URL=http://localhost:8000/api/v1
    export AGORA_TOKEN=agk_...                          # python -m arena_engine.admin issue-token macro_debate
    export OPENPUP_BIN=~/Workspace/Git/openpup/target/debug/openpup
    export OPENPUP_HOME=~/.agora-openpup                # isolated HOME so arena talk never touches your own pup
    python3 pup_team.py                                 # loop; AGORA_ONCE=1 for a single round

Every pup call is `openpup --local ask --pup <pup> <prompt>` with HOME=$OPENPUP_HOME.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agora-arena" / "scripts"))
from run import ApiError, call, order_body  # noqa: E402  (the agora-arena skill's client)

BIN = os.path.expanduser(os.environ.get("OPENPUP_BIN", "openpup"))
PUP_HOME = os.path.expanduser(os.environ.get("OPENPUP_HOME", "~/.agora-openpup"))
INTERVAL = int(os.environ.get("AGORA_INTERVAL", "300"))        # seconds between rounds
MAX_CANDIDATES = int(os.environ.get("AGORA_MAX_CANDIDATES", "2"))
COOLDOWN = int(os.environ.get("AGORA_COOLDOWN", "1200"))        # seconds before the same symbol is debated again
MAX_POSITION_PCT = 15
ROLES = [
    ("多头", "research", "你负责找买入或继续持有的理由。"),
    ("空头", "research", "你负责找卖出或不买的理由，可以直接反驳前面的发言。"),
    ("风控", "ops", "你只关心仓位、现金、T+1 能不能卖、涨跌停排队的风险，给出是否放行。"),
]
CHAIR = ("主席", "research")   # alpha routes long prompts to other pups; research answers directly
RULES = ("A股规则：T+1（当天买的明天才能卖）；买入100股整数倍；涨停封板时只能在涨停价排队，排到或炸板才成交；"
         "佣金万2.5最低5元，卖出印花税0.05%。这是模拟盘，不涉及真钱。")

_last_debated: dict[str, float] = {}
_driver: str | None = None


def driver_name() -> str:
    """'openpup 0.2.2', from the binary itself, shown on the team page as who drives the team."""
    global _driver
    if _driver is None:
        try:
            out = subprocess.run([BIN, "--version"], capture_output=True, text=True, timeout=20).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            out = ""
        _driver = (out or "openpup")[:40]
    return _driver


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# ------------------------------------------------------------------ OpenPup
def ask(pup: str, prompt: str, retries: int = 1) -> dict:
    env = dict(os.environ, HOME=PUP_HOME)
    last = ""
    for _ in range(retries + 1):
        try:
            r = subprocess.run([BIN, "--local", "ask", "--pup", pup, prompt], env=env, capture_output=True,
                               text=True, timeout=180)
        except subprocess.TimeoutExpired:
            last = "timed out"
            continue
        text = r.stdout.strip()
        if "api error" in text.lower():
            raise RuntimeError(f"{pup} pup: {text[:200]}")
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except ValueError:
                last = f"bad JSON: {text[:120]}"
                continue
        last = f"no JSON (stdout={text[:120]!r}, stderr={r.stderr.strip()[-120:]!r})"
    raise RuntimeError(f"{pup} pup: {last}")


# ------------------------------------------------------------------ facts
def facts(sym: dict, book: dict, state: dict) -> str:
    pos = next((p for p in state["positions"] if p["symbol"] == sym["symbol"]), None)
    asks = " ".join(f"{p}×{v // 100}手" for p, v in book["asks"][:3]) or "空（涨停封板）"
    bids = " ".join(f"{p}×{v // 100}手" for p, v in book["bids"][:3]) or "空（跌停封板）"
    held = (f"持有 {pos['qty']} 股，可卖 {pos['sellable_qty']} 股（{pos['frozen_qty']} 股今天买入、T+1 锁定），"
            f"成本 {float(pos['cost_basis']) / pos['qty']:.2f}") if pos else "未持有"
    # Price fields come from the book fetched just before the debate, never from the (older) symbol list.
    chg = (float(book["last_price"]) / float(book["prev_close"]) - 1) * 100
    return (f"股票：{book['name']}（{book['symbol']}），现价 {book['last_price']}，今日 {chg:+.2f}%，"
            f"昨收 {book['prev_close']}，涨停 {book['upper_limit']}，跌停 {book['lower_limit']}。"
            f"卖盘：{asks}。买盘：{bids}。我们的账户：总资产 {state['total_equity']}，可用现金 {state['available_cash']}，"
            f"这只股票{held}。")


def pick_candidates(state: dict, symbols: list[dict]) -> list[dict]:
    now = time.time()
    fresh = [s for s in symbols if "ETF" not in s["name"] and not s["suspended"]
             and now - _last_debated.get(s["symbol"], 0) > COOLDOWN]
    # The biggest movers of the day make the best debates, held or not (a held one may be sold).
    return sorted(fresh, key=lambda s: -abs(s["chg_pct"]))[:MAX_CANDIDATES]


# ------------------------------------------------------------------ one debate
def debate(persona: str, sym: dict, book: dict, state: dict) -> None:
    context = facts(sym, book, state)
    rounds: list[dict] = []
    for role, pup, duty in ROLES:
        said = "\n".join(f"{r['role']}（{r['stance']}）：{r['argument']}" for r in rounds) or "（你第一个发言）"
        prompt = (f"你在 A 股模拟交易擂台的「{persona}」里担任「{role}」。{duty}\n{RULES}\n{context}\n"
                  f"前面的发言：\n{said}\n"
                  "不要调用任何工具。只输出一行 JSON："
                  '{"stance":"buy|sell|hold","argument":"不超过70字的中文，像在辩论，可以有点个性"}')
        out = ask(pup, prompt)
        stance = out.get("stance") if out.get("stance") in ("buy", "sell", "hold") else "hold"
        rounds.append({"role": role, "stance": stance, "argument": str(out.get("argument", ""))[:300],
                       "model_id": f"openpup/{pup}"})
        log(f"  {role}: {stance} · {rounds[-1]['argument']}")

    said = "\n".join(f"{r['role']}（{r['stance']}）：{r['argument']}" for r in rounds)
    chair = ask(CHAIR[1], (
        f"你是 A 股模拟交易擂台「{persona}」的主席，根据下面三方发言拍板，少数服从多数，风控反对时不买。\n{RULES}\n{context}\n"
        f"发言：\n{said}\n"
        "不要调用任何工具。只输出一行 JSON："
        '{"action":"buy|sell|hold","size_pct":0到15的整数（买入占总资产比例，卖出填0表示全部卖出可卖部分）,'
        '"title":"不超过14字的标题，比如「演示龙头：打不打这个板？」","summary":"不超过50字的结论"}'))
    action = chair.get("action") if chair.get("action") in ("buy", "sell", "hold") else "hold"
    rounds.append({"role": "主席", "stance": action, "argument": str(chair.get("summary", ""))[:300],
                   "model_id": f"openpup/{CHAIR[1]}"})
    decision = {"action": action, "title": str(chair.get("title") or sym["name"])[:40],
                "summary": str(chair.get("summary", ""))[:200]}
    log(f"  主席: {action} · {decision['summary']}")

    order = None
    if action == "buy":
        price = book["asks"][0][0] if book["asks"] else book["upper_limit"]  # sealed board: queue at the limit
        pct = max(1, min(MAX_POSITION_PCT, int(chair.get("size_pct") or 10)))
        budget = min(float(state["total_equity"]) * pct / 100, float(state["available_cash"]) * 0.98)
        qty = int(budget / float(price) / 100) * 100
        if qty >= 100:
            order = {"side": "buy", "price": price, "qty": qty}
    elif action == "sell":
        pos = next((p for p in state["positions"] if p["symbol"] == sym["symbol"]), None)
        if pos and pos["sellable_qty"] > 0 and book["bids"]:
            order = {"side": "sell", "price": book["bids"][0][0], "qty": pos["sellable_qty"]}

    d = call("POST", "/agent/debates", {"symbol": sym["symbol"], "rounds": rounds, "final_decision": decision})
    if order:
        o = call("POST", "/agent/orders", order_body({"symbol": sym["symbol"], **order}, d["debate_id"]))
        log(f"  → {order['side']} {order['qty']} @ {order['price']}: {o['status']}"
            + (f"（排队，前方 {o['queue_ahead']} 股）" if o.get("queue_ahead") else ""))
    else:
        log("  → 不下单")


def one_round() -> None:
    state = call("GET", "/agent/state")
    if not state["market"]["trading"]:
        log(f"休市（{state['market']['clock']}），等待")
        return
    symbols = call("GET", "/market/symbols?sort=movers&limit=60", auth=False)["symbols"]
    for sym in pick_candidates(state, symbols):
        _last_debated[sym["symbol"]] = time.time()
        book = call("GET", f"/market/snapshot?symbols={sym['symbol']}", auth=False)["snapshots"][0]
        log(f"辩论 {book['name']} {book['last_price']}")
        try:
            debate(state["portfolio_name"], sym, book, state)
        except ApiError as e:
            log(f"  擂台拒绝：{e.code} {e.message}")
        except (RuntimeError, ValueError) as e:
            log(f"  跳过：{e}")
        state = call("GET", "/agent/state")


def ensure_workspace() -> None:
    """A fresh container has no OpenPup workspace: create it, then rely on OPENPUP_API_KEY & co. from the env."""
    if Path(PUP_HOME, ".openpup", "config.toml").exists():
        return
    if not os.environ.get("OPENPUP_API_KEY"):
        sys.exit(f"no {PUP_HOME}/.openpup/config.toml and no OPENPUP_API_KEY: give the pups an LLM first")
    subprocess.run([BIN, "--local", "status"], env=dict(os.environ, HOME=PUP_HOME), capture_output=True, timeout=60)
    log(f"initialised OpenPup workspace in {PUP_HOME} (LLM settings from environment)")


def wait(seconds: int) -> None:
    """Sleep between rounds, sending a heartbeat every minute so the team shows as online."""
    end = time.time() + seconds
    while True:
        try:
            call("POST", "/agent/heartbeat", {"driver": driver_name()})
        except ApiError as e:
            log(f"heartbeat failed: {e.code} {e.message}")
        left = end - time.time()
        if left <= 0:
            return
        time.sleep(min(60, left))


def main() -> None:
    ensure_workspace()
    log(f"driver: {driver_name()} · arena: {os.environ.get('AGORA_BASE_URL', '(default)')}")
    delay = 10
    while True:   # wait until the arena accepts us (it may still be starting, or the token not yet registered)
        try:
            call("POST", "/agent/heartbeat", {"driver": driver_name()})
            break
        except ApiError as e:
            log(f"arena not accepting this team yet ({e.code}: {e.message}); retry in {delay}s")
            time.sleep(delay)
            delay = min(delay * 2, 300)
    while True:
        try:
            one_round()
        except ApiError as e:
            log(f"arena error: {e.code} {e.message}")
        if os.environ.get("AGORA_ONCE") == "1":
            return
        wait(INTERVAL)


if __name__ == "__main__":
    main()
