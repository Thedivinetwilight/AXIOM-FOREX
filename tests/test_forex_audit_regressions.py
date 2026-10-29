"""Adversarial audit regressions for the forex research implementation.

Each test here is a counterexample found by tracing the code rather than by
re-running the existing suite.  They are deliberately narrow: they pin one
numeric invariant each, so a future change cannot silently reintroduce the
defect they were written for.  Everything is offline; no provider, broker,
credential, order, or live-system path is touched.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.calendar.trading import MarketStatus
from app.common.types import OrderSide
from app.data.models import QuoteSideBar
from axiom_forex.data.classes import (
    DATA_CLASS_USER_SUPPLIED_DEMO,
    DATA_CLASS_USER_SUPPLIED_HISTORICAL,
)
from axiom_forex.evaluation import (
    EvaluationSet,
    TestWindowReuseError as WindowReuseError,
    TestWindowRegistry as WindowRegistry,
    dataset_fingerprint,
    run_research_evaluation,
)
from axiom_forex.evaluation.book import MultiPairBook
from axiom_forex.evaluation.research import _cutoffs

from fixtures.multi_pair import (
    PAIR_A,
    make_multi_pair_calendar,
    make_multi_pair_evaluation_config,
)
from tests.test_forex_research_evaluation import NOW, _imported_series

UTC = timezone.utc
ZERO = Decimal(0)


@pytest.fixture(scope="module")
def config():
    return make_multi_pair_evaluation_config()


@pytest.fixture(scope="module")
def series():
    return _imported_series()


@pytest.fixture(scope="module")
def terms(config, series):
    evaluation = EvaluationSet(
        series=series,
        config=config,
        calendar=make_multi_pair_calendar(),
    )
    return evaluation._pair_terms()


@pytest.fixture()
def new_book(config, terms):
    def make() -> MultiPairBook:
        return MultiPairBook(
            account=config.account,
            terms=terms,
            conversion_rates=config.conversion_rates,
            correlation_matrix=config.correlation_matrix,
            aggregate_limits=config.aggregate_limits,
        )

    return make


def _flat_mark(bar, price: Decimal):
    """A completed bar whose closing bid and ask are both ``price``."""

    side = QuoteSideBar(open=price, high=price, low=price, close=price)
    return replace(bar, bid=side, ask=side)


# -- realized P&L and costs -------------------------------------------------


def test_trade_net_pnl_subtracts_both_entry_and_exit_costs(config, series, new_book):
    book = new_book()
    bars = series[PAIR_A].bars
    entry_bar = bars[3]
    entry_price = entry_bar.executable_price(OrderSide.BUY, point="open")
    position = book.open_position(
        position_id="p",
        pair=PAIR_A,
        side=OrderSide.BUY,
        quantity=Decimal("1"),
        price=entry_price,
        at=entry_bar.timestamp,
        session="S",
        regime="R",
        decision_index=1,
        data_class=DATA_CLASS_USER_SUPPLIED_DEMO,
    )
    trade = book.close_position(
        position_id="p", price=entry_price, at=bars[6].timestamp
    )

    # Same price in and out: the only P&L is cost.
    assert trade.gross_pnl_account == ZERO
    assert trade.costs_account == position.entry_costs_account + (
        trade.costs_account - position.entry_costs_account
    )
    assert trade.costs_account > position.entry_costs_account
    # The three reported figures must agree on one number, not two.
    assert trade.net_pnl_account == trade.gross_pnl_account - trade.costs_account
    assert trade.net_pnl_account == -trade.costs_account
    # The book-level accumulator uses the same definition.
    assert book.realized_by_pair[PAIR_A] == trade.net_pnl_account
    assert book.cost_total_account == trade.costs_account


def test_report_net_pnl_agrees_between_trade_metrics_and_equity(series, config):
    report = run_research_evaluation(
        series=series,
        config=config,
        calendar=make_multi_pair_calendar(),
        now=NOW,
    )
    assert report.status == "PASS"
    trades = [trade for phase in report.phases.values() for trade in phase.trades]
    assert trades

    for trade in trades:
        assert trade.net_pnl_account == (
            trade.gross_pnl_account - trade.costs_account
        ), trade.trade_id
        assert trade.net_pnl_account < trade.gross_pnl_account

    # Headline metrics must equal the sum of the trades they summarise.  The
    # headline groups held-out evidence only (validation + test), so the
    # comparison uses exactly those phases.
    headline_trades = [
        trade
        for label in ("VALIDATION", "TEST")
        for trade in report.phases[label].trades
    ]
    assert report.portfolio_metrics.net_pnl_account == sum(
        (trade.net_pnl_account for trade in headline_trades), ZERO
    )
    assert report.portfolio_metrics.gross_pnl_account - (
        report.portfolio_metrics.costs_account
    ) == report.portfolio_metrics.net_pnl_account

    # A phase that ends flat reports the same P&L whether it is derived from
    # the equity curve or from the closed trades inside it.
    flat_phases = [
        phase for phase in report.phases.values() if not phase.open_positions
    ]
    assert flat_phases
    for phase in flat_phases:
        assert phase.net_pnl_account == sum(
            (trade.net_pnl_account for trade in phase.trades), ZERO
        ), phase.window.label


# -- equity / mark-to-market -------------------------------------------------


def test_equity_at_the_entry_price_equals_liquidation_value(config, series, new_book):
    """Holding a position cannot change equity beyond its costs and marks.

    The pair price and the static conversion book are separate declared inputs;
    when they disagree, an open position must still be valued at its own
    liquidation mark.  Otherwise opening a trade at a price away from the
    implied conversion cross produces a spurious equity jump that disappears
    when the position closes, and the drawdown/equity curve contradicts the
    realized P&L reported for the same trade.
    """

    book = new_book()
    bars = series[PAIR_A].bars
    entry_bar = bars[5]
    entry_price = entry_bar.executable_price(OrderSide.BUY, point="open")
    book.open_position(
        position_id="p",
        pair=PAIR_A,
        side=OrderSide.BUY,
        quantity=Decimal("1"),
        price=entry_price,
        at=entry_bar.timestamp,
        session="S",
        regime="R",
        decision_index=1,
        data_class=DATA_CLASS_USER_SUPPLIED_DEMO,
    )
    entry_costs = book.cost_total_account
    capital = config.account.initial_capital_decimal

    # Marked exactly at the fill price: unrealized P&L is zero.
    marks = {PAIR_A: _flat_mark(entry_bar, entry_price)}
    assert book.unrealized_by_pair(marks, entry_bar.timestamp)[PAIR_A] == ZERO
    assert book.equity(marks, entry_bar.timestamp) == capital - entry_costs

    # A one-pip favourable move is worth exactly one pip of account value.
    pip = series[PAIR_A].spec.pip_size_decimal()
    better = entry_price + pip
    marks_up = {PAIR_A: _flat_mark(entry_bar, better)}
    quote_rate = config.conversion_rates.rates[("BBB", "DDD")].rate
    expected = Decimal("1000") * pip * quote_rate
    assert book.unrealized_by_pair(marks_up, entry_bar.timestamp)[PAIR_A] == expected
    assert book.equity(marks_up, entry_bar.timestamp) == (
        capital - entry_costs + expected
    )

    # Closing at the entry price restores exactly capital minus all costs.
    trade = book.close_position(
        position_id="p", price=entry_price, at=bars[8].timestamp
    )
    assert trade.net_pnl_account == -trade.costs_account
    assert book.equity({PAIR_A: _flat_mark(bars[8], entry_price)}, bars[8].timestamp) == (
        capital - trade.costs_account
    )


def test_short_position_equity_moves_inversely(config, series, new_book):
    book = new_book()
    bars = series[PAIR_A].bars
    bar = bars[5]
    entry_price = bar.executable_price(OrderSide.SELL, point="open")
    book.open_position(
        position_id="s",
        pair=PAIR_A,
        side=OrderSide.SELL,
        quantity=Decimal("1"),
        price=entry_price,
        at=bar.timestamp,
        session="S",
        regime="R",
        decision_index=1,
        data_class=DATA_CLASS_USER_SUPPLIED_DEMO,
    )
    capital = config.account.initial_capital_decimal
    costs = book.cost_total_account

    assert book.equity({PAIR_A: _flat_mark(bar, entry_price)}, bar.timestamp) == (
        capital - costs
    )
    worse = entry_price + series[PAIR_A].spec.pip_size_decimal()
    quote_rate = config.conversion_rates.rates[("BBB", "DDD")].rate
    loss = Decimal("1000") * series[PAIR_A].spec.pip_size_decimal() * quote_rate
    assert book.equity({PAIR_A: _flat_mark(bar, worse)}, bar.timestamp) == (
        capital - costs - loss
    )


# -- test-window custody -----------------------------------------------------


def test_dataset_fingerprint_tracks_bar_content_not_just_labels(series):
    relabeled = {
        pair: replace(item, data_class=DATA_CLASS_USER_SUPPLIED_HISTORICAL)
        for pair, item in series.items()
    }
    # The same bars remain the same evaluated dataset even if their declared
    # class or source label changes: re-labelling must not buy a fresh
    # out-of-sample window.
    assert dataset_fingerprint(series) == dataset_fingerprint(relabeled)

    bars = list(series[PAIR_A].bars)
    original = bars[10]
    moved = replace(
        original,
        bid=replace(original.bid, close=Decimal(original.bid.close) + Decimal("0.5")),
    )
    bars[10] = moved
    changed = dict(series)
    changed[PAIR_A] = replace(series[PAIR_A], bars=tuple(bars))
    assert dataset_fingerprint(changed) != dataset_fingerprint(series)


def test_relabeled_data_cannot_reuse_a_consumed_test_window(series):
    relabeled = {
        pair: replace(item, data_class=DATA_CLASS_USER_SUPPLIED_HISTORICAL)
        for pair, item in series.items()
    }
    start = datetime(2026, 3, 9, 14, 0, tzinfo=UTC)
    end = datetime(2026, 3, 9, 15, 0, tzinfo=UTC)
    registry = WindowRegistry()
    registry.reserve(
        fingerprint=dataset_fingerprint(series), start=start, end=end
    )
    with pytest.raises(WindowReuseError):
        registry.reserve(
            fingerprint=dataset_fingerprint(relabeled), start=start, end=end
        )


# -- financing / rollover ----------------------------------------------------


def test_financing_cutoffs_skip_days_the_instrument_does_not_trade(config):
    calendar = make_multi_pair_calendar()
    cutoffs = _cutoffs(config, calendar, PAIR_A)
    assert cutoffs
    non_trading = {
        MarketStatus.WEEKEND_GAP,
        MarketStatus.HOLIDAY,
        MarketStatus.NOT_YET_VALID,
        MarketStatus.CALENDAR_EXPIRED,
        MarketStatus.INSTRUMENT_UNAVAILABLE,
    }
    for cutoff in cutoffs:
        status = calendar.status(PAIR_A, cutoff)
        assert status.status not in non_trading, (cutoff, status.status)
    # The fixture closes at the weekend: no Saturday/Sunday rollover may remain.
    assert not {day.weekday() for day in cutoffs} & {5, 6}


def test_held_position_accrues_financing_only_on_trading_cutoffs(terms, config):
    """A Friday-to-Monday hold must not also pay Saturday and Sunday."""

    calendar = make_multi_pair_calendar()
    cutoffs = _cutoffs(config, calendar, PAIR_A)
    engine = terms[PAIR_A].cost_model.rollover_engine
    bars = _imported_series()[PAIR_A].bars
    position = type(
        "Position",
        (),
        {
            "pair": PAIR_A,
            "side": OrderSide.BUY,
            "base_units": Decimal("1000"),
            "entry_price": Decimal("1.20000"),
            "entry_timestamp": datetime(2026, 3, 9, 13, 0, tzinfo=UTC),
        },
    )()
    as_of = datetime(2026, 3, 17, 0, 0, tzinfo=UTC)
    accrual = engine.accrue(position, cutoffs=cutoffs, as_of=as_of)
    assert accrual.events
    event_days = {event.cutoff_at.date() for event in accrual.events}
    assert not {day for day in event_days if day.weekday() >= 5}
    assert len(accrual.events) == len(event_days)
    # Every event is a declared cutoff the calendar actually trades through.
    for event in accrual.events:
        assert calendar.status(PAIR_A, event.cutoff_at).status is MarketStatus.DAILY_BREAK
    assert bars  # the fixture data exists; this is a pure schedule assertion


# -- the single-pair paper ledger is the second implementation --------------


def test_paper_ledger_realized_pnl_is_net_of_both_sides(platform):
    bundle = platform.engine_factory()
    portfolio = bundle.portfolio
    bar = platform.bars[1]
    entry_price = bar.ask.open
    position = portfolio.open(
        position_id="audit-open",
        side=OrderSide.BUY,
        quantity=Decimal("1"),
        price=entry_price,
        at=bar.timestamp,
    )
    trade = portfolio.close("audit-open", price=entry_price, at=bar.timestamp)

    assert trade.entry_costs_account == position.costs.total_account > 0
    assert trade.realized_pnl_account == -trade.total_costs_account
    assert trade.total_costs_account == (
        trade.entry_costs_account + trade.costs.total_account
    )
    # The portfolio-level cost accumulator and the trade agree.
    assert portfolio.cost_total_account == trade.total_costs_account
    assert portfolio.realized_pnl_today_account == trade.realized_pnl_account


def test_paper_ledger_equity_marks_the_position_not_the_static_cross(platform):
    bundle = platform.engine_factory()
    portfolio = bundle.portfolio
    bar = platform.bars[1]
    capital = platform.account.initial_capital_decimal
    entry_price = bar.ask.open
    position = portfolio.open(
        position_id="audit-mark",
        side=OrderSide.BUY,
        quantity=Decimal("1"),
        price=entry_price,
        at=bar.timestamp,
    )
    costs = position.costs.total_account

    flat = replace(
        bar,
        bid=QuoteSideBar(
            open=entry_price, high=entry_price, low=entry_price, close=entry_price
        ),
        ask=QuoteSideBar(
            open=entry_price, high=entry_price, low=entry_price, close=entry_price
        ),
    )
    # Marked at the fill price: no unrealized P&L, so equity is capital - costs.
    assert portfolio.equity_account(bar.timestamp, flat) == capital - costs

    # One pip better moves equity by exactly one pip of account value.
    pip = platform.spec.pip_size_decimal()
    better = entry_price + pip
    up = replace(
        flat,
        bid=QuoteSideBar(open=better, high=better, low=better, close=better),
        ask=QuoteSideBar(open=better, high=better, low=better, close=better),
    )
    quote_rate = portfolio._rate(platform.spec.quote_currency, bar.timestamp)
    units = position.base_units
    assert portfolio.equity_account(bar.timestamp, up) == (
        capital - costs + units * pip * quote_rate
    )


# -- paper-order persistence / restart recovery -----------------------------


def test_restart_refuses_a_fill_whose_settlement_ledger_is_missing(tmp_path):
    """A fill without the ledger state it produced must fail closed.

    Settlement writes the order, the fill, the ledger snapshot, the reservation,
    and the audit event in one transaction, so a process can no longer die
    between them.  What *can* still happen is a database written by an older
    path, or edited by hand: a fill with no settlement snapshot.  Recovering
    that would hand the risk gate a book that silently disagrees with its own
    fills, so recovery refuses instead.
    """

    from axiom_forex import build_fixture_platform

    database_path = tmp_path / "audit-recovery.db"
    first = build_fixture_platform(database_path=str(database_path))
    try:
        result = first.run_paper_workflow(workflow_id="audit-workflow-before-crash")
        assert result.executed is True
        fills = first.repository.list_fills()
        assert fills
        victim = fills[-1]
        first.repository.connection.execute(
            "DELETE FROM snapshots WHERE snapshot_id = ?",
            (f"portfolio-settlement:{victim['simulation_id']}",),
        )
        first.repository.connection.commit()
    finally:
        first.close()

    with pytest.raises(ValueError, match="settlement_snapshot_missing"):
        build_fixture_platform(database_path=str(database_path))


def test_restart_refuses_a_book_older_than_its_newest_settlement(tmp_path):
    """A portfolio snapshot that predates a committed fill is not recoverable."""

    from axiom_forex import build_fixture_platform

    database_path = tmp_path / "audit-stale-book.db"
    first = build_fixture_platform(database_path=str(database_path))
    try:
        result = first.run_paper_workflow(workflow_id="audit-stale-book")
        assert result.executed is True
        # Push every non-settlement portfolio snapshot into the past, exactly as
        # a crash that wrote the fill but not the book would have left it.
        first.repository.connection.execute(
            "UPDATE snapshots SET recorded_at = '2000-01-01T00:00:00+00:00' "
            "WHERE kind = 'portfolio' AND snapshot_id NOT LIKE 'portfolio-settlement:%'"
        )
        first.repository.connection.execute(
            "UPDATE snapshots SET payload = json_set(payload, "
            "'$.recorded_at', '2000-01-01T00:00:00+00:00') "
            "WHERE kind = 'portfolio' AND snapshot_id NOT LIKE 'portfolio-settlement:%'"
        )
        first.repository.connection.commit()
    finally:
        first.close()

    with pytest.raises(ValueError, match="recovered_portfolio_predates_settlement"):
        build_fixture_platform(database_path=str(database_path))


def test_operator_surfaces_do_not_disclose_the_local_database_path(tmp_path):
    import json as _json

    from app.audit.log import sanitize_audit_value
    from axiom_forex import build_fixture_platform

    database_path = tmp_path / "audit-private" / "paper-state.db"
    database_path.parent.mkdir(parents=True, exist_ok=True)
    platform = build_fixture_platform(database_path=str(database_path))
    try:
        platform.run_paper_workflow(workflow_id="audit-redaction")
        token = platform.runtime_settings.api_bearer_token
        app = platform.create_wsgi_app()

        def get(path: str) -> str:
            captured: dict[str, str] = {}

            def start_response(status, headers):  # noqa: ANN001
                captured["status"] = status

            body = app(
                {
                    "REQUEST_METHOD": "GET",
                    "PATH_INFO": path,
                    "HTTP_AUTHORIZATION": "Bearer " + token,
                },
                start_response,
            )
            assert captured["status"].startswith("200")
            return b"".join(body).decode("utf-8")

        payloads = {
            "/status": get("/status"),
            "/health": get("/health"),
            "/desktop": get("/desktop"),
            "/evaluation": get("/evaluation"),
            "report": platform.report_markdown(),
            "status": _json.dumps(platform.status(), default=str),
            "desktop": _json.dumps(platform.desktop_viewmodel(), default=str),
            "audit": _json.dumps(platform.audit_events(limit=1000), default=str),
        }
        absolute = str(database_path.resolve())
        for name, body in payloads.items():
            assert absolute not in body, name
            assert str(database_path.parent) not in body, name
            assert token not in body, name
        # The operator still learns what the storage is.
        assert platform.status()["repository"]["database_path"] == (
            "<local-database-file>"
        )
        assert sanitize_audit_value(
            {"database_path": platform.status()["repository"]["database_path"]}
        ) == {"database_path": "<local-database-file>"}
        # In-memory storage stays explicit rather than being hidden.
        memory_platform = build_fixture_platform(database_path=":memory:")
        try:
            assert memory_platform.status()["repository"]["database_path"] == ":memory:"
        finally:
            memory_platform.close()
    finally:
        platform.close()


def test_paper_idempotency_key_cannot_absorb_a_different_order(platform):
    """A repeated request id must replay the *same* order, not a new one."""

    from app.common.types import TradingMode
    from app.risk.models import OrderRequest
    from fixtures.m1 import PROVIDER_ID, PROVIDER_SYMBOL, QUANTITY

    quote = platform.provider.latest_quote(PROVIDER_SYMBOL)
    at = quote.timestamp_utc() + timedelta(milliseconds=200)

    def request(**overrides) -> OrderRequest:
        values = {
            "request_id": "audit-idempotent",
            "provider_id": PROVIDER_ID,
            "provider_symbol": PROVIDER_SYMBOL,
            "side": OrderSide.BUY,
            "quantity": QUANTITY,
            "target_mode": TradingMode.PAPER,
            "evaluated_at": at,
            "quote": quote,
            "strategy_id": "audit-idempotency",
        }
        values.update(overrides)
        return OrderRequest(**values)

    first = platform.paper_execution.submit(request(), platform._gate_context(at=at))
    assert first.state in {"SIMULATED", "REJECTED"}
    before = len(platform.paper_execution.records())

    # Exact replay: same record, no second order.
    again = platform.paper_execution.submit(request(), platform._gate_context(at=at))
    assert again.request_id == first.request_id
    assert again.simulation_id == first.simulation_id
    assert again.state == first.state
    assert len(platform.paper_execution.records()) == before

    if first.state != "SIMULATED":
        pytest.skip("gate rejected the fixture request; nothing to compare")

    # Same key, different payload: fail closed instead of silently replaying
    # the first order as if the second one had been accepted.
    for override in ({"quantity": Decimal(QUANTITY) * 2}, {"side": OrderSide.SELL}):
        with pytest.raises(ValueError, match="request_id_reused"):
            platform.paper_execution.submit(
                request(**override), platform._gate_context(at=at)
            )
        with pytest.raises(ValueError, match="request_id_reused"):
            platform.paper_execution.execute(
                request(**override),
                platform._gate_context(at=at),
                fill_price=platform.bars[-1].ask.open,
                fill_at=at,
                action="OPEN",
            )
    assert len(platform.paper_execution.records()) == before
    assert platform.paper_execution.fills() == ()


def test_stress_suite_leaves_the_baseline_report_and_custody_untouched(series, config):
    import json as _json

    from axiom_forex.evaluation.stress import (
        StressScenario,
        run_stress_suite,
    )

    registry = WindowRegistry()
    baseline = run_research_evaluation(
        series=series,
        config=config,
        calendar=make_multi_pair_calendar(),
        test_registry=registry,
        now=NOW,
    )
    assert baseline.status == "PASS"
    before = _json.dumps(baseline.audit_view(), sort_keys=True, default=str)
    claimed_before = registry.claimed
    assert claimed_before

    scenario = StressScenario(
        scenario_id="audit-spread",
        kind="SPREAD_WIDENING",
        label="audit spread",
        description="hypothetical audit assumption",
        evidence_class="HYPOTHETICAL_ASSUMPTION",
        evidence_reference="",
        parameters={"spread_multiplier": "2"},
    )
    suite = run_stress_suite(
        series=series,
        config=config,
        calendar=make_multi_pair_calendar(),
        scenarios=(scenario,),
        baseline=baseline,
        now=NOW,
    )
    assert suite.status == "PASS"
    run = suite.scenarios[0]
    assert run.report.selection_allowed is False
    assert run.report.qualification.status == "SENSITIVITY_ONLY"
    assert run.report.qualification.qualified is False

    # The baseline report and the claimed out-of-sample windows are unchanged:
    # a sensitivity run cannot edit the evidence it was measured against.
    assert _json.dumps(baseline.audit_view(), sort_keys=True, default=str) == before
    assert registry.claimed == claimed_before


# -- the evaluation must validate the bars it evaluates ---------------------


def _evaluate(series, config):
    return run_research_evaluation(
        series=series,
        config=config,
        calendar=make_multi_pair_calendar(),
        now=NOW,
    )


def _mutated(series, mutate):
    changed = dict(series)
    changed[PAIR_A] = replace(series[PAIR_A], bars=tuple(mutate(list(series[PAIR_A].bars))))
    return changed


@pytest.mark.parametrize(
    ("label", "mutate", "expected"),
    [
        (
            "stale_last_quote",
            lambda bars: [
                replace(bar, last_quote_at=bar.timestamp - timedelta(minutes=30))
                for bar in bars
            ],
            "invalid_bar",
        ),
        (
            "incomplete_bar",
            lambda bars: [replace(bar, complete=False) for bar in bars],
            "invalid_bar",
        ),
        (
            "bar_pair_mismatch",
            lambda bars: [replace(bar, pair="AAA/CCC") for bar in bars],
            "invalid_bar",
        ),
        (
            "duplicate_bar",
            lambda bars: bars[:20] + [bars[19]] + bars[20:],
            "bars_out_of_order",
        ),
        (
            "out_of_order_bars",
            lambda bars: bars[:10] + [bars[50]] + bars[11:],
            "bars_out_of_order",
        ),
    ],
)
def test_evaluation_rejects_bars_its_own_import_contract_would_reject(
    label, mutate, expected, series, config
):
    """The gate must validate the bars it evaluates, not the import label.

    A ``PairSeries`` is a public input to ``run_research_evaluation``.  Its
    ``quality`` object is a label computed at *import* time, so trusting it lets
    a mutated or hand-built series smuggle stale quotes, incomplete bars, bars
    from another instrument, or duplicated/overlapping timestamps into the
    simulation.  Each of those either fabricates a fill or silently re-prices a
    bar; all must fail closed with an explicit reason instead.
    """

    report = _evaluate(_mutated(series, mutate), config)
    assert report.status == "BLOCKED", label
    assert report.phases == {}, label
    assert any(expected in reason for reason in report.eligibility.reasons), (
        label,
        report.eligibility.reasons,
    )


def test_evaluation_rejects_a_crossed_bar_instead_of_trading_through_it(series, config):
    """A negative spread is a data error, not a trading opportunity."""

    def cross(bars):
        bar = bars[30]
        crossed = replace(
            bar,
            bid=replace(
                bar.bid,
                open=Decimal("1.30000"),
                high=Decimal("1.30000"),
                low=Decimal("1.30000"),
                close=Decimal("1.30000"),
            ),
            ask=replace(
                bar.ask,
                open=Decimal("1.20000"),
                high=Decimal("1.20000"),
                low=Decimal("1.20000"),
                close=Decimal("1.20000"),
            ),
        )
        return bars[:30] + [crossed] + bars[31:]

    baseline = _evaluate(series, config)
    report = _evaluate(_mutated(series, cross), config)
    assert baseline.status == "PASS"
    assert report.status == "BLOCKED"
    assert any("invalid_bar" in reason for reason in report.eligibility.reasons)
    # Without the gate this crossed bar produced an *extra* trade (22 vs 21).
    assert report.phases == {}


def test_evaluation_rejects_relabelled_provenance(series, config):
    """The declared data class must match the import it claims to come from."""

    relabelled = {
        pair: replace(item, data_class=DATA_CLASS_USER_SUPPLIED_HISTORICAL)
        for pair, item in series.items()
    }
    report = _evaluate(relabelled, config)
    assert report.status == "BLOCKED"
    assert any(
        "data_class_provenance_mismatch" in reason
        for reason in report.eligibility.reasons
    ), report.eligibility.reasons
    # The genuine article (imported *as* historical) is still evaluable.
    imported = _imported_series(data_class=DATA_CLASS_USER_SUPPLIED_HISTORICAL)
    honest = _evaluate(imported, config)
    assert honest.status == "PASS"
    assert honest.eligibility.qualification_allowed is True


def test_dashboard_requires_the_same_authorization_as_the_status_view(platform):
    """The HTML dashboard renders the same portfolio figures as ``/status``.

    ``/status`` and ``/desktop`` require the bearer token, but ``/dashboard``
    used to be served without one while rendering equity, exposure, margin,
    positions, and the workflow id from that same status view.  Authorization
    must not depend on the response *format*.
    """

    from desktop.contract import contract_audit

    platform.run_paper_workflow(workflow_id="audit-auth")
    token = platform.runtime_settings.api_bearer_token
    app = platform.create_wsgi_app()

    def get(path: str, auth: str | None = None) -> tuple[str, str]:
        captured: dict[str, str] = {}

        def start_response(status, headers):  # noqa: ANN001
            captured["status"] = status

        environ = {"REQUEST_METHOD": "GET", "PATH_INFO": path}
        if auth is not None:
            environ["HTTP_AUTHORIZATION"] = auth
        body = app(environ, start_response)
        return captured["status"], b"".join(body).decode("utf-8")

    # Liveness stays public and carries no account values.
    health_status, health_body = get("/health")
    assert health_status.startswith("200")
    for leaked in ("equity", "margin_account", "gross_exposure", "AAA/BBB"):
        assert leaked not in health_body

    # Account-bearing surfaces are all token-gated, whatever the format.
    assert get("/dashboard")[0].startswith("401")
    assert get("/status")[0].startswith("401")
    assert get("/dashboard", "Bearer wrong-token")[0].startswith("401")
    dashboard_status, dashboard_html = get("/dashboard", "Bearer " + token)
    assert dashboard_status.startswith("200")
    assert "audit-auth" in dashboard_html

    contract = {item["path"]: item for item in contract_audit()}
    assert contract["/dashboard"]["auth_required"] is True
    assert {item["auth_required"] for item in contract.values()} == {
        item["path"] != "/health" for item in contract.values()
    }


# -- the backtest equity curve must be point in time ------------------------


class _OpenLate:
    """Hold until a fixed bar, then buy; never exit (history limit 3)."""

    history_limit = 3
    name = "audit-open-late"

    def __init__(self, buy_at: int, total: int) -> None:
        self.buy_at = buy_at
        self.total = total

    def generate_signal(self, bars):  # noqa: ANN001
        from axiom_forex.strategies.base import Signal

        if len(bars) == self.total:
            return Signal.EXIT
        if len(bars) - 1 == self.buy_at:
            return Signal.BUY
        return Signal.HOLD


def test_backtest_equity_curve_before_the_first_decision_is_flat(platform):
    """Bars that precede any decision cannot reflect later positions.

    The engine used to fill in the equity curve *after* the loop, valuing the
    final portfolio at old bar timestamps.  Those points invented unrealized P&L
    (and therefore drawdown) for bars during which no position existed, and
    ``run_oos`` reads exactly those points to compute train/validation P&L.
    """

    bars = platform.bars
    result = platform.new_engine().run(
        bars, _OpenLate(buy_at=10, total=len(bars)), run_id="audit-point-in-time"
    )
    assert result.usable
    initial = result.initial_equity_account
    first_decision_index = 3 - 1

    early = [point for point in result.equity_curve if int(point["index"]) < first_decision_index]
    assert early
    for point in early:
        assert point["equity_account"] == initial, point["index"]

    # The curve stays ordered and complete (one point per bar plus the opener).
    indexes = [int(point["index"]) for point in result.equity_curve]
    assert indexes == sorted(indexes)
    assert indexes == list(range(-1, len(bars)))

    # The reported drawdown must be reproducible from the curve itself.
    peak = initial
    worst = Decimal(0)
    for point in result.equity_curve:
        peak = max(peak, point["equity_account"])
        worst = max(worst, peak - point["equity_account"])
    assert result.max_drawdown_account == worst


def test_oos_split_pnl_ignores_periods_without_positions(platform):
    bars = platform.bars
    strategy = _OpenLate(buy_at=len(bars) - 6, total=len(bars))
    train = bars[:20]
    validation = bars[20:30]
    test = bars[30:]
    result = platform.run_oos(
        strategy, train_bars=train, validation_bars=validation, test_bars=test
    )
    curve = {int(point["index"]): point["equity_account"] for point in result.result.equity_curve}
    initial = result.result.initial_equity_account
    # Nothing traded during the training segment, so it must show exactly zero
    # P&L rather than the valuation of a position opened much later.
    assert result.train_net_pnl_account == Decimal(0)
    assert curve[len(train) - 1] == initial


# -- candidate selection must not rank on the locked test set ---------------


class _StubResult:
    def __init__(self, curve: list[dict[str, object]], initial: Decimal) -> None:
        self.equity_curve = curve
        self.initial_equity_account = initial
        self.usable = True


class _StubEngine:
    """Returns a designer equity curve: one entry per candidate run."""

    def __init__(self, curves: list[tuple[Decimal, Decimal, Decimal]]) -> None:
        self._curves = curves
        self._runs = 0

    def run(self, bars, strategy, **_kwargs):  # noqa: ANN001
        start, validation, test = self._curves[self._runs]
        self._runs += 1
        length = len(bars)
        # Mirrors TournamentRunner: 30 train, 10 validation, 60 locked test.
        train_end = 29
        validation_end = 39
        curve = [{"index": -1, "timestamp": None, "equity_account": start}]
        for index in range(length):
            if index <= train_end:
                equity = start
            elif index <= validation_end:
                equity = start + validation
            else:
                equity = start + validation + test
            curve.append({"index": index, "timestamp": None, "equity_account": equity})
        return _StubResult(curve, start)


def _approved(strategy_id: str):
    from axiom_forex.learning.lifecycle import LifecycleStage, StrategyLifecycle

    lifecycle = StrategyLifecycle(strategy_id)
    while lifecycle.stage != LifecycleStage.APPROVED:
        lifecycle.promote()
    lifecycle.active = True
    assert lifecycle.approve_active()
    return lifecycle


def test_tournament_selects_on_validation_never_on_the_locked_test_set():
    """Ranking candidates by their locked-test P&L is selection on the test set.

    The tournament reported ``oos_pnl_account`` for every candidate and then fed
    exactly those numbers to the router, so the winner was the one that happened
    to do best on the held-out test window.  A test set used to *choose* stops
    being out of sample: the numbers in the report can no longer be read as
    independent evidence.  Selection must use the validation segment and report
    the test segment as evidence only.
    """

    from axiom_forex.tournament.runner import Candidate, TournamentRunner

    candidates = (
        Candidate("candidate-a", 2, 3),
        Candidate("candidate-b", 2, 4),
    )
    lifecycles = {item.strategy_id: _approved(item.strategy_id) for item in candidates}
    # A: validation loses, test wins.  B: validation wins, test barely moves.
    curves = [
        (Decimal("1000"), Decimal("-5"), Decimal("50")),
        (Decimal("1000"), Decimal("10"), Decimal("1")),
    ]
    bars = tuple(object() for _ in range(100))
    engine = _StubEngine(curves)
    result = TournamentRunner(
        # one engine per candidate, each fed its own scripted curve
        engine_factory=lambda: engine,
        candidates=candidates,
    ).run(
        train_bars=bars[:30],
        validation_bars=bars[30:40],
        test_bars=bars[40:],
        lifecycles=lifecycles,
    )
    view = {item.strategy_id: item for item in result.candidates}
    assert view["candidate-a"].oos_pnl_account == Decimal("50")
    assert view["candidate-b"].validation_pnl_account == Decimal("10")

    # The candidate that won on the locked test must not be selected by it.
    assert result.selected == "candidate-b", result.selected
    assert result.audit_view()["selection_basis"] == "VALIDATION"
    assert result.test_used is True

    # Changing only the test segment cannot change the decision.
    flipped = [(Decimal("1000"), Decimal("-5"), Decimal("-50")), (Decimal("1000"), Decimal("10"), Decimal("1"))]
    flipped_engine = _StubEngine(flipped)
    again = TournamentRunner(
        engine_factory=lambda: flipped_engine,
        candidates=candidates,
    ).run(
        train_bars=bars[:30],
        validation_bars=bars[30:40],
        test_bars=bars[40:],
        lifecycles=lifecycles,
    )
    assert again.selected == result.selected


# -- daily risk counters must belong to one trading day ---------------------


def _multi_day_portfolio():
    """A ledger whose rate book spans weeks, so day boundaries can be probed."""

    from dataclasses import replace
    from decimal import Decimal

    from app.accounting.fx import ConversionRateBook
    from axiom_forex import build_fixture_platform
    from axiom_forex.data.fixtures import make_backtest_conversion_book
    from axiom_forex.portfolio.ledger import ForexPortfolio

    platform = build_fixture_platform()
    rates = make_backtest_conversion_book(
        platform.bars,
        spec=platform.spec,
        account_currency=platform.account.account_currency_code,
        quote_rate=Decimal("1.25"),
        base_rate=Decimal("1.10"),
        source=platform.conversion_rates.source,
    )
    rates = ConversionRateBook(
        source=rates.source,
        freshness=replace(rates.freshness, max_age_seconds=Decimal(str(30 * 86400))),
        rates=rates.rates,
    )
    template = platform.engine_factory().portfolio
    portfolio = ForexPortfolio(
        account=platform.account,
        spec=platform.spec,
        conversion_rates=rates,
        cost_model=replace(template.cost_model, conversion_rates=rates),
        calendar=platform.calendar,
    )
    return platform, portfolio


def test_daily_risk_counters_roll_over_at_the_trading_day_boundary():
    """``*_today`` counters that never reset turn daily limits into lifetime ones.

    Measured before the fix: the ledger booked a loss once and kept reporting it
    for every later timestamp, so ``max_daily_loss_account`` blocked all entries
    for the rest of the process; ``max_trades_per_day`` likewise became a
    lifetime cap.  The counters must belong to one trading day, and that day is
    the broker calendar's local day -- not an arbitrary midnight.
    """

    from datetime import datetime, timezone
    from decimal import Decimal

    from app.common.types import OrderSide

    platform, portfolio = _multi_day_portfolio()
    first, second = platform.bars[0], platform.bars[1]
    portfolio.open(
        position_id="day-one",
        side=OrderSide.BUY,
        quantity=Decimal("0.10"),
        price=first.ask.close,
        at=first.end_at,
    )
    portfolio.close("day-one", price=second.bid.close, at=second.end_at)

    assert portfolio.realized_pnl_today_account < 0  # costs on a round trip
    assert portfolio.trades_today == 1
    assert portfolio.daily_counter_day.isoformat() == "2026-03-10"
    # America/New_York in March is the broker-calendar basis, not UTC.
    assert portfolio.daily_counter_basis == "CALENDAR_LOCAL_DATE:America/New_York"

    # 02:00Z the next UTC date is still 22:00 on the calendar's trading day.
    same_session = datetime(2026, 3, 11, 2, 0, tzinfo=timezone.utc)
    context = portfolio.risk_context(same_session, second)
    assert context.realized_pnl_today_account == portfolio.realized_pnl_today_account
    assert context.trades_today == 1

    # A new calendar day starts with clean counters ...
    next_session = datetime(2026, 3, 11, 14, 0, tzinfo=timezone.utc)
    context = portfolio.risk_context(next_session, second)
    assert context.realized_pnl_today_account == Decimal("0")
    assert context.trades_today == 0
    # ... and reading them must not rewrite the stored day or the totals.
    assert portfolio.realized_pnl_today_account < 0
    assert portfolio.trades_today == 1
    assert portfolio.daily_counter_day.isoformat() == "2026-03-10"

    # The next fill rolls the stored counters onto the new trading day.
    portfolio.open(
        position_id="day-two",
        side=OrderSide.BUY,
        quantity=Decimal("0.10"),
        price=first.ask.close,
        at=next_session,
    )
    assert portfolio.realized_pnl_today_account == Decimal("0")
    assert portfolio.trades_today == 1
    assert portfolio.daily_counter_day.isoformat() == "2026-03-11"

    # Without a calendar the basis is labelled as plain UTC, never as a broker day.
    from axiom_forex.portfolio.ledger import trading_day

    day, basis = trading_day(next_session, None)
    assert day == next_session.date()
    assert basis == "UTC_CALENDAR_DATE"


def test_daily_counter_day_is_persisted_so_a_restart_keeps_the_same_day():
    from datetime import datetime, timezone
    from decimal import Decimal

    from app.common.types import OrderSide
    from axiom_forex.portfolio.ledger import ForexPortfolio

    platform, portfolio = _multi_day_portfolio()
    first, second = platform.bars[0], platform.bars[1]
    portfolio.open(
        position_id="day-one",
        side=OrderSide.BUY,
        quantity=Decimal("0.10"),
        price=first.ask.close,
        at=first.end_at,
    )
    portfolio.close("day-one", price=second.bid.close, at=second.end_at)
    snapshot = portfolio.snapshot(second.end_at, second)
    assert snapshot["daily_counter_day"].isoformat() == "2026-03-10"
    assert snapshot["daily_counter_basis"] == "CALENDAR_LOCAL_DATE:America/New_York"

    restored = ForexPortfolio(
        account=portfolio.account,
        spec=portfolio.spec,
        conversion_rates=portfolio.conversion_rates,
        cost_model=portfolio.cost_model,
        calendar=portfolio.calendar,
    )
    restored.restore_snapshot(snapshot)
    assert restored.daily_counter_day.isoformat() == "2026-03-10"

    same_session = datetime(2026, 3, 11, 2, 0, tzinfo=timezone.utc)
    context = restored.risk_context(same_session, second)
    assert context.realized_pnl_today_account == portfolio.realized_pnl_today_account
    assert context.trades_today == 1

    next_session = datetime(2026, 3, 11, 14, 0, tzinfo=timezone.utc)
    context = restored.risk_context(next_session, second)
    assert context.realized_pnl_today_account == Decimal("0")
    assert context.trades_today == 0


def test_short_entry_is_not_exempted_from_the_entry_limits():
    """Only a close may skip the entry limits; a short entry may not.

    The gate exempted every SELL from ``max_open_positions``,
    ``max_daily_loss_account`` and ``max_trades_per_day``.  Shorts are enabled by
    default in both execution configs, so that exemption let a short *entry*
    through after the daily loss limit was already hit.  The exemption now
    requires a close intent that names an open ledger position, which the
    planner sets only when it is actually closing one.
    """

    from app.common.types import OrderSide

    from axiom_forex import build_fixture_platform
    from axiom_forex.strategies.base import Signal

    platform = build_fixture_platform()
    bars = platform.bars
    decision_index = 5
    short = platform.planner.plan(
        signal=Signal.SELL,
        positions={},
        bars=bars,
        decision_index=decision_index,
        run_id="audit-short-entry",
        strategy_id="audit",
    )
    assert short.action == "OPEN"
    assert short.request.side_value == OrderSide.SELL.value
    assert short.request.close_intent is None
    assert short.request.reduces_exposure is False

    long_position = type("_Position", (), {"side": OrderSide.BUY, "quantity": 1})()
    close = platform.planner.plan(
        signal=Signal.EXIT,
        positions={"p": long_position},
        bars=bars,
        decision_index=decision_index,
        run_id="audit-close",
        strategy_id="audit",
    )
    assert close.action == "CLOSE"
    assert close.request.side_value == OrderSide.SELL.value
    assert close.request.reduces_exposure is True
    assert close.request.close_intent is not None
    assert close.request.close_intent.position_id == "p"


# -- idempotency guard must fail closed -------------------------------------


def _planned_buy(platform, *, run_id: str = "audit-idempotency"):
    from axiom_forex.strategies.base import Signal

    return platform.planner.plan(
        signal=Signal.BUY,
        positions={},
        bars=platform.bars,
        decision_index=5,
        run_id=run_id,
        strategy_id="audit",
    )


def test_reused_request_id_with_a_different_payload_is_refused():
    from dataclasses import replace

    from axiom_forex import build_fixture_platform

    platform = build_fixture_platform()
    plan = _planned_buy(platform)
    engine = platform.paper_execution
    record = engine.submit(plan.request, plan.context)
    # The same request is genuinely idempotent ...
    assert engine.submit(plan.request, plan.context) is record
    # ... but a different order under the same id must be refused, not dropped.
    bigger = replace(plan.request, quantity=plan.request.quantity_decimal() * 2)
    try:
        engine.submit(bigger, plan.context)
    except ValueError as exc:
        assert str(exc) == "request_id_reused_with_different_payload"
    else:  # pragma: no cover - the failure mode under audit
        raise AssertionError("a reused request id with a new payload was accepted")


def test_reused_request_id_with_an_unreadable_stored_payload_is_refused():
    """An unresolvable stored payload is a mismatch, not a duplicate.

    Recovering after a restart rebuilds each intent as a plain audit dict.  If
    the stored quantity or timestamp cannot be read, the guard used to answer
    "already submitted" and silently drop the new order.  It must instead refuse
    the id, because it cannot prove the two payloads are the same order.
    """

    import json

    from axiom_forex import build_fixture_platform

    first = build_fixture_platform()
    plan = _planned_buy(first, run_id="audit-unreadable")
    record = first.paper_execution.submit(plan.request, plan.context)
    stored = json.loads(json.dumps(record.audit_view(), default=str))
    del stored["receipt"]["intent"]["quantity"]

    recovered = build_fixture_platform()
    recovered.paper_execution.restore([stored], [])
    retry = _planned_buy(recovered, run_id="audit-unreadable")
    try:
        recovered.paper_execution.submit(retry.request, retry.context)
    except ValueError as exc:
        assert str(exc) == "request_id_reused_with_different_payload"
    else:  # pragma: no cover - the failure mode under audit
        raise AssertionError("an unreadable stored payload was treated as a duplicate")


def test_paper_submission_never_invents_a_creation_timestamp():
    from dataclasses import replace

    from axiom_forex import build_fixture_platform

    platform = build_fixture_platform()
    plan = _planned_buy(platform, run_id="audit-timestamp")
    unreadable = replace(plan.request, evaluated_at="not-a-timestamp")
    try:
        platform.paper_execution.submit(unreadable, plan.context)
    except ValueError as exc:
        assert "invalid" in str(exc)
    else:  # pragma: no cover - the failure mode under audit
        raise AssertionError("an invalid evaluated_at was replaced with wall-clock time")


# -- scenario keys must be real, unambiguous legs ---------------------------


def test_malformed_scenario_keys_are_refused_instead_of_crashing_or_ignored():
    """A shock key nobody can parse must be a refusal, not a stack trace.

    Measured before the fix: ``rate_shocks={"AAA/BBB/CCC": "1.1"}`` passed
    ``validate_scenarios`` and then died inside the runner with a bare
    ``ValueError: too many values to unpack``; ``correlation_overrides`` with the
    same malformed key passed validation and was silently ignored (an override
    for a pair that does not exist).  Two spellings of one leg were also
    accepted, so which multiplier applied depended on dict ordering.
    """

    from axiom_forex.evaluation import (
        StressScenarioError,
        apply_scenario,
        validate_scenarios,
    )
    from axiom_forex.evaluation.stress import KIND_CONVERSION, KIND_CORRELATION
    from fixtures.multi_pair import make_multi_pair_evaluation_config

    from tests.test_forex_stress_scenarios import scenario
    from tests.test_forex_research_evaluation import _imported_series

    malformed_leg = scenario(
        "malformed-leg", KIND_CONVERSION, {"rate_shocks": {"AAA/BBB/CCC": "1.1"}}
    )
    assert validate_scenarios((malformed_leg,)) == (
        "malformed-leg:invalid_rate_leg:AAA/BBB/CCC",
    )
    with pytest.raises(StressScenarioError):
        apply_scenario(
            malformed_leg,
            series=_imported_series(),
            config=make_multi_pair_evaluation_config(),
        )

    duplicated_leg = scenario(
        "duplicate-leg",
        KIND_CONVERSION,
        {"rate_shocks": {"BBB/DDD": "1.1", "bbb/ddd": "0.9"}},
    )
    assert validate_scenarios((duplicated_leg,)) == (
        "duplicate-leg:duplicate_rate_leg:BBB/DDD",
    )

    malformed_pair = scenario(
        "malformed-pair", KIND_CORRELATION, {"correlation_overrides": {"AAA/BBB/CCC": "0.9"}}
    )
    assert validate_scenarios((malformed_pair,)) == (
        "malformed-pair:invalid_correlation_pair:AAA/BBB/CCC",
    )
    with pytest.raises(StressScenarioError):
        apply_scenario(
            malformed_pair,
            series=_imported_series(),
            config=make_multi_pair_evaluation_config(),
        )


def test_a_close_is_not_blocked_by_the_entry_size_caps():
    """An oversized position must still be closable.

    The gate projected every order as *adding* gross exposure, then applied the
    entry caps to that projection.  Measured before the fix: closing a position
    of 5 lots (notional 990, limit 100) was rejected with
    ``MAX_MARGIN_EXCEEDED``, and a large close could likewise trip
    ``MAX_ORDER_NOTIONAL_EXCEEDED``/``MAX_GROSS_EXPOSURE_EXCEEDED``.  A close is
    now projected from the ledger position it names and must strictly reduce
    exposure; the absolute entry caps apply only to entries.
    """

    from decimal import Decimal

    from app.common.types import OrderSide
    from app.risk.pretrade import RejectionReason
    from fixtures.m1 import (
        make_close_intent,
        make_environment,
        make_position_snapshot,
        make_risk_context,
    )
    from tests.test_pretrade_gate import evaluate

    env = make_environment()
    big = make_position_snapshot(quantity=Decimal("5"), base_units=Decimal("5000"))
    _, close = evaluate(
        env,
        request_overrides={
            "side": OrderSide.SELL,
            "quantity": Decimal("5"),
            "close_intent": make_close_intent(quantity=Decimal("5")),
        },
        context_overrides={
            "open_positions": (big,),
            "risk_context": make_risk_context(
                open_positions=1, gross_exposure_account=Decimal("9000")
            ),
        },
    )
    assert close.allowed, (close.code, close.details)
    assert close.details["exposure_reducing"] is True
    assert close.details["close_position_id"] == "fixture-position"
    assert close.details["margin_released_account"] > 0

    # The same size without a close intent is an entry and is still capped.
    _, entry = evaluate(
        env,
        request_overrides={"side": OrderSide.SELL, "quantity": Decimal("5")},
        env_overrides={
            "risk_context": make_risk_context(
                open_positions=1, gross_exposure_account=Decimal("900")
            )
        },
    )
    assert not entry.allowed
    assert entry.code in {
        RejectionReason.MAX_MARGIN_EXCEEDED,
        RejectionReason.MAX_ORDER_NOTIONAL_EXCEEDED,
        RejectionReason.MAX_GROSS_EXPOSURE_EXCEEDED,
    }

    # Naming a position that is not open is refused, never treated as a close.
    _, missing = evaluate(
        env,
        request_overrides={
            "side": OrderSide.SELL,
            "close_intent": make_close_intent(),
        },
        context_overrides={"open_positions": ()},
    )
    assert not missing.allowed
    assert missing.code is RejectionReason.CLOSE_POSITION_NOT_FOUND


# -- close orders are position-aware, never side-inferred -------------------


def _open_probe_position(platform, *, decision_index: int = 5, run_id: str = "close-audit"):
    from axiom_forex.strategies.base import Signal

    plan = platform.planner.plan(
        signal=Signal.BUY,
        positions={},
        bars=platform.bars,
        decision_index=decision_index,
        run_id=run_id,
        strategy_id="close-audit",
    )
    record, fill = platform.paper_execution.execute(
        plan.request,
        plan.context,
        fill_price=plan.fill_price,
        fill_at=plan.fill_at,
        action="OPEN",
        position_id=None,
    )
    assert record.state == "FILLED"
    return fill.position_id


def _close_plan(platform, position_id, *, decision_index: int = 8, run_id: str = "close-audit"):
    from axiom_forex.strategies.base import Signal

    return platform.planner.plan(
        signal=Signal.EXIT,
        positions=platform.portfolio.positions,
        bars=platform.bars,
        decision_index=decision_index,
        run_id=run_id,
        strategy_id="close-audit",
    )


def test_a_close_is_never_inferred_from_the_order_side():
    """An order is a close only when it names an open position.

    The previous exemption trusted a caller-supplied ``reduces_exposure`` flag,
    so any SELL could claim to be risk-reducing without the gate checking what
    (if anything) it was reducing.
    """

    from dataclasses import replace

    from app.common.types import OrderSide

    from axiom_forex import build_fixture_platform

    platform = build_fixture_platform()
    position_id = _open_probe_position(platform)
    plan = _close_plan(platform, position_id)

    # The planner's close names the position it read from the ledger ...
    assert plan.request.close_intent is not None
    assert plan.request.close_intent.position_id == position_id
    assert plan.request.close_intent.quantity == platform.portfolio.positions[position_id].quantity

    # ... while the same side with no intent is an entry, not a close.
    unflagged = replace(plan.request, request_id="unflagged", close_intent=None)
    assert unflagged.reduces_exposure is False
    decision = platform.gate.evaluate(unflagged, platform._gate_context(at=plan.fill_at))
    assert not decision.allowed or decision.order_intent.close_position_id is None
    assert position_id in platform.portfolio.positions
    assert decision.code.value != "ALLOWED" or decision.details["exposure_reducing"] is False


def test_forged_close_against_an_unrelated_open_position_is_refused():
    """Naming some other position must never be accepted as a close.

    Each forgery is checked against the ledger: a position id that is not open, a
    position owned by a different account, and a request that moves in the same
    direction as the position it claims to close (which would add exposure).
    """

    from dataclasses import replace

    from app.common.types import OrderSide

    from axiom_forex import build_fixture_platform

    platform = build_fixture_platform()
    position_id = _open_probe_position(platform)
    plan = _close_plan(platform, position_id)
    context = platform._gate_context(at=plan.fill_at)
    quantity_before = platform.portfolio.positions[position_id].quantity

    unrelated = replace(
        plan.request,
        request_id="forged-unrelated",
        close_intent=replace(plan.request.close_intent, position_id="position:not-ours"),
    )
    record = platform.paper_execution.submit(unrelated, context)
    assert record.state == "REJECTED"
    assert record.reason == "close_position_not_open"

    foreign = replace(
        plan.request,
        request_id="forged-foreign",
        close_intent=replace(plan.request.close_intent, account_id="ANOTHER_ACCOUNT"),
    )
    record = platform.paper_execution.submit(foreign, context)
    assert record.state == "REJECTED"
    assert record.reason == "CLOSE_ACCOUNT_MISMATCH"

    same_direction = replace(plan.request, request_id="forged-buy", side=OrderSide.BUY)
    record = platform.paper_execution.submit(same_direction, context)
    assert record.state == "REJECTED"
    assert record.reason == "CLOSE_SIDE_MISMATCH"

    # Nothing was closed by any of the forgeries.
    assert platform.portfolio.positions[position_id].quantity == quantity_before
    assert platform.portfolio.closed_trades == []


def test_oversized_close_that_would_reverse_exposure_is_refused():
    """A close larger than the position would flip it, so it must be refused."""

    from dataclasses import replace
    from decimal import Decimal

    from axiom_forex import build_fixture_platform

    platform = build_fixture_platform()
    position_id = _open_probe_position(platform)
    plan = _close_plan(platform, position_id)
    remaining = platform.portfolio.positions[position_id].quantity
    oversized = remaining * 2

    request = replace(
        plan.request,
        request_id="oversized",
        quantity=oversized,
        close_intent=replace(plan.request.close_intent, quantity=oversized),
    )
    record = platform.paper_execution.submit(
        request, platform._gate_context(at=plan.fill_at)
    )
    assert record.state == "REJECTED"
    assert record.reason == "close_quantity_exceeds_position"
    assert platform.portfolio.positions[position_id].quantity == remaining
    assert platform.portfolio.closed_trades == []

    # A close of exactly the remaining quantity is still allowed.
    exact = replace(plan.request, request_id="exact")
    settled, fill = platform.paper_execution.execute(
        exact,
        platform._gate_context(at=plan.fill_at),
        fill_price=plan.fill_price,
        fill_at=plan.fill_at,
        action="CLOSE",
        position_id=position_id,
    )
    assert settled.state == "FILLED"
    assert fill.quantity == remaining
    assert position_id not in platform.portfolio.positions


def test_duplicate_close_request_is_idempotent_and_does_not_double_close():
    from dataclasses import replace

    from axiom_forex import build_fixture_platform

    platform = build_fixture_platform()
    position_id = _open_probe_position(platform)
    plan = _close_plan(platform, position_id)
    context = platform._gate_context(at=plan.fill_at)
    first, first_fill = platform.paper_execution.execute(
        plan.request,
        context,
        fill_price=plan.fill_price,
        fill_at=plan.fill_at,
        action="CLOSE",
        position_id=position_id,
    )
    assert first.state == "FILLED"
    trades_after_first = len(platform.portfolio.closed_trades)
    fills_after_first = len(platform.paper_execution.fills())

    # The very same request again is the same order, not a second close.
    second, second_fill = platform.paper_execution.execute(
        plan.request,
        context,
        fill_price=plan.fill_price,
        fill_at=plan.fill_at,
        action="CLOSE",
        position_id=position_id,
    )
    assert second.simulation_id == first.simulation_id
    assert second_fill == first_fill
    assert len(platform.portfolio.closed_trades) == trades_after_first
    assert len(platform.paper_execution.fills()) == fills_after_first

    # Reusing the id for a *different* position is refused outright.
    other = replace(
        plan.request,
        close_intent=replace(plan.request.close_intent, position_id="position:other"),
    )
    try:
        platform.paper_execution.submit(other, context)
    except ValueError as exc:
        assert str(exc) == "request_id_reused_with_different_payload"
    else:  # pragma: no cover - the failure mode under audit
        raise AssertionError("a reused close id with another target was accepted")
    assert len(platform.portfolio.closed_trades) == trades_after_first


def test_a_stale_close_context_cannot_over_commit_the_position():
    """Two closes racing on one position can only spend it once.

    The gate validates against the context it is handed, and that context can be
    stale by the time the order arrives: here a close of the full 0.10 is
    prepared, half the position is closed by another order, and the stale
    request is then submitted.  The ledger-level reservation -- not the caller's
    snapshot -- must refuse it, and must refuse a second in-flight close too.
    """

    from dataclasses import replace
    from decimal import Decimal

    from axiom_forex import build_fixture_platform

    platform = build_fixture_platform()
    position_id = _open_probe_position(platform)
    plan = _close_plan(platform, position_id)
    stale_context = platform._gate_context(at=plan.fill_at)
    assert platform.portfolio.positions[position_id].quantity == Decimal("0.10")

    # Another close takes half of the position first.
    half = replace(
        plan.request,
        request_id="race-half",
        quantity=Decimal("0.05"),
        close_intent=replace(plan.request.close_intent, quantity=Decimal("0.05")),
    )
    settled, fill = platform.paper_execution.execute(
        half,
        platform._gate_context(at=plan.fill_at),
        fill_price=plan.fill_price,
        fill_at=plan.fill_at,
        action="CLOSE",
        position_id=position_id,
    )
    assert settled.state == "FILLED"
    assert fill.quantity == Decimal("0.05")

    # The stale full-size close is still accepted by the gate ...
    decision = platform.gate.evaluate(plan.request, stale_context)
    assert decision.allowed, decision.code
    # ... but the ledger refuses to reserve more than is left.
    record = platform.paper_execution.submit(plan.request, stale_context)
    assert record.state == "REJECTED"
    assert record.reason == "close_quantity_exceeds_position"
    assert platform.portfolio.positions[position_id].quantity == Decimal("0.05")
    assert len(platform.portfolio.closed_trades) == 1

    # A correctly sized close still works with a fresh context.
    remaining = platform.portfolio.positions[position_id].quantity
    rem = replace(
        plan.request,
        request_id="race-remainder",
        quantity=remaining,
        close_intent=replace(plan.request.close_intent, quantity=remaining),
    )
    settled, fill = platform.paper_execution.execute(
        rem,
        platform._gate_context(at=plan.fill_at),
        fill_price=plan.fill_price,
        fill_at=plan.fill_at,
        action="CLOSE",
        position_id=position_id,
    )
    assert settled.state == "FILLED"
    assert position_id not in platform.portfolio.positions
    assert len(platform.portfolio.closed_trades) == 2
    assert platform.paper_execution.pending_close_quantities() == {}


def test_an_in_flight_close_blocks_a_second_one_until_it_settles():
    from dataclasses import replace

    from axiom_forex import build_fixture_platform

    platform = build_fixture_platform()
    position_id = _open_probe_position(platform)
    plan = _close_plan(platform, position_id)
    context = platform._gate_context(at=plan.fill_at)

    first = replace(plan.request, request_id="inflight-first")
    second = replace(plan.request, request_id="inflight-second")
    first_record = platform.paper_execution.submit(first, context)
    assert first_record.state == "SIMULATED"
    # An unsettled close is an unfinished order: nothing else may be added on
    # top of it, so the second close is refused before it can reserve anything.
    second_record = platform.paper_execution.submit(second, context)
    assert second_record.state == "REJECTED"
    assert second_record.reason == "UNRESOLVED_ORDER_PRESENT"
    assert platform.paper_execution.pending_close_quantities() == {
        position_id: plan.request.quantity
    }

    platform.paper_execution.settle(
        first_record.simulation_id,
        fill_price=plan.fill_price,
        fill_at=plan.fill_at,
        action="CLOSE",
        side=first.side_value,
        quantity=first.quantity_decimal(),
        position_id=position_id,
    )
    assert position_id not in platform.portfolio.positions
    assert len(platform.portfolio.closed_trades) == 1
    assert platform.paper_execution.pending_close_quantities() == {}


def test_partial_closes_add_up_to_one_full_close():
    """Repeated partial closes must equal a single close in every total."""

    from decimal import Decimal

    from app.common.types import OrderSide

    from axiom_forex import build_fixture_platform

    platform = build_fixture_platform()
    bars = platform.bars

    def fresh_book():
        return platform.engine_factory().portfolio

    def open_position(book):
        return book.open(
            position_id="p",
            side=OrderSide.BUY,
            quantity=Decimal("0.10"),
            price=bars[0].ask.close,
            at=bars[0].end_at,
        )

    full_book = fresh_book()
    full_position = open_position(full_book)
    full_trade = full_book.close(
        full_position.position_id, price=bars[4].bid.close, at=bars[4].end_at
    )

    partial_book = fresh_book()
    partial_position = open_position(partial_book)
    entry_costs_total = partial_position.costs.total_account
    first = partial_book.close(
        "p", price=bars[4].bid.close, at=bars[4].end_at, quantity=Decimal("0.04")
    )
    assert first.partial is True
    remaining = partial_book.positions["p"]
    assert remaining.quantity == Decimal("0.06")
    assert remaining.original_quantity == Decimal("0.10")
    second = partial_book.close(
        "p", price=bars[5].bid.close, at=bars[5].end_at, quantity=Decimal("0.06")
    )
    assert second.partial is False
    assert "p" not in partial_book.positions

    # P&L is path dependent (different exit prices), but the accounting
    # identities must hold for every slice and in aggregate.
    for trade in (first, second):
        # ``realized_pnl_account`` is net of both sides' costs by construction.
        assert trade.total_costs_account > 0
        assert trade.closed_base_units > 0
    assert (
        first.entry_costs_account + second.entry_costs_account
        == entry_costs_total
    )
    assert partial_book.cost_total_account == (
        entry_costs_total + first.costs.total_account + second.costs.total_account
    )
    assert partial_book.realized_pnl_today_account == (
        first.realized_pnl_account + second.realized_pnl_account
    )
    assert full_trade.quantity == first.quantity + second.quantity
    assert len(partial_book.closed_trades) == 2
    assert len(full_book.closed_trades) == 1

    # Closing more than the remainder is refused and mutates nothing.
    third_book = fresh_book()
    open_position(third_book)
    third_book.close("p", price=bars[4].bid.close, at=bars[4].end_at, quantity=Decimal("0.04"))
    before = third_book.snapshot(bars[5].end_at, bars[5])
    try:
        third_book.close("p", price=bars[5].bid.close, at=bars[5].end_at, quantity=Decimal("0.10"))
    except ValueError as exc:
        assert str(exc) == "close_quantity_exceeds_position"
    else:  # pragma: no cover - the failure mode under audit
        raise AssertionError("an oversized partial close was accepted")
    after = third_book.snapshot(bars[5].end_at, bars[5])
    assert before["cash_by_currency"] == after["cash_by_currency"]
    assert third_book.positions["p"].quantity == Decimal("0.06")


def test_recovery_after_an_unresolved_partial_close_keeps_the_remainder_and_blocks_retries(
    tmp_path,
):
    """A failed close must never be retried behind the operator's back.

    The partial close is real and persisted; the second close fails during
    settlement and becomes UNRESOLVED.  After a restart the remaining quantity,
    the reservation, and the unresolved order must all survive, and a retry must
    be refused rather than closing the remainder twice.
    """

    from dataclasses import replace
    from decimal import Decimal

    from axiom_forex import build_fixture_platform

    database_path = tmp_path / "close-recovery.db"
    first = build_fixture_platform(database_path=str(database_path))
    try:
        bars = first.bars
        position_id = _open_probe_position(first)
        plan = _close_plan(first, position_id)
        half = replace(
            plan.request,
            request_id="recovery-half",
            quantity=Decimal("0.05"),
            close_intent=replace(plan.request.close_intent, quantity=Decimal("0.05")),
        )
        settled, fill = first.paper_execution.execute(
            half,
            first._gate_context(at=plan.fill_at),
            fill_price=plan.fill_price,
            fill_at=plan.fill_at,
            action="CLOSE",
            position_id=position_id,
        )
        assert settled.state == "FILLED"
        assert fill.quantity == Decimal("0.05")
        assert first.portfolio.positions[position_id].quantity == Decimal("0.05")
        assert first.portfolio.positions[position_id].original_quantity == Decimal("0.10")

        # A close whose settlement fails leaves the book untouched but the
        # order unresolved.
        retry_plan = _close_plan(first, position_id, decision_index=9, run_id="recovery-second")
        unresolved, no_fill = first.paper_execution.execute(
            replace(retry_plan.request, request_id="recovery-unresolved"),
            first._gate_context(at=retry_plan.fill_at),
            fill_price=retry_plan.fill_price,
            fill_at=retry_plan.fill_at,
            action="CLOSE",
            position_id="some-other-position",
        )
        assert unresolved.state == "UNRESOLVED"
        assert no_fill is None
        assert first.portfolio.positions[position_id].quantity == Decimal("0.05")
        expected_pending = first.paper_execution.pending_close_quantities()
        assert expected_pending == {position_id: Decimal("0.05")}
        expected_trades = len(first.portfolio.closed_trades)
        expected_fills = len(first.paper_execution.fills())

        first.repository.save_orders(first.paper_execution.order_records())
        first.repository.save_fills(first.paper_execution.fills())
        first.repository.save_snapshot(
            "recovery-snapshot",
            "portfolio",
            first.portfolio.snapshot(retry_plan.fill_at, bars[11]),
        )
    finally:
        first.close()

    second = build_fixture_platform(database_path=str(database_path))
    try:
        assert second.recovery_result is not None
        recovered = second.portfolio.positions[position_id]
        assert recovered.quantity == Decimal("0.05")
        assert recovered.original_quantity == Decimal("0.10")
        assert recovered.account_id == second.account.account_id
        assert len(second.paper_execution.unresolved_orders()) == 1
        assert second.paper_execution.pending_close_quantities() == expected_pending
        assert len(second.portfolio.closed_trades) == expected_trades
        assert len(second.paper_execution.fills()) == expected_fills

        # A retry is refused because the unfinished close blocks everything.
        retry = _close_plan(second, position_id, decision_index=10, run_id="recovery-retry")
        record = second.paper_execution.submit(
            retry.request, second._gate_context(at=retry.fill_at)
        )
        assert record.state == "REJECTED"
        assert record.reason == "UNRESOLVED_ORDER_PRESENT"
        assert second.portfolio.positions[position_id].quantity == Decimal("0.05")
        assert len(second.portfolio.closed_trades) == expected_trades
    finally:
        second.close()


def test_a_cancelled_close_releases_its_reservation_and_is_idempotent():
    """Cancellation must free the reserved quantity, and never un-fill an order.

    An unreserved-but-cancelled close would either block the position forever or,
    worse, stay reserved invisibly.  The reverse mistake -- "cancelling" a filled
    order -- would desynchronise the ledger from its own history.
    """

    from dataclasses import replace

    from axiom_forex import build_fixture_platform

    platform = build_fixture_platform()
    position_id = _open_probe_position(platform)
    plan = _close_plan(platform, position_id)
    context = platform._gate_context(at=plan.fill_at)

    pending = replace(plan.request, request_id="cancel-me")
    record = platform.paper_execution.submit(pending, context)
    assert record.state == "SIMULATED"
    assert platform.paper_execution.pending_close_quantities() == {
        position_id: pending.quantity_decimal()
    }

    cancelled = platform.paper_execution.cancel(
        record.request_id, cancelled_at=plan.fill_at
    )
    assert cancelled.state == "CANCELLED"
    assert platform.paper_execution.pending_close_quantities() == {}
    # Cancelling again is the same answer, not an error and not a second event.
    assert (
        platform.paper_execution.cancel(record.request_id, cancelled_at=plan.fill_at)
        is cancelled
    )

    # The released quantity can now be closed by a different request.
    retry = replace(plan.request, request_id="after-cancel")
    settled, fill = platform.paper_execution.execute(
        retry,
        context,
        fill_price=plan.fill_price,
        fill_at=plan.fill_at,
        action="CLOSE",
        position_id=position_id,
    )
    assert settled.state == "FILLED"
    assert fill.quantity == pending.quantity_decimal()
    assert len(platform.portfolio.closed_trades) == 1

    # A filled order can no longer be "cancelled".
    try:
        platform.paper_execution.cancel(retry.request_id, cancelled_at=plan.fill_at)
    except ValueError as exc:
        assert str(exc) == "cannot_cancel_filled_order"
    else:  # pragma: no cover - the failure mode under audit
        raise AssertionError("a filled order was cancelled")
    assert len(platform.portfolio.closed_trades) == 1


def test_a_rejected_close_id_still_refuses_a_different_payload():
    """Rejected orders carry no receipt, so the payload digest decides identity."""

    from dataclasses import replace

    from axiom_forex import build_fixture_platform

    platform = build_fixture_platform()
    position_id = _open_probe_position(platform)
    plan = _close_plan(platform, position_id)
    context = platform._gate_context(at=plan.fill_at)

    # Refused because the quantity exceeds the position.
    oversized = replace(plan.request, request_id="rejected-then-reused",
                        quantity=plan.request.quantity * 3,
                        close_intent=replace(plan.request.close_intent,
                                             quantity=plan.request.quantity * 3))
    rejected = platform.paper_execution.submit(oversized, context)
    assert rejected.state == "REJECTED"
    assert rejected.payload_fingerprint

    # The same payload is the same rejected answer ...
    assert platform.paper_execution.submit(oversized, context) is rejected

    # ... but a different order reusing that id is refused, not answered as the
    # old rejection (which would silently drop it).
    different = replace(
        oversized,
        close_intent=replace(oversized.close_intent, position_id="position:another"),
    )
    try:
        platform.paper_execution.submit(different, context)
    except ValueError as exc:
        assert str(exc) == "request_id_reused_with_different_payload"
    else:  # pragma: no cover - the failure mode under audit
        raise AssertionError("a different order reused a rejected request id")


def test_every_partial_close_slice_is_persisted_separately(tmp_path):
    """Partial closes share a position id; persistence must keep every slice."""

    from decimal import Decimal

    from app.common.types import OrderSide
    from axiom_forex import build_fixture_platform

    database_path = tmp_path / "partial-slices.db"
    platform = build_fixture_platform(database_path=str(database_path))
    try:
        bars = platform.bars
        portfolio = platform.portfolio
        portfolio.open(
            position_id="slice-position",
            side=OrderSide.BUY,
            quantity=Decimal("0.10"),
            price=bars[0].ask.close,
            at=bars[0].end_at,
        )
        portfolio.close(
            "slice-position",
            price=bars[2].bid.close,
            at=bars[2].end_at,
            quantity=Decimal("0.04"),
        )
        portfolio.close(
            "slice-position",
            price=bars[3].bid.close,
            at=bars[3].end_at,
            quantity=Decimal("0.06"),
        )
        snapshot = portfolio.snapshot(bars[4].end_at, bars[4])
        assert snapshot["open_positions"] == []
        assert len(snapshot["closed_trades"]) == 2

        platform.repository.save_positions(snapshot["closed_trades"], state="CLOSED")
        stored = platform.repository.list_positions()
        slices = [row for row in stored if row.get("position_id") == "slice-position"]
        assert len(slices) == 2, slices
        assert {row["state"] for row in slices} == {"CLOSED"}
        assert sum(Decimal(str(row["quantity"])) for row in slices) == Decimal("0.10")
    finally:
        platform.close()
