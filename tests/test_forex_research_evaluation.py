"""Deterministic offline tests for the multi-pair research evaluation workflow.

Everything here runs against local canonical-CSV imports and synthetic
instrument/calendar fixtures.  No network, provider, credential, or order path
is exercised, and nothing touches paper execution.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from axiom_forex.data.classes import (
    DATA_CLASS_SYNTHETIC_FIXTURE,
    DATA_CLASS_USER_SUPPLIED_DEMO,
    DATA_CLASS_USER_SUPPLIED_HISTORICAL,
    DATA_CLASS_USER_SUPPLIED_PRODUCTION,
)
from axiom_forex.evaluation import (
    EVALUATION_MODE,
    QUALIFICATION_NEVER_ACTIVATES,
    EvaluationConfig,
    EvaluationError,
    MultiPairBook,
    PairSeries,
    TestWindowRegistry as WindowRegistry,
    TestWindowReuseError as WindowReuseError,
    build_alignment,
    chronological_plan,
    run_research_evaluation,
    series_from_import,
    walk_forward_plans,
    windows_overlap,
)
from axiom_forex.evaluation.book import MissingMarkError
from axiom_forex.evaluation.report import (
    evaluation_summary,
    render_evaluation_markdown,
)
from axiom_forex.evaluation.splits import aligned_timeline, dataset_fingerprint
from dataclasses import replace
from axiom_forex.portfolio.costs import ForexCostModel
from fixtures.canonical_csv import build_canonical_csv, price_path
from fixtures.multi_pair import (
    ACCOUNT_CURRENCY,
    MULTI_PAIRS,
    PAIR_A,
    PAIR_B,
    PAIR_C,
    import_pair_csv,
    make_multi_pair_calendar,
    make_multi_pair_conversion_rates,
    make_multi_pair_evaluation_config,
    pair_reference_prices,
    session_specs,
)

UTC = timezone.utc
NOW = datetime(2026, 3, 11, 0, 0, tzinfo=UTC)
# Inside PAIR_A/PAIR_B/PAIR_C open sessions on a weekday.
DATA_START = datetime(2026, 3, 9, 13, 0, tzinfo=UTC)
BAR_COUNT = 160


def _starts(count: int = BAR_COUNT, start: datetime = DATA_START) -> list[datetime]:
    return [start + timedelta(minutes=index) for index in range(count)]


def _csv(
    pair: str,
    *,
    count: int = BAR_COUNT,
    start: datetime = DATA_START,
    step: Decimal = Decimal("0.00010"),
    block_bars: int = 8,
    base: Decimal | None = None,
    skip_indexes: tuple[int, ...] = (),
) -> str:
    reference = base if base is not None else pair_reference_prices()[pair]
    return build_canonical_csv(
        pair=pair,
        starts=_starts(count, start),
        mids=price_path(
            start=reference, step=step, count=count, block_bars=block_bars
        ),
        skip_indexes=skip_indexes,
    )


def _imported_series(
    pairs=MULTI_PAIRS,
    *,
    data_class: str = DATA_CLASS_USER_SUPPLIED_DEMO,
    count: int = BAR_COUNT,
    skip_indexes: tuple[int, ...] = (),
) -> dict[str, PairSeries]:
    series: dict[str, PairSeries] = {}
    for pair in pairs:
        result = import_pair_csv(
            _csv(pair, count=count, skip_indexes=skip_indexes),
            pair=pair,
            data_class=data_class,
            now=NOW,
        )
        assert result.status == "PASS", (pair, result.reason)
        assert result.dataset is not None
        series[pair] = series_from_import(result.dataset)
    return series


@pytest.fixture(scope="module")
def report():
    return run_research_evaluation(
        series=_imported_series(),
        config=make_multi_pair_evaluation_config(),
        calendar=make_multi_pair_calendar(),
        evaluation_id="eval-demo-multi-pair",
        now=NOW,
    )


# -- pair alignment --------------------------------------------------------


def _without_bars(item: PairSeries, indexes: range) -> PairSeries:
    """Drop bars from an already-validated dataset; a gap is reported, not filled."""

    kept = tuple(bar for index, bar in enumerate(item.bars) if index not in indexes)
    return replace(item, bars=kept)


def test_alignment_tracks_each_pair_calendar_and_missing_intervals():
    series = _imported_series()
    gapped = dict(series)
    gapped[PAIR_A] = _without_bars(series[PAIR_A], range(20, 26))

    alignment = build_alignment(
        series=gapped, calendar=make_multi_pair_calendar(), grid_seconds=60
    )

    coverage = {item.pair: item for item in alignment.pairs}
    assert set(coverage) == set(MULTI_PAIRS)
    assert alignment.grid_seconds == 60
    assert alignment.grid_slots == BAR_COUNT
    assert alignment.grid_start == DATA_START
    assert alignment.grid_end == DATA_START + timedelta(minutes=BAR_COUNT)

    gapped_a = coverage[PAIR_A]
    assert gapped_a.missing_slots >= 6
    assert len(gapped_a.missing_intervals) >= 1
    assert any(
        interval.start <= DATA_START + timedelta(minutes=20)
        and interval.end >= DATA_START + timedelta(minutes=25)
        for interval in gapped_a.missing_intervals
    )
    # Unbroken pairs stay fully covered and never gain phantom gaps.
    assert coverage[PAIR_B].missing_slots == 0
    assert coverage[PAIR_B].coverage_ratio == Decimal(1)
    assert gapped_a.coverage_ratio < Decimal(1)
    assert gapped_a.coverage_ratio < coverage[PAIR_B].coverage_ratio


def _series_for_window(pair: str, start: datetime, minutes: int) -> PairSeries:
    """Import one pair over an explicit window in which it is actually open."""

    result = import_pair_csv(
        _csv(pair, count=minutes, start=start),
        pair=pair,
        now=NOW,
    )
    assert result.status == "PASS", (pair, result.reason)
    assert result.dataset is not None
    return series_from_import(result.dataset)


def test_alignment_reports_asynchronous_sessions_explicitly():
    # 2026-03-09 (New York is on DST): PAIR_A breaks 21:00-22:00 UTC, PAIR_B
    # breaks 19:00-20:00 UTC, and PAIR_C trades through.  Feeding each pair only
    # the bars it actually has during 20:00-22:00 UTC therefore yields one hour
    # where PAIR_A is closed and the other two are open.
    start = datetime(2026, 3, 9, 20, 0, tzinfo=UTC)
    series = {
        PAIR_A: _series_for_window(PAIR_A, start, 60),
        PAIR_B: _series_for_window(PAIR_B, start, 120),
        PAIR_C: _series_for_window(PAIR_C, start, 120),
    }

    alignment = build_alignment(
        series=series, calendar=make_multi_pair_calendar(), grid_seconds=60
    )
    coverage = {item.pair: item for item in alignment.pairs}

    assert alignment.grid_start == start
    assert alignment.grid_slots == 120
    assert coverage[PAIR_A].closed_slots == 60
    assert coverage[PAIR_A].closed_intervals
    assert coverage[PAIR_A].coverage_ratio == Decimal(1)
    assert coverage[PAIR_B].closed_slots == 0
    assert coverage[PAIR_C].closed_slots == 0
    # The shared hour is reported as partly open, never assumed open for all.
    assert alignment.all_pairs_open_slots == 60
    assert alignment.partly_open_slots == 60
    assert alignment.closed_slots == 0
    # Absent bars in a closed window are calendar closures, not data gaps.
    assert coverage[PAIR_A].missing_slots == 0
    assert coverage[PAIR_A].out_of_session_bars == 0

    # Closing the last pair for a slot must not invent an open market.
    later = calendar_only = make_multi_pair_calendar()
    assert later.status(PAIR_A, datetime(2026, 3, 9, 21, 30, tzinfo=UTC)).status.value == (
        "DAILY_BREAK"
    )
    assert calendar_only.status(PAIR_C, datetime(2026, 3, 11, 1, 30, tzinfo=UTC)).status.value == (
        "CLOSED"
    )


# -- currency conversion and portfolio exposure ----------------------------


def make_multi_pair_correlation_matrix_for(pair: str):
    """A correlation matrix covering a single pair only."""

    from axiom_forex.risk.aggregate import CorrelationMatrix

    return CorrelationMatrix(
        pairs=frozenset({pair}),
        values={(pair, pair): Decimal(1)},
        source="SYNTHETIC_SINGLE_PAIR_CORRELATION",
        version="correlation-single-v1",
    )


def _book(**aggregate_overrides):
    """Multi-currency research book over the synthetic multi-pair terms."""

    from axiom_forex.evaluation.book import PairTerms
    from axiom_forex.portfolio.rollover import RolloverEngine
    from fixtures.multi_pair import (
        CALENDAR_ID,
        make_multi_pair_account,
        make_multi_pair_aggregate_limits,
        make_multi_pair_correlation_matrix,
        make_multi_pair_cost_terms,
        make_multi_pair_rollover_policy,
        make_multi_pair_swap_cards,
        make_pair_spec,
    )

    account = make_multi_pair_account()
    conversion = make_multi_pair_conversion_rates()
    cards = make_multi_pair_swap_cards()
    policy = make_multi_pair_rollover_policy()
    terms = {}
    for pair in MULTI_PAIRS:
        spec = make_pair_spec(pair)
        terms[pair] = PairTerms(
            pair=pair,
            spec=spec,
            cost_model=ForexCostModel(
                terms=make_multi_pair_cost_terms(),
                spec=spec,
                account=account,
                conversion_rates=conversion,
                rollover_engine=RolloverEngine(
                    policy=policy,
                    cards={pair: cards[pair]},
                    conversion_rates=conversion,
                    account=account,
                    calendar_id=CALENDAR_ID,
                ),
            ),
            rollover_cutoffs=(),
        )
    return MultiPairBook(
        account=account,
        terms=terms,
        conversion_rates=conversion,
        correlation_matrix=make_multi_pair_correlation_matrix(),
        aggregate_limits=make_multi_pair_aggregate_limits(**aggregate_overrides),
    )


class _Mark:
    """Minimal bid/ask bar stand-in exposing ``executable_price``.

    The default zero spread keeps the conversion arithmetic exact: a mark is
    the closing-side executable price, so a long is valued at the bid.
    """

    def __init__(self, mid: Decimal, spread: Decimal = Decimal("0")):
        half = spread / 2
        self.bid = mid - half
        self.ask = mid + half

    def executable_price(self, side, point: str = "close") -> Decimal:
        from app.common.types import OrderSide

        return self.ask if side == OrderSide.BUY else self.bid


def test_currency_conversion_is_applied_to_notional_and_pnl():
    book = _book()
    at = DATA_START
    # 1 lot = 1000 base units of AAA bought at 1.20000 AAA/BBB.
    position = book.open_position(
        position_id="p1",
        pair=PAIR_A,
        side=_buy(),
        quantity=Decimal("1"),
        price=Decimal("1.20000"),
        at=at,
        session="LONDON",
        regime="TRENDING_UP",
        decision_index=3,
        data_class=DATA_CLASS_USER_SUPPLIED_DEMO,
    )
    # AAA/BBB notional is in BBB; BBB converts to the CCC account at 2.0.
    assert position.base_units == Decimal("1000")
    assert position.entry_notional_account == Decimal("2400.000000")

    marks = {PAIR_A: _Mark(Decimal("1.21000"))}
    unrealized = book.unrealized_by_pair(marks, at + timedelta(minutes=1))
    # 1000 units x 10 pips x 2.0 (BBB->CCC) = 20.00 CCC.
    assert unrealized[PAIR_A] == Decimal("20.0000000")

    # Cash is held in three currencies and converted with the same explicit
    # rate book; equity is therefore expressed in the account currency only.
    assert set(book.cash_by_currency) == {"AAA", "BBB", ACCOUNT_CURRENCY}
    # Opening the trade moved cash but not equity beyond the entry costs, so
    # equity at the new mark equals capital - costs + unrealized P&L.
    equity = book.equity(marks, at + timedelta(minutes=1))
    assert equity == (
        book.account.initial_capital_decimal
        - position.entry_costs_account
        + Decimal("20.0000000")
    )


def test_shared_currency_exposure_aggregates_across_pairs():
    book = _book()
    at = DATA_START
    book.open_position(
        position_id="a1",
        pair=PAIR_A,  # long AAA / short BBB
        side=_buy(),
        quantity=Decimal("1"),
        price=Decimal("1.20000"),
        at=at,
        session="LONDON",
        regime="TRENDING_UP",
        decision_index=1,
        data_class=DATA_CLASS_USER_SUPPLIED_DEMO,
    )
    book.open_position(
        position_id="b1",
        pair=PAIR_B,  # long AAA / short CCC
        side=_buy(),
        quantity=Decimal("1"),
        price=Decimal("2.40000"),
        at=at,
        session="LONDON",
        regime="TRENDING_UP",
        decision_index=1,
        data_class=DATA_CLASS_USER_SUPPLIED_DEMO,
    )
    marks = {PAIR_A: _Mark(Decimal("1.20000")), PAIR_B: _Mark(Decimal("2.40000"))}
    exposure = book.currency_exposures(marks, at + timedelta(minutes=1))

    # Both pairs are long AAA, so the AAA leg is shared and additive, and every
    # leg is normalized to the CCC account currency by the explicit rate book.
    assert exposure["AAA"] == Decimal("4800.0")
    assert exposure["BBB"] == Decimal("-2400.0")
    assert exposure["CCC"] == Decimal("-2400")
    assert exposure["AAA"] == (
        Decimal("2") * Decimal("1000") * book.conversion_rates.rates[("AAA", ACCOUNT_CURRENCY)].rate
    )
    assert book.gross_exposure(marks, at + timedelta(minutes=1)) == (
        Decimal("2400") + Decimal("2400")
    )


def test_correlated_entry_is_rejected_by_aggregate_limits():
    book = _book()
    at = DATA_START
    book.open_position(
        position_id="a1",
        pair=PAIR_A,
        side=_buy(),
        quantity=Decimal("1"),
        price=Decimal("1.20000"),
        at=at,
        session="LONDON",
        regime="TRENDING_UP",
        decision_index=1,
        data_class=DATA_CLASS_USER_SUPPLIED_DEMO,
    )
    # Tighten the *currency* cap only: AAA is shared by both candidate pairs.
    # The existing AAA leg is 2400; the candidate would push AAA to 4800.
    tight = _book(max_abs_currency_exposure_account=Decimal("3000"))
    tight.positions.update(book.positions)
    tight.cash_by_currency.update(book.cash_by_currency)

    marks = {PAIR_A: _Mark(Decimal("1.20000")), PAIR_B: _Mark(Decimal("2.40000"))}
    decision = tight.entry_decision(
        pair=PAIR_B,
        side=_buy(),
        base_units=Decimal("1000"),
        price=Decimal("2.40000"),
        marks=marks,
        at=at + timedelta(minutes=1),
    )
    assert decision.allowed is False
    assert decision.reasons
    assert decision.max_abs_currency_exposure_account is not None
    # Treating the pairs independently would have approved this exposure: the
    # existing book alone is inside every limit, only the candidate breaches it.
    assert tight.aggregate_decision(marks, at + timedelta(minutes=1)).allowed is True


def test_missing_mark_fails_closed_instead_of_guessing_a_price():
    book = _book()
    at = DATA_START
    book.open_position(
        position_id="c1",
        pair=PAIR_C,
        side=_buy(),
        quantity=Decimal("1"),
        price=Decimal("2.00000"),
        at=at,
        session="LATE",
        regime="TRENDING_DOWN",
        decision_index=2,
        data_class=DATA_CLASS_USER_SUPPLIED_DEMO,
    )
    with pytest.raises(MissingMarkError):
        book.equity({}, at + timedelta(minutes=1))


def _buy():
    from app.common.types import OrderSide

    return OrderSide.BUY


# -- split / walk-forward isolation ---------------------------------------


def test_chronological_and_walk_forward_windows_never_reuse_held_out_data():
    series = _imported_series()
    timeline = aligned_timeline(series)
    plan = chronological_plan(
        timeline, fractions=(Decimal("0.6"), Decimal("0.2"), Decimal("0.2")), grid_seconds=60
    )
    assert plan.phases["TRAIN"].end == plan.phases["VALIDATION"].start
    assert plan.phases["VALIDATION"].end == plan.phases["TEST"].start
    assert plan.phases["TEST"].end == timeline[-1] + timedelta(seconds=60)

    folds = walk_forward_plans(
        tuple(stamp for stamp in timeline if stamp < plan.phases["TRAIN"].end),
        train_size=40,
        validation_size=10,
        test_size=20,
        step=20,
        grid_seconds=60,
    )
    assert folds
    for fold in folds:
        assert not windows_overlap(fold.test, plan.phases["TEST"])
        assert not windows_overlap(fold.test, plan.phases["VALIDATION"])
        assert fold.train.end <= fold.validation.start
        assert fold.validation.end <= fold.test.start


def test_reserved_test_window_cannot_be_reused(report):
    series = _imported_series()
    fingerprint = dataset_fingerprint(series)
    registry = WindowRegistry()
    held_out = report.split["TEST"]
    registry.reserve(fingerprint=fingerprint, start=held_out.start, end=held_out.end)

    with pytest.raises(WindowReuseError):
        registry.reserve(fingerprint=fingerprint, start=held_out.start, end=held_out.end)
    with pytest.raises(WindowReuseError):
        registry.reserve(
            fingerprint=fingerprint,
            start=held_out.start - timedelta(minutes=1),
            end=held_out.end,
        )
    # A different dataset fingerprint is a different evaluation, not a reuse.
    registry.reserve(
        fingerprint="other-dataset", start=held_out.start, end=held_out.end
    )


def test_second_run_on_the_same_set_is_blocked_as_test_reuse():
    from axiom_forex.evaluation import EvaluationSet

    evaluation = EvaluationSet(
        series=_imported_series(),
        config=make_multi_pair_evaluation_config(),
        calendar=make_multi_pair_calendar(),
        now=NOW,
    )
    assert evaluation.run().status == "PASS"
    with pytest.raises(WindowReuseError):
        evaluation.run()


def test_report_has_no_lookahead_and_keeps_folds_inside_development(report):
    bars = {pair: item.bars for pair, item in _imported_series().items()}
    for trade in report.phases["TEST"].trades:
        assert trade.exit_at > trade.entry_at
        assert trade.entry_at >= report.split["TEST"].start
        assert trade.exit_at <= report.split["TEST"].end
        # The fill is the *next* same-pair bar open after the decision bar: a
        # trade can never be filled at or before the bar that produced it.
        pair_bars = {bar.timestamp: bar for bar in bars[trade.pair]}
        decision_bar = pair_bars[
            report.split["TEST"].start + timedelta(minutes=trade.decision_index)
        ]
        assert trade.entry_at == decision_bar.end_at
        fill_bar = pair_bars[trade.entry_at]
        expected = fill_bar.executable_price(trade.side, point="open")
        assert trade.entry_price == expected
    for fold in report.folds:
        assert fold.test.window.start >= report.split["TRAIN"].start
        assert fold.test.window.end <= report.split["TRAIN"].end
        assert fold.test.window.end <= report.split["VALIDATION"].start


# -- eligibility and failure cases ----------------------------------------


def test_demo_data_is_evaluable_but_never_qualifies_or_activates(report):
    assert report.status == "PASS"
    assert report.eligibility.evaluation_allowed is True
    assert report.eligibility.qualification_allowed is False
    assert report.eligibility.activation_allowed is False
    assert "demo_data_not_qualifiable" in report.eligibility.qualification_reasons

    assert report.evaluation_data_class == DATA_CLASS_USER_SUPPLIED_DEMO
    assert report.qualification.qualified is False
    assert report.qualification.activation_allowed is False
    assert report.qualification.status != "QUALIFIED_FOR_RESEARCH_REVIEW"
    assert report.qualification.note == QUALIFICATION_NEVER_ACTIVATES
    assert any(
        check["name"] == "activation_never_allowed" and check["passed"]
        for check in report.qualification.checks
    )


def test_historical_class_can_qualify_for_review_but_still_never_activates():
    report = run_research_evaluation(
        series=_imported_series(data_class=DATA_CLASS_USER_SUPPLIED_HISTORICAL),
        config=make_multi_pair_evaluation_config(),
        calendar=make_multi_pair_calendar(),
        now=NOW,
    )
    assert report.status == "PASS"
    assert report.eligibility.qualification_allowed is True
    assert report.evaluation_data_class == DATA_CLASS_USER_SUPPLIED_HISTORICAL
    # Qualification may be positive or negative on synthetic price paths; what
    # must never change is that it cannot activate a strategy.
    assert report.qualification.activation_allowed is False
    assert report.qualification.note == QUALIFICATION_NEVER_ACTIVATES
    assert report.audit_view()["execution_isolation"]["creates_orders"] is False


def test_qualification_can_pass_for_review_yet_still_never_activates():
    """Relaxed thresholds over historical-class data can qualify for review."""

    report = run_research_evaluation(
        series=_imported_series(data_class=DATA_CLASS_USER_SUPPLIED_HISTORICAL),
        config=make_multi_pair_evaluation_config(
            walk_forward_test=30,
            walk_forward_step=30,
            min_positive_fold_ratio=Decimal("0"),
            max_drawdown_fraction=Decimal("1"),
        ),
        calendar=make_multi_pair_calendar(),
        now=NOW,
    )
    assert report.status == "PASS"
    assert report.qualification.status == "QUALIFIED_FOR_RESEARCH_REVIEW"
    assert report.qualification.qualified is True
    # Qualification is a research label only: activation and execution stay off.
    assert report.qualification.activation_allowed is False
    assert report.qualification.note == QUALIFICATION_NEVER_ACTIVATES
    assert report.audit_view()["execution_isolation"]["creates_orders"] is False
    assert report.eligibility.activation_allowed is False


def test_synthetic_fixture_class_is_not_evaluable():
    series = _imported_series()
    relabelled = {
        pair: PairSeries(
            pair=item.pair,
            spec=item.spec,
            timeframe=item.timeframe,
            timeframe_seconds=item.timeframe_seconds,
            provider_id=item.provider_id,
            provider_symbol=item.provider_symbol,
            bars=item.bars,
            quotes=item.quotes,
            quality=item.quality,
            data_class=DATA_CLASS_SYNTHETIC_FIXTURE,
            provenance=dict(item.provenance),
            import_manifest=item.import_manifest,
            dataset_manifest=item.dataset_manifest,
            import_manifest_valid=item.import_manifest_valid,
        )
        for pair, item in series.items()
    }
    config = make_multi_pair_evaluation_config()
    report = run_research_evaluation(
        series=relabelled, config=config, calendar=make_multi_pair_calendar(), now=NOW
    )
    assert report.status == "BLOCKED"
    assert report.passed is False
    assert "synthetic_fixture_not_evaluable" in report.eligibility.reasons
    assert report.phases == {}
    assert report.qualification.status == "BLOCKED"
    assert report.qualification.activation_allowed is False


def test_mixed_data_classes_are_rejected():
    series = _imported_series()
    series[PAIR_B] = _imported_series(
        (PAIR_B,), data_class=DATA_CLASS_USER_SUPPLIED_HISTORICAL
    )[PAIR_B]
    report = run_research_evaluation(
        series=series,
        config=make_multi_pair_evaluation_config(),
        calendar=make_multi_pair_calendar(),
        now=NOW,
    )
    assert report.status == "BLOCKED"
    assert "mixed_data_classes" in report.eligibility.reasons
    assert report.evaluation_data_class == "MIXED"


def test_production_data_is_refused_by_a_synthetic_fixture_profile():
    result = import_pair_csv(
        _csv(PAIR_A), pair=PAIR_A, data_class=DATA_CLASS_USER_SUPPLIED_PRODUCTION, now=NOW
    )
    assert result.status == "FAIL"
    assert "data_class" in result.reason
    assert result.dataset is None


def test_single_pair_set_cannot_produce_a_portfolio_evaluation():
    report = run_research_evaluation(
        series=_imported_series((PAIR_A,)),
        config=make_multi_pair_evaluation_config(),
        calendar=make_multi_pair_calendar(),
        now=NOW,
    )
    assert report.status == "BLOCKED"
    assert "insufficient_pairs" in report.eligibility.reasons


def test_insufficient_bars_blocks_evaluation():
    series = _imported_series(count=25)
    report = run_research_evaluation(
        series=series,
        config=make_multi_pair_evaluation_config(),  # min_bars_per_pair = 30
        calendar=make_multi_pair_calendar(),
        now=NOW,
    )
    assert report.status == "BLOCKED"
    assert "insufficient_bars" in report.eligibility.reasons
    assert report.phases == {}


def test_incomplete_coverage_blocks_evaluation():
    series = _imported_series()
    gapped = dict(series)
    gapped[PAIR_C] = _without_bars(series[PAIR_C], range(10, 60))
    report = run_research_evaluation(
        series=gapped,
        config=make_multi_pair_evaluation_config(min_coverage_ratio=Decimal("0.95")),
        calendar=make_multi_pair_calendar(),
        now=NOW,
    )
    assert report.status == "BLOCKED"
    assert "coverage_below_threshold" in report.eligibility.reasons


def test_out_of_session_bars_block_evaluation():
    # The importer refuses bars stamped inside a declared closure, so a dataset
    # carrying one can only arrive from an explicitly assembled series.  The
    # evaluation must still refuse it rather than silently trade a closed market.
    series = _imported_series(count=60)
    inside_break = datetime(2026, 3, 9, 21, 30, tzinfo=UTC)
    original = series[PAIR_A].bars[-1]
    stray = replace(
        original,
        timestamp=inside_break,
        last_quote_at=inside_break + timedelta(seconds=59),
    )
    series[PAIR_A] = replace(series[PAIR_A], bars=series[PAIR_A].bars[:-1] + (stray,))

    report = run_research_evaluation(
        series=series,
        config=make_multi_pair_evaluation_config(min_bars_per_pair=10),
        calendar=make_multi_pair_calendar(),
        now=NOW,
    )
    assert report.status == "BLOCKED"
    assert "bars_outside_calendar_session" in report.eligibility.reasons
    assert report.phases == {}


def test_missing_calendar_or_correlation_terms_block_evaluation():
    series = _imported_series()
    from fixtures.m1 import make_calendar

    calendar_report = run_research_evaluation(
        series=series,
        config=make_multi_pair_evaluation_config(),
        calendar=make_calendar(),  # declared for AAA/BBB only
        now=NOW,
    )
    assert calendar_report.status == "BLOCKED"
    assert {"calendar_missing_pair", "pair_calendar_mismatch"} & set(
        calendar_report.eligibility.reasons
    )

    single_pair_matrix = make_multi_pair_correlation_matrix_for(PAIR_A)
    matrix_report = run_research_evaluation(
        series=series,
        config=make_multi_pair_evaluation_config(correlation_matrix=single_pair_matrix),
        calendar=make_multi_pair_calendar(),
        now=NOW,
    )
    assert matrix_report.status == "BLOCKED"
    assert "correlation_missing_pair" in matrix_report.eligibility.reasons


def test_invalid_config_and_missing_terms_fail_closed():
    with pytest.raises(EvaluationError):
        run_research_evaluation(
            series=_imported_series(),
            config=make_multi_pair_evaluation_config(train_fraction=Decimal("0.9")),
            calendar=make_multi_pair_calendar(),
            now=NOW,
        )
    config = make_multi_pair_evaluation_config()
    empty = EvaluationConfig(
        strategy_id="",
        strategy_factory=config.strategy_factory,
        account=config.account,
        conversion_rates=config.conversion_rates,
        cost_terms=config.cost_terms,
        rollover_policy=config.rollover_policy,
        swap_cards=config.swap_cards,
        correlation_matrix=config.correlation_matrix,
        aggregate_limits=config.aggregate_limits,
    )
    assert "missing_strategy_id" in empty.validate()


# -- reporting -------------------------------------------------------------


def test_report_labels_dataset_class_provenance_and_isolation(report):
    view = report.audit_view()
    assert view["format"] == "AXIOM_FOREX_RESEARCH_EVALUATION"
    assert view["mode"] == EVALUATION_MODE == "RESEARCH_ONLY"
    assert view["data_class"] == DATA_CLASS_USER_SUPPLIED_DEMO
    assert all(
        item["data_class"] == DATA_CLASS_USER_SUPPLIED_DEMO
        and item["import_manifest_valid"] is True
        for item in view["datasets"]
    )
    assert {item["pair"] for item in view["provenance"]} == set(MULTI_PAIRS)
    for item in view["provenance"]:
        assert item["source"] == "SYNTHETIC_TEST_CSV"
        assert item["source_version"] == "v1"
        assert "/" not in item["source"]
    assert view["execution_isolation"] == {
        "creates_orders": False,
        "touches_paper_order_stream": False,
        "external_execution_enabled": False,
        "paper_broker_used": False,
    }
    assert "profitability" in view["note"]

    markdown = render_evaluation_markdown(report)
    assert "Research evaluation (multi-pair, imported data)" in markdown
    assert DATA_CLASS_USER_SUPPLIED_DEMO in markdown
    assert "SYNTHETIC_TEST_CSV" in markdown
    assert "not a profitability" in markdown
    assert "/home/" not in markdown
    summary = evaluation_summary(report)
    assert summary["data_class"] == DATA_CLASS_USER_SUPPLIED_DEMO
    assert summary["execution_isolation"]["creates_orders"] is False


def test_metrics_are_grouped_by_pair_session_regime_and_portfolio(report):
    expected_pairs = {
        trade.pair
        for phase in report.phases.values()
        for trade in phase.trades
    }
    assert {metric.key for metric in report.pair_metrics} == expected_pairs
    assert {metric.key for metric in report.session_metrics} <= {
        spec.name for spec in session_specs()
    }
    assert report.portfolio_metrics.kind == "portfolio"
    assert report.portfolio_metrics.trades == sum(
        metric.trades for metric in report.pair_metrics
    )
    assert report.portfolio_metrics.costs_account > 0
    assert report.portfolio_metrics.turnover_account > 0
    for metric in report.pair_metrics:
        assert metric.kind == "pair"
        assert metric.costs_account >= 0
        assert metric.trades == metric.wins + metric.losses


def test_evaluation_is_deterministic_across_runs():
    config = make_multi_pair_evaluation_config()
    first = run_research_evaluation(
        series=_imported_series(),
        config=config,
        calendar=make_multi_pair_calendar(),
        evaluation_id="stable",
        now=NOW,
    )
    second = run_research_evaluation(
        series=_imported_series(),
        config=config,
        calendar=make_multi_pair_calendar(),
        evaluation_id="stable",
        now=NOW,
    )
    assert first.audit_view() == second.audit_view()
    assert first.pair_metrics == second.pair_metrics


# -- operator surface ------------------------------------------------------


def test_platform_research_evaluation_is_wired_but_blocks_a_single_pair_import(
    tmp_path,
):
    from axiom_forex import build_fixture_platform as build_platform
    from axiom_forex.ingest import ImportRequest

    platform = build_platform(database_path=":memory:")
    try:
        assert platform.research_evaluation_summary()["status"] == "NOT_RUN"
        result = platform.import_dataset(
            ImportRequest(
                pair=platform.pair,
                timeframe="1m",
                data_class=DATA_CLASS_USER_SUPPLIED_DEMO,
                source="SAMPLE_CSV",
                source_version="v1",
                valid_from=datetime(2026, 3, 10, 0, 0, tzinfo=UTC),
                valid_until=datetime(2026, 3, 11, 0, 0, tzinfo=UTC),
            ),
            path="imports/SAMPLE_canonical_quote_bars.csv",
        )
        assert result.passed is True

        report = platform.research_evaluation()
        assert report.status == "BLOCKED"
        assert "insufficient_pairs" in report.eligibility.reasons
        assert report.qualification.activation_allowed is False

        summary = platform.research_evaluation_summary()
        assert summary["status"] == "BLOCKED"
        assert summary["imported_pairs"] == [platform.pair]
        assert summary["execution_isolation"]["creates_orders"] is False

        # The evaluation section is appended to the platform report and never
        # re-certifies the dataset as execution eligible.
        markdown = platform.report_markdown()
        assert "Research evaluation (multi-pair, imported data)" in markdown
        assert "SYNTHETIC" in markdown.upper()
        assert platform.verify()["checks"]["research_evaluation_isolated"] is True
        assert all(
            item.execution_eligible is False
            for item in platform.research_evaluation_datasets()
        )
    finally:
        platform.close()


# -- fill rules, rejections, and turnover ---------------------------------


class _Scripted:
    """Deterministic per-pair signal script keyed by bar index."""

    history_limit = 1
    name = "scripted"

    def __init__(self, plan):
        self.plan = dict(plan)
        self._seen = 0

    def generate_signal(self, bars):
        from axiom_forex.strategies.base import Signal

        index = len(bars) - 1
        self._seen = index
        return self.plan.get(index, Signal.HOLD)


def _phase_terms():
    from axiom_forex.evaluation.book import PairTerms
    from axiom_forex.portfolio.rollover import RolloverEngine
    from fixtures.multi_pair import (
        CALENDAR_ID,
        make_multi_pair_account,
        make_multi_pair_conversion_rates,
        make_multi_pair_cost_terms,
        make_multi_pair_rollover_policy,
        make_multi_pair_swap_cards,
        make_pair_spec,
    )

    account = make_multi_pair_account()
    conversion = make_multi_pair_conversion_rates()
    cards = make_multi_pair_swap_cards()
    policy = make_multi_pair_rollover_policy()
    terms = {}
    for pair in MULTI_PAIRS:
        spec = make_pair_spec(pair)
        terms[pair] = PairTerms(
            pair=pair,
            spec=spec,
            cost_model=ForexCostModel(
                terms=make_multi_pair_cost_terms(),
                spec=spec,
                account=account,
                conversion_rates=conversion,
                rollover_engine=RolloverEngine(
                    policy=policy,
                    cards={pair: cards[pair]},
                    conversion_rates=conversion,
                    account=account,
                    calendar_id=CALENDAR_ID,
                ),
            ),
            rollover_cutoffs=(),
        )
    return terms


def _run_scripted_phase(series, plan, *, hours=6, **config_overrides):
    from axiom_forex.evaluation import PhaseWindow, run_phase

    terms = _phase_terms()
    start = DATA_START
    window = PhaseWindow("TRAIN", start, start + timedelta(hours=hours))
    config = make_multi_pair_evaluation_config(
        strategy_factory=lambda pair: _Scripted(plan),
        quantity_by_pair={pair: Decimal("1") for pair in MULTI_PAIRS},
        **config_overrides,
    )
    return (
        run_phase(
            window=window,
            series=series,
            config=config,
            calendar=make_multi_pair_calendar(),
            book=_book(),
            pair_terms=terms,
            sessions=session_specs(),
        ),
        terms,
        config,
    )


def test_fills_use_next_bar_open_and_never_the_decision_bar():
    from axiom_forex.strategies.base import Signal

    series = _imported_series()
    phase, _, _ = _run_scripted_phase(
        series, {1: Signal.BUY, 5: Signal.EXIT}, hours=2
    )
    bars = {pair: item.bars for pair, item in series.items()}

    assert len(phase.trades) == len(MULTI_PAIRS)
    for trade in phase.trades:
        decision_bar = bars[trade.pair][trade.decision_index]
        assert trade.entry_at == decision_bar.end_at
        fill_bar = next(bar for bar in bars[trade.pair] if bar.timestamp == trade.entry_at)
        assert trade.entry_price == fill_bar.executable_price("BUY", point="open")
        exit_bar = next(bar for bar in bars[trade.pair] if bar.timestamp == trade.exit_at)
        assert trade.exit_price == exit_bar.executable_price("SELL", point="open")


def test_short_selling_is_refused_when_disabled():
    from axiom_forex.strategies.base import Signal

    series = _imported_series()
    phase, _, _ = _run_scripted_phase(
        series, {1: Signal.SELL}, hours=2, allow_short=False
    )
    assert phase.trades == ()
    codes = {item["code"] for item in phase.rejections}
    assert codes == {"SHORT_DISALLOWED"}
    assert {item["pair"] for item in phase.rejections} == set(MULTI_PAIRS)


def test_portfolio_position_limit_rejects_further_entries():
    from axiom_forex.strategies.base import Signal

    series = _imported_series()
    phase, _, _ = _run_scripted_phase(
        series, {1: Signal.BUY}, hours=2, max_open_positions=1
    )
    assert len(phase.open_positions) == 1
    codes = [item["code"] for item in phase.rejections]
    assert codes.count("MAX_OPEN_POSITIONS") == len(MULTI_PAIRS) - 1
    # Rejections are recorded, never silently dropped.
    assert len(phase.signals) >= len(phase.trades)


def test_exit_without_position_is_rejected():
    from axiom_forex.strategies.base import Signal

    series = _imported_series()
    phase, _, _ = _run_scripted_phase(series, {1: Signal.EXIT}, hours=1)
    assert phase.trades == ()
    assert {item["code"] for item in phase.rejections} == {"NO_OPEN_POSITION"}


def test_final_bar_signal_is_cancelled_not_filled():
    from axiom_forex.strategies.base import Signal

    series = _imported_series()
    # A one-hour phase holds 60 one-minute bars (13:00-13:59), so index 59 is
    # the last bar and its signal has no next bar to fill on.
    phase, _, _ = _run_scripted_phase(series, {59: Signal.BUY}, hours=1)
    assert max(item["index"] for item in phase.signals) == 59
    assert phase.trades == ()
    assert phase.cancelled_final_signals == len(MULTI_PAIRS)
    assert phase.rejections == ()


def test_phase_turnover_matches_two_sided_trade_notional():
    from axiom_forex.strategies.base import Signal

    series = _imported_series()
    phase, _, _ = _run_scripted_phase(
        series, {1: Signal.BUY, 5: Signal.EXIT}, hours=2
    )
    expected = sum(
        (trade.notional_account + trade.exit_notional_account for trade in phase.trades),
        Decimal(0),
    )
    assert phase.turnover_account == expected
    assert expected > 0
    assert phase.turnover_ratio > 0


def test_group_drawdown_is_derived_from_the_realized_trade_sequence(report):
    # Headline metrics use held-out evidence only, so the expectation is built
    # from the validation and test phases.
    trades = [
        trade
        for label in ("VALIDATION", "TEST")
        for trade in report.phases[label].trades
    ]
    for metric in report.pair_metrics:
        group = sorted(
            (trade for trade in trades if trade.pair == metric.key),
            key=lambda item: item.exit_at,
        )
        cumulative = Decimal(0)
        peak = Decimal(0)
        worst = Decimal(0)
        for trade in group:
            cumulative += trade.net_pnl_account
            peak = max(peak, cumulative)
            worst = max(worst, peak - cumulative)
        assert metric.max_drawdown_account == worst
    # The portfolio figure stays the equity-curve drawdown, not a trade sum.
    assert report.portfolio_metrics.max_drawdown_account >= max(
        (metric.max_drawdown_account for metric in report.pair_metrics),
        default=Decimal(0),
    )
