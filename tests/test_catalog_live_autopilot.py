"""The bridge from the selected strategy to AXIOM's existing LIVE path.

Everything here is a fake: a fake selection chain, a fake live workflow and a
fake runner view.  No test connects to MetaTrader, reads an account, arms
anything or sends an order.  What is asserted is *routing and refusal*: which
prerequisite turns submission off, that the refusal is named, that the live
path's own blockers arrive unchanged, and that a paper view can never claim to
be sending.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from app.audit.log import InMemoryAuditLog
from axiom_forex.brokers.mt5 import MT5PasswordStore, MT5TradingAdapter
from axiom_forex.brokers.mt5.catalogue import build_catalogue
from axiom_forex.brokers.mt5.terminal import MT5Terminal
from axiom_forex.config.mt5 import MT5Settings
from axiom_forex.database.repository import ForexRepository
from axiom_forex.database.threaded import ThreadLocalRepository
from axiom_forex.paper_runner.live_workflow import ArmedStrategy
from axiom_forex.paper_runner.provider import build_provider_platform
from axiom_forex.strategies.catalog import live_autopilot as module
from tests.fake_mt5 import FakeMT5
from tests.paper_provider_support import (
    BROKER_SYMBOL,
    PAIR,
    Mode,
    symbol_request,
    write_setup,
)
from tests.test_live_workflow import _live_host, make_live, values as live_values
from tests.test_mt5_adapter import FakeVault, mt5_settings
from axiom_forex.strategies.catalog.live_autopilot import (
    BLOCKED,
    IDLE,
    RUNNING,
    STOPPED,
    Blocker,
    FeedState,
    LiveAutopilot,
    build_live_autopilot,
)

UTC = timezone.utc
NOW = datetime(2026, 11, 2, 9, 20, tzinfo=UTC)
PAIR = "EUR/USD"
BROKER_SYMBOL = "EURUSD.pro"
CANDIDATE = "cat-sma-cross-bracket-none"


# -- fakes -------------------------------------------------------------------


class FakeAutopilot:
    """The selection chain: one selected candidate, running as a paper test."""

    def __init__(
        self,
        *,
        state: str = RUNNING,
        selected: tuple[str, ...] = (CANDIDATE,),
        reason: str = "",
        kronos_policy: str = "OFF",
        raise_on_status: bool = False,
    ) -> None:
        self.state = state
        self.selected = selected
        self.reason = reason
        self.kronos_policy = kronos_policy
        self.raise_on_status = raise_on_status

    def status(self) -> dict[str, Any]:
        if self.raise_on_status:
            raise RuntimeError("selection_unreadable")
        return {
            "state": self.state,
            "reason": self.reason,
            "running": self.state == RUNNING,
            "selection": {"selected": len(self.selected), "evaluated_at": "2026-11-02T09:00:00+00:00"},
            "kronos": {"mode": self.kronos_policy, "vetoes": 0},
            "kronos_policy": self.kronos_policy,
            "paper": {"running": self.state == RUNNING},
        }

    def handoff(self) -> dict[str, Any]:
        proposed = [
            {"candidate_id": item, "pair": PAIR, "timeframe": "1h"} for item in self.selected
        ]
        return {"available": False, "enabled": False, "proposed": proposed, "blockers": []}


def plan(**overrides: Any) -> dict[str, Any]:
    payload = {
        "schema": "axiom-live-plan/1",
        "plan_id": "plan-1",
        "candidate_id": CANDIDATE,
        "strategy_label": "SMA cross, bracket exit",
        "pair": PAIR,
        "broker_symbol": BROKER_SYMBOL,
        "quantity": "0.10",
        "max_open_positions": 1,
        "max_daily_loss_account": "50",
        "account_currency": "USD",
        "evidence_state": "UNVALIDATED",
    }
    payload.update(overrides)
    return payload


def runner_view(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "state": "RUNNING",
        "running": True,
        "data": {"last_tick_at": "2026-11-02T09:19:57+00:00", "symbol": BROKER_SYMBOL},
        "last_signal": "BUY",
        "last_signal_at": "2026-11-02T09:19:58+00:00",
        "last_decision_at": "2026-11-02T09:19:58+00:00",
        "recent": [{"reason": "signal", "detail": "sma_cross", "signal": "BUY"}],
        "counters": {"cycles": 7},
        "live": {
            "configured": True,
            "active": True,
            "blockers": [],
            "history": [
                {
                    "submitted": True,
                    "state": "FILLED",
                    "request_id": "req-1",
                    "at": "2026-11-02T09:19:59+00:00",
                    "reasons": [],
                    "detail": "",
                    "unresolved": False,
                    "booked": True,
                }
            ],
            "last_outcome": {},
        },
        "positions": [],
    }
    payload.update(overrides)
    return payload


class FakeTrading:
    """Stand-in for ``LiveTrading``: the existing live workflow, nothing more."""

    def __init__(
        self,
        *,
        plan_doc: dict[str, Any] | None = None,
        mode: str = "LIVE",
        connected: bool = True,
        armed: bool = True,
        blockers: tuple[dict[str, str], ...] = (),
        runner: dict[str, Any] | None = None,
        session_running: bool = False,
        start_result: dict[str, Any] | None = None,
        start_raises: BaseException | None = None,
        positions: tuple[dict[str, Any], ...] = (),
    ) -> None:
        self.plan = plan_doc if plan_doc is not None else plan()
        self.mode = mode
        self.connected = connected
        self.armed = armed
        self._blockers = blockers
        self._runner = runner
        self.session_running = session_running
        self._start_result = start_result
        self._start_raises = start_raises
        self.positions = positions
        self.started = 0
        self.stopped: list[str] = []
        self.adapter: Any = None

    def view(self) -> dict[str, Any]:
        return {
            "configured": True,
            "connected": self.connected,
            "armed": self.armed,
            "mode": self.mode,
            "live_effective": self.mode == "LIVE",
            "plan": dict(self.plan),
            "blockers": [dict(item) for item in self._blockers],
            "runner": dict(self._runner or {}),
            "session_running": self.session_running,
            "next_signal_sends_real_order": bool(
                self.session_running and self.connected and self.armed and not self._blockers
            ),
            "positions": list(self.positions),
            "stopped_reason": "",
            "last_outcome": {},
        }

    def start_session(self) -> dict[str, Any]:
        self.started += 1
        if self._start_raises is not None:
            raise self._start_raises
        if self._start_result is not None:
            return dict(self._start_result)
        self.session_running = True
        self._runner = self._runner or runner_view()
        return {"started": True, "reason": "", "message": "Live session running."}

    def stop_session(self, *, reason: str = "operator_stopped") -> dict[str, Any]:
        self.stopped.append(reason)
        self.session_running = False
        return {"stopped": True, "reason": reason}


def build(**overrides: Any) -> LiveAutopilot:
    kwargs: dict[str, Any] = {
        "autopilot": FakeAutopilot(),
        "trading": FakeTrading(),
        "clock": lambda: NOW,
    }
    kwargs.update(overrides)
    return build_live_autopilot(**kwargs)


def reasons(view: Any) -> list[str]:
    return [item.reason for item in view.blockers]


# -- prerequisites: one named blocker at a time ------------------------------


def test_without_a_selection_nothing_can_start() -> None:
    view = build(autopilot=None).view()

    assert view.state == IDLE
    assert reasons(view) == [module.NO_AUTOPILOT]
    assert view.paper is False
    assert view.order_sending is False


def test_an_idle_selection_is_not_an_applied_selection() -> None:
    view = build(autopilot=FakeAutopilot(state=IDLE, selected=())).view()

    assert module.SELECTION_NOT_APPLIED in reasons(view)


def test_a_stopped_paper_test_keeps_live_disabled() -> None:
    view = build(autopilot=FakeAutopilot(state=STOPPED, reason="operator_stopped")).view()

    assert reasons(view) == [module.PAPER_NOT_RUNNING]
    assert "not running in the paper test" in view.blockers[0].message
    assert view.blockers[0].detail == "operator_stopped"


def test_a_workstation_without_the_live_wiring_says_so() -> None:
    view = build(trading=None).view()

    assert reasons(view) == [module.NO_LIVE_WORKFLOW]
    assert view.order_sending is False


def test_no_written_plan_is_a_blocker_not_a_default() -> None:
    view = build(trading=FakeTrading(plan_doc={})).view()

    assert module.PLAN_MISSING in reasons(view)


def test_a_plan_for_another_strategy_is_refused() -> None:
    other = plan(candidate_id="cat-rsi-opposite-none")
    view = build(
        autopilot=FakeAutopilot(selected=("cat-rsi-opposite-none",)),
        trading=FakeTrading(plan_doc=other),
    ).view()

    # The selected strategy and the armed plan agree here, so no mismatch...
    assert module.PLAN_NOT_SELECTED not in reasons(view)

    mismatch = build(
        autopilot=FakeAutopilot(selected=("cat-other",)),
        trading=FakeTrading(plan_doc=other),
    ).view()
    assert module.PLAN_NOT_SELECTED in reasons(mismatch)
    assert "cat-rsi-opposite-none" in mismatch.blockers[0].message


def test_the_live_paths_own_blockers_arrive_unchanged() -> None:
    trading = FakeTrading(
        blockers=(
            {"reason": "mode_not_live", "message": "The execution mode is not LIVE.", "hint": ""},
            {"reason": "account_confirmation_mismatch", "message": "Account differs.", "hint": "x"},
        )
    )
    view = build(trading=trading).view()

    assert [(item.reason, item.message) for item in view.blockers] == [
        ("mode_not_live", "The execution mode is not LIVE."),
        ("account_confirmation_mismatch", "Account differs."),
    ]
    assert view.blockers[1].detail == "x"


def test_an_unreadable_selection_is_a_blocker_not_a_crash() -> None:
    view = build(autopilot=FakeAutopilot(raise_on_status=True)).view()

    assert module.NO_AUTOPILOT not in reasons(view)  # it is wired...
    assert module.SELECTION_NOT_APPLIED in reasons(view)  # ...but says nothing


# -- start / stop ------------------------------------------------------------


def test_start_never_touches_the_live_path_when_blocked() -> None:
    trading = FakeTrading(mode="PAPER", blockers=({"reason": "mode_not_live", "message": "no"},))
    view = build(trading=trading).start()

    assert trading.started == 0
    assert view.state == BLOCKED
    assert view.reason == "mode_not_live"
    assert view.order_sending is False


def test_start_uses_the_existing_session_and_reports_sending() -> None:
    trading = FakeTrading()
    view = build(trading=trading).start()

    assert trading.started == 1
    assert view.state == RUNNING
    assert view.blockers == ()
    assert view.order_sending is True
    assert view.strategy["candidate_id"] == CANDIDATE
    assert view.strategy["broker_symbol"] == BROKER_SYMBOL
    assert view.pair == PAIR
    assert "guarded path" in view.message


def test_start_reports_the_live_paths_refusal_verbatim() -> None:
    trading = FakeTrading(
        start_result={"started": False, "reason": "live_not_armed", "message": "Arm the plan first."}
    )
    view = build(trading=trading).start()

    assert view.state == BLOCKED
    assert view.reason == "live_not_armed"
    assert view.message == "Arm the plan first."
    assert view.order_sending is False


def test_a_live_path_that_raises_becomes_a_named_blocker() -> None:
    view = build(trading=FakeTrading(start_raises=RuntimeError("no stack"))).start()

    assert view.state == BLOCKED
    assert view.reason == module.STACK_UNAVAILABLE
    assert "no stack" in view.message


def test_stop_disarms_and_leaves_positions_open() -> None:
    position = {"symbol": BROKER_SYMBOL, "volume": "0.10", "ticket": 42}
    trading = FakeTrading(positions=(position,))
    pilot = build(trading=trading)
    pilot.start()
    view = pilot.stop()

    assert trading.stopped == ["operator_stopped_autopilot"]
    assert view.state == STOPPED
    assert view.order_sending is False
    assert "stays open" in view.message
    assert view.open_positions == 1


def test_a_second_start_while_running_is_a_no_op() -> None:
    trading = FakeTrading()
    pilot = build(trading=trading)
    pilot.start()
    pilot.start()

    assert trading.started == 1


def test_a_session_stopped_by_the_live_path_stops_reading_as_running() -> None:
    trading = FakeTrading()
    pilot = build(trading=trading)
    pilot.start()
    # The host stopped itself: a disconnect, an unknown outcome, a failed booking.
    trading.session_running = False
    trading.view = lambda: {  # type: ignore[method-assign]
        "connected": False,
        "armed": False,
        "mode": "LIVE",
        "plan": plan(),
        "blockers": [],
        "runner": {"stopped_reason": "STOP_DISCONNECTED", "stopped_detail": "terminal gone"},
        "session_running": False,
        "next_signal_sends_real_order": False,
        "positions": [],
    }
    view = pilot.view()

    assert view.state == BLOCKED
    assert view.reason == "STOP_DISCONNECTED"
    assert view.message == "terminal gone"
    assert view.order_sending is False


# -- paper versus live -------------------------------------------------------


def test_paper_mode_can_never_show_order_sending() -> None:
    trading = FakeTrading(mode="PAPER", session_running=True, runner=runner_view())
    view = build(trading=trading, autopilot=FakeAutopilot()).view()

    assert view.paper is True
    assert view.order_sending is False
    assert "PAPER" in view.note
    assert "No order submission" in view.note


def test_the_live_note_names_what_is_preserved() -> None:
    trading = FakeTrading()
    view = build(trading=trading).start()

    assert "gate" in view.note and "lockout" in view.note
    assert "never claims a fill" in view.note


def test_this_module_holds_no_submission_call_of_its_own() -> None:
    source = inspect.getsource(module)

    for forbidden in ("order_send(", "submit_plan(", "route_live(", "arm_trading("):
        assert forbidden not in source


def test_kronos_is_shown_as_an_input_that_can_only_decline() -> None:
    view = build(autopilot=FakeAutopilot(kronos_policy="VETO")).view()

    assert view.kronos["available"] is True
    assert view.kronos["policy"] == "VETO"
    assert "never opens, closes or sizes a trade" in view.kronos["note"]


# -- feed freshness ----------------------------------------------------------


def test_feed_age_comes_from_the_running_feed() -> None:
    data = {"last_tick_at": (NOW - timedelta(seconds=2)).isoformat(), "symbol": BROKER_SYMBOL}
    view = build(trading=FakeTrading(runner=runner_view(data=data))).view()

    assert view.feed.source == "session_feed"
    assert view.feed.age_seconds == 2.0
    assert view.feed.fresh is True


def test_a_stale_feed_is_shown_as_stale() -> None:
    data = {
        "last_tick_at": (NOW - timedelta(seconds=30)).isoformat(),
        "symbol": BROKER_SYMBOL,
        "policy": {"max_quote_age_seconds": 5},
    }
    view = build(trading=FakeTrading(runner=runner_view(data=data))).view()

    assert view.feed.stale is True
    assert view.feed.fresh is False
    assert "30.0s old" in view.feed.detail


def test_an_unmeasurable_feed_is_not_reported_as_fresh() -> None:
    view = build(trading=FakeTrading(runner=runner_view(data={}, last_tick_at=""))).view()

    assert view.feed.fresh is None
    assert view.feed.age_seconds is None
    assert "not measured" in view.feed.detail


def test_a_supplied_feed_provider_keeps_its_own_verdict() -> None:
    def provider() -> FeedState:
        return FeedState(symbol=BROKER_SYMBOL, quoted_at="2026-11-02T09:19:59+00:00", age_seconds=1.0, stale=False, source="supplied")

    view = build(feed_provider=provider).view()

    assert view.feed.source == "supplied"
    assert view.feed.fresh is True


def test_a_feed_provider_that_fails_is_not_fresh() -> None:
    def provider() -> Any:
        raise RuntimeError("no terminal")

    view = build(feed_provider=provider).view()

    assert view.feed.fresh is None
    assert "feed unreadable" in view.feed.detail


# -- what the operator sees about orders ------------------------------------


def test_a_booked_order_reads_as_submitted() -> None:
    view = build(trading=FakeTrading()).start()

    assert view.signal.side == "long"
    assert view.signal.outcome == module.SUBMITTED
    assert view.orders[0].booked is True
    assert view.orders[0].refused is False


def test_a_refusal_keeps_its_reason_and_is_not_retried() -> None:
    refused = {
        "submitted": False,
        "state": "REFUSED",
        "request_id": "req-2",
        "at": "2026-11-02T09:19:59+00:00",
        "reasons": ["gate_refused"],
        "detail": "risk limit reached",
        "unresolved": False,
        "booked": False,
    }
    runner = runner_view(live={"configured": True, "blockers": [], "history": [refused]})
    view = build(trading=FakeTrading(runner=runner)).start()

    assert view.signal.outcome == module.REFUSED
    assert view.orders[0].refusal == "gate_refused"
    assert view.orders[0].detail == "risk limit reached"
    assert "retried" in view.note


def test_an_unresolved_order_stays_visible() -> None:
    unknown = {
        "submitted": False,
        "request_id": "req-3",
        "at": "2026-11-02T09:20:00+00:00",
        "reasons": ["live_order_outcome_unknown"],
        "detail": "",
        "unresolved": True,
        "booked": False,
    }
    runner = runner_view(live={"configured": True, "blockers": [], "history": [unknown]})
    view = build(trading=FakeTrading(runner=runner)).start()

    assert view.orders[0].unresolved is True
    assert view.signal.outcome == module.REFUSED


def test_a_signal_with_no_order_behind_it_is_held() -> None:
    runner = runner_view(live={"configured": True, "blockers": [], "history": []})
    view = build(trading=FakeTrading(runner=runner)).start()

    assert view.signal.outcome == module.HELD
    assert view.signal.side == "long"
    assert view.orders == ()


def test_measured_latency_is_reported_without_promising_a_speed() -> None:
    runner = runner_view(decision_ms=12.5, submit_ms=4.25)
    view = build(trading=FakeTrading(runner=runner)).start()

    assert view.latency.decision_ms == 12.5
    assert view.latency.submit_ms == 4.25
    assert view.latency.start_ms is not None
    assert view.latency.view_ms is not None


def test_absent_latency_stays_absent_rather_than_zero() -> None:
    view = build(trading=FakeTrading(runner=runner_view())).start()

    assert view.latency.decision_ms is None
    assert view.latency.submit_ms is None


# -- the status payload ------------------------------------------------------


def test_status_is_a_plain_payload_for_the_page() -> None:
    payload = build(trading=FakeTrading()).start().as_dict()

    for key in (
        "state",
        "mode",
        "paper",
        "order_sending",
        "armed",
        "ready",
        "strategy",
        "pair",
        "broker_symbol",
        "blockers",
        "feed",
        "signal",
        "orders",
        "positions",
        "latency",
        "kronos",
        "note",
    ):
        assert key in payload
    assert payload["strategy"]["quantity"] == "0.10"
    assert isinstance(payload["latency"]["start_ms"], float)


def test_plan_matches_selection_only_when_both_are_known() -> None:
    matched = build(trading=FakeTrading()).view()
    assert matched.plan_matches_selection is True

    unknown = build(autopilot=FakeAutopilot(selected=("cat-a", "cat-b"))).view()
    assert unknown.plan_matches_selection is None


def test_blocker_payload_carries_reason_message_and_detail() -> None:
    item = Blocker("x", "message", "detail")

    assert item.as_dict() == {"reason": "x", "message": "message", "detail": "detail"}


def test_the_quantity_in_the_plan_is_never_rewritten() -> None:
    view = build(trading=FakeTrading(plan_doc=plan(quantity="0.37"))).view()

    assert view.strategy["quantity"] == "0.37"
    assert Decimal(view.strategy["quantity"]) == Decimal("0.37")


# -- against the real live workflow, with a fake terminal ---------------------
#
# These use the same objects /live uses: a real ``LiveTrading``, a real
# ``MT5TradingAdapter`` driven by ``tests.fake_mt5.FakeMT5``, and the real
# ``arm`` -> ``start_session`` path.  The broker module is a fake, so nothing
# connects to MetaTrader; ``fake.send_requests`` is the proof that starting the
# bridge sends nothing on its own.

LIVE_CANDIDATE = "trend_pullback_v1_8_21"
PHRASE = "ENABLE LIVE TRADING"


class _LiveCase:
    def __init__(self, *, fake, terminal, adapter, controller, mode):
        self.fake = fake
        self.terminal = terminal
        self.adapter = adapter
        self.controller = controller
        self.mode = mode


@pytest.fixture
def live_case(tmp_path):
    fake = FakeMT5()
    terminal = MT5Terminal(
        module=fake,
        settings=MT5Settings(
            terminal_path="terminal64.exe",
            server="Broker-Demo",
            account_login=55500123,
            symbol_suffix=".pro",
        ),
    )
    terminal.connect()
    setup_doc = write_setup(tmp_path)
    report = build_catalogue(terminal, [symbol_request(setup_doc, PAIR)])
    assert report.complete, [item.audit_view() for item in report.refused]
    platform, refusal = build_provider_platform(
        setup=setup_doc,
        resolved=report.resolved_for(PAIR),
        repository=ForexRepository(":memory:"),
        terminal=terminal,
    )
    assert refusal is None, refusal.audit_view()
    armed_strategy = ArmedStrategy()
    settings = mt5_settings(max_price_drift_points=5000)
    adapter = MT5TradingAdapter(
        settings=settings,
        password_store=MT5PasswordStore(settings=settings, vault=FakeVault()),
        repository=ThreadLocalRepository(str(tmp_path / "live-orders.sqlite3")),
        audit_log=InMemoryAuditLog(),
        module=fake,
        terminal_factory=lambda: terminal,
        lifecycle_lookup=armed_strategy.for_strategy,
    )
    adapter.connect()
    mode = Mode("LIVE", True)
    controller = make_live(
        tmp_path,
        adapter=adapter,
        mode=mode,
        setup_path=tmp_path / "provider_setup.json",
        platform=platform,
        armed_strategy=armed_strategy,
    )
    # The workstation wires this through build_live_workflow; here it is the same
    # host the live tests build, so a session really starts when the bridge asks.
    controller.session_factory = lambda plan: _live_host(
        adapter=adapter, mode=mode, plan=plan, terminal=terminal, setup_doc=setup_doc
    )
    case = _LiveCase(fake=fake, terminal=terminal, adapter=adapter, controller=controller, mode=mode)
    try:
        yield case
    finally:
        adapter.disconnect(reason="test")
        terminal.disconnect()


def _arm(case, *, candidate: str = LIVE_CANDIDATE):
    return case.controller.arm(
        live_values(candidate_id=candidate),
        confirmation=PHRASE,
        reviewed=True,
        account_confirmation=case.controller.confirmed_account_fingerprint(),
    )


def _pilot(case, *, selected: str = LIVE_CANDIDATE):
    return LiveAutopilot(
        autopilot=FakeAutopilot(selected=(selected,)),
        trading=case.controller,
        clock=lambda: NOW,
    )


def test_the_bridge_refuses_a_paper_mode_and_starts_nothing(live_case) -> None:
    live_case.mode.mode = "PAPER"
    live_case.mode.live_effective = False
    _arm(live_case)

    view = _pilot(live_case).start()

    assert view.state == BLOCKED
    assert "live_mode_required" in reasons(view)
    assert live_case.controller.session is None
    assert live_case.fake.send_requests == []


def test_the_bridge_refuses_when_no_plan_has_been_written(live_case) -> None:
    view = _pilot(live_case).start()

    assert view.state == BLOCKED
    assert reasons(view) == [module.PLAN_MISSING]
    assert live_case.controller.session is None
    assert live_case.fake.send_requests == []


def test_the_bridge_names_an_unarmed_plan(live_case) -> None:
    _arm(live_case)
    live_case.controller.disarm(reason="test")

    view = _pilot(live_case).start()

    assert view.state == BLOCKED
    assert reasons(view) == [module.NOT_ARMED]
    assert live_case.controller.session is None
    assert live_case.fake.send_requests == []


def test_the_bridge_starts_the_real_session_and_stopping_disarms(live_case) -> None:
    result = _arm(live_case)
    assert result.armed is True, [item.audit_view() for item in result.blockers]

    pilot = _pilot(live_case)
    view = pilot.start()
    try:
        assert view.state == RUNNING
        assert view.blockers == ()
        assert live_case.controller.session is not None
        assert view.strategy["candidate_id"] == LIVE_CANDIDATE
        assert view.strategy["broker_symbol"] == BROKER_SYMBOL
        # Any order here comes from a signal the *runner* produced through the
        # guarded path -- never from the bridge, which has no order call at all.
        assert len(live_case.fake.send_requests) <= 1
    finally:
        sent_before_stop = len(live_case.fake.send_requests)
        pilot.stop()

    assert live_case.controller.armed is False
    # Stopping stops; it never closes a position and never re-sends.
    assert len(live_case.fake.send_requests) == sent_before_stop


def test_the_bridge_refuses_a_plan_armed_for_another_candidate(live_case) -> None:
    _arm(live_case)

    view = _pilot(live_case, selected="cat-sma-cross-bracket-none").start()

    assert view.state == BLOCKED
    assert module.PLAN_NOT_SELECTED in reasons(view)
    assert live_case.controller.session is None
    assert live_case.fake.send_requests == []


def test_the_bridge_refuses_an_unreadable_live_path(live_case) -> None:
    class _Broken:
        plan = {}

        def view(self):
            raise RuntimeError("live path gone")

    view = LiveAutopilot(autopilot=FakeAutopilot(), trading=_Broken(), clock=lambda: NOW).view()

    assert module.PLAN_MISSING in reasons(view)
    assert module.NO_LIVE_WORKFLOW not in reasons(view)
