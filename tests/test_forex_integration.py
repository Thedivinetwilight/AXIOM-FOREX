"""Focused integration tests for one durable offline paper workflow."""

from __future__ import annotations

import json
from datetime import timedelta

from app.common.types import OrderSide, TradingMode
from app.risk.models import OrderRequest
from axiom_forex import build_fixture_platform
from fixtures.m1 import PROVIDER_ID, PROVIDER_SYMBOL, QUANTITY


def call_app(app, path, *, auth=None, method="GET"):
    captured = {}

    def start_response(status, headers):
        captured["status"] = status
        captured["headers"] = dict(headers)

    environ = {"PATH_INFO": path, "REQUEST_METHOD": method}
    if auth is not None:
        environ["HTTP_AUTHORIZATION"] = auth
    body = b"".join(app(environ, start_response))
    return captured["status"], captured["headers"], body


def test_end_to_end_fixture_paper_workflow_uses_normalized_data_and_one_gate(platform):
    result = platform.run_paper_workflow(workflow_id="integration-end-to-end")

    assert result.executed is True
    assert result.blocked is False
    assert result.reason == "local_paper_fill"
    assert result.order is not None and result.order["state"] == "FILLED"
    assert result.fill is not None
    assert result.signal in {"BUY", "SELL", "EXIT"}
    assert result.qualification["qualified"] is True
    assert result.route["selected"] is True
    assert result.routed_signal["accepted"] is True
    assert result.dataset["digest_valid"] is True
    assert result.quality["usable"] is True
    assert result.health["status"] == "HEALTHY"

    loaded = platform.loaded_dataset
    assert loaded is not None
    assert loaded.normalization_count == len(loaded.bars) + len(loaded.quotes)
    assert platform.provider.quotes == loaded.quotes
    assert platform.manifest.dataset_id == loaded.manifest.dataset_id
    assert platform.paper_execution.unresolved_orders() == ()
    assert platform.context_factory.cost_model is platform.forex_cost_model
    assert platform.status()["strategy_active"] is False

    counts = platform.repository.health()["counts"]
    assert counts["orders"] >= 1
    assert counts["fills"] >= 1
    assert counts["positions"] >= 1
    assert counts["backtests"] >= 1
    assert counts["lifecycle"] >= 2
    assert counts["snapshots"] >= 2
    assert counts["audit_events"] >= 5
    snapshots = platform.repository.list_snapshots(kind="workflow")
    assert snapshots[-1]["workflow_id"] == result.workflow_id
    assert all(
        event.get("live") is not True
        for event in platform.audit_events(limit=1000)
    )


def test_restart_recovers_state_and_unresolved_order_blocks_new_workflow(tmp_path):
    database_path = tmp_path / "integration-paper.db"
    first = build_fixture_platform(database_path=str(database_path))
    try:
        first_result = first.run_paper_workflow(workflow_id="integration-before-restart")
        assert first_result.executed is True
        quote = first.provider.latest_quote(PROVIDER_SYMBOL)
        assert quote is not None
        at = quote.timestamp_utc() + timedelta(milliseconds=200)
        request = OrderRequest(
            request_id="integration-unresolved",
            provider_id=PROVIDER_ID,
            provider_symbol=PROVIDER_SYMBOL,
            side=OrderSide.BUY,
            quantity=QUANTITY,
            target_mode=TradingMode.PAPER,
            evaluated_at=at,
            quote=quote,
            strategy_id="integration-recovery-test",
        )
        simulated = first.paper_execution.submit(
            request, first._gate_context(at=at)
        )
        assert simulated.state == "SIMULATED"
        first.repository.save_orders(first.paper_execution.order_records())
        # Order intents are durable from the moment they are submitted, so the
        # database -- not this engine's in-memory book -- is the record that
        # recovery must reproduce.  Other engines sharing the file contribute
        # their own rows.
        expected_orders = len(first.repository.list_orders())
        # Fills are durable from settlement onward, so the database -- not
        # this engine's memory -- is what recovery reproduces.
        expected_fills = len(first.repository.list_fills())
        expected_positions = len(first.portfolio.positions)
    finally:
        first.close()

    second = build_fixture_platform(database_path=str(database_path))
    try:
        recovery = second.recovery_result
        assert recovery is not None and recovery.recovered is True
        assert recovery.audit_view()["unresolved_order_count"] == 1
        assert len(second.paper_execution.records()) == expected_orders
        assert len(second.paper_execution.fills()) == expected_fills
        assert len(second.portfolio.positions) == expected_positions
        assert len(second.paper_execution.unresolved_orders()) == 1

        blocked = second.run_paper_workflow(workflow_id="integration-after-restart")
        assert blocked.executed is False
        assert blocked.blocked is True
        assert blocked.reason == "unresolved_order_present"
        assert blocked.health["status"] == "UNSAFE"
        assert len(second.paper_execution.records()) == expected_orders
        assert len(second.paper_execution.fills()) == expected_fills
        assert len(second.paper_execution.unresolved_orders()) == 1
        assert second.health().audit_view()["status"] == "UNSAFE"
        assert second.status()["workflow"]["workflow_id"] == blocked.workflow_id
        assert second.workflow_status()["reason"] == "unresolved_order_present"
    finally:
        second.close()


def test_api_dashboard_report_and_desktop_share_one_workflow_snapshot(platform):
    result = platform.run_paper_workflow(workflow_id="integration-consistency")
    app = platform.create_wsgi_app()
    auth = "Bearer " + platform.runtime_settings.api_bearer_token

    health_status, _, health_body = call_app(app, "/health")
    assert health_status.startswith("200")
    health = json.loads(health_body)

    status_code, _, status_body = call_app(app, "/status", auth=auth)
    assert status_code.startswith("200")
    status_payload = json.loads(status_body)

    workflow_code, _, workflow_body = call_app(app, "/workflow", auth=auth)
    assert workflow_code.startswith("200")
    workflow_payload = json.loads(workflow_body)

    desktop_code, _, desktop_body = call_app(app, "/desktop", auth=auth)
    assert desktop_code.startswith("200")
    desktop_payload = json.loads(desktop_body)

    dashboard_code, _, dashboard_body = call_app(app, "/dashboard", auth=auth)
    assert dashboard_code.startswith("200")
    dashboard_html = dashboard_body.decode("utf-8")

    report_code, _, report_body = call_app(app, "/report", auth=auth)
    assert report_code.startswith("200")
    report_markdown = report_body.decode("utf-8")

    identifiers = {
        result.workflow_id,
        status_payload["workflow"]["workflow_id"],
        workflow_payload["workflow_id"],
        desktop_payload["workflow"]["workflow_id"],
        desktop_payload["workflow_detail"]["workflow_id"],
    }
    assert identifiers == {result.workflow_id}
    assert health["status"] == status_payload["health"]["status"]
    assert health["status"] == workflow_payload["health"]["status"]
    assert health["status"] == status_payload["workflow"]["health"]["status"]
    assert result.workflow_id in dashboard_html
    assert health["status"] in dashboard_html
    assert result.workflow_id in report_markdown
    assert health["status"] in report_markdown
    assert "No order controls" in dashboard_html
    assert "not a claim of profitability" in report_markdown
    assert desktop_payload["live_controls"] == []
    assert (
        status_payload["workflow"]["persistence"]["orders"]
        == workflow_payload["persistence"]["orders"]
        == desktop_payload["workflow"]["persistence"]["orders"]
    )
    assert (
        status_payload["workflow"]["persistence"]["fills"]
        == workflow_payload["persistence"]["fills"]
        == desktop_payload["workflow"]["persistence"]["fills"]
    )
    assert platform.runtime_settings.api_bearer_token not in dashboard_html
    assert platform.runtime_settings.api_bearer_token not in report_markdown
