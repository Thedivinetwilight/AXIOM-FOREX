"""Fake MT5 only: history evidence must identify one original request."""
from __future__ import annotations

import pytest
from axiom_forex.database.repository import ForexRepository
from axiom_forex.scalper.history_recovery import inspect_exact_history
from tests.fake_mt5 import FakeMT5, make_history_deal, make_history_order, make_position
from tests.test_mt5_adapter import AT, _frozen_clock_adapter, _unresolved_record


@pytest.fixture
def evidence():
    deal = make_history_deal(ticket=9001, order=7001, position_id=9201, symbol="EURUSD.pro",
        side="BUY", entry=0, volume=0.1, price=1.1, magic=26081001, comment="", time=AT)
    position = make_position(ticket=9101, identifier=9201, symbol="EURUSD.pro", side="BUY",
        volume=0.1, price_open=1.1, magic=26081001, sl=1.09)
    fake = FakeMT5(history_deals=[deal], positions=[position])
    repo = ForexRepository(":memory:")
    adapter, _ = _frozen_clock_adapter(fake, repo)
    adapter.connect()
    record = _unresolved_record(order_ticket=7001, strategy_id="ax-scalper-live:strategy")
    identity = {"symbol": "EURUSD.pro", "account_fingerprint": record["account_fingerprint"],
                "account_server": record["account_server"], "account_currency": "USD", "account_mode": "DEMO"}
    row = {"request_id": "req-1", "action": "entry", "identity": identity,
           "intent": {"request_id": "req-1", "symbol": "EUR/USD", "side": "BUY", "lots": "0.1",
                      "stop_price": "1.09", "target_price": None}}
    yield fake, adapter, record, row
    adapter.disconnect(reason="test")
    repo.close()


def query(evidence, account=lambda: True):
    fake, adapter, record, row = evidence
    return inspect_exact_history(adapter=adapter, order=record, journal=row, account_matches=account)


def test_entry_requires_deal_identifier_position_and_exact_protection(evidence):
    fake, _, _, _ = evidence
    proof = query(evidence)
    assert proof["resolved"] and proof["ticket"] == "9101" and proof["position_identifier"] == 9201
    fake.positions[0]["sl"] = 1.08
    assert not query(evidence)["resolved"]
    assert not fake.send_requests


@pytest.mark.parametrize("damage", ["duplicate_deal", "different_order", "missing_ticket", "wrong_account", "missing_position_id"])
def test_ambiguous_entry_never_becomes_proof(evidence, damage):
    fake, _, order, row = evidence
    if damage == "duplicate_deal": fake.history_deals.append(dict(fake.history_deals[0]))
    if damage == "different_order": fake.history_deals[0]["order"] = 7002
    if damage == "missing_ticket": order["order_ticket"] = 0
    if damage == "wrong_account": row["identity"]["account_server"] = "other"
    if damage == "missing_position_id": fake.positions[0]["identifier"] = 0
    assert not query(evidence)["resolved"]
    assert not fake.send_requests


def test_unique_broker_refusal_is_proof_but_absence_is_not(evidence):
    fake, _, _, _ = evidence
    fake.history_deals.clear()
    fake.positions.clear()
    assert not query(evidence)["resolved"]
    fake.history_orders.append(make_history_order(ticket=7001, symbol="EURUSD.pro", side="BUY",
        volume=0.1, state=5, magic=26081001, comment="", time=AT))
    proof = query(evidence)
    assert proof["resolved"] and proof["state"] == "REJECTED", proof
    assert not fake.send_requests


def test_exact_close_uses_deal_position_identifier_and_remaining_position(evidence):
    fake, _, record, row = evidence
    fake.history_deals[0].update(type=1, entry=1)
    fake.positions.clear()
    record.update(side="SELL", close_position_id="mt5:9101", close_position_ticket=9101,
                  close_position_identifier=9201)
    row.update(action="close", intent={"request_id": "req-1", "position_id": "9101", "requested_lots": "0.1",
        "prior_position": {"position_id": "9101", "side": "BUY", "lots": "0.1", "entry_price": "1.1",
                           "stop_price": "1.09", "target_price": None}})
    proof = query(evidence)
    assert proof["resolved"] and proof["state"] == "FILLED", proof
    record["close_position_identifier"] = 9101
    assert not query(evidence)["resolved"]
    assert not fake.send_requests


def test_contradictory_receipt_or_account_switch_never_resolves(evidence):
    fake, _, order, _ = evidence
    order["classification"] = "REJECTED"
    assert not query(evidence)["resolved"]
    order["classification"] = "UNKNOWN"
    checks = iter([True, False])
    assert not query(evidence, account=lambda: next(checks))["resolved"]
    fake.history_deals[0]["type"] = True
    assert not query(evidence)["resolved"]
    assert not fake.send_requests
