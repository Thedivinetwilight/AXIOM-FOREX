"""The live path at *runner* level: one signal, one order, one booked fill.

The provider-lane suite proves the live leg's refusals one by one.  This file
proves the wiring around it:

* a runner whose mode is LIVE and whose adapter is armed sends exactly one
  ``order_send`` for a strategy signal, and books the broker's fill into the same
  ledger the paper lane uses;
* a runner with a live leg but PAPER mode catches nothing -- the leg is inert;
* a runner in LIVE mode with an *unarmed* adapter refuses, and says so;
* an unknown broker outcome stops the runner and never re-sends;
* ``run_live_session`` refuses to build at all unless the adapter is connected and
  armed, because a runner must never arm a broker.

Everything here runs against ``tests/fake_mt5.FakeMT5``.  No terminal, account, or
real order is involved, and nothing here is a claim about any market.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from axiom_forex.brokers.mt5.catalogue import build_catalogue
from axiom_forex.brokers.mt5.terminal import MT5Terminal
from axiom_forex.config.mt5 import MT5Settings
from axiom_forex.database.repository import ForexRepository
from axiom_forex.paper_runner.feed import ReplayBarFeed
from axiom_forex.paper_runner.host import lane_from_platform
from axiom_forex.paper_runner.journal import RunnerJournal
from axiom_forex.paper_runner.live import (
    LiveLeg,
    MODE_NOT_LIVE,
    UNRESOLVED_ORDER,
)
from axiom_forex.paper_runner.live_run import LiveRunConfig, LiveRunRefused, run_live_session
from axiom_forex.paper_runner.provider import build_provider_platform
from axiom_forex.paper_runner.runner import PaperStrategyRunner, RunnerConfig
from axiom_forex.execution.mode_router import ExecutionRouter
from app.universe.specs import QuantityUnit
from tests.fake_mt5 import FakeMT5
from tests.paper_provider_support import (
    BROKER_SYMBOL,
    PAIR,
    START,
    Mode,
    candidate_for,
    make_bars,
    mid_series,
    symbol_request,
    write_setup,
)
from tests.test_mt5_adapter import build_harness


@pytest.fixture
def terminal():
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
    try:
        yield instance
    finally:
        instance.disconnect()


@pytest.fixture
def setup_doc(tmp_path):
    return write_setup(tmp_path)


def build_platform(setup, terminal, *, repository=None, spec=None):
    report = build_catalogue(terminal, [symbol_request(setup, PAIR)])
    assert report.complete, [item.audit_view() for item in report.refused]
    platform, refusal = build_provider_platform(
        setup=setup,
        resolved=report.resolved_for(PAIR),
        repository=repository or ForexRepository(":memory:"),
        spec=spec,
        terminal=terminal,
    )
    assert refusal is None, refusal.audit_view()
    return platform


def make_live_leg(platform, adapter, repository, audit, mode, *, strategy_id="trend_pullback_v1_8_21"):
    router = ExecutionRouter(mode=mode, paper=None, live=adapter, audit=audit)
    return LiveLeg(
        router=router,
        adapter=adapter,
        gate=platform.gate,
        mode=mode,
        spec=platform.spec,
        strategy_id=strategy_id,
        repository=repository,
        audit_log=audit,
        portfolio=platform.portfolio,
        clock=lambda: START,
    )


def make_runner(platform, feed, *, mode, live=None, quantity=Decimal("0.10")):
    chosen = candidate_for()
    return PaperStrategyRunner(
        lane=lane_from_platform(platform),
        feed=feed,
        strategy=chosen.build(),
        candidate=chosen,
        config=RunnerConfig(
            run_id="live-runner",
            symbol=PAIR,
            quantity=quantity,
            pause_seconds=0,
            max_cycles=40,
        ),
        mode=mode,
        journal=RunnerJournal(repository=platform.repository),
        live=live,
    )


# ---------------------------------------------------------------------------
# 1. a live runner sends exactly one order and books the broker's fill
# ---------------------------------------------------------------------------


def test_a_live_runner_sends_one_order_and_books_the_fill(terminal, setup_doc):
    platform = build_platform(setup_doc, terminal)
    harness = build_harness(settings_overrides={"max_price_drift_points": 5000})
    try:
        harness.arm()
        mode = Mode("LIVE", True)
        leg = make_live_leg(platform, harness.adapter, harness.repository, harness.audit, mode)
        runner = make_runner(
            platform,
            ReplayBarFeed(bars=make_bars(mid_series(), platform.spec)),
            mode=mode,
            live=leg,
        )
        runner.restore()
        assert runner.start()["state"] == "RUNNING"
        view = runner.run(max_steps=40)

        assert view["live_configured"] is True
        assert view["trading_capability"] == "live_broker_orders"
        assert view["live_orders"] == 1, view["last_live_outcome"]
        # Exactly one order left the process, and it named the broker's symbol.
        assert len(harness.fake.send_requests) == 1
        assert harness.fake.send_requests[0]["symbol"] == BROKER_SYMBOL
        # The broker's fill is booked into the same ledger the paper lane uses.
        assert len(platform.portfolio.positions) == 1
        booked = next(iter(platform.portfolio.positions.values()))
        assert booked.account_id == platform.account.account_id
        assert booked.quantity == Decimal("0.10")
        # Nothing was simulated: the paper engine never filled anything.
        assert platform.paper_execution.fills() == ()
        assert view["counters"]["fills"] >= 1
    finally:
        harness.adapter.disconnect(reason="test")
        harness.repository.close()


def test_base_unit_catalog_fill_round_trips_through_broker_lots_into_ledger_units(
    terminal, tmp_path
):
    setup = write_setup(tmp_path, execution={"quantity": "10000"})
    report = build_catalogue(terminal, [symbol_request(setup, PAIR)])
    assert report.complete, [item.audit_view() for item in report.refused]
    resolved = report.resolved_for(PAIR)
    base_spec = replace(
        resolved.spec,
        min_quantity=Decimal(1000),
        quantity_step=Decimal(1000),
        max_quantity=Decimal(1000000),
        quantity_unit=QuantityUnit.BASE_UNITS,
    )
    platform = build_platform(
        setup,
        terminal,
        repository=ForexRepository(":memory:"),
        spec=base_spec,
    )
    harness = build_harness(settings_overrides={"max_price_drift_points": 5000})
    try:
        harness.arm()
        mode = Mode("LIVE", True)
        leg = make_live_leg(platform, harness.adapter, harness.repository, harness.audit, mode)
        runner = make_runner(
            platform,
            ReplayBarFeed(bars=make_bars(mid_series(), platform.spec)),
            mode=mode,
            live=leg,
            quantity=Decimal(10000),
        )
        runner.restore()
        assert runner.start()["state"] == "RUNNING"
        view = runner.run(max_steps=40)

        assert view["live_orders"] == 1, view["last_live_outcome"]
        assert len(harness.fake.send_requests) == 1
        assert Decimal(str(harness.fake.send_requests[0]["volume"])) == Decimal("0.1")
        booked = next(iter(platform.portfolio.positions.values()))
        assert booked.quantity == Decimal(10000)
        assert booked.base_units == Decimal(10000)
        assert Decimal(leg.booked[0]["quantity"]) == Decimal(10000)
        assert leg.booked[0]["quantity_unit"] == QuantityUnit.BASE_UNITS.value
        assert leg.booked[0]["broker_volume_lots"] == "0.1"

        invalid_fill = leg._book(
            SimpleNamespace(action="OPEN", request=SimpleNamespace(side="BUY")),
            SimpleNamespace(
                volume_filled=Decimal("0.015"),
                price=Decimal("1.1002"),
                order_ticket=7999,
            ),
        )
        assert invalid_fill["booked"] is False
        assert invalid_fill["detail"] == "invalid_quantity"
        assert "mt5:7999" not in platform.portfolio.positions

        # The close plan is expressed in catalog units, the adapter sends lots,
        # and a broker fill is translated back before the partial close is booked.
        from axiom_forex.brokers.mt5.symbols import to_broker_volume
        from axiom_forex.scalper.live_venue import BrokerPlanFactory

        close_plan = BrokerPlanFactory(
            platform=platform, strategy_id="base-unit-close-test", clock=lambda: START
        )(
            ticket="7001",
            quote=SimpleNamespace(
                source_time=START,
                received_at=START,
                bid=Decimal("1.1000"),
                ask=Decimal("1.1002"),
                tick_id="base-units-close-quote",
            ),
            quantity=Decimal(5000),
            request_id="base-units-close-request",
        )
        assert close_plan.request.quantity == Decimal(5000)
        assert close_plan.request.close_intent.quantity_unit == QuantityUnit.BASE_UNITS.value
        broker_close_volume, conversion = to_broker_volume(
            base_spec, close_plan.request.quantity, terminal.symbol_info(BROKER_SYMBOL)
        )
        assert conversion.valid and broker_close_volume == Decimal("0.05")

        close_booking = leg._book(
            close_plan,
            SimpleNamespace(
                volume_filled=broker_close_volume,
                price=Decimal("1.1000"),
                order_ticket=7002,
                deal_ticket=8002,
            ),
        )
        assert close_booking["booked"] is True, close_booking
        assert Decimal(close_booking["quantity"]) == Decimal(5000)
        assert close_booking["broker_volume_lots"] == "0.05"
        assert platform.portfolio.positions["mt5:7001"].quantity == Decimal(5000)
        assert platform.portfolio.closed_trades[-1].quantity == Decimal(5000)
    finally:
        harness.adapter.disconnect(reason="test")
        harness.repository.close()


def test_a_paper_runner_with_a_live_leg_attached_sends_nothing(terminal, setup_doc):
    platform = build_platform(setup_doc, terminal)
    harness = build_harness(settings_overrides={"max_price_drift_points": 5000})
    try:
        harness.arm()
        mode = Mode("PAPER")
        leg = make_live_leg(platform, harness.adapter, harness.repository, harness.audit, mode)
        runner = make_runner(
            platform,
            ReplayBarFeed(bars=make_bars(mid_series(), platform.spec)),
            mode=mode,
            live=leg,
        )
        runner.restore()
        runner.start()
        view = runner.run(max_steps=40)

        assert harness.fake.send_requests == []
        assert view["live_orders"] == 0
        assert leg.history == []
        # The paper lane did its normal, clearly-labelled local simulation.
        assert platform.paper_execution.fills()
        assert view["counters"]["orders"] >= 1
    finally:
        harness.adapter.disconnect(reason="test")
        harness.repository.close()


# ---------------------------------------------------------------------------
# 2. the runner refuses to start on an unarmed adapter
# ---------------------------------------------------------------------------


def test_a_live_runner_refuses_to_start_while_the_adapter_is_unarmed(terminal, setup_doc):
    platform = build_platform(setup_doc, terminal)
    harness = build_harness(settings_overrides={"max_price_drift_points": 5000})
    try:
        mode = Mode("LIVE", True)
        leg = make_live_leg(platform, harness.adapter, harness.repository, harness.audit, mode)
        runner = make_runner(
            platform,
            ReplayBarFeed(bars=make_bars(mid_series(), platform.spec)),
            mode=mode,
            live=leg,
        )
        runner.restore()
        view = runner.start()
        assert view["state"] == "BLOCKED"
        assert view["blockers"], view
        # The runner surfaces the leg's own refusal verbatim: "live_not_ready"
        # carrying the reason, so the operator sees *why* rather than a category.
        assert {item["reason"] for item in view["blockers"]} == {"live_not_ready"}
        assert "not armed" in view["reason_detail"]
        assert harness.fake.send_requests == []
        assert platform.paper_execution.fills() == ()
    finally:
        harness.adapter.disconnect(reason="test")
        harness.repository.close()


def test_a_live_runner_stops_on_an_unknown_outcome_and_never_resends(terminal, setup_doc):
    platform = build_platform(setup_doc, terminal)
    fake = FakeMT5()
    fake.send_script = [None]  # the terminal gives no answer at all
    harness = build_harness(fake=fake, settings_overrides={"max_price_drift_points": 5000})
    try:
        harness.arm()
        mode = Mode("LIVE", True)
        leg = make_live_leg(platform, harness.adapter, harness.repository, harness.audit, mode)
        runner = make_runner(
            platform,
            ReplayBarFeed(bars=make_bars(mid_series(), platform.spec)),
            mode=mode,
            live=leg,
        )
        runner.restore()
        runner.start()
        view = runner.run(max_steps=60)

        assert view["state"] == "BLOCKED"
        assert len(fake.send_requests) == 1
        assert harness.adapter.armed is False
        assert view["live_orders"] == 1
        assert view["last_live_outcome"]["unknown"] is True
        # Nothing was booked for an unknown outcome, and nothing was re-sent.
        assert platform.portfolio.positions == {}
        assert {item["reason"] for item in leg.status()["blockers"]} & {
            UNRESOLVED_ORDER,
            "live_not_armed",
        }
    finally:
        harness.adapter.disconnect(reason="test")
        harness.repository.close()


# ---------------------------------------------------------------------------
# 3. the operator entry point refuses to assemble without an armed adapter
# ---------------------------------------------------------------------------


def test_run_live_session_refuses_an_unarmed_adapter(terminal, setup_doc):
    harness = build_harness()
    repository = ForexRepository(":memory:")
    report = build_catalogue(terminal, [symbol_request(setup_doc, PAIR)])
    try:
        with pytest.raises(LiveRunRefused) as excinfo:
            run_live_session(
                adapter=harness.adapter,
                terminal=terminal,
                setup=setup_doc,
                config=LiveRunConfig(
                    pair=PAIR, candidate_id="trend_pullback_v1_8_21", quantity=Decimal("0.10")
                ),
                repository=repository,
                resolved=report.resolved_for(PAIR),
                catalogue=report,
            )
        assert excinfo.value.reason == "live_not_armed"
        assert harness.fake.send_requests == []
    finally:
        harness.adapter.disconnect(reason="test")
        harness.repository.close()
        repository.close()


def test_run_live_session_refuses_a_disconnected_adapter(terminal, setup_doc):
    harness = build_harness(connect=False)
    repository = ForexRepository(":memory:")
    report = build_catalogue(terminal, [symbol_request(setup_doc, PAIR)])
    try:
        with pytest.raises(LiveRunRefused) as excinfo:
            run_live_session(
                adapter=harness.adapter,
                terminal=terminal,
                setup=setup_doc,
                config=LiveRunConfig(
                    pair=PAIR, candidate_id="trend_pullback_v1_8_21", quantity=Decimal("0.10")
                ),
                repository=repository,
                resolved=report.resolved_for(PAIR),
                catalogue=report,
            )
        assert excinfo.value.reason == "mt5_not_connected"
        assert harness.fake.send_requests == []
    finally:
        harness.adapter.disconnect(reason="test")
        harness.repository.close()
        repository.close()


# ---------------------------------------------------------------------------
# 4. the runner's own view states the mode and the capability plainly
# ---------------------------------------------------------------------------


def test_the_runner_view_names_the_mode_and_the_capability(terminal, setup_doc):
    platform = build_platform(setup_doc, terminal)
    harness = build_harness()
    try:
        mode = Mode("PAPER")
        leg = make_live_leg(platform, harness.adapter, harness.repository, harness.audit, mode)
        runner = make_runner(
            platform, ReplayBarFeed(bars=make_bars(mid_series(), platform.spec)), mode=mode, live=leg
        )
        view = runner.view()
        assert view["execution_mode"] == "PAPER"
        assert view["live_effective"] is False
        assert view["live_configured"] is True
        assert view["order_sending"] is True
        assert view["live"]["ready"] is False
        assert MODE_NOT_LIVE in {item["reason"] for item in view["live"]["blockers"]}
        assert "password" not in repr(view).lower()
        # A paused runner is not "running", whatever its mode says.
        assert view["running"] is False
        assert view["state"] == "STOPPED"
        assert view["max_cycles"] == 40
        assert view["stops"]
    finally:
        harness.adapter.disconnect(reason="test")
        harness.repository.close()
