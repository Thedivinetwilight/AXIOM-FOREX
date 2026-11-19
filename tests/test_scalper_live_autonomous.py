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
        expected_account=ExpectedAccount(server=SERVER, login=LOGIN, currency="USD", fingerprint=derived_fingerprint()),
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
    verdict = verify_real_account(status(**override), ExpectedAccount(server=SERVER, login=LOGIN, currency="USD", fingerprint=derived_fingerprint()))
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
    real["leg"].expected_account = ExpectedAccount(server=SERVER, login=LOGIN + 1, currency="USD", fingerprint=derived_fingerprint(LOGIN + 1))
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


def build_run(real, *, admitted=True, strategy=None, state_store=None, account_mode="REAL"):
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
        expected_account_mode=account_mode,
        expected_account=ExpectedAccount(server=SERVER, login=LOGIN, currency="USD", fingerprint=derived_fingerprint()),
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
    portfolio.account = replace(portfolio.account, account_id=real["harness"].adapter.status()["account_fingerprint"])
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
    before = run.state_store.path.read_bytes()
    outcome = recovered.recover(state=saved)
    assert not outcome["ok"] and recovered.state == RECOVERY_REQUIRED
    assert "pending protection requires exact durable-request" in outcome["recovery"]["detail"]
    assert recovered.book.open_positions == ()
    assert run.state_store.path.read_bytes() == before
    assert len(real["fake"].send_requests) == 2


def test_pending_target_modification_is_not_adopted_from_broker_echo_alone(real):
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
    before = run.state_store.path.read_bytes()
    outcome = recovered.recover(state=saved)
    assert not outcome["ok"] and recovered.state == RECOVERY_REQUIRED
    assert "pending protection requires exact durable-request" in outcome["recovery"]["detail"]
    assert recovered.book.open_positions == ()
    assert run.state_store.path.read_bytes() == before
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

    outcome = run.on_tick(**_manage_position_tick(real, bid=bid, ask=ask))

    assert len(real["fake"].send_requests) == 1
    assert real["fake"].protection_requests == []
    current = run.book.get(fill.ticket)
    assert current is not None
    if gate == "arming":
        refusal = venue.record.protection_modifications[-1]
        assert refusal["state"] == "REFUSED", refusal
        assert refusal["reason"] == expected_reason
        assert current.protection_verified is False
        assert run.book.snapshot()["pending_protection_modifications"]
    else:
        # Account and execution-mode identity are checked before PositionBook
        # sees a quote: no protection/close request may target the wrong account.
        assert outcome["blocked"] == expected_reason
        assert venue.record.protection_modifications == []
        assert current.protection_verified is True
        assert run.book.snapshot()["pending_protection_modifications"] == []


def test_unknown_protection_change_survives_shutdown_and_stays_unresolved(real):
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

    # An ordinary entry check must not silently adopt a timed-out SLTP even
    # when the broker later echoes it. Explicit recovery owns that transition.
    broker_row = next(
        row for row in real["fake"].positions if int(row["ticket"]) == int(fill.ticket)
    )
    original_stop = broker_row["sl"]
    broker_row["sl"] = float(requested_stop)
    run.state = "RUNNING"
    assert run._entry_block(PAIR, int(START.timestamp() * 1000)) == "protection_reconciliation_required"
    assert run.book.snapshot()["pending_protection_modifications"] == pending
    assert position.stop_price != requested_stop
    broker_row["sl"] = original_stop

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

    # A matching broker echo is not, by itself, proof of the original request.
    broker_row = next(
        row for row in real["fake"].positions if int(row["ticket"]) == int(fill.ticket)
    )
    broker_row["sl"] = float(requested_stop)
    recovered, recovered_venue, _ = build_run(real, state_store=run.state_store)
    recovered_venue._protection_reader = venue._protection_reader
    before = run.state_store.path.read_bytes()
    result = recovered.recover(state=saved)
    assert not result["ok"] and recovered.state == RECOVERY_REQUIRED
    assert "pending protection requires exact durable-request" in result["recovery"]["detail"]
    assert recovered.book.open_positions == ()
    assert run.state_store.path.read_bytes() == before
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
    assert "account_unverified" in reasons
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
    state_before = run.state
    stopped = run.stop(reason="operator_stop")
    assert stopped["state"] == (RECOVERY_REQUIRED if state_before == RECOVERY_REQUIRED else "STOPPED")
    assert "entr" in stopped["message"].lower()
    outcome = run.on_tick(**tick_kwargs(real))
    assert outcome["execution"] is None
    assert outcome["blocked"] == ("recovery_required" if state_before == RECOVERY_REQUIRED else "not_running")
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
    assert saved["schema"] == "axiom-scalper-live-run-v4"
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
    ledger = real["leg"].portfolio
    quantity, _ = real["leg"]._catalog_quantity_from_broker_volume(Decimal("0.10"))
    booked = ledger.open(position_id="mt5:31415", side="BUY", quantity=quantity,
                         price=Decimal("1.10000"), at=START)
    ledger.account = replace(ledger.account, account_id=real["harness"].adapter.status()["account_fingerprint"])
    ledger.positions[booked.position_id] = replace(
        booked, account_id=ledger.account.account_id)
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
    tick_id = "tick-recovery-1"
    fingerprint = run.admitted.strategy.fingerprint
    request_id = f"{fingerprint[:12]}:{tick_id[:12]}:BUY"
    intent = {
        "request_id": request_id,
        "symbol": PAIR,
        "side": "BUY",
        "lots": "0.10",
        "signal_fingerprint": fingerprint,
        "tick_id": tick_id,
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
    assert unresolved._pending_submission["request_id"] == request_id
    assert real["fake"].send_requests == []

    accepted_fill = Fill(
        request_id=request_id,
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
        accepted_fill if request_id == request_id else None
    )
    result = recovered.recover()
    assert result["ok"] is True, result
    assert recovered.state == "ARMED"
    assert recovered._pending_submission is None
    assert recovered.book.get("31415") is not None
    assert recovered.book.get("31415").entry_request_id == request_id
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
    ledger = real["leg"].portfolio
    quantity, _ = real["leg"]._catalog_quantity_from_broker_volume(Decimal("0.10"))
    booked = ledger.open(position_id="mt5:31415", side="BUY", quantity=quantity,
                         price=Decimal("1.10000"), at=START)
    ledger.account = replace(ledger.account, account_id=real["harness"].adapter.status()["account_fingerprint"])
    ledger.positions[booked.position_id] = replace(
        booked, account_id=ledger.account.account_id)
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


@pytest.mark.parametrize("field,value", [("account_mode", "DEMO"), ("currency", "EUR")])
def test_recovery_refuses_account_mode_or_currency_mismatch(real, field, value):
    run, _, _ = build_run(real)
    snapshot = run.snapshot()
    snapshot["account_identity"][field] = value
    fresh, _, _ = build_run(real)
    outcome = fresh.recover(state=snapshot)
    assert not outcome["ok"]
    assert "account identity differs" in outcome["recovery"]["detail"]
    assert real["fake"].send_requests == []


def test_account_change_does_not_relabel_original_durable_run(real):
    run, _, _ = build_run(real)
    assert run.start()["ok"]
    original = run.state_store.load()["account_identity"]
    real["fake"].trade_mode = 0
    real["harness"].adapter.refresh_account()
    assert not run._account().ok
    run.on_tick(symbol=PAIR, bid=Decimal("1.10000"), ask=Decimal("1.10002"),
                source_time=START, now_ms=int(START.timestamp() * 1000))
    assert run.state_store.load()["account_identity"] == original
    assert run.snapshot()["account_identity"] == original
    assert original["account_mode"] == "REAL"
    assert original["currency"] == "USD"
    assert real["fake"].send_requests == []


def test_legacy_run_missing_account_type_and_currency_is_preserved_and_blocked(real):
    store = JsonRunStateStore(real["tmp"] / "legacy-run-state.json")
    run, _, _ = build_run(real, state_store=store)
    old = run.snapshot()
    old["schema"] = "axiom-scalper-live-run-v2"
    old["account_identity"].pop("currency")
    old["account_identity"].pop("account_mode")
    store.save(old)
    before = store.path.read_bytes()
    fresh, _, _ = build_run(real, state_store=store)
    outcome = fresh.recover()
    assert not outcome["ok"]
    assert "account identity differs" in outcome["recovery"]["detail"]
    assert store.path.read_bytes() == before
    assert real["fake"].send_requests == []


def test_recovery_refuses_a_different_strategy(real):
    run, venue, _ = build_run(real)
    snapshot = run.snapshot()
    snapshot["strategy_fingerprint"] = "a-different-strategy"
    fresh, _, _ = build_run(real)
    restored = fresh.recover(state=snapshot)
    assert restored["ok"] is False
    assert fresh.state == RECOVERY_REQUIRED
    assert "strategy fingerprint differs" in restored["recovery"]["detail"]


@pytest.mark.parametrize("redacted", [
    {"server": "", "currency": "USD"},
    {"server": SERVER, "currency": ""},
    {"currency": "USD"},
    {"server": SERVER},
])
def test_real_account_refuses_missing_terminal_identity_fields(redacted):
    expected = ExpectedAccount(server=SERVER, login=LOGIN, currency="USD",
                               fingerprint=derived_fingerprint())
    assert not verify_real_account(status(redacted_account=redacted), expected).ok


@pytest.mark.parametrize("expected", [
    ExpectedAccount(server=SERVER, login=LOGIN, currency="USD", fingerprint=""),
    ExpectedAccount(server=SERVER, login=LOGIN, currency="", fingerprint=derived_fingerprint()),
    ExpectedAccount(server=SERVER, login=0, currency="USD", fingerprint=derived_fingerprint()),
    ExpectedAccount(server=SERVER, login=LOGIN + 1, currency="USD", fingerprint=derived_fingerprint()),
])
def test_real_account_refuses_incomplete_or_inconsistent_declaration(expected):
    verdict = verify_real_account(status(), expected)
    assert not verdict.ok
    assert "expected_account_not_declared" in verdict.reasons


def test_status_read_does_not_adopt_or_clear_pending_protection(real):
    run, venue, _ = build_run(real)
    fill, position, _read_protection = _seed_guarded_scalper_position(real, run, venue)
    real["fake"].protection_script = [None]
    requested_target = Decimal("1.11500")
    run.book.modify_protection(position_id=fill.ticket, stop_price=position.stop_price,
                               target_price=requested_target,
                               now_ms=int(START.timestamp() * 1000) + 100,
                               reason="test_pending")
    real["fake"].positions[0]["tp"] = float(requested_target)
    before = run.book.snapshot()
    status = run.status()
    assert run.book.snapshot() == before
    assert before["pending_protection_modifications"]
    assert any(item["reason"] == "protection_reconciliation_required" for item in status["blockers"])


def test_guarded_query_refuses_missing_or_mismatched_durable_record_even_if_memory_claims_success():
    request_id = "original-request"
    durable = {"request_id": request_id, "account_fingerprint": "expected", "state": "UNRESOLVED"}
    leg = SimpleNamespace(repository=SimpleNamespace(get_order=lambda _id: durable),
                          last_outcome={"request_id": request_id, "state": "REJECTED"},
                          history=[])
    venue = BrokerVenue(leg=leg, plan_factory=lambda **kw: None,
                        query_guard=lambda row: row.get("account_fingerprint") == "expected")
    # The in-memory definitive result cannot overwrite the unresolved durable row.
    assert venue.query(request_id).status == UNKNOWN_OUTCOME
    durable["account_fingerprint"] = "other-account"
    assert venue.query(request_id) is None
    leg.repository = SimpleNamespace(get_order=lambda _id: None)
    assert venue.query(request_id) is None
    assert venue.record.submissions == []


def test_guarded_query_refuses_a_guard_exception():
    leg = SimpleNamespace(repository=SimpleNamespace(get_order=lambda _id: {"request_id": "r"}))
    def broken(_row):
        raise ValueError("identity unavailable")
    venue = BrokerVenue(leg=leg, plan_factory=lambda **kw: None, query_guard=broken)
    assert venue.query("r") is None


@pytest.mark.parametrize("account_mode,trade_mode", [("REAL", 2), ("DEMO", 0)])
def test_same_guarded_fake_runner_arms_starts_and_sends_for_both_mt5_types(real, account_mode, trade_mode):
    # Evidence is a synthetic fixture only; this test is not production admission.
    fake = real["fake"]
    fake.trade_mode = trade_mode
    adapter = real["harness"].adapter
    adapter.refresh_account()
    real["harness"].arm()
    real["leg"].require_real_account = False
    real["leg"].required_account_mode = account_mode
    run, venue, _ = build_run(real, account_mode=account_mode)
    assert run.arm()["ok"]
    assert fake.send_requests == []
    assert run.start()["ok"]
    results = warm(run, real, count=60)
    assert any(row.get("execution") for row in results)
    assert len(fake.send_requests) == 1
    assert run.snapshot()["account_identity"]["account_mode"] == account_mode
    assert venue.leg.router.live is adapter
    before = len(fake.send_requests)
    run.stop(reason="operator_stop")
    assert len(fake.send_requests) == before


@pytest.mark.parametrize("mode", ["CONTEST", "UNKNOWN", "UNCONFIRMED"])
def test_generic_mt5_account_refuses_unsupported_types(mode):
    from axiom_forex.scalper.live_account import verify_mt5_account
    expected = ExpectedAccount(server=SERVER, login=LOGIN, currency="USD", fingerprint=derived_fingerprint())
    assert not verify_mt5_account(status(account_mode=mode), expected).ok


def test_generic_account_type_binding_refuses_mode_change():
    from axiom_forex.scalper.live_account import verify_mt5_account
    expected = ExpectedAccount(server=SERVER, login=LOGIN, currency="USD", fingerprint=derived_fingerprint())
    assert verify_mt5_account(status(account_mode="DEMO"), expected, expected_mode="DEMO").ok
    assert not verify_mt5_account(status(account_mode="REAL"), expected, expected_mode="DEMO").ok


@pytest.mark.parametrize("account_mode,trade_mode", [("REAL", 2), ("DEMO", 0)])
def test_host_arm_and_start_build_real_guarded_stack_on_both_fake_accounts(real, account_mode, trade_mode):
    import json
    from tests.test_scalper_live_workstation import _host
    from axiom_forex.paper_runner.scalper_live_host import ARM_PHRASE, START_PHRASE
    from axiom_forex.scalper.signals import default_strategies

    host, _, _ = _host(real["tmp"])
    host.adapter = real["harness"].adapter
    host.adapter.disarm(reason="test_setup")
    real["fake"].trade_mode = trade_mode
    host.adapter.refresh_account()
    settings = json.loads(host.settings_path.read_text())
    settings["account"] = {"server": SERVER, "login": LOGIN, "currency": "USD", "fingerprint": derived_fingerprint()}
    host.settings_path.write_text(json.dumps(settings), encoding="utf-8")
    host.platform_factory = lambda _pair: real["platform"]
    host._close_platform = lambda _platform: None  # shared fake fixture owns its repository
    host.live_workflow.armed_strategy = SimpleNamespace(register_delegate=lambda *_a: None,
                                                         unregister_delegate=lambda *_a: None)
    candidate = default_strategies()[0].fingerprint
    confirmation = f"{account_mode} {derived_fingerprint()}"
    armed = host._do_arm(candidate_id=candidate, confirmation=ARM_PHRASE, account_confirmation=confirmation)
    assert armed["ok"], armed
    assert real["fake"].send_requests == []
    started = host._do_start(candidate_id=candidate, confirmation=START_PHRASE, account_confirmation=confirmation)
    assert started["ok"], started
    runtime = host._runtime
    assert runtime.expected_account_mode == account_mode
    assert runtime.venue.leg is host._leg
    assert runtime.venue.leg.router.live is host.adapter
    assert runtime.snapshot()["account_identity"]["account_mode"] == account_mode
    stopped = host._do_stop()
    assert stopped["ok"] and real["fake"].send_requests == []


def test_legacy_real_only_leg_cannot_be_weakened_by_a_demo_binding(real):
    real["fake"].trade_mode = 0
    real["harness"].adapter.refresh_account()
    real["leg"].require_real_account = True
    real["leg"].required_account_mode = "DEMO"
    assert REAL_ACCOUNT_REQUIRED in {blocker.reason for blocker in real["leg"].blockers()}
    assert real["fake"].send_requests == []


def test_recovery_refusal_and_stop_preserve_corrupt_state_bytes(real):
    store = JsonRunStateStore(real["tmp"] / "corrupt-stop.json")
    store.path.write_bytes(b'{damaged and preserve exactly')
    before = store.path.read_bytes()
    run, _, _ = build_run(real, state_store=store)
    assert not run.recover()["ok"]
    assert not run.start()["ok"] and run.state == RECOVERY_REQUIRED
    assert not run.arm()["ok"] and run.state == RECOVERY_REQUIRED
    assert run.flatten() == []
    result = run.stop(close_positions=True)
    assert result["ok"] and result["state"] == RECOVERY_REQUIRED
    assert result["state_preserved"] and not result["closed"]
    assert store.path.read_bytes() == before
    assert not real["fake"].send_requests


def test_ambiguous_entry_query_and_stop_leave_original_state_unchanged(real):
    store = JsonRunStateStore(real["tmp"] / "unknown-entry-preserved.json")
    run, venue, _ = build_run(real, state_store=store)
    run._pending_submission = {"request_id": "entry-original"}
    run.state = "RUNNING"
    store.save(run.snapshot())
    before = store.path.read_bytes()
    venue.query = lambda _request_id: None
    assert not run.reconcile_submission()["resolved"]
    assert store.path.read_bytes() == before
    assert run.stop()["state"] == RECOVERY_REQUIRED
    assert store.path.read_bytes() == before
    assert not real["fake"].send_requests


@pytest.mark.parametrize("status,simulated,mismatched_id", [
    (UNKNOWN_OUTCOME, False, False),
    ("rejected", True, False),
    ("rejected", False, True),
])
def test_ambiguous_close_reconcile_preserves_original_state(real, status, simulated, mismatched_id):
    store = JsonRunStateStore(real["tmp"] / "unknown-close-preserved.json")
    run, venue, _ = build_run(real, state_store=store)
    position = run.book.open_position(
        position_id="31415", symbol=PAIR, side="BUY", lots=Decimal("0.10"),
        entry_price=Decimal("1.10000"), stop_price=Decimal("1.09500"),
        target_price=None, now_ms=int(START.timestamp() * 1000), request_id="entry-31415",
    )
    assert run._record_position_risk(position)
    run.book._pending_closes["31415"] = {
        "position_id": "31415", "request_id": "close-original", "requested_lots": "0.10"
    }
    run.state = RECOVERY_REQUIRED
    store.save(run.snapshot())
    before = store.path.read_bytes()
    queried = []
    def query(request_id):
        queried.append(request_id)
        return Fill(request_id="wrong-request" if mismatched_id else request_id, status=status,
                    filled_lots=Decimal(0), requested_lots=Decimal("0.10"), price=None,
                    spread_points=Decimal(0), slippage_points=Decimal(0), latency_ms=0,
                    ticket="31415", simulated=simulated)
    venue.query = query
    result = run.reconcile_close(position_id="31415")
    assert not result["resolved"] and queried == ["close-original"]
    assert store.path.read_bytes() == before
    assert run.book.get("31415") is not None
    assert not real["fake"].send_requests


def test_recovery_required_tick_never_manages_or_persists(real):
    run, venue, _ = build_run(real)
    position = run.book.open_position(
        position_id="31415", symbol=PAIR, side="BUY", lots=Decimal("0.10"),
        entry_price=Decimal("1.10000"), stop_price=Decimal("1.09500"),
        target_price=None, now_ms=int(START.timestamp() * 1000), request_id="entry-31415",
    )
    assert run._record_position_risk(position)
    run.state = RECOVERY_REQUIRED
    run.state_store.save(run.snapshot())
    before = run.state_store.path.read_bytes()
    def forbidden(**kwargs):
        raise AssertionError("recovery must not reach management")
    run.book.on_tick = forbidden
    outcome = run.on_tick(**tick_kwargs(real))
    assert outcome["blocked"] == "recovery_required"
    assert run.state_store.path.read_bytes() == before
    assert not real["fake"].send_requests


@pytest.mark.parametrize("raw", [
    b'{"schema":"one","schema":"two"}',
    b'{"schema":"one","equity":NaN}',
    b'{"schema":"one","equity":Infinity}',
])
def test_invalid_json_state_is_preserved_and_cannot_arm(real, raw):
    store = JsonRunStateStore(real["tmp"] / "invalid-json-state.json")
    store.path.write_bytes(raw)
    run, _, _ = build_run(real, state_store=store)
    assert not run.recover()["ok"]
    assert not run.start()["ok"]
    assert run.stop()["state"] == RECOVERY_REQUIRED
    assert store.path.read_bytes() == raw
    assert not real["fake"].send_requests


@pytest.mark.parametrize("removed", ["target_price", "previous_stop_price", "previous_target_price"])
def test_incomplete_pending_protection_is_rejected_and_preserved(real, removed):
    store = JsonRunStateStore(real["tmp"] / "incomplete-protection.json")
    run, venue, _ = build_run(real, state_store=store)
    fill, position, _ = _seed_guarded_scalper_position(real, run, venue)
    real["fake"].protection_script = [None]
    run.book.modify_protection(position_id=fill.ticket,
        stop_price=position.stop_price, target_price=Decimal("1.11500"),
        now_ms=int(START.timestamp() * 1000), request_id="protect-original")
    state = store.load()
    del state["position_book"]["pending_protection_modifications"][0][removed]
    store.save(state)
    before = store.path.read_bytes()
    recovered, _, _ = build_run(real, state_store=store)
    result = recovered.recover()
    assert not result["ok"] and recovered.state == RECOVERY_REQUIRED
    assert "position-book state is not restorable" in result["recovery"]["detail"]
    assert store.path.read_bytes() == before
    assert len(real["fake"].protection_requests) == 1


@pytest.mark.parametrize("field,value", [
    ("request_id", "different-request"), ("signal_fingerprint", "different-strategy"),
    ("source_time", "not-a-time"), ("lots", "NaN"), ("tick_id", ""),
    ("risk_account", "0"), ("target_price", "NaN"), ("symbol", "OTHER"),
    ("requested_at_ms", True),
])
def test_damaged_pending_entry_is_not_adopted_or_rewritten(real, field, value):
    store = JsonRunStateStore(real["tmp"] / "damaged-entry.json")
    run, _, _ = build_run(real, state_store=store)
    fingerprint = run.admitted.strategy.fingerprint
    tick_id = "tick-recovery-1"
    run._pending_submission = {
        "request_id": f"{fingerprint[:12]}:{tick_id[:12]}:BUY", "symbol": PAIR,
        "side": "BUY", "lots": "0.10", "signal_fingerprint": fingerprint,
        "tick_id": tick_id, "source_time": START.isoformat(), "bid": "1.09990",
        "ask": "1.10010", "stop_price": "1.09500", "target_price": "1.11000",
        "requested_at_ms": int(START.timestamp() * 1000), "risk_account": "50",
        "notional_account": "11000",
    }
    run._pending_submission[field] = value
    store.save(run.snapshot())
    before = store.path.read_bytes()
    recovered, venue, _ = build_run(real, state_store=store)
    venue.query = lambda _request_id: pytest.fail("damaged intent must not be queried")
    result = recovered.recover()
    assert not result["ok"] and recovered.state == RECOVERY_REQUIRED
    assert "submission intent is incomplete or inconsistent" in result["recovery"]["detail"]
    assert recovered._pending_submission is None
    assert store.path.read_bytes() == before
    assert not real["fake"].send_requests


def test_pending_close_without_original_id_is_not_adopted_or_rewritten(real):
    store = JsonRunStateStore(real["tmp"] / "damaged-close.json")
    run, _, _ = build_run(real, state_store=store)
    position = run.book.open_position(position_id="31415", symbol=PAIR, side="BUY",
        lots=Decimal("0.10"), entry_price=Decimal("1.10000"),
        stop_price=Decimal("1.09500"), target_price=None,
        now_ms=int(START.timestamp() * 1000), request_id="entry-31415")
    assert run._record_position_risk(position)
    run.book._pending_closes["31415"] = {"request_id": "", "requested_lots": "0.10"}
    store.save(run.snapshot())
    before = store.path.read_bytes()
    recovered, _, _ = build_run(real, state_store=store)
    result = recovered.recover()
    assert not result["ok"] and recovered.state == RECOVERY_REQUIRED
    assert "position-book state is not restorable" in result["recovery"]["detail"]
    assert store.path.read_bytes() == before
    assert not real["fake"].send_requests


@pytest.mark.parametrize("overrides", [
    {"submitted": False},
    {"classification": "REJECTED"},
    {"state": "REJECTED", "unknown": True, "volume_filled": "0"},
    {"state": "REJECTED", "unknown": False, "volume_filled": "0.1"},
])
def test_guarded_query_does_not_clear_contradictory_unknown_outcome(overrides):
    record = {"request_id": "request-original", "state": "UNRESOLVED", **overrides}
    leg = SimpleNamespace(repository=SimpleNamespace(get_order=lambda _id: record))
    venue = BrokerVenue(leg=leg, plan_factory=lambda **kw: None, query_guard=lambda _row: True)
    result = venue.query("request-original")
    assert result is None or result.status == UNKNOWN_OUTCOME
    assert venue.has_unresolved
    assert venue.record.submissions == []


def test_guarded_query_refuses_nested_outcome_relabeling_durable_request():
    record = {"request_id": "original", "state": "UNRESOLVED",
              "leg_outcome": {"request_id": "original", "state": "REJECTED"}}
    leg = SimpleNamespace(repository=SimpleNamespace(get_order=lambda _id: record))
    venue = BrokerVenue(leg=leg, plan_factory=lambda **kw: None, query_guard=lambda _row: True)
    assert venue.query("original") is None
    assert venue.has_unresolved
    assert venue.record.submissions == []


@pytest.mark.parametrize("booked,booking_booked", [(True, False), (False, True), ("true", True)])
def test_guarded_query_requires_durable_booking_consensus(booked, booking_booked):
    record = {"request_id": "original", "state": "FILLED", "volume_filled": "0.1",
              "volume_requested": "0.1", "booked": booked,
              "booking": {"booked": booking_booked, "action": "OPEN", "position_id": "42", "price": "1.1"}}
    leg = SimpleNamespace(repository=SimpleNamespace(get_order=lambda _id: record))
    venue = BrokerVenue(leg=leg, plan_factory=lambda **kw: None, query_guard=lambda _row: True)
    assert venue.query("original").status == UNKNOWN_OUTCOME
    assert venue.has_unresolved
    assert venue.record.submissions == []


def test_guarded_query_accepts_only_consistent_durable_booking():
    booking = {"booked": True, "action": "OPEN", "position_id": "42", "price": "1.1"}
    record = {"request_id": "original", "state": "FILLED", "volume_filled": "0.1",
              "volume_requested": "0.1", "booked": True, "booking": booking,
              "leg_outcome": {"request_id": "original", "state": "FILLED", "submitted": True,
                              "volume_filled": "0.1", "volume_requested": "0.1",
                              "booked": True, "booking": dict(booking)}}
    leg = SimpleNamespace(repository=SimpleNamespace(get_order=lambda _id: record))
    venue = BrokerVenue(leg=leg, plan_factory=lambda **kw: None, query_guard=lambda _row: True)
    fill = venue.query("original")
    assert fill.status == ACCEPTED and fill.ticket == "42"
    record["leg_outcome"]["booking"]["position_id"] = "99"
    assert venue.query("original") is None
    assert venue.has_unresolved
    assert venue.record.submissions == []


def test_guarded_fake_broker_booking_is_queryable_without_resending(real):
    venue = _venue(real)
    quote = real["harness"].adapter.quote(real["platform"].spec)
    first = venue.submit(_order("fake-query-booked"), quote)
    assert first.status == ACCEPTED
    before = len(real["fake"].send_requests)
    queried = venue.query("fake-query-booked")
    assert queried is not None and queried.status == ACCEPTED, queried
    assert queried.request_id == first.request_id and queried.ticket == first.ticket
    assert len(real["fake"].send_requests) == before


def test_run_identity_persists_and_mismatched_schema_is_never_adopted(real):
    run, _, _ = build_run(real)
    saved = run.snapshot()
    assert len(saved["run_id"]) == 32
    fresh, _, _ = build_run(real)
    assert fresh.run_id != saved["run_id"]
    result = fresh.recover(state=saved)
    assert result["ok"] is True
    assert fresh.run_id == saved["run_id"]
    invalid = dict(saved, run_id="bad-identity")
    other, _, _ = build_run(real)
    result = other.recover(state=invalid)
    assert not result["ok"] and "run identity" in result["recovery"]["detail"]


def test_journal_fsync_precedes_fake_mt5_entry_and_records_response(real):
    from axiom_forex.scalper.mutation_journal import MutationJournal
    journal = MutationJournal(real["tmp"] / "mutation.db", create=True)
    run, _, _ = build_run(real)
    run.mutation_journal = journal
    observed = []
    def before_send(_request):
        records = journal.unresolved()
        observed.append(records)
        assert len(records) == 1
        assert records[0]["action"] == "entry"
        assert records[0]["identity"]["run_id"] == run.run_id
        assert records[0]["result"] is None
        assert run.state_store.load()["pending_submission"]["request_id"] == records[0]["request_id"]
    real["fake"].on_send = before_send
    assert run.start()["ok"]
    warm(run, real, count=60)
    assert len(observed) == 1 and len(real["fake"].send_requests) == 1
    pending = journal.unresolved()
    assert len(pending) == 1 and pending[0]["result"] is not None
    assert pending[0]["request_id"].startswith("entry-")
    journal.close()


def test_fake_sltp_journal_persists_intent_response_and_readback(real):
    from axiom_forex.scalper.mutation_journal import MutationJournal
    run, venue, _ = build_run(real)
    fill, position, _ = _seed_guarded_scalper_position(real, run, venue)
    journal = MutationJournal(real["tmp"] / "protection-mutation.db", create=True)
    run.mutation_journal = journal
    before_send = []
    real["fake"].on_send = lambda _request: before_send.append(journal.unresolved())
    action = run.book.modify_protection(position_id=fill.ticket,
        stop_price=position.stop_price, target_price=Decimal("1.11500"),
        now_ms=int(START.timestamp() * 1000) + 100, reason="operator_target_update")
    assert action.kind == MODIFY_PROTECTION
    assert len(before_send) == 1 and len(before_send[0]) == 1
    record = journal.list_all()[0]
    assert record["resolution"]["reason"] == "durable_sltp_receipt_and_broker_protection_match"
    assert record["action"] == "protection"
    assert record["intent"]["position_id"] == fill.ticket
    assert record["intent"]["previous_target_price"] is None
    assert record["intent"]["target_price"] == "1.11500"
    assert record["result"]["receipts"][0]["state"] == "CONFIRMED"
    assert record["readback"]["readable"] is True
    assert len(real["fake"].protection_requests) == 1
    journal.close()


def test_journal_failure_blocks_protection_before_broker_mutation(real):
    class BrokenJournal:
        def prepare(self, **_kwargs):
            raise OSError("disk unavailable")
    run, venue, _ = build_run(real)
    fill, position, _ = _seed_guarded_scalper_position(real, run, venue)
    run.mutation_journal = BrokenJournal()
    action = run.book.modify_protection(position_id=fill.ticket,
        stop_price=position.stop_price, target_price=Decimal("1.11500"),
        now_ms=int(START.timestamp() * 1000) + 100)
    assert action.kind == "hold" and run.state == RECOVERY_REQUIRED
    assert real["fake"].protection_requests == []
    assert run.state_store.load()["position_book"]["pending_protection_modifications"]


def test_fake_close_journal_is_written_before_send_and_unknown_never_resends(real):
    from axiom_forex.scalper.mutation_journal import MutationJournal
    run, venue, _ = build_run(real)
    entry, position, _ = _seed_guarded_scalper_position(real, run, venue)
    journal = MutationJournal(real["tmp"] / "close-mutation.db", create=True)
    run.mutation_journal = journal
    calls = []
    venue.close_request_id = lambda _ticket: "close-exact-request"
    def unknown_close(**kwargs):
        calls.append(kwargs)
        row = journal.unresolved()[0]
        assert row["action"] == "close"
        assert row["intent"]["position_id"] == entry.ticket
        assert row["intent"]["prior_position"]["lots"] == str(position.lots)
        assert row["result"] is None
        return Fill(request_id="close-exact-request", status=UNKNOWN_OUTCOME,
                    filled_lots=Decimal(0), requested_lots=position.lots, price=None,
                    spread_points=Decimal(0), slippage_points=Decimal(0), latency_ms=0,
                    ticket=entry.ticket, simulated=False)
    venue.close = unknown_close
    action = run.book._close(position, position.entry_price, int(START.timestamp() * 1000),
                             "test_close", "test", quote=None)
    assert action.kind == "hold"
    row = journal.unresolved()[0]
    assert row["result"]["observation"]["fill"]["status"] == UNKNOWN_OUTCOME
    assert row["readback"]["readable"] is True
    run.book._close(position, position.entry_price, int(START.timestamp() * 1000) + 1,
                    "test_close", "test", quote=None)
    assert len(calls) == 1
    journal.close()


def test_unknown_entry_restart_loads_recovery_only_without_arm_or_resend(real):
    from axiom_forex.scalper.mutation_journal import MutationJournal
    path = real["tmp"] / "restart-entry-mutation.db"
    journal = MutationJournal(path, create=True)
    run, _, _ = build_run(real)
    run.mutation_journal = journal
    real["fake"].send_script.insert(0, None)
    assert run.start()["ok"]
    warm(run, real, count=60)
    assert len(real["fake"].send_requests) == 1
    pending = journal.unresolved()[0]
    before = run.state_store.path.read_bytes()
    # A new process has no in-memory arm. The recovery-only instance performs
    # reads and cannot arm, start, manage, close or submit.
    real["harness"].adapter.disarm(reason="fake_restart")
    fresh, _, _ = build_run(real, state_store=run.state_store)
    fresh.mutation_journal = journal
    fresh.mode = SimpleNamespace(mode="PAPER", live_effective=False)
    result = fresh.recover(reconciliation_only=True)
    assert result.get("prepared") is True, result
    assert fresh._pending_submission["request_id"] == pending["request_id"]
    assert fresh.arm()["ok"] is False
    assert fresh.start()["ok"] is False
    assert fresh.on_tick(**tick_kwargs(real))["blocked"] == "recovery_required"
    assert fresh.stop(close_positions=True)["closed"] == []
    # Even an internal transition to ARMED cannot enable flatten on a
    # reconciliation-only runtime.
    fresh.state = "ARMED"
    assert fresh.flatten() == []
    fresh.state = RECOVERY_REQUIRED
    assert not fresh.reconcile_submission()["resolved"]
    assert len(real["fake"].send_requests) == 1
    assert run.state_store.path.read_bytes() == before
    journal.close()


@pytest.mark.parametrize("answer", ["success", "refusal", "unknown"])
def test_sltp_fake_restart_requires_receipt_and_exact_readback(real, answer):
    from axiom_forex.scalper.mutation_journal import MutationJournal
    run, venue, _ = build_run(real)
    fill, position, read_protection = _seed_guarded_scalper_position(real, run, venue)
    journal = MutationJournal(real["tmp"] / "sltp-restart.db", create=True)
    run.mutation_journal = journal
    if answer == "success":
        # The successful synchronous receipt is durable, while first read-back
        # fails; it can only settle after the terminal exposes the exact pair.
        real["fake"].on_send = lambda _request: setattr(real["fake"], "fail_positions_get", True)
    elif answer == "refusal":
        real["fake"].protection_script = [{"retcode": 10016, "comment": "invalid stops"}]
    else:
        real["fake"].protection_script = [None]
    action = run.book.modify_protection(position_id=fill.ticket, stop_price=position.stop_price,
        target_price=Decimal("1.11500"), now_ms=int(START.timestamp() * 1000) + 100,
        reason="operator_target_update")
    assert action.kind == "hold"
    real["fake"].fail_positions_get = False
    real["fake"].on_send = None
    request_id = journal.unresolved()[0]["request_id"]
    sends = len(real["fake"].send_requests)
    real["harness"].adapter.disarm(reason="fake_restart")
    fresh, fresh_venue, _ = build_run(real, state_store=run.state_store)
    fresh.mutation_journal = journal
    fresh_venue._protection_reader = read_protection
    fresh.mode = SimpleNamespace(mode="PAPER", live_effective=False)
    loaded = fresh.recover(reconciliation_only=True)
    assert loaded.get("prepared") is True, loaded
    result = fresh.reconcile_protection(request_id=request_id)
    if answer == "unknown":
        assert not result["resolved"] and journal.unresolved()[0]["request_id"] == request_id
    else:
        assert result["resolved"] is True, result
        assert not journal.unresolved()
        expected = Decimal("1.11500") if answer == "success" else position.target_price
        assert fresh.book.get(fill.ticket).target_price == expected
    assert fresh.start()["ok"] is False
    assert len(real["fake"].send_requests) == sends
    journal.close()


def test_unknown_close_restart_remains_latched_and_never_resends(real):
    from axiom_forex.scalper.mutation_journal import MutationJournal
    run, venue, _ = build_run(real)
    entry, position, read_protection = _seed_guarded_scalper_position(real, run, venue)
    journal = MutationJournal(real["tmp"] / "close-restart.db", create=True)
    run.mutation_journal = journal
    venue.close_request_id = lambda _ticket: "close-exact-restart"
    calls = []
    def unknown_close(**kwargs):
        calls.append(kwargs)
        return Fill(request_id="close-exact-restart", status=UNKNOWN_OUTCOME,
                    filled_lots=Decimal(0), requested_lots=position.lots, price=None,
                    spread_points=Decimal(0), slippage_points=Decimal(0), latency_ms=0,
                    ticket=entry.ticket, simulated=False)
    venue.close = unknown_close
    run.book._close(position, position.entry_price, int(START.timestamp() * 1000),
                    "test_close", "test", quote=None)
    before = run.state_store.path.read_bytes()
    real["harness"].adapter.disarm(reason="fake_restart")
    fresh, fresh_venue, _ = build_run(real, state_store=run.state_store)
    fresh.mutation_journal = journal
    fresh_venue._protection_reader = read_protection
    fresh.mode = SimpleNamespace(mode="PAPER", live_effective=False)
    result = fresh.recover(reconciliation_only=True)
    assert result.get("prepared") is True, result
    result = fresh.reconcile_close(position_id=entry.ticket)
    assert result["resolved"] is False
    assert fresh.start()["ok"] is False
    assert fresh.stop(close_positions=True)["closed"] == []
    assert journal.unresolved()[0]["request_id"] == "close-exact-restart"
    assert before == run.state_store.path.read_bytes()
    assert len(calls) == 1
    journal.close()


def test_restart_refuses_empty_provider_ledger_even_with_matching_broker(real):
    """Broker + managed book alone cannot prove the provider ledger survived."""
    run, venue, _ = build_run(real)
    _fill, _position, reader = _seed_guarded_scalper_position(real, run, venue)
    before = run.state_store.path.read_bytes()
    # A workstation rebuild makes a new, empty provider ledger.
    real["leg"].portfolio = real["platform"].portfolio.empty_clone()
    fresh, fresh_venue, _ = build_run(real, state_store=run.state_store)
    fresh_venue._protection_reader = reader
    result = fresh.recover()
    assert not result["ok"], result
    assert "ledger" in result["recovery"]["detail"].lower()
    assert run.state_store.path.read_bytes() == before
    assert len(real["fake"].send_requests) == 1


def test_scalper_booking_and_provider_ledger_are_atomic_and_bound(real, monkeypatch):
    from axiom_forex.paper_runner.live import LiveOutcome
    leg = real["leg"]
    leg.strategy_id = "ax-scalper-live:test"
    identity = {"run_id": "run-atomic", "account_fingerprint": derived_fingerprint(),
                "account_mode": "REAL", "symbol": "EURUSD.pro"}
    leg.scalper_ledger_identity = lambda: dict(identity)
    repo = real["repository"]
    repo.save_orders([{"request_id": "atomic", "state": "FILLED", "volume_filled": "0.1"}])
    booking = {"booked": True, "action": "OPEN", "position_id": "mt5:42", "price": "1.1"}
    outcome = LiveOutcome(submitted=True, request_id="atomic", state="FILLED", booked=True,
                          booking=booking, volume_requested=Decimal("0.1"),
                          volume_filled=Decimal("0.1"), price=Decimal("1.1"))
    save_snapshot = repo.save_snapshot
    monkeypatch.setattr(repo, "save_snapshot", lambda *_args, **_kw: (_ for _ in ()).throw(OSError("disk")))
    assert not leg._persist_booking_result(outcome)
    assert repo.get_order("atomic").get("booked") is not True
    assert not repo.list_snapshots(kind="ax-scalper-live-ledger")
    monkeypatch.setattr(repo, "save_snapshot", save_snapshot)
    assert leg._persist_booking_result(outcome)
    rows = repo.list_snapshots(kind="ax-scalper-live-ledger")
    assert len(rows) == 1 and rows[0]["identity"] == identity
    assert rows[0]["request_id"] == "atomic" and repo.get_order("atomic")["booking"] == booking
    assert not leg._persist_booking_result(outcome)  # idempotency, no duplicate booking
    assert not real["fake"].send_requests


@pytest.mark.parametrize("damage", ["account", "run_id", "strategy", "guardian"])
def test_unresolved_entry_restart_identity_damage_preserves_all_records(real, damage):
    from axiom_forex.scalper.mutation_journal import MutationJournal
    journal = MutationJournal(real["tmp"] / f"identity-{damage}.db", create=True)
    run, _, _ = build_run(real)
    run.mutation_journal = journal
    real["fake"].send_script.insert(0, None)
    assert run.start()["ok"]
    warm(run, real, count=60)
    saved = run.state_store.load()
    if damage == "account":
        saved["account_identity"]["server"] = "different-server"
    elif damage == "run_id":
        saved["run_id"] = "f" * 32
    elif damage == "strategy":
        saved["strategy_fingerprint"] = "different-strategy"
    else:
        saved["guardian"] = None
    run.state_store.save(saved)
    before = run.state_store.path.read_bytes()
    pending = journal.unresolved()
    real["harness"].adapter.disarm(reason="fake_restart")
    fresh, _, _ = build_run(real, state_store=run.state_store)
    fresh.mutation_journal = journal
    result = fresh.recover(reconciliation_only=True)
    assert not result.get("prepared"), result
    assert fresh.arm()["ok"] is False and fresh.start()["ok"] is False
    assert run.state_store.path.read_bytes() == before
    assert journal.unresolved() == pending
    assert len(real["fake"].send_requests) == 1
    journal.close()


def test_history_fill_books_ledger_atomically_once_without_broker_send(real, monkeypatch):
    from axiom_forex.scalper.history_recovery import inspect_exact_history
    from tests.fake_mt5 import make_history_deal, make_position
    from tests.test_mt5_adapter import _unresolved_record, AT
    leg, repo, fake = real["leg"], real["repository"], real["fake"]
    leg.strategy_id = "ax-scalper-live:test"
    fingerprint = derived_fingerprint()
    leg.portfolio.account = replace(leg.portfolio.account, account_id=fingerprint)
    identity = {"run_id": "run-history", "account_fingerprint": fingerprint,
                "account_server": SERVER, "account_currency": "USD", "account_mode": "REAL",
                "symbol": "EURUSD.pro"}
    leg.scalper_ledger_identity = lambda: dict(identity)
    order = _unresolved_record(request_id="history-entry", order_ticket=7001,
        strategy_id=leg.strategy_id, account_mode="REAL", account_fingerprint=fingerprint, sent_at=START)
    row = {"request_id": "history-entry", "action": "entry", "identity": identity,
           "intent": {"request_id": "history-entry", "side": "BUY", "lots": "0.1",
                      "stop_price": "1.09", "target_price": None}}
    fake.history_deals = [make_history_deal(ticket=9001, order=7001, position_id=9201,
        symbol="EURUSD.pro", side="BUY", entry=0, volume=0.1, price=1.1,
        magic=26081001, comment="", time=START)]
    fake.positions = [make_position(ticket=9101, identifier=9201, symbol="EURUSD.pro", side="BUY",
        volume=0.1, price_open=1.1, magic=26081001, sl=1.09)]
    repo.save_orders([order])
    stored = repo.get_order("history-entry")
    proof = inspect_exact_history(adapter=real["harness"].adapter, order=stored, journal=row,
                                  account_matches=lambda: True)
    assert proof.get("resolved"), proof
    save_snapshot = repo.save_snapshot
    monkeypatch.setattr(repo, "save_snapshot", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk")))
    assert not leg.commit_scalper_history(record=stored, proof=proof, account_matches=lambda: True)
    assert repo.get_order("history-entry") == stored
    assert not leg.portfolio.positions and leg.scalper_ledger_head == ""
    monkeypatch.setattr(repo, "save_snapshot", save_snapshot)
    checks = iter([True, True, False])
    assert not leg.commit_scalper_history(record=stored, proof=proof, account_matches=lambda: next(checks))
    assert repo.get_order("history-entry") == stored
    assert not leg.portfolio.positions and leg.scalper_ledger_head == ""
    assert leg.commit_scalper_history(record=stored, proof=proof, account_matches=lambda: True), leg.booking_failures
    booked = repo.get_order("history-entry")
    assert booked["booked"] and booked["booking"]["position_id"] == "mt5:9101"
    assert "mt5:9101" in leg.portfolio.positions
    assert repo.get_snapshot("ax-scalper-live-ledger:run-history")["request_id"] == "history-entry"
    assert not leg.commit_scalper_history(record=stored, proof=proof, account_matches=lambda: True)
    assert not fake.send_requests


def test_fake_timeout_entry_restarts_and_reconciles_one_exact_history_without_resend(real):
    from axiom_forex.scalper.mutation_journal import MutationJournal
    from axiom_forex.scalper.history_recovery import inspect_exact_history
    from tests.fake_mt5 import make_history_deal, make_position
    journal = MutationJournal(real["tmp"] / "entry-history-restart.db", create=True)
    run, venue, _ = build_run(real)
    run.mutation_journal = journal
    leg, fake, repo = real["leg"], real["fake"], real["repository"]
    leg.strategy_id = "ax-scalper-live:test"
    leg.portfolio.account = replace(leg.portfolio.account, account_id=derived_fingerprint())
    venue._plan_factory.strategy_id = leg.strategy_id
    leg.scalper_ledger_identity = lambda: run._journal_identity(PAIR)
    fake.send_script.insert(0, {"retcode": 10012, "order": 7001, "volume": 0})
    assert run.start()["ok"]
    warm(run, real, count=60)
    assert len(fake.send_requests) == 1
    row = journal.unresolved()[0]
    request_id = row["request_id"]
    stored = repo.get_order(request_id)
    assert stored["order_ticket"] == 7001 and stored["state"] == "UNRESOLVED"
    intent = row["intent"]
    fill_price = Decimal(str(intent["ask"]))
    fake.history_deals = [make_history_deal(ticket=9001, order=7001, position_id=9201,
        symbol="EURUSD.pro", side=intent["side"], entry=0, volume=float(intent["lots"]),
        price=float(fill_price), magic=stored["magic"], comment="", time=START)]
    fake.positions = [make_position(ticket=9101, identifier=9201, symbol="EURUSD.pro", side=intent["side"],
        volume=float(intent["lots"]), price_open=float(fill_price), magic=stored["magic"],
        sl=float(intent["stop_price"]), tp=None if intent["target_price"] is None else float(intent["target_price"]))]
    def protection(_ticket):
        p = fake.positions[0]
        return Decimal(str(p["sl"])), None if not p["tp"] else Decimal(str(p["tp"]))
    real["harness"].adapter.disarm(reason="fake_restart")
    fresh, fresh_venue, _ = build_run(real, state_store=run.state_store)
    fresh.mutation_journal = journal
    fresh.mode = SimpleNamespace(mode="PAPER", live_effective=False)
    fresh_venue._protection_reader = protection
    fresh_venue._query_guard = lambda _record: True
    fresh.run_id = run.run_id
    # Exercise the workstation's per-request operation over fake MT5. The
    # unresolved request is rendered before it can be queried, and arm/start
    # remain blocked until exact reconciliation completes.
    from tests.test_scalper_live_workstation import _host
    from axiom_forex.paper_runner.scalper_live_host import ARM_PHRASE, START_PHRASE
    from axiom_forex.workstation.ui.pages.live import autonomous_scalper_section
    host_path = real["tmp"] / "host"
    host_path.mkdir()
    host, _, _ = _host(host_path)
    host.adapter = real["harness"].adapter
    host.state_store = run.state_store
    host.journal_path = journal.path
    host._journal, host._leg, host._runtime = journal, leg, fresh
    host._expected, host._bound_mode = fresh.expected_account, "REAL"
    host._strategy_id, host._state = leg.strategy_id, RECOVERY_REQUIRED
    fresh_venue.history_preview = lambda rid: host._inspect_history_request(
        fresh, journal, leg, expected=fresh.expected_account, symbol="EURUSD.pro",
        strategy_id=leg.strategy_id, request_id=rid)
    result = fresh.recover(reconciliation_only=True)
    assert result.get("prepared"), result
    saved = host._saved_recovery_view()
    html = autonomous_scalper_section({"scalper_autonomous": {"saved_recovery": saved}}, "test-nonce")
    assert request_id in html and "ax-scalper-live-reconcile-request" in html
    assert not host._do_arm(candidate_id="unused", confirmation=ARM_PHRASE,
                             account_confirmation=f"REAL {derived_fingerprint()}")["ok"]
    assert not host._do_start(candidate_id="unused", confirmation=START_PHRASE,
                               account_confirmation=f"REAL {derived_fingerprint()}")["ok"]
    # An uncertain in-process submission can leave the owner PAUSED rather
    # than already in recovery-only mode. Explicit reconciliation safely
    # transitions that same owner to read-only without resending.
    fresh._reconciliation_only = False
    fresh.state = "PAUSED"
    resumed = host._prepare_recovery_runtime()
    assert resumed.get("prepared") and fresh._reconciliation_only
    result = host._do_reconcile_request(request_id=request_id)
    assert result["resolved"], (result, leg.booking_failures)
    assert fresh.book.get("9101") is not None
    assert len(fake.send_requests) == 1
    assert host._runtime is None and host._state == "STOPPED"


@pytest.mark.parametrize("partial", [False, True])
def test_fake_timeout_close_restarts_and_reconciles_exact_history_without_resend(real, partial):
    from axiom_forex.scalper.mutation_journal import MutationJournal
    from axiom_forex.scalper.history_recovery import inspect_exact_history
    from tests.fake_mt5 import make_history_deal
    run, venue, _ = build_run(real)
    entry, position, protection = _seed_guarded_scalper_position(real, run, venue)
    journal = MutationJournal(real["tmp"] / "close-history-restart.db", create=True)
    run.mutation_journal = journal
    leg, fake, repo = real["leg"], real["fake"], real["repository"]
    leg.strategy_id = "ax-scalper-live:test"
    bound_account = replace(leg.portfolio.account, account_id=derived_fingerprint())
    leg.portfolio.account = bound_account
    real["platform"].context_factory = replace(real["platform"].context_factory, account=bound_account)
    venue._plan_factory.strategy_id = leg.strategy_id
    leg.scalper_ledger_identity = lambda: run._journal_identity(PAIR)
    fake.send_script.insert(0, {"retcode": 10012, "order": 7002, "volume": 0})
    from axiom_forex.scalper.ticks import Tick
    quote = Tick(symbol=PAIR, bid=Decimal("1.10000"), ask=Decimal("1.10020"),
                 source_time=START, received_at=START, sequence=1, source="fake", ingest_latency_ms=0)
    requested = position.lots / 2 if partial else position.lots
    action = run.book._close(position, position.entry_price, int(START.timestamp() * 1000),
                             "operator_close", "test", quote=quote, quantity=requested)
    assert action.kind == "hold"
    assert len(fake.send_requests) == 2, (action, venue.record.closes, venue.record.refusals, leg.last_outcome)
    row = journal.unresolved()[0]
    request_id = row["request_id"]
    stored = repo.get_order(request_id)
    assert stored["order_ticket"] == 7002 and stored["state"] == "UNRESOLVED"
    pid = stored["close_position_identifier"]
    fake.history_deals = [make_history_deal(ticket=9002, order=7002, position_id=pid,
        symbol="EURUSD.pro", side="SELL", entry=1, volume=float(requested),
        price=float(position.entry_price), magic=stored["magic"], comment="", time=START)]
    if partial:
        fake.positions[0]["volume"] = float(position.lots - requested)
    else:
        fake.positions = []
    real["harness"].adapter.disarm(reason="fake_restart")
    fresh, fresh_venue, _ = build_run(real, state_store=run.state_store)
    fresh.mutation_journal = journal
    fresh.mode = SimpleNamespace(mode="PAPER", live_effective=False)
    fresh_venue._protection_reader = protection
    fresh_venue._query_guard = lambda _record: True
    fresh.run_id = run.run_id
    loaded = fresh.recover(reconciliation_only=True)
    assert loaded.get("prepared"), loaded
    proof = inspect_exact_history(adapter=real["harness"].adapter, order=stored,
                                  journal=row, account_matches=lambda: True)
    assert proof["resolved"], proof
    assert leg.commit_scalper_history(record=stored, proof=proof, account_matches=lambda: True), leg.booking_failures
    # Crash immediately after ledger+order commit, before the manager snapshot
    # consumes the close. A second restart recognizes that exact committed fill.
    again, again_venue, _ = build_run(real, state_store=run.state_store)
    again.mutation_journal = journal
    again.mode = SimpleNamespace(mode="PAPER", live_effective=False)
    again_venue._protection_reader = protection
    again_venue._query_guard = lambda _record: True
    again.run_id = run.run_id
    loaded_again = again.recover(reconciliation_only=True)
    assert loaded_again.get("prepared"), loaded_again
    fresh = again
    result = fresh.reconcile_close(position_id=entry.ticket)
    assert result["resolved"], result
    assert not journal.unresolved()
    if partial:
        assert fresh.book.get(entry.ticket).lots == position.lots - requested
    else:
        assert fresh.book.get(entry.ticket) is None
    assert len(fake.send_requests) == 2
    journal.close()


def test_fake_timeout_entry_later_broker_refusal_reconciles_without_resend(real):
    from axiom_forex.scalper.mutation_journal import MutationJournal
    from tests.fake_mt5 import make_history_order
    from tests.test_scalper_live_workstation import _host
    journal = MutationJournal(real["tmp"] / "entry-refusal-restart.db", create=True)
    run, venue, _ = build_run(real)
    run.mutation_journal = journal
    leg, fake, repo = real["leg"], real["fake"], real["repository"]
    leg.strategy_id = "ax-scalper-live:test"
    venue._plan_factory.strategy_id = leg.strategy_id
    leg.scalper_ledger_identity = lambda: run._journal_identity(PAIR)
    fake.send_script.insert(0, {"retcode": 10012, "order": 7001, "volume": 0})
    assert run.start()["ok"]
    warm(run, real, count=60)
    row = journal.unresolved()[0]
    request_id = row["request_id"]
    stored = repo.get_order(request_id)
    fake.history_orders = [make_history_order(ticket=7001, symbol="EURUSD.pro", side=row["intent"]["side"],
        volume=float(row["intent"]["lots"]), state=5, magic=stored["magic"], comment="", time=START)]
    real["harness"].adapter.disarm(reason="fake_restart")
    fresh, fresh_venue, _ = build_run(real, state_store=run.state_store)
    fresh.mutation_journal = journal
    fresh.mode = SimpleNamespace(mode="PAPER", live_effective=False)
    fresh_venue._query_guard = lambda _record: True
    fresh.run_id = run.run_id
    host_path = real["tmp"] / "refusal-host"
    host_path.mkdir()
    host, _, _ = _host(host_path)
    host.adapter = real["harness"].adapter
    host.state_store, host.journal_path = run.state_store, journal.path
    host._journal, host._leg, host._runtime = journal, leg, fresh
    host._expected, host._bound_mode = fresh.expected_account, "REAL"
    host._strategy_id, host._state = leg.strategy_id, RECOVERY_REQUIRED
    fresh_venue.history_preview = lambda rid: host._inspect_history_request(
        fresh, journal, leg, expected=fresh.expected_account, symbol="EURUSD.pro",
        strategy_id=leg.strategy_id, request_id=rid)
    loaded = fresh.recover(reconciliation_only=True)
    assert loaded.get("prepared"), loaded
    result = host._do_reconcile_request(request_id=request_id)
    assert result.get("resolved"), result
    assert fresh._pending_submission is None and not fresh.book.open_positions
    assert repo.get_order(request_id)["state"] == "REJECTED"
    assert len(fake.send_requests) == 1 and host._runtime is None


def test_fake_timeout_close_later_refused_preserves_open_position_without_resend(real):
    from axiom_forex.scalper.mutation_journal import MutationJournal
    from axiom_forex.scalper.history_recovery import inspect_exact_history
    from axiom_forex.scalper.ticks import Tick
    from tests.fake_mt5 import make_history_order
    run, venue, _ = build_run(real)
    entry, position, protection = _seed_guarded_scalper_position(real, run, venue)
    journal = MutationJournal(real["tmp"] / "close-refusal-restart.db", create=True)
    run.mutation_journal = journal
    leg, fake, repo = real["leg"], real["fake"], real["repository"]
    leg.strategy_id = "ax-scalper-live:test"
    account = replace(leg.portfolio.account, account_id=derived_fingerprint())
    leg.portfolio.account = account
    real["platform"].context_factory = replace(real["platform"].context_factory, account=account)
    venue._plan_factory.strategy_id = leg.strategy_id
    leg.scalper_ledger_identity = lambda: run._journal_identity(PAIR)
    fake.send_script.insert(0, {"retcode": 10012, "order": 7002, "volume": 0})
    quote = Tick(symbol=PAIR, bid=Decimal("1.10000"), ask=Decimal("1.10020"),
                 source_time=START, received_at=START, sequence=1, source="fake", ingest_latency_ms=0)
    run.book._close(position, position.entry_price, int(START.timestamp() * 1000),
                    "operator_close", "test", quote=quote)
    assert len(fake.send_requests) == 2
    row = journal.unresolved()[0]
    request_id = row["request_id"]
    stored = repo.get_order(request_id)
    fake.history_orders = [make_history_order(ticket=7002, symbol="EURUSD.pro", side="SELL",
        volume=float(position.lots), state=5, magic=stored["magic"], comment="", time=START)]
    real["harness"].adapter.disarm(reason="fake_restart")
    fresh, fresh_venue, _ = build_run(real, state_store=run.state_store)
    fresh.mutation_journal = journal
    fresh.mode = SimpleNamespace(mode="PAPER", live_effective=False)
    fresh_venue._protection_reader = protection
    fresh_venue._query_guard = lambda _record: True
    fresh.run_id = run.run_id
    loaded = fresh.recover(reconciliation_only=True)
    assert loaded.get("prepared"), loaded
    proof = inspect_exact_history(adapter=real["harness"].adapter, order=stored,
                                  journal=row, account_matches=lambda: True)
    assert proof["resolved"] and proof["state"] == "REJECTED", proof
    assert leg.commit_scalper_history(record=stored, proof=proof, account_matches=lambda: True)
    result = fresh.reconcile_close(position_id=entry.ticket)
    assert result["resolved"], result
    assert not journal.unresolved() and not fresh.book.close_blockers
    assert fresh.book.get(entry.ticket) is not None
    assert len(fake.send_requests) == 2
    journal.close()


def test_assembled_host_restart_retry_and_nonce_reconciliation_no_resend(real):
    """Whole host assembly with a fake terminal; never reaches a real broker."""
    from tests.test_scalper_live_workstation import _host
    from tests.fake_mt5 import make_history_deal
    from axiom_forex.scalper.live_account import ExpectedAccount
    from axiom_forex.scalper.signals import default_strategies
    from axiom_forex.workstation.app import WorkstationApp
    from tests.test_workstation_ui import live_wizard
    from axiom_forex.paper_runner.scalper_live_host import ARM_PHRASE, START_PHRASE
    import json

    directory = real["tmp"] / "assembled"
    directory.mkdir()
    host, _, _ = _host(directory)
    settings = json.loads(host.settings_path.read_text())
    settings["risk"]["risk_fraction"] = "0.001"
    settings["risk"]["max_leverage"] = "100"
    settings["risk"]["max_currency_exposure_fraction"] = "1"
    settings["account"] = {"server": SERVER, "login": LOGIN, "currency": "USD",
                           "fingerprint": derived_fingerprint()}
    host.settings_path.write_text(json.dumps(settings))
    host.adapter, host.mode = real["harness"].adapter, real["mode"]
    host.platform_factory = lambda pair: make_platform(real["platform"].setup, real["terminal"], real["repository"])
    host._close_platform = lambda platform: None
    candidate = default_strategies()[0]
    expected = ExpectedAccount(server=SERVER, login=LOGIN, currency="USD", fingerprint=derived_fingerprint())
    host._bound_mode, host._expected = "REAL", expected
    host._candidate_id = candidate.fingerprint
    host._strategy_id = "ax-scalper-live:" + candidate.fingerprint
    host._lifecycle = SimpleNamespace(approve_active=lambda: True)
    prepared = host._prepare_candidate(candidate.fingerprint)
    assert prepared[0], prepared
    evidence, policy = prepared[1][4], prepared[1][5]
    run = host._build_runtime(expected=expected, strategy=candidate, evidence=evidence, policy_version=policy)
    host._runtime = run
    class TestSignal(AlwaysBuy):
        name = candidate.name
        family = candidate.family
        @property
        def fingerprint(self):
            return candidate.fingerprint
        def evaluate(self, context):
            result = super().evaluate(context)
            return replace(result, signal=replace(result.signal, stop_points=Decimal("200"), target_points=Decimal("400")))
    run.admitted = replace(run.admitted, strategy=TestSignal())
    run.clock = lambda: START
    run.core._clock = lambda: START
    host._leg.clock = lambda: START
    fake = real["fake"]
    fake.send_script.insert(0, {"retcode": 10012, "order": 7001, "volume": 0})
    assert run.start()["ok"], run.readiness()
    outcomes = []
    for index in range(60):
        bid = Decimal("1.10000") + Decimal(index) * Decimal("0.00001")
        when = START - timedelta(milliseconds=10 * (60 - index))
        outcomes.append(run.on_tick(symbol="EURUSD.pro", bid=bid, ask=bid + Decimal("0.00020"),
                                    source_time=when, now_ms=int(when.timestamp() * 1000)))
    assert len(fake.send_requests) == 1, repr((run.venue.record.audit_view(), host._leg.last_outcome))
    row = host._journal.unresolved()[0]
    request_id = row["request_id"]
    stored = real["repository"].get_order(request_id)
    real["harness"].adapter.disarm(reason="fake_restart")
    host._journal.close()
    host._run_lock.close()
    host._journal = host._run_lock = host._runtime = None
    # The fake fixture's in-memory database normally belongs to the test
    # thread. Give the host worker the same isolated fake DB, with thread use
    # enabled; production creates its repository inside the owner thread.
    import sqlite3
    repo = real["repository"]
    connection = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    repo.connection.backup(connection)
    repo.connection.close()
    repo.connection = connection
    # Restart assembles a new runtime and portfolio through the real host path.
    from axiom_forex.paper_runner.scalper_live_host import ScalperLiveAutonomousHost
    restarted = ScalperLiveAutonomousHost(session=host.session, adapter=real["harness"].adapter,
        mode=real["mode"], platform_factory=host.platform_factory, admission_host=host.admission_host,
        live_workflow=host.live_workflow, settings_path=host.settings_path,
        state_path=host.state_store.path, poll_seconds=0.1)
    restarted._close_platform = lambda platform: None
    app = WorkstationApp(wizard=live_wizard(), live=SimpleNamespace(
        view=lambda: {"configured": True, "positions": [], "account": {}}, scalper_autonomous=restarted))
    try:
        saved = restarted.view()["saved_recovery"]
        assert any(item["request_id"] == request_id for item in saved["requests"])
        status, _, _ = app.act("ax-scalper-live-reconcile-request", {"nonce": ["wrong"], "request_id": [request_id]})
        assert int(status) == 403
        for name, phrase in [("ax-scalper-live-arm", ARM_PHRASE), ("ax-scalper-live-start", START_PHRASE)]:
            status, _, _ = app.act(name, {"nonce": [app.nonce], "candidate_id": [candidate.fingerprint],
                "confirmation": [phrase], "account_confirmation": ["REAL " + derived_fingerprint()]})
            assert int(status) == 409
        status, _, _ = app.act("ax-scalper-live-reconcile-request", {"nonce": [app.nonce], "request_id": [request_id]})
        assert int(status) == 409 and len(fake.send_requests) == 1
        intent = row["intent"]
        price = Decimal(intent["ask"])
        fake.history_deals = [make_history_deal(ticket=9001, order=7001, position_id=8101,
            symbol="EURUSD.pro", side="BUY", entry=0, volume=float(intent["lots"]),
            price=float(price), magic=stored["magic"], comment="", time=START)]
        fake.positions = [{"ticket": 9101, "identifier": 8101, "symbol": "EURUSD.pro", "type": 0,
            "volume": float(intent["lots"]), "price_open": float(price),
            "sl": float(intent["stop_price"]), "tp": float(intent["target_price"]),
            "magic": stored["magic"], "time": int(START.timestamp())}]
        status, _, message = app.act("ax-scalper-live-reconcile-request", {"nonce": [app.nonce], "request_id": [request_id]})
        assert int(status) == 303, message
        assert not restarted.view()["saved_recovery"]["requests"] and len(fake.send_requests) == 1
    finally:
        restarted.close()


def test_local_pre_submission_venue_refusal_resolves_without_claiming_fill(real):
    from axiom_forex.scalper.mutation_journal import MutationJournal
    run, venue, _ = build_run(real)
    journal = MutationJournal(real["tmp"] / "local-refusal.sqlite3", create=True)
    run.mutation_journal = journal
    venue._stop_requirement = lambda: {"valid": True, "distance_points": 200}
    assert run.start()["ok"]
    outcomes = warm(run, real, count=35)
    reports = [item["execution"] for item in outcomes if item.get("execution")]
    assert reports and all(item["status"] == "blocked" for item in reports)
    assert not real["fake"].send_requests and not journal.unresolved()
    assert all(row["resolution"]["reason"] == "local_pre_submission_refusal" for row in journal.list_all())
    assert not run.book.open_positions and run._pending_submission is None
    journal.close()
