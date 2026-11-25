import pytest

pytest.importorskip("nautilus_trader")

from arena_engine.adapters.nautilus_bridge import make_equity, to_depth10, to_quote_tick, to_trade_tick  # noqa: E402
from conftest import Tape, at  # noqa: E402


def test_equity_definitions_follow_board_lot_rules():
    main = make_equity("600000.SH")
    star = make_equity("688981.SH")
    assert str(main.id) == "600000.SSE" and str(main.quote_currency) == "CNY"
    assert int(main.lot_size) == 100 and int(main.min_quantity) == 100
    assert int(star.lot_size) == 1 and int(star.min_quantity) == 200
    assert str(make_equity("000001.SZ").id) == "000001.SZSE"


def test_snapshot_conversions_including_sealed_board():
    tape = Tape()
    s = tape.snap(at("09:30:00"), [("9.99", 500), ("9.98", 800)], [("10.00", 300)], "10.00", 10_000)
    depth = to_depth10(s)
    assert len(depth.bids) == 10 and str(depth.bids[0].price) == "9.99" and depth.bids[1].size.as_double() == 800
    assert str(to_quote_tick(s).ask_price) == "10.00"
    assert to_trade_tick(s, 0) is None and to_trade_tick(s, 700).size.as_double() == 700

    sealed = tape.snap(at("10:00:00"), [("11.00", 50_000)], [], "11.00", 20_000)
    assert to_quote_tick(sealed) is None            # one-sided book cannot be a QuoteTick
    assert str(to_depth10(sealed).bids[0].price) == "11.00"
