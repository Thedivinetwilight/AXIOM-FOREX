"""The strategy page must never let four different counts look like one.

What is pinned here:

* the library figure comes from the declared catalog, not from what happened to
  be evaluated, so a half-finished evaluation cannot look like a small library;
* parameter variants are reported as variants of a smaller number of rule
  structures -- the count is not padded, and it is not hidden either;
* passed, failed, "too little evidence", skipped and not-yet-evaluated are
  counted separately and add up to the library;
* distinct *strategies* are counted, not rows: one candidate on two symbols is
  one strategy;
* every row keeps its own reason, and failed rows are rendered, not filtered
  away;
* nothing reaches "live-ready" from a stored historical record.

All data here is synthetic fixture data.  Nothing in this file is evidence about
a real market, and no result here is a profitability claim.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from axiom_forex.strategies.catalog.candidates import summary_counts
from axiom_forex.strategies.catalog.funnel import (
    SCREEN_FAILED,
    SCREEN_PASSED,
    SCREEN_SKIPPED,
    SCREEN_THIN,
    build,
    empty,
)
from axiom_forex.strategies.library.panel import StrategyLibraryPanel
from tests.test_workstation_start import _app, _evaluate


def _record(rows, *, candidates=0, symbols=("EURUSD", "GBPUSD"), timeframe="1h"):
    """A stored record shaped like the evaluator's, with the fields we read."""

    return {
        "status": "COMPLETE",
        "data_origin": "MT5_TERMINAL_READ_ONLY",
        "claim": "Historical research estimate, not a forecast.",
        "notes": ["costs are labelled, not measured"],
        "plan": {
            "candidates_considered": candidates,
            "slots": [{"symbol": symbol, "timeframe": timeframe} for symbol in symbols],
        },
        "rows": rows,
    }


def _row(candidate, verdict, *, symbol="EURUSD", reasons=(), screen_reasons=(), trades=50):
    return {
        "candidate_id": candidate,
        "symbol": symbol,
        "timeframe": "1h",
        "family": "trend",
        "structure": "sma_cross+fixed+native",
        "screen": verdict,
        "reasons": list(reasons),
        "screen_reasons": list(screen_reasons),
        "trades": trades,
        "folds_positive": 3,
        "costs_incomplete": True,
        "score": 0.01,
    }


LIBRARY = {"candidates": 10, "distinct_rule_structures": 4, "families": {"trend": {}, "momentum": {}}, "note": "declared"}


# ---------------------------------------------------------------------------
# 1. the counts
# ---------------------------------------------------------------------------


def test_the_library_count_comes_from_the_catalog_not_from_the_record():
    """A partial evaluation must not shrink the reported library."""

    rows = [_row("cat-a", SCREEN_PASSED, screen_reasons=(), trades=100)]
    funnel = build(_record(rows, candidates=9999), library=LIBRARY)

    assert funnel["library"]["candidates"] == 10
    assert funnel["library"]["structures"] == 4
    assert funnel["counts"]["not_evaluated"] == 9


def test_variants_are_reported_alongside_the_structures_they_vary():
    funnel = build(_record([]), library=LIBRARY)

    # Neither number is hidden: the count is small per structure, not padded.
    assert funnel["library"]["candidates"] == 10
    assert funnel["library"]["structures"] == 4
    assert "structure" in funnel["library"]["note"].lower() or funnel["library"]["note"]


def test_one_candidate_on_two_symbols_counts_as_one_strategy_in_each_status():
    rows = [
        _row("cat-a", SCREEN_PASSED, symbol="EURUSD"),
        _row("cat-a", SCREEN_PASSED, symbol="GBPUSD"),
        _row("cat-b", SCREEN_FAILED, symbol="EURUSD", screen_reasons=["net return is not positive"]),
        _row("cat-b", SCREEN_FAILED, symbol="GBPUSD", screen_reasons=["net return is not positive"]),
    ]
    funnel = build(_record(rows, candidates=2), library=LIBRARY)

    counts = funnel["counts"]
    assert counts["passed_strategies"] == 1
    assert counts["passed_rows"] == 2
    assert counts["failed_strategies"] == 1
    assert counts["failed_rows"] == 2


def test_every_status_is_counted_separately_and_they_add_up_to_the_library():
    rows = [
        _row("cat-a", SCREEN_PASSED),
        _row("cat-b", SCREEN_FAILED, screen_reasons=["drawdown too deep"]),
        _row("cat-c", SCREEN_THIN, screen_reasons=["fewer than 8 trades in train (2)"]),
        _row("cat-d", SCREEN_SKIPPED, reasons=["timeframe_not_offered_by_candidate"], trades=0),
    ]
    funnel = build(_record(rows, candidates=4), library=LIBRARY)

    counts = funnel["counts"]
    total = (
        counts["passed_strategies"]
        + counts["failed_strategies"]
        + counts["thin_strategies"]
        + counts["skipped_strategies"]
        + counts["not_evaluated"]
    )
    assert total == funnel["library"]["candidates"]
    assert counts["passed_strategies"] == 1
    assert counts["failed_strategies"] == 1
    assert counts["thin_strategies"] == 1
    assert counts["skipped_strategies"] == 1
    assert counts["not_evaluated"] == 6  # 10 declared, 4 accounted for


def test_a_candidate_skipped_for_its_timeframe_is_not_evaluated_and_not_compatible():
    rows = [
        _row("cat-a", SCREEN_PASSED),
        _row("cat-b", SCREEN_SKIPPED, reasons=["timeframe_not_offered_by_candidate"], trades=0),
    ]
    funnel = build(_record(rows, candidates=2), library=LIBRARY)

    assert funnel["compatible"]["candidates"] == 1
    assert funnel["compatible"]["incompatible_candidates"] == 1
    assert funnel["evaluated"]["candidates"] == 1
    assert funnel["evaluated"]["rows"] == 1


def test_selected_counts_the_operators_choices_not_the_passers():
    rows = [_row("cat-a", SCREEN_PASSED), _row("cat-b", SCREEN_PASSED)]
    funnel = build(_record(rows, candidates=2), library=LIBRARY, selected=["cat-b"])

    assert funnel["counts"]["selected"] == 1
    assert funnel["counts"]["passed_strategies"] == 2


# ---------------------------------------------------------------------------
# 2. no result is hidden, and each carries its reason
# ---------------------------------------------------------------------------


def test_a_failed_row_keeps_its_verbatim_reason():
    rows = [
        _row(
            "cat-b",
            SCREEN_FAILED,
            screen_reasons=[
                "train net return after costs is not positive",
                "one validation trade carries 100% of the validation result",
            ],
        )
    ]
    funnel = build(_record(rows, candidates=1), library=LIBRARY, verdict=SCREEN_FAILED)

    assert funnel["rows"]
    reason = funnel["rows"][0]["reason"]
    assert "train net return after costs is not positive" in reason
    assert "one validation trade carries 100%" in reason


def test_a_skipped_row_keeps_the_reason_it_did_not_run():
    rows = [_row("cat-d", SCREEN_SKIPPED, reasons=["timeframe_not_offered_by_candidate"], trades=0)]
    funnel = build(_record(rows, candidates=1), library=LIBRARY, verdict=SCREEN_SKIPPED)

    assert funnel["rows"][0]["reason"] == "timeframe_not_offered_by_candidate"
    assert funnel["rows"][0]["trades"] == 0


def test_every_verdict_is_reachable_by_filter_including_failures():
    rows = [
        _row("cat-a", SCREEN_PASSED),
        _row("cat-b", SCREEN_FAILED, screen_reasons=["too concentrated"]),
        _row("cat-c", SCREEN_THIN, screen_reasons=["fewer than 8 trades"]),
        _row("cat-d", SCREEN_SKIPPED, reasons=["timeframe_not_offered_by_candidate"]),
    ]
    for verdict, expected in (
        (SCREEN_PASSED, "cat-a"),
        (SCREEN_FAILED, "cat-b"),
        (SCREEN_THIN, "cat-c"),
        (SCREEN_SKIPPED, "cat-d"),
    ):
        funnel = build(_record(rows, candidates=4), library=LIBRARY, verdict=verdict)
        assert [row["candidate_id"] for row in funnel["rows"]] == [expected]


def test_the_table_is_bounded_but_says_how_many_rows_there_are():
    rows = [_row(f"cat-{index}", SCREEN_THIN, screen_reasons=["fewer than 8 trades"]) for index in range(200)]
    funnel = build(_record(rows, candidates=200), library=LIBRARY)

    assert funnel["shown"] == 60
    assert funnel["matching"] == 200


# ---------------------------------------------------------------------------
# 3. the readiness ladder
# ---------------------------------------------------------------------------


def test_nothing_is_ever_live_ready_from_a_historical_record():
    rows = [_row("cat-a", SCREEN_PASSED)]
    funnel = build(_record(rows, candidates=1), library=LIBRARY)

    assert funnel["ladder"]["live_ready"] == 0
    assert "live plan" in funnel["ladder"]["live_ready_note"]
    assert "confirmation phrase" in funnel["ladder"]["live_ready_note"]


def test_the_ladder_descends_from_library_to_paper_testable():
    rows = [
        _row("cat-a", SCREEN_PASSED),
        _row("cat-b", SCREEN_FAILED, screen_reasons=["negative validation"]),
        _row("cat-c", SCREEN_SKIPPED, reasons=["timeframe_not_offered_by_candidate"]),
    ]
    funnel = build(_record(rows, candidates=3), library={**LIBRARY, "candidates": 3})
    ladder = funnel["ladder"]

    assert ladder["in_library"] == 3
    assert ladder["evaluated"] == 2
    assert ladder["paper_testable"] == 1
    assert ladder["live_ready"] == 0


def test_an_empty_funnel_still_reports_the_whole_library_as_unevaluated():
    funnel = empty(library=LIBRARY)

    assert funnel["available"] is False
    assert funnel["library"]["candidates"] == 10
    assert funnel["counts"]["not_evaluated"] == 10
    assert funnel["ladder"]["live_ready"] == 0


# ---------------------------------------------------------------------------
# 4. the page
# ---------------------------------------------------------------------------


def _page_app(tmp_path):
    app = _app(tmp_path, connect=True, symbols=True)
    app.strategies = StrategyLibraryPanel(root=tmp_path)
    _evaluate(app)
    return app


def _text(html: str) -> str:
    body = html.split("<div class='content'>", 1)[1].split("<footer", 1)[0]
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))


def test_the_page_shows_every_count_separately(tmp_path):
    app = _page_app(tmp_path)
    _status, html = app.page("/strategies")
    text = _text(html)

    for label in (
        "In the library",
        "Compatible now",
        "Evaluated",
        "Passed the screen",
        "Failed",
        "Too little evidence",
        "Skipped",
        "Not yet evaluated",
        "Selected",
    ):
        assert label in text, label

    funnel = app.strategy_funnel()
    assert funnel["library"]["candidates"] == summary_counts()["candidates"]
    assert f"{funnel['library']['candidates']:,}" in text


def test_the_page_links_to_a_filter_for_every_status(tmp_path):
    app = _page_app(tmp_path)
    _status, html = app.page("/strategies")

    for verdict in (SCREEN_PASSED, SCREEN_FAILED, SCREEN_THIN, SCREEN_SKIPPED):
        assert f"verdict={verdict}" in html


def test_the_page_renders_failed_candidates_with_their_reasons(tmp_path):
    app = _page_app(tmp_path)
    _status, html = app.page("/strategies", {"verdict": [SCREEN_FAILED]})
    text = _text(html)

    funnel = app.strategy_funnel(query={"verdict": [SCREEN_FAILED]})
    assert funnel["counts"]["failed_rows"] > 0, "the fixture must produce a failed row"
    assert "Failed" in text
    # The reason text is the record's own words, not a placeholder.
    assert "fewer than 8 trades" in text or "not positive" in text or "concentrat" in text.lower()


def test_the_page_never_calls_a_passing_result_live_ready_or_profitable(tmp_path):
    app = _page_app(tmp_path)
    _status, html = app.page("/strategies")
    text = _text(html)

    assert "Live-ready" in text
    assert "backtests cannot reach this rung" in text
    lowered = text.lower()
    # Any mention of profit must be a denial, never a claim.  Asserting on the
    # bare words would match the disclaimer that contains them.
    assert "guaranteed" not in lowered
    assert "not a profitable strategy" in lowered
    assert "no profitability claim" in lowered
    # The ladder figure is zero, and the page says why.
    assert "Nothing reaches this rung" in text


def test_the_page_renders_before_anything_is_evaluated(tmp_path):
    app = _app(tmp_path, connect=True, symbols=True)
    app.strategies = StrategyLibraryPanel(root=tmp_path)
    _status, html = app.page("/strategies")
    text = _text(html)

    assert "No evaluation on record yet" in text
    assert "In the library" in text
    funnel = app.strategy_funnel()
    assert funnel["available"] is False
    assert funnel["counts"]["not_evaluated"] == summary_counts()["candidates"]


def test_the_strategy_page_offers_no_action_that_could_trade_or_arm(tmp_path):
    app = _page_app(tmp_path)
    _status, html = app.page("/strategies")

    for forbidden in (
        "/actions/live-arm",
        "/actions/live-start",
        "/actions/runner-start",
        "/actions/mode-live",
        "/actions/buy",
        "/actions/sell",
        "order_send",
    ):
        assert forbidden not in html, forbidden


# ---------------------------------------------------------------------------
# 5. browse every row, and evaluate the whole catalog from this page
# ---------------------------------------------------------------------------


def test_paging_reaches_rows_past_the_first_page():
    rows = [
        _row(f"cat-{index:04d}", SCREEN_THIN, screen_reasons=["fewer than 8 trades"])
        for index in range(150)
    ]
    first = build(_record(rows, candidates=150), library=LIBRARY)
    second = build(_record(rows, candidates=150), library=LIBRARY, offset=60)

    assert first["rows"][0]["candidate_id"] != second["rows"][0]["candidate_id"]
    assert first["has_next"] is True
    assert second["has_previous"] is True
    # Every row stays reachable: paging never drops the tail.
    tail = build(_record(rows, candidates=150), library=LIBRARY, offset=120)
    assert tail["shown"] == 30
    assert tail["has_next"] is False


def test_the_page_offers_a_next_link_when_there_are_more_rows(tmp_path):
    app = _page_app(tmp_path)
    _status, html = app.page("/strategies")

    assert "Next" in html
    assert "from=60" in html


def test_the_page_can_evaluate_the_whole_catalog_without_a_new_endpoint(tmp_path):
    app = _page_app(tmp_path)
    _status, html = app.page("/strategies")

    assert "/actions/catalog-evaluate" in html
    assert "name='return_to' value='/strategies'" in html
    # It evaluates the selection the funnel counted, not a default.
    assert "name='symbols' value='EURUSD'" in html
    assert "name='timeframe' value='1h'" in html


def test_evaluating_from_the_strategy_page_returns_to_it(tmp_path):
    from tests.test_workstation_start import _wait_for

    app = _app(tmp_path, connect=True, symbols=True)
    app.strategies = StrategyLibraryPanel(root=tmp_path)
    status, back, _message = app.act(
        "catalog-evaluate",
        {
            "nonce": [app.nonce],
            "return_to": ["/strategies"],
            "symbols": ["EURUSD", "GBPUSD"],
            "timeframe": ["1h"],
        },
    )
    assert int(status) == 303
    assert back == "/strategies"
    _wait_for(lambda: not app.catalog.evaluation_view()["running"])

    funnel = app.catalog.funnel()
    assert funnel["available"] is True
    assert funnel["library"]["candidates"] == summary_counts()["candidates"]


def test_an_unknown_return_target_falls_back_to_the_catalog_page(tmp_path):
    app = _app(tmp_path, connect=True, symbols=True)
    app.strategies = StrategyLibraryPanel(root=tmp_path)
    _status, back, _message = app.act(
        "catalog-evaluate",
        {"nonce": [app.nonce], "return_to": ["https://example.invalid/"], "symbols": ["EURUSD"]},
    )
    assert back == "/quick-setup"


def test_every_row_carries_its_version_parameters_and_compatibility():
    rows = [_row("cat-436360f1fa675ee5", SCREEN_THIN, screen_reasons=["fewer than 8 trades"])]
    funnel = build(_record(rows, candidates=1), library=LIBRARY, verdict=SCREEN_THIN)

    row = funnel["rows"][0]
    assert row["signature"], "a row without its rule signature is not reproducible"
    assert row["parameters"]
    assert isinstance(row["timeframes"], list) and row["timeframes"]
    assert row["execution_compatible"] is True
    assert row["min_bars"] > 0
