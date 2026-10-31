"""The Monday paper test: refuses what it should, fills only on fresh quotes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from axiom_forex.paper_runner.catalog_paper import (
    EVENT_FILL,
    EVENT_STALE,
    MAX_WATCHES,
    REASON_ALREADY_RUNNING,
    REASON_NO_WATCHES,
    REASON_SYMBOL_UNAVAILABLE,
    STOPPED,
    Watch,
    watches_from_rows,
    CatalogPaperTest,
)
from axiom_forex.strategies.catalog.candidates import catalog

UTC = timezone.utc
NOW = datetime(2026, 11, 2, 9, 15, tzinfo=UTC)

ROW_CANDIDATE = next(
    item
    for item in catalog()
    if item.entry == "sma_cross" and item.exit == "bracket" and item.filter == "none"
)


@dataclass
class FakeFacts:
    point: float = 0.00001
    contract_size: float = 100_000.0
    pair: str = "EUR/USD"
    profit_currency: str = "USD"
    account_currency: str = "USD"
    swap_state: str = "POINTS"
    swap_long: float = -0.5
    swap_short: float = 0.2


@dataclass
class FakeTick:
    symbol: str
    bid: float
    ask: float
    time: datetime


def _bars(closes, *, start: datetime, seconds: int = 900):
    rows = []
    previous = closes[0]
    for index, close in enumerate(closes):
        rows.append(
            {
                "timestamp": start + timedelta(seconds=seconds * index),
                "open": round(previous, 5),
                "high": round(max(previous, close) + 0.00002, 5),
                "low": round(min(previous, close) - 0.00002, 5),
                "close": round(close, 5),
                "spread_points": 4,
                "tick_volume": 3,
            }
        )
        previous = close
    return rows


CROSS_CLOSES = [round(1.1000 - 0.0004 * index, 5) for index in range(150)] + [1.1400]


class FakeQuotes:
    """A read-only quote surface: no order calls exist on this object at all."""

    def __init__(self, *, age_seconds: float = 0.0, symbols=("EURUSD.pro",), closes=None):
        self.age = age_seconds
        self._symbols = list(symbols)
        self._closes = list(closes or CROSS_CLOSES)
        self.calls = 0

    def symbol_names(self):
        return list(self._symbols)

    def symbol_info(self, symbol):
        if symbol not in self._symbols:
            raise LookupError("symbol unavailable")
        return FakeFacts()

    def tick(self, symbol):
        self.calls += 1
        when = NOW - timedelta(seconds=self.age)
        mid = self._closes[-1]
        return FakeTick(symbol=symbol, bid=mid, ask=round(mid + 0.00004, 5), time=when)

    def rates(self, symbol, timeframe, start, end):
        if symbol not in self._symbols:
            raise LookupError("symbol unavailable")
        return _bars(self._closes, start=NOW - timedelta(seconds=900 * len(self._closes)))


def _watch(candidate_id: str = ROW_CANDIDATE.candidate_id) -> Watch:
    return Watch(
        candidate_id=candidate_id,
        symbol="EURUSD.pro",
        timeframe="15m",
        candidate=ROW_CANDIDATE,
    )


def _test(tmp_path: Path, quotes: FakeQuotes, watches=None, **kwargs) -> CatalogPaperTest:
    return CatalogPaperTest(
        quotes=quotes,
        watches=watches if watches is not None else (_watch(),),
        root=tmp_path / "paper",
        clock=lambda: NOW,
        **kwargs,
    )


def test_watches_from_rows_refuses_symbols_the_terminal_does_not_offer(tmp_path):
    rows = [
        {"candidate_id": ROW_CANDIDATE.candidate_id, "symbol": "EURUSD.pro", "timeframe": "15m"},
        {"candidate_id": ROW_CANDIDATE.candidate_id, "symbol": "AAOIUSD", "timeframe": "15m"},
        {"candidate_id": "cat-not-a-candidate", "symbol": "EURUSD.pro", "timeframe": "15m"},
    ]
    watches, refused = watches_from_rows(rows, symbol_names=["EURUSD.pro"])
    assert [watch.symbol for watch in watches] == ["EURUSD.pro"]
    reasons = {item["reason"] for item in refused}
    assert REASON_SYMBOL_UNAVAILABLE in reasons


def test_watches_are_deduplicated_and_capped():
    rows = [
        {"candidate_id": ROW_CANDIDATE.candidate_id, "symbol": "EURUSD.pro", "timeframe": "15m"}
    ] * 3
    watches, _refused = watches_from_rows(rows, symbol_names=["EURUSD.pro"], limit=10)
    assert len(watches) == 1
    many = [
        {"candidate_id": item.candidate_id, "symbol": "EURUSD.pro", "timeframe": "15m"}
        for item in catalog()[:60]
    ]
    watches, _ = watches_from_rows(many, symbol_names=["EURUSD.pro"], limit=MAX_WATCHES + 10)
    assert len(watches) == MAX_WATCHES


def test_a_test_with_no_watches_cannot_start(tmp_path):
    test = _test(tmp_path, FakeQuotes(), watches=())
    result = test.start()
    assert result["started"] is False
    assert result["blockers"][0]["reason"] == REASON_NO_WATCHES


def test_starting_twice_is_refused(tmp_path):
    test = _test(tmp_path, FakeQuotes(), poll_seconds=10.0)
    first = test.start()
    assert first["started"] is True
    second = test.start()
    assert second["started"] is False
    assert any(item["reason"] == REASON_ALREADY_RUNNING for item in second["blockers"])
    test.stop()
    assert test.status == STOPPED


def test_a_stale_quote_never_produces_a_fill(tmp_path):
    quotes = FakeQuotes(age_seconds=600.0)
    test = _test(tmp_path, quotes)
    test._cycle()
    snapshot = test.snapshot()
    assert snapshot["fills"] == []
    assert snapshot["positions"] == []
    assert snapshot["stats"]["stale_quotes_skipped"] >= 1
    assert any(event["kind"] == EVENT_STALE for event in snapshot["events"])
    assert all(item["fresh"] is False for item in snapshot["quotes"])


def test_a_fresh_quote_with_a_signal_records_a_locally_simulated_fill(tmp_path):
    quotes = FakeQuotes(age_seconds=2.0)
    test = _test(tmp_path, quotes)
    test._cycle()
    snapshot = test.snapshot()
    assert snapshot["orders"], snapshot
    order = snapshot["orders"][0]
    assert order["simulated"] is True
    assert order["priced_from"] == "BROKER_TICK"
    assert order["direction"] == "LONG"
    assert order["price"] >= quotes.tick("EURUSD.pro").ask
    ledger = (tmp_path / "paper" / "catalog-paper-ledger.jsonl").read_text(encoding="utf-8")
    assert "simulated_fill" in ledger
    state = json.loads((tmp_path / "paper" / "catalog-paper-state.json").read_text())
    assert state["positions"], "the open position is part of the persisted state"
    assert state["watches"] and state["watches"][0]["candidate_id"] == ROW_CANDIDATE.candidate_id


def test_a_second_cycle_on_the_same_bar_does_not_double_fill(tmp_path):
    quotes = FakeQuotes(age_seconds=1.0)
    test = _test(tmp_path, quotes)
    test._cycle()
    first = len(test.snapshot()["orders"])
    test._cycle()
    assert len(test.snapshot()["orders"]) == first


def test_an_open_position_survives_a_restart_and_is_marked_as_restored(tmp_path):
    quotes = FakeQuotes(age_seconds=1.0)
    test = _test(tmp_path, quotes)
    test._cycle()
    assert test.snapshot()["positions"], "the fixture should open one position"
    test.stop()

    revived = _test(tmp_path, FakeQuotes(age_seconds=1.0), poll_seconds=10.0)
    revived.start()
    snapshot = revived.snapshot()
    assert snapshot["positions"], "the open position should be restored"
    assert snapshot["positions"][0]["restored_after_restart"] is True
    revived.stop()


def test_the_test_cannot_reach_an_order_surface():
    source = Path("axiom_forex/paper_runner/catalog_paper.py").read_text(encoding="utf-8")
    for forbidden in ("order_send", "import socket", "Adapter", "initialize("):
        assert forbidden not in source
    snapshot = _test(Path("/tmp") / "catalog-paper-check", FakeQuotes()).snapshot()
    assert "no order" in snapshot["claim"].lower() or "No order" in snapshot["claim"]
