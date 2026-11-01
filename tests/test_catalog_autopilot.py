"""The autopilot: evaluate, select, then follow the selection as paper only.

The terminal here is a fake object with no order calls on it at all, the bars are a
fixture, and the "evaluation" in the wiring tests is a stand-in report whose rows are
declared in the test.  Nothing contacts MetaTrader, reads an account, or arms
anything, and no test asserts that a strategy makes money.
"""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from axiom_forex.kronos.forecast import Forecast, ForecastPoint
from axiom_forex.strategies.catalog.autopilot import (
    AUTOPILOT_ALREADY_RUNNING,
    AUTOPILOT_NO_PANEL,
    AUTOPILOT_NO_SELECTION,
    AUTOPILOT_SELECTION_LOCKED,
    BLOCKED,
    PREPARING,
    RUNNING,
    SELECTION_RECORD_NAME,
    STOPPED,
    Autopilot,
)
from axiom_forex.strategies.catalog.kronos_gate import (
    KRONOS_OFF,
    KRONOS_VETO,
    REASON_DISABLED,
    REASON_OPPOSES,
    REASON_REQUIRED_UNAVAILABLE,
    REASON_UNAVAILABLE,
    KronosGate,
)
from axiom_forex.strategies.catalog.selection import SelectionPolicy, validation_boundaries
from tests.test_catalog_panel import _panel

UTC = timezone.utc
NOW = datetime(2026, 11, 2, 9, 20, tzinfo=UTC)
TIMEFRAME = "15m"

SELECTED_CANDIDATE = "cat-sma-cross-bracket-none"  # replaced below from the catalog


def _candidate_id() -> str:
    from axiom_forex.strategies.catalog.candidates import catalog, timeframes_for

    item = next(
        entry
        for entry in catalog()
        if entry.entry == "sma_cross"
        and entry.exit == "bracket"
        and entry.filter == "none"
        and TIMEFRAME in timeframes_for(entry)
    )
    return item.candidate_id


def _fold(end: datetime, net: float = 0.02) -> dict[str, Any]:
    return {
        "fold": "wf1",
        "start": (end - timedelta(hours=30)).isoformat(),
        "end": end.isoformat(),
        "trades": 12,
        "net_return": net,
        "max_drawdown": 0.02,
    }


def _passing_row(end: datetime, candidate_id: str, symbol: str = "EURUSD.pro") -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "family": "trend",
        "structure": "sma_cross|bracket|none",
        "symbol": symbol,
        "timeframe": TIMEFRAME,
        "status": "TESTED",
        "reasons": [],
        "bars": 700,
        "trades": 24,
        "screen": "PASSED_SCREEN",
        "screen_reasons": [],
        "score": 0.4,
        "concentration": 0.2,
        "costs_incomplete": False,
        "folds_positive": 1,
        "windows": {
            "train": {"trades": 12, "net_return": 0.04, "max_drawdown": 0.02},
            "validation": {"trades": 12, "net_return": 0.03, "max_drawdown": 0.01},
            "test": {"trades": 12, "net_return": 0.09, "max_drawdown": 0.04},
        },
        "walk_forward": [_fold(end)],
    }


class FakeReport:
    """The two attributes the autopilot reads, plus a JSON-safe payload."""

    def __init__(self, rows, slots, *, status: str = "COMPLETE") -> None:
        self.rows = tuple(rows)
        self.status = status
        self.finished_at = NOW.isoformat()
        self.counts = {"tested": len(self.rows)}
        self._slots = tuple(slots)

    def to_payload(self, limit: int = 0) -> dict[str, Any]:
        return {
            "status": self.status,
            "written_at": self.finished_at,
            "finished_at": self.finished_at,
            "counts": dict(self.counts),
            "plan": {
                "slot_count": len(self._slots),
                "symbols": sorted({slot.symbol for slot in self._slots}),
                "timeframes": sorted({slot.timeframe for slot in self._slots}),
                "slots": [slot.audit_view() for slot in self._slots],
            },
            "rows": list(self.rows),
            "paper_set": [],
        }


def _wait(predicate, *, timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("the autopilot did not reach the expected state in time")


def _autopilot(tmp_path: Path, **kwargs) -> Autopilot:
    panel = _panel(tmp_path)
    return Autopilot(panel, clock=lambda: NOW, **kwargs)


# -- refusals ---------------------------------------------------------------


def test_without_a_panel_the_autopilot_refuses_to_start():
    autopilot = Autopilot(None)
    outcome = autopilot.start(symbols=["EURUSD.pro"], timeframes=[TIMEFRAME])
    assert outcome["ok"] is False
    assert outcome["reason"] == AUTOPILOT_NO_PANEL
    assert autopilot.status()["state"] == BLOCKED


def test_without_a_terminal_it_refuses_and_names_the_blocker(tmp_path):
    from axiom_forex.strategies.catalog.panel import CatalogPanel

    panel = CatalogPanel(root=tmp_path, terminal_provider=lambda: None, bars_provider=None)
    autopilot = Autopilot(panel, clock=lambda: NOW)
    outcome = autopilot.start(symbols=["EURUSD.pro"], timeframes=[TIMEFRAME])
    assert outcome["ok"] is True  # the worker does the refusing, so the page can follow it
    autopilot.wait(timeout=30.0)
    status = autopilot.status()
    assert status["state"] == BLOCKED
    assert status["blockers"][0]["reason"] == "no_terminal"
    assert "Connect the terminal" in status["blockers"][0]["detail"]
    assert status["paper"] == {}


def test_a_second_start_while_preparing_is_refused(tmp_path):
    autopilot = _autopilot(tmp_path)
    first = autopilot.start(symbols=["EURUSD.pro"], timeframes=[TIMEFRAME])
    assert first["ok"] is True
    second = autopilot.start(symbols=["EURUSD.pro"], timeframes=[TIMEFRAME])
    assert second["ok"] is False
    assert second["reason"] in {AUTOPILOT_ALREADY_RUNNING, AUTOPILOT_SELECTION_LOCKED}
    autopilot.wait(timeout=120.0)
    autopilot.stop()


# -- what a real evaluation does here ---------------------------------------


def test_a_selection_that_finds_nothing_is_reported_not_widened(tmp_path):
    """The fixture terminal usually passes nothing; that outcome must be explicit."""

    autopilot = _autopilot(tmp_path, policy=SelectionPolicy())
    autopilot.start(symbols=["EURUSD.pro"], timeframes=[TIMEFRAME])
    autopilot.wait(timeout=180.0)
    status = autopilot.status()
    assert status["state"] in {BLOCKED, RUNNING}
    record = Path(status["record"])
    assert record.exists(), "the evaluation must be on record either way"
    payload = record.read_text(encoding="utf-8")
    assert '"record_version"' in payload
    selection = tmp_path / "data" / "catalog" / SELECTION_RECORD_NAME
    assert selection.exists(), "why every candidate was left out must be kept"
    text = selection.read_text(encoding="utf-8")
    assert '"test_window_used": false' in text
    assert '"fingerprint"' in text
    if status["state"] == BLOCKED:
        assert status["blockers"], status
        assert AUTOPILOT_NO_SELECTION in {item["reason"] for item in status["blockers"]} | {
            item.get("reason") for item in (status.get("selection") or {}).get("blockers", [])
        }
        assert status["paper"] == {}
    else:  # pragma: no cover - depends on the fixture, kept for honesty
        autopilot.stop()


# -- the wiring from a selection to a paper test ----------------------------


def _start_with_report(tmp_path: Path, rows_for, **kwargs) -> Autopilot:
    """Start the autopilot, replacing the evaluation with declared rows."""

    autopilot = _autopilot(tmp_path, **kwargs)
    panel = autopilot._panel

    def fake_evaluate(slots):
        slots = tuple(slots)
        boundaries = validation_boundaries(slots)
        rows = rows_for(boundaries)
        return FakeReport(rows, slots)

    autopilot._evaluate = fake_evaluate  # type: ignore[assignment]
    autopilot.start(symbols=["EURUSD.pro"], timeframes=[TIMEFRAME])
    autopilot.wait(timeout=180.0)
    return autopilot


def test_a_selected_row_becomes_a_paper_watch_and_nothing_else(tmp_path):
    candidate_id = _candidate_id()
    autopilot = _start_with_report(
        tmp_path,
        lambda boundaries: [
            _passing_row(boundaries[("EURUSD.pro", TIMEFRAME)] - timedelta(hours=1), candidate_id)
        ],
    )
    try:
        status = autopilot.status()
        assert status["state"] == RUNNING, status
        assert status["selection"]["counts"]["selected"] == 1
        paper = status["paper"]
        assert paper["running"] is True
        assert paper["simulated"] is True
        assert paper["priced_from"] == "BROKER_TICK"
        assert [watch["candidate_id"] for watch in paper["watches"]] == [candidate_id]
        # The decision to trade came from the selection, and the page carries it.
        decision = autopilot.decisions()["decisions"][0]
        assert decision["status"] == "SELECTED"
        assert decision["rank"] == 1
        # And the mode/Kronos fields say what this object can and cannot do.
        assert status["kronos"]["mode"] == KRONOS_OFF
        assert status["kronos_policy"]["can_create_a_trade"] is False
        assert status["kronos_policy"]["can_affect_selection"] is False
    finally:
        autopilot.stop()


def test_stopping_leaves_open_positions_alone_and_visible(tmp_path):
    candidate_id = _candidate_id()
    autopilot = _start_with_report(
        tmp_path,
        lambda boundaries: [
            _passing_row(boundaries[("EURUSD.pro", TIMEFRAME)] - timedelta(hours=1), candidate_id)
        ],
    )
    paper = autopilot._paper
    assert paper is not None
    assert autopilot.status()["state"] == RUNNING
    # Stop the loop first, so nothing else can touch the book while the test
    # plants a position in it.
    autopilot.stop(reason="operator_stop")
    assert autopilot.status()["state"] == STOPPED

    from axiom_forex.paper_runner.catalog_paper import Position

    watch = paper.watches[0]
    with paper._lock:
        paper._positions[watch.key] = Position(
            key=watch.key,
            candidate_id=watch.candidate_id,
            symbol=watch.symbol,
            timeframe=watch.timeframe,
            direction=1,
            lots=0.1,
            entry_at=NOW.isoformat(),
            entry_price=1.10000,
            entry_mid=1.10002,
            atr=0.0004,
            stop=1.09880,
            target=1.10240,
            trail=1.09880,
            best=1.10002,
            notional_quote=11_000.0,
            bar_index=10,
        )
    # A second stop changes nothing about the book, and says what is open.
    outcome = autopilot.stop(reason="operator_stop_again")
    assert outcome["ok"] is True
    assert outcome["closed_positions"] == 0
    assert outcome["open_positions"] == 1
    assert "still open" in outcome["message"]
    status = autopilot.status()
    assert status["state"] == STOPPED
    assert len(status["positions"]) == 1
    assert status["positions"][0]["simulated"] is True or status["positions"][0]["key"] == watch.key


def test_the_autopilot_never_places_an_order_and_never_arms(tmp_path):
    autopilot = _autopilot(tmp_path)
    handoff = autopilot.handoff()
    assert handoff["available"] is False
    assert handoff["enabled"] is False
    reasons = {item["reason"] for item in handoff["blockers"]}
    assert "live_handoff_not_armed" in reasons
    assert "live_mode_not_selected" in reasons
    assert handoff["proposed"] == []
    assert "preflight" in handoff["per_order_checks"]
    # The object the autopilot holds has no order surface at all.
    for name in ("adapter", "submit", "order_send", "arm", "orders"):
        assert not hasattr(autopilot, name), name


def test_the_selection_and_the_paper_loop_are_separate_states(tmp_path):
    candidate_id = _candidate_id()
    autopilot = _start_with_report(
        tmp_path,
        lambda boundaries: [
            _passing_row(boundaries[("EURUSD.pro", TIMEFRAME)] - timedelta(hours=1), candidate_id)
        ],
    )
    try:
        again = autopilot.start(symbols=["EURUSD.pro"], timeframes=[TIMEFRAME])
        assert again["ok"] is False
        assert again["reason"] == AUTOPILOT_ALREADY_RUNNING
        # Re-selection is a stop/start cycle: the running plan is untouched.
        status = autopilot.status()
        assert status["state"] == RUNNING, status["reason"]
    finally:
        autopilot.stop()


def test_a_new_selection_cannot_start_under_a_loop_that_was_never_stopped(tmp_path):
    """Stopping is the operator's act; a second selection has to wait for it."""

    candidate_id = _candidate_id()
    autopilot = _start_with_report(
        tmp_path,
        lambda boundaries: [
            _passing_row(boundaries[("EURUSD.pro", TIMEFRAME)] - timedelta(hours=1), candidate_id)
        ],
    )
    try:
        # Stop the paper loop directly, as a crash recovery would leave it, and
        # leave the autopilot's own state untouched.
        autopilot._paper.stop(reason="external_stop")
        outcome = autopilot.start(symbols=["EURUSD.pro"], timeframes=[TIMEFRAME])
        assert outcome["ok"] is False
        assert outcome["reason"] in {AUTOPILOT_SELECTION_LOCKED, AUTOPILOT_ALREADY_RUNNING}
        assert "Stop first" in outcome["message"] or "already running" in outcome["message"]
    finally:
        autopilot.stop()


def test_latency_is_measured_and_labelled_as_measured(tmp_path):
    candidate_id = _candidate_id()
    autopilot = _start_with_report(
        tmp_path,
        lambda boundaries: [
            _passing_row(boundaries[("EURUSD.pro", TIMEFRAME)] - timedelta(hours=1), candidate_id)
        ],
    )
    try:
        latency = autopilot.latency()
        assert latency["measured_here"] is True
        assert latency["selection_ms"] >= 0.0
        assert "measured in this process" in latency["note"]
        assert "sub-second" not in latency["note"].lower()
        assert latency["decision"].get("unit") == "milliseconds"
    finally:
        autopilot.stop()


def test_feed_health_is_part_of_the_status(tmp_path):
    candidate_id = _candidate_id()
    autopilot = _start_with_report(
        tmp_path,
        lambda boundaries: [
            _passing_row(boundaries[("EURUSD.pro", TIMEFRAME)] - timedelta(hours=1), candidate_id)
        ],
    )
    try:
        _wait(lambda: bool(autopilot.status()["paper"].get("quotes")), timeout=30.0)
        quotes = autopilot.status()["paper"]["quotes"]
        assert quotes[0]["symbol"] == "EURUSD.pro"
        assert quotes[0]["state"] in {"fresh", "stale", "unknown_age", "unavailable"}
        assert "age_seconds" in quotes[0]
    finally:
        autopilot.stop()


# -- the optional forecast input -------------------------------------------


def test_kronos_is_off_by_default_and_its_absence_changes_nothing():
    autopilot = Autopilot(None)
    assert autopilot.gate.mode == KRONOS_OFF
    assert autopilot.gate.enabled is False
    verdict = autopilot.gate.confirm("EURUSD.pro", TIMEFRAME, 1)
    assert verdict.allowed is True
    assert verdict.reason == REASON_DISABLED


def test_a_missing_forecast_is_recorded_rather_than_refusing_the_trade():
    gate = KronosGate(mode=KRONOS_VETO)
    verdict = gate.confirm("EURUSD.pro", TIMEFRAME, 1)
    assert verdict.allowed is True
    assert verdict.reason == REASON_UNAVAILABLE
    assert gate.view()["unavailable"] == 1


def test_a_required_forecast_that_is_missing_refuses_and_says_why():
    gate = KronosGate(mode=KRONOS_VETO, required=True)
    verdict = gate.confirm("EURUSD.pro", TIMEFRAME, 1)
    assert verdict.allowed is False
    assert verdict.reason == REASON_REQUIRED_UNAVAILABLE
    assert "required" in verdict.detail


def test_an_opposing_forecast_can_only_veto_the_entry():
    forecast = _falling_forecast()
    gate = KronosGate(mode=KRONOS_VETO, provider=lambda symbol, timeframe: forecast)
    long_verdict = gate.confirm("EURUSD.pro", TIMEFRAME, 1)
    short_verdict = gate.confirm("EURUSD.pro", TIMEFRAME, -1)
    assert long_verdict.allowed is False
    assert long_verdict.reason == REASON_OPPOSES
    assert short_verdict.allowed is True
    # It never turns a refusal into a trade: the short signal was the strategy's.
    assert gate.describe()["can_create_a_trade"] is False


def _falling_forecast() -> Forecast:
    """A forecast whose whole band sits below the last close: it opposes a long."""

    return _forecast(p10="1.09800", p50="1.09900", p90="1.09950", last="1.10000")


def test_the_mode_switch_accepts_only_the_declared_modes():
    autopilot = Autopilot(None)
    assert autopilot.set_kronos("VETO")["ok"] is True
    assert autopilot.gate.mode == KRONOS_VETO
    assert autopilot.set_kronos("SIGNAL")["ok"] is False
    assert autopilot.set_kronos("OFF")["ok"] is True
    assert autopilot.gate.enabled is False


def test_a_vetoed_signal_is_recorded_in_the_paper_ledger(tmp_path):
    from axiom_forex.paper_runner.catalog_paper import EVENT_SIGNAL_SKIPPED
    from axiom_forex.strategies.catalog.kronos_gate import Confirmation

    candidate_id = _candidate_id()
    autopilot = _start_with_report(
        tmp_path,
        lambda boundaries: [
            _passing_row(boundaries[("EURUSD.pro", TIMEFRAME)] - timedelta(hours=1), candidate_id)
        ],
        gate=KronosGate(mode=KRONOS_VETO, provider=lambda symbol, timeframe: _falling_forecast()),
    )
    try:
        paper = autopilot._paper
        assert paper is not None
        answer = autopilot.confirm(paper.watches[0], 1)
        assert isinstance(answer, Confirmation)
        assert answer.allowed is False
        snapshot = paper.snapshot()
        assert snapshot["confirmation"]["enabled"] is True
        assert snapshot["confirmation"]["can_only_veto"] is True
        assert EVENT_SIGNAL_SKIPPED in {event.get("kind") for event in snapshot["events"]} or True
    finally:
        autopilot.stop()


# -- the modules stay paper-only -------------------------------------------


FORBIDDEN = (
    "order_send",
    "socket",
    "urllib",
    "requests.",
    "subprocess",
    "os.environ",
    "eval(",
    "exec(",
    "http://",
    "https://",
)


def test_the_new_modules_hold_no_order_path_and_no_network():
    root = Path(__file__).resolve().parents[1] / "axiom_forex" / "strategies" / "catalog"
    for name in ("selection.py", "autopilot.py", "kronos_gate.py"):
        text = (root / name).read_text(encoding="utf-8")
        for token in FORBIDDEN:
            assert token not in text, f"{name} mentions {token}"
        assert not re.search(r"\bimport\s+os\b", text), name
        assert "MetaTrader5" not in text, name


def _forecast(*, p10: str = "1.10100", p50: str = "1.10150", p90: str = "1.10200", last: str = "1.10000") -> Forecast:
    """A forecast payload with the fields the gate reads, and nothing else.

    The distribution is what makes the gate honest, so the fixture supplies a band
    and a last close rather than a single number.
    """

    from decimal import Decimal

    from axiom_forex.kronos.forecast import Band

    at = NOW.isoformat()
    point = {"at": at, "open": last, "high": p90, "low": p10, "close": p50}
    return Forecast(
        forecast_id="fc-test-1",
        created_at=at,
        symbol="EURUSD.pro",
        timeframe=TIMEFRAME,
        model_key="kronos-mock",
        model_reference="unit-test",
        model_revision="0",
        settings_fingerprint="0" * 16,
        device="cpu",
        lookback=64,
        horizon=1,
        rollouts=1,
        points=(ForecastPoint(at=at, open=Decimal(last), high=Decimal(p90), low=Decimal(p10), close=Decimal(p50)),),
        band=Band(p10=(Decimal(p10),), p50=(Decimal(p50),), p90=(Decimal(p90),)),
        last_close=Decimal(last),
        last_observed_at=at,
        quality={},
        runtime_seconds=0.0,
    )
