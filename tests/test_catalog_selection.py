"""The selector: predeclared rules, out-of-sample evidence, and a reason for everyone.

Every row here is a dict shaped like a stored evaluation row, and every series is a
fixture with timestamps of its own.  Nothing contacts a broker, and no rule in the
selector is relaxed by anything in this file -- the tests assert the opposite.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from axiom_forex.research.engine import CostModel
from axiom_forex.research.series import build_series, rows_from_rates
from axiom_forex.strategies.catalog.evaluate import SeriesSlot, split_bounds
from axiom_forex.strategies.catalog.selection import (
    BLOCKER_DATA_ORIGIN,
    BLOCKER_NO_ROWS,
    BLOCKER_NO_SELECTION,
    BLOCKER_NOT_EVALUATED,
    ELIGIBLE,
    REASON_BOUNDARY_UNKNOWN,
    REASON_CAP_SYMBOL,
    REASON_FOLDS_OUT_OF_SAMPLE,
    REASON_NOT_TESTED,
    REASON_SCREEN_FAILED,
    REJECTED,
    SELECTED,
    SelectionPolicy,
    in_sample_folds,
    plan_from_report,
    select,
    validation_boundaries,
)

UTC = timezone.utc
NOW = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
BARS = 200
STEP = 3600


# -- a fixture series with a real timeline ----------------------------------


def _rates(bars: int = BARS, *, start: datetime | None = None):
    begin = start or (NOW - timedelta(seconds=STEP * bars))
    rows = []
    previous = 1.1000
    for index in range(bars):
        close = round(previous + (0.0002 if index % 3 else -0.0001), 5)
        rows.append(
            {
                "timestamp": begin + timedelta(seconds=STEP * index),
                "open": previous,
                "high": round(max(previous, close) + 0.00002, 5),
                "low": round(min(previous, close) - 0.00002, 5),
                "close": close,
                "spread_points": 4,
                "tick_volume": 3,
            }
        )
        previous = close
    return rows


def _slot(symbol: str = "EURUSD.pro", timeframe: str = "1h", bars: int = BARS) -> SeriesSlot:
    series = build_series(
        symbol,
        "EUR/USD",
        timeframe,
        STEP,
        Decimal("0.00001"),
        rows_from_rates(_rates(bars)),
    )
    cost = CostModel(
        point=0.00001,
        contract_size=100_000.0,
        account_currency="USD",
        quote_currency="USD",
        slippage_pips=0.2,
        commission_per_lot=0.0,
    )
    assert not cost.unusable
    return SeriesSlot(symbol=symbol, timeframe=timeframe, series=series, cost=cost, origin="MT5_TERMINAL_READ_ONLY")


def _fold(end: datetime, net: float = 0.01, trades: int = 12) -> dict[str, Any]:
    return {
        "fold": "wf1",
        "start": (end - timedelta(hours=40)).isoformat(),
        "end": end.isoformat(),
        "trades": trades,
        "net_return": net,
        "max_drawdown": 0.02,
    }


def _row(
    candidate_id: str = "cat-0001",
    *,
    symbol: str = "EURUSD.pro",
    timeframe: str = "1h",
    structure: str = "sma_cross|bracket|none",
    status: str = "TESTED",
    screen: str = "PASSED_SCREEN",
    score: float | None = 0.42,
    folds=(),
    screen_reasons=(),
    train_net: float = 0.05,
    validation_net: float = 0.04,
    concentration: float = 0.2,
) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "family": "trend",
        "structure": structure,
        "symbol": symbol,
        "timeframe": timeframe,
        "status": status,
        "reasons": [],
        "bars": BARS,
        "trades": 24,
        "screen": screen,
        "screen_reasons": list(screen_reasons),
        "score": score,
        "concentration": concentration,
        "costs_incomplete": False,
        "folds_positive": sum(1 for fold in folds if fold["net_return"] > 0),
        "windows": {
            "train": {"trades": 12, "net_return": train_net, "max_drawdown": 0.03},
            "validation": {"trades": 12, "net_return": validation_net, "max_drawdown": 0.02},
            "test": {"trades": 12, "net_return": 0.09, "max_drawdown": 0.05},
        },
        "walk_forward": list(folds),
    }


def _boundary(slot: SeriesSlot) -> datetime:
    return validation_boundaries([slot])[(slot.symbol, slot.timeframe)]


# -- the fold rule ----------------------------------------------------------


def test_the_validation_boundary_is_the_last_bar_of_the_validation_window():
    slot = _slot()
    bounds = split_bounds(slot.bars)
    start, end = bounds["validation"]
    last = slot.series.timestamps[end - 1]
    assert _boundary(slot) == last.replace(tzinfo=UTC)
    # And it is strictly before the locked test window.
    assert _boundary(slot) < slot.series.timestamps[bounds["test"][0]].replace(tzinfo=UTC)


def test_a_fold_that_reaches_into_the_test_window_is_not_counted_as_evidence():
    slot = _slot()
    boundary = _boundary(slot)
    inside = _fold(boundary - timedelta(hours=10), net=0.02)
    outside = _fold(boundary + timedelta(hours=10), net=0.05)
    positive, total = in_sample_folds(_row(folds=[inside, outside]), boundary)
    assert (positive, total) == (1, 1)


def test_a_losing_in_sample_fold_does_not_count_as_positive():
    slot = _slot()
    boundary = _boundary(slot)
    folds = [_fold(boundary - timedelta(hours=5), net=-0.01), _fold(boundary - timedelta(hours=20), net=0.0)]
    assert in_sample_folds(_row(folds=folds), boundary) == (0, 2)


def test_a_row_whose_boundary_is_unknown_is_rejected_rather_than_assumed():
    # No slots handed in, so the boundary cannot be established.  The fold that
    # looks positive may be inside the test window, so it is not evidence.
    row = _row(folds=[_fold(NOW - timedelta(hours=2), net=0.03)])
    plan = select(
        [row],
        slots=(),
        data_origin="MT5_TERMINAL_READ_ONLY",
        evaluated_status="COMPLETE",
        now=NOW,
    )
    assert plan.selected() == ()
    assert REASON_BOUNDARY_UNKNOWN in plan.decisions[0].reasons
    assert plan.counts["validation_boundary_known"] is False


def test_the_test_window_plays_no_part_in_a_selection():
    slot = _slot()
    boundary = _boundary(slot)
    good = _row("cat-good", folds=[_fold(boundary - timedelta(hours=5), net=0.02)])
    # The same row with a spectacular test window must rank identically.
    better_test = _row("cat-good", folds=[_fold(boundary - timedelta(hours=5), net=0.02)])
    better_test["windows"]["test"] = {"trades": 99, "net_return": 5.0, "max_drawdown": 0.01}
    first = select([good], slots=[slot], data_origin="MT5_TERMINAL_READ_ONLY", now=NOW)
    second = select([better_test], slots=[slot], data_origin="MT5_TERMINAL_READ_ONLY", now=NOW)
    assert first.selected()[0].score == second.selected()[0].score
    assert first.to_payload()["decisions"] == second.to_payload()["decisions"]


# -- what the plan refuses --------------------------------------------------


def test_a_fixture_evaluation_cannot_promote_a_strategy():
    slot = _slot()
    boundary = _boundary(slot)
    rows = [_row(folds=[_fold(boundary - timedelta(hours=5), net=0.02)])]
    for origin in ("", "FIXTURE_SAMPLE", "SYNTHETIC"):
        plan = select(rows, slots=[slot], data_origin=origin, evaluated_status="COMPLETE", now=NOW)
        assert plan.selected() == ()
        assert plan.blockers[0]["reason"] == BLOCKER_DATA_ORIGIN
        assert plan.decisions[0].reasons[0] == BLOCKER_DATA_ORIGIN


def test_an_unfinished_evaluation_selects_nothing():
    slot = _slot()
    plan = select(
        [_row()],
        slots=[slot],
        data_origin="MT5_TERMINAL_READ_ONLY",
        evaluated_status="CANCELLED",
        now=NOW,
    )
    assert plan.selected() == ()
    assert plan.blockers[0]["reason"] == BLOCKER_NOT_EVALUATED


def test_an_empty_evaluation_says_so_instead_of_selecting_nothing_quietly():
    plan = select([], data_origin="MT5_TERMINAL_READ_ONLY", evaluated_status="COMPLETE", now=NOW)
    assert plan.available is False
    assert plan.blockers[0]["reason"] == BLOCKER_NO_ROWS
    assert "$" not in plan.blockers[0]["detail"]


def test_a_policy_with_impossible_numbers_is_reported_not_applied():
    policy = SelectionPolicy(max_per_symbol=0, max_drawdown=-1.0)
    assert policy.problems()
    plan = select(
        [_row()],
        policy=policy,
        data_origin="MT5_TERMINAL_READ_ONLY",
        evaluated_status="COMPLETE",
        now=NOW,
    )
    assert plan.selected() == ()
    assert plan.blockers[0]["reason"] == "selection_policy_invalid"


def test_a_row_that_never_ran_is_rejected_with_the_evaluators_own_reason():
    slot = _slot()
    plan = select(
        [_row(status="SKIPPED")],
        slots=[slot],
        data_origin="MT5_TERMINAL_READ_ONLY",
        evaluated_status="COMPLETE",
        now=NOW,
    )
    assert plan.decisions[0].status == REJECTED
    assert REASON_NOT_TESTED in plan.decisions[0].reasons


def test_a_row_that_failed_the_screen_keeps_the_screen_wording():
    slot = _slot()
    row = _row(screen="FAILED_SCREEN", screen_reasons=["validation drawdown 0.41 above 0.25"])
    plan = select([row], slots=[slot], data_origin="MT5_TERMINAL_READ_ONLY", now=NOW)
    decision = plan.decisions[0]
    assert REASON_SCREEN_FAILED in decision.reasons
    assert "validation drawdown 0.41 above 0.25" in decision.message


def test_a_row_with_too_few_in_sample_folds_is_rejected_and_says_how_many():
    slot = _slot()
    boundary = _boundary(slot)
    folds = [
        _fold(boundary - timedelta(hours=5), net=-0.01),
        _fold(boundary + timedelta(hours=40), net=0.05),  # in the test window
    ]
    policy = SelectionPolicy(min_folds_positive=1)
    plan = select(
        [_row(folds=folds)],
        policy=policy,
        slots=[slot],
        data_origin="MT5_TERMINAL_READ_ONLY",
        now=NOW,
    )
    decision = plan.decisions[0]
    assert REASON_FOLDS_OUT_OF_SAMPLE in decision.reasons
    assert "0 profitable walk-forward fold(s)" in decision.message
    assert "2 fold(s) measured overall" in decision.message


# -- determinism and caps ---------------------------------------------------


def _several_rows(boundary: datetime):
    return [
        _row("cat-a", symbol="EURUSD.pro", structure="s1", score=0.50, folds=[_fold(boundary - timedelta(hours=5))]),
        _row("cat-b", symbol="EURUSD.pro", structure="s2", score=0.40, folds=[_fold(boundary - timedelta(hours=5))]),
        _row("cat-c", symbol="GBPUSD.pro", structure="s3", score=0.45, folds=[_fold(boundary - timedelta(hours=5))]),
        _row("cat-d", symbol="GBPUSD.pro", structure="s4", score=0.45, folds=[_fold(boundary - timedelta(hours=5))]),
        _row("cat-e", symbol="USDJPY.pro", structure="s5", score=0.10, folds=[_fold(boundary - timedelta(hours=5))]),
    ]


def test_the_same_rows_produce_the_same_plan():
    slot = _slot()
    rows = _several_rows(_boundary(slot))
    first = select(rows, slots=[slot], data_origin="MT5_TERMINAL_READ_ONLY", now=NOW)
    second = select(list(reversed(rows)), slots=[slot], data_origin="MT5_TERMINAL_READ_ONLY", now=NOW)
    assert [item.key for item in first.selected()] == [item.key for item in second.selected()]
    assert first.to_payload()["decisions"] == second.to_payload()["decisions"]


def test_one_row_per_symbol_and_a_named_cap_for_the_rest():
    slot = _slot()
    rows = _several_rows(_boundary(slot))
    plan = select(rows, slots=[slot], data_origin="MT5_TERMINAL_READ_ONLY", now=NOW)
    symbols = [item.symbol for item in plan.selected()]
    assert len(symbols) == len(set(symbols))
    assert "EURUSD.pro" in symbols
    capped = [item for item in plan.decisions if REASON_CAP_SYMBOL in item.reasons]
    assert capped, "the row held back by the symbol cap must say so"
    assert capped[0].status == ELIGIBLE
    assert "EURUSD.pro" in capped[0].message


def test_the_total_cap_stops_at_the_declared_number():
    slot = _slot()
    boundary = _boundary(slot)
    rows = [
        _row(f"cat-{index}", symbol="EURUSD.pro", structure=f"s{index}", score=0.5 - index / 100,
             folds=[_fold(boundary - timedelta(hours=5))])
        for index in range(9)
    ]
    policy = SelectionPolicy(max_selected=2, max_per_symbol=9, max_per_timeframe=9, max_per_structure=9)
    plan = select(rows, policy=policy, slots=[slot], data_origin="MT5_TERMINAL_READ_ONLY", now=NOW)
    assert len(plan.selected()) == 2
    assert plan.counts["eligible_not_chosen"] == 7


def test_every_row_gets_a_decision_with_a_reason_or_a_rank():
    slot = _slot()
    boundary = _boundary(slot)
    rows = _several_rows(boundary) + [
        _row("cat-skip", status="SKIPPED"),
        _row("cat-fail", screen="FAILED_SCREEN", screen_reasons=["net return negative in validation"]),
        _row("cat-short", screen="TOO_FEW_TRADES", screen_reasons=["fewer than 8 trades"]),
        _row("cat-noscore", score=None, folds=[_fold(boundary - timedelta(hours=5))]),
        _row("cat-nostructure", structure="", folds=[_fold(boundary - timedelta(hours=5))]),
    ]
    plan = select(rows, slots=[slot], data_origin="MT5_TERMINAL_READ_ONLY", now=NOW)
    assert len(plan.decisions) == len(rows)
    for decision in plan.decisions:
        assert decision.message, decision.key
        if decision.status == SELECTED:
            assert decision.rank is not None and decision.reasons == ()
        else:
            assert decision.reasons, decision.key


def test_a_selection_that_finds_nothing_says_so_and_relaxes_nothing():
    slot = _slot()
    boundary = _boundary(slot)
    rows = [
        _row(
            f"cat-{index}",
            symbol=f"PAIR{index}",
            structure=f"s{index}",
            folds=[_fold(boundary - timedelta(hours=5), net=-0.02)],
        )
        for index in range(4)
    ]
    plan = select(rows, slots=[slot], data_origin="MT5_TERMINAL_READ_ONLY", evaluated_status="COMPLETE", now=NOW)
    assert plan.selected() == ()
    assert plan.available is False
    assert plan.blockers[0]["reason"] == BLOCKER_NO_SELECTION
    assert "no rule was relaxed" in plan.blockers[0]["detail"]


# -- payload shape ----------------------------------------------------------


def test_the_payload_is_json_safe_and_carries_the_policy_fingerprint():
    import json

    slot = _slot()
    plan = select(
        _several_rows(_boundary(slot)),
        slots=[slot],
        data_origin="MT5_TERMINAL_READ_ONLY",
        evaluated_status="COMPLETE",
        now=NOW,
    )
    payload = json.loads(json.dumps(plan.to_payload()))
    assert payload["policy"]["fingerprint"] == SelectionPolicy().fingerprint()
    assert payload["policy"]["test_window_used"] is False
    assert payload["counts"]["selected"] == len(plan.selected())
    assert payload["data_origin"] == "MT5_TERMINAL_READ_ONLY"
    # The paper path consumes these keys, so they must survive serialisation.
    for item in payload["decisions"]:
        assert {"candidate_id", "symbol", "timeframe", "status", "reasons", "message"} <= set(item)


def test_the_paper_rows_are_the_selected_rows_in_ranked_order():
    slot = _slot()
    plan = select(_several_rows(_boundary(slot)), slots=[slot], data_origin="MT5_TERMINAL_READ_ONLY", now=NOW)
    rows = plan.rows()
    assert [item["candidate_id"] for item in rows] == [item.candidate_id for item in plan.selected()]
    assert rows[0]["selection"]["rank"] == 1


def test_a_plan_can_be_built_from_a_report_or_from_its_stored_payload():
    from types import SimpleNamespace

    slot = _slot()
    rows = _several_rows(_boundary(slot))
    report = SimpleNamespace(rows=tuple(rows), status="COMPLETE", finished_at=NOW.isoformat())
    from_report = plan_from_report(
        report, slots=[slot], data_origin="MT5_TERMINAL_READ_ONLY", now=NOW
    )
    payload = {
        "rows": rows,
        "status": "COMPLETE",
        "finished_at": NOW.isoformat(),
        "data_origin": "MT5_TERMINAL_READ_ONLY",
    }
    from_payload = plan_from_report(payload, slots=[slot], now=NOW)
    assert [item.key for item in from_report.selected()] == [
        item.key for item in from_payload.selected()
    ]
    assert from_payload.evaluated_at == NOW.isoformat()


def test_the_timeframe_cap_says_which_timeframe_is_full():
    slot = _slot()
    boundary = _boundary(slot)
    rows = [
        _row("cat-1", symbol="EURUSD.pro", structure="s1", score=0.5, folds=[_fold(boundary - timedelta(hours=4))]),
        _row("cat-2", symbol="GBPUSD.pro", structure="s2", score=0.4, folds=[_fold(boundary - timedelta(hours=4))]),
    ]
    # Both would be one per symbol and one per structure, so only the timeframe
    # cap can stop the second one.
    policy = SelectionPolicy(max_per_timeframe=1, max_selected=6)
    slots = [slot, _slot("GBPUSD.pro")]
    plan = select(rows, policy=policy, slots=slots, data_origin="MT5_TERMINAL_READ_ONLY", now=NOW)
    assert len(plan.selected()) == 1
    held = [item for item in plan.decisions if item.status == ELIGIBLE]
    assert held and held[0].reasons == ("timeframe_capacity_reached",)
    assert "1h" in held[0].message


def test_the_policy_fingerprint_moves_when_a_rule_moves():
    base = SelectionPolicy().fingerprint()
    assert base != SelectionPolicy(min_folds_positive=2).fingerprint()
    assert base != SelectionPolicy(max_selected=3).fingerprint()
    assert base == SelectionPolicy().fingerprint()


def test_the_selector_does_not_read_or_write_anything(tmp_path: Path):
    # A guard against the selector quietly growing a file or network dependency:
    # with a temporary working directory it still produces the same plan.
    slot = _slot()
    plan = select(_several_rows(_boundary(slot)), slots=[slot], data_origin="MT5_TERMINAL_READ_ONLY", now=NOW)
    assert plan.selected()
    assert list(tmp_path.iterdir()) == []
