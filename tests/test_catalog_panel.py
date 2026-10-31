"""The Monday flow end to end, with a fake terminal and fake bars.

Nothing here contacts MetaTrader, reads an account, places an order, or changes an
execution mode.  The terminal is a dict-shaped stand-in; the bars are a fixture.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from axiom_forex.strategies.catalog.candidates import catalog
from axiom_forex.strategies.catalog.panel import (
    ACTION_EVALUATE,
    ACTION_PAPER_FLAT,
    ACTION_PAPER_START,
    ACTION_PAPER_STOP,
    CatalogPanel,
)
from axiom_forex.strategies.catalog.store import RECORD_VERSION

UTC = timezone.utc
NOW = datetime(2026, 11, 2, 9, 20, tzinfo=UTC)


def _symbol_record(name: str, base: str, profit: str, *, tradable: bool = True) -> dict[str, Any]:
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
        "trade_allowed": tradable,
        "spread": 4,
        "swap_mode": 1,
        "swap_long": Decimal("-0.5"),
        "swap_short": Decimal("0.2"),
    }


class FakeTick:
    def __init__(self, symbol: str, bid: float, ask: float, when: datetime) -> None:
        self.symbol = symbol
        self.bid = bid
        self.ask = ask
        self.time = when


class FakeTerminal:
    """The narrow read-only surface the panel is allowed to touch."""

    def __init__(self, *, age_seconds: float = 1.0, bars: int = 700) -> None:
        self.age = age_seconds
        self.bar_count = bars
        self.calls: list[str] = []

    def symbol_names(self) -> list[str]:
        return ["EURUSD.pro", "GBPUSD.pro", "AAOIUSD", "XAUUSD"]

    def symbol_info(self, name: str):
        self.calls.append(f"symbol_info:{name}")
        table = {
            "EURUSD.pro": _symbol_record(name, "EUR", "USD"),
            "GBPUSD.pro": _symbol_record(name, "GBP", "USD"),
            "AAOIUSD": _symbol_record(name, "", "USD"),
            "XAUUSD": _symbol_record(name, "XAU", "USD"),
        }
        record = table.get(name)
        if record is None:
            raise LookupError("symbol not found")
        return [record]

    def tick(self, symbol: str) -> FakeTick:
        self.calls.append(f"tick:{symbol}")
        return FakeTick(symbol, 1.10000, 1.10004, NOW - timedelta(seconds=self.age))

    def rates(self, symbol: str, timeframe: str, start: datetime, end: datetime):
        self.calls.append(f"rates:{symbol}:{timeframe}")
        rows = []
        previous = 1.1000
        step = 900 if timeframe == "15m" else 3600
        base_time = NOW - timedelta(seconds=step * self.bar_count)
        for index in range(self.bar_count):
            drift = 0.0004 if (index // 120) % 2 == 0 else -0.0004
            close = round(previous + drift, 5)
            rows.append(
                {
                    "timestamp": base_time + timedelta(seconds=step * index),
                    "open": round(previous, 5),
                    "high": round(max(previous, close) + 0.00003, 5),
                    "low": round(min(previous, close) - 0.00003, 5),
                    "close": close,
                    "spread_points": 4,
                    "tick_volume": 3,
                }
            )
            previous = close
        return rows


def _bars(symbol: str, timeframe: str, count: int):
    step = 900 if timeframe == "15m" else 3600
    rows = []
    previous = 1.1000
    start = NOW - timedelta(seconds=step * count)
    for index in range(count):
        drift = 0.0004 if (index // 150) % 2 == 0 else -0.0004
        close = round(previous + drift, 5)
        rows.append(
            {
                "timestamp": start + timedelta(seconds=step * index),
                "open": round(previous, 5),
                "high": round(max(previous, close) + 0.00003, 5),
                "low": round(min(previous, close) - 0.00003, 5),
                "close": close,
                "spread_points": 4,
                "tick_volume": 3,
            }
        )
        previous = close
    return rows


def _panel(tmp_path: Path, terminal: FakeTerminal | None = None) -> CatalogPanel:
    terminal = terminal or FakeTerminal()
    return CatalogPanel(
        root=tmp_path,
        terminal_provider=lambda: terminal,
        bars_provider=_bars,
        budget_seconds=30.0,
        clock=lambda: NOW,
    )


def _wait_for(predicate, *, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("the background evaluation did not finish in time")


# -- the symbol offer -------------------------------------------------------


def test_the_symbol_offer_is_forex_only_and_majors_first(tmp_path):
    panel = _panel(tmp_path)
    offer = panel.fx_choices()
    assert offer["available"] is True
    pairs = [item["pair"] for item in offer["choices"]]
    assert "EUR/USD" in pairs and "GBP/USD" in pairs
    assert "XAU/USD" not in pairs, "a metal is not a currency pair"
    excluded = {item["broker_symbol"] for item in offer["excluded"]}
    assert {"AAOIUSD", "XAUUSD"} <= excluded
    assert offer["recommended"][0] in {"EURUSD.pro", "GBPUSD.pro"}


def test_without_a_terminal_the_offer_says_so_instead_of_inventing_symbols(tmp_path):
    panel = CatalogPanel(root=tmp_path, terminal_provider=lambda: None, bars_provider=_bars)
    offer = panel.fx_choices()
    assert offer["available"] is False
    assert offer["choices"] == []


# -- evaluation -------------------------------------------------------------


def test_an_evaluation_runs_writes_a_record_and_reports_its_counts(tmp_path):
    panel = _panel(tmp_path)
    outcome = panel.act(ACTION_EVALUATE, {"symbols": "EURUSD.pro", "timeframe": "15m"})
    assert outcome["ok"] is True, outcome
    _wait_for(lambda: not panel.evaluation_view()["running"])
    view = panel.evaluation_view()
    assert view["on_record"] is True
    assert view["counts"]["tested"] > 0
    assert view["data_origin"] in {"MT5_TERMINAL_READ_ONLY", "MIXED_TERMINAL_AND_SAMPLE"}
    record = json.loads(Path(view["record_path"]).read_text(encoding="utf-8"))
    assert record["record_version"] == RECORD_VERSION
    assert record["counts"]["candidates_considered"] >= 1000


def test_evaluating_without_a_terminal_is_refused(tmp_path):
    panel = CatalogPanel(root=tmp_path, terminal_provider=lambda: None, bars_provider=_bars)
    outcome = panel.act(ACTION_EVALUATE, {"symbols": "EURUSD.pro"})
    assert outcome["ok"] is False
    assert outcome["reason"] == "no_terminal"


def test_evaluating_without_bars_is_refused_rather_than_filled_with_samples(tmp_path):
    panel = CatalogPanel(
        root=tmp_path,
        terminal_provider=lambda: FakeTerminal(),
        bars_provider=lambda symbol, timeframe, count: [],
    )
    outcome = panel.act(ACTION_EVALUATE, {"symbols": "EURUSD.pro", "timeframe": "15m"})
    assert outcome["ok"] is False
    assert outcome["reason"] == "no_usable_history"


# -- the paper test ---------------------------------------------------------


def _write_record(panel: CatalogPanel, *, candidate_id: str, symbol: str = "EURUSD.pro") -> None:
    payload = {
        "record_version": RECORD_VERSION,
        "status": "COMPLETE",
        "written_at": NOW.isoformat(),
        "data_origin": "MT5_TERMINAL_READ_ONLY",
        "counts": {"tested": 12, "skipped": 0, "failed": 0},
        "plan": {"slots": [{"origin": "MT5_TERMINAL_READ_ONLY"}]},
        "claim": "no profit claim",
        "paper_set": [
            {
                "candidate_id": candidate_id,
                "symbol": symbol,
                "timeframe": "15m",
                "folds_positive": 3,
                "score": 0.01,
            }
        ],
    }
    panel.store.root.mkdir(parents=True, exist_ok=True)
    panel.store.path.write_text(json.dumps(payload), encoding="utf-8")


def test_the_paper_test_cannot_start_without_an_evaluation_on_record(tmp_path):
    panel = _panel(tmp_path)
    outcome = panel.act(ACTION_PAPER_START, {})
    assert outcome["ok"] is False
    assert outcome["reason"] == "no_evaluation_on_record"


def test_the_paper_test_starts_from_the_screened_set_and_stops_cleanly(tmp_path):
    panel = _panel(tmp_path)
    candidate = catalog()[0]
    _write_record(panel, candidate_id=candidate.candidate_id)
    started = panel.act(ACTION_PAPER_START, {})
    assert started["ok"] is True, started
    view = panel.paper_view()
    assert view["running"] is True
    assert view["watches"] and view["watches"][0]["candidate_id"] == candidate.candidate_id
    stopped = panel.act(ACTION_PAPER_STOP, {})
    assert stopped["ok"] is True
    assert panel.paper_view()["running"] is False
    flat = panel.act(ACTION_PAPER_FLAT, {})
    assert flat["ok"] is True


def test_a_second_start_is_refused_and_the_first_run_is_left_alone(tmp_path):
    panel = _panel(tmp_path)
    candidate = catalog()[0]
    _write_record(panel, candidate_id=candidate.candidate_id)
    assert panel.act(ACTION_PAPER_START, {})["ok"] is True
    second = panel.act(ACTION_PAPER_START, {})
    assert second["ok"] is False
    assert second["reason"] == "paper_test_already_running"
    assert panel.paper_view()["running"] is True
    panel.act(ACTION_PAPER_STOP, {})


def test_a_screened_row_for_a_symbol_the_terminal_does_not_offer_is_refused(tmp_path):
    panel = _panel(tmp_path)
    candidate = catalog()[0]
    _write_record(panel, candidate_id=candidate.candidate_id, symbol="NOT_A_SYMBOL")
    outcome = panel.act(ACTION_PAPER_START, {})
    assert outcome["ok"] is False
    assert outcome["reason"] == "no_usable_watch"


def test_the_panel_view_is_json_friendly_and_says_what_it_cannot_do(tmp_path):
    panel = _panel(tmp_path)
    view = panel.view()
    assert view["available"] is True
    assert "not a profitable strategy" in view["claim"]
    assert panel.act("catalog-not-a-thing", {})["ok"] is False
    panel.stop()


def test_a_restart_picks_up_a_newer_evaluation(tmp_path):
    panel = _panel(tmp_path)
    first = catalog()[0]
    second = catalog()[1]
    _write_record(panel, candidate_id=first.candidate_id)
    assert panel.act(ACTION_PAPER_START, {})["ok"] is True
    panel.act(ACTION_PAPER_STOP, {})
    assert panel.paper_view()["watches"][0]["candidate_id"] == first.candidate_id
    _write_record(panel, candidate_id=second.candidate_id)
    assert panel.act(ACTION_PAPER_START, {})["ok"] is True
    assert panel.paper_view()["watches"][0]["candidate_id"] == second.candidate_id
    panel.act(ACTION_PAPER_STOP, {})


def test_a_running_test_does_not_show_start_as_its_own_blocker(tmp_path):
    panel = _panel(tmp_path)
    candidate = catalog()[0]
    _write_record(panel, candidate_id=candidate.candidate_id)
    assert panel.act(ACTION_PAPER_START, {})["ok"] is True
    reasons = {item["reason"] for item in panel.paper_view()["blockers"]}
    assert "paper_test_already_running" not in reasons
    panel.act(ACTION_PAPER_STOP, {})
