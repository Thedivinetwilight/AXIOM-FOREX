from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from app.common.types import OrderSide
from app.data.models import FreshnessReason, QuoteSideBar
from app.data.providers import MarketDataProvider
from fixtures.m1 import (
    ASK,
    BID,
    EVALUATED_AT,
    PAIR,
    make_quote,
    make_quote_bar,
    make_quote_freshness,
    make_spec,
)


def test_quote_mid_executable_sides_and_spread():
    quote = make_quote()
    spec = make_spec()
    assert quote.validate(spec).valid
    assert quote.mid == (BID + ASK) / Decimal(2)
    assert quote.executable_price(OrderSide.BUY) == ASK
    assert quote.executable_price("SELL") == BID
    assert quote.spread == ASK - BID
    assert quote.spread_pips(spec) == Decimal("2.0")


@pytest.mark.parametrize(
    "overrides, expected_error",
    [
        ({"bid": None}, "missing_bid"),
        ({"ask": None}, "missing_ask"),
        ({"bid": ASK, "ask": BID}, "crossed_quote"),
        ({"bid": float("nan")}, "non_finite_bid"),
        ({"ask": Decimal("1.100201")}, "ask_precision_exceeded"),
        ({"source": ""}, "missing_quote_source"),
        ({"timestamp": datetime(2026, 3, 10, 10, 0)}, "naive_quote_timestamp"),
    ],
)
def test_quote_validation_rejects_bad_inputs(overrides, expected_error):
    quote = replace(make_quote(), **overrides)
    result = quote.validate(make_spec())
    assert not result.valid
    assert expected_error in result.errors


def test_quote_pair_mismatch_is_rejected():
    quote = replace(make_quote(), pair="XXX/YYY")
    result = quote.validate(make_spec())
    assert not result.valid
    assert "pair_mismatch" in result.errors


def test_quote_freshness_accepts_only_fresh_complete_timing():
    policy = make_quote_freshness()
    assert policy.check(make_quote(), EVALUATED_AT).reason == FreshnessReason.OK

    stale_ts = EVALUATED_AT - timedelta(seconds=10)
    stale = policy.check(make_quote(EVALUATED_AT, timestamp=stale_ts), EVALUATED_AT)
    assert stale.reason == FreshnessReason.STALE_QUOTE

    future_ts = EVALUATED_AT + timedelta(seconds=3)
    future = policy.check(make_quote(EVALUATED_AT, timestamp=future_ts), EVALUATED_AT)
    assert future.reason == FreshnessReason.FUTURE_QUOTE

    latency_ts = EVALUATED_AT - timedelta(seconds=2)
    latency_quote = make_quote(
        EVALUATED_AT,
        timestamp=latency_ts,
        received_at=latency_ts + timedelta(seconds=2),
    )
    latency = policy.check(latency_quote, EVALUATED_AT)
    assert latency.reason == FreshnessReason.FEED_LATENCY_EXCEEDED

    no_received = policy.check(
        make_quote(EVALUATED_AT, include_received_at=False), EVALUATED_AT
    )
    assert no_received.reason == FreshnessReason.MISSING_RECEIVED_AT

    bad_policy = make_quote_freshness(max_age_seconds=Decimal("0"))
    assert bad_policy.check(make_quote(), EVALUATED_AT).reason == FreshnessReason.INVALID_POLICY


def test_quote_freshness_check_instant_for_rates():
    policy = make_quote_freshness()
    assert policy.check_instant(EVALUATED_AT - timedelta(seconds=1), EVALUATED_AT).ok
    stale = policy.check_instant(EVALUATED_AT - timedelta(seconds=10), EVALUATED_AT)
    assert stale.reason == FreshnessReason.STALE_QUOTE
    future = policy.check_instant(EVALUATED_AT + timedelta(seconds=3), EVALUATED_AT)
    assert future.reason == FreshnessReason.FUTURE_QUOTE


def test_quote_bar_validation_and_executable_sides():
    bar = make_quote_bar()
    spec = make_spec()
    assert bar.validate().valid
    assert bar.validate(spec).valid
    assert bar.mid_close == (BID + ASK) / Decimal(2)
    assert bar.executable_price(OrderSide.BUY, point="close") == ASK
    assert bar.executable_price("SELL", point="open") == Decimal("1.09990")


def test_quote_bar_rejects_incomplete_or_impossible_data():
    spec = make_spec()
    assert "incomplete_bar" in replace(make_quote_bar(), complete=False).validate(spec).errors

    bad_bid = QuoteSideBar(
        open=Decimal("1.09990"),
        high=Decimal("1.09995"),
        low=Decimal("1.09980"),
        close=BID,
    )
    assert "high_below_open_or_close" in replace(make_quote_bar(), bid=bad_bid).validate(spec).errors

    crossed_bid = QuoteSideBar(
        open=Decimal("1.09990"),
        high=Decimal("1.10030"),
        low=Decimal("1.09980"),
        close=Decimal("1.10030"),
    )
    assert "crossed_close" in replace(make_quote_bar(), bid=crossed_bid).validate(spec).errors

    assert "missing_volume_kind" in replace(
        make_quote_bar(), volume=Decimal("10")
    ).validate(spec).errors
    assert "volume_kind_without_volume" in replace(
        make_quote_bar(), volume_kind="TICK_COUNT"
    ).validate(spec).errors

    bar = make_quote_bar()
    outside = replace(bar, last_quote_at=bar.timestamp + timedelta(seconds=60))
    assert "last_quote_outside_bar" in outside.validate(spec).errors


def test_market_data_provider_is_a_protocol_only():
    class FakeProvider:
        provider_id = "SYNTHETIC"
        supported_timeframes = frozenset({"1m"})

        def latest_quote(self, provider_symbol):
            return None

        def historical_bars(self, provider_symbol, timeframe, start, end):
            return ()

    assert isinstance(FakeProvider(), MarketDataProvider)
