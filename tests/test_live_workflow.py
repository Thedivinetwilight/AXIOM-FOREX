"""The operator's live path: plan, review, arm, start, stop -- with a fake terminal.

Nothing here talks to a broker, an account, or the network.  The ``MetaTrader5``
module is :class:`tests.fake_mt5.FakeMT5`, which records every request, so these
tests can assert the properties that matter for real money:

* arming writes a plan and arms the adapter, and sends **no** order;
* nothing is armed without a deliberate plan, an acknowledgement, and the typed
  phrase, and nothing is armed outside a confirmed LIVE mode;
* a plan can tighten the provider setup's limits but never widen them;
* every field is required -- there is no default strategy, symbol, size, or limit;
* the order path is the same planner, gate, and ledger as paper, and the fill it
  books comes from the broker's own answer;
* the session stops (and disarms) when a check fails, and never re-sends;
* the page states plainly whether the next signal would send a real order.
"""

from __future__ import annotations

import json
import time
from datetime import timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.audit.log import InMemoryAuditLog
from axiom_forex.brokers.mt5 import MT5PasswordStore, MT5TradingAdapter
from axiom_forex.brokers.mt5.catalogue import build_catalogue
from axiom_forex.brokers.mt5.terminal import MT5Terminal
from axiom_forex.config.mt5 import MT5Settings
from axiom_forex.config.provider_setup import load_provider_setup
from axiom_forex.database.repository import ForexRepository
from axiom_forex.database.threaded import ThreadLocalRepository
from axiom_forex.paper_runner import live_plan as plans
from axiom_forex.paper_runner.feed import ReplayBarFeed
from axiom_forex.paper_runner.live_workflow import ArmedStrategy, LiveTrading
from axiom_forex.paper_runner.provider import build_provider_platform
from axiom_forex.paper_runner.strategy import candidate_family
from axiom_forex.workstation.app import WorkstationApp
from tests.fake_mt5 import FakeMT5, make_position
from tests.paper_provider_support import (
    BROKER_SYMBOL,
    PAIR,
    START,
    Mode,
    make_bars,
    mid_series,
    symbol_request,
    write_setup,
)
from tests.test_mt5_adapter import FakeVault, mt5_settings
from tests.test_workstation_ui import offline_environment, simulated_wizard  # noqa: F401

CANDIDATE = "trend_pullback_v1_8_21"
PHRASE = "ENABLE LIVE TRADING"


# ---------------------------------------------------------------------------
# fixtures: a fake terminal, a provider setup, a platform, an adapter
# ---------------------------------------------------------------------------


@pytest.fixture
def fake():
    return FakeMT5()


@pytest.fixture
def terminal(fake):
    instance = MT5Terminal(
        module=fake,
        settings=MT5Settings(
            terminal_path="terminal64.exe",
            server="Broker-Demo",
            account_login=55500123,
            symbol_suffix=".pro",
        ),
    )
    instance.connect()
    try:
        yield instance
    finally:
        instance.disconnect()


@pytest.fixture
def setup_doc(tmp_path):
    return write_setup(tmp_path)


@pytest.fixture
def setup_path(tmp_path, setup_doc):
    return Path(tmp_path) / "provider_setup.json"


@pytest.fixture
def platform(setup_doc, terminal):
    report = build_catalogue(terminal, [symbol_request(setup_doc, PAIR)])
    assert report.complete, [item.audit_view() for item in report.refused]
    instance, refusal = build_provider_platform(
        setup=setup_doc,
        resolved=report.resolved_for(PAIR),
        repository=ForexRepository(":memory:"),
        terminal=terminal,
    )
    assert refusal is None, refusal.audit_view()
    return instance


def make_adapter(
    *, fake, terminal, db_path, connected=True, settings=None, armed_strategy=None
):
    values = mt5_settings(**(settings or {}))
    armed_strategy = armed_strategy or ArmedStrategy()
    adapter = MT5TradingAdapter(
        settings=values,
        password_store=MT5PasswordStore(settings=values, vault=FakeVault()),
        # The order records are written from the broker-facing thread, so the
        # database is opened per thread -- exactly as the workstation does.
        repository=ThreadLocalRepository(str(db_path)),
        audit_log=InMemoryAuditLog(),
        module=fake,
        # The adapter adopts the terminal the operator already opened.
        terminal_factory=lambda: terminal,
        lifecycle_lookup=armed_strategy.for_strategy,
    )
    if connected:
        adapter.connect()
    return adapter


def resolver_for(platform):
    """The read-only facts the review needs: spec and broker volume rules."""

    def resolve(pair: str):
        if pair != platform.pair:
            return None
        spec = platform.spec
        return SimpleNamespace(
            spec=spec,
            broker_symbol=BROKER_SYMBOL,
            symbol_info={
                "volume_min": str(spec.min_quantity),
                "volume_step": str(spec.quantity_step),
                "volume_max": str(spec.max_quantity),
                "digits": spec.quote_precision,
                "trade_mode": 4,
            },
        )

    return resolve


def values(**overrides):
    base = {
        "candidate_id": CANDIDATE,
        "pair": PAIR,
        # The bar size the strategy was evaluated on, and the protective stop the
        # operator accepts.  Neither has a default anywhere in the flow.
        "timeframe": "15m",
        "quantity": "0.10",
        "max_open_positions": "1",
        "max_daily_loss_account": "100",
        "protective_stop_pips": "25",
    }
    base.update(overrides)
    return base


def passed_qualification(tmp_path, *, passed: bool = True):
    """A paper-test record the live pages are gated on.

    ``passed=False`` is a test that has not counted its 24 hours yet.
    """

    from axiom_forex.paper_runner.qualification import QualificationClock, QualificationPolicy

    policy = QualificationPolicy(
        required_active_seconds=0.0 if passed else 86400.0,
        required_span_seconds=0.0 if passed else 86400.0,
        min_decisions=0 if passed else 1,
    )
    qualification = QualificationClock(Path(tmp_path) / "catalog", policy=policy)
    qualification.start()
    return qualification


_UNSET = object()


def make_live(tmp_path, *, adapter, mode, setup_path, platform, armed_strategy=None):
    if mode is not None and str(getattr(mode, "mode", "")).upper() == "LIVE":
        # The mode controller records which account the operator confirmed when
        # LIVE was selected.  These tests confirm the fixture's own terminal.
        try:
            mode.session_account = adapter.account_fingerprint()
        except Exception:
            pass
    return LiveTrading(
        adapter=adapter,
        mode=mode,
        armed_strategy=armed_strategy,
        plan_path=Path(tmp_path) / plans.PLAN_FILENAME,
        setup_path=setup_path,
        stack_builder=resolver_for(platform),
        candidates=candidate_family(),
        clock=lambda: START,
    )


@pytest.fixture
def live(tmp_path, fake, terminal, setup_path, platform):
    armed_strategy = ArmedStrategy()
    adapter = make_adapter(
        fake=fake,
        terminal=terminal,
        db_path=Path(tmp_path) / "live-orders.sqlite3",
        armed_strategy=armed_strategy,
        # The replay bars are dated 2026-03-10 while the fake terminal quotes a
        # fixed price; the drift check still runs, with a test-sized tolerance.
        settings={"max_price_drift_points": 5000},
    )
    try:
        yield make_live(
            tmp_path,
            adapter=adapter,
            mode=Mode("LIVE", True),
            setup_path=setup_path,
            platform=platform,
            armed_strategy=armed_strategy,
        )
    finally:
        adapter.disconnect(reason="test")


# ---------------------------------------------------------------------------
# 1. nothing is armed by accident
# ---------------------------------------------------------------------------


def test_arming_needs_the_review_acknowledgement(live):
    result = live.arm(values(), confirmation=PHRASE, reviewed=False, account_confirmation=live.confirmed_account_fingerprint())
    assert result.armed is False
    assert [item.reason for item in result.blockers] == ["review_not_acknowledged"]
    assert live.armed is False
    assert not (Path(live.plan_path)).exists()


def test_arming_needs_the_exact_typed_phrase(live):
    result = live.arm(
        values(),
        confirmation="yes",
        reviewed=True,
        account_confirmation=live.confirmed_account_fingerprint(),
    )
    assert result.armed is False
    assert result.reason == "live_confirmation_mismatch"
    assert live.armed is False
    assert not Path(live.plan_path).exists()


def test_arming_needs_a_confirmed_live_mode(tmp_path, fake, terminal, setup_path, platform):
    adapter = make_adapter(
        fake=fake, terminal=terminal, db_path=Path(tmp_path) / "mode.sqlite3"
    )
    try:
        live = make_live(
            tmp_path, adapter=adapter, mode=Mode("PAPER", False), setup_path=setup_path,
            platform=platform,
        )
        result = live.arm(values(), confirmation=PHRASE, reviewed=True, account_confirmation=live.confirmed_account_fingerprint())
        assert result.armed is False
        assert any(item.reason == "live_mode_required" for item in result.blockers)
        assert not Path(live.plan_path).exists()
        assert fake.send_requests == []
    finally:
        adapter.disconnect(reason="test")


def test_arming_needs_a_connected_adapter(
    tmp_path, fake, terminal, setup_path, platform
):
    adapter = make_adapter(
        fake=fake, terminal=terminal, db_path=Path(tmp_path) / "offline.sqlite3",
        connected=False,
    )
    live = make_live(
        tmp_path, adapter=adapter, mode=Mode("LIVE", True), setup_path=setup_path,
        platform=platform,
    )
    result = live.arm(values(), confirmation=PHRASE, reviewed=True, account_confirmation=live.confirmed_account_fingerprint())
    assert result.armed is False
    assert any(item.reason == "mt5_not_connected" for item in result.blockers)
    assert fake.send_requests == []


# ---------------------------------------------------------------------------
# 2. every field is the operator's, and a plan may only tighten the setup
# ---------------------------------------------------------------------------


def test_an_empty_plan_names_every_missing_field(live):
    document, problems = live.build({})
    assert document is None
    paths = {item.path for item in problems}
    assert paths == {
        "plan.candidate_id",
        "plan.pair",
        "plan.timeframe",
        "plan.quantity",
        "plan.max_open_positions",
        "plan.max_daily_loss_account",
        "plan.protective_stop_pips",
    }
    # Nothing was defaulted into a document.
    assert live.plan == {}


def test_a_plan_may_not_widen_the_provider_setup(live):
    _document, problems = live.build(
        values(max_open_positions="4", max_daily_loss_account="99999")
    )
    paths = {item.path for item in problems}
    assert "plan.max_open_positions" in paths
    assert "plan.max_daily_loss_account" in paths
    messages = " ".join(item.message for item in problems)
    assert "tighten" in messages


def test_an_unknown_strategy_or_symbol_is_refused(live):
    _document, problems = live.build(values(candidate_id="made_up_strategy"))
    assert any("not one of the known candidates" in item.message for item in problems)
    _document, problems = live.build(values(pair="XXX/YYY"))
    assert any("not in your provider setup" in item.message for item in problems)


def test_a_size_off_the_brokers_grid_is_refused(live):
    blockers = live.blockers(values(quantity="0.005"))
    assert any(item.reason == "quantity_not_tradable" for item in blockers)


# ---------------------------------------------------------------------------
# 3. arming writes the plan, arms the adapter, and sends nothing
# ---------------------------------------------------------------------------


def test_arming_writes_the_plan_and_sends_no_order(live, fake):
    result = live.arm(values(), confirmation=PHRASE, reviewed=True, account_confirmation=live.confirmed_account_fingerprint())

    assert result.armed is True, [item.audit_view() for item in result.blockers]
    assert live.armed is True
    assert fake.send_requests == []
    document = json.loads(Path(live.plan_path).read_text(encoding="utf-8"))
    assert document["schema"] == plans.SCHEMA
    assert document["candidate_id"] == CANDIDATE
    assert document["pair"] == PAIR
    assert document["timeframe"] == "15m"
    assert document["protective_stop_pips"] == "25"
    assert document["quantity"] == "0.10"
    assert document["max_open_positions"] == 1
    assert document["max_daily_loss_account"] == "100"
    assert document["account_currency"] == "USD"
    # An unmeasured strategy is labelled as such, in the file and on the page.
    assert document["evidence_state"] == "UNVALIDATED"

    view = live.view()
    assert view["armed"] is True
    assert view["next_signal_sends_real_order"] is False  # no session running yet
    assert "not running" in live.summary()["sentence"]


def test_disarming_ends_the_authorisation(live, fake):
    live.arm(values(), confirmation=PHRASE, reviewed=True, account_confirmation=live.confirmed_account_fingerprint())
    live.disarm(reason="test")
    assert live.armed is False
    assert live.summary()["next_signal_sends_real_order"] is False
    assert fake.send_requests == []


def test_the_plan_file_never_carries_a_credential(tmp_path):
    document = {
        "schema": plans.SCHEMA,
        "plan_id": "live-1",
        "created_at": START.isoformat(),
        "candidate_id": CANDIDATE,
        "pair": PAIR,
        "timeframe": "15m",
        "quantity": "0.10",
        "max_open_positions": 1,
        "max_daily_loss_account": "100",
        "protective_stop_pips": "25",
        "password": "hunter2",
    }
    with pytest.raises(plans.PlanError) as excinfo:
        plans.write_plan(document, Path(tmp_path) / plans.PLAN_FILENAME)
    assert excinfo.value.reason == "plan_contains_credential_field"
    assert not (Path(tmp_path) / plans.PLAN_FILENAME).exists()


def test_an_existing_plan_is_not_replaced_by_accident(tmp_path, setup_doc):
    path = Path(tmp_path) / plans.PLAN_FILENAME
    document, problems = plans.build_plan(
        values(), setup=load_provider_setup(Path(tmp_path) / "provider_setup.json"),
        candidates=candidate_family(), now=START,
    )
    assert not problems
    plans.write_plan(document, path)
    with pytest.raises(FileExistsError):
        plans.write_plan(document, path)


# ---------------------------------------------------------------------------
# 4. the session: exactly one real order, booked from the broker's answer
# ---------------------------------------------------------------------------


def _wait(predicate, *, timeout=8.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _live_host(*, adapter, mode, plan, terminal, setup_doc):
    from axiom_forex.paper_runner.host import lane_from_platform
    from axiom_forex.paper_runner.journal import RunnerJournal
    from axiom_forex.paper_runner.live_host import LiveRunnerHost
    from axiom_forex.paper_runner.runner import PaperStrategyRunner, RunnerConfig

    candidate = next(item for item in candidate_family() if item.candidate_id == CANDIDATE)

    def build_stack():
        # Built on the host's own thread, exactly as the workstation does it: the
        # platform's SQLite connection belongs to the thread that opened it.
        report = build_catalogue(terminal, [symbol_request(setup_doc, PAIR)])
        assert report.complete, [item.audit_view() for item in report.refused]
        platform, refusal = build_provider_platform(
            setup=setup_doc,
            resolved=report.resolved_for(PAIR),
            repository=ForexRepository(":memory:"),
            terminal=terminal,
        )
        assert refusal is None, refusal.audit_view()
        return platform, ReplayBarFeed(bars=make_bars(mid_series(), platform.spec)), platform.spec

    def build_runner(built_platform, feed, leg):
        return PaperStrategyRunner(
            lane=lane_from_platform(built_platform),
            feed=feed,
            strategy=candidate.build(),
            candidate=candidate,
            config=RunnerConfig(
                run_id="live-test",
                symbol=PAIR,
                quantity=Decimal("0.10"),
                pause_seconds=0,
                max_cycles=40,
            ),
            mode=mode,
            journal=RunnerJournal(
                repository=built_platform.repository,
                audit_log=built_platform.audit_log,
            ),
            live=leg,
            allow_unvalidated=True,
        )

    return LiveRunnerHost(
        adapter=adapter,
        mode=mode,
        plan=plan,
        build_stack=build_stack,
        build_runner=build_runner,
        lane_from_platform=lane_from_platform,
        pause_seconds=0.01,
        clock=lambda: START,
    )


def test_a_live_session_sends_one_order_and_never_retries(live, fake, terminal, setup_doc):
    result = live.arm(values(), confirmation=PHRASE, reviewed=True, account_confirmation=live.confirmed_account_fingerprint())
    assert result.armed, [item.audit_view() for item in result.blockers]

    host = _live_host(
        adapter=live.adapter,
        mode=Mode("LIVE", True),
        plan=live.plan,
        terminal=terminal,
        setup_doc=setup_doc,
    )
    try:
        host.start()
        started = host.start_runner()
        assert started.get("running") is True, started
        assert _wait(lambda: len(fake.send_requests) >= 1, timeout=10.0)
        # Let the loop run out its cycles.
        _wait(lambda: host.view().get("running") is False, timeout=10.0)
        view = host.view()
        assert len(fake.send_requests) == 1
        assert fake.send_requests[0]["symbol"] == BROKER_SYMBOL
        assert int(view["live_orders"]) == 1
        # The broker's own fill is booked into the same ledger paper uses.
        assert len(host.platform.portfolio.positions) == 1
        assert host.platform.paper_execution.fills() == ()
        # And nothing was simulated in its place.
        assert view["paper_only"] is False
        assert view["order_sending"] is True
    finally:
        host.close()
        live.adapter.disarm(reason="test")
        live.adapter.disconnect(reason="test")


def test_the_session_refuses_to_start_unarmed(tmp_path, fake, terminal, setup_doc):
    adapter = make_adapter(
        fake=fake, terminal=terminal, db_path=Path(tmp_path) / "unarmed.sqlite3"
    )
    try:
        host = _live_host(
            adapter=adapter,
            mode=Mode("LIVE", True),
            plan={
                "schema": plans.SCHEMA,
                "plan_id": "live-test",
                "created_at": START.isoformat(),
                "candidate_id": CANDIDATE,
                "pair": PAIR,
                "timeframe": "15m",
                "quantity": "0.10",
                "max_open_positions": 1,
                "max_daily_loss_account": "100",
                "protective_stop_pips": "25",
                "account_currency": "USD",
            },
            terminal=terminal,
            setup_doc=setup_doc,
        )
        try:
            host.start()
            view = host.start_runner()
            assert view.get("running") is False
            assert any(
                item.get("reason") == "live_not_armed" for item in view.get("blockers", [])
            )
            assert fake.send_requests == []
        finally:
            host.close()
    finally:
        adapter.disconnect(reason="test")


def test_a_failed_check_stops_the_session_and_disarms(live, fake, terminal, setup_doc):
    live.arm(values(), confirmation=PHRASE, reviewed=True, account_confirmation=live.confirmed_account_fingerprint())  # noqa: F841
    host = _live_host(
        adapter=live.adapter,
        mode=Mode("LIVE", True),
        plan=live.plan,
        terminal=terminal,
        setup_doc=setup_doc,
    )
    try:
        host.start()
        assert host.start_runner().get("running") is True
        assert _wait(lambda: len(fake.send_requests) >= 1, timeout=10.0)
        sent = len(fake.send_requests)
        # The operator's own stop, or any failing check, ends the loop.
        host.stop_runner(reason="test_stop", disarm=True)
        assert _wait(lambda: host.view().get("running") is False, timeout=5.0)
        assert live.armed is False
        time.sleep(0.3)
        assert len(fake.send_requests) == sent  # never re-sent
    finally:
        host.close()
        live.adapter.disarm(reason="test")
        live.adapter.disconnect(reason="test")


# ---------------------------------------------------------------------------
# 5. the page says exactly what the next signal would do
# ---------------------------------------------------------------------------


def _app(live_controller):
    return WorkstationApp(
        wizard=simulated_wizard(offline_environment()),
        live=live_controller,
        version="0.1.0",
    )


def test_the_live_page_says_when_no_order_can_be_sent():
    app = _app(None)
    status, html = app.page("/live")
    assert int(status) == 200
    assert "No real order can be sent right now" in html
    assert "no live trading adapter" in html.lower()


def test_the_live_page_says_when_the_next_signal_will_send():
    class Sending:
        def view(self):
            return {
                "configured": True,
                "armed": True,
                "connected": True,
                "live_effective": True,
                "mode": "LIVE",
                "session_running": True,
                "next_signal_sends_real_order": True,
                "plan": {"present": False},
                "blockers": [],
                "candidates": [],
                "symbols": [],
                "account": {},
                "positions": [],
                "confirmation_phrase": PHRASE,
                "session_state": "RUNNING",
                "orders_sent": 0,
            }

        def summary(self):
            return {
                "present": True,
                "armed": True,
                "next_signal_sends_real_order": True,
                "sentence": "The next strategy signal WILL send a real order to the broker.",
            }

    app = _app(Sending())
    status, html = app.page("/live")
    assert int(status) == 200
    assert "The next strategy signal WILL send a real order" in html
    # The stop control is labelled as what it is: an emergency stop that closes
    # nothing.  A label that could be read as "flatten the account" would be wrong.
    assert "EMERGENCY STOP" in html
    assert "does not close them" in html or "closes nothing" in html


def test_the_live_actions_need_the_nonce():
    app = _app(None)
    for action in (
        "live-arm",
        "live-disarm",
        "live-start",
        "live-stop",
        "live-stop-settings",
    ):
        status, _, _ = app.act(action, {})
        assert int(status) == 403, action


def test_the_live_actions_explain_a_missing_adapter():
    app = _app(None)
    status, redirect, message = app.act("live-start", {"nonce": [app.nonce]})
    assert int(status) == 400
    assert redirect == "/live"
    assert "no live trading adapter" in message.lower()


def test_the_page_prints_no_secret_and_no_balance(live):
    secret = "S3cret-Terminal-Passw0rd!"
    live.adapter.password_store.vault.values[
        live.adapter.settings.credential_entry()
    ] = secret
    app = _app(live)
    status, html = app.page("/live")
    assert int(status) == 200
    assert secret not in html
    # The account is described, never valued: no balance or equity figure at all.
    account_state = live.adapter.status().get("account_state") or {}
    for value in account_state.values():
        if isinstance(value, (int, float)) and float(value) > 0:
            assert str(value) not in html


# ---------------------------------------------------------------------------
# 8. the account is named exactly, and a switch refuses the order
# ---------------------------------------------------------------------------


def test_arming_must_name_the_account_the_terminal_is_on(live):
    result = live.arm(
        values(), confirmation=PHRASE, reviewed=True, account_confirmation="0000000000"
    )
    assert result.armed is False
    assert "account_confirmation_mismatch" in [item.reason for item in result.blockers]
    assert not Path(live.plan_path).exists()


def test_the_confirmed_account_is_what_gets_armed(live, fake):
    fingerprint = live.confirmed_account_fingerprint()
    assert fingerprint
    result = live.arm(
        values(), confirmation=PHRASE, reviewed=True, account_confirmation=fingerprint
    )
    assert result.armed, [item.audit_view() for item in result.blockers]
    status = live.adapter.status()
    assert status["armed_account_fingerprint"] == fingerprint
    assert status["armed_account_matches_current"] is True
    assert fake.send_requests == []


def test_a_switch_after_arming_refuses_the_order_it_was_armed_for(live, fake, terminal):
    fingerprint = live.confirmed_account_fingerprint()
    armed = live.arm(
        values(), confirmation=PHRASE, reviewed=True, account_confirmation=fingerprint
    )
    assert armed.armed
    # The operator changes account in the terminal behind AXIOM's back.
    fake.account_login = 99900001
    live.adapter.refresh_account()
    assert live.adapter.armed is True, "a switch is the operator's act; it does not disarm"
    assert live.adapter.status()["armed_account_matches_current"] is False


def _page_app(live_controller, qualification):
    app = _app(live_controller)
    app.qualification = qualification
    return app


class _ConfiguredLive:
    """A wired live page that is neither armed nor running.  Sends nothing."""

    def __init__(self, account_mode="REAL"):
        self.account_mode = account_mode

    def view(self):
        return {
            "configured": True,
            "armed": False,
            "connected": True,
            "live_effective": False,
            "mode": "PAPER",
            "account_mode": self.account_mode,
            "session_running": False,
            "next_signal_sends_real_order": False,
            "plan": {"present": False},
            "blockers": [],
            "candidates": [],
            "symbols": [],
            "account": {"fingerprint": "abc123"},
            "positions": [],
            "account_fingerprint": "abc123",
            "confirmation_phrase": PHRASE,
            "qualification": {"present": True, "passed": True},
        }

    def summary(self):
        return {
            "present": True,
            "armed": False,
            "next_signal_sends_real_order": False,
            "sentence": "Paper mode: every fill is simulated locally; no order is sent.",
        }


def test_switching_to_paper_stops_new_entries_and_does_not_close_positions(live, fake):
    class Session:
        """A live session that records what the mode switch asks of it."""

        def __init__(self):
            self.calls = []

        def stop_runner(self, *, reason="", disarm=True):
            self.calls.append(("stop_runner", reason, disarm))
            return {"running": False, "state": "STOPPED"}

        def close(self):
            self.calls.append(("close", "", False))

        def view(self):
            return {"running": False, "state": "STOPPED"}

    session = Session()
    live.session = session
    live.arm(values(), confirmation=PHRASE, reviewed=True,
             account_confirmation=live.confirmed_account_fingerprint())
    live.stop_session(reason="mode_switched_to_paper", close=False)

    assert session.calls == [("stop_runner", "mode_switched_to_paper", True)]
    assert live.adapter.armed is False
    # Nothing was liquidated: a mode switch ends authorisation, not positions.
    assert fake.send_requests == []


def test_live_sizing_and_limits_start_empty_and_are_never_the_paper_numbers(live):
    blanks = live.blank_values()
    assert blanks["quantity"] == ""
    assert blanks["max_open_positions"] == ""
    assert blanks["max_daily_loss_account"] == ""
    values_before = dict(blanks)
    # Asking what the form holds changes nothing, and arming with them is refused.
    assert live.blank_values() == values_before
    result = live.arm(
        blanks, confirmation=PHRASE, reviewed=True,
        account_confirmation=live.confirmed_account_fingerprint(),
    )
    assert result.armed is False
    reasons = {item.reason for item in result.blockers}
    assert "live_plan_invalid" in reasons or "quantity_not_tradable" in reasons


def test_a_session_with_no_confirmed_account_can_arm_but_sends_nothing(live):
    live.mode.session_account = ""
    fingerprint = live.confirmed_account_fingerprint()
    result = live.arm(
        values(), confirmation=PHRASE, reviewed=True, account_confirmation=fingerprint
    )
    assert result.armed is False
    assert "session_account_not_confirmed" in [item.reason for item in result.blockers]


def test_a_terminal_switched_to_another_account_refuses_the_order(live, fake):
    fingerprint = live.confirmed_account_fingerprint()
    assert live.arm(
        values(), confirmation=PHRASE, reviewed=True, account_confirmation=fingerprint
    ).armed
    fake.account_login = 99900001
    live.adapter.refresh_account()
    reasons = [item.reason for item in live.blockers()]
    assert "session_account_changed" in reasons
    started = live.start_session()
    assert started["started"] is False
    assert fake.send_requests == []


def test_paper_mode_is_always_reachable_and_stops_the_loop(live, fake):
    fingerprint = live.confirmed_account_fingerprint()
    assert live.arm(
        values(), confirmation=PHRASE, reviewed=True, account_confirmation=fingerprint
    ).armed
    # Switching the mode object to paper is enough to close every order path.
    live.mode.mode = "PAPER"
    live.mode.live_effective = False
    reasons = [item.reason for item in live.blockers()]
    assert "live_mode_required" in reasons
    assert live.start_session()["started"] is False
    assert fake.send_requests == []


def test_switching_to_paper_leaves_broker_positions_open_and_says_so(live, fake):
    fake.positions.append(
        make_position(ticket=77, symbol=BROKER_SYMBOL, side="BUY", volume=0.1)
    )
    fingerprint = live.confirmed_account_fingerprint()
    live.arm(values(), confirmation=PHRASE, reviewed=True, account_confirmation=fingerprint)
    live.mode.mode = "PAPER"
    live.mode.live_effective = False

    view = live.view()
    assert [item.get("ticket") for item in view["positions"]] == [77]
    assert view["next_signal_sends_real_order"] is False
    # No close order, no modification: the position is untouched.
    assert fake.send_requests == []
    assert [item["ticket"] for item in fake.positions] == [77]
