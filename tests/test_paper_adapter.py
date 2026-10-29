import json
from dataclasses import replace

import pytest

from app.common.types import TradingMode
from app.execution.broker import BrokerAdapter, LocalPaperBroker, TradingBlocked
from fixtures.m1 import make_capabilities, make_environment


def test_local_paper_broker_creates_deterministic_simulated_intent():
    first = make_environment()
    second = make_environment()
    receipt_a = first.broker().simulate_order_intent(first.request, first.context())
    receipt_b = second.broker().simulate_order_intent(second.request, second.context())

    assert receipt_a.status == "SIMULATED_INTENT"
    assert receipt_a.live is False
    assert receipt_a.mode == "PAPER"
    assert receipt_a.simulation_id == receipt_b.simulation_id
    assert receipt_a.intent.pair == "AAA/BBB"
    assert isinstance(first.broker(), BrokerAdapter)

    events = first.audit_log.events
    assert events[0]["event_type"] == "PRE_TRADE_DECISION"
    assert events[0]["allowed"] is True
    assert events[-1]["event_type"] == "LOCAL_PAPER_INTENT_SIMULATED"
    assert events[-1]["live"] is False
    json.dumps(events[-1])


def test_local_paper_broker_blocks_live_request_before_gate():
    env = make_environment()
    request = replace(env.request, target_mode=TradingMode.LIVE)
    with pytest.raises(TradingBlocked) as excinfo:
        env.broker().simulate_order_intent(request, env.context())
    assert excinfo.value.reason == "live_trading_forbidden"
    assert env.audit_log.last()["event_type"] == "LOCAL_PAPER_LIVE_REQUEST_BLOCKED"
    assert not env.audit_log.find(event_type="LOCAL_PAPER_INTENT_SIMULATED")


def test_local_paper_broker_blocks_gate_rejection_without_receipt():
    env = make_environment()
    request = replace(env.request, quote=None)
    with pytest.raises(TradingBlocked) as excinfo:
        env.broker().simulate_order_intent(request, env.context())
    assert excinfo.value.reason == "INCOMPLETE_QUOTE"
    assert excinfo.value.decision is not None
    assert env.audit_log.last()["code"] == "INCOMPLETE_QUOTE"
    assert not env.audit_log.find(event_type="LOCAL_PAPER_INTENT_SIMULATED")


def test_local_paper_broker_rejects_live_capabilities():
    env = make_environment()
    capabilities = make_capabilities(supports_live_orders=True)
    with pytest.raises(ValueError):
        LocalPaperBroker(env.gate(), capabilities, env.audit_log)


def test_local_paper_broker_has_no_remote_order_methods():
    env = make_environment()
    broker = env.broker()
    forbidden = {
        "place_order",
        "submit_order",
        "send_order",
        "cancel_order",
        "reconcile",
        "connect",
        "authenticate",
    }
    assert forbidden.isdisjoint(dir(broker))
