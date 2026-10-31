"""The research series and the fast rule evaluation: equal, or refused.

The sweep can only be trusted if the fast path is *the same rule*.  Pinned here:

* the series keeps brokers bars as bid plus the bar's own quoted spread, and says
  so in its price basis;
* an unusable rate row is skipped and reported, never repaired into a zero bar;
* coverage refuses short, sparse, or stale history with a named reason;
* ``fastrules`` returns exactly the rule's own signal for every bar of every
  predeclared variant, including on tied and flat data where the comparison is
  ambiguous -- because those bars are re-decided by the real rule;
* the fallback rate on ordinary data stays small enough for the time budget.

Fixture prices are synthetic.  Nothing here is a market result.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from axiom_forex.research.fastrules import EXACT_FALLBACK_BUDGET, signals_for
from axiom_forex.research.series import (
    BID_PLUS_SPREAD,
    Series,
    audit_coverage,
    build_series,
    rows_from_rates,
)
from axiom_forex.strategies.base import Signal
from axiom_forex.strategies.library.variants import all_variants

NOW = datetime(2026, 10, 31, tzinfo=timezone.utc)
POINT = Decimal("0.00001")


def make_rows(count: int, *, step_seconds: int = 300, kind: str = "walk", seed: int = 7):
    """Synthetic rate rows, ending just before ``NOW``.  Not market data."""

    random.seed(seed)
    start = NOW - timedelta(seconds=step_seconds * count)
    price = 1.10000
    rows = []
    for index in range(count):
        if kind == "walk":
            close = round(price + random.gauss(0, 0.00022), 5)
        elif kind == "flat":
            close = 1.10000
        elif kind == "step":
            close = round(1.10000 + (0.00010 if index % 3 == 0 else 0.0), 5)
        else:  # pragma: no cover - guard
            raise AssertionError("unknown fixture kind")
        open_ = price
        high = round(max(open_, close) + abs(random.gauss(0, 0.00008)), 5)
        low = round(min(open_, close) - abs(random.gauss(0, 0.00008)), 5)
        rows.append(
            {
                "timestamp": start + timedelta(seconds=step_seconds * index),
                "open": f"{open_:.5f}",
                "high": f"{high:.5f}",
                "low": f"{low:.5f}",
                "close": f"{close:.5f}",
                "spread_points": 12,
                "tick_volume": 3,
                "source": "SAMPLE_DATA",
            }
        )
        price = close
    return rows


def make_series(count: int = 320, kind: str = "walk", seed: int = 7) -> Series:
    return build_series(
        "EURUSD.pro",
        "EUR/USD",
        "5m",
        300,
        POINT,
        rows_from_rates(make_rows(count, kind=kind, seed=seed)),
    )


# -- the series ------------------------------------------------------------


def test_series_names_its_price_basis_and_keeps_bid_plus_spread():
    series = make_series(10)
    assert series.price_basis == BID_PLUS_SPREAD
    assert len(series) == 10
    # The ask of a bar is its bid plus the spread that bar quoted.
    assert series.ask(3) == pytest.approx(series.bid(3) + series.spread[3])
    assert series.spread[3] == pytest.approx(12 * float(POINT))
    # The mid sits half a spread above the bid, and the rule sees the mid close.
    assert series.mid[3] == pytest.approx(series.bid(3, "c") + series.spread[3] / 2)


def test_unusable_rows_are_skipped_and_counted_not_repaired():
    rows = make_rows(6)
    broken = dict(rows[2])
    broken["close"] = "not a number"
    crossed = dict(rows[3])
    crossed["high"] = "1.0"  # below open/close: an impossible bar
    series = build_series(
        "EURUSD.pro", "EUR/USD", "5m", 300, POINT, rows_from_rates([*rows[:2], broken, crossed, *rows[4:]])
    )
    assert len(series) == 4
    assert series.skipped == 2
    assert any("unusable" in note for note in series.notes)


def test_series_without_a_point_size_is_marked_bid_only():
    series = build_series("EURUSD.pro", "EUR/USD", "5m", 300, None, rows_from_rates(make_rows(5)))
    assert series.point == 0.0
    assert all(value == 0.0 for value in series.spread)
    assert any("bid-only" in note for note in series.notes)


def test_duplicate_timestamps_are_dropped_and_noted():
    rows = make_rows(6)
    series = build_series(
        "EURUSD.pro", "EUR/USD", "5m", 300, POINT, rows_from_rates([*rows, rows[3]])
    )
    assert len(series) == 6
    assert any("duplicate" in note for note in series.notes)


def test_coverage_accepts_a_full_recent_year():
    series = build_series(
        "EURUSD.pro",
        "EUR/USD",
        "1h",
        3600,
        POINT,
        rows_from_rates(make_rows(2000, step_seconds=3600)),
    )
    coverage = audit_coverage(series, window_days=365, min_bars=500, now=NOW)
    assert coverage.usable
    assert coverage.coverage_fraction == pytest.approx(1.0)


def test_coverage_names_every_reason_it_refuses():
    empty = build_series("X", "EUR/USD", "1h", 3600, POINT, ())
    assert audit_coverage(empty, window_days=365, now=NOW).reason == "no_bars_for_this_window"

    few = build_series(
        "X", "EUR/USD", "1h", 3600, POINT, rows_from_rates(make_rows(100, step_seconds=3600))
    )
    assert audit_coverage(few, window_days=365, min_bars=500, now=NOW).reason.startswith(
        "too_few_bars"
    )

    stale = build_series(
        "X", "EUR/USD", "1h", 3600, POINT, rows_from_rates(make_rows(2000, step_seconds=3600))
    )
    later = NOW + timedelta(days=800)
    assert (
        audit_coverage(stale, window_days=365, min_bars=500, now=later).reason
        == "history_does_not_reach_the_recent_window"
    )

    gappy = build_series(
        "X",
        "EUR/USD",
        "1h",
        3600,
        POINT,
        rows_from_rates(make_rows(600, step_seconds=36000)),
    )
    reason = audit_coverage(gappy, window_days=365, min_bars=500, min_coverage=0.90, now=NOW)
    assert not reason.usable
    assert reason.reason.startswith("coverage_below")


def test_gaps_of_a_day_or_more_are_labelled_as_closures_not_missing_data():
    rows = make_rows(800, step_seconds=3600)
    # Remove a weekend-sized stretch in the middle.
    kept = rows[:400] + rows[448:]
    series = build_series("X", "EUR/USD", "1h", 3600, POINT, rows_from_rates(kept))
    coverage = audit_coverage(series, window_days=365, min_bars=500, now=NOW)
    assert coverage.weekend_gaps >= 1
    assert any("market closures" in note for note in coverage.notes)


# -- the fast rules --------------------------------------------------------


@pytest.mark.parametrize("seed,kind", [(7, "walk"), (5, "flat"), (3, "step")])
def test_fast_signals_equal_the_rules_on_every_bar(seed, kind):
    """Every variant, every bar: the fast path is the rule or it is refused."""

    series = make_series(220, kind=kind, seed=seed)
    mismatches = []
    for variant in all_variants():
        strategy = variant.strategy()
        limit = strategy.history_limit
        outcome = signals_for(series, variant)
        assert len(outcome.signals) == len(series)
        assert outcome.fallbacks <= outcome.bars
        for index in range(len(series)):
            exact = strategy.generate_signal(series.exact_window(index, limit))
            if exact != outcome.signals[index]:
                mismatches.append((variant.variant_id, index, exact, outcome.signals[index]))
    assert mismatches == [], mismatches[:5]


def test_tied_bars_are_redecided_by_the_real_rule_and_counted():
    """A flat series ties on every comparison, so the exact path must carry it."""

    series = make_series(120, kind="flat", seed=1)
    variant = next(item for item in all_variants() if item.card_id == "meanrev_bollinger")
    outcome = signals_for(series, variant)
    assert outcome.fallbacks > 0
    assert outcome.fallback_fraction > EXACT_FALLBACK_BUDGET


def test_fallback_rate_on_ordinary_data_stays_inside_the_budget():
    series = make_series(320, kind="walk", seed=11)
    total = 0
    bars = 0
    for variant in all_variants():
        outcome = signals_for(series, variant)
        total += outcome.fallbacks
        bars += outcome.bars
    assert total / bars < EXACT_FALLBACK_BUDGET


def test_a_signal_is_only_ever_a_declared_verb():
    series = make_series(120)
    for variant in all_variants()[:6]:
        for signal in signals_for(series, variant).signals:
            assert signal in {Signal.BUY, Signal.SELL, Signal.EXIT, Signal.HOLD}
