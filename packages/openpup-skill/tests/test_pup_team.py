"""Runner logic with a fake pup and a fake arena (no LLM calls, no network)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runner"))
import pup_team  # noqa: E402

STATE = {"portfolio_name": "OpenPup 宏观辩论团", "total_equity": "1000000.00", "available_cash": "500000.00",
         "market": {"trading": True, "clock": "10:00:00"},
         "positions": [{"symbol": "999002.SH", "qty": 1000, "sellable_qty": 1000, "frozen_qty": 0, "cost_basis": "5000"}]}
BOOK = {"symbol": "999001.SH", "name": "演示龙头", "last_price": "12.87", "prev_close": "11.70", "upper_limit": "12.87",
        "lower_limit": "10.53", "bids": [["12.87", 5_000_000]], "asks": []}
SYMS = [
    {"symbol": "999300.SH", "name": "演示300ETF", "chg_pct": 3.0, "suspended": False},
    {"symbol": "999002.SH", "name": "演示银行", "chg_pct": 0.4, "suspended": False},
    {"symbol": "999001.SH", "name": "演示龙头", "chg_pct": 10.0, "suspended": False},
    {"symbol": "998006.SZ", "name": "演示医药", "chg_pct": -6.0, "suspended": False},
]


def test_candidates_are_biggest_movers_and_skip_etfs_and_cooldown():
    pup_team._last_debated.clear()
    assert [s["symbol"] for s in pup_team.pick_candidates(STATE, SYMS)] == ["999001.SH", "998006.SZ"]
    pup_team._last_debated["999001.SH"] = pup_team.time.time()
    assert [s["symbol"] for s in pup_team.pick_candidates(STATE, SYMS)][0] == "998006.SZ"


def test_facts_use_the_fresh_book():
    text = pup_team.facts(SYMS[2], BOOK, STATE)
    assert "现价 12.87" in text and "+10.00%" in text and "空（涨停封板）" in text and "未持有" in text


def test_debate_buys_at_the_limit_on_a_sealed_board(monkeypatch):
    calls, prompts = [], []

    def fake_ask(pup, prompt, retries=1):
        prompts.append(prompt)
        if "主席" in prompt and "拍板" in prompt:
            return {"action": "buy", "size_pct": 50, "title": "打不打？", "summary": "2:1 打板"}
        return {"stance": "buy", "argument": "封单很厚"}

    def fake_call(method, path, body=None, auth=True):
        calls.append((method, path, body))
        return {"debate_id": "d-1"} if path == "/agent/debates" else {"status": "new", "queue_ahead": 5_000_000}

    monkeypatch.setattr(pup_team, "ask", fake_ask)
    monkeypatch.setattr(pup_team, "call", fake_call)
    pup_team.debate("OpenPup 宏观辩论团", SYMS[2], BOOK, STATE)

    assert len(prompts) == 4 and "多头（buy）：封单很厚" in prompts[1]      # later speakers see earlier ones
    (_, _, debate), (_, _, order) = calls
    assert [r["role"] for r in debate["rounds"]] == ["多头", "空头", "风控", "主席"]
    assert debate["rounds"][0]["model_id"] == "openpup/research"
    assert order["price"] == "12.87" and order["debate_id"] == "d-1"
    assert order["qty"] == 11600            # size capped at 15% of equity: 150,000 / 12.87 -> 116 lots
    assert order["idempotency_key"].startswith("pup-")
