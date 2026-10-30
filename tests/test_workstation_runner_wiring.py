"""The paper runner's own wiring to the workstation session.

The bug this file pins: the provider-backed runner asked the
``ObservationSession`` for ``.terminal``, which that class deliberately does not
have, so pressing Start on the Positions page produced
``AttributeError: 'ObservationSession' object has no attribute 'terminal'`` in a
worker thread and the page showed the exception text.

Two rules follow from it, and both are tested here:

* the session exposes a **narrow, read-only** data surface, and the runner asks
  for *that* — never for an attribute that would imply an order surface;
* when the session or the feed cannot provide it, the runner start fails with a
  typed blocker the page can render, and no exception text ever reaches the UI.

Everything is offline: a fake MetaTrader 5 module stands in for the terminal, and
no connection to any account is made.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from axiom_forex.brokers.mt5.terminal import MT5Terminal
from axiom_forex.config.mt5 import MT5Settings
from axiom_forex.paper_runner.feed import TerminalBarFeed
from axiom_forex.paper_runner.host import PaperRunnerHost, RunnerSetupRefused
from axiom_forex.workstation import launch
from axiom_forex.workstation.app import WorkstationApp
from axiom_forex.workstation.session import ObservationSession
from axiom_forex.workstation.setup import SetupWizard
from tests.fake_mt5 import FakeMT5
from tests.paper_provider_support import PAIR, write_setup
from tests.test_workstation_ui import offline_environment, simulated_wizard


class ReadOnlyTerminalSource:
    """A session source that carries a terminal and nothing else."""

    source_id = "mt5-terminal"
    kind = "LIVE_TERMINAL"
    label = "MetaTrader 5 terminal"

    def __init__(self, terminal: Any) -> None:
        self.terminal = terminal
        self.connect_calls = 0

    def connect(self) -> None:
        self.connect_calls += 1

    def disconnect(self) -> None:
        self.terminal = None

    def snapshot(self) -> dict[str, Any]:
        return {"connected": True, "account": {}, "symbols": []}

    def market_data(self, freshness: Any = None) -> Any:
        return "market-data-surface"


class SimulatedSource(ReadOnlyTerminalSource):
    """A connected session with no terminal behind it (the simulator preview)."""

    kind = "SIMULATED"

    def __init__(self) -> None:
        super().__init__(terminal=None)


def fake_terminal() -> MT5Terminal:
    instance = MT5Terminal(
        module=FakeMT5(),
        settings=MT5Settings(
            terminal_path="terminal64.exe",
            server="Broker-Demo",
            account_login=55500123,
            symbol_suffix=".pro",
        ),
    )
    instance.connect()
    return instance


def connected_session(terminal: Any) -> tuple[ObservationSession, ReadOnlyTerminalSource]:
    source = ReadOnlyTerminalSource(terminal)
    session = ObservationSession(source=source)
    session.connect()
    assert session.connected
    return session, source


# ---------------------------------------------------------------------------
# 1. the session's own interface
# ---------------------------------------------------------------------------


def test_the_observation_session_has_no_terminal_attribute():
    session = ObservationSession(source=ReadOnlyTerminalSource(terminal="t"))
    # The attribute the runner used to ask for does not exist, by design.
    assert not hasattr(session, "terminal")
    assert session.market_data_source() is None  # not connected yet
    assert session.view()["market_data_available"] is False
    assert session.view()["trading_capability"] == "none"


def test_a_connected_session_exposes_only_the_read_only_surface():
    terminal = fake_terminal()
    try:
        session, source = connected_session(terminal)
        assert session.market_data_source() is terminal
        # Read-only: the object handed to the runner has no order surface.
        for forbidden in ("order_send", "order_check", "buy", "sell"):
            assert not hasattr(terminal, forbidden)
        assert session.view()["market_data_available"] is True
        session.disconnect()
        assert session.market_data_source() is None
        assert source.connect_calls == 1
    finally:
        terminal.disconnect()


def test_a_simulator_session_has_no_market_data_source():
    session = ObservationSession(source=SimulatedSource())
    session.connect()
    assert session.connected
    assert session.market_data_source() is None


# ---------------------------------------------------------------------------
# 2. the launch helper turns that into a typed blocker
# ---------------------------------------------------------------------------


def test_the_runner_refuses_a_disconnected_session_with_a_typed_blocker():
    session = ObservationSession(source=ReadOnlyTerminalSource(terminal="t"))
    with pytest.raises(RunnerSetupRefused) as excinfo:
        launch.read_only_terminal(session)
    refusal = excinfo.value
    assert refusal.reason == "terminal_not_connected"
    assert "Connect" in refusal.hint
    assert "attribute" not in f"{refusal.detail} {refusal.hint}".lower()


def test_the_runner_refuses_a_simulator_session_with_a_typed_blocker():
    session = ObservationSession(source=SimulatedSource())
    session.connect()
    with pytest.raises(RunnerSetupRefused) as excinfo:
        launch.read_only_terminal(session)
    assert excinfo.value.reason == "market_data_unavailable"
    assert "terminal" in excinfo.value.hint.lower()


def test_the_platform_is_built_from_the_session_not_from_a_missing_attribute(tmp_path):
    terminal = fake_terminal()
    try:
        session, _ = connected_session(terminal)
        setup = write_setup(tmp_path)
        platform = launch._provider_platform(
            session, str(tmp_path / "provider_setup.json"), str(tmp_path / "paper.db"), PAIR
        )
        # A real, fully-built provider platform: symbol, spec, gate, ledger.
        assert platform.pair == PAIR
        assert platform.repository is not None
        feed = launch.read_only_terminal(session)
        assert feed is terminal
        bar_feed = TerminalBarFeed(
            market=session.market_data(None),
            spec=platform.spec,
            policy=__import__(
                "axiom_forex.paper_runner.feed", fromlist=["FeedPolicy"]
            ).FeedPolicy(interval_seconds=60, max_quote_age_seconds=Decimal("5")),
        )
        assert bar_feed is not None
        assert setup.terms_for(PAIR) is not None
    finally:
        terminal.disconnect()


# ---------------------------------------------------------------------------
# 3. the host reports a blocker instead of exception text
# ---------------------------------------------------------------------------


def failing_host(exc: BaseException) -> PaperRunnerHost:
    def build_platform():
        raise exc

    return PaperRunnerHost(
        build_platform=build_platform,
        build_feed=lambda platform: None,
        defer_platform=False,
    )


def test_a_build_failure_becomes_a_blocker_without_exception_text():
    host = failing_host(
        AttributeError("'ObservationSession' object has no attribute 'terminal'")
    )
    view = host.start()
    assert view["available"] is True
    assert view["blocked"] is True
    assert view["running"] is False
    assert view["reason"] == "runner_build_failed"
    rendered = repr(view) + repr(host.view())
    assert "has no attribute" not in rendered
    assert "AttributeError" not in rendered
    assert view["blockers"][0]["reason"] == "runner_build_failed"
    assert "console" in view["blockers"][0]["hint"].lower()
    host.close()


def test_a_typed_refusal_reaches_the_page_verbatim():
    host = failing_host(
        RunnerSetupRefused(
            "terminal_not_connected",
            "No terminal session is open.",
            "Press Connect, then Start.",
        )
    )
    view = host.start()
    assert view["blocked"] is True
    assert view["reason"] == "terminal_not_connected"
    assert view["blockers"] == [
        {
            "reason": "terminal_not_connected",
            "message": "No terminal session is open.",
            "hint": "Press Connect, then Start.",
        }
    ]
    host.close()


def test_starting_again_after_connecting_clears_the_blocker():
    state = {"connected": False}

    def build_platform():
        if not state["connected"]:
            raise RunnerSetupRefused("terminal_not_connected", "not connected", "Connect")
        return object()

    host = PaperRunnerHost(
        build_platform=build_platform,
        build_feed=lambda platform: object(),
        build_runner=lambda platform, feed: _NeverRunner(),
        defer_platform=True,
    )
    host.start()
    assert host.view()["pending"] is True

    blocked = host.start_runner()
    assert blocked["blocked"] is True

    state["connected"] = True
    started = host.start_runner()
    assert started.get("blocked") is not True
    assert started["state"] == "IDLE"
    host.close()


class _NeverRunner:
    """The smallest thing that behaves like a runner's lifecycle."""

    state = "IDLE"

    def restore(self) -> None:
        return None

    def start(self) -> dict[str, Any]:
        self.state = "IDLE"
        return {"available": True, "state": "IDLE", "running": False, "blockers": []}

    def stop(self, *, reason: str = "") -> dict[str, Any]:
        return {"available": True, "state": "IDLE", "running": False, "blockers": []}

    def view(self) -> dict[str, Any]:
        return {"available": True, "state": self.state, "running": False, "blockers": []}


# ---------------------------------------------------------------------------
# 4. what the pages show
# ---------------------------------------------------------------------------


class _ViewRunner:
    def __init__(self, view: dict[str, Any]) -> None:
        self._view = view
        self.starts = 0

    def view(self) -> dict[str, Any]:
        return dict(self._view)

    def start_runner(self) -> dict[str, Any]:
        self.starts += 1
        return dict(self._view)

    def stop_runner(self) -> dict[str, Any]:
        return dict(self._view)


def app_with_runner(runner: Any, tmp_path: Path | None = None, **kwargs: Any) -> WorkstationApp:
    app = WorkstationApp(
        wizard=SetupWizard(
            environment=offline_environment(), session=simulated_wizard().session
        ),
        platform=None,
        version="0.1.0",
        import_dir=tmp_path,
        research_db_path="",
        runner=runner,
        **kwargs,
    )
    return app


def test_the_positions_page_renders_the_blocked_runner_state():
    view = {
        "available": True,
        "pending": False,
        "blocked": True,
        "state": "BLOCKED",
        "running": False,
        "reason": "runner_build_failed",
        "blockers": [
            {
                "reason": "runner_build_failed",
                "message": "The runner could not be built from the terminal session.",
                "hint": "Check the console, then press Start again.",
            }
        ],
    }
    app = app_with_runner(_ViewRunner(view))
    for path in ("/positions", "/activity"):
        status, html = app.page(path)
        assert int(status) == 200
        assert "runner_build_failed" in html
        assert "Start runner" in html
        assert "has no attribute" not in html


def test_the_pages_state_every_remaining_blocker_when_connected():
    app = app_with_runner(
        _ViewRunner({"available": True, "pending": False, "running": False, "blockers": []}),
        provider_setup_path="",
        runner_source="terminal",
    )
    _, html = app.page("/positions")
    assert "terminal_not_connected" in html
    assert "provider_setup_missing" in html
    assert "no_imported_history" in html
    assert "strategy_not_evaluated" in html
    # And it names the exact place to fix each one.
    assert "Data &amp; evaluation" in html or "Data & evaluation" in html


def test_a_configured_but_missing_setup_file_is_its_own_blocker(tmp_path):
    app = app_with_runner(
        _ViewRunner({"available": True, "pending": False, "running": False, "blockers": []}),
        tmp_path,
        provider_setup_path="provider_setup.local.json",
        runner_source="terminal",
        project_root=str(tmp_path),
    )
    reasons = [item["reason"] for item in app.runner_setup_blockers({}, {"dataset_files": [], "evidence": {}})]
    assert "provider_setup_file_missing" in reasons
    _, html = app.page("/positions")
    assert "provider_setup_file_missing" in html
    assert "Provider setup" in html


def test_no_imported_dataset_and_no_evidence_are_named_separately(tmp_path):
    app = app_with_runner(
        _ViewRunner({"available": True, "pending": False, "running": False, "blockers": []}),
        tmp_path,
        provider_setup_path="provider_setup.local.json",
        runner_source="terminal",
    )
    blockers = app.runner_setup_blockers({}, app.data_view())
    reasons = [item["reason"] for item in blockers]
    assert "provider_setup_missing" not in reasons
    assert "no_imported_history" in reasons
    assert "strategy_not_evaluated" in reasons


def test_a_connected_session_with_an_approved_dataset_shows_no_setup_blocker(tmp_path):
    # The configured setup file has to exist: a missing one is its own blocker.
    (tmp_path / "provider_setup.local.json").write_text("{}", encoding="utf-8")
    app = app_with_runner(
        _ViewRunner({"available": True, "pending": False, "running": False, "blockers": []}),
        tmp_path,
        provider_setup_path="provider_setup.local.json",
        runner_source="terminal",
        project_root=str(tmp_path),
    )
    # A session that really is connected, and a data view that reports one
    # evaluable file and one selected candidate.  The blockers must disappear;
    # nothing here fabricates market data or an evaluation.
    source = ReadOnlyTerminalSource(terminal="t")
    session = ObservationSession(source=source)
    session.connect()
    app.wizard.session = session
    data = {
        "dataset_files": [{"name": "EURUSD_ticks_canonical.csv"}],
        "evidence": {"rows": [{"candidate_id": "trend_pullback_v1_8_21", "selected": True}]},
    }
    assert app.runner_setup_blockers({}, data) == []


# ---------------------------------------------------------------------------
# 5. the deferred provider build: nothing is built until Start
# ---------------------------------------------------------------------------


class _CountingHost(PaperRunnerHost):
    pass


def test_a_deferred_host_builds_nothing_until_start_is_pressed():
    builds = {"platform": 0, "feed": 0}

    def build_platform():
        builds["platform"] += 1
        return object()

    def build_feed(platform):
        builds["feed"] += 1
        return object()

    host = PaperRunnerHost(
        build_platform=build_platform,
        build_feed=build_feed,
        build_runner=lambda platform, feed: _NeverRunner(),
        defer_platform=True,
        pause_seconds=0.01,
    )
    try:
        host.start()
        assert builds == {"platform": 0, "feed": 0}
        assert host.view()["pending"] is True
        started = host.start_runner()
        assert builds == {"platform": 1, "feed": 1}
        assert started["available"] is True
        assert started["state"] == "IDLE"
    finally:
        host.close()


def test_a_deferred_build_that_fails_says_so_without_exception_text():
    def build_platform():
        raise AttributeError("'ObservationSession' object has no attribute 'terminal'")

    host = PaperRunnerHost(
        build_platform=build_platform,
        build_feed=lambda platform: object(),
        build_runner=lambda platform, feed: _NeverRunner(),
        defer_platform=True,
        pause_seconds=0.01,
    )
    try:
        host.start()
        started = host.start_runner()
        assert started["available"] is True
        assert started["blocked"] is True
        assert started["reason"] == "runner_build_failed"
        assert "has no attribute" not in repr(started)
        # The card still offers Start, so the operator can connect and retry.
        app = app_with_runner(_ViewRunner(started))
        _, html = app.page("/positions")
        assert "Start runner" in html
    finally:
        host.close()


# ---------------------------------------------------------------------------
# 6. can this runner actually start?  The production launch path, offline.
# ---------------------------------------------------------------------------


def _runner_args(tmp_path: Path, setup_path: Path, **overrides: Any):
    import argparse

    values = {
        "simulate": True,
        "paper_runner": True,
        "paper_runner_provider_setup": str(setup_path),
        "paper_runner_pair": PAIR,
        "paper_runner_database": str(tmp_path / "runner.db"),
        "paper_runner_source": "terminal",
        "paper_runner_interval": 60,
        "paper_runner_quote_age": "5",
        "paper_runner_quantity": None,
        "paper_runner_candidate": "trend_pullback_v1_8_21",
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _provider_app(session: ObservationSession) -> WorkstationApp:
    return WorkstationApp(
        wizard=SetupWizard(
            environment=offline_environment(),
            session=session,
        ),
        platform=None,
        version="0.1.0",
        allow_framing=True,
        provider_setup_path="provider_setup.json",
        runner_source="terminal",
    )


def test_the_provider_runner_builds_from_a_connected_session_and_reports_a_blocker(tmp_path):
    terminal = fake_terminal()
    try:
        session, _ = connected_session(terminal)
        setup = write_setup(tmp_path)
        app = _provider_app(session)
        host = launch._build_runner_host(
            _runner_args(tmp_path, tmp_path / "provider_setup.json"), app, tmp_path
        )
        assert host is not None, "the runner host must be buildable from the session"
        try:
            # Deferred: nothing is built until Start, so the catalogue is read
            # from the terminal as it is at that moment.
            assert host.view()["pending"] is True
            started = host.start_runner()
            # The lane really was built from the session's terminal: the runner
            # exists and answers with its own state, not a build failure.
            assert started.get("reason") != "runner_build_failed", started
            assert started.get("state") == "BLOCKED", started
            assert "has no attribute" not in repr(started)
            # No evaluation evidence exists in this database, so the only honest
            # outcome is a clear refusal -- never a trade.
            assert started.get("running") is False
            reasons = [item.get("reason") for item in started.get("blockers") or ()]
            assert reasons, started
            assert any(
                reason in {"candidate_not_selected", "strategy_not_promoted"}
                for reason in reasons
            ), reasons
        finally:
            host.close()
        assert setup.terms_for(PAIR) is not None
    finally:
        terminal.disconnect()


def test_the_runner_card_explains_a_blocked_provider_runner(tmp_path):
    terminal = fake_terminal()
    try:
        session, _ = connected_session(terminal)
        write_setup(tmp_path)
        app = _provider_app(session)
        host = launch._build_runner_host(
            _runner_args(tmp_path, tmp_path / "provider_setup.json"), app, tmp_path
        )
        assert host is not None
        try:
            started = host.start_runner()
            assert started.get("running") is False
            app.runner = _ViewRunner(started)
            status, html = app.page("/positions")
            assert int(status) == 200
            assert "has no attribute" not in html
            assert "AttributeError" not in html
            assert "candidate_not_selected" in html
            assert "Start runner" in html
        finally:
            host.close()
    finally:
        terminal.disconnect()
