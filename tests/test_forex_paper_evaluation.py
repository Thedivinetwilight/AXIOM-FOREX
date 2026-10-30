"""Tests for the chronological evaluation that gate the paper runner.

The point of these tests is not that a strategy makes money -- it does not have to,
and no result here says anything about a real market.  They pin the *rules*:

* segments are chronological and the locked test window is spent once;
* a fixture or demo dataset can be evaluated but can never promote a candidate;
* the report always carries win rate, net expectancy after costs, drawdown, trade
  count, and losing periods;
* walk-forward windows come after their training windows;
* repeating the run on the same data reproduces the same numbers.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from axiom_forex import build_fixture_platform
from axiom_forex.paper_runner.evaluation import (
    DEFAULT_CRITERIA,
    claim_locked_test,
    comparison_table,
    evaluate_candidate,
    latest_evidence,
    locked_test_consumed,
    record_evaluation,
    summarize,
)
from axiom_forex.paper_runner.strategy import (
    STATUS_INSUFFICIENT,
    candidate_family,
    status_of,
)
from axiom_forex.strategies.base import Signal, Strategy


@pytest.fixture
def long_platform():
    """A fixture platform whose conversion rates cover a 200-bar window."""

    instance = build_fixture_platform(bar_count=200)
    try:
        yield instance
    finally:
        instance.close()


def candidate():
    return candidate_family()[1]  # trend_pullback_v1_8_21


class BuyThenExit(Strategy):
    name = "test_buy_then_exit"
    history_limit = 1

    def generate_signal(self, bars):
        if len(bars) == 1:
            return Signal.BUY
        if len(bars) == 12:
            return Signal.EXIT
        return Signal.HOLD


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------


def test_summary_reports_win_rate_expectancy_drawdown_and_losing_periods(long_platform):
    result = long_platform.new_engine().run(
        long_platform.bars, BuyThenExit(), run_id="metrics"
    )
    view = summarize(result)
    for key in (
        "closed_trades",
        "win_rate",
        "net_pnl_account",
        "expectancy_per_trade_account",
        "max_drawdown_account",
        "max_drawdown_fraction",
        "losing_days",
        "observed_days",
        "cost_total_account",
        "rollover_total_account",
        "max_losing_streak",
    ):
        assert key in view, key
    assert view["closed_trades"] >= 1
    assert Decimal(view["cost_total_account"]) > 0
    # Win rate and expectancy are reported side by side on purpose: a high win
    # rate alone says nothing about profitability.
    assert view["win_rate"] != ""


# ---------------------------------------------------------------------------
# the evaluation flow
# ---------------------------------------------------------------------------


def test_evaluation_splits_chronologically_and_spends_the_locked_test_once(long_platform):
    outcome = evaluate_candidate(
        engine_factory=long_platform.new_engine,
        candidate=candidate(),
        bars=long_platform.bars,
        data_class="SYNTHETIC_FIXTURE",
        dataset_id="fixture-200",
    )
    payload = outcome.payload
    assert payload["evaluated"] is True
    assert payload["locked_test_consumed"] is True
    windows = payload["windows"]
    # Contiguous bars: one segment's last bar ends exactly where the next
    # begins, and the segments only ever move forward in time.
    assert windows["train"]["last_bar_end"] <= windows["validation"]["first_bar_start"]
    assert windows["validation"]["last_bar_end"] <= windows["locked_test"]["first_bar_start"]
    assert windows["train"]["first_bar_start"] < windows["validation"]["first_bar_start"]
    assert windows["validation"]["first_bar_start"] < windows["locked_test"]["first_bar_start"]
    assert int(windows["train"]["bars"]) > int(windows["locked_test"]["bars"]) > 0


def test_fixture_data_can_never_promote_a_candidate(long_platform):
    outcome = evaluate_candidate(
        engine_factory=long_platform.new_engine,
        candidate=candidate(),
        bars=long_platform.bars,
        data_class="SYNTHETIC_FIXTURE",
        dataset_id="fixture-only",
    )
    payload = outcome.payload
    assert payload["promoting_data_class"] is False
    assert payload["ineligible_reason"] == "data_class_cannot_promote"
    assert payload["passed"] is False
    assert status_of(candidate().candidate_id, payload) == STATUS_INSUFFICIENT


def test_the_report_carries_assumptions_criteria_and_baselines(long_platform):
    outcome = evaluate_candidate(
        engine_factory=long_platform.new_engine,
        candidate=candidate(),
        bars=long_platform.bars,
        data_class="SYNTHETIC_FIXTURE",
        dataset_id="fixture-report",
    )
    payload = outcome.payload
    assert payload["assumptions"]
    assert payload["criteria"] == DEFAULT_CRITERIA.audit_view()
    assert payload["criteria_version"].startswith("paper-runner-criteria")
    assert set(payload["baselines"]) == {"baseline_hold", "baseline_sma_cross_8_21"}
    table = comparison_table(payload)
    assert table and table[0]["name"] == candidate().candidate_id
    assert {row["name"] for row in table} >= {"baseline_hold"}


def test_walk_forward_windows_come_after_their_training_windows(long_platform):
    outcome = evaluate_candidate(
        engine_factory=long_platform.new_engine,
        candidate=candidate(),
        bars=long_platform.bars,
        data_class="SYNTHETIC_FIXTURE",
        dataset_id="fixture-wf",
    )
    folds = outcome.payload["walk_forward"]
    assert folds, "200 bars should yield at least one walk-forward fold"
    for fold in folds:
        assert fold["test_window"]["first_bar_start"] >= fold["train_window"]["last_bar_end"]
    starts = [fold["test_window"]["first_bar_start"] for fold in folds]
    assert starts == sorted(starts)


def test_evaluation_is_deterministic(long_platform):
    first = evaluate_candidate(
        engine_factory=long_platform.new_engine,
        candidate=candidate(),
        bars=long_platform.bars,
        data_class="SYNTHETIC_FIXTURE",
        dataset_id="fixture-repeat",
    ).payload
    second = evaluate_candidate(
        engine_factory=long_platform.new_engine,
        candidate=candidate(),
        bars=long_platform.bars,
        data_class="SYNTHETIC_FIXTURE",
        dataset_id="fixture-repeat",
    ).payload
    assert first["aggregate"] == second["aggregate"]
    assert first["metrics"]["locked_test"] == second["metrics"]["locked_test"]
    assert [fold["net_pnl_account"] for fold in first["walk_forward"]] == [
        fold["net_pnl_account"] for fold in second["walk_forward"]
    ]


def test_too_little_data_is_insufficient_not_a_pass(long_platform):
    outcome = evaluate_candidate(
        engine_factory=long_platform.new_engine,
        candidate=candidate(),
        bars=long_platform.bars[:30],
        data_class="SYNTHETIC_FIXTURE",
        dataset_id="short",
    )
    payload = outcome.payload
    assert payload["passed"] is False
    assert payload["ineligible_reason"] == "insufficient_bars"
    assert payload["locked_test_consumed"] is False


def test_the_locked_test_window_cannot_be_spent_twice(long_platform):
    repository = long_platform.repository
    assert locked_test_consumed(repository=repository, dataset_id="locked") is False
    claim_locked_test(repository=repository, dataset_id="locked", at="2026-03-10T00:00:00+00:00")
    assert locked_test_consumed(repository=repository, dataset_id="locked") is True
    with pytest.raises(ValueError, match="locked_test_already_consumed"):
        claim_locked_test(
            repository=repository, dataset_id="locked", at="2026-03-10T01:00:00+00:00"
        )


def test_recorded_evidence_round_trips_and_stays_unselectable_for_fixtures(long_platform):
    outcome = evaluate_candidate(
        engine_factory=long_platform.new_engine,
        candidate=candidate(),
        bars=long_platform.bars,
        data_class="SYNTHETIC_FIXTURE",
        dataset_id="fixture-recorded",
    )
    record_evaluation(repository=long_platform.repository, payload=outcome.payload)
    evidence = latest_evidence(
        repository=long_platform.repository, candidate_id=candidate().candidate_id
    )
    assert evidence is not None
    assert evidence["evaluated"] is True
    assert evidence["data_class"] == "SYNTHETIC_FIXTURE"
    assert status_of(candidate().candidate_id, evidence) == STATUS_INSUFFICIENT


def test_a_missing_dataset_has_no_evidence_at_all(long_platform):
    assert (
        latest_evidence(repository=long_platform.repository, candidate_id="never-evaluated")
        is None
    )
