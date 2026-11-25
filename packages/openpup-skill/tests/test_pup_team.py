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


MOVERS = sorted(SYMS, key=lambda s: -abs(s["chg_pct"]))        # the arena returns them sorted
TREND = [{"symbol": "998007.SZ", "name": "演示消费", "chg_pct": 1.0, "suspended": False, "ret20": 0.2}]


def test_candidates_review_holdings_then_mix_movers_and_trend(monkeypatch):
    pup_team._last_debated.clear()
    monkeypatch.setattr(pup_team, "MAX_CANDIDATES", 3)
    picks = pup_team.pick_candidates(STATE, MOVERS, TREND)
    # the held bank is due for review; then the biggest non-ETF mover, then the strongest trend
    assert [(s["symbol"], s["why"]) for s in picks] == [
        ("999002.SH", "持仓复盘"), ("999001.SH", "今日异动"), ("998007.SZ", "趋势最强")]
    now = pup_team.time.time()
    pup_team._last_debated.update({"999002.SH": now, "999001.SH": now})
    picks = pup_team.pick_candidates(STATE, MOVERS, TREND)
    assert [s["symbol"] for s in picks] == ["998006.SZ", "998007.SZ"]      # cooldown and fresh review respected


def test_context_lines():
    f = {"ret5": 0.02, "ret20": 0.1, "ret60": -0.05, "ma20": 10.0, "ma60": 13.0, "rsi2": 12.3, "vol60": 0.3,
         "div_yield": 0.0, "_last": 11.0}
    text = pup_team.stock_context(f)
    assert "20 日 +10.0%" in text and "20 日线上方" in text and "60 日线下方" in text and "RSI(2) 12" in text
    br = {"up": 3000, "down": 1800, "limit_up": 60, "limit_down": 5, "indices": {"沪深300": {"chg_pct": 0.8}}}
    assert "沪深300 +0.80%" in pup_team.market_context(br) and "涨停 60 家" in pup_team.market_context(br)


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
