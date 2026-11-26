"""House strategies on hand-made history: each fires on its textbook setup and nowhere else."""
from array import array
from datetime import date, datetime, time
from decimal import Decimal

from arena_engine.domain.enums import ControllerType, Side
from arena_engine.domain.models import Account, Position
from arena_engine.house_bots import HouseBots, MarketView
from arena_engine.market.calendar import CST
from arena_engine.market.history import Bars, History, rsi, sma, volatility
from arena_engine.market.snapshot import L1Snapshot, Level

DAY = date(2026, 11, 26)
D = Decimal


def bars(closes, *, lu_last=False, div=0.0, float_shares=1e8):
    n = len(closes)
    ds = [20250101 + i for i in range(n)]            # only order matters, all before DAY
    lu = bytearray(n)
    if lu_last:
        lu[-1] = 1
    return Bars(ds, array("d", closes), array("d", closes), array("d", closes), array("d", [1e6] * n),
                array("d", [1e7] * n), bytes(lu), div, float_shares)


def book(sym, last, prev, *, sealed=False, bid_vol=100_000):
    last, prev = D(str(last)), D(str(prev))
    up, down = (prev * D("1.1")).quantize(D("0.01")), (prev * D("0.9")).quantize(D("0.01"))
    if sealed:
        last = up
        bids, asks = (Level(up, bid_vol),), ()
    else:
        bids, asks = (Level(last - D("0.01"), 10_000),), (Level(last, 10_000),)
    return L1Snapshot(sym, datetime.combine(DAY, time(14, 0), CST), 1, bids, asks, last, 1_000_000, up, down, prev)


def view(books, hist, at, *, members=None, day_open=None, names=None, held_days=None):
    h = History(hist, members or {"000300": list(hist), "000016": list(hist)}, {}, DAY.isoformat())
    return MarketView(DAY, datetime.combine(DAY, at, CST), {b.symbol: b for b in books},
                      day_open or {b.symbol: b.prev_close for b in books}, {}, names or {}, h, held_days or {})


def account(pid="p", cash="1000000", positions=None):
    a = Account(pid, ControllerType.OPENPUP_AI, D(cash), D(cash))
    for sym, (qty, cost) in (positions or {}).items():
        a.positions[sym] = Position(sym, qty, 0, 0, D(str(cost)) * qty)
    return a


def bots(tag):
    return HouseBots({"p": tag}, "510300.SH", ("601398.SH",))


def test_indicators():
    c = [10.0, 11.0, 10.0, 9.0]
    assert sma(c, 2) == 9.5 and sma(c, 2, 1) == 10.5 and rsi(c, 2) == 0.0
    assert rsi([1, 2, 3], 2) == 100.0
    assert volatility([10.0] * 61, 60) == 0.0


def test_mean_reversion_buys_the_deepest_dip_in_an_uptrend_late_in_the_day():
    up = [10 + i * 0.02 for i in range(220)]           # long uptrend, ends at 14.38
    dip = up[:-2] + [up[-3] * 0.97, up[-3] * 0.94]     # two red days, -6%
    deeper = up[:-2] + [up[-3] * 0.95, up[-3] * 0.90]
    downtrend = [20 - i * 0.03 for i in range(220)]    # below MA200: never
    hist = {"600001.SH": bars(dip), "600002.SH": bars(deeper), "600003.SH": bars(downtrend)}
    books = [book("600001.SH", round(dip[-1] * 0.99, 2), round(dip[-1], 2)),
             book("600002.SH", round(deeper[-1] * 0.99, 2), round(deeper[-1], 2)),
             book("600003.SH", round(downtrend[-1] * 0.95, 2), round(downtrend[-1], 2))]
    b = bots("contrarian")
    assert b.decide(view(books, hist, time(14, 0)), {"p": account()}) == []       # not before 14:45
    out = b.decide(view(books, hist, time(14, 46)), {"p": account()})
    assert [i.symbol for i in out] == ["600002.SH", "600001.SH"]                   # deepest first
    assert all(i.side is Side.BUY for i in out) and "RSI(2)" in out[0].rounds[0]["argument"]


def test_mean_reversion_exits():
    closes = [10.0] * 220
    hist = {"600001.SH": bars(closes)}
    pos = {"600001.SH": (1000, 10.0)}
    stop = bots("contrarian").decide(view([book("600001.SH", "9.10", "9.50")], hist, time(10, 0)),
                                     {"p": account(positions=pos)})
    assert stop and stop[0].side is Side.SELL and "止损" in stop[0].summary         # -9%: any time
    hold = bots("contrarian").decide(view([book("600001.SH", "9.90", "9.95")], hist, time(14, 46),
                                          held_days={("p", "600001.SH"): 2}), {"p": account(positions=pos)})
    assert hold == []                                                              # below MA5, day 2: wait
    timed = bots("contrarian").decide(view([book("600001.SH", "9.90", "9.95")], hist, time(14, 46),
                                           held_days={("p", "600001.SH"): 5}), {"p": account(positions=pos)})
    assert timed and "时间止损" in timed[0].summary


def test_ma_crossover_only_on_a_fresh_golden_cross_with_rising_ma20():
    flat_then_up = [10.0] * 30 + [9.9, 9.9, 9.9, 9.9, 9.95]         # MA5 9.91 < MA20 9.98 until today's jump
    already_above = [10 + i * 0.05 for i in range(35)]               # MA5 has been above for weeks
    hist = {"600001.SH": bars(flat_then_up), "600002.SH": bars(already_above)}
    books = [book("600001.SH", "10.40", "9.95"), book("600002.SH", "11.80", "11.70")]
    b = bots("ma_crossover")
    assert b.decide(view(books, hist, time(14, 0)), {"p": account()}) == []       # decided at 14:50
    out = b.decide(view(books, hist, time(14, 51)), {"p": account()})
    assert [i.symbol for i in out] == ["600001.SH"] and "上穿" in out[0].rounds[0]["argument"]
    fading = {"600002.SH": bars([11.0] * 30 + [10.9, 10.8, 10.7, 10.65, 10.6])}
    held = bots("ma_crossover").decide(view([book("600002.SH", "10.55", "10.60")], fading, time(14, 51)),
                                       {"p": account(positions={"600002.SH": (1000, 11.0)})})
    assert held and held[0].side is Side.SELL                                       # dropped under MA20


def test_first_board_only():
    hist = {"600001.SH": bars([10.0] * 30), "600002.SH": bars([10.0] * 30, lu_last=True),
            "600003.SH": bars([10.0] * 30), "600004.SH": bars([10.0] * 30, float_shares=5e10)}
    books = [book("600001.SH", 0, "10.00", sealed=True, bid_vol=2_500_000),   # first board, 2.5% of float
             book("600002.SH", 0, "10.00", sealed=True, bid_vol=2_500_000),   # second board: no
             book("600003.SH", 0, "10.00", sealed=True, bid_vol=2_500_000),   # one-price board: no
             book("600004.SH", 0, "10.00", sealed=True, bid_vol=100_000)]     # thin seal on a big cap: no
    opened = {"600001.SH": D("10.20"), "600002.SH": D("10.20"), "600003.SH": D("11.00"), "600004.SH": D("10.2")}
    b = bots("limit_up_momentum")
    out = b.decide(view(books, hist, time(10, 5), day_open=opened), {"p": account()})
    assert [i.symbol for i in out] == ["600001.SH"] and out[0].price == D("11.00")
    assert b.decide(view(books, hist, time(11, 5), day_open=opened), {"p": account()}) == []  # after 11:00
    # next day: rides a holding board, sells a broken one
    pos = {"600001.SH": (1000, 11.0), "600002.SH": (1000, 11.0)}
    nxt = bots("limit_up_momentum").decide(
        view([book("600001.SH", 0, "11.00", sealed=True), book("600002.SH", "11.20", "11.00")], hist, time(9, 40)),
        {"p": account(positions=pos)})
    assert [(i.symbol, i.side) for i in nxt] == [("600002.SH", Side.SELL)]


def test_dividend_low_vol_picks_high_yield_then_low_volatility():
    hist, books = {}, []
    for i in range(40):
        sym = f"60{i:04d}.SH"
        wiggle = 0.002 + i * 0.001                     # later symbols are more volatile
        closes = [10 * (1 + (wiggle if k % 2 else -wiggle)) for k in range(70)]
        hist[sym] = bars(closes, div=0.2 + 0.01 * (i % 20))   # yields 2%..3.9%
        books.append(book(sym, "10.00", "10.00"))
    out = bots("dividend_conservative").decide(view(books, hist, time(10, 0)), {"p": account()})
    b = bots("dividend_conservative")
    b.decide(view(books, hist, time(10, 0)), {"p": account()})
    picked = set(b._div_target)
    assert len(picked) == 10 and len(out) == 2                      # two buys per minute
    top30 = sorted(hist, key=lambda s: -hist[s].div_ttm)[:30]
    assert picked <= set(top30)
    assert all("股息率" in i.rounds[0]["argument"] for i in out)




def test_actions_beyond_two_per_minute_are_retried_next_minute():
    hist, books = {}, []
    for i in range(40):
        sym = f"60{i:04d}.SH"
        wiggle = 0.002 + i * 0.001
        hist[sym] = bars([10 * (1 + (wiggle if k % 2 else -wiggle)) for k in range(70)], div=0.2 + 0.01 * (i % 20))
        books.append(book(sym, "10.00", "10.00"))
    b = bots("dividend_conservative")
    seen = []
    for minute in range(5):
        out = b.decide(view(books, hist, time(10, minute)), {"p": account()})
        assert len(out) == 2
        seen += [i.symbol for i in out]
    assert len(set(seen)) == 10            # all ten targets bought over five minutes, none twice


def test_only_filled_boards_count_and_the_rest_are_cancelled_at_the_cap():
    hist = {f"60000{i}.SH": bars([10.0] * 30) for i in range(1, 7)}
    books = [book(s, 0, "10.00", sealed=True, bid_vol=2_500_000) for s in hist]
    opened = {s: D("10.20") for s in hist}
    # two boards queued, none filled: queuing may go on (only fills count), never twice on the same symbol
    mv = view(books, hist, time(10, 5), day_open=opened)
    mv.pending_buys = {"p": {"600001.SH": "o1", "600002.SH": "o2"}}
    out = bots("limit_up_momentum").decide(mv, {"p": account()})
    assert len(out) == 2 and not {i.symbol for i in out} & {"600001.SH", "600002.SH"}
    # five filled, one still queued: cancel it, place nothing new
    mv = view(books, hist, time(10, 9), day_open=opened)
    mv.pending_buys = {"p": {"600006.SH": "o6"}}
    filled = account(positions={f"60000{i}.SH": (100, 11.0) for i in range(1, 6)})
    for p in filled.positions.values():
        p.frozen_qty = p.qty                     # bought today: not sellable, so no exit intents
    out = bots("limit_up_momentum").decide(mv, {"p": filled})
    assert [i.cancel_order_id for i in out] == ["o6"]
    # four filled: still room for one more board
    four = account(positions={f"60000{i}.SH": (100, 11.0) for i in range(1, 5)})
    for p in four.positions.values():
        p.frozen_qty = p.qty
    out = bots("limit_up_momentum").decide(view(books, hist, time(10, 9), day_open=opened), {"p": four})
    assert len(out) == 1 and out[0].symbol in {"600005.SH", "600006.SH"} and not out[0].cancel_order_id
