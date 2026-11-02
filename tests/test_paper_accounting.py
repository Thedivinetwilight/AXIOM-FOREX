"""The paper figures, pinned: one ledger, one set of numbers, one truth.

These tests exist because the workstation once showed, at the same moment:

* "5 orders, 0 filled" while five entries sat in the ledger;
* "10 open positions" beside a list of four symbol names;
* a quote marked fresh at 15:40 that was ten minutes old at 15:50;
* a P&L figure that came from a different book than the fills it described.

Each test below drives the real accounting module over a real ledger file, then
renders the real pages, and asserts that the numbers agree -- and that anything
which cannot be reconciled is *shown as unavailable* rather than smoothed over.

Nothing here connects a terminal, sends an order, arms anything or reads a
credential. The only inputs are a temporary folder and JSON written by the test.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from axiom_forex.paper_runner import accounting
from axiom_forex.paper_runner.accounting import (
    QuoteMark,
    read_account,
    read_capital,
    write_capital,
)
from axiom_forex.workstation.app import GET_ROUTES, WorkstationApp
from axiom_forex.workstation.setup import SetupWizard

from tests.test_workstation_start import _app as start_app
from tests.test_workstation_ui import (
    _Page,
    live_session,
    offline_environment,
    render,
)

NOW = datetime(2026, 11, 2, 15, 50, tzinfo=timezone.utc)





# ---------------------------------------------------------------------------
# a ledger, written the way the paper test writes one
# ---------------------------------------------------------------------------


def _entry(key: str, symbol: str, direction: str, price: float, at: str, **extra):
    event = {
        "at": at,
        "kind": "simulated_fill",
        "reason": "ENTRY_SIGNAL",
        "key": key,
        "candidate_id": "cat-0001",
        "symbol": symbol,
        "timeframe": "1h",
        "direction": direction,
        "lots": 0.1,
        "price": price,
        "quote_currency": "USD",
        "contract_size": 100000.0,
        "quote_bid": price - 0.0001,
        "quote_ask": price + 0.0001,
        "quote_time": at,
        "simulated": True,
        "priced_from": "BROKER_TICK",
    }
    event.update(extra)
    return event


def _exit(
    key: str,
    symbol: str,
    direction: str,
    entry_price: float,
    exit_price: float,
    at: str,
    *,
    entry_at: str,
    gross: float | None,
    basis: str = "BID_ASK",
    **extra,
):
    event = {
        "at": at,
        "kind": "simulated_close",
        "reason": "SIGNAL_EXIT" if basis == "BID_ASK" else "STOP_LOSS",
        "key": key,
        "candidate_id": "cat-0001",
        "symbol": symbol,
        "timeframe": "1h",
        "direction": direction,
        "lots": 0.1,
        "entry_at": entry_at,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "simulated_net_quote": gross,
        "simulated_gross_quote": gross,
        "quote_currency": "USD",
        "contract_size": 100000.0,
        "exit_basis": basis,
        "costs_modelled": ["spread_open", "slippage_open"]
        + (["spread_close", "slippage_close"] if basis == "BID_ASK" else []),
        "costs_not_modelled": ["commission", "swap"],
        "quote_bid": exit_price,
        "quote_ask": exit_price,
        "quote_time": at,
        "simulated": True,
        "priced_from": "BROKER_TICK",
    }
    event.update(extra)
    return event


def _write(root: Path, events, *, positions=(), running=True, status="RUNNING", **state):
    (root / "catalog-paper-ledger.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8"
    )
    payload = {
        "version": 1,
        "updated_at": "2026-11-02T15:45:00+00:00",
        "status": status,
        "running": running,
        "positions": list(positions),
        "watches": [{"symbol": "EURUSD"}],
    }
    payload.update(state)
    (root / "catalog-paper-state.json").write_text(json.dumps(payload), encoding="utf-8")


def _mark(symbol: str, price: float, *, spread: float = 0.0002, age_seconds: float = 5.0, now=NOW):
    return QuoteMark(
        symbol=symbol,
        bid=price - spread / 2,
        ask=price + spread / 2,
        quote_time=now - timedelta(seconds=age_seconds),
        source="paper session",
    )


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    return tmp_path


# ---------------------------------------------------------------------------
# 1. an entry fill is a fill  ("5 orders, 0 filled" must be impossible)
# ---------------------------------------------------------------------------


def test_the_entry_fills_are_counted_as_fills_and_orders_equals_fills(root):
    events = [
        _entry(f"k{index}", "EURUSD", "LONG", 1.0800 + index / 1000, f"2026-11-02T0{index}:00:00+00:00")
        for index in range(1, 6)
    ]
    _write(root, events, positions=[{"key": f"k{i}", "symbol": "EURUSD", "direction": "LONG"} for i in range(1, 6)])

    account = read_account(root, now=NOW)

    counts = account["counts"]
    assert counts["entry_fills"] == 5
    assert counts["exit_fills"] == 0
    # Both words describe the same events, so they cannot disagree.
    assert counts["orders"] == counts["fills"] == 5
    assert account["metric_by_key"]["orders"]["value"] == "5"
    assert account["metric_by_key"]["fills"]["value"] == "5"


def test_the_reported_conflict_cannot_render_orders_without_fills(tmp_path):
    """The reported triple, rendered through the real pages.

    "5 orders, 0 filled" came from counting entry events as orders and close
    events as filled; "10 open, four named" came from counting one list and
    slicing another.  Both are pinned here against a ledger that really holds
    what the report described: five entry fills, ten open positions across five
    symbols, nothing closed.
    """

    symbols = ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD")
    events = []
    positions = []
    for index, symbol in enumerate(symbols):
        for leg in range(2):  # ten open positions, two per symbol
            key = f"k{index}-{leg}"
            at = f"2026-11-02T1{index}:{leg}0:00+00:00"
            events.append(
                _entry(key, symbol, "LONG" if leg == 0 else "SHORT", 1.0800 + index / 100, at)
            )
            positions.append(
                {
                    "key": key,
                    "symbol": symbol,
                    "direction": "LONG" if leg == 0 else "SHORT",
                    "lots": 0.1,
                    "entry_at": at,
                    "entry_price": 1.0800 + index / 100,
                }
            )
    _write(tmp_path, events, positions=positions)

    marks = {symbol: _mark(symbol, 1.0800 + index / 100) for index, symbol in enumerate(symbols)}
    app = _account_app(tmp_path, marks=marks)
    account = app.paper_account()

    def _no_zero_filled(page_text: str) -> None:
        # "10 filled" must not be read as "0 filled": match the whole number.
        assert not re.search(r"(?<![\d,.])0 filled", page_text), page_text[:200]

    # One count, and it is the filled count: every entry fill is a fill.
    assert account["counts"]["entry_fills"] == 10
    assert account["counts"]["orders"] == 10
    assert account["counts"]["fills"] == 10
    assert account["counts"]["exit_fills"] == 0
    assert account["counts"]["open_positions"] == 10
    assert account["counts"]["closed_trades"] == 0

    home = _text(render(app, "/"))
    assert "10 filled" in home
    _no_zero_filled(home)
    assert "10 entry fill(s) and 0 exit fill(s)" in home

    positions_page = render(app, "/positions")
    positions_text = _text(positions_page)
    # The count and the list are the same thing, and every symbol is named.
    assert "10 open across 5 symbol(s)" in positions_text
    for symbol in symbols:
        assert symbol in positions_text, symbol
    assert len(account["positions"]) == 10
    assert "Nothing open" not in positions_text

    performance = _text(render(app, "/performance"))
    assert "10 open" in performance
    assert "0 trade(s)" in performance

    # And the five-entry shape the report started with reads as five filled.
    only_five = tmp_path / "five"
    only_five.mkdir()
    five_events = [
        _entry(f"five-{index}", "EURUSD", "LONG", 1.0800 + index / 100, f"2026-11-02T0{index}:00:00+00:00")
        for index in range(1, 6)
    ]
    _write(only_five, five_events)
    five_app = _account_app(only_five, marks={"EURUSD": _mark("EURUSD", 1.0800)})
    five_account = five_app.paper_account()
    assert five_account["counts"]["orders"] == five_account["counts"]["fills"] == 5
    assert five_account["counts"]["open_positions"] == 5
    five_home = _text(render(five_app, "/"))
    assert "5 filled" in five_home
    _no_zero_filled(five_home)


def test_five_entries_and_no_closes_read_as_five_filled_not_zero(root):
    events = [
        _entry(f"k{index}", "EURUSD", "LONG", 1.08, f"2026-11-02T0{index}:00:00+00:00")
        for index in range(1, 6)
    ]
    _write(root, events, positions=[{"key": f"k{i}", "symbol": "EURUSD", "direction": "LONG"} for i in range(1, 6)])

    account = read_account(root, now=NOW)
    orders = account["metric_by_key"]["orders"]
    fills = account["metric_by_key"]["fills"]

    assert orders["value"] == fills["value"] == "5"
    assert "0 filled" not in orders["value"]
    assert "every one filled" in orders["note"]


# ---------------------------------------------------------------------------
# 2. the position count and the position list are the same list
# ---------------------------------------------------------------------------


def test_the_position_count_and_the_list_are_the_same_thing(root):
    symbols = ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD")
    events = []
    positions = []
    for index in range(10):
        symbol = symbols[index % len(symbols)]
        key = f"k{index}"
        events.append(_entry(key, symbol, "LONG", 1.0, f"2026-11-02T0{index}:00:00+00:00"))
        positions.append(
            {
                "key": key,
                "symbol": symbol,
                "direction": "LONG",
                "lots": 0.1,
                "entry_price": 1.0,
                "entry_at": f"2026-11-02T0{index}:00:00+00:00",
            }
        )
    _write(root, events, positions=positions)

    account = read_account(root, now=NOW)

    assert account["counts"]["open_positions"] == 10
    assert len(account["positions"]) == 10
    assert len({item["symbol"] for item in account["positions"]}) == 4


def test_the_start_page_names_every_grouped_symbol_behind_the_count(tmp_path):
    symbols = ("EURUSD", "GBPUSD", "USDJPY", "AUDUSD")
    events, positions = [], []
    for index in range(10):
        symbol = symbols[index % len(symbols)]
        key = f"k{index}"
        events.append(_entry(key, symbol, "LONG", 1.0, f"2026-11-02T0{index}:00:00+00:00"))
        positions.append({"key": key, "symbol": symbol, "direction": "LONG", "lots": 0.1})
    _write(tmp_path, events, positions=positions)
    write_capital(tmp_path, "10000", "USD")

    app = _account_app(tmp_path)
    text = _text(render(app, "/"))

    tile = _tile(text, "Open paper positions")
    assert "10" in tile
    # All four symbols appear in the same tile as the count of ten.
    for symbol in symbols:
        assert symbol in tile, tile


def test_a_very_long_book_says_how_many_it_did_not_name(tmp_path):
    events, positions = [], []
    for index in range(30):
        key = f"k{index}"
        events.append(_entry(key, f"AA{index}USD", "LONG", 1.0, "2026-11-02T10:00:00+00:00"))
        positions.append({"key": key, "symbol": f"AA{index}USD", "direction": "LONG"})
    _write(tmp_path, events, positions=positions)

    text = _text(render(_account_app(tmp_path), "/"))
    tile = _tile(text, "Open paper positions")

    assert "30" in tile
    # Never silently truncated: the tile says how many it left to the page.
    assert "more (see Paper positions)" in tile


# ---------------------------------------------------------------------------
# 3. freshness is judged now, not at the last render
# ---------------------------------------------------------------------------


def test_a_quote_ten_minutes_old_is_reported_as_ten_minutes_old(root):
    _write(root, [_entry("k1", "EURUSD", "LONG", 1.08, "2026-11-02T15:39:00+00:00")])
    marks = {"EURUSD": _mark("EURUSD", 1.08, age_seconds=600)}

    account = read_account(root, now=NOW, marks=marks)

    quote = account["quotes"][0]
    assert quote["fresh"] is False
    assert 595 <= quote["age_seconds"] <= 605
    assert "min old" in quote["age_text"]
    assert account["trading"]["paused"] is True
    assert "EURUSD" in account["trading"]["reason"]


def test_the_same_quote_becomes_stale_as_time_moves_without_being_re_read(root):
    _write(root, [_entry("k1", "EURUSD", "LONG", 1.08, "2026-11-02T15:39:00+00:00")])
    marks = {"EURUSD": _mark("EURUSD", 1.08, age_seconds=10)}

    early = read_account(root, now=NOW, marks=marks)
    later = read_account(root, now=NOW + timedelta(minutes=10), marks=marks)

    assert early["quotes"][0]["fresh"] is True
    assert early["trading"]["paused"] is False
    # Same quote object, no new read: the answer changes because the clock did.
    assert later["quotes"][0]["fresh"] is False
    assert later["trading"]["paused"] is True


def test_the_paper_snapshot_recomputes_age_from_the_quote_time():
    from axiom_forex.paper_runner.catalog_paper import CatalogPaperTest

    class Clock:
        def __init__(self, now):
            self.now = now

        def __call__(self):
            return self.now

    clock = Clock(NOW)
    test = CatalogPaperTest(quotes=None, watches=(), root=Path("/tmp"), clock=clock)
    # What the loop recorded: the quote's own instant, not a frozen verdict.
    test._quotes_seen["EURUSD"] = {
        "checked_at": "2026-11-02T15:40:00+00:00",
        "quote_time": "2026-11-02T15:49:50+00:00",
        "state": "fresh",
        "bid": 1.0799,
        "ask": 1.0801,
    }

    first = test.snapshot()["quotes"][0]
    clock.now = NOW + timedelta(minutes=10)
    later = test.snapshot()["quotes"][0]

    assert first["fresh"] is True
    assert later["fresh"] is False
    assert later["age_seconds"] >= 600
    assert later["quote_time"] == "2026-11-02T15:49:50+00:00"


def test_a_stale_quote_pauses_trading_on_the_page_and_shows_the_age(tmp_path):
    _write(tmp_path, [_entry("k1", "EURUSD", "LONG", 1.08, "2026-11-02T15:39:00+00:00")])
    app = _account_app(tmp_path, marks={"EURUSD": _mark("EURUSD", 1.08, age_seconds=600)})

    page = render(app, "/performance")
    text = _text(page)

    assert "Trading decisions are paused" in text
    assert "EURUSD" in text
    assert "min old" in text
    # The age is stated with the last quote time, not a re-used "fresh" verdict.
    assert "2026-11-02T15:40" in page or "2026-11-02 15:40" in page


def test_the_same_ledger_reads_paused_when_stale_and_running_when_fresh(tmp_path):
    """Freshness is a verdict about *now*, so flipping only the clock flips it."""

    _write(tmp_path, [_entry("k1", "EURUSD", "LONG", 1.08, "2026-11-02T15:39:00+00:00")])

    stale = _account_app(tmp_path, marks={"EURUSD": _mark("EURUSD", 1.08, age_seconds=600)})
    stale_account = stale.paper_account()
    assert stale_account["trading"]["paused"] is True
    assert "600" in stale_account["trading"]["reason"] or "10.0 min" in stale_account["trading"]["reason"]
    for path in ("/", "/performance", "/positions"):
        text = _text(render(stale, path))
        assert "PAUSED" in text, path
        assert "allowance" in text.lower() or "120" in text, path

    fresh = _account_app(tmp_path, marks={"EURUSD": _mark("EURUSD", 1.08, age_seconds=3)})
    fresh_account = fresh.paper_account()
    assert fresh_account["trading"]["paused"] is False
    assert fresh_account["trading"]["reason"] == ""
    # Same ledger, same marks, three seconds old: every page now says fresh, and
    # none of them says paused.
    for path in ("/", "/performance", "/positions", "/activity"):
        text = _text(render(fresh, path))
        assert "PAUSED" not in text, path
    assert "QUOTES FRESH" in _text(render(fresh, "/performance"))
    assert "FRESH" in _text(render(fresh, "/"))  # the market-data tile
    # ...and the quote's own timestamp travels with the reading, both ways.
    row = next(item for item in fresh_account["quotes"] if item["symbol"] == "EURUSD")
    assert row["quote_time"] == "2026-11-02T15:49:57+00:00"
    assert row["age_seconds"] == 3.0


def test_no_quote_at_all_is_not_reported_as_fresh(tmp_path):
    _write(tmp_path, [_entry("k1", "EURUSD", "LONG", 1.08, "2026-11-02T15:39:00+00:00")])
    account = read_account(tmp_path, now=NOW, marks={})

    quote = account["quotes"][0]
    # The ledger's fill quote is used, and it is old: labelled, not hidden.
    assert quote["fresh"] is False
    assert quote["source"] == "paper ledger (last fill)"
    assert account["trading"]["paused"] is True


# ---------------------------------------------------------------------------
# 4. marks use the closing side, and the P&L maths is the stated one
# ---------------------------------------------------------------------------


def test_a_long_is_marked_at_the_bid_and_a_short_at_the_ask(root):
    events = [
        _entry("long", "EURUSD", "LONG", 1.08000, "2026-11-02T15:00:00+00:00"),
        _entry("short", "GBPUSD", "SHORT", 1.27000, "2026-11-02T15:00:00+00:00"),
    ]
    _write(
        root,
        events,
        positions=[
            {"key": "long", "symbol": "EURUSD", "direction": "LONG", "lots": 0.1},
            {"key": "short", "symbol": "GBPUSD", "direction": "SHORT", "lots": 0.1},
        ],
    )
    marks = {
        "EURUSD": QuoteMark("EURUSD", bid=1.08100, ask=1.08120, quote_time=NOW - timedelta(seconds=5)),
        "GBPUSD": QuoteMark("GBPUSD", bid=1.26900, ask=1.26920, quote_time=NOW - timedelta(seconds=5)),
    }

    account = read_account(root, now=NOW, marks=marks)
    by_key = {item["key"]: item for item in account["positions"]}

    assert by_key["long"]["mark_side"] == "bid"
    assert by_key["long"]["mark"] == pytest.approx(1.08100)
    # 0.001 x 100000 x 0.1 lots
    assert by_key["long"]["unrealized_gross"] == pytest.approx(10.0)

    assert by_key["short"]["mark_side"] == "ask"
    assert by_key["short"]["mark"] == pytest.approx(1.26920)
    # Short from 1.27 to 1.2692: direction -1, so a gain of 0.0008 x 100000 x 0.1
    assert by_key["short"]["unrealized_gross"] == pytest.approx(8.0)


def test_realised_pnl_is_the_sum_of_the_fills_and_uses_the_recorded_currency(root):
    events = [
        _entry("k1", "EURUSD", "LONG", 1.08000, "2026-11-02T14:00:00+00:00"),
        _exit(
            "k1",
            "EURUSD",
            "LONG",
            1.08000,
            1.08500,
            "2026-11-02T15:00:00+00:00",
            entry_at="2026-11-02T14:00:00+00:00",
            gross=50.0,
        ),
    ]
    _write(root, events)
    write_capital(root, "10000", "USD")

    account = read_account(root, now=NOW)
    values = account["values"]

    assert account["currency"] == "USD"
    assert values["realized_gross"] == pytest.approx(50.0)
    assert values["cash"] == pytest.approx(10050.0)
    assert values["equity"] == pytest.approx(10050.0)
    assert values["return_pct"] == pytest.approx(0.5)
    assert account["trades"][0]["gross"] == pytest.approx(50.0)


def test_an_unknown_cost_marks_the_net_figure_unavailable_and_never_zero(root):
    events = [
        _entry("k1", "EURUSD", "LONG", 1.08, "2026-11-02T14:00:00+00:00"),
        _exit(
            "k1",
            "EURUSD",
            "LONG",
            1.08,
            1.085,
            "2026-11-02T15:00:00+00:00",
            entry_at="2026-11-02T14:00:00+00:00",
            gross=50.0,
        ),
    ]
    _write(root, events)
    write_capital(root, "10000", "USD")

    account = read_account(root, now=NOW)
    trade = account["trades"][0]

    assert trade["costs_complete"] is False
    assert trade["net"] is None
    assert "commission" in " ".join(trade["costs_missing"])
    assert account["values"]["realized_net"] is None
    assert account["metric_by_key"]["net_pnl"]["available"] is False
    assert account["metric_by_key"]["realized_gross"]["available"] is True


def test_costs_are_subtracted_when_the_broker_reports_them(root):
    class Model:
        quote_currency = "USD"
        contract_size = 100000.0
        swap_state = "POINTS"
        commission_state = "OBSERVED"

        def labels(self):
            return {"commission": "OBSERVED", "swap": "OBSERVED", "slippage": "OBSERVED"}

        def commission_quote(self, lot):
            return 7.0 * lot * 2.0

        def swap_quote(self, direction, lot, nights):
            return 0.5 * lot * nights

    events = [
        _entry("k1", "EURUSD", "LONG", 1.08, "2026-11-01T14:00:00+00:00"),
        _exit(
            "k1",
            "EURUSD",
            "LONG",
            1.08,
            1.085,
            "2026-11-02T15:00:00+00:00",
            entry_at="2026-11-01T14:00:00+00:00",
            gross=50.0,
        ),
    ]
    _write(root, events)
    write_capital(root, "10000", "USD")

    account = read_account(root, now=NOW, cost_model=lambda symbol: Model())
    trade = account["trades"][0]

    assert trade["costs_complete"] is True
    assert trade["nights"] == 1
    assert trade["commission"] == pytest.approx(1.4)
    assert trade["swap"] == pytest.approx(0.05)
    assert trade["net"] == pytest.approx(48.55)
    assert account["values"]["realized_net"] == pytest.approx(48.55)


def test_an_unpriced_trade_is_unavailable_rather_than_a_zero(root):
    events = [
        _entry("k1", "EURUSD", "LONG", 1.08, "2026-11-02T14:00:00+00:00"),
        _exit(
            "k1",
            "EURUSD",
            "LONG",
            1.08,
            1.085,
            "2026-11-02T15:00:00+00:00",
            entry_at="2026-11-02T14:00:00+00:00",
            gross=None,
            unpriced="contract_size_unknown",
        ),
    ]
    _write(root, events)
    write_capital(root, "10000", "USD")

    account = read_account(root, now=NOW)

    assert account["values"]["realized_gross"] is None
    assert account["metric_by_key"]["realized_gross"]["available"] is False
    assert account["integrity"]["unpriced_trades"] == 1


# ---------------------------------------------------------------------------
# 5. currency is never silently added across pairs
# ---------------------------------------------------------------------------


def test_totals_are_unavailable_when_the_ledger_holds_two_quote_currencies(root):
    events = [
        _entry("a", "EURUSD", "LONG", 1.08, "2026-11-02T14:00:00+00:00"),
        _exit(
            "a",
            "EURUSD",
            "LONG",
            1.08,
            1.085,
            "2026-11-02T15:00:00+00:00",
            entry_at="2026-11-02T14:00:00+00:00",
            gross=50.0,
        ),
        _entry("b", "EURJPY", "LONG", 160.0, "2026-11-02T14:30:00+00:00", quote_currency="JPY"),
        _exit(
            "b",
            "EURJPY",
            "LONG",
            160.0,
            160.5,
            "2026-11-02T15:30:00+00:00",
            entry_at="2026-11-02T14:30:00+00:00",
            gross=5000.0,
            quote_currency="JPY",
        ),
    ]
    _write(root, events)
    write_capital(root, "10000", "USD")

    account = read_account(root, now=NOW)

    assert account["integrity"]["currency_mixed"] is True
    assert account["values"]["realized_gross"] is None
    assert account["values"]["equity"] is None
    assert "more than one quote currency" in account["unavailable"]["realized_gross"]
    # The individual trades are still shown, in their own currencies.
    assert {trade["currency"] for trade in account["trades"]} == {"USD", "JPY"}


def test_paper_capital_in_another_currency_is_not_added_to_the_pnl(root):
    events = [
        _entry("k1", "EURUSD", "LONG", 1.08, "2026-11-02T14:00:00+00:00"),
        _exit(
            "k1",
            "EURUSD",
            "LONG",
            1.08,
            1.085,
            "2026-11-02T15:00:00+00:00",
            entry_at="2026-11-02T14:00:00+00:00",
            gross=50.0,
        ),
    ]
    _write(root, events)
    write_capital(root, "10000", "EUR")

    account = read_account(root, now=NOW)

    assert account["integrity"]["currency_mixed"] is False
    assert account["values"]["cash"] is None
    assert "no conversion rate" in account["unavailable"]["cash"]


# ---------------------------------------------------------------------------
# 6. integrity warnings instead of invented values
# ---------------------------------------------------------------------------


def test_a_broken_ledger_line_is_a_named_warning_not_a_silent_gap(root):
    _write(root, [_entry("k1", "EURUSD", "LONG", 1.08, "2026-11-02T15:00:00+00:00")])
    with (root / "catalog-paper-ledger.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("{this is not json\n")

    account = read_account(root, now=NOW)

    assert account["integrity"]["ok"] is False
    warnings = " ".join(item["detail"] for item in account["integrity"]["warnings"])
    assert "not valid JSON" in warnings
    # The readable events are still counted; nothing is thrown away silently.
    assert account["counts"]["entry_fills"] == 1


def test_a_blocked_restart_is_a_data_integrity_warning_on_the_page(tmp_path):
    """A test that could not resume its position must say so, not look flat."""

    events = [_entry("k1", "EURUSD", "LONG", 1.08, "2026-11-02T15:39:00+00:00")]
    _write(
        tmp_path,
        events,
        positions=[
            {
                "key": "k1",
                "symbol": "EURUSD",
                "direction": "LONG",
                "lots": 0.1,
                "entry_at": "2026-11-02T15:39:00+00:00",
                "entry_price": 1.08,
            }
        ],
        running=False,
        status="BLOCKED",
        reason="paper_state_incomplete",
        recovery={
            "ok": False,
            "detail": "1 stored paper position(s) cannot be resumed exactly",
            "failed": [{"key": "k1", "symbol": "EURUSD", "reason": "the record's entry price is missing"}],
        },
    )
    app = _account_app(tmp_path, marks={"EURUSD": _mark("EURUSD", 1.08)})
    account = app.paper_account()

    assert account["session"]["recovery"]["ok"] is False
    warnings = [item["detail"] for item in account["integrity"]["warnings"]]
    joined = " ".join(warnings)
    assert "cannot resume" in joined
    assert "EURUSD" in joined
    assert "No value was substituted" in joined

    text = _text(render(app, "/performance"))
    assert "Data integrity warning" in text
    assert "cannot resume" in text or "blocked" in text
    # The session is blocked, so it is not described as running.
    assert "NOT RUNNING" in text or "BLOCKED" in text


def test_a_state_position_with_no_entry_fill_is_flagged(root):
    _write(
        root,
        [],
        positions=[{"key": "ghost", "symbol": "EURUSD", "direction": "LONG", "lots": 0.1}],
    )

    account = read_account(root, now=NOW)

    warnings = " ".join(item["detail"] for item in account["integrity"]["warnings"])
    assert "no entry fill" in warnings
    assert account["integrity"]["unreconciled_positions"] == 1
    # And the ghost is shown, with the ledger as the authority, rather than
    # silently vanishing from the page.
    assert any(item["key"] == "ghost" for item in account["positions"])


# ---------------------------------------------------------------------------
# 7. starting capital: configured, or asked for -- never guessed
# ---------------------------------------------------------------------------


def test_an_unset_starting_capital_leaves_cash_and_equity_unavailable(root):
    _write(root, [_entry("k1", "EURUSD", "LONG", 1.08, "2026-11-02T15:00:00+00:00")])

    account = read_account(root, now=NOW)

    assert account["capital"]["configured"] is False
    assert account["values"]["starting_capital"] is None
    assert account["values"]["cash"] is None
    assert account["values"]["equity"] is None
    assert account["metric_by_key"]["equity"]["available"] is False
    assert "guessed" in account["unavailable"]["starting_capital"]


@pytest.mark.parametrize(
    "amount,currency",
    [("0", "USD"), ("-5", "USD"), ("", "USD"), ("abc", "USD"), ("10", "US"), ("10", "US$")],
)
def test_a_bad_starting_capital_is_refused(root, amount, currency):
    with pytest.raises(ValueError):
        write_capital(root, amount, currency)
    assert read_capital(root).configured is False


def test_writing_and_reading_the_starting_capital_round_trips(root):
    capital = write_capital(root, "10,000.5", "usd", clock=lambda: NOW)

    assert capital.configured is True
    assert capital.amount == "10000.50"
    assert capital.currency == "USD"
    stored = read_capital(root)
    assert stored.amount == "10000.50"
    assert stored.currency == "USD"
    # The document holds no credential and no account number.
    text = (root / "paper-capital.local.json").read_text(encoding="utf-8")
    assert "password" not in text.lower()
    assert "login" not in text.lower()


def test_the_capital_action_saves_the_value_and_refuses_a_bad_one(tmp_path):
    app = _account_app(tmp_path)

    status, redirect, _message = app.act(
        "paper-capital", {"nonce": [app.nonce], "amount": ["5000"], "currency": ["usd"]}
    )
    assert int(status) == 303
    assert redirect == "/performance"
    assert read_capital(tmp_path).amount == "5000.00"

    status, redirect, message = app.act(
        "paper-capital", {"nonce": [app.nonce], "amount": ["lots"], "currency": ["USD"]}
    )
    assert int(status) == 409
    assert redirect == "/performance"
    assert "decimal number" in message
    # The refused value did not overwrite the good one.
    assert read_capital(tmp_path).amount == "5000.00"


# ---------------------------------------------------------------------------
# 8. the pages: same numbers everywhere, and an honest empty state
# ---------------------------------------------------------------------------


def _account_app(
    root: Path,
    *,
    marks: dict | None = None,
    connect: bool = True,
) -> WorkstationApp:
    """A workstation pointed at a temporary ledger, with no terminal behind it."""

    app = WorkstationApp(
        wizard=SetupWizard(environment=offline_environment(), session=live_session()),
    )
    app.project_root = str(root)

    # A panel-shaped object, which is all the accounting asks for: where the paper
    # folder is, the newest quotes, and (optionally) cost terms.  With no marks it
    # reports no snapshot and the ledger's own fill quotes are used instead.
    class Panel:
        data_root = root

        def paper_root(self):
            return root

        def paper_snapshot(self):
            if marks is None:
                return {}
            return {
                "running": True,
                "status": "RUNNING",
                "max_quote_age_seconds": 120.0,
                "quotes": [
                    {
                        "symbol": mark.symbol,
                        "bid": str(mark.bid),
                        "ask": str(mark.ask),
                        "quote_time": mark.quote_time.isoformat(),
                        "checked_at": mark.quote_time.isoformat(),
                        "fresh": True,
                    }
                    for mark in marks.values()
                ],
            }

        def cost_model(self, symbol):
            return None

    app.catalog = Panel()
    # A pinned clock, so quote ages are judged against the fixture's instant
    # rather than the day this test happens to run on.
    app.clock = lambda: NOW
    if connect:
        app.act("connect", {"nonce": [app.nonce]})
    return app


def _text(page_html: str) -> str:
    page = _Page()
    page.feed(page_html)
    return " ".join(page.text_content.split())


def _tile(text: str, label: str) -> str:
    start = text.find(label)
    return text[start : start + 320] if start >= 0 else ""


def _full_ledger(root: Path) -> None:
    events = [
        _entry("k1", "EURUSD", "LONG", 1.08000, "2026-11-02T13:00:00+00:00"),
        _exit(
            "k1",
            "EURUSD",
            "LONG",
            1.08000,
            1.08500,
            "2026-11-02T14:00:00+00:00",
            entry_at="2026-11-02T13:00:00+00:00",
            gross=50.0,
            basis="LEVEL",
        ),
        _entry("k2", "GBPUSD", "SHORT", 1.27000, "2026-11-02T15:00:00+00:00"),
    ]
    _write(
        root,
        events,
        positions=[
            {
                "key": "k2",
                "symbol": "GBPUSD",
                "direction": "SHORT",
                "lots": 0.1,
                "entry_price": 1.27,
                "stop": 1.272,
                "target": 1.265,
            }
        ],
    )
    write_capital(root, "10000", "USD")


def test_an_empty_ledger_shows_a_helpful_state_and_no_sample_profit(tmp_path):
    app = _account_app(tmp_path)
    (tmp_path / "catalog-paper-ledger.jsonl").write_text("", encoding="utf-8")

    page = render(app, "/performance")
    text = _text(page)

    assert "Paper performance" in text
    assert "No closed paper trades yet" in text
    assert "No dated performance yet" in text
    assert "Set the paper starting capital" in text
    # No invented money anywhere: no sample balance, no example profit.
    assert "10,000" not in text
    assert "profit of" not in text.lower()


def test_the_dashboard_and_the_performance_page_show_the_same_paper_numbers(tmp_path):
    _full_ledger(tmp_path)
    app = _account_app(tmp_path, marks={"GBPUSD": _mark("GBPUSD", 1.2680)})

    account = app.paper_account()
    metrics = account["metric_by_key"]
    equity = metrics["equity"]["value"]
    realized = metrics["realized_gross"]["value"]
    open_positions = metrics["open_positions"]["value"]
    fills = metrics["fills"]["value"]

    dashboard = _text(render(app, "/"))
    performance = _text(render(app, "/performance"))
    positions = _text(render(app, "/positions"))

    # The same three strings appear on the pages that show them: no view recomputes
    # a figure for itself.
    assert equity in dashboard and equity in performance
    assert realized in dashboard and realized in performance
    assert open_positions in performance and open_positions in positions
    assert fills in dashboard and fills in performance

    # 10,000 capital + 50 realised on the closed trade + 19 unrealised on the open
    # short (marked at the ask, 1.26810 against a 1.27000 entry on 0.1 lots).
    assert equity == "10,069.00 USD"
    assert realized == "50.00 USD"
    assert open_positions == "1"
    assert fills == "3"


def test_the_performance_page_shows_every_required_metric_and_its_source(tmp_path):
    _full_ledger(tmp_path)
    app = _account_app(tmp_path, marks={"GBPUSD": _mark("GBPUSD", 1.2680)})

    text = _text(render(app, "/performance"))

    for label in (
        "Starting capital (local simulated)",
        "Paper cash",
        "Open position value (marked)",
        "Paper equity",
        "Realised P&L (price)",
        "Unrealised P&L (price)",
        "Net P&L (after costs)",
        "Return on starting capital",
        "Open positions",
        "Closed trades",
        "Trade history",
        "Equity and P&L over time",
    ):
        assert label in text, label
    # The data source and the latest update are both named.
    assert "Durable paper ledger" in text
    assert "last ledger update" in text
    # A trade history row carries the fields the brief asks for.
    assert "EURUSD" in text and "LONG" in text and "10,000.00 USD" in text


def test_the_performance_page_never_mixes_in_the_broker_account(tmp_path):
    _full_ledger(tmp_path)
    app = _account_app(tmp_path, marks={"GBPUSD": _mark("GBPUSD", 1.2680)})

    text = _text(render(app, "/performance"))

    assert "never read into these figures" in text
    assert "REAL BROKER" in text and "PAPER" in text
    assert "not a broker balance" in text


def test_the_live_page_still_warns_before_any_live_action(tmp_path):
    app = _account_app(tmp_path, connect=False)
    page = render(app, "/live")
    text = _text(page)

    assert "Before you use this page" in text
    assert "real order" in text
    assert "Paper mode is the default" in text
    assert "No MetaTrader 5 trading adapter is wired in" in text or "No real order can be sent" in text
    # The warning comes before every live control, so nothing that can lead to a
    # real order is reachable without having passed it.  (A shell-level
    # "disconnect" form may sit above it; that sends nothing.)
    warning = page.find("Before you use this page")
    assert warning != -1
    for marker in ("/actions/live-", "value='LIVE'", "ENABLE LIVE TRADING"):
        position = page.find(marker)
        assert position == -1 or warning < position, marker


def test_every_page_renders_with_a_paper_account_reading(tmp_path):
    _full_ledger(tmp_path)
    app = _account_app(tmp_path, marks={"GBPUSD": _mark("GBPUSD", 1.2680)})

    for path in GET_ROUTES:
        status, html = app.page(path)
        assert int(status) == 200, path
        assert "<main" in html, path


def test_the_paper_capital_form_is_only_offered_when_it_is_missing(tmp_path):
    app = _account_app(tmp_path)
    (tmp_path / "catalog-paper-ledger.jsonl").write_text("", encoding="utf-8")

    assert "Set the paper starting capital" in _text(render(app, "/performance"))

    write_capital(tmp_path, "2500", "EUR")
    text = _text(render(app, "/performance"))
    assert "Set the paper starting capital" not in text
    assert "2,500.00 EUR" in text


def test_the_activity_page_pages_through_the_ledger_without_dropping_rows(tmp_path):
    events = [
        _entry(f"k{index}", "EURUSD", "LONG", 1.08, "2026-11-02T14:00:00+00:00")
        for index in range(60)
    ]
    _write(tmp_path, events, positions=[])
    app = _account_app(tmp_path)

    first = _text(render(app, "/activity"))
    assert "Page 1 of 3" in first
    assert "60 events" in first

    status, html = app.page("/activity", {"page": ["3"]})
    assert int(status) == 200
    assert "Page 3 of 3" in _text(html)


# ---------------------------------------------------------------------------
# 9. the real writer and the real reader, end to end
# ---------------------------------------------------------------------------


def test_the_paper_test_s_written_ledger_is_readable_without_a_terminal(tmp_path):
    """Drive the paper test itself, then read its ledger with no quotes at all.

    This is the case the pages hit after a restart: the terminal may be gone, but
    the ledger is on disk and every figure must still be traceable to it.
    """

    from tests.test_catalog_paper import FakeQuotes, _test

    quotes = FakeQuotes(age_seconds=2.0)
    test = _test(tmp_path, quotes)
    test._cycle()

    root = tmp_path / "paper"
    account = read_account(root, now=NOW, marks={})

    counts = account["counts"]
    assert counts["entry_fills"] == 1
    assert counts["exit_fills"] == 0
    assert counts["orders"] == counts["fills"] == 1
    assert counts["open_positions"] == 1

    position = account["positions"][0]
    # The contract size and the quote currency came out of the ledger itself.
    assert position["contract_size"] == 100000.0
    assert position["currency"] == "USD"
    # The ledger's fill quote is what marks it, labelled as the historical mark
    # it is, and it is old, so it does not pretend to be a live price.
    assert position["mark_fresh"] is False
    assert position["unrealized_gross"] is None
    assert "quote" in position["unavailable_reason"]


def test_a_closed_paper_trade_reconciles_with_the_ledger_it_wrote(tmp_path):
    from tests.test_catalog_paper import FakeQuotes, _test

    quotes = FakeQuotes(age_seconds=2.0)
    test = _test(tmp_path, quotes)
    test._cycle()
    position = next(iter(test._positions.values()))
    # Close it the way the loop would, at the quote's own bid/ask.
    test._close(
        position,
        test._fill_price(position.direction, quotes.tick("EURUSD.pro"), closing=True),
        quotes.tick("EURUSD.pro"),
        "SIGNAL_EXIT",
    )
    test._write_state()

    root = tmp_path / "paper"
    account = read_account(root, now=NOW, marks={})

    assert account["counts"]["closed_trades"] == 1
    trade = account["trades"][0]
    ledger = [
        json.loads(line)
        for line in (root / "catalog-paper-ledger.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    closed_event = next(item for item in ledger if item["kind"] == "simulated_close")
    assert trade["gross"] == pytest.approx(closed_event["simulated_net_quote"])
    assert trade["currency"] == "USD"
    # The equity curve has a dated point for the day the trade closed.
    assert [point["date"] for point in account["equity_curve"]] == ["2026-11-02"]
    assert account["equity_curve"][0]["realized_gross"] == pytest.approx(trade["gross"])


def test_a_ledger_written_by_an_older_build_does_not_count_a_close_twice(root):
    """An older build wrote a summary record beside the priced close.

    Both carried the same event kind and the same position key, so counting the
    kind alone made one close look like two trades -- and made "closed trades"
    disagree with the fills next to it.
    """

    events = [
        _entry("k1", "EURUSD", "LONG", 1.08, "2026-11-02T14:00:00+00:00"),
        _exit(
            "k1",
            "EURUSD",
            "LONG",
            1.08,
            1.085,
            "2026-11-02T15:00:00+00:00",
            entry_at="2026-11-02T14:00:00+00:00",
            gross=50.0,
        ),
        # The summary record the old writer appended, with no prices in it.
        {"at": "2026-11-02T15:00:00+00:00", "kind": "simulated_close", "key": "k1", "reason": "SIGNAL_EXIT"},
    ]
    _write(root, events)
    write_capital(root, "10000", "USD")

    account = read_account(root, now=NOW)

    assert account["counts"]["closed_trades"] == 1
    assert account["counts"]["close_notes"] == 1
    assert account["values"]["realized_gross"] == pytest.approx(50.0)
    assert account["integrity"]["ok"] is True


def test_the_writer_no_longer_emits_a_second_close_record(tmp_path):
    from tests.test_catalog_paper import FakeQuotes, _test

    quotes = FakeQuotes(age_seconds=2.0)
    test = _test(tmp_path, quotes)
    test._cycle()
    position = next(iter(test._positions.values()))
    test._close(
        position,
        test._fill_price(position.direction, quotes.tick("EURUSD.pro"), closing=True),
        quotes.tick("EURUSD.pro"),
        "SIGNAL_EXIT",
    )

    kinds = [
        json.loads(line)["kind"]
        for line in (tmp_path / "paper" / "catalog-paper-ledger.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert kinds.count("simulated_close") == 1
    assert kinds.count("close_recorded") == 1


# ---------------------------------------------------------------------------
# 10. the four key actions, and the live guard, stay visible and honest
# ---------------------------------------------------------------------------


def test_the_home_page_offers_the_four_key_actions_with_plain_explanations(tmp_path):
    _full_ledger(tmp_path)
    app = _account_app(tmp_path, marks={"GBPUSD": _mark("GBPUSD", 1.2680)})

    page = render(app, "/")
    text = _text(page)

    # Connect (already connected here, so it is a disabled control that says so),
    # start/stop paper, and view performance.
    assert "Terminal connected" in text
    assert "Start paper trading" in text
    assert "View performance" in text
    assert "Already connected: there is nothing to connect to twice." in text
    assert "Why it is disabled:" in text or "Starting needs the ticked candidates" in text
    assert "/actions/connect" in page or "Terminal connected" in page


def test_a_running_paper_session_offers_stop_in_the_same_row(tmp_path):
    from axiom_forex.workstation import guide

    account = {
        "counts": {"orders": 1, "fills": 1, "entry_fills": 1, "exit_fills": 0, "open_positions": 0},
        "metric_by_key": {},
        "positions": [],
        "quotes": [],
        "trading": {"paused": False, "reasons": []},
        "session": {"running": True},
        "available": True,
        "capital": {"configured": True},
        "integrity": {"ok": True, "warnings": []},
    }
    model = {
        "steps": [guide.Step(key="paper", number=5, title="", why="", state=guide.RUNNING, status="RUNNING")],
        "summary": {},
    }
    from axiom_forex.workstation import views

    html = views._primary_actions(_ctx(), model, account)

    assert "Stop paper trading" in html
    assert "catalog-paper-stop" in html
    assert "It closes nothing and sends nothing" in html


def _ctx():
    from axiom_forex.workstation.context import build_context

    return build_context(
        platform=None,
        wizard=SetupWizard(environment=offline_environment(), session=live_session()),
        page="start",
        title="Start",
        action_nonce="nonce",
    )


def test_live_mode_keeps_its_warning_and_paper_pages_never_offer_a_live_action(tmp_path):
    _full_ledger(tmp_path)
    app = _account_app(tmp_path, marks={"GBPUSD": _mark("GBPUSD", 1.2680)})
    app.act(
        "mode-live",
        {
            "nonce": [app.nonce],
            "confirmation": ["ENABLE LIVE TRADING"],
            "account_confirmation": ["0123456789ab"],
        },
    )

    for path in ("/", "/performance", "/positions", "/activity"):
        page = render(app, path)
        text = _text(page)
        # The mode is stated, and no live action is offered from a paper page.
        assert "PAPER" in text or "LIVE" in text
        assert "/actions/live-arm" not in page, path
        assert "/actions/live-start" not in page, path
