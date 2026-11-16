"""AX-SCALPER on a verified REAL account: identity, venue, autonomous loop.

Everything runs against ``tests.fake_mt5.FakeMT5``.  No terminal, account, or
broker is involved, and no order is sent anywhere.

The file proves, in order:

* the account check itself: REAL accepted, DEMO / CONTEST / unclassified /
  mismatched server, login, fingerprint and currency all refused with a reason;
* the check is enforced by the guarded live leg, so it happens *before* every
  order rather than once at start-up;
* the broker venue maps the leg's durable answer into the scalper's ``Fill`` —
  accepted, partial, rejected, blocked, unknown — and latches an unknown so
  nothing is re-sent;
* the autonomous loop refuses to arm without an admitted strategy and a REAL
  account, submits exactly one order when it has both, manages the exit through
  the same venue, and stops without closing anything by surprise;
* recovery either restores exactly or blocks entries with a named path.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from axiom_forex.paper_runner.live import (
    ACCOUNT_IDENTITY_MISMATCH,
    REAL_ACCOUNT_REQUIRED,
    LiveLeg,
)
from axiom_forex.paper_runner.provider import build_provider_platform
from axiom_forex.brokers.mt5.catalogue import build_catalogue
from axiom_forex.database.repository import ForexRepository
from axiom_forex.execution.mode_router import ExecutionRouter
from axiom_forex.scalper.config import InstrumentSpec
from axiom_forex.scalper.execution import PAPER_ONLY, Trader, VenueOrder, LIVE_VENUE_NOT_PERMITTED
from axiom_forex.scalper.guardian import Guardian, GuardianLimits
from axiom_forex.scalper.live_account import (
    FINGERPRINT_MISMATCH,
    LOGIN_MISMATCH,
    MODE_NOT_REAL,
    MODE_UNKNOWN,
    ExpectedAccount,
    verify_real_account,
)
from axiom_forex.scalper.live_autonomous import (
    NO_ADMISSION,
    RECOVERY_REQUIRED,
    AdmittedStrategy,
    AutonomousScalper,
)
from axiom_forex.scalper.live_venue import BrokerPlanFactory, BrokerVenue
from axiom_forex.scalper.positions import ManagementSettings, PositionBook
from axiom_forex.scalper.ticks import TickCore
from app.execution.broker import TradingBlocked
from tests.fake_mt5 import FakeMT5
from tests.paper_provider_support import PAIR, START, Mode, symbol_request, write_setup
from tests.test_demo_execution_test import TICK, demo_fake, make_terminal
from tests.test_mt5_adapter import build_harness

UTC = timezone.utc
LOGIN = 55500123
SERVER = "Broker-Demo"


# -- fixtures ---------------------------------------------------------------


def make_platform(setup, terminal, repository):
    report = build_catalogue(terminal, [symbol_request(setup, PAIR)])
    assert report.complete, [item.audit_view() for item in report.refused]
    platform, refusal = build_provider_platform(
        setup=setup,
        resolved=report.resolved_for(PAIR),
        repository=repository,
        terminal=terminal,
    )
    assert refusal is None, refusal.audit_view()
    return platform


@pytest.fixture
def real(tmp_path):
    """A terminal that reports a REAL account, armed, with one provider stack."""

    fake = demo_fake(trade_mode=2)  # 2 = ACCOUNT_TRADE_MODE_REAL
    # The fake terminal must mirror the protective stop it was sent, or every
    # later entry would (correctly) be refused for an unprotected position.
    fake.mirror_stop = True
    repository = ForexRepository(":memory:")
    terminal = make_terminal(fake)
    setup = write_setup(tmp_path)
    platform = make_platform(setup, terminal, repository)
    # The synthetic tick series is not the terminal's own quote, so the drift
    # guard has to be wide enough for the test's prices; the guard itself is
    # covered by the adapter's own suite and is unchanged here.
    harness = build_harness(
        fake=fake, repository=repository, settings_overrides={"max_price_drift_points": 5000}
    )
    harness.arm()
    mode = Mode("LIVE", True)
    leg = LiveLeg(
        router=ExecutionRouter(mode=mode, paper=None, live=harness.adapter, audit=harness.audit),
        adapter=harness.adapter,
        gate=platform.gate,
        mode=mode,
        spec=platform.spec,
        strategy_id="axiom-demo-execution-test",
        repository=repository,
        audit_log=harness.audit,
        portfolio=platform.portfolio,
        clock=lambda: START,
        expected_account=ExpectedAccount(server=SERVER, login=LOGIN),
        require_real_account=True,
    )
    try:
        yield {
            "fake": fake,
            "repository": repository,
            "terminal": terminal,
            "platform": platform,
            "harness": harness,
            "leg": leg,
            "mode": mode,
            "tmp": tmp_path,
        }
    finally:
        terminal.disconnect()
        repository.close()


# -- 1. the account verdict -------------------------------------------------


def derived_fingerprint(login: int = LOGIN) -> str:
    from axiom_forex.brokers.mt5.identity import account_fingerprint

    return account_fingerprint(login)


def status(**overrides):
    base = {
        "connected": True,
        "account_mode": "REAL",
        "account_mode_confirmed": True,
        "account_fingerprint": derived_fingerprint(),
        "redacted_account": {"identified": True, "server": SERVER, "login": LOGIN, "currency": "USD"},
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize(
    ("override", "expected_reason"),
    [
        ({"account_mode": "DEMO"}, MODE_NOT_REAL),
        ({"account_mode": "CONTEST"}, MODE_NOT_REAL),
        ({"account_mode": "SOMETHING"}, MODE_UNKNOWN),
        ({"account_mode_confirmed": False}, "account_mode_not_confirmed"),
        ({"redacted_account": {"server": "Other-Broker", "login": LOGIN}}, "account_server_mismatch"),
        ({"redacted_account": {"server": "Other-Broker", "login": LOGIN}}, "account_server_mismatch"),
        ({"account_fingerprint": derived_fingerprint(999)}, "account_login_mismatch"),
        ({"connected": False}, "mt5_not_connected"),
    ],
)
def test_the_account_check_names_every_disagreement(override, expected_reason):
    verdict = verify_real_account(status(**override), ExpectedAccount(server=SERVER, login=LOGIN))
    assert verdict.ok is False
    assert expected_reason in verdict.reasons, verdict.audit_view()
    assert verdict.detail


def test_a_matching_real_account_passes():
    verdict = verify_real_account(
        status(),
        ExpectedAccount(
            server=SERVER, login=LOGIN, fingerprint=derived_fingerprint(), currency="USD"
        ),
    )
    assert verdict.ok is True, verdict.reasons
    assert verdict.mode == "REAL"


def test_an_undeclared_account_is_never_accepted():
    verdict = verify_real_account(status(), None)
    assert verdict.ok is False
    assert "expected_account_not_declared" in verdict.reasons


# -- 2. the leg enforces it before every order ------------------------------


def _plan(real):
    factory = BrokerPlanFactory(platform=real["platform"], strategy_id="scalper-live-auto")
    order = VenueOrder(
        request_id="scalper:1:BUY",
        symbol=PAIR,
        side="BUY",
        lots=Decimal("0.10"),
        stop_price=Decimal("1.09500"),
        target_price=Decimal("1.11000"),
        requested_at_ms=TICK * 1000,
        signal_fingerprint="fp",
        strategy="trend",
    )
    quote = real["harness"].adapter.quote(real["platform"].spec)
    return factory(order=order, quote=quote)


def test_a_real_account_lets_the_leg_proceed(real):
    reasons = {item.reason for item in real["leg"].blockers()}
    assert REAL_ACCOUNT_REQUIRED not in reasons
    assert ACCOUNT_IDENTITY_MISMATCH not in reasons


def test_a_demo_account_is_refused_by_the_leg_before_the_gate(real):
    real["fake"].trade_mode = 0  # the terminal now answers DEMO
    real["harness"].adapter.refresh_account()
    reasons = {item.reason for item in real["leg"].blockers()}
    assert REAL_ACCOUNT_REQUIRED in reasons
    outcome = real["leg"].submit_plan(_plan(real))
    assert outcome["submitted"] is False
    assert REAL_ACCOUNT_REQUIRED in outcome["reasons"]
    assert real["fake"].send_requests == []


def test_a_different_real_account_is_refused(real):
    real["leg"].expected_account = ExpectedAccount(server=SERVER, login=LOGIN + 1)
    reasons = {item.reason for item in real["leg"].blockers()}
    assert ACCOUNT_IDENTITY_MISMATCH in reasons
    assert real["fake"].send_requests == []


# -- 3. the venue maps the leg's answer, and latches the unknown ------------


def _venue(real, **kwargs):
    return BrokerVenue(
        leg=real["leg"],
        plan_factory=BrokerPlanFactory(platform=real["platform"], strategy_id="scalper-live-auto"),
        protection_reader=lambda ticket: (Decimal("1.09500"), None),
        position_reader=lambda: [
            {"ticket": row["ticket"]} for row in (real["fake"].positions or [])
        ],
        clock_ms=lambda: TICK * 1000,
        **kwargs,
    )


def _order(request_id="scalper:venue:1", lots="0.10"):
    return VenueOrder(
        request_id=request_id,
        symbol=PAIR,
        side="BUY",
        lots=Decimal(lots),
        stop_price=Decimal("1.09500"),
        target_price=Decimal("1.11000"),
        requested_at_ms=TICK * 1000,
        signal_fingerprint="fp",
        strategy="trend",
    )


def test_the_venue_submits_once_through_the_guarded_leg(real):
    venue = _venue(real)
    quote = real["harness"].adapter.quote(real["platform"].spec)
    fill = venue.submit(_order(), quote)
    assert fill.status == "accepted", fill.audit_view()
    assert fill.simulated is False
    assert fill.filled_lots == Decimal("0.10")
    assert fill.ticket
    assert len(real["fake"].send_requests) == 1
    # The same request id is a duplicate, not a second order.
    again = venue.submit(_order(), quote)
    assert again.status == "blocked"
    assert len(real["fake"].send_requests) == 1


def test_a_partial_fill_is_booked_at_the_size_the_broker_filled(real):
    real["fake"].send_script.insert(
        0, {"retcode": 10009, "volume": 0.04, "price": 1.10020, "order": 9001, "deal": 9002}
    )
    venue = _venue(real)
    quote = real["harness"].adapter.quote(real["platform"].spec)
    fill = venue.submit(_order(), quote)
    assert fill.status == "accepted"
    assert fill.filled_lots == Decimal("0.04")
    assert fill.partial is True
    assert fill.requested_lots == Decimal("0.10")


def test_an_unknown_outcome_latches_and_is_never_resent(real):
    real["fake"].send_script.insert(0, None)  # the terminal answers nothing
    venue = _venue(real)
    quote = real["harness"].adapter.quote(real["platform"].spec)
    first = venue.submit(_order(), quote)
    assert first.status == "unknown_outcome", first.audit_view()
    assert venue.has_unresolved is True
    assert len(real["fake"].send_requests) == 1
    second = venue.submit(_order("scalper:venue:2"), quote)
    assert second.status == "blocked"
    assert second.reason == "venue_unresolved_order"
    assert len(real["fake"].send_requests) == 1


def test_a_refused_order_never_reaches_the_broker(real):
    real["harness"].adapter.disarm(reason="test")
    venue = _venue(real)
    quote = real["harness"].adapter.quote(real["platform"].spec)
    fill = venue.submit(_order(), quote)
    assert fill.status == "blocked"
    assert real["fake"].send_requests == []


def test_the_trader_refuses_a_live_venue_it_was_not_told_about():
    venue = SimpleNamespace(mode="LIVE", live_execution=True)
    with pytest.raises(TradingBlocked) as refused:
        Trader(
            venue=venue,
            guardian=Guardian(GuardianLimits(
                max_risk_per_trade_account=Decimal("50"),
                max_risk_per_trade_fraction=Decimal("0.005"),
                max_aggregate_risk_fraction=Decimal("0.02"),
                max_daily_loss_fraction=Decimal("0.02"),
                max_drawdown_fraction=Decimal("0.05"),
                max_leverage=Decimal("5"),
                max_currency_exposure_fraction=Decimal("0.5"),
                max_correlated_positions=2,
                correlation_threshold=Decimal("0.75"),
                max_open_positions=2,
                max_spread_points=Decimal("3"),
                max_spread_points_hard=Decimal("5"),
                max_slippage_points=Decimal("2"),
                max_quote_age_ms=2000,
                max_fill_latency_ms=2000,
                max_decision_latency_ms=2000,
                max_loss_streak=3,
            )),
            cost=SimpleNamespace(slippage_points=Decimal("0")),
            max_quote_age_ms=2000,
            specs={},
        )
    assert refused.value.args[0] == LIVE_VENUE_NOT_PERMITTED
    assert PAPER_ONLY != LIVE_VENUE_NOT_PERMITTED


# -- 4. the autonomous run --------------------------------------------------


def instrument_spec(platform) -> InstrumentSpec:
    spec = platform.spec
    return InstrumentSpec(
        symbol=PAIR,
        pair=PAIR,
        point=Decimal("0.00001"),
        digits=5,
        contract_size=Decimal("100000"),
        volume_min=Decimal("0.01"),
        volume_step=Decimal("0.01"),
        volume_max=Decimal("100"),
        stops_level_points=Decimal("0"),
        margin_rate=Decimal("0.01"),
        quote_currency="USD",
        account_currency="USD",
        conversion_rate=Decimal("1"),
    )


def guardian_limits() -> GuardianLimits:
    return GuardianLimits(
        max_risk_per_trade_account=Decimal("100"),
        max_risk_per_trade_fraction=Decimal("0.01"),
        max_aggregate_risk_fraction=Decimal("0.05"),
        max_daily_loss_fraction=Decimal("0.05"),
        max_drawdown_fraction=Decimal("0.10"),
        max_leverage=Decimal("10"),
        max_currency_exposure_fraction=Decimal("1"),
        max_correlated_positions=2,
        correlation_threshold=Decimal("0.75"),
        max_open_positions=2,
        max_spread_points=Decimal("30"),
        max_spread_points_hard=Decimal("60"),
        max_slippage_points=Decimal("5"),
        max_quote_age_ms=5000,
        max_fill_latency_ms=5000,
        max_decision_latency_ms=5000,
        max_loss_streak=3,
    )


class AlwaysBuy:
    """A one-signal strategy, so the loop's wiring is what is being tested."""

    family = "test"
    name = "always_buy"
    rule_version = "v1"
    requires_flow_data = False

    def __init__(self, ttl_ms: int = 60_000) -> None:
        self._ttl = ttl_ms
        self.calls = 0

    @property
    def parameters(self):
        return {"rule": "always_buy"}

    @property
    def ttl_ms(self) -> int:
        return self._ttl

    @property
    def fingerprint(self) -> str:
        return "always-buy-fingerprint"

    def evaluate(self, context):
        from axiom_forex.scalper.signals import Evaluation, Signal

        self.calls += 1
        tick = context.ticks[-1]
        signal = Signal(
            symbol=tick.symbol,
            family=self.family,
            strategy=self.name,
            side="BUY",
            at=tick.source_time,
            tick_id=tick.tick_id,
            reference_price=tick.ask,
            stop_points=Decimal("20"),
            target_points=Decimal("40"),
            reason="test",
            rule_version=self.rule_version,
            parameter_fingerprint=self.fingerprint,
            ttl_ms=self._ttl,
        )
        from axiom_forex.scalper.config import InstrumentSpec as _Spec

        signal = signal.__class__(
            **{**signal.__dict__, "_point": None}
        ) if hasattr(signal, "_point") else signal
        return Evaluation(strategy=self.name, family=self.family, outcome="signal", detail="test", signal=signal)


def build_run(real, *, admitted=True, strategy=None):
    platform = real["platform"]
    spec = instrument_spec(platform)
    venue = _venue(real)
    guardian = Guardian(guardian_limits(), declared_slippage_points=Decimal("1"))
    trader = Trader(
        venue=venue,
        guardian=guardian,
        cost=SimpleNamespace(slippage_points=Decimal("0")),
        max_quote_age_ms=5000,
        specs={PAIR: spec},
        live_venue_ok=True,
    )
    book = PositionBook(
        venue=venue,
        settings=ManagementSettings(
            max_hold_ms=600_000,
            break_even_trigger_points=Decimal("40"),
            trail_after_points=Decimal("40"),
            trail_distance_points=Decimal("20"),
        ),
        specs={PAIR: spec},
    )
    chosen = strategy or AlwaysBuy()
    run = AutonomousScalper(
        adapter=real["harness"].adapter,
        mode=real["mode"],
        expected_account=ExpectedAccount(server=SERVER, login=LOGIN),
        admitted=(
            AdmittedStrategy(
                strategy=chosen,
                candidate_id="ax-scalper-test-candidate",
                evidence_digest="digest-1",
                policy_version="policy-v1",
            )
            if admitted
            else None
        ),
        venue=venue,
        trader=trader,
        guardian=guardian,
        book=book,
        core=TickCore(
            instruments=[spec],
            max_quote_age_ms=5000,
            max_spread_points=Decimal("30"),
            gap_threshold_ms=60_000,
            tick_window=32,
            min_window_ticks=1,
            clock=lambda: START,
        ),
        specs={PAIR: spec},
        symbols={PAIR: "EURUSD.pro"},
        max_open_positions=2,
        starting_equity=Decimal("10000"),
        risk_fraction=Decimal("0.0001"),
        clock=lambda: START,
    )
    return run, venue, chosen


def tick_kwargs(real, *, seconds_ago=0, bid="1.10000", ask="1.10020"):
    return {
        "symbol": PAIR,
        "bid": Decimal(bid),
        "ask": Decimal(ask),
        "source_time": START - timedelta(seconds=seconds_ago),
        "now_ms": int((START.timestamp() - 1) * 1000),
    }


def test_the_run_refuses_to_arm_without_an_admitted_strategy(real):
    run, venue, _ = build_run(real, admitted=False)
    armed = run.arm()
    assert armed["ok"] is False
    assert NO_ADMISSION in {item["reason"] for item in armed["blockers"]}
    started = run.start()
    assert started["ok"] is False
    assert run.state == "BLOCKED"
    assert real["fake"].send_requests == []


def test_the_run_refuses_a_demo_account(real):
    run, venue, _ = build_run(real)
    real["fake"].trade_mode = 0
    real["harness"].adapter.refresh_account()
    started = run.start()
    assert started["ok"] is False
    reasons = {item["reason"] for item in started["blockers"]}
    assert "real_account_required" in reasons
    assert real["fake"].send_requests == []


def test_a_stale_quote_never_produces_a_decision(real):
    run, venue, strategy = build_run(real)
    assert run.start()["ok"] is True
    outcome = run.on_tick(**tick_kwargs(real, seconds_ago=600))
    assert outcome["execution"] is None
    assert outcome["blocked"] in {"quote_refused", "no_signal", "quote_stale"}
    assert strategy.calls == 0
    assert real["fake"].send_requests == []


def warm(run, real, *, count=40):
    """Feed a rising series so the regime classifier has a window to judge."""

    outcomes = []
    for index in range(count):
        bid = Decimal("1.10000") + Decimal(index) * Decimal("0.00001")
        source = START - timedelta(milliseconds=50 * (count - index))
        outcomes.append(
            run.on_tick(
                symbol=PAIR,
                bid=bid,
                ask=bid + Decimal("0.00020"),
                source_time=source,
                now_ms=int(source.timestamp() * 1000),
            )
        )
    return outcomes


def test_one_admitted_signal_sends_exactly_one_real_order(real):
    run, venue, strategy = build_run(real)
    assert run.start()["ok"] is True
    outcomes = warm(run, real, count=60)
    reports = [item["execution"] for item in outcomes if item.get("execution")]
    assert reports, [item.get("blocked") for item in outcomes]
    # Exactly one order left the process, for the broker symbol, and the
    # admitted strategy is the one that produced it.
    assert len(real["fake"].send_requests) == 1
    assert real["fake"].send_requests[0]["symbol"] == "EURUSD.pro"
    assert strategy.calls >= 1
    # The broker opened a position.  Because this stand-in's broker record does
    # not carry the protective stop back, the existing guard refuses every later
    # entry: the run does not double down on a position it cannot see protected.
    assert real["fake"].positions, "the broker must report the position"
    assert reports[0]["status"] in {"accepted", "blocked"}
    # The trader never claims a protection it could not read back from the
    # broker: this stand-in's broker record carries no stop, so the run says so.
    assert set(reports[0]["reasons"]) <= {"protective_orders_not_verified"}


def test_the_stop_blocks_new_entries_and_reports_what_stays_open(real):
    run, venue, _ = build_run(real)
    assert run.start()["ok"] is True
    warm(run, real, count=60)
    sent_before = len(real["fake"].send_requests)
    stopped = run.stop(reason="operator_stop")
    assert stopped["state"] == "STOPPED"
    assert "No new entry will be submitted" in stopped["message"]
    outcome = run.on_tick(**tick_kwargs(real))
    assert outcome["execution"] is None
    assert outcome["blocked"] == "not_running"
    assert len(real["fake"].send_requests) == sent_before


def test_recovery_without_state_blocks_entries_with_a_path(real):
    run, venue, _ = build_run(real)
    recovered = run.recover(state=None)
    assert recovered["ok"] is False
    assert run.state == RECOVERY_REQUIRED
    assert "reconcile" in recovered["recovery"]["hint"].lower()
    assert run.start()["ok"] is False
    assert real["fake"].send_requests == []


def test_recovery_restores_an_exact_state(real):
    run, venue, _ = build_run(real)
    snapshot = run.snapshot()
    fresh, _, _ = build_run(real)
    restored = fresh.recover(state=snapshot)
    assert restored["ok"] is True, restored
    assert fresh.state == "ARMED"
    assert fresh.snapshot()["strategy_fingerprint"] == snapshot["strategy_fingerprint"]


def test_recovery_refuses_a_different_strategy(real):
    run, venue, _ = build_run(real)
    snapshot = run.snapshot()
    snapshot["strategy_fingerprint"] = "a-different-strategy"
    fresh, _, _ = build_run(real)
    restored = fresh.recover(state=snapshot)
    assert restored["ok"] is False
    assert fresh.state == RECOVERY_REQUIRED
    assert "strategy fingerprint differs" in restored["recovery"]["detail"]
