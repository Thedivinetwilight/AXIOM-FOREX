"""AX-SCALPER unit tests, part 1: AX-TICK CORE.

These are the behaviours a scalping decision depends on before any strategy is
consulted: what is accepted, what is refused, and what the desk is told about the
feed afterwards.  Every clock value is injected, so nothing here depends on wall
time.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from axiom_forex.scalper import (
    HistoricalTickFeed,
    ReplayTick,
    TickCore,
    benchmark_ingest,
    fixture_instrument,
    read_tick_file,
    synthetic_ticks,
)
from axiom_forex.scalper import ticks as ticks_module

UTC = timezone.utc
START = datetime(2026, 3, 2, 9, 0, 0, tzinfo=UTC)
SPEC = fixture_instrument()
POINT = SPEC.point


def make_core(**overrides):
    kwargs = dict(
        instruments=[SPEC],
        max_quote_age_ms=1500,
        max_spread_points=Decimal("30"),
        gap_threshold_ms=5000,
        spread_expansion_multiple=Decimal("2.5"),
        tick_window=240,
        min_window_ticks=10,
        clock_reset_threshold_ms=60_000,
        resync_ticks=3,
    )
    kwargs.update(overrides)
    return TickCore(**kwargs)


def ingest(
    core,
    *,
    at=START,
    latency_ms=50,
    bid="1.10000",
    ask="1.10008",
    symbol=None,
    volume=None,
    received_at=None,
):
    source_time = at
    return core.ingest(
        symbol=symbol or SPEC.symbol,
        bid=bid,
        ask=ask,
        source_time=source_time,
        received_at=received_at or source_time + timedelta(milliseconds=latency_ms),
        volume=volume,
    )


def seed(core, count=12, step_ms=250, spread_points=8):
    """Push ``count`` ordinary ticks and return the last result."""
    last = None
    mid = Decimal("1.10000")
    for index in range(count):
        move = Decimal(index % 3) / Decimal(100000)
        bid = (mid + move).quantize(Decimal("0.00001"))
        ask = bid + Decimal(spread_points) * POINT
        last = ingest(
            core,
            at=START + timedelta(milliseconds=step_ms * index),
            bid=str(bid),
            ask=str(ask),
        )
        assert last.accepted, last.reason
    return last


# -- acceptance and identity ------------------------------------------------


def test_accepted_tick_reports_mid_spread_and_sequence():
    core = make_core()
    result = ingest(core)
    assert result.accepted and result.tick is not None
    tick = result.tick
    assert tick.mid == Decimal("1.10004")
    assert tick.spread == Decimal("0.00008")
    assert tick.spread_points(POINT) == Decimal("8")
    assert tick.sequence == 1
    assert core.sequence() == 1


def test_tick_identity_is_content_not_arrival_order():
    first = make_core()
    second = make_core()
    one = ingest(first, at=START).tick
    two = ingest(second, at=START).tick
    assert one is not None and two is not None
    assert one.tick_id == two.tick_id
    assert one.sequence == two.sequence == 1


def test_unknown_symbol_is_refused_and_named():
    core = make_core()
    result = ingest(core, symbol="GBPUSD.raw")
    assert not result.accepted
    assert result.reason == ticks_module.REJECT_UNKNOWN_SYMBOL
    assert "GBPUSD.raw" in result.detail


@pytest.mark.parametrize("bid,ask", [("0", "1.10000"), ("1.10000", "0"), ("-1", "1.1"), ("x", "1.1")])
def test_unquotable_prices_are_refused(bid, ask):
    core = make_core()
    result = ingest(core, bid=bid, ask=ask)
    assert not result.accepted
    assert result.reason == ticks_module.REJECT_INVALID_PRICE


def test_crossed_quote_is_refused():
    core = make_core()
    result = ingest(core, bid="1.10010", ask="1.10000")
    assert not result.accepted
    assert result.reason == ticks_module.REJECT_CROSSED


def test_duplicate_content_is_accepted_once():
    core = make_core()
    first = ingest(core, at=START)
    # Same content, later arrival: still the same tick.
    again = ingest(core, received_at=START + timedelta(milliseconds=400))
    assert first.accepted and not again.accepted
    assert again.reason == ticks_module.REJECT_DUPLICATE
    assert len(core.window(SPEC.symbol)) == 1
    assert core.sequence() == 1
    assert core.quality(SPEC.symbol)["counters"]["duplicate_tick"] == 1


def test_out_of_order_tick_is_refused_not_reordered():
    core = make_core()
    assert ingest(core, at=START + timedelta(milliseconds=500)).accepted
    late = ingest(core, at=START + timedelta(milliseconds=400))
    assert not late.accepted
    assert late.reason == ticks_module.REJECT_OUT_OF_ORDER
    assert core.last(SPEC.symbol).source_time == START + timedelta(milliseconds=500)


# -- freshness --------------------------------------------------------------


def test_stale_quote_is_refused_and_recorded():
    core = make_core(max_quote_age_ms=1500)
    result = ingest(core, latency_ms=1501)
    assert not result.accepted
    assert result.reason == ticks_module.REJECT_STALE
    assert core.last(SPEC.symbol) is None
    events = core.events(symbol=SPEC.symbol)
    assert any(event.kind == ticks_module.EVENT_STALE for event in events)
    assert core.quality(SPEC.symbol)["counters"]["stale_tick"] == 1


def test_tick_stamped_ahead_of_our_clock_is_refused():
    core = make_core(max_quote_age_ms=1500)
    result = ingest(core, latency_ms=-5000)
    assert not result.accepted
    assert result.reason == ticks_module.REJECT_FUTURE


def test_quality_flags_a_quote_that_goes_stale_while_held():
    core = make_core(max_quote_age_ms=1500)
    seed(core, count=12)
    fresh = core.quality(SPEC.symbol, now=START + timedelta(milliseconds=1000))
    assert fresh["usable"] is True
    assert fresh["reasons"] == []
    stale_at = START + timedelta(milliseconds=250 * 11 + 5000)
    stale = core.quality(SPEC.symbol, now=stale_at)
    assert stale["usable"] is False
    assert "stale_quote" in stale["reasons"]


def test_latency_summary_is_measured_and_labelled_as_broker_clock_basis():
    core = make_core()
    seed(core, count=6)
    summary = core.latency()
    assert summary["samples"] == 6
    assert summary["basis"] == "broker_clock_basis"
    assert "not exchange latency" in summary["note"].lower()
    assert summary["max_ms"] == pytest.approx(50.0)


# -- gaps, spreads, resynchronisation ---------------------------------------


def test_gap_emits_an_event_with_the_measured_silence():
    core = make_core(gap_threshold_ms=5000)
    assert ingest(core, at=START).accepted
    result = ingest(core, at=START + timedelta(milliseconds=7000))
    assert result.accepted
    gaps = [event for event in result.events if event.kind == ticks_module.EVENT_GAP]
    assert len(gaps) == 1
    assert "7000 ms" in gaps[0].detail


def test_spread_expansion_is_detected_against_the_window_baseline():
    core = make_core()
    seed(core, count=14, spread_points=8)
    wide = ingest(
        core,
        at=START + timedelta(milliseconds=250 * 14),
        bid="1.10000",
        ask=str(Decimal("1.10000") + 40 * POINT),
    )
    assert wide.accepted
    assert core.quality(SPEC.symbol)["counters"]["spread_expansion"] >= 1
    assert any(
        event.kind == ticks_module.EVENT_SPREAD_EXPANSION for event in core.events(limit=5)
    )


def test_spread_above_the_hard_limit_makes_the_feed_unusable():
    core = make_core(max_spread_points=Decimal("30"))
    seed(core, count=12, spread_points=8)
    ingest(
        core,
        at=START + timedelta(milliseconds=250 * 12),
        bid="1.10000",
        ask=str(Decimal("1.10000") + 44 * POINT),
    )
    quality = core.quality(SPEC.symbol, now=START + timedelta(milliseconds=250 * 12 + 100))
    assert quality["usable"] is False
    assert "spread_too_wide" in quality["reasons"]


def test_clock_reset_clears_the_baseline_and_requires_clean_ticks():
    core = make_core(resync_ticks=3)
    seed(core, count=14)
    assert not core.resyncing(SPEC.symbol)
    reset = ingest(core, at=START - timedelta(seconds=120))
    assert reset.accepted
    assert core.resyncing(SPEC.symbol) is True
    assert core.window(SPEC.symbol) == (reset.tick,)
    assert any(event.kind == ticks_module.EVENT_CLOCK_RESET for event in core.events(limit=5))
    quality = core.quality(SPEC.symbol, now=reset.tick.received_at)
    assert quality["usable"] is False
    assert "resynchronising" in quality["reasons"]
    for index in range(3):
        tick = ingest(core, at=START - timedelta(seconds=120) + timedelta(milliseconds=250 * (index + 1)))
        assert tick.accepted
    assert core.resyncing(SPEC.symbol) is False
    assert any(event.kind == ticks_module.EVENT_RESYNCED for event in core.events(limit=8))


def test_small_backwards_step_is_not_a_clock_reset():
    core = make_core(clock_reset_threshold_ms=60_000)
    seed(core, count=5)
    late = ingest(
        core,
        at=START + timedelta(milliseconds=500),
        bid="1.09990",
        ask="1.09998",
    )
    assert not late.accepted
    assert late.reason == ticks_module.REJECT_OUT_OF_ORDER
    assert core.resyncing(SPEC.symbol) is False


# -- window and reporting ---------------------------------------------------


def test_window_is_bounded_and_marks_insufficient_history():
    core = make_core(tick_window=20, min_window_ticks=15)
    seed(core, count=12)
    short = core.quality(SPEC.symbol, now=START + timedelta(milliseconds=3000))
    assert "insufficient_window" in short["reasons"]
    seed_more = 0
    for index in range(12, 20):
        result = ingest(core, at=START + timedelta(milliseconds=250 * index))
        if result.accepted:
            seed_more += 1
    assert seed_more == 8
    assert len(core.window(SPEC.symbol)) == 20
    full = core.quality(SPEC.symbol, now=START + timedelta(milliseconds=250 * 19 + 100))
    assert "insufficient_window" not in full["reasons"]


def test_quality_never_presents_one_feed_as_the_whole_market():
    core = make_core()
    seed(core, count=12)
    quality = core.quality(SPEC.symbol, now=START + timedelta(milliseconds=3000))
    assert "not a market-wide order book" in quality["note"].lower()


def test_unknown_symbol_quality_fails_closed():
    core = make_core()
    quality = core.quality("GBPUSD.raw")
    assert quality["usable"] is False
    assert quality["reasons"] == ["unknown_symbol"]


def test_spread_points_is_none_before_the_first_tick():
    core = make_core()
    assert core.spread_points(SPEC.symbol) is None
    assert core.age_ms(SPEC.symbol) is None
    assert core.last(SPEC.symbol) is None


def test_describe_lists_declared_symbols_and_recent_events():
    core = make_core()
    assert ingest(core, bid="1.10000", ask="1.10008").accepted
    described = core.describe()
    assert described["symbols"] == [SPEC.symbol]
    assert described["sequence"] == 1
    assert described["latency"]["samples"] == 1


# -- replay files -----------------------------------------------------------


def write_ticks(path: Path, rows):
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return path


def test_read_tick_file_requires_a_real_quote_pair(tmp_path):
    path = write_ticks(
        tmp_path / "ticks.jsonl",
        [
            {"symbol": SPEC.symbol, "at": "2026-03-02T09:00:00+00:00", "bid": "1.10000", "ask": "1.10008"},
            {"symbol": SPEC.symbol, "at": "2026-03-02T09:00:00.250000+00:00", "bid": "1.10002", "ask": "1.10010"},
        ],
    )
    ticks = read_tick_file(path)
    assert [tick.bid for tick in ticks] == [Decimal("1.10000"), Decimal("1.10002")]
    assert ticks[0].spread == Decimal("0.00008")


@pytest.mark.parametrize(
    "line,needle",
    [
        ("{not json", "not JSON"),
        (json.dumps({"symbol": "X", "bid": "1", "ask": "2"}), "no timestamp"),
        (json.dumps({"symbol": "X", "at": "nonsense", "bid": "1", "ask": "2"}), "bad timestamp"),
        (json.dumps({"symbol": "X", "at": "2026-03-02T09:00:00+00:00", "bid": "2", "ask": "1"}), "quotable"),
    ],
)
def test_read_tick_file_is_strict_about_bad_lines(tmp_path, line, needle):
    path = tmp_path / "bad.jsonl"
    path.write_text(line + "\n", encoding="utf-8")
    with pytest.raises(ValueError) as excinfo:
        read_tick_file(path)
    assert needle in str(excinfo.value)


def test_read_tick_file_sorts_chronologically(tmp_path):
    path = write_ticks(
        tmp_path / "unordered.jsonl",
        [
            {"symbol": SPEC.symbol, "at": "2026-03-02T09:00:01+00:00", "bid": "1.2", "ask": "1.21"},
            {"symbol": SPEC.symbol, "at": "2026-03-02T09:00:00+00:00", "bid": "1.1", "ask": "1.11"},
        ],
    )
    ticks = read_tick_file(path)
    assert [tick.at.second for tick in ticks] == [0, 1]


def test_replay_uses_the_recorded_stamp_so_old_files_are_not_all_stale():
    replay = synthetic_ticks(count=20, step_ms=250)
    feed = HistoricalTickFeed(replay, latency_ms=120, sleep=lambda _: None)
    assert feed.received_at_basis == "recorded_tick_stamp_plus_declared_latency"
    core = make_core()
    summary = feed.feed_into(core)
    assert summary["accepted"] == 20
    assert summary["refused"] == {}
    assert summary["data_class"] == ticks_module.SRC_REPLAY
    assert "not evidence of live latency" in summary["note"]


def test_replay_with_declared_latency_beyond_the_limit_is_refused():
    replay = synthetic_ticks(count=6, step_ms=250)
    feed = HistoricalTickFeed(replay, latency_ms=9000, sleep=lambda _: None)
    core = make_core(max_quote_age_ms=1500)
    summary = feed.feed_into(core)
    assert summary["accepted"] == 0
    assert summary["refused"] == {ticks_module.REJECT_STALE: 6}


def test_replay_with_an_injected_clock_records_the_process_clock_basis():
    replay = synthetic_ticks(count=3, step_ms=250)
    feed = HistoricalTickFeed(replay, latency_ms=0, clock=lambda: START, sleep=lambda _: None)
    stamps = [received for _, received in feed.iter_ticks()]
    assert feed.received_at_basis == "process_clock"
    assert set(stamps) == {START}


# -- benchmark and synthetic data -------------------------------------------


def test_benchmark_reports_a_local_rate_and_not_an_exchange_claim():
    replay = synthetic_ticks(count=200, step_ms=250)
    result = benchmark_ingest(replay, instruments=[SPEC], repeat=1)
    assert result["ticks"] == 200
    assert result["repeats"] == 1
    assert result["ticks_per_second_median"] > 0
    assert result["microseconds_per_tick_median"] > 0
    assert "this machine" in result["basis"].lower()
    assert "nothing about exchange latency" in result["note"].lower()


def test_synthetic_ticks_are_deterministic_and_declared_as_sample_data():
    first = synthetic_ticks(count=40, drift_points=0.5)
    second = synthetic_ticks(count=40, drift_points=0.5)
    assert first == second
    assert first[0].symbol == SPEC.symbol
    assert first[-1].at > first[0].at
    # ReplayTick has no data-class field, so the class is declared by the producer.
    assert ticks_module.SRC_SAMPLE == "SAMPLE_DATA"


def test_synthetic_drift_moves_the_mid_in_the_declared_direction():
    up = synthetic_ticks(count=60, drift_points=1.0, noise_points=0.0)
    down = synthetic_ticks(count=60, drift_points=-1.0, noise_points=0.0)
    assert up[-1].mid > up[0].mid
    assert down[-1].mid < down[0].mid


def test_ingest_counts_each_refusal_under_its_own_name():
    core = make_core(max_quote_age_ms=1500)
    ingest(core, bid="0", ask="1")
    ingest(core, bid="1.1", ask="1.0")
    ingest(core, latency_ms=9999)
    counters = core.quality(SPEC.symbol)["counters"]
    assert counters["invalid_price"] == 1
    assert counters["crossed_quote"] == 1
    assert counters["stale_tick"] == 1
