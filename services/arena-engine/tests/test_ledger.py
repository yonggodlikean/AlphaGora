from decimal import Decimal

from arena_engine.domain.enums import Side
from arena_engine.ledger.journal import fill_journal, funding_journal
from conftest import ALICE, D, D2, Tape, at, req

BIDS = [("9.99", 5000)]
ASKS = [("10.00", 5000)]


def test_fill_journals_balance_and_reconcile_with_account(engine, tape):
    engine.on_snapshot(tape.snap(at("09:30:00"), BIDS, ASKS, "10.00", 10_000))
    buy = engine.submit_order(req(qty=1000, price="10.00"), at("09:30:01")).fills
    engine.end_trading_day(at("15:00:00"))
    engine.begin_trading_day(D2, at("09:15:00", D2))
    t2 = Tape()
    engine.on_snapshot(t2.snap(at("09:30:00", D2), [("10.50", 5000)], [("10.51", 5000)], "10.50", 1_000))
    sell = engine.submit_order(req(side=Side.SELL, qty=1000, price="10.50"), at("09:30:01", D2)).fills

    journals = [funding_journal(ALICE, "season-1", D("1000000"))] + [fill_journal(f) for f in buy + sell]
    balances: dict[tuple, Decimal] = {}
    for j in journals:
        assert sum(p.amount for p in j.postings) == 0
        for p in j.postings:
            balances[(p.account, p.symbol)] = balances.get((p.account, p.symbol), D(0)) + p.amount
    acct = engine.account(ALICE)
    assert balances[("cash", None)] == acct.cash
    assert balances[("position", "600000.SH")] == 0
    assert -balances[("realized_pnl", None)] == acct.realized_pnl == D("500.00")
