"""MT5 adapter behaviour with a fake ``MetaTrader5`` module.

No real terminal, account, or package is touched.  The fake module records every
call, so these tests can assert the properties that matter:

* nothing is sent on construction or connection;
* the durable record exists *before* ``order_send`` is called;
* an unknown outcome is never resent and blocks further trading;
* recovery resolves in-doubt orders from broker history and never from a guess;
* the password never appears in a receipt, audit event, or stored order.
"""

from __future__ import annotations

import importlib.util
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.audit.log import InMemoryAuditLog
from app.common.types import OrderSide
from app.execution.broker import TradingBlocked
from app.risk.models import ApprovedOrderIntent
from app.risk.pretrade import GateDecision, RejectionReason
from app.universe.specs import (
    CurrencyPairSpec,
    InstrumentStatus,
    QuantityUnit,
)
from axiom_forex.brokers.external import ExternalBrokerBoundary
from axiom_forex.brokers.mt5 import (
    MT5DataRefused,
    MT5ExecutionMode,
    MT5ModuleUnavailable,
    MT5OrderRefused,
    MT5PasswordStore,
    MT5Settings,
    MT5SubmitRefused,
    MT5TradingAdapter,
    RetcodeClass,
    aggregate_ticks_to_bars,
    build_market_request,
    classify,
    close_volume_check,
    cross_check,
    filling_for,
    netting_conflict,
    open_positions,
    reconcile_positions,
    send_order,
    to_broker_volume,
)
from axiom_forex.brokers.mt5.terminal import MT5Terminal
from axiom_forex.config.vault import SecureStorageUnavailable
from axiom_forex.database.repository import ForexRepository
from tests.fake_mt5 import (
    FakeMT5,
    make_history_deal,
    make_history_order,
    make_position,
    make_tick,
)

AT = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc)
PASSWORD = "S3cret-Terminal-Passw0rd!"


# -- fixtures and helpers ---------------------------------------------------


@dataclass
class FakeVault:
    available_flag: bool = True
    values: dict[str, str] = field(default_factory=dict)

    def describe(self):
        return {"backend": "fake_os_vault", "secure": True, "plaintext_fallback": False}

    def available(self) -> bool:
        return self.available_flag

    def read(self, entry: str) -> str | None:
        return self.values.get(entry)

    def write(self, entry: str, value: str) -> None:
        if not self.available_flag:
            raise SecureStorageUnavailable("secure_storage_unavailable")
        self.values[entry] = value

    def erase(self, entry: str) -> bool:
        return self.values.pop(entry, None) is not None


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


def allow(intent: ApprovedOrderIntent) -> GateDecision:
    return GateDecision(
        allowed=True,
        code=RejectionReason.ALLOWED,
        reason="allowed",
        details={},
        audit_event={"event_id": "decision-1"},
        order_intent=intent,
    )


def mt5_settings(**overrides) -> MT5Settings:
    values = dict(
        terminal_path="terminal64.exe",
        server="Broker-Demo",
        account_login=55500123,
        mode=MT5ExecutionMode.TRADING_ENABLED,
        symbol_suffix=".pro",
        magic=26081001,
        deviation_points=10,
        max_price_drift_points=20,
    )
    values.update(overrides)
    return MT5Settings(**values)


APPROVED = SimpleNamespace(approve_active=lambda: True, stage="APPROVED")
NOT_APPROVED = SimpleNamespace(approve_active=lambda: False, stage="TESTING")


@dataclass
class Harness:
    adapter: MT5TradingAdapter
    fake: FakeMT5
    repository: ForexRepository
    audit: InMemoryAuditLog
    settings: MT5Settings

    def arm(self) -> None:
        self.adapter.arm_trading(
            account_login=self.fake.account_login,
            server=self.fake.account_server,
            confirm=f"ENABLE LIVE TRADING {self.fake.account_login}",
        )


def build_harness(
    *,
    fake: FakeMT5 | None = None,
    lifecycle=APPROVED,
    lifecycle_lookup="default",
    settings_overrides: dict | None = None,
    repository: ForexRepository | None = None,
    vault: FakeVault | None = None,
    connect: bool = True,
) -> Harness:
    fake = fake or FakeMT5()
    repository = repository or ForexRepository(":memory:")
    audit = InMemoryAuditLog()
    settings = mt5_settings(**(settings_overrides or {}))
    store = MT5PasswordStore(
        settings=settings, vault=vault or FakeVault(), audit_log=audit
    )
    if lifecycle_lookup == "default":
        lookup = lambda strategy_id: lifecycle  # noqa: E731
    elif lifecycle_lookup is None:
        lookup = None
    else:
        lookup = lifecycle_lookup
    adapter = MT5TradingAdapter(
        settings=settings,
        password_store=store,
        repository=repository,
        audit_log=audit,
        module=fake,
        lifecycle_lookup=lookup,
        clock=lambda: AT,
    )
    if connect:
        adapter.connect()
    return Harness(
        adapter=adapter,
        fake=fake,
        repository=repository,
        audit=audit,
        settings=settings,
    )


@pytest.fixture
def harness():
    instance = build_harness()
    try:
        yield instance
    finally:
        instance.adapter.disconnect(reason="test")
        instance.repository.close()


# -- safety at startup ------------------------------------------------------


def test_constructing_and_connecting_sends_nothing():
    instance = build_harness()
    try:
        assert instance.fake.send_requests == []
        assert instance.repository.list_orders() == []
        assert instance.adapter.connected is True
        assert instance.adapter.armed is False
        assert instance.adapter.status()["no_order_on_connect"] is True
    finally:
        instance.adapter.disconnect(reason="test")
        instance.repository.close()


def test_module_is_never_imported_at_package_import():
    import axiom_forex.brokers.mt5 as package

    assert "MetaTrader5" not in str(package.__dict__.keys())
    assert hasattr(package, "import_mt5")


@pytest.mark.skipif(
    importlib.util.find_spec("MetaTrader5") is not None,
    reason="a real MetaTrader5 package is installed in this interpreter",
)
def test_missing_package_is_reported_not_crashed():
    from axiom_forex.brokers.mt5 import import_mt5

    with pytest.raises(MT5ModuleUnavailable) as excinfo:
        import_mt5()
    assert excinfo.value.reason == "metatrader5_package_not_installed"


# -- terminal and account ---------------------------------------------------


def test_connect_requires_matching_account(harness):
    assert harness.adapter.status()["account"]["login"] == 55500123
    other = build_harness(settings_overrides={"account_login": 55500999}, connect=False)
    try:
        terminal = MT5Terminal(other.settings, module=FakeMT5(account_login=55500001))
        with pytest.raises(Exception) as excinfo:
            terminal.connect()
        assert getattr(excinfo.value, "reason", "") == "mt5_account_mismatch"
        assert terminal.connected is False
    finally:
        other.repository.close()


def test_connect_refusal_is_reported(harness):
    fake = FakeMT5()
    fake.initialize_result = False
    terminal = MT5Terminal(harness.settings, module=fake)
    with pytest.raises(Exception) as excinfo:
        terminal.connect()
    assert getattr(excinfo.value, "reason", "") == "mt5_initialize_refused"
    assert fake.send_requests == []


def test_settings_require_no_market_access_token():
    settings = mt5_settings()
    assert settings.market_access_token_required is False
    assert settings.use_existing_session is True
    view = settings.audit_view()
    assert view["market_access_token_required"] is False
    assert "token" not in json.dumps(view).lower().replace(
        "market_access_token_required", ""
    )


def test_connect_uses_only_the_terminal_path_by_default():
    vault = FakeVault()
    settings = mt5_settings()  # use_existing_session defaults to True
    store = MT5PasswordStore(settings=settings, vault=vault)
    store.save(PASSWORD)
    fake = FakeMT5()
    repository = ForexRepository(":memory:")
    adapter = MT5TradingAdapter(
        settings=settings,
        password_store=store,
        repository=repository,
        audit_log=InMemoryAuditLog(),
        module=fake,
        lifecycle_lookup=lambda strategy_id: APPROVED,
        clock=lambda: AT,
    )
    try:
        adapter.connect()
        path, kwargs = fake.initialize_calls[0]
        assert path == "terminal64.exe"
        assert set(kwargs) == {"timeout"}
        assert "password" not in json.dumps(kwargs)
        assert adapter.connected is True
    finally:
        adapter.disconnect(reason="test")
        repository.close()


def test_connect_reuses_an_existing_session_without_the_vault():
    vault = FakeVault()
    instance = build_harness(vault=vault)
    try:
        # The fake initializes without credentials, so no vault read happens.
        assert vault.values == {}
        attempts = instance.fake.initialize_calls
        assert attempts and "password" not in attempts[0][1]
    finally:
        instance.adapter.disconnect(reason="test")
        instance.repository.close()


def test_connect_uses_the_vault_only_when_the_session_is_not_logged_in():
    fake = FakeMT5()
    original = fake.initialize
    calls: list[dict] = []

    def initialize(path, **kwargs):
        calls.append(kwargs)
        if "password" not in kwargs:
            fake.account_login = 0  # not logged in yet
        else:
            fake.account_login = 55500123
        return original(path, **kwargs)

    fake.initialize = initialize  # type: ignore[assignment]
    vault = FakeVault()
    # Explicitly opting out of "use the existing terminal session" is the only
    # case in which AXIOM may log in itself, and then only from the OS vault.
    settings = mt5_settings(use_existing_session=False)
    store = MT5PasswordStore(settings=settings, vault=vault)
    store.save(PASSWORD)
    audit = InMemoryAuditLog()
    repository = ForexRepository(":memory:")
    adapter = MT5TradingAdapter(
        settings=settings,
        password_store=store,
        repository=repository,
        audit_log=audit,
        module=fake,
        lifecycle_lookup=lambda strategy_id: APPROVED,
        clock=lambda: AT,
    )
    try:
        adapter.connect()
        assert calls[-1]["password"] == PASSWORD
        assert calls[-1]["login"] == 55500123
        assert calls[-1]["server"] == "Broker-Demo"
        # The password is not retained by the adapter and never audited.
        assert PASSWORD not in json.dumps(adapter.status(), default=str)
        assert PASSWORD not in json.dumps(audit.events, default=str)
    finally:
        adapter.disconnect(reason="test")
        repository.close()


def test_status_reports_account_terms_and_position_accounting(harness):
    status = harness.adapter.status()
    assert status["account"]["currency"] == "USD"
    assert status["account"]["leverage"] == 100
    assert status["account"]["position_accounting"] == "HEDGING"
    assert status["token_required_by_broker"] is False
    assert status["password"]["configured"] is False
    assert "password" not in json.dumps(status["account"], default=str)


def test_exchange_margin_accounts_are_unsupported():
    fake = FakeMT5(margin_mode=1)
    with pytest.raises(Exception) as excinfo:
        build_harness(fake=fake)
    assert getattr(excinfo.value, "reason", "") == "mt5_margin_mode_unsupported"


def test_netting_accounts_are_reported_as_netting():
    fake = FakeMT5(margin_mode=0)
    instance = build_harness(fake=fake)
    try:
        assert instance.adapter.status()["account"]["position_accounting"] == "NETTING"
    finally:
        instance.adapter.disconnect(reason="test")
        instance.repository.close()


# -- symbols ----------------------------------------------------------------


@pytest.mark.parametrize(
    "symbols, spec_overrides, expected",
    [
        (None, {}, "EURUSD.pro"),
        ({"EURUSD.pro": {}}, {"provider_aliases": {}}, "EURUSD.pro"),
        ({"EURUSD": {}}, {"provider_aliases": {}}, "EURUSD"),
        ({"EURUSD.pro": {}}, {"provider_aliases": {"mt5": "EURUSD.pro"}}, "EURUSD.pro"),
    ],
)
def test_symbol_resolution(harness, symbols, spec_overrides, expected):
    fake = FakeMT5(symbols=symbols or {})
    terminal = MT5Terminal(harness.settings, module=fake)
    terminal.connect()
    spec = make_spec(**spec_overrides)
    assert terminal.resolve_symbol(spec) == expected


def test_unresolved_symbol_fails_closed(harness):
    fake = FakeMT5(symbols={"GBPUSD": {}}, default_symbol=False)
    terminal = MT5Terminal(harness.settings, module=fake)
    terminal.connect()
    with pytest.raises(MT5DataRefused) as excinfo:
        terminal.resolve_symbol(make_spec())
    assert excinfo.value.reason == "mt5_symbol_unresolved"
    assert "tried" in excinfo.value.detail


def test_cross_check_accepts_matching_terms():
    fake = FakeMT5()
    terminal = MT5Terminal(mt5_settings(), module=fake)
    terminal.connect()
    info = terminal.symbol_info("EURUSD.pro")
    check = cross_check(make_spec(), info)
    assert check.data_ok and check.trading_ok
    assert check.differences == ()


@pytest.mark.parametrize(
    "override, reason",
    [
        ({"trade_contract_size": 1000.0}, "mt5_symbol_contract_size_mismatch"),
        ({"volume_min": 0.1}, "mt5_symbol_min_volume_mismatch"),
        ({"volume_step": 0.1}, "mt5_symbol_volume_step_mismatch"),
        ({"digits": 4}, "mt5_symbol_precision_mismatch"),
        ({"point": 0.0001}, "mt5_symbol_tick_mismatch"),
        ({"currency_profit": "JPY"}, "mt5_symbol_currency_mismatch"),
        ({"trade_mode": 3}, "mt5_symbol_not_fully_tradable"),
    ],
)
def test_cross_check_reports_broker_differences(override, reason):
    fake = FakeMT5(symbols={"EURUSD.pro": override})
    terminal = MT5Terminal(mt5_settings(), module=fake)
    terminal.connect()
    info = terminal.symbol_info("EURUSD.pro")
    check = cross_check(make_spec(), info)
    assert not check.ok
    assert reason in check.errors
    assert check.differences


def test_max_quantity_above_broker_maximum_is_refused():
    fake = FakeMT5(symbols={"EURUSD.pro": {"volume_max": 0.5}})
    terminal = MT5Terminal(mt5_settings(), module=fake)
    terminal.connect()
    check = cross_check(make_spec(), terminal.symbol_info("EURUSD.pro"))
    assert "mt5_symbol_max_volume_exceeded" in check.errors


def test_to_broker_volume_variants():
    terminal = MT5Terminal(mt5_settings(), module=FakeMT5())
    terminal.connect()
    symbol_info = terminal.symbol_info("EURUSD.pro")
    lots, result = to_broker_volume(make_spec(), Decimal("0.25"), symbol_info)
    assert result.valid and lots == Decimal("0.25")

    base_spec = make_spec(quantity_unit=QuantityUnit.BASE_UNITS, min_quantity=Decimal("1000"), quantity_step=Decimal("1000"), max_quantity=Decimal("1000000"))
    lots, result = to_broker_volume(base_spec, Decimal("25000"), symbol_info)
    assert result.valid and lots == Decimal("0.25")

    lots, result = to_broker_volume(base_spec, Decimal("25001"), symbol_info)
    assert lots is None
    assert "quantity_not_step_aligned" in result.errors

    lots, result = to_broker_volume(make_spec(min_quantity=Decimal("0.001"), quantity_step=Decimal("0.001")), Decimal("0.005"), symbol_info)
    assert lots is None
    assert "quantity_not_step_aligned_with_broker" in result.errors


# -- market data ------------------------------------------------------------


def test_quote_maps_tick_with_explicit_basis(harness):
    quote = harness.adapter.quote(make_spec())
    assert quote.bid == Decimal("1.1")
    assert quote.ask == Decimal("1.1002")
    assert quote.source == "mt5_terminal"
    assert quote.metadata["price_basis"] == "BID_ASK"
    assert quote.metadata["provider_symbol"] == "EURUSD.pro"


def test_invalid_tick_is_refused(harness):
    harness.fake.symbols["EURUSD.pro"]["ask"] = 1.0  # crossed
    with pytest.raises(MT5DataRefused) as excinfo:
        harness.adapter.quote(make_spec())
    assert excinfo.value.reason == "mt5_tick_invalid"


def test_bars_are_two_sided_and_gaps_stay_visible():
    start = AT
    end = AT + timedelta(minutes=3)
    ticks = [
        make_tick(start + timedelta(seconds=10), 1.10000, 1.10020),
        make_tick(start + timedelta(seconds=40), 1.10030, 1.10050),
        make_tick(start + timedelta(minutes=1, seconds=5), 1.10040, 1.10060),
        make_tick(start + timedelta(minutes=2, seconds=50), 1.10050, 1.10070),
    ]
    fake = FakeMT5(ticks=ticks)
    terminal = MT5Terminal(mt5_settings(), module=fake)
    terminal.connect()
    parsed = terminal.ticks("EURUSD.pro", start, end)
    bars = aggregate_ticks_to_bars(
        make_spec(), "EURUSD.pro", "1m", parsed, start=start, end=end
    )
    assert len(bars) == 3
    first = bars[0]
    assert first.bid.open == Decimal("1.1")
    assert first.ask.high == Decimal("1.1005")
    assert first.quote_count == 2
    assert first.complete is True
    assert bars[-1].complete is False
    for bar in bars:
        assert bar.bid.parsed()[1].valid and bar.ask.parsed()[1].valid


def test_unsupported_timeframe_is_refused(harness):
    with pytest.raises(MT5DataRefused):
        aggregate_ticks_to_bars(
            make_spec(), "EURUSD.pro", "7m", [], start=AT, end=AT
        )


def test_rates_are_labelled_bid_only(harness):
    harness.fake.rates = [
        {
            "time": AT,
            "open": 1.1,
            "high": 1.11,
            "low": 1.09,
            "close": 1.105,
            "tick_volume": 10,
            "real_volume": 0,
            "spread": 12,
        }
    ]
    rows = harness.adapter.terminal.rates(
        "EURUSD.pro", "1m", start=AT, end=AT + timedelta(minutes=1)
    )
    assert rows[0]["price_basis"] == "BID_ONLY"
    assert rows[0]["source"] == "mt5_copy_rates_range"


# -- retcodes and order shaping --------------------------------------------


@pytest.mark.parametrize(
    "retcode, expected",
    [
        (10009, RetcodeClass.OK),
        (10008, RetcodeClass.OK),
        (10010, RetcodeClass.OK),
        (10006, RetcodeClass.REJECTED),
        (10018, RetcodeClass.REJECTED),
        (10019, RetcodeClass.REJECTED),
        (10030, RetcodeClass.REJECTED),
        (10004, RetcodeClass.RETRYABLE),
        (10020, RetcodeClass.RETRYABLE),
        (10021, RetcodeClass.RETRYABLE),
        (10012, RetcodeClass.UNKNOWN),
        (10031, RetcodeClass.UNKNOWN),
        (12345, RetcodeClass.UNKNOWN),
        (None, RetcodeClass.UNKNOWN),
    ],
)
def test_retcode_classification(retcode, expected):
    assert classify(retcode) is expected


def test_no_result_is_unknown_even_with_a_known_retcode():
    assert classify(10009, has_result=False) is RetcodeClass.UNKNOWN


@pytest.mark.parametrize(
    "mask, label",
    [(1, "FOK"), (2, "IOC"), (4, "RETURN")],
)
def test_filling_mode_comes_from_the_broker(mask, label):
    terminal = MT5Terminal(mt5_settings(), module=FakeMT5())
    terminal.connect()
    harness_info = terminal.symbol_info("EURUSD.pro")
    object.__setattr__(harness_info, "filling_mask", mask)
    value, name = filling_for(harness_info)
    assert name == label
    assert isinstance(value, int)


def test_unknown_filling_mask_is_refused():
    terminal = MT5Terminal(mt5_settings(), module=FakeMT5())
    terminal.connect()
    info = terminal.symbol_info("EURUSD.pro")
    object.__setattr__(info, "filling_mask", 0)
    with pytest.raises(MT5OrderRefused) as excinfo:
        filling_for(info)
    assert excinfo.value.reason == "mt5_filling_mode_unknown"


def test_market_request_carries_the_gated_fields():
    request = build_market_request(
        symbol="EURUSD.pro",
        side="BUY",
        volume=Decimal("0.10"),
        price=Decimal("1.1002"),
        deviation=10,
        magic=26081001,
        comment="req-1",
        filling=0,
    )
    assert request["symbol"] == "EURUSD.pro"
    assert request["volume"] == 0.1
    assert request["type"] == 0
    assert request["deviation"] == 10
    assert request["magic"] == 26081001
    assert request["type_filling"] == 0
    assert request["action"] == 1


# -- send_order classification ---------------------------------------------


def _terminal() -> MT5Terminal:
    terminal = MT5Terminal(mt5_settings(), module=FakeMT5())
    terminal.connect()
    return terminal


def test_partial_fill_is_reported_as_partial():
    fake = FakeMT5()
    fake.send_script = [
        {"retcode": 10010, "order": 1, "deal": 2, "volume": 0.04, "price": 1.1}
    ]
    terminal = MT5Terminal(mt5_settings(), module=fake)
    terminal.connect()
    outcome = send_order(
        terminal,
        {"volume": 0.1, "symbol": "EURUSD.pro"},
        request_id="req-1",
        sent_at=AT,
    )
    assert outcome.classification == RetcodeClass.OK.value
    assert outcome.partially_filled is True
    assert outcome.volume_filled == Decimal("0.04")
    assert outcome.state == "PARTIAL"
    assert outcome.permits_resend is False


def test_done_without_volume_is_unknown():
    fake = FakeMT5()
    fake.send_script = [{"retcode": 10009, "order": 1, "deal": 2, "volume": 0}]
    terminal = MT5Terminal(mt5_settings(), module=fake)
    terminal.connect()
    outcome = send_order(terminal, {"volume": 0.1}, request_id="req-1", sent_at=AT)
    assert outcome.unknown is True
    assert outcome.state == "UNRESOLVED"
    assert outcome.permits_resend is False


@pytest.mark.parametrize("scripted", ["raise", None])
def test_no_answer_is_an_unknown_outcome(scripted):
    fake = FakeMT5()
    fake.send_script = [scripted]
    terminal = MT5Terminal(mt5_settings(), module=fake)
    terminal.connect()
    outcome = send_order(terminal, {"volume": 0.1}, request_id="req-1", sent_at=AT)
    assert outcome.unknown is True
    assert outcome.state == "UNRESOLVED"
    assert outcome.permits_resend is False


def test_requote_may_be_re_decided_but_is_not_silently_retried():
    fake = FakeMT5()
    fake.send_script = [{"retcode": 10004, "order": 0, "deal": 0, "volume": 0.0}]
    terminal = MT5Terminal(mt5_settings(), module=fake)
    terminal.connect()
    outcome = send_order(terminal, {"volume": 0.1}, request_id="req-1", sent_at=AT)
    assert outcome.classification == RetcodeClass.RETRYABLE.value
    assert outcome.state == "REJECTED"
    assert len(fake.send_requests) == 1


def test_unknown_retcode_is_unknown_outcome():
    fake = FakeMT5()
    fake.send_script = [{"retcode": 99999, "order": 0, "deal": 0, "volume": 0.0}]
    terminal = MT5Terminal(mt5_settings(), module=fake)
    terminal.connect()
    outcome = send_order(terminal, {"volume": 0.1}, request_id="req-1", sent_at=AT)
    assert outcome.state == "UNRESOLVED"
    assert outcome.permits_resend is False


# -- positions, netting, hedging ------------------------------------------


def test_positions_are_parsed(harness):
    harness.fake.positions = [
        make_position(ticket=11, symbol="EURUSD.pro", side="BUY", volume=0.1)
    ]
    positions = open_positions(harness.adapter.terminal)
    assert positions[0].ticket == 11
    assert positions[0].side == "BUY"
    assert positions[0].volume == Decimal("0.1")


def test_netting_conflict_rules():
    position = make_position(ticket=1, symbol="EURUSD.pro", side="BUY", volume=0.1)
    parsed = open_positions(
        MT5Terminal(mt5_settings(), module=SimpleNamespace(positions_get=lambda **kw: (SimpleNamespace(**position),))),
        symbol="EURUSD.pro",
    )
    reason = netting_conflict(
        parsed,
        symbol="EURUSD.pro",
        side="SELL",
        accounting="NETTING",
        approved_close=False,
    )
    assert reason == "mt5_netting_account_opposite_order_would_reduce_position"
    assert (
        netting_conflict(
            parsed,
            symbol="EURUSD.pro",
            side="SELL",
            accounting="NETTING",
            approved_close=True,
        )
        is None
    )
    assert (
        netting_conflict(
            parsed,
            symbol="EURUSD.pro",
            side="BUY",
            accounting="NETTING",
            approved_close=True,
        )
        == "mt5_close_side_matches_open_position"
    )
    assert (
        netting_conflict(
            parsed,
            symbol="EURUSD.pro",
            side="SELL",
            accounting="HEDGING",
            approved_close=False,
        )
        is None
    )


def test_close_volume_checks():
    position = open_positions(
        MT5Terminal(
            mt5_settings(),
            module=SimpleNamespace(
                positions_get=lambda **kw: (
                    SimpleNamespace(
                        **make_position(
                            ticket=5, symbol="EURUSD.pro", side="SELL", volume=0.2
                        )
                    ),
                )
            ),
        ),
        symbol="EURUSD.pro",
    )[0]
    assert close_volume_check(position, Decimal("0.1")) is None
    assert close_volume_check(position, Decimal("0.3")) == "close_volume_exceeds_position"
    assert close_volume_check(position, Decimal("0")) == "non_positive_quantity"


# -- arming ----------------------------------------------------------------


def test_submit_before_arming_is_refused(harness):
    with pytest.raises(MT5SubmitRefused) as excinfo:
        harness.adapter.submit(allow(make_intent()), make_intent(), spec=make_spec())
    assert excinfo.value.reason == "mt5_trading_not_armed"
    assert harness.fake.send_requests == []


def test_arming_requires_the_exact_confirmation(harness):
    with pytest.raises(MT5SubmitRefused) as excinfo:
        harness.adapter.arm_trading(
            account_login=55500123, server="Broker-Demo", confirm="yes"
        )
    assert excinfo.value.reason == "mt5_arming_confirmation_mismatch"
    assert harness.adapter.armed is False


def test_arming_requires_matching_account_and_server(harness):
    with pytest.raises(MT5SubmitRefused) as excinfo:
        harness.adapter.arm_trading(
            account_login=55500999,
            server="Broker-Demo",
            confirm="ENABLE LIVE TRADING 55500999",
        )
    assert excinfo.value.reason == "mt5_arming_account_mismatch"
    with pytest.raises(MT5SubmitRefused) as server_error:
        harness.adapter.arm_trading(
            account_login=55500123,
            server="Other",
            confirm="ENABLE LIVE TRADING 55500123",
        )
    assert server_error.value.reason == "mt5_arming_server_mismatch"


def test_observe_only_mode_cannot_arm():
    instance = build_harness(
        settings_overrides={"mode": MT5ExecutionMode.OBSERVE_ONLY}
    )
    try:
        with pytest.raises(Exception) as excinfo:
            instance.arm()
        assert getattr(excinfo.value, "reason", "") == "mt5_trading_not_enabled"
        assert instance.adapter.armed is False
    finally:
        instance.adapter.disconnect(reason="test")
        instance.repository.close()


def test_terminal_without_trading_permission_cannot_arm():
    instance = build_harness(fake=FakeMT5(trade_allowed=False))
    try:
        with pytest.raises(MT5SubmitRefused) as excinfo:
            instance.arm()
        assert excinfo.value.reason == "mt5_terminal_trading_not_allowed"
    finally:
        instance.adapter.disconnect(reason="test")
        instance.repository.close()


def test_arming_without_naming_the_account(harness):
    harness.adapter.arm_trading(confirm="ENABLE LIVE TRADING")
    assert harness.adapter.armed is True
    # The authorisation is not pinned to this account.
    harness.fake.account_login = 77889900
    harness.adapter.refresh_account()
    assert harness.adapter.armed is True
    harness.adapter.disarm(reason="test")
    assert harness.adapter.armed is False


def test_arming_then_disarming(harness):
    harness.arm()
    assert harness.adapter.armed is True
    assert harness.adapter.status()["execution_authorised"] is True
    harness.adapter.disarm(reason="test")
    assert harness.adapter.armed is False
    kinds = [event["event_type"] for event in harness.audit.events]
    assert "MT5_TRADING_ARMED" in kinds
    assert "MT5_TRADING_DISARMED" in kinds


# -- submission preflight --------------------------------------------------


def _refusal(harness, decision, intent, spec=None) -> str:
    with pytest.raises(MT5SubmitRefused) as excinfo:
        harness.adapter.submit(decision, intent, spec=spec or make_spec())
    return excinfo.value.reason


def test_rejected_decision_is_refused(harness):
    harness.arm()
    intent = make_intent()
    decision = GateDecision(
        allowed=False,
        code=RejectionReason.STALE_QUOTE,
        reason="stale",
        details={},
        audit_event={"event_id": "d"},
        order_intent=None,
    )
    assert _refusal(harness, decision, intent) == "mt5_gate_decision_not_allowed"
    assert harness.fake.send_requests == []


def test_intent_must_match_the_approved_one(harness):
    harness.arm()
    approved = make_intent()
    tampered = make_intent(quantity=Decimal("0.50"))
    assert (
        _refusal(harness, allow(approved), tampered)
        == "mt5_intent_differs_from_approved_intent"
    )
    assert harness.fake.send_requests == []


def test_different_request_id_is_refused(harness):
    harness.arm()
    approved = make_intent()
    other = make_intent(request_id="req-2")
    assert _refusal(harness, allow(approved), other) == "mt5_intent_request_mismatch"


def test_strategy_must_be_approved_and_active():
    instance = build_harness(lifecycle=NOT_APPROVED)
    try:
        instance.arm()
        assert (
            _refusal(instance, allow(make_intent()), make_intent())
            == "mt5_strategy_not_approved_active"
        )
    finally:
        instance.adapter.disconnect(reason="test")
        instance.repository.close()


def test_missing_lifecycle_lookup_fails_closed():
    instance = build_harness(lifecycle_lookup=None)
    try:
        instance.arm()
        assert (
            _refusal(instance, allow(make_intent()), make_intent())
            == "mt5_strategy_lifecycle_not_supplied"
        )
    finally:
        instance.adapter.disconnect(reason="test")
        instance.repository.close()


def test_spec_version_mismatch_is_refused(harness):
    harness.arm()
    intent = make_intent(spec_version="spec-mt5-v2")
    assert _refusal(harness, allow(intent), intent) == "mt5_spec_version_mismatch"


def test_account_currency_mismatch_is_refused(harness):
    harness.arm()
    intent = make_intent(account_currency="EUR")
    assert _refusal(harness, allow(intent), intent) == "mt5_account_currency_mismatch"


def test_price_drift_is_refused(harness):
    harness.arm()
    intent = make_intent(executable_price=Decimal("1.20000"))
    assert _refusal(harness, allow(intent), intent) == "mt5_price_drift_exceeded"
    assert harness.fake.send_requests == []


def test_stale_quote_is_refused(harness):
    harness.arm()
    intent = make_intent()
    with pytest.raises(MT5SubmitRefused) as excinfo:
        harness.adapter.submit(
            allow(intent), intent, spec=make_spec(), max_quote_age_seconds=Decimal("5")
        )
    assert excinfo.value.reason == "mt5_quote_stale"


def test_insufficient_free_margin_is_refused():
    instance = build_harness(fake=FakeMT5(margin_free=1.0))
    try:
        instance.arm()
        assert (
            _refusal(instance, allow(make_intent()), make_intent())
            == "mt5_insufficient_free_margin"
        )
        assert instance.fake.send_requests == []
    finally:
        instance.adapter.disconnect(reason="test")
        instance.repository.close()


def test_failed_order_check_sends_nothing(harness):
    harness.arm()
    harness.fake.check_result = {"retcode": 10019, "comment": "no money", "margin": 0}
    assert _refusal(harness, allow(make_intent()), make_intent()) == "mt5_order_check_refused"
    assert harness.fake.send_requests == []
    assert harness.repository.get_order("req-1") is None
    assert harness.audit.find(event_type="MT5_ORDER_CHECK_REFUSED")


def test_broker_terms_mismatch_stops_the_order(harness):
    harness.fake.symbols["EURUSD.pro"]["trade_contract_size"] = 1000.0
    harness.arm()
    assert _refusal(harness, allow(make_intent()), make_intent()) == "mt5_symbol_spec_mismatch"
    assert harness.fake.send_requests == []


# -- submission happy path -------------------------------------------------


def test_live_order_is_written_ahead_then_submitted_once(harness):
    harness.arm()
    observed: list[str | None] = []

    def on_send(request):
        record = harness.repository.get_order("req-1")
        observed.append(record["state"] if record else None)

    harness.fake.on_send = on_send
    intent = make_intent()
    receipt = harness.adapter.submit(allow(intent), intent, spec=make_spec())

    assert observed == ["SUBMITTING"]
    assert len(harness.fake.send_requests) == 1
    request = harness.fake.send_requests[0]
    assert request["symbol"] == "EURUSD.pro"
    assert request["volume"] == 0.1
    assert request["comment"] == "req-1"
    assert request["type_filling"] in (0, 1, 2)
    assert receipt.state == "FILLED"
    assert receipt.live is True
    assert receipt.account_login == 55500123
    stored = harness.repository.get_order("req-1")
    assert stored["state"] == "FILLED"
    assert stored["broker_id"] == "mt5_terminal"
    kinds = [event["event_type"] for event in harness.audit.events]
    assert "MT5_ORDER_SUBMITTING" in kinds
    assert "MT5_ORDER_RESULT" in kinds


def test_live_order_audit_and_receipts_carry_no_secret(harness):
    vault = FakeVault()
    harness.adapter.password_store = MT5PasswordStore(
        settings=harness.settings, vault=vault, audit_log=harness.audit
    )
    harness.adapter.password_store.save(PASSWORD)
    harness.arm()
    intent = make_intent()
    receipt = harness.adapter.submit(allow(intent), intent, spec=make_spec())
    blobs = [
        json.dumps(receipt.audit_view(), default=str),
        json.dumps(harness.audit.events, default=str),
        json.dumps(harness.repository.list_orders(), default=str),
        json.dumps(harness.adapter.status(), default=str),
    ]
    for blob in blobs:
        assert PASSWORD not in blob


def test_partial_fill_is_persisted_and_blocks_further_orders(harness):
    harness.arm()
    harness.fake.send_script = [
        {"retcode": 10010, "order": 7001, "deal": 8001, "volume": 0.04, "price": 1.1}
    ]
    intent = make_intent()
    receipt = harness.adapter.submit(allow(intent), intent, spec=make_spec())
    assert receipt.state == "PARTIAL"
    assert harness.repository.get_order("req-1")["state"] == "PARTIAL"
    assert harness.adapter.unresolved_request_ids() == ["req-1"]

    follow_up = make_intent(request_id="req-2")
    assert _refusal(harness, allow(follow_up), follow_up) == "mt5_unresolved_order_present"
    assert len(harness.fake.send_requests) == 1


def test_unknown_outcome_disarms_and_is_never_resent(harness):
    harness.arm()
    harness.fake.send_script = ["raise"]
    intent = make_intent()
    receipt = harness.adapter.submit(allow(intent), intent, spec=make_spec())
    assert receipt.state == "UNRESOLVED"
    assert harness.adapter.armed is False
    assert harness.repository.get_order("req-1")["state"] == "UNRESOLVED"
    assert harness.audit.find(event_type="MT5_TRADING_DISARMED")
    assert len(harness.fake.send_requests) == 1

    second = make_intent(request_id="req-2")
    assert _refusal(harness, allow(second), second) == "mt5_trading_not_armed"
    assert len(harness.fake.send_requests) == 1


# -- recovery ---------------------------------------------------------------


def _unresolved_record(**overrides) -> dict:
    record = {
        "broker_id": "mt5_terminal",
        # Order records are account-scoped; recovery only looks at the account
        # the terminal currently has selected.
        "account_login": 55500123,
        "request_id": "req-1",
        "simulation_id": "",
        "state": "UNRESOLVED",
        "side": "BUY",
        "pair": "EUR/USD",
        "provider_symbol": "EURUSD.pro",
        "volume": "0.1",
        "magic": 26081001,
        "comment": "req-1",
        "sent_at": AT - timedelta(minutes=1),
        "updated_at": AT - timedelta(minutes=1),
        "live": True,
        "order_ticket": 0,
    }
    record.update(overrides)
    return record


def _frozen_clock_adapter(fake: FakeMT5, repository: ForexRepository):
    audit = InMemoryAuditLog()
    settings = mt5_settings()
    adapter = MT5TradingAdapter(
        settings=settings,
        password_store=MT5PasswordStore(settings=settings, vault=FakeVault()),
        repository=repository,
        audit_log=audit,
        module=fake,
        lifecycle_lookup=lambda strategy_id: APPROVED,
        clock=lambda: AT,
    )
    return adapter, audit


def test_recovery_resolves_from_deal_history():
    fake = FakeMT5(
        history_deals=[
            make_history_deal(
                ticket=9001,
                order=7001,
                position_id=42,
                symbol="EURUSD.pro",
                side="BUY",
                entry=0,
                volume=0.1,
                price=1.1,
                magic=26081001,
                comment="req-1",
                time=AT - timedelta(seconds=30),
            )
        ]
    )
    repository = ForexRepository(":memory:")
    repository.save_orders([_unresolved_record()])
    adapter, audit = _frozen_clock_adapter(fake, repository)
    try:
        adapter.connect()
        stored = repository.get_order("req-1")
        assert stored["state"] == "FILLED"
        assert stored["recovery"]["evidence"] == "deal_history"
        assert stored["resend_allowed"] is False
        assert adapter.unresolved_request_ids() == []
        assert audit.find(event_type="MT5_ORDER_RECOVERED")
    finally:
        adapter.disconnect(reason="test")
        repository.close()


def test_recovery_marks_a_cancelled_order_rejected():
    fake = FakeMT5(
        history_orders=[
            make_history_order(
                ticket=7001,
                symbol="EURUSD.pro",
                side="BUY",
                volume=0.1,
                state=2,  # CANCELED
                magic=26081001,
                comment="req-1",
                time=AT - timedelta(seconds=30),
            )
        ]
    )
    repository = ForexRepository(":memory:")
    repository.save_orders([_unresolved_record(order_ticket=7001)])
    adapter, _ = _frozen_clock_adapter(fake, repository)
    try:
        adapter.connect()
        stored = repository.get_order("req-1")
        assert stored["state"] == "REJECTED"
        assert stored["recovery"]["evidence"] == "order_history_canceled"
    finally:
        adapter.disconnect(reason="test")
        repository.close()


def test_recovery_concludes_not_placed_only_from_a_covered_window():
    fake = FakeMT5()
    repository = ForexRepository(":memory:")
    repository.save_orders([_unresolved_record()])
    adapter, _ = _frozen_clock_adapter(fake, repository)
    try:
        adapter.connect()
        stored = repository.get_order("req-1")
        assert stored["state"] == "RESOLVED_NOT_PLACED"
        assert stored["recovery"]["evidence"] == "history_absence"
        assert stored["recovery"]["resend_allowed"] is False
    finally:
        adapter.disconnect(reason="test")
        repository.close()


def test_recovery_stays_unresolved_when_history_is_unavailable():
    fake = FakeMT5()
    fake.fail_history = True
    repository = ForexRepository(":memory:")
    repository.save_orders([_unresolved_record()])
    adapter, _ = _frozen_clock_adapter(fake, repository)
    try:
        adapter.connect()
        stored = repository.get_order("req-1")
        assert stored["state"] == "UNRESOLVED"
        assert stored["recovery"]["evidence"] == "history_unavailable"
        assert adapter.unresolved_request_ids() == ["req-1"]
    finally:
        adapter.disconnect(reason="test")
        repository.close()


def test_recovery_keeps_an_open_position_unresolved():
    fake = FakeMT5(
        positions=[
            make_position(ticket=55, symbol="EURUSD.pro", side="BUY", volume=0.1)
        ]
    )
    repository = ForexRepository(":memory:")
    repository.save_orders([_unresolved_record()])
    adapter, _ = _frozen_clock_adapter(fake, repository)
    try:
        adapter.connect()
        stored = repository.get_order("req-1")
        assert stored["state"] == "ACCEPTED"
        assert stored["recovery"]["evidence"] == "open_position_magic_match"
        assert adapter.unresolved_request_ids() == ["req-1"]
    finally:
        adapter.disconnect(reason="test")
        repository.close()


def test_recovery_ignores_paper_records():
    repository = ForexRepository(":memory:")
    repository.save_orders(
        [
            {
                "request_id": "paper-1",
                "simulation_id": "sim-1",
                "state": "SIMULATED",
                "broker_id": "local_paper",
            }
        ]
    )
    adapter, _ = _frozen_clock_adapter(FakeMT5(), repository)
    try:
        adapter.connect()
        assert adapter.unresolved_request_ids() == []
        assert repository.get_order("paper-1")["state"] == "SIMULATED"
    finally:
        adapter.disconnect(reason="test")
        repository.close()


# -- reconciliation --------------------------------------------------------


def test_reconciliation_reports_every_difference():
    broker = [
        make_position(ticket=1, symbol="EURUSD.pro", side="BUY", volume=0.1),
        make_position(ticket=2, symbol="GBPUSD.pro", side="SELL", volume=0.2),
    ]
    ledger = [
        {"position_id": "p1", "pair": "EUR/USD", "side": "BUY", "quantity": "0.1"},
        {"position_id": "p2", "pair": "USD/JPY", "side": "BUY", "quantity": "0.1"},
    ]
    report = reconcile_positions(
        broker,
        ledger,
        pair_for_symbol={"EURUSD.pro": "EURUSD", "GBPUSD.pro": "GBPUSD"},
    )
    assert report.reconciled is False
    assert report.matched[0]["pair"] == "EURUSD"
    assert report.broker_only[0]["pair"] == "GBPUSD"
    assert report.ledger_only[0]["pair"] == "USDJPY"


def test_reconciliation_flags_volume_and_side_mismatch():
    broker = [make_position(ticket=1, symbol="EURUSD.pro", side="BUY", volume=0.2)]
    mapping = {"EURUSD.pro": "EUR/USD"}
    volume = reconcile_positions(
        broker,
        [{"position_id": "p1", "pair": "EUR/USD", "side": "BUY", "quantity": "0.1"}],
        pair_for_symbol=mapping,
    )
    assert volume.volume_mismatch
    side = reconcile_positions(
        broker,
        [{"position_id": "p1", "pair": "EUR/USD", "side": "SELL", "quantity": "0.2"}],
        pair_for_symbol=mapping,
    )
    assert side.side_mismatch


def test_adapter_reconcile_uses_broker_positions(harness):
    harness.fake.positions = [
        make_position(ticket=1, symbol="EURUSD.pro", side="BUY", volume=0.1)
    ]
    harness.adapter.symbol_for(make_spec())
    report = harness.adapter.reconcile(
        [{"position_id": "p1", "pair": "EUR/USD", "side": "BUY", "quantity": "0.1"}]
    )
    assert report.reconciled is True


# -- history ---------------------------------------------------------------


def test_history_reads_orders_and_deals(harness):
    harness.fake.history_deals = [
        make_history_deal(
            ticket=1,
            order=2,
            position_id=3,
            symbol="EURUSD.pro",
            side="BUY",
            entry=0,
            volume=0.1,
            price=1.1,
            magic=26081001,
            comment="req-1",
            time=AT - timedelta(minutes=1),
        )
    ]
    payload = harness.adapter.history(start=AT - timedelta(hours=1), end=AT)
    assert payload["deals"][0]["entry"] == "IN"
    assert payload["deals"][0]["source"] == "mt5_history_deals_get"


# -- external boundary ----------------------------------------------------


def test_boundary_stays_disabled_without_an_adapter():
    from axiom_forex.facade import build_fixture_platform

    platform = build_fixture_platform()
    try:
        boundary = platform.external_broker_boundary
        assert isinstance(boundary, ExternalBrokerBoundary)
        assert boundary.enabled is False
        view = boundary.audit_view()
        assert view["mt5_attached"] is False
        assert view["mt5_status"]["reason"] == "no_mt5_adapter_attached"
    finally:
        platform.close()


def test_boundary_with_an_adapter_still_refuses_an_intent(harness):
    from app.execution.broker import BrokerCapabilities

    capabilities = BrokerCapabilities(
        broker_id="mt5_terminal",
        capability_version="1",
        execution_mode="LOCAL_PAPER",
        supports_live_orders=False,
        supports_partial_fills=True,
        order_types=frozenset({"SIMULATED_INTENT"}),
        calendar_id="CAL-MT5",
    )
    boundary = ExternalBrokerBoundary(
        capabilities=capabilities, enabled=True, mt5=harness.adapter
    )
    view = boundary.audit_view()
    assert view["mt5_attached"] is True
    assert view["mt5"]["connected"] is True
    assert view["mt5"]["token_required_by_broker"] is False

    from app.risk.models import OrderRequest

    request = OrderRequest(
        request_id="req-x",
        provider_id="mt5",
        provider_symbol="EURUSD.pro",
        side="BUY",
        quantity="0.1",
        target_mode="PAPER",
        evaluated_at=AT,
        quote=None,
        strategy_id="strat-1",
    )
    with pytest.raises(TradingBlocked, match="requires_gate_decision"):
        boundary.simulate_order_intent(request, None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# arming is bound to the account the operator confirmed
# ---------------------------------------------------------------------------


def test_arming_refuses_a_fingerprint_that_is_not_the_account_in_front_of_you():
    harness = build_harness()
    try:
        with pytest.raises(MT5SubmitRefused) as caught:
            harness.adapter.arm_trading(
                confirm="ENABLE LIVE TRADING",
                account_fingerprint="000000000000",
            )
        assert caught.value.reason == "mt5_arming_account_confirmation_mismatch"
        assert harness.adapter.armed is False
        assert harness.fake.send_requests == []
    finally:
        harness.repository.close()


def test_arming_records_the_confirmed_fingerprint_and_reports_it():
    harness = build_harness()
    try:
        current = harness.adapter.account_fingerprint()
        harness.adapter.arm_trading(
            confirm="ENABLE LIVE TRADING", account_fingerprint=current
        )
        status = harness.adapter.status()
        assert status["armed"] is True
        assert status["armed_account_fingerprint"] == current
        assert status["armed_account_matches_current"] is True
    finally:
        harness.repository.close()


def test_a_switch_after_arming_keeps_arming_but_matches_nothing():
    harness = build_harness()
    try:
        confirmed = harness.adapter.account_fingerprint()
        harness.adapter.arm_trading(
            confirm="ENABLE LIVE TRADING", account_fingerprint=confirmed
        )
        harness.fake.account_login = 99999999
        harness.fake.trade_mode = 2
        harness.adapter.refresh_account()
        # No silent disarm: switching accounts is the operator's own act.
        assert harness.adapter.armed is True
        assert harness.adapter.armed_account_matches_current is False
        # ...and switching back restores the match without re-arming anything.
        harness.fake.account_login = 55500123
        harness.fake.trade_mode = 0
        harness.adapter.refresh_account()
        assert harness.adapter.armed_account_matches_current is True
    finally:
        harness.repository.close()
