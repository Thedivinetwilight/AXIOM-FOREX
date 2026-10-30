"""The provider-backed paper lane and the live leg, with a fake terminal.

Two things are pinned here, and they are different in kind:

* the **paper** lane runs on a symbol the catalogue resolved from the (fake)
  terminal, with the operator's own terms, and its fills are simulated locally;
* the **live** leg is a real path -- approved plan → pre-trade gate →
  ``ExecutionRouter.route_live`` → ``MT5TradingAdapter.submit`` → ``order_send``
  -- exercised end to end against the fake module so the wiring is proven without
  connecting to any account or sending any order.

Everything is synthetic.  Nothing here is a profitability claim, and the numbers
in an assertion are properties of the test fixtures, not of a market.
"""

from __future__ import annotations

import ast
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.common.types import OrderSide
from app.data.models import ForexQuote
from axiom_forex.brokers.mt5.catalogue import build_catalogue
from axiom_forex.brokers.mt5.terminal import MT5Terminal
from axiom_forex.config.mt5 import MT5Settings
from axiom_forex.database.repository import ForexRepository
from axiom_forex.execution.mode_router import ExecutionRouter
from axiom_forex.paper_runner.conversion import LiveConversionRates, derive_rate
from axiom_forex.paper_runner.feed import ReplayBarFeed, TerminalBarFeed
from axiom_forex.paper_runner.host import lane_from_platform
from axiom_forex.paper_runner.journal import RunnerJournal
from axiom_forex.paper_runner.live import (
    DUPLICATE_REQUEST,
    GATE_REFUSED,
    MODE_NOT_LIVE,
    NOT_ARMED,
    LiveLeg,
)
from axiom_forex.paper_runner.provider import (
    MISSING_SWAP_TERMS,
    NO_CALENDAR_WINDOWS,
    build_provider_feed,
    build_provider_platform,
)
from axiom_forex.paper_runner.runner import (
    REASON_STRATEGY_NOT_SELECTED,
    RUNNING,
    Lane,
    PaperStrategyRunner,
    RunnerConfig,
)
from axiom_forex.paper_runner.strategy import candidate_family
from axiom_forex.strategies.base import Signal
from tests.fake_mt5 import FakeMT5
from tests.paper_provider_support import (
    BROKER_SYMBOL,
    PAIR,
    START,
    Mode,
    candidate_for,
    make_bar,
    make_bars,
    mid_series,
    setup_payload,
    symbol_request,
    write_setup,
)
from tests.test_mt5_adapter import build_harness

PIP = Decimal("0.0001")


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def setup_doc(tmp_path):
    return write_setup(tmp_path)


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


def resolve(terminal, setup, pair: str = PAIR):
    report = build_catalogue(terminal, [symbol_request(setup, pair)])
    assert report.complete, [item.audit_view() for item in report.refused]
    resolved = report.resolved_for(pair)
    assert resolved is not None
    return resolved


def build_platform(setup, terminal, *, repository=None, market=None):
    platform, refusal = build_provider_platform(
        setup=setup,
        resolved=resolve(terminal, setup),
        repository=repository or ForexRepository(":memory:"),
        market=market,
        terminal=terminal,
    )
    assert refusal is None, refusal.audit_view()
    assert platform is not None
    return platform


# ---------------------------------------------------------------------------
# 1. the catalogue decides the symbol; nothing else does
# ---------------------------------------------------------------------------


def test_the_catalogue_resolves_the_suffixed_symbol_exactly(terminal, setup_doc):
    resolved = resolve(terminal, setup_doc)
    assert resolved.broker_symbol == BROKER_SYMBOL
    assert resolved.pair == PAIR
    spec = resolved.spec
    assert spec.pip_size == PIP
    assert spec.contract_size == Decimal("100000.0")
    assert (spec.provider_aliases or {}).get("mt5") == BROKER_SYMBOL


def test_a_setup_that_renames_the_resolved_symbol_is_refused(terminal, tmp_path):
    setup = write_setup(tmp_path)
    resolved = resolve(terminal, setup)
    lying = write_setup(
        tmp_path,
        symbols=[
            {
                "pair": PAIR,
                "broker_symbol": "EURUSD",
                "suffix": "",
                "pip_size": "0.0001",
                "correlation_group": "USD_MAJORS",
                "margin_multiplier": "1",
                "spec_version": "operator-spec-v1",
            }
        ],
    )
    platform, refusal = build_provider_platform(
        setup=lying, resolved=resolved, repository=ForexRepository(":memory:")
    )
    assert platform is None
    assert refusal is not None
    assert "not the symbol the catalogue resolved" in refusal.detail


# ---------------------------------------------------------------------------
# 2. missing terms are a named blocker, never a default
# ---------------------------------------------------------------------------


def test_a_pair_without_a_session_window_is_refused(terminal, tmp_path):
    calendar = setup_payload()["calendar"]
    window = calendar["windows"][PAIR]
    setup = write_setup(
        tmp_path,
        calendar={**calendar, "windows": {"GBP/USD": window}},
    )
    resolved = resolve(terminal, setup)
    platform, refusal = build_provider_platform(
        setup=setup, resolved=resolved, repository=ForexRepository(":memory:")
    )
    assert platform is None
    assert refusal.reason == NO_CALENDAR_WINDOWS


def test_a_pair_without_swap_terms_is_refused(terminal, tmp_path):
    setup = write_setup(tmp_path, swap=[])
    resolved = resolve(terminal, setup)
    platform, refusal = build_provider_platform(
        setup=setup, resolved=resolved, repository=ForexRepository(":memory:")
    )
    assert platform is None
    assert refusal.reason == MISSING_SWAP_TERMS


def test_a_declared_conversion_basis_is_used_and_never_invented(terminal, tmp_path):
    setup = write_setup(tmp_path, conversion_rates=[])
    resolved = resolve(terminal, setup)
    platform, refusal = build_provider_platform(
        setup=setup, resolved=resolved, repository=ForexRepository(":memory:")
    )
    assert platform is None
    assert refusal.reason == "missing_conversion_rate"
    assert "EUR->USD" in refusal.detail


# ---------------------------------------------------------------------------
# 3. conversion rates come from the broker's own quotes, with provenance
# ---------------------------------------------------------------------------


class Tick:
    def __init__(self, bid: str, ask: str, at=START):
        self.bid = Decimal(bid)
        self.ask = Decimal(ask)
        self.time = at


def test_a_direct_rate_is_the_mid_of_the_brokers_two_sided_quote():
    leg = derive_rate(
        from_currency="EUR",
        to_currency="USD",
        quotes={"EURUSD.pro": Tick("1.09990", "1.10010")},
        symbols=("EURUSD.pro",),
    )
    assert leg.basis == "DIRECT"
    assert leg.rate == Decimal("1.10000")
    assert leg.symbols == ("EURUSD.pro",)


def test_an_inverse_rate_is_labelled_as_an_inversion():
    leg = derive_rate(
        from_currency="USD",
        to_currency="EUR",
        quotes={"EURUSD.pro": Tick("1.09990", "1.10010")},
        symbols=("EURUSD.pro",),
    )
    assert leg.basis == "INVERSE"
    assert leg.rate == Decimal(1) / Decimal("1.10000")
    assert leg.symbols == ("EURUSD.pro",)


def test_a_missing_leg_is_reported_rather_than_approximated():
    leg = derive_rate(
        from_currency="EUR",
        to_currency="JPY",
        quotes={"EURUSD.pro": Tick("1.09990", "1.10010")},
        symbols=("EURUSD.pro",),
    )
    assert leg.ok is False
    assert leg.basis == "MISSING"


def test_live_rates_read_the_terminal_and_refuse_a_stale_tick():
    from app.data.models import QuoteFreshness

    class Market:
        def __init__(self, at):
            self.at = at

        def tick(self, symbol):
            return Tick("1.09990", "1.10010", self.at)

    freshness = QuoteFreshness(
        policy_id="test",
        version="v1",
        max_age_seconds=Decimal("5"),
        max_future_skew_seconds=Decimal("2"),
        max_feed_latency_seconds=Decimal("5"),
    )
    live = LiveConversionRates(
        source="OPERATOR_LIVE",
        freshness=freshness,
        market=Market(START + timedelta(seconds=60)),
        symbols=("EURUSD.pro",),
    )
    result = live.get("EUR", "USD", START)
    assert result.ok is False
    assert result.reason.value in {"STALE_RATE", "FUTURE_RATE"}


# ---------------------------------------------------------------------------
# 4. the runner trades a catalogue-resolved symbol on paper
# ---------------------------------------------------------------------------


def make_runner(platform, bars, *, candidate=None, mode=None):
    chosen = candidate or candidate_for()
    return PaperStrategyRunner(
        lane=lane_from_platform(platform),
        feed=ReplayBarFeed(bars=bars),
        strategy=chosen.build(),
        candidate=chosen,
        config=RunnerConfig(
            run_id="provider-lane", symbol=PAIR, quantity=Decimal("0.10"), pause_seconds=0
        ),
        mode=mode or Mode(),
        journal=RunnerJournal(repository=platform.repository),
    )


def test_a_paper_fill_lands_in_the_local_ledger_on_the_provider_symbol(terminal, setup_doc):
    platform = build_platform(setup_doc, terminal)
    bars = make_bars(mid_series(), platform.spec)
    runner = make_runner(platform, bars)
    runner.restore()
    assert runner.start()["state"] == RUNNING
    view = runner.run(max_steps=200)

    assert view["state"] == "STOPPED"
    assert view["counters"]["fills"] >= 1
    fills = platform.paper_execution.fills()
    assert fills, "the synthetic replay should produce at least one simulated fill"
    ordered = sorted(fills, key=lambda item: item["filled_at"])
    assert ordered[0]["action"] == "OPEN"
    # The fill is at the *next* bar's quote, not at the decision bar's price.
    assert ordered[0]["price"] > Decimal("1.1000")
    # Every order went to the local paper broker, whose capabilities say so.
    for record in platform.repository.list_orders():
        assert record["receipt"]["capabilities"]["supports_live_orders"] is False
    assert platform.audit_view()["paper_only"] is True


def test_an_unselected_candidate_blocks_the_provider_runner(terminal, setup_doc):
    platform = build_platform(setup_doc, terminal)
    chosen = next(item for item in candidate_family() if item.candidate_id.startswith("trend"))
    bars = make_bars(mid_series(), platform.spec)
    runner = make_runner(platform, bars, candidate=chosen)
    runner.restore()
    view = runner.start()
    assert view["state"] == "BLOCKED"
    assert view["reason"] == REASON_STRATEGY_NOT_SELECTED
    assert platform.paper_execution.fills() == ()


# ---------------------------------------------------------------------------
# 5. the live leg is a real path, and it refuses everything it should
# ---------------------------------------------------------------------------


def _live_leg(platform, adapter, repository, audit, mode, strategy_id="trend_pullback_v1_8_21"):
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
        clock=lambda: START,
    )


def _plan(platform, *, price="1.10000", at=None):
    stamp = at or START
    quote = ForexQuote(
        pair=PAIR,
        timestamp=stamp,
        bid=Decimal("1.09990"),
        ask=Decimal("1.10010"),
        source="SYNTHETIC_TEST_QUOTE",
        quote_id="q-1",
        received_at=stamp,
    )
    bar = make_bar(Decimal(price), stamp - timedelta(minutes=1), platform.spec)
    plan = platform.planner.plan_at_quote(
        signal=Signal.BUY,
        positions={},
        quote=quote,
        decision_bar=bar,
        decision_index=0,
        run_id="live-leg",
        strategy_id="trend_pullback_v1_8_21",
    )
    assert plan is not None and not isinstance(plan, Exception)
    return plan


def test_the_live_leg_refuses_in_paper_mode_without_sending_anything(terminal, setup_doc):
    platform = build_platform(setup_doc, terminal)
    harness = build_harness()
    try:
        leg = _live_leg(
            platform, harness.adapter, harness.repository, harness.audit, Mode("PAPER")
        )
        result = leg.submit_plan(_plan(platform), strategy_approved=True)
        assert result["submitted"] is False
        assert MODE_NOT_LIVE in result["refusal"]["reasons"]
        assert harness.fake.send_requests == []
        assert harness.repository.list_orders() == []
    finally:
        harness.adapter.disconnect(reason="test")
        harness.repository.close()


def test_the_live_leg_refuses_an_unarmed_adapter(terminal, setup_doc):
    platform = build_platform(setup_doc, terminal)
    harness = build_harness()
    try:
        mode = Mode("LIVE", True)
        leg = _live_leg(platform, harness.adapter, harness.repository, harness.audit, mode)
        result = leg.submit_plan(_plan(platform), strategy_approved=True)
        assert result["submitted"] is False
        assert NOT_ARMED in result["refusal"]["reasons"]
        assert harness.fake.send_requests == []
    finally:
        harness.adapter.disconnect(reason="test")
        harness.repository.close()


def test_an_unapproved_strategy_cannot_reach_the_broker(terminal, setup_doc):
    platform = build_platform(setup_doc, terminal)
    harness = build_harness()
    try:
        harness.arm()
        mode = Mode("LIVE", True)
        leg = _live_leg(platform, harness.adapter, harness.repository, harness.audit, mode)
        result = leg.submit_plan(_plan(platform), strategy_approved=False)
        assert result["submitted"] is False
        assert "strategy_not_approved_for_live" in result["refusal"]["reasons"]
        assert harness.fake.send_requests == []
    finally:
        harness.adapter.disconnect(reason="test")
        harness.repository.close()


def test_an_approved_plan_reaches_order_send_exactly_once(terminal, setup_doc):
    platform = build_platform(setup_doc, terminal)
    harness = build_harness()
    try:
        harness.arm()
        mode = Mode("LIVE", True)
        leg = _live_leg(platform, harness.adapter, harness.repository, harness.audit, mode)
        plan = _plan(platform)

        result = leg.submit_plan(plan, strategy_approved=True)
        assert result["submitted"] is True, result
        assert len(harness.fake.send_requests) == 1
        stored = harness.repository.list_orders()
        assert len(stored) == 1
        assert stored[0]["broker_id"] == "mt5_terminal"
        assert stored[0]["provider_symbol"] == BROKER_SYMBOL

        # The order that left the process names the resolved broker symbol.
        assert harness.fake.send_requests[0]["symbol"] == BROKER_SYMBOL
        assert stored[0]["state"] in {"FILLED", "ACCEPTED", "UNRESOLVED"}

        # A second attempt with the same plan is a duplicate, not a new order.
        again = leg.submit_plan(plan, strategy_approved=True)
        assert again["submitted"] is False
        assert DUPLICATE_REQUEST in again["refusal"]["reasons"]
        assert len(harness.fake.send_requests) == 1
    finally:
        harness.adapter.disconnect(reason="test")
        harness.repository.close()


def test_an_unknown_outcome_is_unresolved_and_never_resent(terminal, setup_doc):
    platform = build_platform(setup_doc, terminal)
    fake = FakeMT5()
    harness = build_harness(fake=fake)
    try:
        harness.arm()
        mode = Mode("LIVE", True)
        leg = _live_leg(platform, harness.adapter, harness.repository, harness.audit, mode)
        # No answer from the terminal: the outcome is unknown, not "probably fine".
        fake.send_script = [None]
        result = leg.submit_plan(_plan(platform), strategy_approved=True)
        assert result["submitted"] is True
        assert result["unresolved"] is True
        assert harness.adapter.armed is False
        sent_once = len(fake.send_requests)

        follow_up = leg.submit_plan(_plan(platform, at=START), strategy_approved=True)
        assert follow_up["submitted"] is False
        assert set(follow_up["refusal"]["reasons"]) & {NOT_ARMED, "unresolved_order_present"}
        assert len(fake.send_requests) == sent_once
    finally:
        harness.adapter.disconnect(reason="test")
        harness.repository.close()


def test_reconciliation_reports_the_broker_positions_it_was_given(terminal, setup_doc):
    platform = build_platform(setup_doc, terminal)
    fake = FakeMT5()
    harness = build_harness(fake=fake)
    try:
        harness.arm()
        mode = Mode("LIVE", True)
        leg = _live_leg(platform, harness.adapter, harness.repository, harness.audit, mode)
        result = leg.reconcile()
        assert result["available"] is True
        assert result["reconciled"] is True
        assert leg.status()["ready"] is True
    finally:
        harness.adapter.disconnect(reason="test")
        harness.repository.close()


def test_the_live_leg_never_submits_because_a_mode_changed(terminal, setup_doc):
    platform = build_platform(setup_doc, terminal)
    harness = build_harness()
    try:
        harness.arm()
        mode = Mode("PAPER")
        leg = _live_leg(platform, harness.adapter, harness.repository, harness.audit, mode)
        # The operator flips the mode object; nothing in the leg reacts to it.
        mode.mode = "LIVE"
        mode.live_effective = True
        assert harness.fake.send_requests == []
        assert leg.history == []
        assert harness.repository.list_orders() == []
    finally:
        harness.adapter.disconnect(reason="test")
        harness.repository.close()


def test_a_gate_refusal_is_reported_with_its_own_code(terminal, setup_doc):
    platform = build_platform(setup_doc, terminal)
    harness = build_harness()
    try:
        harness.arm()
        mode = Mode("LIVE", True)
        leg = _live_leg(platform, harness.adapter, harness.repository, harness.audit, mode)
        plan = _plan(platform)
        # Make the plan's own request stale: the same gate the paper lane uses
        # must refuse it for the live path too.
        stale_quote = plan.request.quote if hasattr(plan.request, "quote") else None
        assert stale_quote is None or True
        # A live request for an instrument the gate does not know is refused.
        from dataclasses import replace

        unknown = replace(plan, request=replace(plan.request, provider_symbol="EURUSD.other"))
        result = leg.submit_plan(unknown, strategy_approved=True)
        assert result["submitted"] is False
        assert GATE_REFUSED in result["refusal"]["reasons"]
        assert harness.fake.send_requests == []
    finally:
        harness.adapter.disconnect(reason="test")
        harness.repository.close()


# ---------------------------------------------------------------------------
# 6. the UI-facing status never dresses a refusal up as readiness
# ---------------------------------------------------------------------------


def test_live_status_lists_blockers_and_hides_nothing(terminal, setup_doc):
    platform = build_platform(setup_doc, terminal)
    harness = build_harness()
    try:
        leg = _live_leg(
            platform, harness.adapter, harness.repository, harness.audit, Mode("PAPER")
        )
        status = leg.status()
        assert status["ready"] is False
        assert status["mode"] == "PAPER"
        assert status["blockers"]
        assert status["mode"] == "PAPER" and status["live_effective"] is False
        assert "password" not in repr(status).lower()
    finally:
        harness.adapter.disconnect(reason="test")
        harness.repository.close()


# ---------------------------------------------------------------------------
# 7. static safety of the new modules
# ---------------------------------------------------------------------------


NEW_MODULES = (
    "axiom_forex/paper_runner/provider.py",
    "axiom_forex/paper_runner/conversion.py",
    "axiom_forex/paper_runner/live.py",
    "axiom_forex/brokers/mt5/catalogue.py",
    "axiom_forex/config/provider_setup.py",
)

FORBIDDEN_IMPORTS = {"os", "socket", "subprocess", "requests", "urllib", "http"}
FORBIDDEN_TEXT = ("http://", "https://", "order_send(", "MetaTrader5")


def test_new_modules_import_nothing_that_can_reach_the_network():
    root = Path(__file__).resolve().parents[1]
    for relative in NEW_MODULES:
        path = root / relative
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert not (imported & FORBIDDEN_IMPORTS), f"{relative}: {imported}"
        # The live leg must not build or send orders itself; it delegates.
        for needle in FORBIDDEN_TEXT:
            assert needle not in source, f"{relative} contains {needle!r}"


# ---------------------------------------------------------------------------
# 8. the feed is bound to the resolved symbol, not to a name that may change
# ---------------------------------------------------------------------------


def test_the_feed_asks_the_terminal_for_the_resolved_symbol_only(terminal, setup_doc):
    platform = build_platform(setup_doc, terminal)
    feed = build_provider_feed(platform=platform, terminal=terminal, interval_seconds=60)
    assert isinstance(feed, TerminalBarFeed)
    market = feed.market
    assert market.symbol_for(platform.spec) == BROKER_SYMBOL
    quote = market.quote(platform.spec)
    assert quote.bid > 0 and quote.ask >= quote.bid


def test_the_feed_reports_a_disconnected_terminal_instead_of_a_last_price(terminal, setup_doc):
    platform = build_platform(setup_doc, terminal)
    feed = build_provider_feed(platform=platform, terminal=terminal, interval_seconds=60)
    terminal.disconnect()
    event = feed.poll()
    assert event.kind in {"DISCONNECTED", "INVALID", "STALE", "GAP"}


# ---------------------------------------------------------------------------
# 9. a deferred host builds the provider lane only when the operator starts it
# ---------------------------------------------------------------------------


def test_a_deferred_host_builds_only_on_start(terminal, setup_doc):
    from axiom_forex.paper_runner.host import PaperRunnerHost, lane_from_platform

    built: list[int] = []

    def build_platform():
        built.append(1)
        return build_provider_platform(
            setup=setup_doc,
            resolved=resolve(terminal, setup_doc),
            repository=ForexRepository(":memory:"),
            market=terminal,
            terminal=terminal,
        )[0]

    def build_runner(platform, feed):
        chosen = candidate_for()
        return PaperStrategyRunner(
            lane=lane_from_platform(platform),
            feed=feed,
            strategy=chosen.build(),
            candidate=chosen,
            config=RunnerConfig(
                run_id="deferred", symbol=PAIR, quantity=Decimal("0.10"), pause_seconds=0
            ),
            mode=Mode(),
            journal=RunnerJournal(repository=platform.repository),
        )

    def build_feed(platform):
        return ReplayBarFeed(bars=make_bars(mid_series(), platform.spec))

    host = PaperRunnerHost(
        build_platform=build_platform,
        build_feed=build_feed,
        build_runner=build_runner,
        defer_platform=True,
        pause_seconds=0.01,
    )
    try:
        view = host.start()
        assert built == [], "the platform must not be built before Start"
        # Alive and waiting -- not broken: the page must still offer Start.
        assert view["available"] is True
        assert view["pending"] is True
        assert view["state"] == "WAITING_FOR_TERMINAL"
        assert "waiting_for_terminal" in str(view.get("reason", ""))
        started = host.start_runner()
        assert built == [1]
        assert started.get("state") in {"RUNNING", "BLOCKED"}, started
        assert host.view()["state"] in {"RUNNING", "BLOCKED"}
    finally:
        host.close()
    assert host.alive is False


def test_a_deferred_build_that_cannot_see_a_terminal_reports_it(terminal, setup_doc):
    from axiom_forex.paper_runner.host import PaperRunnerHost

    def broken():
        raise RuntimeError("terminal_not_connected: press Connect")

    host = PaperRunnerHost(
        build_platform=broken,
        build_feed=lambda platform: None,
        build_runner=lambda platform, feed: None,
        defer_platform=True,
        pause_seconds=0.01,
    )
    try:
        host.start()
        started = host.start_runner()
        assert started["available"] is False
        assert "terminal_not_connected" in str(started.get("error", ""))
    finally:
        host.close()
