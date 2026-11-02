"""A stored paper book that cannot be resumed must be closable — and only honestly.

The situation these tests cover is a real one: ten simulated positions are on disk,
the operator re-runs the autopilot, it selects two different rows, and the paper test
refuses to start (``paper_state_incomplete``) because no watched row would ever
manage the old book.  The page used to offer nothing, so the book could never be
closed and the autopilot could never start again.

What is proved here:

* the refusal is real, and it leaves the state file and the ledger byte-for-byte as
  they were;
* the recovery close writes one priced fill *per position*, through the ordinary
  ledger close path, at the closing side of the spread — and the accounting then
  pairs every close with its own entry fill and reports a flat book;
* a stale quote, a missing quote, or one incomplete record refuses the **whole**
  action and writes nothing at all;
* the confirmation and the request nonce are both required;
* a second run reconciles rather than closing a position twice;
* nothing on the close path can reach a broker, and the execution mode is untouched;
* after a valid flatten the paper autopilot's own start succeeds.

Everything is a fake terminal, a fixture book and a temporary folder.  No order is
sent, no MetaTrader 5 position is touched, and nothing here is a profitability or
live-readiness claim.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from axiom_forex.paper_runner import accounting
from axiom_forex.paper_runner.catalog_paper import (
    BASIS_BID_ASK,
    EVENT_CLOSED,
    EVENT_CLOSE_RECORDED,
    EVENT_FILL,
    EVENT_FLATTENED,
    EXIT_OPERATOR,
    REASON_FLATTEN_FIELDS,
    REASON_FLATTEN_QUOTE,
    REASON_FLATTEN_RUNNING,
    REASON_FLATTEN_STATE,
    REASON_STATE_RECOVERY,
    STATE_VERSION,
    CatalogPaperTest,
    Watch,
)
from axiom_forex.strategies.catalog.candidates import catalog
from axiom_forex.strategies.catalog.panel import (
    ACTION_PAPER_CLOSE_ALL,
    CONFIRM_CLOSE_ALL,
    CatalogPanel,
)

from tests.test_paper_accounting import _account_app, _text
from tests.test_workstation_ui import render

UTC = timezone.utc
NOW = datetime(2026, 11, 2, 9, 20, tzinfo=UTC)

#: The row the *current* selection follows.  Its key is deliberately not in the
#: stored book: that mismatch is the defect.
TRAILING = next(
    item
    for item in catalog()
    if item.entry == "sma_cross" and item.exit == "trailing_atr" and item.filter == "none"
)

BOOK_SIZE = 10


def _symbol_record(name: str, base: str, profit: str) -> dict[str, Any]:
    return {
        "name": name,
        "currency_base": base,
        "currency_profit": profit,
        "currency_margin": base,
        "digits": 5,
        "point": Decimal("0.00001"),
        "trade_contract_size": Decimal("100000"),
        "volume_min": Decimal("0.01"),
        "volume_max": Decimal("100"),
        "volume_step": Decimal("0.01"),
        "trade_mode": 4,
        "trade_allowed": True,
        "spread": 4,
        "swap_mode": 1,
        "swap_long": Decimal("-0.5"),
        "swap_short": Decimal("0.2"),
    }


@dataclass
class FakeTick:
    symbol: str
    bid: float
    ask: float
    time: Any


class FakeTerminal:
    """The read-only surface the paper path is allowed to touch, and nothing else.

    There is deliberately no order method on it: a test that patched one in would
    be testing a surface the production code cannot reach.
    """

    #: Prices per symbol, so a test can assert the closing side exactly.
    PRICES = {"EURUSD.pro": 1.10000, "GBPUSD.pro": 1.27000}
    SPREAD = 0.00004

    def __init__(self, *, age_seconds: float = 1.0) -> None:
        self.age = age_seconds
        self.calls: list[tuple[str, str]] = []

    def symbol_names(self) -> list[str]:
        return list(self.PRICES)

    def symbol_info(self, name: str):
        self.calls.append(("symbol_info", name))
        if name not in self.PRICES:
            raise LookupError("symbol not found")
        base, profit = name.split(".")[0][:3], name.split(".")[0][3:6]
        return [_symbol_record(name, base, profit)]

    def tick(self, symbol: str) -> FakeTick:
        self.calls.append(("tick", symbol))
        if symbol not in self.PRICES:
            raise LookupError("symbol not found")
        mid = self.PRICES[symbol]
        return FakeTick(symbol, mid, round(mid + self.SPREAD, 5), NOW - timedelta(seconds=self.age))

    def rates(self, symbol: str, timeframe: str, start: datetime, end: datetime):
        self.calls.append(("rates", symbol))
        return []


def _paper_root(base: Path) -> Path:
    """Where the panel keeps the paper ledger and state for this root."""

    return base / "data" / "catalog" / "paper"


def _stored_record(index: int, *, symbol: str, at: str, entry: float) -> dict[str, Any]:
    """One position as the previous run wrote it: complete, and not resumable."""

    return {
        "state_version": STATE_VERSION,
        "key": f"cat-{index:04d}@{symbol}|15m",
        "candidate_id": f"cat-{index:04d}",
        "symbol": symbol,
        "timeframe": "15m",
        "direction": "LONG" if index % 3 else "SHORT",
        "lots": 0.10,
        "entry_at": at,
        "entry_price": entry,
        "entry_mid": entry,
        "atr": 0.00120,
        "bar_index": 100 + index,
        "bar_at": at,
        "entry_bar_at": at,
        "entry_bar_index": 100 + index,
        "stop": None,
        "target": None,
        "trail": None,
        "best": entry,
        "stop_initial": None,
        "target_initial": None,
        "trail_initial": None,
        "notional_quote": 0.0,
        "quote_currency": "USD",
        "contract_size": 100000.0,
        "restored_after_restart": False,
        "migrated": False,
        "migration": "",
        "priced_from": "BROKER_TICK",
        "simulated": True,
    }


def _entry_fill(record: dict[str, Any]) -> dict[str, Any]:
    entry = float(record["entry_price"])
    return {
        "at": record["entry_at"],
        "kind": EVENT_FILL,
        "reason": "SIGNAL_ENTRY",
        "key": record["key"],
        "candidate_id": record["candidate_id"],
        "symbol": record["symbol"],
        "timeframe": record["timeframe"],
        "direction": record["direction"],
        "lots": record["lots"],
        "price": entry,
        "quote_currency": record["quote_currency"],
        "contract_size": record["contract_size"],
        "quote_bid": entry,
        "quote_ask": round(entry + 0.00004, 5),
        "quote_time": record["entry_at"],
        "simulated": True,
        "priced_from": "BROKER_TICK",
    }


def _seed_book(base: Path, *, size: int = BOOK_SIZE, mutate=None) -> Path:
    """Ten stored positions, their entry fills, and the refusal that follows them.

    ``mutate(records)`` lets a test damage exactly one record or one fill, without
    having to write its own book.
    """

    paper = _paper_root(base)
    paper.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    for index in range(size):
        symbol = "EURUSD.pro" if index % 2 == 0 else "GBPUSD.pro"
        entry = round(FakeTerminal.PRICES[symbol] + index * 0.0005, 5)
        at = (NOW - timedelta(hours=6) + timedelta(minutes=15 * index)).isoformat()
        records.append(_stored_record(index, symbol=symbol, at=at, entry=entry))
    fills = [_entry_fill(record) for record in records]
    if mutate is not None:
        mutate(records, fills)

    document = {
        "version": STATE_VERSION,
        "updated_at": NOW.isoformat(),
        "status": "BLOCKED",
        "running": False,
        "started_at": "",
        "stopped_at": "",
        "reason": REASON_STATE_RECOVERY,
        "lots": 0.10,
        "max_quote_age_seconds": 120.0,
        # A watch list this build must preserve: the flatten may not rewrite it.
        "watches": [{"key": "stored-watch-key", "symbol": "EURUSD.pro", "timeframe": "15m"}],
        "stats": {"cycles": 41, "signals": 10, "fills": 10, "closes": 0, "waiting": 0},
        "positions": records,
        "last_bar": {"stored-watch-key": "2026-11-02T08:45:00+00:00"},
        "recovery": {
            "ok": False,
            "version": STATE_VERSION,
            "restored": 0,
            "migrated": 0,
            "failed": [
                {
                    "key": record["key"],
                    "symbol": record["symbol"],
                    "reason": "no watched row matches this position, so nothing would "
                    "manage it (it would sit open for ever)",
                }
                for record in records
            ],
            "detail": f"{len(records)} stored paper position(s) cannot be resumed exactly",
        },
    }
    (paper / "catalog-paper-state.json").write_text(
        json.dumps(document, indent=2, sort_keys=True), encoding="utf-8"
    )
    with (paper / "catalog-paper-ledger.jsonl").open("w", encoding="utf-8") as handle:
        for fill in fills:
            handle.write(json.dumps(fill, sort_keys=True) + "\n")
    return paper


def _watch() -> Watch:
    return Watch(
        candidate_id=TRAILING.candidate_id,
        symbol="EURUSD.pro",
        timeframe="15m",
        candidate=TRAILING,
    )


def _panel(base: Path, *, terminal: Any) -> CatalogPanel:
    return CatalogPanel(
        root=base,
        terminal_provider=lambda: terminal,
        bars_provider=None,
        clock=lambda: NOW,
    )


def _runner(paper: Path, *, terminal: Any) -> CatalogPaperTest:
    return CatalogPaperTest(
        quotes=terminal,
        watches=(_watch(),),
        root=paper,
        clock=lambda: NOW,
    )


def _ledger(paper: Path) -> list[dict[str, Any]]:
    path = paper / "catalog-paper-ledger.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _state(paper: Path) -> dict[str, Any]:
    return json.loads((paper / "catalog-paper-state.json").read_text(encoding="utf-8"))


def _digest(paper: Path) -> tuple[bytes, bytes]:
    """Both durable artefacts, byte for byte, for a 'nothing was written' claim."""

    return (
        (paper / "catalog-paper-ledger.jsonl").read_bytes(),
        (paper / "catalog-paper-state.json").read_bytes(),
    )


# -- the situation ----------------------------------------------------------


def test_the_stored_book_blocks_a_start_and_leaves_both_files_untouched(tmp_path):
    """The defect as the operator met it: refused, unclosable, nothing to click."""

    paper = _seed_book(tmp_path)
    terminal = FakeTerminal()
    runner = _runner(paper, terminal=terminal)
    before = _digest(paper)

    outcome = runner.start()

    assert outcome["started"] is False
    assert outcome["status"] == "BLOCKED"
    assert outcome["blockers"][0]["reason"] == REASON_STATE_RECOVERY
    assert "will not resume" in outcome["blockers"][0]["detail"]
    # A refusal is not a trade: the state file is untouched, and the ledger gains
    # one evidence event for the refused resume -- never a fill and never a close.
    assert (paper / "catalog-paper-state.json").read_bytes() == before[1]
    events = _ledger(paper)
    assert [event["kind"] for event in events[BOOK_SIZE:]] == ["state_recovery_failed"]
    assert not [event for event in events if event["kind"] in {EVENT_CLOSED, EVENT_FLATTENED}]
    account = accounting.read_account(paper, now=NOW)
    assert account["counts"]["open_positions"] == BOOK_SIZE
    assert account["integrity"]["ok"] is False


# -- the recovery close ----------------------------------------------------


def test_the_operator_can_close_every_stored_position_through_the_ledger(tmp_path):
    paper = _seed_book(tmp_path)
    terminal = FakeTerminal()
    panel = _panel(tmp_path, terminal=terminal)

    outcome = panel.act(ACTION_PAPER_CLOSE_ALL, {"confirm": CONFIRM_CLOSE_ALL})

    assert outcome["ok"] is True, outcome
    assert outcome["path"] == "/positions"
    assert "Simulated closing 10 paper position" in outcome["message"]
    assert "no broker order was sent" in outcome["message"]

    closes = [event for event in _ledger(paper) if event.get("kind") == EVENT_CLOSED]
    assert len(closes) == BOOK_SIZE, "one close per position, and no summary close"
    notes = [event for event in _ledger(paper) if event.get("kind") == EVENT_CLOSE_RECORDED]
    assert len(notes) == BOOK_SIZE, "one audit note per close"
    summaries = [event for event in _ledger(paper) if event.get("kind") == EVENT_FLATTENED]
    assert len(summaries) == 1
    assert summaries[0]["closed"] == BOOK_SIZE
    assert summaries[0]["simulated"] is True
    assert summaries[0]["broker_orders"] == 0

    # Every close is a real, priced, paired trade in the accounting read model.
    account = accounting.read_account(paper, now=NOW)
    assert account["counts"]["entry_fills"] == BOOK_SIZE
    assert account["counts"]["exit_fills"] == BOOK_SIZE
    assert account["counts"]["open_positions"] == 0
    assert account["counts"]["closed_trades"] == BOOK_SIZE
    assert account["counts"]["close_notes"] == 0, "a note is not a trade"
    assert account["integrity"]["ok"] is True, account["integrity"]["warnings"]
    assert len(account["trades"]) == BOOK_SIZE
    for trade in account["trades"]:
        assert trade["reason"] == EXIT_OPERATOR

    state = _state(paper)
    assert state["positions"] == []
    assert state["recovery"]["ok"] is True
    assert state["recovery"]["closed_by_operator"] == BOOK_SIZE
    assert len(state["flattened"]) == BOOK_SIZE
    assert state["last_flatten"]["closed"] == BOOK_SIZE
    assert state["last_flatten"]["broker_orders"] == 0
    # Nothing this build does not understand was thrown away.
    assert state["watches"] == [{"key": "stored-watch-key", "symbol": "EURUSD.pro", "timeframe": "15m"}]
    assert state["last_bar"] == {"stored-watch-key": "2026-11-02T08:45:00+00:00"}
    assert state["stats"]["cycles"] == 41


def test_each_close_is_priced_at_its_own_closing_side_with_the_ledger_costs(tmp_path):
    paper = _seed_book(tmp_path, size=2)
    terminal = FakeTerminal()

    outcome = _runner(paper, terminal=terminal).close_stored_book()

    assert outcome["ok"] is True, outcome
    closes = [event for event in _ledger(paper) if event.get("kind") == EVENT_CLOSED]
    for event in closes:
        symbol = event["symbol"]
        bid = FakeTerminal.PRICES[symbol]
        ask = round(bid + FakeTerminal.SPREAD, 5)
        if event["direction"] == "LONG":
            # A long sells the bid, and the modelled slippage may only make it worse.
            assert event["exit_price"] <= bid + 1e-12, "a long never sells above the bid"
            assert event["exit_price"] == pytest.approx(bid, abs=1e-4)
        else:
            # A short buys the ask, and the modelled slippage may only make it worse.
            assert event["exit_price"] >= ask - 1e-12, "a short never buys below the ask"
            assert event["exit_price"] == pytest.approx(ask, abs=1e-4)
        assert event["exit_basis"] == BASIS_BID_ASK
        assert "spread_close" in event["costs_modelled"]
        assert event["costs_not_modelled"] == ["commission", "swap"]
        assert event["quote_bid"] == pytest.approx(bid, abs=1e-9)
        assert event["quote_ask"] == pytest.approx(ask, abs=1e-9)
        assert event["priced_from"] == "BROKER_TICK"
        assert event["simulated"] is True
        expected = (
            (1 if event["direction"] == "LONG" else -1)
            * (event["exit_price"] - event["entry_price"])
            * 100000.0
            * 0.10
        )
        assert event["simulated_net_quote"] == pytest.approx(expected, abs=1e-4)


def test_a_stale_quote_refuses_the_whole_close_and_writes_nothing(tmp_path):
    paper = _seed_book(tmp_path)
    before = _digest(paper)
    terminal = FakeTerminal(age_seconds=900.0)

    outcome = _runner(paper, terminal=terminal).close_stored_book()

    assert outcome["ok"] is False
    assert outcome["reason"] == REASON_FLATTEN_QUOTE
    assert "No fresh quote for" in outcome["message"]
    assert "nothing was closed" in outcome["message"].lower()
    assert {item["symbol"] for item in outcome["refusals"]} == {"EURUSD.pro", "GBPUSD.pro"}
    assert all(item["reason"] == "stale_quote" for item in outcome["refusals"])
    # Not one byte moved -- in either file.
    assert _digest(paper) == before
    assert len(_ledger(paper)) == BOOK_SIZE
    assert len(_state(paper)["positions"]) == BOOK_SIZE


def test_a_missing_quote_refuses_the_whole_close_and_writes_nothing(tmp_path):
    paper = _seed_book(tmp_path)

    class HalfTerminal(FakeTerminal):
        def tick(self, symbol):
            if symbol == "GBPUSD.pro":
                raise LookupError("symbol not found")
            return super().tick(symbol)

    before = _digest(paper)
    outcome = _runner(paper, terminal=HalfTerminal()).close_stored_book()

    assert outcome["ok"] is False
    assert outcome["reason"] == REASON_FLATTEN_QUOTE
    assert [item["symbol"] for item in outcome["refusals"]] == ["GBPUSD.pro"]
    assert outcome["refusals"][0]["reason"] == "no_tick"
    assert _digest(paper) == before


def test_one_incomplete_position_refuses_the_whole_close_and_writes_nothing(tmp_path):
    def damage(records, fills):
        # Neither the stored record nor its entry fill carries a lot size, so the
        # close could not be priced from anything that actually happened.
        records[2]["lots"] = None
        fills[2]["lots"] = None

    paper = _seed_book(tmp_path, mutate=damage)
    before = _digest(paper)

    outcome = _runner(paper, terminal=FakeTerminal()).close_stored_book()

    assert outcome["ok"] is False
    assert outcome["reason"] == REASON_FLATTEN_FIELDS
    assert "positive lot size" in outcome["refusals"][0]["detail"]
    assert outcome["refusals"][0]["key"] == "cat-0002@EURUSD.pro|15m"
    assert _digest(paper) == before
    assert len(_ledger(paper)) == BOOK_SIZE


def test_a_position_the_ledger_never_opened_is_refused_rather_than_closed(tmp_path):
    """A close with no entry fill to pair with is not a trade, so it is not written."""

    def orphan(records, fills):
        fills.pop(4)

    paper = _seed_book(tmp_path, mutate=orphan)
    before = _digest(paper)

    outcome = _runner(paper, terminal=FakeTerminal()).close_stored_book()

    assert outcome["ok"] is False
    assert outcome["reason"] == REASON_FLATTEN_FIELDS
    assert outcome["refusals"][0]["reason"] == "no_entry_fill"
    assert _digest(paper) == before


def test_a_ledger_that_stops_taking_fills_leaves_the_rest_open_and_says_so(tmp_path):
    """A partial write is reported as partial: never a flat book that is not flat."""

    paper = _seed_book(tmp_path)
    runner = _runner(paper, terminal=FakeTerminal())
    real_append = runner._append_ledger  # noqa: SLF001 - the seam under test
    written = {"n": 0}

    def failing(event):
        # The fourth close event does not reach the file, and nothing after it may
        # pretend to.  Notes still go through, so only the fills are counted.
        if event.get("kind") == EVENT_CLOSED:
            written["n"] += 1
            if written["n"] > 3:
                return False
        return real_append(event)

    runner._append_ledger = failing  # type: ignore[assignment]

    outcome = runner.close_stored_book()

    assert outcome["ok"] is False
    assert outcome["reason"] == "paper_ledger_unreadable"
    assert outcome["closed"] == 3
    assert outcome["open"] == BOOK_SIZE - 3
    assert "could not be written to the paper ledger" in outcome["message"]
    state = _state(paper)
    assert len(state["positions"]) == BOOK_SIZE - 3, "the rest are still open"
    assert state["recovery"]["ok"] is False, "a half-closed book stays blocked"
    assert state["last_flatten"]["closed"] == 3
    account = accounting.read_account(paper, now=NOW)
    assert account["counts"]["open_positions"] == BOOK_SIZE - 3
    assert account["counts"]["closed_trades"] == 3
    # Every close that was written still pairs with its own entry fill...
    warnings = " ".join(item["detail"] for item in account["integrity"]["warnings"])
    assert "no entry fill before it" not in warnings
    # ...and the book that could not be closed is still reported as blocked, because
    # it is.
    assert account["integrity"]["ok"] is False
    assert "still open because their simulated close could not be written" in warnings


def test_the_close_is_refused_while_the_loop_is_running(tmp_path):
    paper = _seed_book(tmp_path)
    runner = _runner(paper, terminal=FakeTerminal())

    class Alive:
        def is_alive(self):
            return True

    runner._thread = Alive()  # noqa: SLF001 - the only way to stand in for a live loop
    before = _digest(paper)

    outcome = runner.close_stored_book()

    assert outcome["ok"] is False
    assert outcome["reason"] == REASON_FLATTEN_RUNNING
    assert "Stop it first" in outcome["message"]
    assert _digest(paper) == before


def test_an_unreadable_state_file_is_refused_and_left_alone(tmp_path):
    paper = _seed_book(tmp_path)
    (paper / "catalog-paper-state.json").write_text("{ not json", encoding="utf-8")
    before = _digest(paper)

    outcome = _runner(paper, terminal=FakeTerminal()).close_stored_book()

    assert outcome["ok"] is False
    assert outcome["reason"] == REASON_FLATTEN_STATE
    assert _digest(paper) == before


def test_a_second_close_reconciles_instead_of_closing_a_position_twice(tmp_path):
    """A crash between the ledger write and the state write must not double-close."""

    paper = _seed_book(tmp_path)
    stale_state = (paper / "catalog-paper-state.json").read_bytes()
    runner = _runner(paper, terminal=FakeTerminal())
    assert runner.close_stored_book()["ok"] is True

    # Stand the state file back up as it was: the ledger, however, already has the
    # closes.  The honest answer is to reconcile, not to write ten more fills.
    (paper / "catalog-paper-state.json").write_bytes(stale_state)
    again = _runner(paper, terminal=FakeTerminal()).close_stored_book()

    assert again["ok"] is True, again
    assert again["closed"] == 0
    assert again["reconciled"] == BOOK_SIZE
    closes = [event for event in _ledger(paper) if event.get("kind") == EVENT_CLOSED]
    assert len(closes) == BOOK_SIZE, "still one close per position"
    account = accounting.read_account(paper, now=NOW)
    assert account["counts"]["open_positions"] == 0
    assert account["integrity"]["ok"] is True, account["integrity"]["warnings"]
    assert _state(paper)["positions"] == []


# -- the guard rails -------------------------------------------------------


def test_the_confirmation_box_is_required_and_writes_nothing(tmp_path):
    paper = _seed_book(tmp_path)
    before = _digest(paper)
    panel = _panel(tmp_path, terminal=FakeTerminal())

    for payload in ({}, {"confirm": ""}, {"confirm": "yes"}, {"confirm": "close-all"}):
        outcome = panel.act(ACTION_PAPER_CLOSE_ALL, payload)
        assert outcome["ok"] is False
        assert outcome["reason"] == "confirmation_required"
        assert "tick the confirmation box" in outcome["message"]

    assert _digest(paper) == before


def test_without_a_terminal_there_is_no_price_and_nothing_is_closed(tmp_path):
    paper = _seed_book(tmp_path)
    before = _digest(paper)
    panel = CatalogPanel(root=tmp_path, terminal_provider=lambda: None, bars_provider=None)

    outcome = panel.act(ACTION_PAPER_CLOSE_ALL, {"confirm": CONFIRM_CLOSE_ALL})

    assert outcome["ok"] is False
    assert outcome["reason"] == "no_terminal"
    assert _digest(paper) == before


def _app(tmp_path: Path):
    """A real workstation whose only wired panel is the catalog one."""

    from axiom_forex.workstation.app import WorkstationApp
    from axiom_forex.workstation.setup import SetupWizard

    from tests.test_workstation_ui import live_session, offline_environment

    app = WorkstationApp(
        wizard=SetupWizard(environment=offline_environment(), session=live_session())
    )
    app.catalog = _panel(tmp_path, terminal=FakeTerminal())
    return app


def test_the_workstation_rejects_a_stale_nonce_and_writes_nothing(tmp_path):
    paper = _seed_book(tmp_path)
    before = _digest(paper)
    app = _app(tmp_path)

    status, _redirect, message = app.act(
        ACTION_PAPER_CLOSE_ALL,
        {"nonce": ["not-the-nonce"], "confirm": [CONFIRM_CLOSE_ALL]},
    )

    assert int(status) == 403
    assert "stale" in message
    assert _digest(paper) == before


def test_the_workstation_accepts_the_confirmed_close_and_returns_to_positions(tmp_path):
    paper = _seed_book(tmp_path)
    app = _app(tmp_path)

    status, redirect, message = app.act(
        ACTION_PAPER_CLOSE_ALL,
        {
            "nonce": [app.nonce],
            "confirm": [CONFIRM_CLOSE_ALL],
            "return_to": ["/positions"],
        },
    )

    assert int(status) == 303
    assert redirect == "/positions"
    assert message == ""
    assert app.catalog_result["ok"] is True
    assert accounting.read_account(paper, now=NOW)["counts"]["open_positions"] == 0


def test_nothing_on_the_close_path_can_reach_a_broker_or_change_the_mode(tmp_path):
    sources = {
        name: Path(name).read_text(encoding="utf-8")
        for name in (
            "axiom_forex/paper_runner/catalog_paper.py",
            "axiom_forex/strategies/catalog/panel.py",
        )
    }
    for name, source in sources.items():
        for forbidden in (
            "order_send",
            "order_check",
            "positions_get",
            "MetaTrader5",
            "mt5.",
            "live_arm",
            "set_live",
            "enable_live",
            "set_paper",
        ):
            assert forbidden not in source, f"{forbidden} appears in {name}"

    # The close reads quotes and specs through this surface and nothing else.
    paper = _seed_book(tmp_path)
    terminal = FakeTerminal()
    runner = CatalogPaperTest(quotes=terminal, watches=(), root=paper, clock=lambda: NOW)
    assert runner.close_stored_book()["ok"]
    assert {name for name, _symbol in terminal.calls} <= {"tick", "symbol_info"}


# -- the page ---------------------------------------------------------------


def test_the_positions_page_offers_the_close_only_when_the_book_cannot_resume(tmp_path):
    paper = _seed_book(tmp_path)
    app = _account_app(paper)
    html = render(app, "/positions")
    text = _text(html)

    assert "has to be closed before the paper test can start" in text
    assert "Simulate closing all paper positions" in text
    assert "no order is sent to a broker" in text
    assert "no MetaTrader 5 position is touched" in text
    assert "no watched row matches this position" in text
    # The form itself: the action, the process nonce, and the confirmation box that
    # a person has to tick before anything is written.
    assert "action='/actions/catalog-paper-close-all'" in html
    assert f"name='nonce' value='{app.nonce}'" in html
    assert "name='confirm' value='close-all-paper-positions'" in html
    assert "type='checkbox'" in html and "required" in html
    assert "name='return_to' value='/positions'" in html


def test_positions_known_only_to_the_ledger_can_still_be_closed(tmp_path):
    """The ledger is authoritative for which positions are open; the close uses it."""

    paper = _seed_book(tmp_path)
    (paper / "catalog-paper-state.json").unlink()

    outcome = _runner(paper, terminal=FakeTerminal()).close_stored_book()

    assert outcome["ok"] is True, outcome
    assert outcome["closed"] == BOOK_SIZE
    account = accounting.read_account(paper, now=NOW)
    assert account["counts"]["open_positions"] == 0
    assert account["integrity"]["ok"] is True, account["integrity"]["warnings"]
    state = _state(paper)
    assert state["positions"] == []
    assert len(state["flattened"]) == BOOK_SIZE
    # The records are gone, so the archive is what says what was closed.
    assert {item["key"] for item in state["flattened"]} == {
        event["key"] for event in _ledger(paper) if event.get("kind") == EVENT_CLOSED
    }


def test_a_book_with_no_recovery_report_says_so_and_offers_the_close(tmp_path):
    paper = _seed_book(tmp_path)
    state = _state(paper)
    state.pop("recovery")
    (paper / "catalog-paper-state.json").write_text(json.dumps(state), encoding="utf-8")
    app = _account_app(paper)

    html = render(app, "/positions")
    text = _text(html)

    # Never the confident claim: with no report the page cannot prove a resume.
    assert "cannot prove these positions can be resumed" in text
    assert "can be resumed exactly" not in text
    assert "Simulate closing all paper positions" in text
    assert "action='/actions/catalog-paper-close-all'" in html


def test_a_book_that_can_resume_is_not_offered_a_close(tmp_path):
    paper = _seed_book(tmp_path)
    state = _state(paper)
    state["recovery"] = {"ok": True, "version": STATE_VERSION, "restored": BOOK_SIZE,
                         "migrated": 0, "failed": [], "detail": ""}
    (paper / "catalog-paper-state.json").write_text(json.dumps(state), encoding="utf-8")
    app = _account_app(paper)

    html = render(app, "/positions")
    text = _text(html)

    assert "can be resumed exactly" in text
    assert "start the paper autopilot to carry on" in text
    assert "Simulate closing all paper positions" not in text
    assert "/actions/catalog-paper-close-all" not in html


def test_the_dashboard_and_the_positions_page_agree_after_the_close(tmp_path):
    paper = _seed_book(tmp_path)
    assert _runner(paper, terminal=FakeTerminal()).close_stored_book()["ok"] is True

    app = _account_app(paper)
    account = app.paper_account()
    assert account["counts"]["open_positions"] == 0
    assert account["counts"]["closed_trades"] == BOOK_SIZE
    assert account["integrity"]["ok"] is True

    positions_text = _text(render(app, "/positions"))
    assert "Nothing open" in positions_text
    assert f"{BOOK_SIZE} trade(s)" in positions_text
    assert "has to be closed before the paper test can start" not in positions_text
    performance_text = _text(render(app, "/performance"))
    assert f"{BOOK_SIZE} trade(s)" in performance_text


# -- and then the autopilot can start --------------------------------------


def test_the_paper_autopilot_starts_after_a_valid_flatten(tmp_path):
    paper = _seed_book(tmp_path)
    assert _runner(paper, terminal=FakeTerminal()).start()["started"] is False

    assert _runner(paper, terminal=FakeTerminal()).close_stored_book()["ok"] is True

    # What the autopilot does on Start: build a runner over the same root with the
    # rows it just selected, and start it.  It used to be refused here.
    runner = _runner(paper, terminal=FakeTerminal())
    outcome = runner.start()
    try:
        assert outcome["started"] is True, outcome
        assert outcome["status"] == "RUNNING"
        assert outcome["blockers"] == []
        assert runner.running is True
    finally:
        runner.stop()
    assert _state(paper)["positions"] == []


def test_the_autopilot_itself_starts_after_the_flatten(tmp_path):
    """The whole story, at the level the operator clicks: Start, refused, close, Start."""

    from datetime import timedelta

    from tests.test_catalog_autopilot import (
        TIMEFRAME,
        _candidate_id,
        _passing_row,
        _start_with_report,
    )

    _seed_book(tmp_path)
    candidate_id = _candidate_id()
    rows = lambda boundaries: [  # noqa: E731 - one row, as the selection would give
        _passing_row(boundaries[("EURUSD.pro", TIMEFRAME)] - timedelta(hours=1), candidate_id)
    ]

    first = _start_with_report(tmp_path, rows)
    status = first.status()
    assert status["state"] == "BLOCKED", status
    assert status["blockers"][0]["reason"] == REASON_STATE_RECOVERY
    account = accounting.read_account(_paper_root(tmp_path), now=NOW)
    assert account["counts"]["open_positions"] == BOOK_SIZE

    closed = first.flatten()
    assert closed["ok"] is True, closed
    assert closed["closed"] == BOOK_SIZE

    second = _start_with_report(tmp_path, rows)
    try:
        assert second.status()["state"] == "RUNNING", second.status()
        assert second.status()["paper"]["running"] is True
    finally:
        second.stop()


def test_the_autopilot_flatten_closes_a_book_this_process_does_not_hold(tmp_path):
    """After a restart the autopilot holds no positions; the close still works."""

    from axiom_forex.strategies.catalog.autopilot import Autopilot

    paper = _seed_book(tmp_path)
    panel = _panel(tmp_path, terminal=FakeTerminal())
    pilot = Autopilot(panel=panel, clock=lambda: NOW)
    assert pilot._paper is None  # noqa: SLF001 - the point of the test

    outcome = pilot.flatten()

    assert outcome["ok"] is True, outcome
    assert outcome["closed"] == BOOK_SIZE
    assert accounting.read_account(paper, now=NOW)["counts"]["open_positions"] == 0
    # And the panel's own action sees a flat book rather than closing anything twice.
    flat = panel.act(ACTION_PAPER_CLOSE_ALL, {"confirm": CONFIRM_CLOSE_ALL})
    assert flat["ok"] is True
    assert "already flat" in flat["message"]
