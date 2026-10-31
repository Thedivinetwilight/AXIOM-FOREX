"""The simulation's promises: causality, fill prices, stops, and honest costs."""

from __future__ import annotations

from axiom_forex.strategies.catalog.candidates import catalog
from axiom_forex.strategies.catalog.indicators import IndicatorCache
from axiom_forex.strategies.catalog.simulate import (
    END,
    SIGNAL_EXIT,
    STOP,
    TARGET,
    context_for,
    event_codes,
    signals_for,
    simulate_candidate,
)
from tests.catalog_support import SAMPLE_COST, series_from_closes, trending

from axiom_forex.research.engine import CostModel, segment_stats


def _candidate(entry: str, exit_name: str = "bracket", filter_name: str = "none"):
    for item in catalog():
        if item.entry == entry and item.exit == exit_name and item.filter == filter_name:
            return item
    raise AssertionError(f"{entry}/{exit_name}/{filter_name} is not in the catalog")


def _signals(candidate, series):
    return signals_for(candidate, context_for(series, IndicatorCache.of(series), candidate))


def test_trades_only_exist_inside_the_segment_and_end_forced():
    series = series_from_closes(trending(600))
    candidate = _candidate("sma_cross")
    signals = _signals(candidate, series)
    trades, unfilled = simulate_candidate(
        series, signals, candidate, cost=SAMPLE_COST, lot=0.1, segment="train", start=100, end=500
    )
    assert unfilled >= 0
    stamps = [stamp.isoformat() for stamp in series.timestamps]
    for trade in trades:
        assert trade.segment == "train"
        entry_index = stamps.index(trade.entry_at)
        exit_index = stamps.index(trade.exit_at)
        assert 100 <= entry_index < 500
        assert 100 <= exit_index < 500
        assert entry_index <= exit_index
    if trades and series.timestamps[499].isoformat() == trades[-1].exit_at:
        assert trades[-1].exit_reason == END


def test_a_decision_is_not_changed_by_bars_that_have_not_happened_yet():
    closes = trending(500)
    full = series_from_closes(closes)
    cut = 380
    shorter = series_from_closes(closes[:cut])
    candidate = _candidate("ema_cross")
    full_signals = _signals(candidate, full)
    short_signals = _signals(candidate, shorter)
    assert list(full_signals.longs[:cut]) == list(short_signals.longs)
    assert list(full_signals.shorts[:cut]) == list(short_signals.shorts)
    full_trades, _ = simulate_candidate(
        full, full_signals, candidate, cost=SAMPLE_COST, lot=0.1, segment="t", start=0, end=cut
    )
    short_trades, _ = simulate_candidate(
        shorter, short_signals, candidate, cost=SAMPLE_COST, lot=0.1, segment="t", start=0, end=cut
    )
    assert [trade.cell() for trade in full_trades] == [trade.cell() for trade in short_trades]


def test_a_buy_pays_the_next_open_plus_the_spread_and_slippage():
    # Down for 60 bars, then up: the fast average must cross back above the slow.
    closes = [round(1.1000 - 0.0004 * index, 5) for index in range(60)] + [
        round(1.0760 + 0.0004 * index, 5) for index in range(60)
    ]
    series = series_from_closes(closes, spread_points=6)
    candidate = _candidate("ema_cross")
    signals = _signals(candidate, series)
    codes = event_codes(candidate, signals)
    trades, _ = simulate_candidate(
        series, signals, candidate, cost=SAMPLE_COST, lot=0.1, segment="t", start=0, end=len(closes)
    )
    long_entries = [index for index in range(len(closes)) if codes[index] & 1]
    assert long_entries, "the fixture should produce at least one long entry"
    first = trades[0]
    assert first.direction == 1
    entry_index = [stamp.isoformat() for stamp in series.timestamps].index(first.entry_at)
    decision = long_entries[0]
    # Fill on the bar AFTER the decision bar, at its open, paying the ask plus slippage.
    assert entry_index == decision + 1
    expected = series.o[entry_index] + series.spread[entry_index] + SAMPLE_COST.slippage_price
    assert abs(first.entry_price - expected) < 1e-9
    assert first.cost_pips >= 0


def test_a_bar_that_touches_both_the_stop_and_the_target_is_scored_as_the_stop():
    # A hand-built series with one very wide bar, and hand-built signals, so the
    # precedence rule is tested on its own rather than through a rule's warm-up.
    from axiom_forex.strategies.catalog.simulate import Signals

    bars = 12
    closes = [1.1000] * bars
    series = series_from_closes(closes, wick=0.00002)
    # Bar 8 spans both brackets: low far below, high far above.
    series = _widen(series, 8, low=1.0900, high=1.1100)
    longs = [False] * bars
    longs[4] = True
    atr = [None] * bars
    atr[5] = 0.0020
    atr[6] = 0.0020
    signals = Signals(
        bars=bars,
        longs=longs,
        shorts=[False] * bars,
        exit_events=[False] * bars,
        allowed_long=[True] * bars,
        allowed_short=[True] * bars,
        atr=atr,
        events=1,
    )
    candidate = _candidate("sma_cross")
    trades, _ = simulate_candidate(
        series, signals, candidate, cost=SAMPLE_COST, lot=0.1, segment="t", start=0, end=bars
    )
    assert len(trades) == 1
    assert trades[0].exit_reason == STOP
    assert trades[0].direction == 1


def _widen(series, index: int, *, low: float, high: float):
    """The same series with one bar's range replaced (bid-side bars; mid follows)."""

    import dataclasses

    highs = list(series.h)
    lows = list(series.l)
    highs[index] = max(highs[index], high)
    lows[index] = min(lows[index], low)
    return dataclasses.replace(series, h=tuple(highs), l=tuple(lows))


def test_costs_are_subtracted_from_the_gross_pnl_and_never_hidden():
    series = series_from_closes(trending(700))
    candidate = _candidate("momentum_roc", exit_name="time_exit")
    signals = _signals(candidate, series)
    trades, _ = simulate_candidate(
        series, signals, candidate, cost=SAMPLE_COST, lot=0.1, segment="t", start=0, end=700
    )
    assert trades
    for trade in trades:
        assert trade.net_quote == trade.gross_quote - trade.spread_slippage_quote - (
            trade.commission_quote or 0.0
        ) - (trade.swap_quote or 0.0)
        assert trade.cost_pips >= 0

    free = CostModel(
        point=0.00001,
        contract_size=100_000.0,
        account_currency="USD",
        quote_currency="USD",
        slippage_pips=0.0,
        swap_state="DISABLED",
    )
    free_trades, _ = simulate_candidate(
        series, signals, candidate, cost=free, lot=0.1, segment="t", start=0, end=700
    )
    stats_paid = segment_stats("t", trades, 700)
    stats_free = segment_stats("t", free_trades, 700)
    assert stats_free.net_return > stats_paid.net_return


def test_an_unknown_cost_is_labelled_rather_than_zeroed():
    unknown = CostModel(point=0.00001, contract_size=100_000.0, slippage_state="ESTIMATE")
    assert unknown.incomplete()
    series = series_from_closes(trending(400))
    candidate = _candidate("sma_cross")
    signals = _signals(candidate, series)
    trades, _ = simulate_candidate(
        series, signals, candidate, cost=unknown, lot=0.1, segment="t", start=0, end=400
    )
    stats = segment_stats("t", trades, 400, unknown.incomplete())
    assert stats.costs_incomplete
    assert any("swap is unknown" in line for line in stats.costs_incomplete)


def test_segment_stats_matches_the_engine_conventions():
    series = series_from_closes(trending(400))
    candidate = _candidate("sma_cross")
    signals = _signals(candidate, series)
    trades, _ = simulate_candidate(
        series, signals, candidate, cost=SAMPLE_COST, lot=0.1, segment="t", start=0, end=400
    )
    stats = segment_stats("t", trades, 400)
    assert stats.trades == len(trades)
    assert stats.forced_exits == sum(1 for trade in trades if trade.exit_reason == END)
    assert stats.net_return == sum(trade.net_return for trade in trades)


def test_a_signal_exit_reason_is_used_when_the_rule_says_so():
    series = series_from_closes(trending(700))
    candidate = _candidate("sma_cross", exit_name="ma_return")
    signals = _signals(candidate, series)
    trades, _ = simulate_candidate(
        series, signals, candidate, cost=SAMPLE_COST, lot=0.1, segment="t", start=0, end=700
    )
    assert any(trade.exit_reason == SIGNAL_EXIT for trade in trades)
