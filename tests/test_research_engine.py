"""The one-year sweep: costs, windows, selection, budget, and what it refuses.

Pinned here:

* a decision fills at the **next** bar's open, on the side the trade pays, plus the
  stated slippage -- never at its own bar's price;
* spread comes from the bar's own quote, and commission/swap are included only
  when they can be priced -- otherwise they are named in ``costs_incomplete``;
* the split is chronological 60/20/20, selection is made on **validation**, and
  the test window is measured once for the selected variant alone;
* a budget or a cancel stops the run and lists exactly what did not run;
* unusable history produces a named reason and no trades, and every measured
  attempt is written to the ledger -- failures included;
* nothing in a report claims profit or live readiness.

Fixture prices are synthetic and are labelled as such.  Nothing here is a market
result, and no order can be placed from any of it.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from axiom_forex.research import engine as engine_module
from axiom_forex.research.engine import (
    ASSUMED_ZERO,
    CLAIM_NOTE,
    SEGMENTS,
    SPLIT,
    STATUS_COMPLETE,
    STATUS_TIMEOUT,
    UNKNOWN,
    CostModel,
    cost_model_for,
    record_attempts,
    run_sweep,
    segment_stats,
    simulate,
)
from axiom_forex.research.series import build_series, rows_from_rates
from axiom_forex.strategies.base import Signal
from axiom_forex.strategies.library.ledger import StrategyLedger
from axiom_forex.strategies.library.variants import all_variants, variants_for

NOW = datetime(2026, 10, 31, tzinfo=timezone.utc)
POINT = Decimal("0.00001")


def make_series(count: int = 2400, step_seconds: int = 900, seed: int = 5, drift: float = 0.0):
    random.seed(seed)
    start = NOW - timedelta(seconds=step_seconds * count)
    price = 1.10000
    rows = []
    for index in range(count):
        close = round(price + random.gauss(drift, 0.00012), 5)
        open_ = price
        rows.append(
            {
                "timestamp": start + timedelta(seconds=step_seconds * index),
                "open": f"{open_:.5f}",
                "high": f"{round(max(open_, close) + 0.00006, 5):.5f}",
                "low": f"{round(min(open_, close) - 0.00006, 5):.5f}",
                "close": f"{close:.5f}",
                "spread_points": 14,
                "tick_volume": 5,
                "source": "SAMPLE_DATA",
            }
        )
        price = close
    return build_series(
        "EURUSD.pro",
        "EUR/USD",
        "15m",
        step_seconds,
        POINT,
        rows_from_rates(rows),
    )


def make_cost(**overrides) -> CostModel:
    values = dict(
        point=float(POINT),
        contract_size=100000.0,
        account_currency="USD",
        quote_currency="USD",
        slippage_pips=0.2,
        commission_per_lot=0.0,
        swap_state="POINTS",
        swap_long=-1.0,
        swap_short=0.5,
        rollover_utc_hours=(21,),
    )
    values.update(overrides)
    return CostModel(**values)


def one_variant(card_id: str = "meanrev_bollinger"):
    return variants_for(next(card for card in _cards() if card.card_id == card_id))[0]


def _cards():
    from axiom_forex.strategies.library.cards import CARDS

    return CARDS


# -- fills -----------------------------------------------------------------


def test_a_decision_fills_at_the_next_bar_open_on_the_side_it_pays():
    series = build_series(
        "EURUSD.pro",
        "EUR/USD",
        "5m",
        300,
        POINT,
        rows_from_rates(
            [
                {"timestamp": NOW - timedelta(seconds=300 * (4 - index)),
                 "open": "1.1000", "high": "1.1020", "low": "1.0990",
                 "close": "1.1000", "spread_points": 10, "tick_volume": 1,
                 "source": "SAMPLE_DATA"}
                for index in range(4)
            ]
        ),
    )
    cost = make_cost(slippage_pips=0.0)
    signals = [Signal.HOLD, Signal.BUY, Signal.EXIT, Signal.HOLD]
    trades, unfilled = simulate(
        series,
        signals,
        variant_id="v",
        segment="train",
        cost=cost,
        lot=0.1,
        start=0,
        end=4,
    )
    assert unfilled == 0
    assert len(trades) == 1
    trade = trades[0]
    # Decision on bar 1, filled on bar 2.
    assert trade.entry_at == series.timestamps[2].isoformat()
    assert trade.entry_price == pytest.approx(series.ask(2, "o"))
    # EXIT on bar 2, filled on bar 3 at the bid.
    assert trade.exit_at == series.timestamps[3].isoformat()
    assert trade.exit_price == pytest.approx(series.bid(3, "o"))


def test_slippage_is_charged_against_the_trade_on_both_legs():
    series = make_series(30)
    cost = make_cost(slippage_pips=1.0)
    no_slip = make_cost(slippage_pips=0.0)
    signals = [Signal.HOLD] * 5 + [Signal.BUY] + [Signal.HOLD] * 4 + [Signal.EXIT] + [Signal.HOLD] * 19
    with_slip, _ = simulate(
        series, signals, variant_id="v", segment="train", cost=cost, lot=0.1, start=0, end=30
    )
    without, _ = simulate(
        series, signals, variant_id="v", segment="train", cost=no_slip, lot=0.1, start=0, end=30
    )
    assert len(with_slip) == len(without) == 1
    assert with_slip[0].net_pips < without[0].net_pips
    assert with_slip[0].cost_pips > 0


def test_commission_and_swap_enter_the_net_only_when_they_can_be_priced():
    series = make_series(60, seed=3)
    signals = [Signal.HOLD, Signal.BUY] + [Signal.HOLD] * 40 + [Signal.EXIT] + [Signal.HOLD] * 17
    cheap = make_cost(commission_per_lot=0.0)
    dear = make_cost(commission_per_lot=7.0)
    a, _ = simulate(series, signals, variant_id="v", segment="train", cost=cheap, lot=1.0, start=0, end=60)
    b, _ = simulate(series, signals, variant_id="v", segment="train", cost=dear, lot=1.0, start=0, end=60)
    assert len(a) == len(b) == 1
    assert b[0].commission_quote == pytest.approx(14.0)  # round turn
    assert b[0].net_quote < a[0].net_quote

    unpriceable = make_cost(
        account_currency="EUR", quote_currency="USD", commission_per_lot=7.0
    )
    assert unpriceable.conversion_known is False
    assert unpriceable.commission_quote(1.0) is None
    assert any("quote currency" in note for note in unpriceable.incomplete())


def test_swap_is_charged_per_night_and_unknown_swap_is_reported():
    series = make_series(120, step_seconds=3600, seed=9)
    signals = [Signal.HOLD, Signal.BUY] + [Signal.HOLD] * 70 + [Signal.EXIT] + [Signal.HOLD] * 47
    priced, _ = simulate(
        series,
        signals,
        variant_id="v",
        segment="train",
        cost=make_cost(swap_state="POINTS", swap_long=-2.0),
        lot=1.0,
        start=0,
        end=120,
    )
    assert len(priced) == 1
    assert priced[0].nights >= 2
    assert priced[0].swap_quote is not None and priced[0].swap_quote < 0

    unknown = make_cost(swap_state="UNKNOWN", swap_long=None, swap_short=None)
    assert unknown.swap_quote(1, 1.0, 3) is None
    assert any("swap is unknown" in note for note in unknown.incomplete())

    interest = make_cost(swap_state="INTEREST_MODE")
    assert interest.swap_quote(1, 1.0, 3) is None
    assert any("interest mode" in note for note in interest.incomplete())


def test_an_open_position_at_the_segment_end_is_closed_and_flagged():
    series = make_series(20)
    signals = [Signal.HOLD] * 10 + [Signal.BUY] + [Signal.HOLD] * 9
    trades, _ = simulate(
        series, signals, variant_id="v", segment="train", cost=make_cost(), lot=0.1, start=0, end=20
    )
    assert len(trades) == 1
    assert trades[0].exit_reason == engine_module.FORCED


def test_drawdown_and_return_are_computed_from_the_trade_sequence():
    series = make_series(40, seed=21)
    signals = [Signal.HOLD, Signal.BUY] + [Signal.HOLD] * 15 + [Signal.EXIT] + [Signal.HOLD] * 22
    trades, _ = simulate(
        series, signals, variant_id="v", segment="train", cost=make_cost(), lot=0.1, start=0, end=40
    )
    stats = segment_stats("train", trades, 40)
    assert stats.trades == 1
    assert stats.max_drawdown >= 0.0
    assert stats.net_return == pytest.approx(sum(trade.net_return for trade in trades))
    assert stats.long_trades + stats.short_trades == 1


# -- the cost model --------------------------------------------------------


def test_cost_model_from_observed_symbol_facts_keeps_unknowns_unknown():
    class Symbol:
        point = Decimal("0.00001")
        contract_size = Decimal("100000")
        profit_currency = "usd"
        swap_long = Decimal("-0.75")
        swap_short = Decimal("0.25")
        swap_state = "POINTS"

    model = cost_model_for(Symbol(), account_currency="usd")
    assert model.quote_currency == "USD"
    assert model.conversion_known is True
    assert model.labels()["spread"] == "BROKER_RATES_QUOTED_SPREAD"
    assert model.labels()["commission"] == ASSUMED_ZERO
    assert model.labels()["rollover"] == "UNKNOWN"
    assert any("rollover" in note for note in model.incomplete())

    class Hidden:
        point = Decimal("0.0001")
        contract_size = Decimal("100000")
        profit_currency = "JPY"
        swap_long = None
        swap_short = None
        swap_state = "INTEREST_MODE"

    hidden = cost_model_for(Hidden(), account_currency="USD", commission_per_lot=5)
    assert hidden.conversion_known is False
    assert hidden.swap_quote(1, 1.0, 2) is None
    assert any("swap" in note for note in hidden.incomplete())


def test_scaled_costs_multiply_only_the_assumptions():
    model = make_cost(commission_per_lot=4.0, slippage_pips=0.2)
    doubled = model.scaled(2.0)
    assert doubled.slippage_pips == pytest.approx(0.4)
    assert doubled.commission_per_lot == pytest.approx(8.0)
    assert doubled.point == model.point


# -- the sweep -------------------------------------------------------------


@pytest.fixture(scope="module")
def sweep_report():
    series = make_series(2400, seed=5)
    return run_sweep(
        {("EURUSD.pro", "15m"): series},
        costs={"EURUSD.pro": make_cost()},
        lot_size=0.1,
        now=NOW,
    )


def test_the_sweep_measures_every_predeclared_variant(sweep_report):
    assert sweep_report.status == STATUS_COMPLETE
    assert sweep_report.complete
    result = sweep_report.series[0]
    assert result.coverage.usable
    assert result.bars == 2400
    assert result.measured == len(all_variants())
    assert result.train_bars + result.validation_bars + result.test_bars == 2400
    assert SPLIT == (0.6, 0.2, 0.2)
    assert tuple(SEGMENTS) == ("train", "validation", "test")


def test_selection_uses_validation_and_the_test_window_is_measured_once(sweep_report):
    result = sweep_report.series[0]
    assert result.selected_variant_id
    selected = next(item for item in result.variants if item.variant_id == result.selected_variant_id)
    assert selected.test is not None
    others = [item for item in result.variants if item.variant_id != result.selected_variant_id]
    assert others and all(item.test is None for item in others)
    assert all(item.test_disclosure for item in others)
    assert "validation" in result.selection_reason
    assert "test window" in result.selection_reason
    # Walk-forward and sensitivity are measured for the selected variant only.
    assert result.walk_forward is not None
    assert result.walk_forward.folds
    assert result.sensitivity and result.sensitivity[0]["window"] == "validation"


def test_every_row_reports_trades_costs_and_drawdown_by_strategy_symbol_timeframe(sweep_report):
    rows = sweep_report.table()
    assert len(rows) == len(all_variants())
    row = rows[0]
    for key in ("strategy", "symbol", "timeframe", "family", "status"):
        assert key in row
    assert "validation_trades" in row
    assert "validation_cost_return" in row
    assert "validation_max_drawdown" in row


def test_the_report_never_claims_profit_or_live_readiness(sweep_report):
    assert sweep_report.claim == CLAIM_NOTE
    assert "Not a forecast" in sweep_report.claim
    payload = sweep_report.to_payload()
    text = repr(payload).lower()
    for forbidden in ("profit expected", "guaranteed", "risk-free", "will make money"):
        assert forbidden not in text


def test_unusable_history_is_named_and_nothing_is_invented():
    stale = make_series(600)
    report = run_sweep(
        {("EURUSD.pro", "15m"): stale},
        costs={"EURUSD.pro": make_cost()},
        now=NOW + timedelta(days=900),
    )
    result = report.series[0]
    assert result.coverage.usable is False
    assert result.measured == 0
    assert result.selected_variant_id == ""
    assert all(item.status == "NOT_RUN" for item in result.variants)
    assert "history not usable" in result.selection_reason


def test_a_series_without_a_cost_model_is_not_run_and_says_why():
    report = run_sweep({("EURUSD.pro", "15m"): make_series(600)}, costs={}, now=NOW)
    assert report.series == ()
    assert report.not_run and report.not_run[0]["reason"] == "no_cost_model_for_symbol"
    assert report.status == STATUS_TIMEOUT


def test_the_budget_stops_the_run_and_lists_what_did_not_run():
    series = make_series(2400, seed=5)
    ticks = iter(range(1, 100000))

    def clock() -> float:
        return float(next(ticks))

    report = run_sweep(
        {("EURUSD.pro", "15m"): series},
        costs={"EURUSD.pro": make_cost()},
        budget_seconds=20.0,
        clock=clock,
        now=NOW,
    )
    assert report.status == STATUS_TIMEOUT
    assert report.not_run
    assert {item["reason"] for item in report.not_run} & {"budget_exhausted", "cancelled"}
    assert report.summary()["not_run"] == len(report.not_run)


def test_cancel_stops_the_run_immediately():
    report = run_sweep(
        {("EURUSD.pro", "15m"): make_series(2400, seed=5)},
        costs={"EURUSD.pro": make_cost()},
        cancel=lambda: True,
        now=NOW,
    )
    assert report.status == "CANCELLED_PARTIAL"
    assert report.not_run and report.not_run[0]["reason"] == "cancelled"


def test_progress_is_reported_for_every_unit_and_ends_at_one():
    seen: list[tuple[float, str]] = []
    report = run_sweep(
        {("EURUSD.pro", "15m"): make_series(1200, seed=5)},
        costs={"EURUSD.pro": make_cost()},
        variants=all_variants()[:6],
        now=NOW,
        progress=lambda fraction, label: seen.append((fraction, label)),
    )
    assert report.status == STATUS_COMPLETE
    assert seen
    assert all(0.0 <= fraction <= 1.0 for fraction, _ in seen)
    assert seen[-1][0] == pytest.approx(1.0)
    fractions = [fraction for fraction, _ in seen]
    assert fractions == sorted(fractions)


def test_a_variant_the_template_does_not_support_is_unavailable_not_silent():
    # A 1-minute series: the momentum card is declared for 15m and slower.
    series = build_series(
        "EURUSD.pro",
        "EUR/USD",
        "1m",
        60,
        POINT,
        rows_from_rates(
            [
                {
                    "timestamp": NOW - timedelta(seconds=60 * (900 - index)),
                    "open": "1.10000",
                    "high": "1.10010",
                    "low": "1.09990",
                    "close": "1.10000",
                    "spread_points": 10,
                    "tick_volume": 1,
                    "source": "SAMPLE_DATA",
                }
                for index in range(900)
            ]
        ),
    )
    report = run_sweep(
        {("EURUSD.pro", "1m"): series},
        costs={"EURUSD.pro": make_cost()},
        variants=all_variants(),
        now=NOW,
    )
    result = report.series[0]
    unavailable = [item for item in result.variants if item.status == "UNAVAILABLE"]
    assert unavailable
    assert all("declared for" in item.reason for item in unavailable)


def test_every_measured_attempt_is_written_to_the_ledger(tmp_path, sweep_report):
    ledger = StrategyLedger(tmp_path / "ledger.sqlite")
    written = record_attempts(sweep_report, ledger, note="one_year_terminal_history")
    assert written
    attempts = ledger.attempts()
    assert len(attempts) == len(written)
    first = attempts[0]
    assert first.status in {"ATTEMPTED", "UNAVAILABLE", "INSUFFICIENT_EVIDENCE"}
    assert first.metrics["claim"] == CLAIM_NOTE
    assert "validation" in first.metrics
    assert first.dataset_fingerprint


def test_the_same_bars_produce_the_same_dataset_fingerprint():
    one = make_series(300, seed=2)
    two = make_series(300, seed=2)
    assert engine_module.series_identity(one) == engine_module.series_identity(two)
    other = make_series(300, seed=4)
    assert engine_module.series_identity(other)[1] != engine_module.series_identity(one)[1]


def test_a_non_finite_cost_term_is_named_and_makes_the_model_unusable():
    """``nan`` compares false against every bound, so it must never slip through.

    A symbol whose point size or contract size arrives as ``nan`` used to pass a
    plain ``<= 0`` guard and turn every figure in the run into ``nan``.  It is now
    reported as a defect, the model is marked unusable, and the callers refuse to
    price with it.
    """

    class Broken:
        point = Decimal("NaN")
        contract_size = Decimal("NaN")
        profit_currency = "USD"
        swap_long = Decimal("NaN")
        swap_short = None
        swap_state = "POINTS"

    model = cost_model_for(Broken(), account_currency="USD")

    assert model.unusable is True
    assert any("point size" in item for item in model.defects)
    assert any("contract size" in item for item in model.defects)
    # A non-finite swap is unknown financing, not zero financing.
    assert any("swap" in item for item in model.defects)
    assert model.swap_state == UNKNOWN
    assert model.swap_long is None
    # The unpriceable terms are reported, and the model carries no number that a
    # caller could mistake for a price.
    notes = model.incomplete()
    assert any("point size" in note and "usable number" in note for note in notes)
    assert model.point == 0.0 and model.contract_size == 0.0


def test_an_infinite_slippage_or_commission_is_a_defect_not_zero():
    class Symbol:
        point = Decimal("0.0001")
        contract_size = Decimal("100000")
        profit_currency = "USD"
        swap_long = None
        swap_short = None
        swap_state = UNKNOWN

    model = cost_model_for(
        Symbol(),
        account_currency="USD",
        slippage_pips=float("inf"),
        commission_per_lot=float("nan"),
    )

    assert model.unusable is True
    assert any("slippage" in item for item in model.defects)
    assert any("commission" in item for item in model.defects)


def test_a_sound_cost_model_is_not_marked_unusable():
    class Symbol:
        point = Decimal("0.00001")
        contract_size = Decimal("100000")
        profit_currency = "USD"
        swap_long = Decimal("-1.2")
        swap_short = Decimal("-0.8")
        swap_state = "POINTS"

    model = cost_model_for(Symbol(), account_currency="USD")

    assert model.unusable is False
    assert model.defects == ()
    assert model.pip_value(1) == pytest.approx(1.0)


def test_scaled_keeps_the_defects_it_was_built_with():
    class Broken:
        point = Decimal("NaN")
        contract_size = Decimal("100000")
        profit_currency = "USD"
        swap_long = None
        swap_short = None
        swap_state = UNKNOWN

    model = cost_model_for(Broken(), account_currency="USD")

    assert model.scaled(2.0).unusable is True
    assert model.scaled(2.0).defects == model.defects
