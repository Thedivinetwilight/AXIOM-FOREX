from datetime import timedelta

import pytest

from app.common.types import OrderSide, TradingMode
from app.execution.broker import TradingBlocked
from app.risk.models import OrderRequest
from axiom_forex.brokers.external import conformance_checklist
from fixtures.m1 import PROVIDER_ID, PROVIDER_SYMBOL, QUANTITY


def _paper_request(platform, suffix, *, target_mode=TradingMode.PAPER):
    quote = platform.provider.latest_quote(PROVIDER_SYMBOL)
    assert quote is not None
    at = quote.timestamp_utc() + timedelta(milliseconds=200)
    request = OrderRequest(
        request_id=f"paper-{suffix}",
        provider_id=PROVIDER_ID,
        provider_symbol=PROVIDER_SYMBOL,
        side=OrderSide.BUY,
        quantity=QUANTITY,
        target_mode=target_mode,
        evaluated_at=at,
        quote=quote,
        strategy_id="paper-execution-test",
    )
    return request, quote, at


def test_paper_execution_is_idempotent(platform):
    request, quote, at = _paper_request(platform, "idempotent")
    context = platform._gate_context(at=at)
    record, fill = platform.paper_execution.execute(
        request,
        context,
        fill_price=quote.ask,
        fill_at=at,
        action="OPEN",
    )
    assert record.state == "FILLED"
    assert fill is not None
    assert len(platform.paper_execution.fills()) == 1

    duplicate, duplicate_fill = platform.paper_execution.execute(
        request,
        context,
        fill_price=quote.ask,
        fill_at=at,
        action="OPEN",
    )
    assert duplicate.simulation_id == record.simulation_id
    assert duplicate_fill == fill
    assert len(platform.paper_execution.fills()) == 1
    assert len(platform.paper_execution.records()) == 1


def test_unresolved_simulated_order_blocks_new_orders(platform):
    first, _, at = _paper_request(platform, "unresolved-first")
    second, _, _ = _paper_request(platform, "unresolved-second")
    context = platform._gate_context(at=at)
    simulated = platform.paper_execution.submit(first, context)
    assert simulated.state == "SIMULATED"
    assert len(platform.paper_execution.unresolved_orders()) == 1

    blocked = platform.paper_execution.submit(second, context)
    assert blocked.state == "REJECTED"
    assert blocked.reason == "UNRESOLVED_ORDER_PRESENT"
    assert len(platform.paper_execution.fills()) == 0


def test_settlement_failure_leaves_order_unresolved_and_blocks_retry(platform):
    request, quote, at = _paper_request(platform, "settlement-failure")
    context = platform._gate_context(at=at)
    record, fill = platform.paper_execution.execute(
        request,
        context,
        fill_price=quote.ask,
        fill_at=at,
        action="NOT_A_VALID_ACTION",
    )
    assert record.state == "UNRESOLVED"
    assert fill is None
    assert "settlement_error" in record.reason
    assert platform.audit_log.find(event_type="PAPER_ORDER_UNRESOLVED")

    retry, _, _ = _paper_request(platform, "settlement-retry")
    blocked = platform.paper_execution.submit(retry, context)
    assert blocked.state == "REJECTED"
    assert blocked.reason == "UNRESOLVED_ORDER_PRESENT"


def test_live_request_is_blocked_and_audited(platform):
    request, quote, at = _paper_request(
        platform, "live", target_mode=TradingMode.LIVE
    )
    context = platform._gate_context(at=at)
    record, fill = platform.paper_execution.execute(
        request,
        context,
        fill_price=quote.ask,
        fill_at=at,
        action="OPEN",
    )
    assert record.state == "REJECTED"
    assert record.reason == "live_trading_forbidden"
    assert fill is None
    assert platform.audit_log.find(event_type="LOCAL_PAPER_LIVE_REQUEST_BLOCKED")
    assert platform.paper_execution.fills() == ()


def test_external_broker_boundary_is_disabled_and_documented(platform):
    boundary = platform.external_broker_boundary
    assert boundary.enabled is False
    assert boundary.audit_view()["enabled"] is False
    assert any("triple" in item for item in conformance_checklist())
    request, _, at = _paper_request(platform, "external")
    context = platform._gate_context(at=at)
    with pytest.raises(TradingBlocked, match="external_broker_boundary_disabled"):
        boundary.simulate_order_intent(request, context)
