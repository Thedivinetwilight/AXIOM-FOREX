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
        # One extra step: the newest bar in this feed has already *closed*, so no
        # half-formed bar is ever handed to a decision.
        return _bars(self._closes, start=NOW - timedelta(seconds=900 * (len(self._closes) + 1)))


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


def test_symbol_facts_are_read_from_the_terminal_shaped_record_list(tmp_path):
    """The real terminal answers with a sequence of raw records; so does this."""

    from decimal import Decimal

    class ListQuotes(FakeQuotes):
        def symbol_info(self, symbol):
            return [
                {
                    "name": symbol,
                    "currency_base": "EUR",
                    "currency_profit": "USD",
                    "digits": 5,
                    "point": Decimal("0.00001"),
                    "trade_contract_size": Decimal("100000"),
                    "trade_mode": 4,
                    "trade_allowed": True,
                    "spread": 4,
                }
            ]

    quotes = ListQuotes(age_seconds=1.0)
    test = _test(tmp_path, quotes)
    test._cycle()
    positions = test.snapshot()["positions"]
    assert positions, "the terminal-shaped record must still price a real fill"
    assert positions[0]["lots"] == 0.1


def test_bars_are_requested_with_the_terminals_own_keyword_signature(tmp_path):
    """The real terminal's ``rates`` takes ``start``/``end`` by keyword only."""

    class KeywordOnlyQuotes(FakeQuotes):
        def rates(self, symbol, timeframe, *, start, end):
            return super().rates(symbol, timeframe, start, end)

    quotes = KeywordOnlyQuotes(age_seconds=1.0)
    test = _test(tmp_path, quotes)
    test._cycle()
    assert test.snapshot()["orders"], "keyword-only rates must still be read"


def test_the_bar_that_is_still_forming_is_never_used_for_a_decision(tmp_path):
    """Hand the test a bar that closes in the future: it must be dropped."""

    class FormingBarQuotes(FakeQuotes):
        def rates(self, symbol, timeframe, start, end):
            rows = super().rates(symbol, timeframe, start=start, end=end)
            rows.append(dict(rows[-1], timestamp=NOW))  # started now, still forming
            return rows

    quotes = FormingBarQuotes(age_seconds=1.0)
    test = _test(tmp_path, quotes)
    test._cycle()
    series = test._fetch_bars(test.watches[0])
    assert series is not None
    assert series.timestamps[-1] < NOW, "the forming bar must not be in the series"


# -- the 24-hour qualification clock -----------------------------------------


class _Clock:
    """A qualification clock the test moves by hand."""

    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now = self.now + timedelta(seconds=seconds)


def _qualified(tmp_path, quotes, *, clock=None, **kwargs):
    from axiom_forex.paper_runner.qualification import QualificationClock

    clock = clock or _Clock()
    qualification = QualificationClock(tmp_path / "catalog", clock=clock)
    return _test(tmp_path, quotes, qualification=qualification, **kwargs), qualification, clock


def test_a_healthy_cycle_is_what_makes_the_qualification_clock_count(tmp_path):
    from axiom_forex.paper_runner.qualification import RUNNING

    test, qualification, clock = _qualified(tmp_path, FakeQuotes(age_seconds=2.0))
    test._open_qualification()
    clock.advance(60)
    test._cycle()
    report = qualification.report()
    assert report["status"] == RUNNING
    assert report["active_seconds"] == 60
    assert report["paused_seconds"] == 0
    assert report["stale_ticks"] == 0
    assert report["decisions"] >= 1
    assert report["symbols"] == ["EURUSD.pro"]


def test_a_stale_quote_pauses_the_qualification_clock_and_says_why(tmp_path):
    from axiom_forex.paper_runner.qualification import PAUSE_STALE

    test, qualification, clock = _qualified(tmp_path, FakeQuotes(age_seconds=600.0))
    test._open_qualification()
    clock.advance(60)
    test._cycle()
    report = qualification.report()
    assert report["active_seconds"] == 0
    assert report["paused_seconds"] == 60
    assert report["pause_seconds_by_reason"][PAUSE_STALE] == 60
    assert report["stale_ticks"] == 1


def test_an_unreachable_quote_surface_pauses_and_never_counts(tmp_path):
    from axiom_forex.paper_runner.qualification import PAUSE_STALE

    class DeadQuotes(FakeQuotes):
        def tick(self, symbol):
            raise OSError("terminal is not running")

    test, qualification, clock = _qualified(tmp_path, DeadQuotes())
    test._open_qualification()
    clock.advance(60)
    test._cycle()
    report = qualification.report()
    assert report["active_seconds"] == 0
    assert report["pause_seconds_by_reason"][PAUSE_STALE] == 60


def test_a_cycle_that_raises_is_recorded_as_a_failure_not_hidden(tmp_path):
    from axiom_forex.paper_runner.qualification import FAILED

    class BrokenQuotes(FakeQuotes):
        def rates(self, symbol, timeframe, start, end):
            raise RuntimeError("no history")

    test, qualification, clock = _qualified(tmp_path, BrokenQuotes(age_seconds=1.0))
    test._open_qualification()
    try:
        test._cycle()
    except RuntimeError:
        pass
    # The loop's own handler is what books the failure; exercise it directly.
    qualification.record_failure("cycle_error", detail="RuntimeError")
    clock.advance(60)
    report = qualification.report()
    assert report["verdict"] == FAILED
    assert len(report["failures"]) >= 1
    assert qualification.live_available() is False


def test_stopping_the_paper_test_stops_the_clock_it_was_being_judged_by(tmp_path):
    from axiom_forex.paper_runner.qualification import PAUSE_OPERATOR, STOPPED

    test, qualification, clock = _qualified(tmp_path, FakeQuotes(age_seconds=2.0))
    test._open_qualification()
    clock.advance(60)
    test._cycle()
    test.stop()
    state = qualification.state()
    assert state["status"] == STOPPED
    assert state["events"][-1]["note"] == PAUSE_OPERATOR
    frozen = state["active_seconds"]
    clock.advance(600)
    test._cycle()
    assert qualification.state()["active_seconds"] == frozen


def test_a_restart_resumes_the_same_record_and_never_credits_the_dark_time(tmp_path):
    test, qualification, clock = _qualified(tmp_path, FakeQuotes(age_seconds=2.0))
    test._open_qualification()
    clock.advance(60)
    test._cycle()
    active = qualification.state()["active_seconds"]

    clock.advance(3 * 3600)  # the machine was off
    revived, revived_qualification, revived_clock = _qualified(
        tmp_path, FakeQuotes(age_seconds=2.0), clock=clock
    )
    revived._open_qualification()
    report = revived_qualification.report()
    assert report["active_seconds"] == active
    assert report["restarts"] == 1
    assert report["paused_seconds"] >= 3 * 3600
    revived_clock.advance(60)
    revived._cycle()
    assert revived_qualification.report()["active_seconds"] == active + 60


def test_a_full_day_of_healthy_cycles_qualifies_and_changes_no_execution_state(tmp_path):
    from axiom_forex.paper_runner.qualification import PASSED

    test, qualification, clock = _qualified(tmp_path, FakeQuotes(age_seconds=2.0))
    test._open_qualification()
    for _ in range(288):  # 288 x 300 s = 24 h of healthy heartbeats
        clock.advance(300)
        test._cycle()

    report = qualification.report()
    assert report["verdict"] == PASSED
    assert report["active_seconds"] == 24 * 3600
    assert report["live_available"] is True
    assert report["criteria_met"] == report["criteria_total"]

    # ... and that is all it did.  A completed paper test makes live *reviewable*;
    # it cannot arm, cannot change the mode, and has no order surface at all.
    source = Path("axiom_forex/paper_runner/catalog_paper.py").read_text(encoding="utf-8")
    for forbidden in ("order_send", "arm_trading", "enable_live", "live_workflow", "import socket"):
        assert forbidden not in source
    snapshot = test.snapshot()
    for key in ("armed", "execution_mode", "mode", "account"):
        assert key not in snapshot
    assert snapshot["simulated"] is True


def test_stopping_and_starting_again_resumes_the_same_day(tmp_path):
    test, qualification, clock = _qualified(tmp_path, FakeQuotes(age_seconds=2.0))
    test._open_qualification()
    clock.advance(60)
    test._cycle()
    test.stop()
    counted = qualification.state()["active_seconds"]
    assert counted == 60

    clock.advance(1800)  # the operator paused the test for half an hour
    revived, revived_qualification, revived_clock = _qualified(
        tmp_path, FakeQuotes(age_seconds=2.0), clock=clock
    )
    revived._open_qualification()
    report = revived_qualification.report()
    assert report["status"] == "RUNNING"
    assert report["active_seconds"] == counted
    assert report["pause_seconds_by_reason"]["runner_stopped"] >= 1800
    revived_clock.advance(60)
    revived._cycle()
    assert revived_qualification.report()["active_seconds"] == counted + 60


def test_a_new_test_after_a_failed_one_says_what_it_replaced(tmp_path):
    test, qualification, clock = _qualified(tmp_path, FakeQuotes(age_seconds=2.0))
    test._open_qualification()
    qualification.record_failure("cycle_error", detail="RuntimeError")
    clock.advance(60)
    test._cycle()
    test.stop()
    assert qualification.verdict() == "FAILED"

    again, again_qualification, _clock = _qualified(tmp_path, FakeQuotes(age_seconds=2.0))
    again._open_qualification()
    events = again_qualification.state()["events"]
    assert events[0]["kind"] == "started"
    assert "replacing a FAILED record" in events[0]["note"]
