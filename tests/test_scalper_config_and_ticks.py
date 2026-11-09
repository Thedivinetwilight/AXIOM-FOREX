"""Configuration and AX-TICK CORE: identity, ordering, freshness and refusal.

Every test here is about the front door.  If the tick core lets a duplicate in, or
sorts an out-of-order tick into a decision, no amount of downstream care helps.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from axiom_forex.scalper.config import (
    ASSUMED_ZERO,
    CONFIG_NOT_PAPER,
    CONFIG_NO_INSTRUMENTS,
    CONFIG_UNKNOWN_COST,
    LIVE,
    UNKNOWN,
    CostTerms,
    ScalperConfigError,
    build_config,
    default_cost_terms,
    fixture_config,
    fixture_instrument,
)
from axiom_forex.scalper.ticks import (
    EVENT_CLOCK_RESET,
    EVENT_GAP,
    EVENT_RATE_DROP,
    EVENT_RESYNCED,
    EVENT_SPREAD_EXPANSION,
    REJECT_CROSSED,
    REJECT_DUPLICATE,
    REJECT_FUTURE,
    REJECT_INVALID_PRICE,
    REJECT_OUT_OF_ORDER,
    REJECT_STALE,
    REJECT_UNKNOWN_SYMBOL,
    HistoricalTickFeed,
    ReplayTick,
    TickCore,
    benchmark_ingest,
    read_tick_file,
    synthetic_ticks,
)

SYMBOL = "EURUSD.pro"
START = datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)


def core(**overrides):
    config = fixture_config()
    arguments = dict(
        instruments=config.instruments,
        max_quote_age_ms=config.max_quote_age_ms,
        max_spread_points=config.max_spread_points,
        gap_threshold_ms=config.gap_threshold_ms,
        min_window_ticks=config.min_window_ticks,
        tick_window=config.tick_window,
        clock_reset_threshold_ms=config.clock_reset_threshold_ms,
        resync_ticks=config.resync_ticks,
    )
    arguments.update(overrides)
    return TickCore(**arguments)


def at(milliseconds: int) -> datetime:
    return START + timedelta(milliseconds=milliseconds)


def feed(core_instance, ticks, *, spread_points: str = "8"):
    results = []
    for tick in ticks:
        results.append(
            core_instance.ingest(
                symbol=tick.symbol,
                bid=tick.bid,
                ask=tick.ask,
                source_time=tick.at,
                received_at=tick.at,
                volume=tick.volume,
            )
        )
    return results


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


def test_configuration_refuses_anything_but_paper():
    with pytest.raises(ScalperConfigError) as error:
        fixture_config(mode=LIVE)
    assert error.value.reason == CONFIG_NOT_PAPER


def test_configuration_needs_an_instrument():
    with pytest.raises(ScalperConfigError) as error:
        build_config(instruments=[])
    assert error.value.reason == CONFIG_NO_INSTRUMENTS


def test_unknown_commission_is_not_zero():
    cost = CostTerms(commission_per_lot=Decimal("7"), commission_state=UNKNOWN)
    assert cost.commission_known is False
    with pytest.raises(ScalperConfigError) as error:
        fixture_config(cost=cost)
    assert CONFIG_UNKNOWN_COST in str(error.value)


def test_assumed_zero_commission_is_declared_not_observed():
    cost = default_cost_terms()
    assert cost.commission_state == ASSUMED_ZERO
    assert cost.commission_known is True
    assert cost.audit_view()["commission_state"] == ASSUMED_ZERO


def test_instrument_without_conversion_is_incomplete():
    spec = fixture_instrument(
        pair="EUR/USD", quote_currency="EUR", account_currency="USD", conversion_rate=None
    )
    assert spec.point_value_per_lot is None
    assert spec.complete is False
    assert "no_conversion_between_quote_and_account_currency" in spec.validate()
    with pytest.raises(ScalperConfigError):
        build_config(instruments=[spec])


def test_instrument_conversion_makes_the_point_value_knowable():
    spec = fixture_instrument(quote_currency="EUR", account_currency="USD", conversion_rate="1.08")
    assert spec.point_value_per_lot == Decimal("100000") * Decimal("0.00001") * Decimal("1.08")
    assert spec.complete is True


def test_duplicate_symbols_are_refused():
    with pytest.raises(ScalperConfigError):
        build_config(instruments=[fixture_instrument(), fixture_instrument()])


def test_risk_bounds_are_bounded():
    with pytest.raises(ScalperConfigError):
        fixture_config(risk_fraction=Decimal("0.5"))
    with pytest.raises(ScalperConfigError):
        fixture_config(max_drawdown_fraction=Decimal("0.5"))


def test_config_audit_view_states_the_mode():
    view = fixture_config().audit_view()
    assert view["mode"] == "PAPER"
    assert view["symbols"] == [SYMBOL]


# ---------------------------------------------------------------------------
# ingestion identity and ordering
# ---------------------------------------------------------------------------


def test_identical_content_is_refused_as_a_duplicate():
    instance = core()
    first = instance.ingest(symbol=SYMBOL, bid="1.10000", ask="1.10008", source_time=at(0), received_at=at(0))
    second = instance.ingest(symbol=SYMBOL, bid="1.10000", ask="1.10008", source_time=at(0), received_at=at(0))
    assert first.accepted is True
    assert second.accepted is False
    assert second.reason == REJECT_DUPLICATE
    assert instance.quality(SYMBOL, now=at(0))["counters"][REJECT_DUPLICATE] == 1


def test_the_same_price_at_a_later_time_is_a_new_tick():
    instance = core()
    instance.ingest(symbol=SYMBOL, bid="1.10000", ask="1.10008", source_time=at(0), received_at=at(0))
    later = instance.ingest(
        symbol=SYMBOL, bid="1.10000", ask="1.10008", source_time=at(100), received_at=at(100)
    )
    assert later.accepted is True
    assert later.tick.tick_id != ""


def test_an_out_of_order_tick_is_refused_not_sorted():
    instance = core()
    instance.ingest(symbol=SYMBOL, bid="1.10000", ask="1.10008", source_time=at(100), received_at=at(100))
    late = instance.ingest(
        symbol=SYMBOL, bid="1.10010", ask="1.10018", source_time=at(50), received_at=at(100)
    )
    assert late.accepted is False
    assert late.reason == REJECT_OUT_OF_ORDER
    assert instance.last(SYMBOL).source_time == at(100)


def test_a_large_backward_jump_is_a_clock_reset_and_suspends_trading():
    instance = core(clock_reset_threshold_ms=1_000, resync_ticks=3)
    instance.ingest(symbol=SYMBOL, bid="1.10000", ask="1.10008", source_time=at(60_000), received_at=at(60_000))
    reset = instance.ingest(
        symbol=SYMBOL, bid="1.10010", ask="1.10018", source_time=at(0), received_at=at(60_000)
    )
    # The reset itself is what matters.  This particular tick is then refused as
    # stale relative to our own clock, which is correct: after a reset nothing about
    # the new chronology is trusted until the resync window has passed.
    assert any(event.kind == EVENT_CLOCK_RESET for event in reset.events)
    assert reset.reason == REJECT_STALE
    assert instance.resyncing(SYMBOL) is True
    assert "resynchronising" in instance.quality(SYMBOL, now=at(60_000))["reasons"]


def test_resynchronisation_ends_after_the_declared_number_of_clean_ticks():
    instance = core(clock_reset_threshold_ms=1_000, resync_ticks=2)
    instance.ingest(symbol=SYMBOL, bid="1.10000", ask="1.10008", source_time=at(60_000), received_at=at(60_000))
    instance.ingest(symbol=SYMBOL, bid="1.10010", ask="1.10018", source_time=at(0), received_at=at(60_000))
    events = []
    for index in range(2):
        result = instance.ingest(
            symbol=SYMBOL,
            bid=f"1.1002{index}",
            ask=f"1.1002{index + 8}",
            source_time=at(index + 1),
            received_at=at(index + 1),
        )
        assert result.accepted, result
        events.extend(result.events)
    assert instance.resyncing(SYMBOL) is False
    assert any(event.kind == EVENT_RESYNCED for event in events)


def test_a_tick_from_the_future_is_refused():
    instance = core()
    future = instance.ingest(
        symbol=SYMBOL, bid="1.10000", ask="1.10008", source_time=at(10_000), received_at=at(0)
    )
    assert future.accepted is False
    assert future.reason == REJECT_FUTURE


def test_a_stale_tick_is_refused_and_recorded():
    instance = core(max_quote_age_ms=500)
    stale = instance.ingest(
        symbol=SYMBOL, bid="1.10000", ask="1.10008", source_time=at(0), received_at=at(2_000)
    )
    assert stale.accepted is False
    assert stale.reason == REJECT_STALE
    assert any(event.kind == "quote_stale" for event in instance.events())


def test_crossed_and_non_positive_quotes_are_refused():
    instance = core()
    crossed = instance.ingest(
        symbol=SYMBOL, bid="1.10020", ask="1.10010", source_time=at(0), received_at=at(0)
    )
    invalid = instance.ingest(symbol=SYMBOL, bid="0", ask="1.10010", source_time=at(0), received_at=at(0))
    assert crossed.reason == REJECT_CROSSED
    assert invalid.reason == REJECT_INVALID_PRICE


def test_an_undeclared_symbol_is_refused_by_name():
    instance = core()
    result = instance.ingest(
        symbol="GBPUSD", bid="1.2", ask="1.2001", source_time=at(0), received_at=at(0)
    )
    assert result.accepted is False
    assert result.reason == REJECT_UNKNOWN_SYMBOL
    assert "GBPUSD" in result.detail


# ---------------------------------------------------------------------------
# quality monitoring
# ---------------------------------------------------------------------------


def test_a_gap_between_ticks_is_reported():
    instance = core(gap_threshold_ms=1_000)
    instance.ingest(symbol=SYMBOL, bid="1.10000", ask="1.10008", source_time=at(0), received_at=at(0))
    after_gap = instance.ingest(
        symbol=SYMBOL, bid="1.10010", ask="1.10018", source_time=at(5_000), received_at=at(5_000)
    )
    assert any(event.kind == EVENT_GAP for event in after_gap.events)


def test_spread_expansion_is_reported_and_changes_the_verdict():
    instance = core(max_spread_points=Decimal("30"), spread_expansion_multiple=Decimal("2.5"))
    for index in range(20):
        instance.ingest(
            symbol=SYMBOL,
            bid="1.10000",
            ask="1.10008",
            source_time=at(index * 100),
            received_at=at(index * 100),
        )
    wide = instance.ingest(
        symbol=SYMBOL, bid="1.10000", ask="1.10100", source_time=at(2_100), received_at=at(2_100)
    )
    assert any(event.kind == EVENT_SPREAD_EXPANSION for event in wide.events)
    quality = instance.quality(SYMBOL, now=at(2_100))
    assert "spread_too_wide" in quality["reasons"]
    assert quality["usable"] is False


def test_quality_is_not_usable_without_a_window():
    instance = core()
    for index in range(3):
        instance.ingest(
            symbol=SYMBOL, bid="1.10000", ask="1.10008", source_time=at(index * 100), received_at=at(index * 100)
        )
    quality = instance.quality(SYMBOL, now=at(200))
    assert quality["usable"] is False
    assert "insufficient_window" in quality["reasons"]


def test_latency_is_reported_on_a_broker_clock_basis_never_as_exchange_latency():
    instance = core(max_quote_age_ms=5_000)
    instance.ingest(
        symbol=SYMBOL, bid="1.10000", ask="1.10008", source_time=at(0), received_at=at(37)
    )
    latency = instance.latency()
    assert latency["samples"] == 1
    assert latency["basis"] == "broker_clock_basis"
    assert "Not exchange latency" in latency["note"]
    assert latency["mean_ms"] == pytest.approx(37.0)


def test_the_quality_note_denies_market_wide_meaning():
    instance = core()
    quality = instance.quality(SYMBOL, now=at(0))
    assert "one broker's quote stream" in quality["note"].lower()


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------


def test_replay_feeds_the_same_core_and_labels_its_source():
    ticks = synthetic_ticks(count=60, step_ms=100)
    instance = core(gap_threshold_ms=10_000)
    feed_object = HistoricalTickFeed(ticks, latency_ms=5)
    report = feed_object.feed_into(instance)
    assert report["accepted"] == 60
    assert report["refused"] == {}
    assert report["data_class"] == "HISTORICAL_TICK_REPLAY"
    assert report["received_at_basis"] == "recorded_tick_stamp_plus_declared_latency"
    assert instance.last(SYMBOL).source == "HISTORICAL_TICK_REPLAY"


def test_tick_file_reading_is_strict(tmp_path):
    path = tmp_path / "ticks.jsonl"
    path.write_text(
        '{"symbol": "EURUSD.pro", "bid": "1.1", "ask": "1.1001", "at": "2026-03-02T09:00:00+00:00"}\n',
        encoding="utf-8",
    )
    ticks = read_tick_file(path)
    assert len(ticks) == 1
    assert ticks[0].symbol == SYMBOL

    path.write_text('{"symbol": "EURUSD.pro"}\n', encoding="utf-8")
    with pytest.raises(ValueError):
        read_tick_file(path)


def test_benchmark_measures_this_machine_and_says_so():
    ticks = synthetic_ticks(count=400)
    report = benchmark_ingest(ticks, instruments=fixture_config().instruments, repeat=1)
    assert report["ticks"] == 400
    assert report["ticks_per_second_median"] > 0
    assert report["microseconds_per_tick_median"] > 0
    assert "this machine" in report["basis"]
    assert "says nothing about exchange latency" in report["note"]


def test_synthetic_ticks_are_labelled_sample_data():
    ticks = synthetic_ticks(count=3)
    instance = core()
    report = HistoricalTickFeed(ticks).feed_into(instance)
    assert report["data_class"] == "HISTORICAL_TICK_REPLAY"
    assert "not evidence of live latency" in report["note"]
    # The generator itself is a sample; only feeding it through a replay labels it as such.
    assert ticks[0].symbol == SYMBOL
    assert instance.last(SYMBOL).source != "MT5_POLL"
