"""The forecast benchmark: chronological, against dumb baselines, and honest.

Pinned here:

* only the out-of-sample window is scored, and nothing is fitted on it;
* steps are non-overlapping unless the caller asks otherwise, and the report says
  which it was;
* a refused input window is skipped and counted, never repaired;
* the table is ordered by model key, not by score -- the page must not imply a
  ranking it did not declare;
* every figure is a forecast-accuracy figure with the spread subtracted, and the
  module says in as many words that it is not a profit number.

Bars are synthetic fixtures; a fake forecaster stands in for Kronos.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.data.models import ForexQuoteBar, QuoteSideBar
from axiom_forex.kronos.benchmark import (
    BENCHMARK_NOTE,
    DriftModel,
    HoldModel,
    KronosModel,
    MomentumModel,
    compare_table,
    evaluate,
    standard_baselines,
)

UTC = timezone.utc
PIP = Decimal("0.0001")
START = datetime(2026, 3, 10, 8, 0, tzinfo=UTC)


def make_bar(mid: Decimal, timestamp: datetime) -> ForexQuoteBar:
    half = PIP / Decimal(2)
    bid = mid - half
    ask = mid + half
    return ForexQuoteBar(
        pair="EURUSD",
        timeframe="1m",
        timestamp=timestamp,
        duration_seconds=60,
        bid=QuoteSideBar(open=bid, high=bid + half, low=bid - half, close=bid),
        ask=QuoteSideBar(open=ask, high=ask + half, low=ask - half, close=ask),
        source="KRONOS_BENCHMARK_TEST_BARS",
        quote_count=7,
        last_quote_at=timestamp + timedelta(seconds=59),
        complete=True,
    )


def trending_bars(count: int = 300) -> list[ForexQuoteBar]:
    series = []
    mid = Decimal("1.1000")
    for index in range(count):
        series.append(make_bar(mid, START + timedelta(minutes=index)))
        mid += PIP / Decimal(3)
    return series


class Band:
    def __init__(self, low, mid, high):
        self.p10 = (low,)
        self.p50 = (mid,)
        self.p90 = (high,)


class FakeResult:
    def __init__(self, band):
        self.band = band


class FakeForecaster:
    """Predicts 'slightly up', which is right on this synthetic trend."""

    def __init__(self):
        self.calls = 0

    def forecast(self, data):
        self.calls += 1
        close = data.last_close
        return FakeResult(Band(close - PIP, close + PIP / Decimal(10), close + PIP))


def run_evaluate(**kwargs):
    params = dict(
        symbol="EURUSD",
        timeframe="1m",
        bar_seconds=60,
        lookback=40,
        horizon=5,
        models=[HoldModel(), DriftModel(), MomentumModel()],
        max_steps=40,
    )
    params.update(kwargs)
    return evaluate(trending_bars(kwargs.pop("bar_count", 300)), **params)


def test_only_the_out_of_sample_window_is_scored():
    series = trending_bars()
    result = evaluate(
        series,
        symbol="EURUSD",
        timeframe="1m",
        bar_seconds=60,
        lookback=40,
        horizon=5,
        models=[HoldModel()],
        max_steps=40,
    )
    test_start = series[int(len(series) * 0.8)].timestamp
    assert result.steps > 0
    assert datetime.fromisoformat(result.first_at) >= test_start
    assert any("test window" in note for note in result.notes)


def test_a_hold_forecast_is_compared_on_equal_terms_and_is_not_zero_error():
    result = run_evaluate()
    table = {row["key"]: row for row in result.table()}
    assert set(table) == {"hold", "drift", "momentum"}
    # The series moves, so "no move" cannot have zero error; that is the floor.
    assert Decimal(table["hold"]["mean_absolute_error"]) > 0
    for row in table.values():
        assert row["observations"] == result.steps
        assert "not a profit figure" in BENCHMARK_NOTE


def test_kronos_is_scored_alongside_the_baselines_it_must_beat():
    forecaster = FakeForecaster()
    result = run_evaluate(models=[HoldModel(), DriftModel(), MomentumModel(), KronosModel(forecaster)])
    table = {row["key"]: row for row in result.table()}
    assert table["kronos"]["observations"] == result.steps
    assert forecaster.calls == result.steps
    # A band was reported for every step, and its coverage is a count, not a claim.
    assert table["kronos"]["band_reported"] == result.steps
    assert table["kronos"]["band_coverage"] is not None


def test_steps_are_non_overlapping_by_default_and_the_report_says_so():
    result = run_evaluate()
    assert any("non-overlapping" in note for note in result.notes)
    overlapping = run_evaluate(stride=1)
    assert any("overlapping" in note for note in overlapping.notes)
    # Overlapping steps produce more observations from the same bars.
    assert overlapping.steps > result.steps


def test_a_refused_window_is_skipped_and_never_repaired():
    series = trending_bars()
    # A duplicate timestamp inside the test window is an input refusal, not data
    # to be fixed up silently.
    index = len(series) - 40
    series.insert(index, make_bar(Decimal("1.2000"), series[index].timestamp))
    result = evaluate(
        series,
        symbol="EURUSD",
        timeframe="1m",
        bar_seconds=60,
        lookback=40,
        horizon=5,
        models=[HoldModel()],
        max_steps=40,
    )
    assert any("refused" in note for note in result.notes)
    assert result.steps > 0


def test_the_table_is_ordered_by_name_so_it_does_not_imply_a_ranking():
    forecaster = FakeForecaster()
    result = run_evaluate(models=[KronosModel(forecaster), MomentumModel(), HoldModel()])
    keys = [row["key"] for row in result.table()]
    assert keys == sorted(keys)


def test_too_few_bars_is_a_refusal_not_a_thin_benchmark():
    import pytest

    with pytest.raises(ValueError):
        evaluate(
            trending_bars(50),
            symbol="EURUSD",
            timeframe="1m",
            bar_seconds=60,
            lookback=40,
            horizon=20,
            models=[HoldModel()],
        )


def test_the_comparison_table_flattens_every_pair_and_timeframe():
    series = trending_bars()
    rows = compare_table(
        [
            evaluate(
                series,
                symbol="EURUSD",
                timeframe="1m",
                bar_seconds=60,
                lookback=40,
                horizon=5,
                models=[HoldModel(), DriftModel()],
                max_steps=20,
            ),
            evaluate(
                series,
                symbol="EURUSD",
                timeframe="5m",
                bar_seconds=300,
                lookback=40,
                horizon=5,
                models=[HoldModel(), DriftModel()],
                max_steps=20,
            ),
        ]
    )
    assert {row["timeframe"] for row in rows} == {"1m", "5m"}
    assert all(row["symbol"] == "EURUSD" for row in rows)
    assert standard_baselines()[0].key == "hold"
