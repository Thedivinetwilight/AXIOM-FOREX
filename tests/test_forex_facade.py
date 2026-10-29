import json

import pytest

from axiom_forex.boundaries import DEFAULT_BOUNDARIES, INTEGRATION_BOUNDARIES
from axiom_forex.cli import main
from axiom_forex.config.runtime import ForexRuntimeSettings
from axiom_forex.dependencies import OPEN_DECISIONS, decision_index
from axiom_forex.strategies.examples import SmaCrossStrategy
from axiom_forex.tournament.runner import Candidate


def _strategy():
    return SmaCrossStrategy(fast_period=3, slow_period=8, name="fixture-sma-cross")


def test_fixture_platform_verify_is_structural_and_paper_only(platform):
    result = platform.verify()
    assert result["overall"] is True
    assert all(result["checks"].values())
    assert result["health"]["status"] == "HEALTHY"
    assert result["checks"]["live_bridge_disabled"] is True
    assert result["checks"]["external_broker_disabled"] is True
    assert result["checks"]["no_active_strategy"] is True
    assert "not a profitability claim" in result["note"]
    assert result["backtest"]["usable"] is True


def test_status_includes_architecture_boundaries_and_dependencies(platform):
    status = platform.status()
    expected_keys = {
        "version",
        "settings",
        "safety",
        "boundaries",
        "open_decisions",
        "health",
        "calendar",
        "dataset",
        "data_quality",
        "portfolio",
        "aggregate_risk",
        "provider",
        "repository",
        "backtests",
        "orders",
        "audit_events",
        "lifecycle",
        "strategy_active",
        "external_broker",
    }
    assert expected_keys <= set(status)
    assert status["strategy_active"] is False
    assert status["external_broker"]["enabled"] is False
    assert status["repository"]["database_path"] == ":memory:"
    assert len(status["open_decisions"]) == len(OPEN_DECISIONS)
    assert status["aggregate_risk"]["allowed"] is True


def test_run_backtest_persists_result_orders_and_audit(platform):
    result = platform.run_backtest(_strategy(), run_id="persisted-run")
    assert result.usable
    assert result.cost_total_account >= 0
    assert len(platform.backtests()) == 1
    assert platform.backtests()[0]["run_id"] == "persisted-run"
    assert platform.repository.health()["counts"]["backtests"] == 1
    assert platform.repository.health()["counts"]["audit_events"] >= 1
    assert not any(
        event.get("live") is True for event in platform.audit_events(limit=200)
    )


def test_run_oos_and_tournament_do_not_select_unapproved_strategy(platform):
    bars = platform.bars
    validation = platform.run_oos(
        _strategy(),
        train_bars=bars[:20],
        validation_bars=bars[20:30],
        test_bars=bars[30:],
    )
    assert validation.test_used
    assert validation.result.out_of_sample
    assert validation.result.quality.usable
    assert platform.paper_execution.records() == ()

    tournament = platform.run_tournament(
        candidates=[Candidate("fixture-sma-cross", 3, 8)],
        train_bars=bars[:20],
        validation_bars=bars[20:30],
        test_bars=bars[30:],
    )
    assert tournament.test_used
    assert tournament.selected is None
    assert tournament.router_reason == "no_qualified_candidate"
    assert tournament.candidates[0].lifecycle_stage == "RESEARCH"
    assert tournament.candidates[0].selected is False
    assert platform.status()["strategy_active"] is False


def test_new_engine_is_clean_and_reuse_fails_closed(platform):
    first = platform.new_engine()
    second = platform.new_engine()
    assert first is not second
    assert first.portfolio is not second.portfolio
    result = first.run(platform.bars, _strategy(), run_id="first-engine")
    assert result.usable
    with pytest.raises(ValueError, match="engine_already_used"):
        first.run(platform.bars, _strategy(), run_id="first-engine-reuse")
    assert second.run(platform.bars, _strategy(), run_id="second-engine").usable


def test_cli_verify_and_report_are_offline(capsys):
    assert main(["verify"]) == 0
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["overall"] is True
    assert payload["checks"]["live_bridge_disabled"] is True

    assert main(["report"]) == 0
    report = capsys.readouterr().out
    assert "# AXIOM-FOREX verification report" in report
    assert "not a claim of profitability" in report


def test_boundaries_and_runtime_defaults_are_isolated():
    assert all(not boundary.touched for boundary in DEFAULT_BOUNDARIES)
    assert {boundary.name for boundary in DEFAULT_BOUNDARIES} == set(
        INTEGRATION_BOUNDARIES
    )
    settings = ForexRuntimeSettings()
    assert settings.validate().valid
    assert settings.research_only is True
    assert settings.live_bridge_enabled is False
    assert settings.account_currency is None
    assert settings.leverage is None
    assert settings.strategy_id is None
    assert settings.broker_id is None
    assert settings.data_provider_id is None
    assert set(decision_index()) == {decision.key for decision in OPEN_DECISIONS}
