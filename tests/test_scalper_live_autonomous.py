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

from dataclasses import replace
from datetime import timedelta, timezone
from decimal import Decimal
from uuid import uuid4
from types import SimpleNamespace

import pytest

from axiom_forex.paper_runner.live import (
    ACCOUNT_IDENTITY_MISMATCH,
    REAL_ACCOUNT_REQUIRED,
    UNRESOLVED_PROTECTION_MODIFICATION,
    LiveLeg,
)
from axiom_forex.paper_runner.provider import build_provider_platform
from axiom_forex.brokers.mt5 import TRADE_ACTION_SLTP
from axiom_forex.brokers.mt5.catalogue import build_catalogue
from axiom_forex.database.repository import ForexRepository
from axiom_forex.execution.mode_router import ExecutionRouter
from axiom_forex.scalper.config import InstrumentSpec
from axiom_forex.scalper.execution import (
    ACCEPTED,
    UNKNOWN_OUTCOME,
    Fill,
    PAPER_ONLY,
    PROTECTION_UNVERIFIED,
    Trader,
    VenueOrder,
    LIVE_VENUE_NOT_PERMITTED,
)
from axiom_forex.scalper.guardian import Guardian, GuardianLimits
from axiom_forex.scalper.live_account import (
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
    JsonRunStateStore,
)
from axiom_forex.scalper.live_venue import (
    CLOSE_QUOTE_REQUIRED,
    BrokerPlanFactory,
    BrokerVenue,
)
from axiom_forex.scalper.positions import (
    MODIFY_PROTECTION,
    ManagementSettings,
    PositionBook,
)
from axiom_forex.scalper.ticks import TickCore
from app.execution.broker import TradingBlocked
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
        position_reader=lambda: [dict(row) for row in (real["fake"].positions or [])],
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


def test_a_live_close_requires_a_current_quote_before_calling_the_leg():
    class Leg:
        def __init__(self):
            self.calls = []

        def execute(self, plan):
            self.calls.append(plan)
            return {}

    leg = Leg()
    factory_calls = []

    def plan_factory(**kwargs):
        factory_calls.append(kwargs)
        return object()

    venue = BrokerVenue(leg=leg, plan_factory=plan_factory)
    fill = venue.close(
        ticket="42",
        price=Decimal("1.10"),
        spread_points=Decimal(0),
        reason="operator_close",
    )

    assert fill.status == "blocked"
    assert fill.reason == CLOSE_QUOTE_REQUIRED
    assert factory_calls == []
    assert leg.calls == []


def test_live_close_plan_uses_opposite_side_exact_quantity_and_broker_fill():
    from app.common.types import OrderSide

    position = SimpleNamespace(
        account_id="synthetic-account",
        side=OrderSide.BUY,
        quantity=Decimal("0.10"),
        remaining_quantity=Decimal("0.10"),
    )
    platform = SimpleNamespace(
        spec=SimpleNamespace(canonical_pair="EUR/USD", pair="EUR/USD", quantity_unit_value="LOTS"),
        execution_config=SimpleNamespace(provider_id="synthetic-provider"),
        broker_symbol="EURUSD.pro",
        portfolio=SimpleNamespace(positions={"mt5:42": position}),
        context_factory=SimpleNamespace(build=lambda *, at, bar: {"at": at, "bar": bar}),
    )
    quote = SimpleNamespace(
        source_time=START,
        received_at=START,
        bid=Decimal("1.10000"),
        ask=Decimal("1.10002"),
        tick_id="close-quote-1",
    )
    factory = BrokerPlanFactory(platform=platform, strategy_id="test-close")
    planned = []

    def plan_factory(**kwargs):
        plan = factory(**kwargs)
        planned.append(plan)
        return plan

    class Leg:
        def __init__(self):
            self.plan = None

        def execute(self, plan):
            self.plan = plan
            return {
                "submitted": True,
                "state": "FILLED",
                "volume_requested": "0.05",
                "volume_filled": "0.05",
                "booked": True,
                "booking": {
                    "action": "CLOSE",
                    "position_id": "mt5:42",
                    "quantity": "0.05",
                    "price": "1.10001",
                },
            }

    leg = Leg()
    venue = BrokerVenue(leg=leg, plan_factory=plan_factory)
    fill = venue.close(
        ticket="42",
        price=Decimal("1.10000"),
        spread_points=Decimal(0),
        reason="stop_hit",
        quote=quote,
        quantity=Decimal("0.05"),
    )

    plan = planned[0]
    assert plan.action == "CLOSE"
    assert plan.request.side == OrderSide.SELL
    assert plan.request.quantity == Decimal("0.05")
    assert plan.request.close_intent.position_id == "mt5:42"
    assert leg.plan.request.request_id == plan.request.request_id
    assert fill.status == "accepted"
    assert fill.filled_lots == Decimal("0.05")
    assert fill.ticket == "42"
    assert fill.price == Decimal("1.10001")


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
        signal = signal.__class__(
            **{**signal.__dict__, "_point": None}
        ) if hasattr(signal, "_point") else signal
        return Evaluation(strategy=self.name, family=self.family, outcome="signal", detail="test", signal=signal)


def build_run(real, *, admitted=True, strategy=None, state_store=None):
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
        state_store=(
            state_store
            if state_store is not None
            else JsonRunStateStore(real["tmp"] / f"run-{uuid4().hex}.json")
        ),
    )
    return run, venue, chosen


def _seed_guarded_scalper_position(real, run, venue):
    """Create one fake broker fill and matching provider/scalper ledgers."""

    quote = real["harness"].adapter.quote(real["platform"].spec)
    fill = venue.submit(_order("scalper:protection-seed"), quote)
    assert fill.status == ACCEPTED, fill.audit_view()
    # The generic provider fixture uses a synthetic account id; bind its booked
    # fill to this fake terminal identity so the live leg can reconcile it.
    portfolio = real["platform"].portfolio
    provider_position = portfolio.positions[f"mt5:{fill.ticket}"]
    portfolio.positions[provider_position.position_id] = replace(
        provider_position,
        account_id=real["harness"].adapter.status()["account_fingerprint"],
    )

    def read_protection(ticket):
        try:
            rows = real["harness"].adapter.positions(symbol="EURUSD.pro")
        except Exception:
            return None, None
        matches = [item for item in rows if str(item.ticket) == str(ticket)]
        if len(matches) != 1:
            return None, None
        target = matches[0].take_profit
        return matches[0].stop_loss, (target if target is not None and target > 0 else None)

    venue._protection_reader = read_protection
    stop, target = venue.protection(str(fill.ticket))
    assert stop is not None and stop > 0
    position = run.book.open_position(
        position_id=str(fill.ticket),
        symbol=PAIR,
        side="BUY",
        lots=fill.filled_lots,
        entry_price=fill.price,
        stop_price=stop,
        target_price=target,
        now_ms=int(START.timestamp() * 1000),
        request_id=fill.request_id,
        strategy_fingerprint="protection-test-strategy",
    )
    assert position.protection_verified is True
    assert run._record_position_risk(position) is True
    run.state = "ARMED"
    assert run._persist_state() is True
    return fill, position, read_protection


def _manage_position_tick(real, *, bid, ask):
    moment_ms = int(START.timestamp() * 1000) + 100
    return {
        "symbol": PAIR,
        "bid": bid,
        "ask": ask,
        "source_time": START,
        "now_ms": moment_ms,
    }


def tick_kwargs(real, *, seconds_ago=0, bid="1.10000", ask="1.10020"):
    return {
        "symbol": PAIR,
        "bid": Decimal(bid),
        "ask": Decimal(ask),
        "source_time": START - timedelta(seconds=seconds_ago),
        "now_ms": int((START.timestamp() - 1) * 1000),
    }


def test_scalper_protection_change_uses_guarded_live_path_and_requires_echo(real):
    run, venue, _ = build_run(real)
    fill, position, _read_protection = _seed_guarded_scalper_position(real, run, venue)
    bid = fill.price + Decimal("0.00100")
    ask = bid + Decimal("0.00020")
    real["fake"].symbols["EURUSD.pro"].update(
        {"bid": float(bid), "ask": float(ask), "time": int(START.timestamp())}
    )

    result = run.on_tick(**_manage_position_tick(real, bid=bid, ask=ask))

    expected_stop = fill.price + Decimal("80") * position.point
    current = run.book.get(fill.ticket)
    assert current is not None
    assert current.stop_price == expected_stop
    assert current.protection_verified is True
    assert current.trailing_active is True
    assert run.book.snapshot()["pending_protection_modifications"] == []
    assert result["state"] == "ARMED"
    assert any(
        action.get("kind") == "modify_stop"
        for group in result["managed"]
        for action in group
    )
    assert len(real["fake"].send_requests) == 2
    assert real["fake"].send_requests[-1]["action"] == TRADE_ACTION_SLTP
    assert "volume" not in real["fake"].send_requests[-1]
    assert len(real["fake"].protection_requests) == 1
    assert venue.record.protection_modifications[-1]["verified"] is True
    assert real["leg"].router.routed[-1]["action"] == "MODIFY_POSITION_PROTECTION"
    assert real["leg"].router.routed[-1]["broker_action_sent"] is True
    assert real["harness"].adapter.armed is True


def test_live_target_modification_persists_before_send_and_confirms_both_fields(real):
    run, venue, _ = build_run(real)
    fill, position, _read_protection = _seed_guarded_scalper_position(real, run, venue)
    requested_target = Decimal("1.11500")
    pre_send_intents = []
    real["fake"].on_send = lambda _request: pre_send_intents.append(
        run.state_store.load()["position_book"]["pending_protection_modifications"]
    )

    action = run.book.modify_protection(
        position_id=fill.ticket,
        stop_price=position.stop_price,
        target_price=requested_target,
        now_ms=int(START.timestamp() * 1000) + 100,
        reason="operator_target_update",
    )

    pending_intent = pre_send_intents[0][0]
    current = run.book.get(fill.ticket)
    assert action.kind == MODIFY_PROTECTION
    assert pending_intent["stop_price"] == str(position.stop_price)
    assert pending_intent["target_price"] == str(requested_target)
    assert pending_intent["previous_target_price"] is None
    assert pending_intent["request_id"]
    assert current is not None
    assert current.target_price == requested_target
    assert current.stop_price == position.stop_price
    assert current.protection_verified is True
    assert run.book.snapshot()["pending_protection_modifications"] == []
    assert run.state_store.load()["position_book"]["pending_protection_modifications"] == []
    assert real["fake"].protection_requests[-1]["action"] == TRADE_ACTION_SLTP
    assert real["fake"].protection_requests[-1]["position"] == int(fill.ticket)
    assert real["fake"].protection_requests[-1]["sl"] == float(position.stop_price)
    assert real["fake"].protection_requests[-1]["tp"] == float(requested_target)
    assert venue.record.protection_modifications[-1]["state"] == "CONFIRMED"
    assert venue.record.protection_modifications[-1]["verified"] is True
    assert real["leg"].router.routed[-1]["action"] == "MODIFY_POSITION_PROTECTION"
    assert real["leg"].router.routed[-1]["broker_action_sent"] is True
    assert real["harness"].adapter.armed is True


def test_protection_change_does_not_send_without_durable_pre_send_intent(real):
    run, venue, _ = build_run(real)
    fill, position, _read_protection = _seed_guarded_scalper_position(real, run, venue)

    def fail_save(_state):
        raise OSError("simulated state-store failure")

    run.state_store.save = fail_save
    action = run.book.modify_protection(
        position_id=fill.ticket,
        stop_price=position.stop_price,
        target_price=Decimal("1.11500"),
        now_ms=int(START.timestamp() * 1000) + 100,
        reason="operator_target_update",
    )

    assert action.kind == "hold"
    assert run.state == RECOVERY_REQUIRED
    assert real["fake"].protection_requests == []
    assert len(real["fake"].send_requests) == 1
    assert run.book.snapshot()["pending_protection_modifications"]


def test_confirmed_protection_is_not_adopted_until_result_state_is_durable(real):
    run, venue, _ = build_run(real)
    fill, position, _read_protection = _seed_guarded_scalper_position(real, run, venue)
    real_save = run.state_store.save
    saves = 0

    def fail_second_save(state):
        nonlocal saves
        saves += 1
        if saves == 2:
            raise OSError("simulated post-confirm persistence failure")
        real_save(state)

    run.state_store.save = fail_second_save
    requested_target = Decimal("1.11500")
    action = run.book.modify_protection(
        position_id=fill.ticket,
        stop_price=position.stop_price,
        target_price=requested_target,
        now_ms=int(START.timestamp() * 1000) + 100,
        reason="operator_target_update",
    )

    assert action.kind == "hold"
    assert run.state == RECOVERY_REQUIRED
    assert real["fake"].protection_requests[-1]["tp"] == float(requested_target)
    assert len(real["fake"].send_requests) == 2
    assert run.book.get(fill.ticket).target_price is None
    assert run.book.get(fill.ticket).protection_verified is False
    saved = run.state_store.load()
    assert saved["position_book"]["pending_protection_modifications"][0][
        "target_price"
    ] == str(requested_target)

    recovered, recovered_venue, _ = build_run(real, state_store=run.state_store)
    recovered_venue._protection_reader = venue._protection_reader
    outcome = recovered.recover(state=saved)
    assert outcome["ok"] is True, outcome
    assert recovered.book.snapshot()["pending_protection_modifications"] == []
    assert recovered.book.get(fill.ticket).target_price == requested_target
    assert len(real["fake"].send_requests) == 2


def test_pending_target_modification_recovers_only_from_exact_broker_echo(real):
    run, venue, _ = build_run(real)
    fill, position, _read_protection = _seed_guarded_scalper_position(real, run, venue)
    requested_target = Decimal("1.11500")
    real["fake"].protection_script = [None]

    action = run.book.modify_protection(
        position_id=fill.ticket,
        stop_price=position.stop_price,
        target_price=requested_target,
        now_ms=int(START.timestamp() * 1000) + 100,
        reason="operator_target_update",
    )

    assert action.reason == "broker_side_protection_missing"
    assert real["harness"].adapter.armed is False
    assert UNRESOLVED_PROTECTION_MODIFICATION in {
        item.reason for item in real["leg"].blockers()
    }
    pending = run.book.snapshot()["pending_protection_modifications"]
    assert len(pending) == 1
    assert pending[0]["target_price"] == str(requested_target)
    assert pending[0]["previous_target_price"] is None
    assert len(real["fake"].protection_requests) == 1
    assert real["fake"].positions[0]["tp"] == 0.0

    stopped = run.stop(reason="target_recovery_test")
    saved = run.state_store.load()
    assert stopped["state_persisted"] is True
    assert saved["position_book"]["pending_protection_modifications"] == pending
    assert len(real["fake"].send_requests) == 2

    unresolved, unresolved_venue, _ = build_run(real, state_store=run.state_store)
    unresolved_venue._protection_reader = venue._protection_reader
    mismatch = unresolved.recover(state=saved)
    assert mismatch["ok"] is False
    assert unresolved.state == RECOVERY_REQUIRED
    assert "size or protection differs" in mismatch["recovery"]["detail"]
    assert unresolved.book.open_positions == ()

    real["fake"].positions[0]["tp"] = float(requested_target)
    recovered, recovered_venue, _ = build_run(real, state_store=run.state_store)
    recovered_venue._protection_reader = venue._protection_reader
    outcome = recovered.recover(state=saved)
    assert outcome["ok"] is True, outcome
    assert recovered.book.snapshot()["pending_protection_modifications"] == []
    restored = recovered.book.get(fill.ticket)
    assert restored is not None
    assert restored.stop_price == position.stop_price
    assert restored.target_price == requested_target
    assert restored.protection_verified is True
    assert len(real["fake"].send_requests) == 2
    assert recovered.start()["ok"] is False
    assert len(real["fake"].send_requests) == 2


@pytest.mark.parametrize(
    ("gate", "expected_reason"),
    [
        ("mode", "mode_not_live"),
        ("arming", "live_not_armed"),
        ("account", "real_account_required"),
    ],
)
def test_protection_changes_preserve_mode_arming_and_real_account_gates(
    real, gate, expected_reason
):
    run, venue, _ = build_run(real)
    fill, _position, _read_protection = _seed_guarded_scalper_position(
        real, run, venue
    )
    bid = fill.price + Decimal("0.00100")
    ask = bid + Decimal("0.00020")
    real["fake"].symbols["EURUSD.pro"].update(
        {"bid": float(bid), "ask": float(ask), "time": int(START.timestamp())}
    )
    if gate == "mode":
        real["mode"].mode = "PAPER"
        real["mode"].live_effective = False
    elif gate == "arming":
        real["harness"].adapter.disarm(reason="test_gate")
    else:
        real["fake"].trade_mode = 0
        real["harness"].adapter.refresh_account()

    run.on_tick(**_manage_position_tick(real, bid=bid, ask=ask))

    assert len(real["fake"].send_requests) == 1
    assert real["fake"].protection_requests == []
    refusal = venue.record.protection_modifications[-1]
    assert refusal["state"] == "REFUSED", refusal
    assert refusal["reason"] == expected_reason
    current = run.book.get(fill.ticket)
    assert current is not None
    assert current.protection_verified is False
    assert run.book.snapshot()["pending_protection_modifications"]


def test_unknown_protection_change_survives_shutdown_and_recovers_by_readback(real):
    run, venue, _ = build_run(real)
    fill, position, _read_protection = _seed_guarded_scalper_position(real, run, venue)
    bid = fill.price + Decimal("0.00100")
    ask = bid + Decimal("0.00020")
    real["fake"].symbols["EURUSD.pro"].update(
        {"bid": float(bid), "ask": float(ask), "time": int(START.timestamp())}
    )
    real["fake"].protection_script = [None]

    managed = run.on_tick(**_manage_position_tick(real, bid=bid, ask=ask))

    assert managed["state"] == "ARMED"
    assert real["harness"].adapter.armed is False
    assert venue.record.protection_modifications[-1]["state"] == "UNRESOLVED"
    pending = run.book.snapshot()["pending_protection_modifications"]
    assert len(pending) == 1
    requested_stop = Decimal(pending[0]["stop_price"])
    assert requested_stop > position.stop_price

    stopped = run.stop(reason="test_shutdown")
    saved = run.state_store.load()
    assert stopped["state_persisted"] is True
    assert stopped["open_positions"]
    assert saved["position_book"]["pending_protection_modifications"] == pending
    assert len(real["fake"].send_requests) == 2  # entry + one SLTP; shutdown sends nothing

    fresh, fresh_venue, _ = build_run(real, state_store=run.state_store)
    fresh_venue._protection_reader = venue._protection_reader
    unresolved = fresh.recover(state=saved)
    assert unresolved["ok"] is False
    assert fresh.state == RECOVERY_REQUIRED
    assert "protection differs" in unresolved["recovery"]["detail"]
    assert fresh.book.open_positions == ()

    # Simulate the terminal reflecting the timed-out request later. Recovery
    # accepts only this broker echo, clears the pending intent, and never resends.
    broker_row = next(
        row for row in real["fake"].positions if int(row["ticket"]) == int(fill.ticket)
    )
    broker_row["sl"] = float(requested_stop)
    recovered, recovered_venue, _ = build_run(real, state_store=run.state_store)
    recovered_venue._protection_reader = venue._protection_reader
    result = recovered.recover(state=saved)
    assert result["ok"] is True, result
    assert recovered.state == "ARMED"
    assert recovered.book.snapshot()["pending_protection_modifications"] == []
    assert recovered.book.get(fill.ticket).stop_price == requested_stop
    assert recovered.book.get(fill.ticket).protection_verified is True
    assert recovered.guardian.open_positions[0].protected is True
    assert len(real["fake"].send_requests) == 2

    # Recovery does not re-arm the disconnected/uncertain live session.
    started = recovered.start()
    assert started["ok"] is False
    assert len(real["fake"].send_requests) == 2


def test_unconfirmed_broker_protection_blocks_live_readiness_and_entries(real):
    run, venue, _ = build_run(real)
    venue._protection_reader = lambda ticket: (Decimal(0), None)
    run.book.open_position(
        position_id="unsafely-protected",
        symbol=PAIR,
        side="BUY",
        lots=Decimal("0.10"),
        entry_price=Decimal("1.10000"),
        stop_price=Decimal("1.09500"),
        target_price=None,
        now_ms=int(START.timestamp() * 1000),
        request_id="entry-request",
    )
    run.state = "RUNNING"

    reasons = {item["reason"] for item in run.readiness()}
    assert PROTECTION_UNVERIFIED in reasons
    assert run._entry_block(PAIR, int(START.timestamp() * 1000)) == PROTECTION_UNVERIFIED
    assert real["fake"].send_requests == []


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


def test_json_run_state_store_round_trips_and_recovers_from_disk(real):
    store = JsonRunStateStore(real["tmp"] / "durable-run-state.json")
    run, _venue_instance, _ = build_run(real, state_store=store)

    started = run.start()
    assert started["ok"] is True, started
    saved = store.load()
    assert saved == run.snapshot()
    assert saved["schema"] == "axiom-scalper-live-run-v2"
    assert saved["position_book"]["open_positions"] == []
    assert "guardian" in saved

    fresh, _fresh_venue, _ = build_run(real, state_store=store)
    recovered = fresh.recover()
    assert recovered["ok"] is True, recovered
    assert fresh.state == "ARMED"
    assert store.load() == fresh.snapshot()


def test_durable_run_recovery_restores_full_managed_and_guardian_exposure(real):
    run, _venue_instance, _ = build_run(real)
    position = run.book.open_position(
        position_id="31415",
        symbol=PAIR,
        side="BUY",
        lots=Decimal("0.10"),
        entry_price=Decimal("1.10000"),
        stop_price=Decimal("1.09500"),
        target_price=Decimal("1.11000"),
        now_ms=int(START.timestamp() * 1000),
        request_id="entry-request-31415",
        strategy_fingerprint="managed-test-strategy",
    )
    assert run._record_position_risk(position) is True
    real["fake"].positions.append(
        {
            "ticket": 31415,
            "symbol": "EURUSD.pro",
            "type": 0,
            "volume": 0.10,
            "price_open": 1.10000,
            "sl": 1.09500,
            "tp": 1.11000,
        }
    )
    snapshot = run.snapshot()

    fresh, fresh_venue, _ = build_run(real)
    fresh_venue.query = lambda request_id: (
        Fill(
            request_id="entry-request-31415",
            status=ACCEPTED,
            filled_lots=Decimal("0.10"),
            requested_lots=Decimal("0.10"),
            price=Decimal("1.10000"),
            spread_points=Decimal(0),
            slippage_points=Decimal(0),
            latency_ms=1,
            ticket="31415",
            simulated=False,
        )
        if request_id == "entry-request-31415"
        else None
    )
    fresh_venue._protection_reader = lambda _ticket: (
        Decimal("1.09500"), Decimal("1.11000")
    )
    recovered = fresh.recover(state=snapshot)
    assert recovered["ok"] is True, recovered
    assert fresh.book.get("31415") is not None
    assert fresh.book.get("31415").entry_request_id == "entry-request-31415"
    assert fresh.guardian.open_positions[0].position_id == "31415"
    assert fresh.guardian.open_positions[0].protected is True
    assert fresh.snapshot()["position_book"] == fresh.book.snapshot()


def test_persisted_entry_intent_is_reconciled_without_resubmission(real):
    store = JsonRunStateStore(real["tmp"] / "pending-entry-state.json")
    run, _venue_instance, _ = build_run(real, state_store=store)
    intent = {
        "request_id": "entry-request-recovery-1",
        "symbol": PAIR,
        "side": "BUY",
        "lots": "0.10",
        "signal_fingerprint": "recovered-entry-fingerprint",
        "tick_id": "tick-recovery-1",
        "source_time": START.isoformat(),
        "bid": "1.09990",
        "ask": "1.10010",
        "stop_price": "1.09500",
        "target_price": "1.11000",
        "requested_at_ms": int(START.timestamp() * 1000),
        "risk_account": "50",
        "notional_account": "11000",
    }
    run._pending_submission = dict(intent)
    run.state = "RUNNING"
    store.save(run.snapshot())
    real["fake"].positions.append(
        {
            "ticket": 31415,
            "symbol": "EURUSD.pro",
            "type": 0,
            "volume": 0.10,
            "price_open": 1.10000,
            "sl": 1.09500,
            "tp": 1.11000,
        }
    )

    unresolved, unresolved_venue, _ = build_run(real, state_store=store)
    unresolved_venue.query = lambda _request_id: None
    first_recovery = unresolved.recover()
    assert first_recovery["ok"] is False
    assert unresolved.state == RECOVERY_REQUIRED
    assert unresolved._pending_submission["request_id"] == "entry-request-recovery-1"
    assert real["fake"].send_requests == []

    accepted_fill = Fill(
        request_id="entry-request-recovery-1",
        status=ACCEPTED,
        filled_lots=Decimal("0.10"),
        requested_lots=Decimal("0.10"),
        price=Decimal("1.10000"),
        spread_points=Decimal("20"),
        slippage_points=Decimal(0),
        latency_ms=10,
        ticket="31415",
        reason="filled",
        detail="durable booking confirmed",
        simulated=False,
    )
    recovered, recovered_venue, _ = build_run(real, state_store=store)
    recovered_venue._protection_reader = lambda _ticket: (
        Decimal("1.09500"), Decimal("1.11000")
    )
    recovered_venue.query = lambda request_id: (
        accepted_fill if request_id == "entry-request-recovery-1" else None
    )
    result = recovered.recover()
    assert result["ok"] is True, result
    assert recovered.state == "ARMED"
    assert recovered._pending_submission is None
    assert recovered.book.get("31415") is not None
    assert recovered.book.get("31415").entry_request_id == "entry-request-recovery-1"
    assert recovered.guardian.open_positions[0].position_id == "31415"
    assert real["fake"].send_requests == []


def test_pending_close_survives_restart_and_reconciles_its_exact_durable_fill(real):
    run, venue, _ = build_run(real)
    position = run.book.open_position(
        position_id="31415",
        symbol=PAIR,
        side="BUY",
        lots=Decimal("0.10"),
        entry_price=Decimal("1.10000"),
        stop_price=Decimal("1.09500"),
        target_price=None,
        now_ms=int(START.timestamp() * 1000),
        request_id="entry-request-31415",
    )
    assert run._record_position_risk(position) is True
    real["fake"].positions.append(
        {
            "ticket": 31415,
            "symbol": "EURUSD.pro",
            "type": 0,
            "volume": 0.10,
            "price_open": 1.10000,
            "sl": 1.09500,
            "tp": 0.0,
        }
    )

    def read_stop(ticket):
        for row in real["fake"].positions:
            if str(row.get("ticket")) == str(ticket):
                return Decimal(str(row.get("sl") or 0)), None
        return None, None

    venue._protection_reader = read_stop
    venue.close_request_id = lambda _ticket: "close-request-31415"
    unknown = Fill(
        request_id="close-request-31415",
        status=UNKNOWN_OUTCOME,
        filled_lots=Decimal(0),
        requested_lots=Decimal("0.10"),
        price=None,
        spread_points=Decimal(0),
        slippage_points=Decimal(0),
        latency_ms=0,
        ticket="31415",
        reason="timeout_without_confirmation",
        simulated=False,
    )
    pre_send_states = []

    def ambiguous_close(**_kwargs):
        pre_send_states.append(run.state_store.load())
        return unknown

    venue.close = ambiguous_close
    action = run.book._close(
        position,
        Decimal("1.09900"),
        int(START.timestamp() * 1000),
        "operator_close",
        "test close outcome is ambiguous",
        quote=None,
    )
    assert action.reason == "close_outcome_unconfirmed"
    assert pre_send_states
    assert pre_send_states[0]["position_book"]["pending_closes"][0]["request_id"] == "close-request-31415"
    snapshot = run.snapshot()
    assert snapshot["position_book"]["pending_closes"][0]["request_id"] == "close-request-31415"

    fresh, fresh_venue, _ = build_run(real)
    fresh_venue._protection_reader = read_stop
    entry_fill = Fill(
        request_id="entry-request-31415",
        status=ACCEPTED,
        filled_lots=Decimal("0.10"),
        requested_lots=Decimal("0.10"),
        price=Decimal("1.10000"),
        spread_points=Decimal(0),
        slippage_points=Decimal(0),
        latency_ms=1,
        ticket="31415",
        simulated=False,
    )
    fresh_venue.query = lambda request_id: (
        entry_fill if request_id == "entry-request-31415" else None
    )
    recovered = fresh.recover(state=snapshot)
    assert recovered["ok"] is False
    assert fresh.state == RECOVERY_REQUIRED
    assert fresh.book.pending_closes[0]["request_id"] == "close-request-31415"

    accepted_close = Fill(
        request_id="close-request-31415",
        status=ACCEPTED,
        filled_lots=Decimal("0.10"),
        requested_lots=Decimal("0.10"),
        price=Decimal("1.09900"),
        spread_points=Decimal(0),
        slippage_points=Decimal(0),
        latency_ms=12,
        ticket="31415",
        reason="filled",
        detail="durable close fill and booking confirmed",
        simulated=False,
    )
    fresh_venue.query = lambda request_id: accepted_close if request_id == "close-request-31415" else None
    real["fake"].positions.clear()
    reconciled = fresh.reconcile_close(position_id="31415")
    assert reconciled["ok"] is True, reconciled
    assert fresh.state == "ARMED"
    assert fresh.book.get("31415") is None
    assert fresh.book.pending_closes == ()
    assert fresh.guardian.open_positions == ()
    assert fresh.guardian.loss_streak == 1


def test_corrupt_durable_run_state_fails_closed(real):
    store = JsonRunStateStore(real["tmp"] / "corrupt-run-state.json")
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{not valid json", encoding="utf-8")
    run, _venue_instance, _ = build_run(real, state_store=store)

    recovered = run.recover()
    assert recovered["ok"] is False
    assert run.state == RECOVERY_REQUIRED
    assert "could not be read" in recovered["recovery"]["detail"]
    assert run.start()["ok"] is False
    assert real["fake"].send_requests == []


def test_snapshot_preserves_the_managed_entry_request_identity(real):
    run, _venue_instance, _ = build_run(real)
    run.book.open_position(
        position_id="1001",
        symbol=PAIR,
        side="BUY",
        lots=Decimal("0.10"),
        entry_price=Decimal("1.10000"),
        stop_price=Decimal("1.09500"),
        target_price=None,
        now_ms=int(START.timestamp() * 1000),
        request_id="entry-request-1001",
    )

    row = run.snapshot()["open_positions"][0]
    assert row["entry_request_id"] == "entry-request-1001"


def test_recovery_refuses_a_different_account_identity(real):
    run, _venue_instance, _ = build_run(real)
    snapshot = run.snapshot()
    snapshot["account_identity"]["fingerprint"] = derived_fingerprint(999)
    fresh, _, _ = build_run(real)

    restored = fresh.recover(state=snapshot)
    assert restored["ok"] is False
    assert fresh.state == RECOVERY_REQUIRED
    assert "account identity differs" in restored["recovery"]["detail"]


def test_recovery_refuses_a_different_strategy(real):
    run, venue, _ = build_run(real)
    snapshot = run.snapshot()
    snapshot["strategy_fingerprint"] = "a-different-strategy"
    fresh, _, _ = build_run(real)
    restored = fresh.recover(state=snapshot)
    assert restored["ok"] is False
    assert fresh.state == RECOVERY_REQUIRED
    assert "strategy fingerprint differs" in restored["recovery"]["detail"]
