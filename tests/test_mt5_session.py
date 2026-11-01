"""Bounded MT5 session cycle: demo-only enforcement, account mode, redaction.

All runs use the fake ``MetaTrader5`` module and a fake OS vault, so no terminal,
account, credential, or order is touched.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.audit.log import InMemoryAuditLog
from app.common.types import OrderSide
from app.risk.models import ApprovedOrderIntent
from app.risk.pretrade import GateDecision, RejectionReason
from app.universe.specs import CurrencyPairSpec, InstrumentStatus, QuantityUnit
from axiom_forex.brokers.mt5 import (
    DEMO_ONLY,
    SELECTED_ACCOUNT,
    MT5PasswordStore,
    MT5SessionCycle,
    MT5SessionRefused,
    MT5Settings,
    MT5TradingAdapter,
    SessionPolicy,
    run_session,
)
from axiom_forex.config.vault import SecureStorageUnavailable
from axiom_forex.database.repository import ForexRepository
from tests.fake_mt5 import FakeMT5, make_position

AT = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc)
PASSWORD = "S3cret-Terminal-Passw0rd!"
LOGIN = 55500123


@dataclass
class FakeVault:
    values: dict[str, str] | None = None

    def describe(self):
        return {"backend": "fake_os_vault", "secure": True, "plaintext_fallback": False}

    def available(self) -> bool:
        return True

    def read(self, entry: str) -> str | None:
        return (self.values or {}).get(entry)

    def write(self, entry: str, value: str) -> None:
        self.values = self.values or {}
        self.values[entry] = value

    def erase(self, entry: str) -> bool:
        return bool((self.values or {}).pop(entry, None))


def make_spec(**overrides) -> CurrencyPairSpec:
    values = dict(
        pair="EUR/USD",
        pip_size=Decimal("0.0001"),
        price_tick_size=Decimal("0.00001"),
        quote_precision=5,
        contract_size=Decimal("100000"),
        min_quantity=Decimal("0.01"),
        quantity_step=Decimal("0.01"),
        max_quantity=Decimal("100"),
        quantity_unit=QuantityUnit.LOTS,
        margin_multiplier=Decimal("1"),
        correlation_group="EUR",
        trading_calendar_id="CAL-MT5",
        trading_calendar_version="cal-1",
        status=InstrumentStatus.ACTIVE,
        spec_version="spec-mt5-v1",
        source="TEST_BROKER_CATALOG",
        provider_aliases={"mt5": "EURUSD.pro"},
    )
    values.update(overrides)
    return CurrencyPairSpec(**values)


def make_intent(**overrides) -> ApprovedOrderIntent:
    values: dict = dict(
        request_id="req-1",
        pair="EUR/USD",
        side=OrderSide.BUY,
        quantity=Decimal("0.10"),
        quantity_unit="LOTS",
        base_units=Decimal("10000"),
        executable_price=Decimal("1.10020"),
        quote_currency="USD",
        account_currency="USD",
        evaluated_at=AT,
        strategy_id="strat-1",
        spec_version="spec-mt5-v1",
        calendar_signature="cal-1",
        cost_model_version="cost-v1",
        rollover_schedule_version="roll-v1",
        risk_limits_version="risk-v1",
        risk_calculation=None,
    )
    values.update(overrides)
    return ApprovedOrderIntent(**values)


def decision_for(intent: ApprovedOrderIntent, *, allowed: bool = True) -> GateDecision:
    return GateDecision(
        allowed=allowed,
        code=RejectionReason.ALLOWED if allowed else RejectionReason.STALE_QUOTE,
        reason="allowed" if allowed else "stale",
        details={},
        audit_event={"event_id": "decision-1"},
        order_intent=intent if allowed else None,
    )


class FakeGate:
    def __init__(self, decision: GateDecision | None = None, *, raises: bool = False):
        self.decision = decision
        self.raises = raises
        self.calls: list[tuple] = []

    def evaluate(self, request, context):
        self.calls.append((request, context))
        if self.raises:
            raise ValueError("gate blew up")
        return self.decision


APPROVED = SimpleNamespace(approve_active=lambda: True, stage="APPROVED")
NOT_APPROVED = SimpleNamespace(approve_active=lambda: False, stage="TESTING")


def build_adapter(
    *,
    fake: FakeMT5 | None = None,
    lifecycle=APPROVED,
    vault: FakeVault | None = None,
    mode: str = "TRADING_ENABLED",
    repository: ForexRepository | None = None,
):
    fake = fake or FakeMT5()
    # Align the fake's quote clock with the adapter/session clock so the
    # freshness check measures the fake's own age, not a seven-month gap.
    fake.symbols["EURUSD.pro"]["time"] = int(AT.timestamp())
    repository = repository or ForexRepository(":memory:")
    audit = InMemoryAuditLog()
    settings = MT5Settings(
        terminal_path="terminal64.exe",
        server="Broker-Demo",
        account_login=LOGIN,
        mode=mode,
        symbol_suffix=".pro",
        magic=26081001,
        deviation_points=10,
        max_price_drift_points=20,
        protective_stops={"EUR/USD": "200 points"},
    )
    store = MT5PasswordStore(
        settings=settings, vault=vault or FakeVault(), audit_log=audit
    )
    adapter = MT5TradingAdapter(
        settings=settings,
        password_store=store,
        repository=repository,
        audit_log=audit,
        module=fake,
        lifecycle_lookup=lambda strategy_id: lifecycle,
        clock=lambda: AT,
    )
    return adapter, fake, repository, audit


def full_configuration(*, signal=OrderSide.BUY, gate=None, lifecycle=APPROVED):
    intent = make_intent()
    gate = gate if gate is not None else FakeGate(decision_for(intent))
    return dict(
        spec=make_spec(),
        strategy_id="strat-1",
        lifecycle=lifecycle,
        gate=gate,
        request_factory=lambda spec: SimpleNamespace(request_id="req-1"),
        context_factory=lambda at: SimpleNamespace(),
        signal_source=(lambda: signal),
    )


# -- policy ----------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs, reason",
    [
        ({"account_mode_policy": "SOMETIMES"}, "invalid_account_mode_policy"),
        ({"max_orders": 2}, "max_orders_exceeds_cycle_limit"),
        ({"max_orders": -1}, "invalid_max_orders"),
        ({"max_duration_seconds": 0}, "invalid_max_duration"),
    ],
)
def test_policy_validation(kwargs, reason):
    with pytest.raises(MT5SessionRefused) as excinfo:
        SessionPolicy(**kwargs).validate()
    assert excinfo.value.reason == reason


def test_default_policy_uses_the_terminal_selected_account():
    policy = SessionPolicy()
    assert policy.account_mode_policy == SELECTED_ACCOUNT
    assert policy.max_orders == 1
    policy.validate()


def test_demo_only_remains_available_as_an_explicit_choice():
    policy = SessionPolicy(account_mode_policy=DEMO_ONLY)
    assert policy.account_mode_policy == DEMO_ONLY
    policy.validate()


def test_cycle_requires_a_real_adapter():
    with pytest.raises(MT5SessionRefused):
        MT5SessionCycle(object())  # type: ignore[arg-type]


# -- connection and account mode -------------------------------------------


def test_cycle_connects_and_reports_the_account_mode_without_an_order():
    adapter, fake, repository, _ = build_adapter()
    try:
        report = run_session(adapter, policy=SessionPolicy())
        view = report.audit_view()
        assert view["connected"] is True
        assert view["account"]["account_mode"] == "DEMO"
        assert view["account"]["account_mode_confirmed"] is True
        assert view["account"]["login"] == "<redacted>"
        assert view["account"]["balance_reporting"] == "withheld"
        assert view["live_account_involved"] is False
        assert view["order"] is None
        assert view["orders_sent"] == 0
        assert view["submission_attempted"] is False
        assert view["observe_only"] is True
        assert fake.send_requests == []
        assert fake.shutdown_calls >= 1
    finally:
        repository.close()


def test_missing_configuration_is_reported_not_invented():
    adapter, fake, repository, _ = build_adapter()
    try:
        report = run_session(adapter, policy=SessionPolicy())
        blockers = " ".join(report.blockers)
        assert "no_instrument_configured" in blockers
        assert "no_approved_strategy_configured" in blockers
        assert "no_pretrade_gate_configured" in blockers
        assert "no_order_request_factory_configured" in blockers
        assert "no_gate_context_factory_configured" in blockers
        assert "no_signal_source_configured" in blockers
        assert report.orders_sent == 0
        assert adapter.armed is False
        assert fake.send_requests == []
    finally:
        repository.close()


def test_real_account_with_demo_policy_stops_before_arming():
    adapter, fake, repository, _ = build_adapter(fake=FakeMT5(trade_mode=2))
    try:
        report = run_session(
            adapter,
            policy=SessionPolicy(account_mode_policy=DEMO_ONLY),
            **full_configuration(),
        )
        assert report.connected is True
        assert report.account["account_mode"] == "REAL"
        assert report.live_account_involved is True
        assert "account_mode_is_demo" in " ".join(report.blockers)
        assert report.armed_during_cycle is False
        assert report.submission_attempted is False
        assert fake.send_requests == []
        assert adapter.armed is False
    finally:
        repository.close()


def test_unconfirmed_account_mode_is_reported_and_not_a_gate():
    adapter, fake, repository, _ = build_adapter(fake=FakeMT5(trade_mode=9))
    try:
        report = run_session(
            adapter,
            policy=SessionPolicy(account_mode_policy=DEMO_ONLY),
            **full_configuration(),
        )
        assert report.account["account_mode"] == "UNCONFIRMED"
        assert report.account["account_mode_confirmed"] is False
        assert "account_mode_is_demo" in " ".join(report.blockers)
        assert report.submission_attempted is False
        assert fake.send_requests == []
    finally:
        repository.close()


def test_unconfirmed_account_mode_still_reports_under_the_default_policy():
    adapter, fake, repository, _ = build_adapter(fake=FakeMT5(trade_mode=9))
    try:
        report = run_session(adapter, policy=SessionPolicy(), **full_configuration())
        assert report.account["account_mode"] == "UNCONFIRMED"
        # Reported and audited, but neither a blocker nor a reason to refuse.
        assert report.blockers == ()
        assert report.orders_sent == 1
        details = {check.name: check.detail for check in report.checks}
        assert details["account_mode_detected"].startswith("mode=UNCONFIRMED")
        assert any(
            check.name == "account_note" and "unconfirmed" in check.detail
            for check in report.checks
        )
    finally:
        repository.close()


def test_default_policy_routes_a_real_account_through_the_same_workflow():
    adapter, fake, repository, _ = build_adapter(fake=FakeMT5(trade_mode=2))
    try:
        report = run_session(adapter, policy=SessionPolicy(), **full_configuration())
        assert report.connected is True
        assert report.policy == SELECTED_ACCOUNT
        assert report.live_account_involved is True
        assert report.demo_only_enforced is False
        assert report.submission_attempted is True
        assert report.orders_sent == 1
        assert len(fake.send_requests) == 1
        assert report.order["state"] == "FILLED"
        assert report.armed_during_cycle is True
    finally:
        repository.close()


def test_connection_failure_is_reported_without_arming():
    adapter, fake, repository, _ = build_adapter()
    fake.initialize_result = False
    try:
        report = run_session(adapter, policy=SessionPolicy(), **full_configuration())
        assert report.connected is False
        assert "terminal_connection_failed" in " ".join(report.blockers)
        assert report.submission_attempted is False
        assert fake.send_requests == []
    finally:
        repository.close()


# -- the qualified-signal path --------------------------------------------


def test_qualified_signal_submits_exactly_one_demo_order():
    adapter, fake, repository, audit = build_adapter()
    try:
        report = run_session(adapter, policy=SessionPolicy(), **full_configuration())
        assert report.passed, report.blockers
        assert report.order["state"] == "FILLED"
        assert report.order["live"] is True
        assert report.order["account"] == "<redacted>"
        assert report.orders_sent == 1
        assert len(fake.send_requests) == 1
        assert fake.send_requests[0]["symbol"] == "EURUSD.pro"
        assert fake.send_requests[0]["volume"] == 0.1
        assert report.observe_only is False
        assert report.live_account_involved is False
        assert "MT5_ORDER_SUBMITTING" in report.audited_events
        assert "MT5_ORDER_RESULT" in report.audited_events
        # The cycle disarms and disconnects itself.
        assert adapter.armed is False
        assert adapter.connected is False
        assert audit.find(event_type="MT5_TRADING_DISARMED")
    finally:
        repository.close()


def test_no_signal_means_no_order_and_a_clear_reason():
    adapter, fake, repository, _ = build_adapter()
    try:
        report = run_session(
            adapter,
            policy=SessionPolicy(),
            **full_configuration(signal=None),
        )
        assert report.signal == {"qualified": False, "reason": "no_signal"}
        assert report.orders_sent == 0
        assert report.submission_attempted is False
        assert fake.send_requests == []
        assert "signal_qualified" in " ".join(report.blockers)
    finally:
        repository.close()


def test_max_orders_zero_never_submits():
    adapter, fake, repository, _ = build_adapter()
    try:
        report = run_session(
            adapter,
            policy=SessionPolicy(max_orders=0),
            **full_configuration(),
        )
        assert report.orders_sent == 0
        assert report.submission_attempted is False
        assert fake.send_requests == []
    finally:
        repository.close()


def test_rejected_risk_decision_blocks_the_order():
    adapter, fake, repository, _ = build_adapter()
    gate = FakeGate(decision_for(make_intent(), allowed=False))
    try:
        report = run_session(
            adapter,
            policy=SessionPolicy(),
            **full_configuration(gate=gate),
        )
        blockers = " ".join(report.blockers)
        assert "risk_decision_allowed" in blockers
        assert report.submission_attempted is False
        assert fake.send_requests == []
        assert len(gate.calls) == 1
    finally:
        repository.close()


def test_unapproved_strategy_blocks_arming():
    adapter, fake, repository, _ = build_adapter(lifecycle=NOT_APPROVED)
    try:
        report = run_session(
            adapter,
            policy=SessionPolicy(),
            **full_configuration(lifecycle=NOT_APPROVED),
        )
        assert "strategy_approved_active" in " ".join(report.blockers)
        assert report.armed_during_cycle is False
        assert fake.send_requests == []
    finally:
        repository.close()


def test_signal_source_failure_is_contained():
    adapter, fake, repository, _ = build_adapter()
    config = full_configuration()

    def explode():
        raise RuntimeError("strategy failed")

    config["signal_source"] = explode
    try:
        report = run_session(adapter, policy=SessionPolicy(), **config)
        assert "signal_source_failed" in " ".join(report.blockers)
        assert fake.send_requests == []
    finally:
        repository.close()


def test_gate_failure_is_contained():
    adapter, fake, repository, _ = build_adapter()
    gate = FakeGate(raises=True)
    try:
        report = run_session(
            adapter,
            policy=SessionPolicy(),
            **full_configuration(gate=gate),
        )
        assert "risk_evaluation_failed" in " ".join(report.blockers)
        assert report.submission_attempted is False
        assert fake.send_requests == []
    finally:
        repository.close()


def test_broker_term_mismatch_blocks_before_arming():
    adapter, fake, repository, _ = build_adapter()
    fake.symbols["EURUSD.pro"]["trade_contract_size"] = 1000.0
    try:
        report = run_session(adapter, policy=SessionPolicy(), **full_configuration())
        assert "broker_terms_match" in " ".join(report.blockers)
        assert report.armed_during_cycle is False
        assert fake.send_requests == []
    finally:
        repository.close()


def test_unresolved_order_blocks_the_cycle():
    # History is unavailable, so recovery cannot explain the in-doubt order and
    # the cycle must refuse to arm.
    adapter, fake, repository, _ = build_adapter(fake=FakeMT5())
    fake.fail_history = True
    repository.save_orders(
        [
            {
                "broker_id": "mt5_terminal",
                "request_id": "old-1",
                "simulation_id": "",
                "state": "UNRESOLVED",
                "account_login": LOGIN,
                "sent_at": AT,
                "provider_symbol": "EURUSD.pro",
                "side": "BUY",
                "volume": "0.1",
                "magic": 26081001,
                "comment": "old-1",
            }
        ]
    )
    try:
        report = run_session(adapter, policy=SessionPolicy(), **full_configuration())
        assert "no_unresolved_orders" in " ".join(report.blockers)
        assert report.armed_during_cycle is False
        assert fake.send_requests == []
    finally:
        repository.close()


def test_order_check_refusal_blocks_the_order():
    adapter, fake, repository, _ = build_adapter()
    fake.check_result = {"retcode": 10019, "comment": "no money", "margin": 0}
    try:
        report = run_session(adapter, policy=SessionPolicy(), **full_configuration())
        assert report.submission_attempted is True
        assert report.orders_sent == 0
        assert "submission_refused" in " ".join(report.blockers)
        assert fake.send_requests == []
        assert repository.get_order("req-1") is None
    finally:
        repository.close()


def test_unknown_outcome_is_marked_unresolved_and_not_resent():
    fake = FakeMT5()
    fake.send_script = ["raise"]
    adapter, fake, repository, _ = build_adapter(fake=fake)
    try:
        report = run_session(adapter, policy=SessionPolicy(), **full_configuration())
        assert report.order is not None
        assert report.order["unknown"] is True
        assert report.order["state"] == "UNRESOLVED"
        assert report.order["permits_resend"] is False
        assert report.orders_sent == 1
        assert len(fake.send_requests) == 1
        assert adapter.armed is False
        assert adapter.unresolved_request_ids() == ["req-1"]
        assert adapter.account_orders()[0]["account_fingerprint"] == adapter.account_fingerprint()
        assert "order_outcome_known" in " ".join(report.blockers)
    finally:
        repository.close()


# -- a switch mid-run is refused, not routed ---------------------------------


def test_switch_mid_run_is_refused_because_arming_was_confirmed_for_one_account():
    adapter, fake, repository, audit = build_adapter()
    config = full_configuration()
    original_gate = config["gate"]

    def switching_gate(request, context):
        # The operator changes the terminal account between the decision and the
        # order.  Arming was confirmed against the first account, so this order
        # must be refused -- the confirmed account is the only one AXIOM may use.
        fake.account_login = 77777777
        fake.trade_mode = 2
        fake.currency = "USD"
        return original_gate.evaluate(request, context)

    config["gate"] = SimpleNamespace(evaluate=switching_gate)
    try:
        report = run_session(adapter, policy=SessionPolicy(), **config)
        blockers = " ".join(report.blockers)
        assert "submission_refused:mt5_account_changed_since_confirmation" in blockers
        assert report.orders_sent == 0
        assert fake.send_requests == []
        # The switch is audited, and the arming state is kept: AXIOM never disarms
        # behind the operator's back.  What it does do is refuse to send.
        assert adapter.account_changes
        assert adapter.account_changes[-1]["to_mode"] == "REAL"
        assert adapter.account_changes[-1]["trading_authorisation"] == "kept"
        assert audit.find(event_type="MT5_ACCOUNT_CHANGED")
        # The cycle always disarms when it ends, so the flag is down here; the
        # point is that the *switch* did not do it.  While that cycle was running,
        # the arming still named the first account.
        assert adapter.armed_account_matches_current is False
    finally:
        repository.close()


def test_switch_to_an_account_that_fails_validation_blocks_the_order():
    adapter, fake, repository, _ = build_adapter()
    adapter.settings = adapter.settings.__class__(
        **{
            **adapter.settings.__dict__,
            "declared_account_currency": "USD",
            "declared_leverage": 100,
        }
    )
    config = full_configuration()
    original_gate = config["gate"]

    def switching_gate(request, context):
        # New account with a different currency: whatever the reason, it is not
        # the account the arming named, so nothing may be sent to it.
        fake.account_login = 66666666
        fake.currency = "JPY"
        fake.leverage = 500
        return original_gate.evaluate(request, context)

    config["gate"] = SimpleNamespace(evaluate=switching_gate)
    try:
        report = run_session(adapter, policy=SessionPolicy(), **config)
        blockers = " ".join(report.blockers)
        assert "submission_refused:mt5_account_changed_since_confirmation" in blockers
        assert report.orders_sent == 0
        assert fake.send_requests == []
    finally:
        repository.close()


def test_missing_account_profile_lists_the_exact_items():
    adapter, fake, repository, _ = build_adapter()
    adapter.account_profile_check = lambda terms: [
        "account_profile_missing",
        "conversion_rates_missing",
    ]
    try:
        report = run_session(adapter, policy=SessionPolicy(), **full_configuration())
        assert "account_terms_valid" in " ".join(report.blockers)
        assert "account_profile_missing" in " ".join(report.blockers)
        assert "conversion_rates_missing" in " ".join(report.blockers)
        assert report.account_validation["valid"] is False
        assert report.submission_attempted is False
        assert fake.send_requests == []
    finally:
        repository.close()


def test_another_accounts_unresolved_order_does_not_block_this_account():
    adapter, fake, repository, _ = build_adapter()
    repository.save_orders(
        [
            {
                "broker_id": "mt5_terminal",
                "request_id": "other-1",
                "simulation_id": "",
                "state": "UNRESOLVED",
                "account_fingerprint": adapter.account_fingerprint(99999999),
            }
        ]
    )
    try:
        adapter.connect()
        other = adapter.account_fingerprint(99999999)
        adapter.disconnect(reason="test")
        repository.save_orders(
            [
                {
                    "broker_id": "mt5_terminal",
                    "request_id": "other-1",
                    "simulation_id": "",
                    "state": "UNRESOLVED",
                    "account_fingerprint": other,
                }
            ]
        )
        report = run_session(adapter, policy=SessionPolicy(), **full_configuration())
        assert report.passed, report.blockers
        assert report.orders_sent == 1
        # The other account's record is untouched.
        assert repository.get_order("other-1")["state"] == "UNRESOLVED"
    finally:
        repository.close()


def test_cycle_keeps_trading_enabled_separate_from_account_mode():
    adapter, fake, repository, _ = build_adapter(fake=FakeMT5(trade_mode=2))
    try:
        report = run_session(adapter, policy=SessionPolicy(), **full_configuration())
        assert report.account["account_mode"] == "REAL"
        assert report.trading_enabled_setting is True
        assert report.demo_only_enforced is False
        assert report.orders_sent == 1
    finally:
        repository.close()


def test_no_market_access_token_is_used_on_the_mt5_path():
    vault = FakeVault()
    adapter, fake, repository, _ = build_adapter(vault=vault)
    try:
        report = run_session(adapter, policy=SessionPolicy(), **full_configuration())
        # Nothing was ever written to the vault for a session that was already
        # logged in, and the adapter reports no token requirement.
        assert (vault.values or {}) == {}
        assert adapter.status()["market_access_token_required"] is False
        blob = json.dumps(report.audit_view(), default=str).lower()
        assert "token" not in blob.replace("no_token", "")
    finally:
        repository.close()


# -- time budget -----------------------------------------------------------


def test_time_budget_stops_the_cycle_before_submission():
    clock = {"now": AT}
    adapter, fake, repository, _ = build_adapter()
    adapter.clock = lambda: clock["now"]

    config = full_configuration()

    def slow_gate(request, context):
        clock["now"] = AT + timedelta(seconds=120)
        return decision_for(make_intent())

    config["gate"] = SimpleNamespace(evaluate=slow_gate)
    try:
        report = run_session(
            adapter,
            policy=SessionPolicy(max_duration_seconds=5),
            clock=lambda: clock["now"],
            **config,
        )
        assert "session_time_budget_exceeded" in " ".join(report.blockers)
        assert report.submission_attempted is False
        assert fake.send_requests == []
    finally:
        repository.close()


# -- account switching and redaction --------------------------------------


def test_account_switch_is_audited_and_keeps_trading_authorisation():
    adapter, fake, repository, audit = build_adapter()
    try:
        adapter.connect()
        adapter.arm_trading(confirm="ENABLE LIVE TRADING")
        assert adapter.armed is True
        first = adapter.redacted_account()["account_fingerprint"]

        fake.account_login = 99999999
        fake.trade_mode = 2
        refreshed = adapter.refresh_account()

        # The account is the operator's choice in MT5: a switch is audited and
        # revalidated, never treated as a reason to disarm on its own.
        assert adapter.armed is True
        assert adapter.account_changes[-1]["trading_authorisation"] == "kept"
        assert refreshed["account_mode"] == "REAL"
        assert refreshed["login"] == "<redacted>"
        assert refreshed["account_fingerprint"] != first
        assert adapter.account_changes[-1]["to_mode"] == "REAL"
        assert audit.find(event_type="MT5_ACCOUNT_CHANGED")
        # The switch is not a licence to trade: nothing was sent.
        assert fake.send_requests == []
    finally:
        adapter.disconnect(reason="test")
        repository.close()


def test_refresh_detects_a_switch_back_and_revalidates_each_account():
    adapter, fake, repository, _ = build_adapter()
    try:
        adapter.connect()
        fake.account_login = 11111111
        adapter.refresh_account()
        fake.account_login = LOGIN
        refreshed = adapter.refresh_account()
        assert refreshed["account_mode"] == "DEMO"
        assert [item["to_mode"] for item in adapter.account_changes] == ["DEMO", "DEMO"]
        assert adapter.validate_account_terms()["valid"] is True
    finally:
        adapter.disconnect(reason="test")
        repository.close()


def test_session_report_contains_no_identifiers_or_balances():
    vault = FakeVault()
    adapter, fake, repository, _ = build_adapter(vault=vault)
    adapter.password_store.save(PASSWORD)
    try:
        report = run_session(adapter, policy=SessionPolicy(), **full_configuration())
        blob = json.dumps(report.audit_view(), default=str)
        assert str(LOGIN) not in blob
        assert PASSWORD not in blob
        assert "10000" not in blob  # balance/equity are never reported
        assert "equity" not in blob.lower()
        assert "<redacted>" in blob
        assert "withheld" in blob
    finally:
        repository.close()


def test_redacted_account_excludes_financials():
    adapter, fake, repository, _ = build_adapter()
    try:
        adapter.connect()
        view = adapter.redacted_account()
        assert view["login"] == "<redacted>"
        assert view["balance_reporting"] == "withheld"
        for key in ("balance", "equity", "margin", "margin_free", "margin_level"):
            assert key not in view
        assert len(view["account_fingerprint"]) == 12
    finally:
        adapter.disconnect(reason="test")
        repository.close()


# -- CLI -------------------------------------------------------------------


def _settings_file(tmp_path, **overrides):
    from axiom_forex.config.mt5 import mt5_settings_document

    values = dict(
        terminal_path="terminal64.exe",
        server="Broker-Demo",
        account_login=LOGIN,
        mode="TRADING_ENABLED",
    )
    values.update(overrides)
    settings = MT5Settings(**values)
    path = tmp_path / "mt5_settings.local.json"
    path.write_text(json.dumps(mt5_settings_document(settings)), encoding="utf-8")
    return path


def test_cli_session_requires_local_settings(tmp_path, capsys):
    from axiom_forex.cli import main

    code = main(["mt5-session", "--mt5-settings-path", str(tmp_path / "absent.json")])
    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["status"] == "BLOCKED"
    assert payload["order_sent"] is False


def test_cli_session_rejects_an_invalid_policy(tmp_path, capsys):
    from axiom_forex.cli import main

    path = _settings_file(tmp_path)
    code = main(
        ["mt5-session", "--mt5-settings-path", str(path), "--mt5-max-orders", "5"]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload["reason"] == "max_orders_exceeds_cycle_limit"
    assert payload["order_sent"] is False


def test_cli_session_reports_a_missing_package_without_connecting(tmp_path, capsys):
    import importlib.util

    if importlib.util.find_spec("MetaTrader5") is not None:
        pytest.skip("a real MetaTrader5 package is installed in this interpreter")
    from axiom_forex.cli import main

    path = _settings_file(tmp_path)
    code = main(["mt5-session", "--mt5-settings-path", str(path)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 3
    assert payload["status"] == "BLOCKED"
    assert payload["reason"] == "metatrader5_package_not_installed"
    assert payload["order_sent"] is False
    assert "No connection was attempted" in payload["next"]
