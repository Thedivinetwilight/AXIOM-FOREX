"""The evaluation's promises: honest counts, no test-window peeking, a budget."""

from __future__ import annotations

from axiom_forex.research.engine import SegmentStats
from axiom_forex.strategies.catalog.candidates import catalog
from axiom_forex.strategies.catalog.evaluate import (
    SCREEN_FAILED,
    SCREEN_PASSED,
    SKIP_BUDGET,
    SKIP_CANCELLED,
    SKIP_HISTORY,
    STATUS_SKIPPED,
    STATUS_TESTED,
    EvaluationPlan,
    Row,
    SeriesSlot,
    _screen_row,
    evaluate,
    split_bounds,
)
from tests.catalog_support import SAMPLE_COST, random_walk, series_from_closes, trending


def _ticking_clock(step: float = 0.001):
    """A monotonic clock under the test's control, so the budget is exact."""

    state = {"now": 0.0}

    def clock() -> float:
        state["now"] += step
        return state["now"]

    return clock


def _slot(bars: int = 900, *, timeframe: str = "15m", seconds: int = 900, seed: int = 3) -> SeriesSlot:
    series = series_from_closes(
        trending(bars, seed=seed), timeframe=timeframe, bar_seconds=seconds
    )
    return SeriesSlot(symbol="EURUSD.pro", timeframe=timeframe, series=series, cost=SAMPLE_COST, origin="SAMPLE_DATA")


def _subset(limit: int = 24):
    return catalog()[:limit]


def test_counts_add_up_and_every_row_says_what_happened():
    report = evaluate(
        EvaluationPlan(slots=(_slot(),), lot=0.1, budget_seconds=600),
        candidates=_subset(),
    )
    assert len(report.rows) == len(_subset())
    counts = report.counts
    assert counts["tested"] + counts["skipped"] + counts["failed"] == counts["pairs_planned"]
    assert counts["tested"] == len([row for row in report.rows if row.status == STATUS_TESTED])
    assert report.status == "COMPLETE"
    assert report.claim and "not evidence" in report.claim
    assert report.search_note


def test_split_is_chronological_and_covers_the_whole_sample():
    bounds = split_bounds(1000)
    assert bounds["train"][0] == 0
    assert bounds["train"][1] == bounds["validation"][0]
    assert bounds["validation"][1] == bounds["test"][0]
    assert bounds["test"][1] == 1000


def test_a_budget_that_is_already_gone_runs_nothing_and_says_so():
    report = evaluate(
        EvaluationPlan(slots=(_slot(),), budget_seconds=0.0),
        candidates=_subset(5),
        clock=_ticking_clock(step=1.0),
    )
    assert report.status == "TIMEOUT_PARTIAL"
    assert report.counts["tested"] == 0
    assert all(row.status == STATUS_SKIPPED for row in report.rows)
    assert all(SKIP_BUDGET in row.reasons for row in report.rows)


def test_cancelling_stops_before_the_next_row_and_is_reported():
    calls = {"n": 0}

    def cancel() -> bool:
        calls["n"] += 1
        return calls["n"] > 1

    report = evaluate(
        EvaluationPlan(slots=(_slot(),), budget_seconds=600),
        candidates=_subset(6),
        cancel=cancel,
    )
    assert report.status == "CANCELLED_PARTIAL"
    assert any(SKIP_CANCELLED in row.reasons for row in report.rows)


def test_history_too_short_is_a_skip_with_a_reason_not_a_result():
    short = _slot(120)
    report = evaluate(
        EvaluationPlan(slots=(short,), budget_seconds=600), candidates=_subset(4)
    )
    assert all(row.status == STATUS_SKIPPED for row in report.rows)
    assert all(SKIP_HISTORY in row.reasons for row in report.rows)
    assert report.counts["tested"] == 0
    assert any("did not run" in note for note in report.notes)


def test_the_test_window_is_measured_but_never_used_to_screen():
    def block(net: float, drawdown: float = 0.01, trades: int = 40) -> SegmentStats:
        return SegmentStats(
            segment="x",
            bars=100,
            trades=trades,
            long_trades=trades // 2,
            short_trades=trades - trades // 2,
            winners=trades // 2,
            gross_return=net + 0.05,
            cost_return=0.05,
            net_return=net,
            max_drawdown=drawdown,
            gross_pips=10.0,
            cost_pips=5.0,
            net_pips=5.0,
            nights=0,
            forced_exits=0,
            bars_held=trades,
        )

    row = Row(
        candidate_id="cat-test",
        family="trend",
        structure="a+b+c",
        symbol="EURUSD.pro",
        timeframe="15m",
        status=STATUS_TESTED,
        windows={"train": block(0.10), "validation": block(0.05), "test": block(-0.90)},
    )
    screen, reasons, score, _concentration = _screen_row(
        row, min_trades=8, max_drawdown=0.25, concentration_limit=0.6
    )
    assert screen == SCREEN_PASSED
    assert reasons == ()
    assert score is not None and score > 0

    # A terrible train window fails the screen even with a wonderful test window.
    row = Row(
        candidate_id="cat-test",
        family="trend",
        structure="a+b+c",
        symbol="EURUSD.pro",
        timeframe="15m",
        status=STATUS_TESTED,
        windows={"train": block(-0.10), "validation": block(0.05), "test": block(0.90)},
    )
    screen, reasons, score, _ = _screen_row(
        row, min_trades=8, max_drawdown=0.25, concentration_limit=0.6
    )
    assert screen == SCREEN_FAILED
    assert score is None
    assert any("train" in reason for reason in reasons)


def test_a_row_with_too_few_trades_is_not_ranked():
    row = Row(
        candidate_id="cat-test",
        family="trend",
        structure="a+b+c",
        symbol="EURUSD.pro",
        timeframe="15m",
        status=STATUS_TESTED,
        windows={
            "train": SegmentStats(
                segment="train", bars=100, trades=2, long_trades=1, short_trades=1, winners=1,
                gross_return=0.1, cost_return=0.01, net_return=0.09, max_drawdown=0.01,
                gross_pips=10.0, cost_pips=1.0, net_pips=9.0, nights=0, forced_exits=0, bars_held=4,
            ),
            "validation": SegmentStats(
                segment="validation", bars=100, trades=2, long_trades=1, short_trades=1, winners=1,
                gross_return=0.1, cost_return=0.01, net_return=0.09, max_drawdown=0.01,
                gross_pips=10.0, cost_pips=1.0, net_pips=9.0, nights=0, forced_exits=0, bars_held=4,
            ),
        },
    )
    screen, reasons, score, _ = _screen_row(
        row, min_trades=8, max_drawdown=0.25, concentration_limit=0.6
    )
    assert screen == "TOO_FEW_TRADES"
    assert score is None
    assert reasons


def test_the_same_inputs_give_the_same_result():
    plan = EvaluationPlan(slots=(_slot(),), budget_seconds=600)
    first = evaluate(plan, candidates=_subset(12))
    second = evaluate(plan, candidates=_subset(12))
    assert first.counts == second.counts
    assert [row.compact() for row in first.rows] == [row.compact() for row in second.rows]


def test_a_sample_series_is_labelled_as_one_in_the_report():
    report = evaluate(
        EvaluationPlan(slots=(_slot(),), budget_seconds=600), candidates=_subset(4)
    )
    assert any("sample or fixture" in note for note in report.notes)
    assert report.plan_summary["slots"][0]["origin"] == "SAMPLE_DATA"
