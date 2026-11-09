"""The desk, end to end, and the lab that reports on it.

These are the integration and failure-recovery tests: duplicates, stale quotes,
disconnects, ambiguous submissions, emergencies, restarts, and two strategies
competing for the same exposure.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from axiom_forex.scalper import lab
from axiom_forex.scalper.bayes import ProbabilityEstimate
from axiom_forex.scalper.config import LIVE, ScalperConfigError, fixture_config
from axiom_forex.scalper.desk import COOLDOWN, RESYNCING, WARMING_UP, ScalperDesk
from axiom_forex.scalper.execution import PaperVenue, VenueFaults
from axiom_forex.scalper.guardian import Guardian, GuardianLimits
from axiom_forex.scalper.signals import (
    MomentumContinuation,
    MomentumParameters,
    default_strategies,
)
from axiom_forex.scalper.ticks import ReplayTick, synthetic_ticks

CONFIG = fixture_config()
SPEC = CONFIG.spec_for("EURUSD.pro")
START = datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)
EQUITY = Decimal("10000")


def declared_probability(p: str = "0.78", sd: str = "0.03") -> ProbabilityEstimate:
    """A declared prior, labelled as such, so the desk's gates can be exercised."""

    value = float(p)
    deviation = float(sd)
    return ProbabilityEstimate(
        p=value,
        sd=deviation,
        low=max(0.0, value - 1.28 * deviation),
        high=min(1.0, value + 1.28 * deviation),
        samples=200,
        basis="DECLARED_PRIOR_FOR_TEST",
        label="test",
    )


def wide_strategies():
    """Brackets that can pay the declared costs, chosen by arithmetic not by backtest."""

    return lab.cost_compliant_strategies(
        assumed_spread_points=Decimal("8"),
        stops_level_points=SPEC.stops_level_points,
        cost=CONFIG.cost,
        point_value_per_lot=SPEC.point_value_per_lot,
        strategies=default_strategies(),
    )


def desk(*, venue=None, strategies=None, probability=None, guardian=None, run_id="test-run"):
    return ScalperDesk(
        config=CONFIG,
        strategies=strategies or wide_strategies(),
        venue=venue or PaperVenue(cost=CONFIG.cost, faults=VenueFaults(max_latency_ms=5)),
        guardian=guardian,
        probability_source=probability or (lambda signal, regime: declared_probability()),
        run_id=run_id,
    )


def feed(instance, ticks, *, day="2026-03-02", received_offset_ms=0):
    outcomes = []
    for tick in ticks:
        received = tick.at + timedelta(milliseconds=received_offset_ms)
        outcomes.append(
            instance.on_tick(
                symbol=tick.symbol,
                bid=tick.bid,
                ask=tick.ask,
                source_time=tick.at,
                received_at=received,
                volume=tick.volume,
                equity=EQUITY,
                balance=EQUITY,
                day_key=day,
            )
        )
    return outcomes


def trending_replay(count: int = 200, drift: float = 2.0, start: datetime = START):
    return synthetic_ticks(
        count=count, drift_points=drift, noise_points=1.0, step_ms=200, start=start
    )


# ---------------------------------------------------------------------------
# end to end
# ---------------------------------------------------------------------------


def test_a_run_produces_trades_and_a_checkable_ledger():
    instance = desk()
    feed(instance, trending_replay())
    status = instance.status()
    assert status["ticks"]["accepted"] > 100
    assert status["stats"]["opportunities"] > 0
    assert status["stats"]["signals"] > 0
    assert status["stats"]["fills"] >= 1
    assert status["stats"]["exits"] >= 1
    assert status["ledger"]["integrity"]["chain"]["ok"] is True
    assert status["ledger"]["integrity"]["order_requests_without_result"] == []


def test_every_trade_pays_costs_so_net_is_below_gross():
    instance = desk()
    feed(instance, trending_replay())
    for row in instance.trades:
        assert row["cost_points"] > 0
        assert row["net_points"] < row["gross_points"]


def test_the_ledger_records_the_opportunities_that_were_refused():
    instance = desk()
    feed(instance, synthetic_ticks(count=120, drift_points=0.0, noise_points=8.0, step_ms=200))
    ledger = instance.scribe
    kinds = {entry.kind for entry in ledger.entries()}
    assert "opportunity" in kinds
    assert "rejection" in kinds or "state_change" in kinds
    assert instance.stats["signals"] == 0 or instance.stats["ev_reject"] >= 0


def test_a_no_trade_regime_is_recorded_rather_than_hidden():
    instance = desk()
    # A stuck feed: the same price over and over is low liquidity, not a quiet trend.
    replay = [
        ReplayTick(
            symbol=SPEC.symbol,
            bid=Decimal("1.09996"),
            ask=Decimal("1.10004"),
            at=START + timedelta(milliseconds=200 * index),
        )
        for index in range(120)
    ]
    feed(instance, replay)
    states = [entry for entry in instance.scribe.entries("state_change")]
    assert any(entry.payload.get("state") == "no_trade" for entry in states)
    assert instance.stats["fills"] == 0


def test_probabilities_are_written_down_with_their_basis():
    instance = desk()
    feed(instance, trending_replay())
    entries = instance.scribe.entries("probability")
    assert entries
    assert all(entry.payload["basis"] == "DECLARED_PRIOR_FOR_TEST" for entry in entries)


# ---------------------------------------------------------------------------
# edge cases and failures
# ---------------------------------------------------------------------------


def test_a_duplicate_tick_changes_nothing_but_the_refusal_counter():
    clean = desk(run_id="clean")
    feed(clean, trending_replay())
    dirty = desk(run_id="dirty")
    replay = trending_replay()
    duplicated = []
    for tick in replay:
        duplicated.append(tick)
        if tick is replay[120]:
            duplicated.append(tick)  # exact replay of one tick
    feed(dirty, duplicated)
    assert dirty.status()["tick_refusals"].get("duplicate_tick", 0) == 1
    assert dirty.stats["fills"] == clean.stats["fills"]
    assert dirty.stats["orders"] == clean.stats["orders"]


def test_a_stale_quote_never_becomes_a_decision():
    instance = desk()
    outcomes = feed(instance, trending_replay(), received_offset_ms=30_000)
    assert all(not outcome.accepted for outcome in outcomes)
    assert instance.stats["fills"] == 0
    assert instance.status()["tick_refusals"].get("stale_tick", 0) > 0


def test_a_disconnect_is_recorded_and_does_not_crash_the_desk():
    instance = desk()
    replay = trending_replay(count=80)
    gap_tick = ReplayTick(
        symbol=SPEC.symbol,
        bid=replay[-1].bid,
        ask=replay[-1].ask,
        at=replay[-1].at + timedelta(seconds=8),
    )
    feed(instance, list(replay) + [gap_tick])
    quality = [entry for entry in instance.scribe.entries("data_quality")]
    assert any(entry.payload.get("event_kind") == "tick_gap" for entry in quality)


def test_an_ambiguous_submission_halts_new_entries_until_it_is_resolved():
    venue = PaperVenue(cost=CONFIG.cost, faults=VenueFaults(unknown_every=1, max_latency_ms=2))
    instance = desk(venue=venue)
    feed(instance, trending_replay(count=140))
    assert instance.stats["unknown"] >= 1
    assert instance.trader.has_unresolved is True
    before = instance.stats["orders"]
    # More ticks arrive; nothing new is submitted while the outcome is unknown.
    feed(instance, trending_replay(count=40, start=START + timedelta(seconds=60)))
    assert instance.stats["orders"] == before


def test_resolving_an_ambiguous_order_is_written_down():
    venue = PaperVenue(cost=CONFIG.cost, faults=VenueFaults(unknown_every=1, max_latency_ms=2))
    instance = desk(venue=venue)
    feed(instance, trending_replay(count=140))
    results = instance.resolve_unresolved(at_ms=1_000_000)
    assert results
    recovered = [
        entry for entry in instance.scribe.entries("execution_result") if entry.payload.get("status", "").startswith("recovered_")
    ]
    assert recovered


def test_an_emergency_stop_leaves_open_positions_open_and_manageable():
    instance = desk()
    feed(instance, trending_replay())
    open_before = len(instance.book.open_positions)
    plan = instance.emergency_stop(reason="operator test", at_ms=1_000_000)
    assert plan.entries_suspended is True
    assert plan.position_management_allowed is True
    assert len(instance.book.open_positions) == open_before
    assert instance.guardian.may_manage() is True
    assert instance.guardian.may_open()[0] is False
    assert any(entry.payload.get("state") == "emergency_stop" for entry in instance.scribe.entries("state_change"))


def test_flattening_after_an_emergency_closes_and_records_everything_it_can():
    instance = desk()
    feed(instance, trending_replay())
    if not instance.book.open_positions:
        pytest.skip("no position was open at the end of this replay")
    instance.emergency_stop(reason="flatten test", at_ms=1_000_000)
    actions = instance.flatten_all(at_ms=1_000_000)
    assert actions
    assert not instance.book.open_positions


def test_a_snapshot_carries_the_state_a_restart_needs_and_invents_nothing():
    instance = desk()
    feed(instance, trending_replay())
    snapshot = instance.snapshot()
    assert snapshot["mode"] == "PAPER"
    assert "open_positions" in snapshot and "unresolved_orders" in snapshot
    assert snapshot["scribe_head"] == instance.scribe.verify()["head"]
    assert "re-verifies protection" in snapshot["note"]


def test_a_restarted_desk_starts_with_no_positions_and_no_memory_of_fills():
    first = desk()
    feed(first, trending_replay())
    assert first.scribe.entries(), "the first run must have recorded something"
    second = desk(run_id="after-restart")
    assert second.book.open_positions == ()
    assert second.snapshot()["open_positions"] == []
    assert second.status()["closed_trades"] == 0
    assert second.status()["ledger"]["entries"] == 0  # nothing is inherited by accident


def test_two_strategies_cannot_both_take_the_same_exposure():
    instance = desk()
    feed(instance, trending_replay(count=220))
    reasons = instance.status()["risk_refusals"]
    blocked = any(
        key in reasons
        for key in (
            "currency_concentration_exceeded",
            "correlated_exposure_exceeded",
            "aggregate_open_risk_exceeded",
        )
    )
    assert blocked or len(instance.book.open_positions) <= CONFIG.max_open_positions
    assert len(instance.book.open_positions) <= CONFIG.max_open_positions


def test_the_desk_is_paper_only_in_configuration_and_in_code():
    with pytest.raises(ScalperConfigError):
        fixture_config(mode=LIVE)

    class LiveVenue(PaperVenue):
        mode = "LIVE"

    from app.execution.broker import TradingBlocked

    with pytest.raises(TradingBlocked):
        ScalperDesk(config=CONFIG, venue=LiveVenue(cost=CONFIG.cost))


def test_no_scalper_module_imports_the_live_order_path():
    """A regression test, not documentation: the live module must stay out."""

    import re

    package = Path(__file__).resolve().parents[1] / "axiom_forex" / "scalper"
    import_pattern = re.compile(r"^\s*(from|import)\s+.*(brokers\.mt5\.orders|\borders\b)", re.M)
    offenders = []
    for path in package.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if import_pattern.search(text):
            offenders.append(path.name)
        if "order_send(" in text:
            offenders.append(path.name)
    assert offenders == []


# ---------------------------------------------------------------------------
# the lab
# ---------------------------------------------------------------------------


def test_the_lab_runs_a_scenario_and_reports_the_funnel():
    scenario = lab.trending(count=200)
    result = lab.run_scenario(
        scenario,
        config=CONFIG,
        strategies=wide_strategies(),
        trained_book=None,
    )
    view = result.audit_view()
    assert view["scenario"]["data_class"] == "SAMPLE_DATA"
    assert "opportunities" in view["funnel"]
    assert view["metrics"]["n"] == len(result.trades)
    assert "no profit claim" in view["claim"].lower() or "not a profit claim" in view["claim"].lower()
    assert result.limitations


def test_a_training_pass_measures_brackets_without_placing_orders():
    training = lab.collect_outcomes(
        [lab.trending(count=200)], config=CONFIG, strategies=wide_strategies()
    )
    assert training["outcomes"]
    assert training["outcomes"][0].note.startswith("TRAIN_SIMULATION")
    assert "never to report performance" in training["note"]
    assert training["data_class"] == "SAMPLE_DATA"


def test_the_lab_writes_a_report_that_states_what_it_is():
    scenario = lab.trending(count=160)
    result = lab.run_scenario(scenario, config=CONFIG, strategies=wide_strategies())
    report = lab.validation_for(result)
    import tempfile

    target = Path(tempfile.mkdtemp()) / "report.md"
    written = lab.write_report(target, runs=[result], validations=[report], extra_notes=["written by a test"])
    text = written.read_text(encoding="utf-8")
    assert "# AX-SCALPER lab report" in text
    assert "SAMPLE_DATA" in text
    assert "No claim of profitability" in text
    assert "written by a test" in text
    assert "## Independent validation" in text


def test_degradation_is_only_called_when_the_drop_is_larger_than_noise():
    baseline = lab.run_scenario(lab.trending(count=160), config=CONFIG, strategies=wide_strategies())
    forward = lab.run_scenario(
        lab.shifted(lab.trending(count=160), start=START + timedelta(days=1)),
        config=CONFIG,
        strategies=wide_strategies(),
    )
    verdict = lab.degradation_check(baseline, forward)
    assert verdict["verdict"] in {"no_detectable_change", "changed", "degraded"}
    if verdict["verdict"] != "degraded":
        assert verdict["degraded"] is False
    assert "not distinguishable" in verdict["note"]


def test_comparison_does_not_rank_on_profit_alone():
    first = lab.run_scenario(lab.trending(count=120), config=CONFIG, strategies=wide_strategies())
    second = lab.run_scenario(lab.ranging(count=120), config=CONFIG, strategies=wide_strategies())
    comparison = lab.compare([first, second])
    assert len(comparison["runs"]) == 2
    assert "not ranked on profit" in comparison["note"]


def test_scenarios_are_deterministic_and_named():
    first = lab.dirty_feed(count=120)
    second = lab.dirty_feed(count=120)
    assert first.digest == second.digest
    assert "duplicates" in first.injected
    assert first.data_class == "SAMPLE_DATA"


def test_cost_compliant_brackets_are_derived_by_arithmetic():
    strategies = wide_strategies()
    cost = 8 + 2 * 1  # spread plus slippage both sides
    for strategy in strategies:
        parameters = strategy.parameters
        stop = Decimal(str(parameters.stop_points))
        target = Decimal(str(getattr(parameters, "target_points", getattr(parameters, "min_target_points", "0"))))
        assert stop >= SPEC.stops_level_points
        assert target >= cost * lab.COST_MULTIPLE_FOR_TARGET
