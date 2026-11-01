"""Broker-side protective stop loss: sizing, refusal, and verification.

The properties asserted here are the ones an operator relies on:

* a live entry is refused outright unless a stop distance is configured, so no
  unprotected position can be opened by this build;
* the stop price sent to the broker is derived from the symbol's own point size
  and the broker's ``stops_level``, never guessed;
* "protected" is only ever claimed when the broker's position record echoes the
  stop back -- a sent stop the broker ignored is reported as unconfirmed, the
  adapter disarms, and the next entry is refused;
* a position this deployment placed without a stop blocks further entries until
  it is dealt with;
* a close carries no new stop, and a position belonging to another expert
  advisor is not this build's to judge.

Everything runs against ``tests.fake_mt5``.  No terminal, account, or package is
touched, and no real order is sent.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.common.types import OrderSide
from axiom_forex.brokers.mt5 import MT5SubmitRefused
from axiom_forex.brokers.mt5.orders import protective_stop_price, stop_loss_confirmed
from axiom_forex.brokers.mt5.reconcile import reconcile_positions, unprotected_positions
from axiom_forex.brokers.mt5.terminal import MT5SymbolInfo
from tests.fake_mt5 import FakeMT5, make_position
from tests.test_mt5_adapter import (
    AT,
    allow,
    build_harness,
    make_intent,
    make_spec,
)

POINTS = 200  # 200 points at 0.00001 == 20 pips on a 5-digit symbol


def stop_harness(**overrides):
    settings = {"protective_stops": {"EUR/USD": f"{POINTS} points"}}
    settings.update(overrides.pop("settings_overrides", {}))
    return build_harness(settings_overrides=settings, **overrides)


def submit(harness, **intent_overrides):
    intent = make_intent(**intent_overrides)
    return harness.adapter.submit(allow(intent), intent, spec=make_spec())


def audit_types(harness) -> list[str]:
    return [event.get("event_type", "") for event in harness.audit.events]


# -- the distance itself ----------------------------------------------------


@pytest.mark.parametrize(
    ("side", "expected"),
    [("BUY", Decimal("1.09820")), ("SELL", Decimal("1.10200"))],
)
def test_stop_price_is_measured_from_the_executable_side(side, expected):
    price, reason = protective_stop_price(
        side=side,
        reference=Decimal("1.10020") if side == "BUY" else Decimal("1.10000"),
        point=Decimal("0.00001"),
        digits=5,
        points=POINTS,
    )
    assert reason == ""
    assert price == expected


@pytest.mark.parametrize(
    ("points", "point", "stops_level", "reason"),
    [
        (0, Decimal("0.00001"), 0, "mt5_protective_stop_not_configured"),
        (-5, Decimal("0.00001"), 0, "mt5_protective_stop_not_configured"),
        (POINTS, None, 0, "mt5_symbol_point_unavailable"),
        (POINTS, Decimal("0"), 0, "mt5_symbol_point_unavailable"),
        (POINTS, Decimal("0.00001"), 500, "mt5_protective_stop_too_close"),
    ],
)
def test_an_unusable_distance_is_a_named_refusal(points, point, stops_level, reason):
    price, actual = protective_stop_price(
        side="BUY",
        reference=Decimal("1.10020"),
        point=point,
        digits=5,
        points=points,
        stops_level=stops_level,
    )
    assert price is None
    assert actual == reason


def test_a_broker_rounded_stop_is_confirmed_within_one_point():
    position = make_position(
        ticket=1, symbol="EURUSD.pro", side="BUY", volume=0.1, sl=1.09821
    )
    from axiom_forex.brokers.mt5.orders import parse_positions

    parsed = parse_positions([position])[0]
    confirmed, detail = stop_loss_confirmed(
        parsed, Decimal("1.09820"), tolerance=Decimal("0.00001")
    )
    assert confirmed is True
    assert detail == ""


# -- entries -----------------------------------------------------------------


def test_entry_without_a_configured_stop_is_refused_before_anything_is_written():
    harness = stop_harness(settings_overrides={"protective_stops": {}})
    try:
        harness.arm()
        with pytest.raises(MT5SubmitRefused) as excinfo:
            submit(harness)
        assert excinfo.value.reason == "mt5_protective_stop_not_configured_for_symbol"
        assert harness.fake.send_requests == []
        assert harness.repository.list_orders() == []
    finally:
        harness.adapter.disconnect(reason="test")
        harness.repository.close()


def test_a_pip_distance_is_converted_with_the_symbols_own_point():
    harness = stop_harness(settings_overrides={"protective_stops": {"EUR/USD": "20 pips"}})
    try:
        harness.arm()
        submit(harness)
        assert harness.fake.send_requests[0]["sl"] == pytest.approx(1.09820)
        record = harness.repository.list_orders()[0]
        assert record["protective_stop"]["declared"] == "20 pips"
        assert record["protective_stop"]["points"] == 200
    finally:
        harness.adapter.disconnect(reason="test")
        harness.repository.close()


def test_the_legacy_global_distance_is_never_used_for_an_entry():
    harness = stop_harness(
        settings_overrides={"protective_stops": {}, "protective_stop_points": 200}
    )
    try:
        harness.arm()
        requirement = harness.adapter.stop_requirement(make_spec())
        assert requirement["legacy_global_points_ignored"] == 200
        assert requirement["valid"] is False
        with pytest.raises(MT5SubmitRefused) as excinfo:
            submit(harness)
        assert excinfo.value.reason == "mt5_protective_stop_not_configured_for_symbol"
        assert "EUR/USD" in excinfo.value.detail
        assert harness.fake.send_requests == []
    finally:
        harness.adapter.disconnect(reason="test")
        harness.repository.close()


def test_a_distance_for_one_pair_does_not_configure_another():
    harness = stop_harness(
        settings_overrides={"protective_stops": {"GBP/USD": "30 pips"}}
    )
    try:
        harness.arm()
        eur = harness.adapter.stop_requirement(make_spec())
        gbp = harness.adapter.stop_requirement(make_spec(pair="GBP/USD"))
        assert eur["valid"] is False and eur["declared"] == ""
        assert gbp["declared"] == "30 pips"
        with pytest.raises(MT5SubmitRefused):
            submit(harness)
    finally:
        harness.adapter.disconnect(reason="test")
        harness.repository.close()


def test_a_bare_number_without_a_unit_is_refused():
    # Typed into a control: judged by the same code, and refused for want of a unit.
    harness = stop_harness()
    try:
        harness.arm()
        requirement = harness.adapter.stop_requirement(make_spec(), text="200")
        assert requirement["valid"] is False
        assert requirement["reason"] == "mt5_protective_stop_invalid"
        assert "points or pips" in requirement["detail"]
    finally:
        harness.adapter.disconnect(reason="test")
        harness.repository.close()


def test_a_settings_document_with_an_unreadable_distance_is_refused_at_connect():
    # A hand-edited file is not repaired or partly believed.
    with pytest.raises(MT5SubmitRefused) as excinfo:
        stop_harness(settings_overrides={"protective_stops": {"EUR/USD": "200"}})
    assert excinfo.value.reason == "mt5_settings_invalid"
    assert "invalid_protective_stop_value:EUR/USD" in excinfo.value.detail


def test_a_pip_distance_that_is_not_whole_points_is_refused_not_rounded():
    harness = stop_harness(
        settings_overrides={"protective_stops": {"EUR/USD": "0.05 pips"}}
    )
    try:
        harness.arm()
        requirement = harness.adapter.stop_requirement(make_spec())
        assert requirement["reason"] == "mt5_protective_stop_not_on_point_grid"
        with pytest.raises(MT5SubmitRefused) as excinfo:
            submit(harness)
        assert excinfo.value.reason == "mt5_protective_stop_not_on_point_grid"
        assert harness.fake.send_requests == []
    finally:
        harness.adapter.disconnect(reason="test")
        harness.repository.close()


def test_the_requirement_reports_what_the_control_has_to_show():
    harness = stop_harness()
    try:
        harness.arm()
        requirement = harness.adapter.stop_requirement(make_spec())
        assert requirement["pair"] == "EUR/USD"
        assert requirement["broker_symbol"] == "EURUSD.pro"
        assert requirement["declared"] == "200 points"
        assert requirement["distance"] == "200"
        assert requirement["unit"] == "points"
        assert requirement["point"] == "0.00001"
        assert requirement["pip_size"] == "0.0001"
        assert requirement["broker_min_points"] == 0
        assert requirement["broker_min_price"] == "0"
        assert requirement["distance_price"] == "0.002"
        assert requirement["valid"] is True
    finally:
        harness.adapter.disconnect(reason="test")
        harness.repository.close()


def test_a_preview_value_is_judged_without_touching_the_settings():
    harness = stop_harness()
    try:
        harness.arm()
        preview = harness.adapter.stop_requirement(make_spec(), text="15 pips")
        assert preview["valid"] is True
        assert preview["distance_points"] == 150
        assert preview["source"] == "operator_input"
        # The configured value is unchanged by asking about another one.
        assert harness.adapter.stop_requirement(make_spec())["declared"] == "200 points"
    finally:
        harness.adapter.disconnect(reason="test")
        harness.repository.close()


def test_entry_request_carries_the_stop_and_the_broker_echo_is_verified():
    harness = stop_harness()
    try:
        harness.arm()
        receipt = submit(harness)
        request = harness.fake.send_requests[0]
        assert request["sl"] == pytest.approx(1.09820)
        assert receipt.state == "FILLED"
        record = harness.repository.list_orders()[0]
        assert record["protective_stop"]["requested"] == "1.09820"
        assert record["protective_stop"]["points"] == POINTS
        assert record["protective_stop"]["verified"] is True
        assert Decimal(record["protective_stop"]["broker_stop_loss"]) == Decimal(
            "1.09820"
        )
        assert "MT5_ORDER_STOP_VERIFIED" in audit_types(harness)
        assert harness.adapter.armed is True
    finally:
        harness.adapter.disconnect(reason="test")
        harness.repository.close()


def test_sell_entry_places_its_stop_above_the_bid():
    harness = stop_harness()
    try:
        harness.arm()
        submit(
            harness,
            side=OrderSide.SELL,
            executable_price=Decimal("1.10000"),
            request_id="req-sell",
        )
        request = harness.fake.send_requests[0]
        assert request["sl"] == pytest.approx(1.10200)
    finally:
        harness.adapter.disconnect(reason="test")
        harness.repository.close()


def test_a_stop_inside_the_brokers_minimum_distance_is_refused():
    harness = stop_harness(
        fake=FakeMT5(symbols={"EURUSD.pro": {"trade_stops_level": 500}})
    )
    try:
        harness.arm()
        with pytest.raises(MT5SubmitRefused) as excinfo:
            submit(harness)
        assert excinfo.value.reason == "mt5_protective_stop_below_broker_minimum"
        assert harness.fake.send_requests == []
    finally:
        harness.adapter.disconnect(reason="test")
        harness.repository.close()


def test_a_stop_the_broker_ignores_is_unconfirmed_and_disarms():
    harness = stop_harness(fake=FakeMT5())
    harness.fake.mirror_stop = False
    try:
        harness.arm()
        receipt = submit(harness)
        # The order itself did fill; that is reported honestly.
        assert receipt.state == "FILLED"
        record = harness.repository.list_orders()[0]
        stop = record["protective_stop"]
        assert stop["verified"] is False
        assert stop["detail"] == "broker_reports_zero_stop_loss"
        assert Decimal(stop["broker_stop_loss"]) == Decimal("0")
        assert harness.adapter.armed is False
        assert "MT5_ORDER_STOP_UNCONFIRMED" in audit_types(harness)
        assert "MT5_ORDER_STOP_VERIFIED" not in audit_types(harness)
    finally:
        harness.adapter.disconnect(reason="test")
        harness.repository.close()


def test_a_position_without_a_stop_blocks_the_next_entry():
    fake = FakeMT5(
        positions=[
            make_position(ticket=5001, symbol="EURUSD.pro", side="BUY", volume=0.1)
        ]
    )
    harness = stop_harness(fake=fake)
    try:
        harness.arm()
        with pytest.raises(MT5SubmitRefused) as excinfo:
            submit(harness)
        assert excinfo.value.reason == "mt5_unprotected_position_present"
        assert "5001" in excinfo.value.detail
        assert harness.fake.send_requests == []
    finally:
        harness.adapter.disconnect(reason="test")
        harness.repository.close()


def test_a_protected_position_does_not_block_the_next_entry():
    fake = FakeMT5(
        positions=[
            make_position(
                ticket=5002, symbol="EURUSD.pro", side="BUY", volume=0.1, sl=1.0982
            )
        ]
    )
    harness = stop_harness(fake=fake)
    try:
        harness.arm()
        receipt = submit(harness)
        assert receipt.state == "FILLED"
    finally:
        harness.adapter.disconnect(reason="test")
        harness.repository.close()


def test_another_expert_advisors_position_is_not_ours_to_block_on():
    fake = FakeMT5(
        positions=[
            make_position(
                ticket=5003,
                symbol="EURUSD.pro",
                side="BUY",
                volume=0.1,
                magic=999,
            )
        ]
    )
    harness = stop_harness(fake=fake)
    try:
        harness.arm()
        assert harness.adapter.unprotected_positions() == []
        receipt = submit(harness)
        assert receipt.state == "FILLED"
    finally:
        harness.adapter.disconnect(reason="test")
        harness.repository.close()


def test_a_close_never_carries_a_stop():
    fake = FakeMT5(
        positions=[
            make_position(
                ticket=5004, symbol="EURUSD.pro", side="BUY", volume=0.1, sl=1.0982
            )
        ]
    )
    harness = stop_harness(fake=fake)
    try:
        harness.arm()
        receipt = submit(
            harness,
            side=OrderSide.SELL,
            executable_price=Decimal("1.10000"),
            close_position_id="5004",
            exposure_reducing=True,
            request_id="req-close",
        )
        request = harness.fake.send_requests[0]
        assert "sl" not in request
        assert request["position"] == 5004
        assert receipt.state == "FILLED"
        record = harness.repository.list_orders()[0]
        assert record["protective_stop"]["detail"] == "not_an_entry"
    finally:
        harness.adapter.disconnect(reason="test")
        harness.repository.close()


def test_the_entry_after_an_unconfirmed_stop_is_refused():
    """The requirement, end to end: no stop, then no second position.

    The first order fills; the broker ignores the stop.  Arming again does not
    help: the unprotected position is what blocks the next entry.
    """

    harness = stop_harness(fake=FakeMT5())
    harness.fake.mirror_stop = False
    try:
        harness.arm()
        submit(harness, request_id="req-first")
        harness.arm()
        with pytest.raises(MT5SubmitRefused) as excinfo:
            submit(harness, request_id="req-second")
        assert excinfo.value.reason == "mt5_unprotected_position_present"
        assert len(harness.fake.send_requests) == 1
    finally:
        harness.adapter.disconnect(reason="test")
        harness.repository.close()


# -- reconciliation ---------------------------------------------------------


def test_reconciliation_reports_an_unprotected_position_as_a_defect():
    broker = [
        make_position(ticket=6001, symbol="EURUSD.pro", side="BUY", volume=0.2),
        make_position(
            ticket=6002, symbol="EURUSD.pro", side="BUY", volume=0.1, sl=1.0982
        ),
    ]
    ledger = [
        {"position_id": "6001", "pair": "EUR/USD", "quantity": "0.2", "side": "BUY"},
        {"position_id": "6002", "pair": "EUR/USD", "quantity": "0.1", "side": "BUY"},
    ]
    result = reconcile_positions(
        broker,
        ledger,
        pair_for_symbol={"EURUSD.pro": "EUR/USD"},
        magic=26081001,
    )
    assert result.reconciled is True
    assert result.stops_confirmed is False
    assert [row["ticket"] for row in result.unprotected] == [6001]
    view = result.audit_view()
    assert view["stops_confirmed"] is False
    assert view["unprotected_positions"][0]["reason"] == "stop_loss_missing"


def test_reconciliation_confirms_stops_when_every_position_has_one():
    broker = [
        make_position(
            ticket=6003, symbol="EURUSD.pro", side="SELL", volume=0.1, sl=1.102
        )
    ]
    result = reconcile_positions(
        broker,
        [{"position_id": "6003", "pair": "EUR/USD", "quantity": "0.1", "side": "SELL"}],
        pair_for_symbol={"EURUSD.pro": "EUR/USD"},
        magic=26081001,
    )
    assert result.stops_confirmed is True
    assert result.audit_view()["unprotected_positions"] == []


def test_unprotected_positions_ignores_positions_of_other_magics():
    from axiom_forex.brokers.mt5.orders import parse_positions

    parsed = parse_positions(
        [
            make_position(ticket=7001, symbol="EURUSD.pro", side="BUY", volume=0.1),
            make_position(
                ticket=7002, symbol="EURUSD.pro", side="BUY", volume=0.1, magic=42
            ),
        ]
    )
    assert [row["ticket"] for row in unprotected_positions(parsed, magic=26081001)] == [
        7001
    ]


def test_symbol_info_carries_the_stops_level_the_check_reads():
    info = MT5SymbolInfo(
        name="EURUSD.pro",
        description="Euro vs US Dollar",
        digits=5,
        point=Decimal("0.00001"),
        trade_contract_size=Decimal("100000"),
        volume_min=Decimal("0.01"),
        volume_max=Decimal("100"),
        volume_step=Decimal("0.01"),
        currency_base="EUR",
        currency_profit="USD",
        currency_margin="EUR",
        trade_mode=4,
        filling_mask=1,
        spread=12,
        stops_level=0,
        freeze_level=0,
        bid=Decimal("1.10000"),
        ask=Decimal("1.10020"),
        time=AT,
    )
    price, reason = protective_stop_price(
        side="BUY",
        reference=info.ask,
        point=info.point,
        digits=info.digits,
        points=POINTS,
        stops_level=info.stops_level,
    )
    assert price == Decimal("1.09820")
    assert reason == ""
