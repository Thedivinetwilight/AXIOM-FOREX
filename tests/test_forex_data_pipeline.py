from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from app.data.models import ForexQuote, ForexQuoteBar
from axiom_forex.data.normalization import DataNormalizer, parse_timestamp
from axiom_forex.data.quality import assess_bars
from axiom_forex.research.datasets import build_manifest, dataset_digest, verify_manifest
from fixtures.m1 import (
    EVALUATED_AT,
    PROVIDER_ID,
    PROVIDER_SYMBOL,
    local_utc,
    make_calendar,
    make_catalog,
    make_quote_bar,
    make_quote_freshness,
    make_spec,
)


def _side_payload(side):
    return {point: str(getattr(side, point)) for point in ("open", "high", "low", "close")}


def _quote_payload(**overrides):
    values = {
        "symbol": PROVIDER_SYMBOL,
        "timestamp": EVALUATED_AT.isoformat(),
        "received_at": (EVALUATED_AT + timedelta(milliseconds=100)).isoformat(),
        "bid": "1.10000",
        "ask": "1.10020",
        "source": "TEST_QUOTE_SOURCE",
        "quote_id": "q1",
        "metadata": {"fixture": "test"},
    }
    values.update(overrides)
    return values


def _bar_payload(**overrides):
    fixture_bar = make_quote_bar()
    values = {
        "symbol": PROVIDER_SYMBOL,
        "timestamp": (EVALUATED_AT - timedelta(minutes=1)).isoformat(),
        "last_quote_at": (EVALUATED_AT - timedelta(seconds=1)).isoformat(),
        "timeframe": "1m",
        "duration_seconds": 60,
        "quote_count": 12,
        "complete": True,
        "source": "TEST_BAR_SOURCE",
        "bid": _side_payload(fixture_bar.bid),
        "ask": _side_payload(fixture_bar.ask),
    }
    values.update(overrides)
    return values


def test_normalizer_accepts_valid_quote_and_bar():
    spec = make_spec()
    normalizer = DataNormalizer(make_catalog(spec), PROVIDER_ID)
    quote_result = normalizer.normalize_quote(_quote_payload())
    bar_result = normalizer.normalize_bar(_bar_payload())

    assert quote_result.ok
    assert isinstance(quote_result.value, ForexQuote)
    assert quote_result.value.canonical_pair == "AAA/BBB"
    assert quote_result.value.bid == Decimal("1.10000")
    assert bar_result.ok
    assert isinstance(bar_result.value, ForexQuoteBar)
    assert bar_result.value.quote_count == 12
    assert bar_result.value.validate(spec).valid


def test_normalizer_rejects_unknown_alias_and_missing_fields():
    normalizer = DataNormalizer(make_catalog(), PROVIDER_ID)
    unknown = normalizer.normalize_quote(_quote_payload(symbol="UNKNOWN"))
    missing_ask = normalizer.normalize_quote(_quote_payload(ask=None))
    missing_bid_bar = normalizer.normalize_bar(_bar_payload(bid=None))
    bad_timestamp = normalizer.normalize_quote(
        _quote_payload(timestamp="not-a-timestamp")
    )

    assert unknown.errors == ("unknown_provider_symbol",)
    assert "missing_ask" in missing_ask.errors
    assert "missing_bid_bar" in missing_bid_bar.errors
    assert any("timestamp" in error for error in bad_timestamp.errors)


def test_parse_timestamp_rejects_naive_values():
    value, error = parse_timestamp("2026-03-10T10:00:00", "sample")
    assert value is None
    assert error == "naive_sample"


def test_synthetic_provider_and_dataset_digest(platform):
    bars = platform.bars
    quote = platform.provider.latest_quote(PROVIDER_SYMBOL)
    history = platform.provider.historical_bars(
        PROVIDER_SYMBOL, "1m", bars[0].timestamp, bars[-1].end_at
    )
    assert quote is not None
    assert quote.bid is not None and quote.ask is not None
    assert history == bars
    assert platform.provider.historical_bars(
        "UNKNOWN", "1m", bars[0].timestamp, bars[-1].end_at
    ) == ()

    manifest = build_manifest(
        bars,
        dataset_id="test-dataset",
        spec_version=platform.spec.spec_version,
        calendar_signature=platform.calendar.signature(),
        source="TEST_SOURCE",
    )
    assert verify_manifest(bars, manifest)
    modified = (replace(bars[0], quote_count=11),) + bars[1:]
    assert dataset_digest(modified) != manifest.sha256
    assert not verify_manifest(modified, manifest)


def test_quality_accepts_contiguous_bars_during_dst_session(platform):
    report = assess_bars(
        platform.bars,
        spec=platform.spec,
        calendar=platform.calendar,
        freshness=make_quote_freshness(),
        evaluated_at=platform.bars[-1].end_at,
    )
    assert report.classification == "VALID"
    assert report.usable
    assert report.findings == ()


def test_quality_rejects_missing_bar_without_interpolation(platform):
    bars = platform.bars[:5] + platform.bars[6:]
    report = assess_bars(
        bars,
        spec=platform.spec,
        calendar=platform.calendar,
        freshness=make_quote_freshness(),
        evaluated_at=bars[-1].end_at,
    )
    assert report.classification == "MISSING"
    assert not report.usable
    assert any(finding.code == "MISSING_BAR" for finding in report.findings)


def test_quality_treats_weekend_gap_as_expected_not_missing():
    first = make_quote_bar(evaluated_at=local_utc(2026, 3, 7, 0, 0))
    second = make_quote_bar(evaluated_at=local_utc(2026, 3, 8, 17, 1))
    report = assess_bars(
        (first, second),
        spec=make_spec(),
        calendar=make_calendar(),
        freshness=make_quote_freshness(),
        evaluated_at=second.end_at,
    )
    assert report.classification == "WARNING"
    assert report.usable
    assert any(finding.code == "EXPECTED_SESSION_GAP" for finding in report.findings)
    assert all(finding.code != "MISSING_BAR" for finding in report.findings)


def test_quality_rejects_out_of_session_and_future_bars(platform):
    holiday_at = local_utc(2026, 3, 9, 10, 0)
    out_of_session = replace(
        platform.bars[0],
        timestamp=holiday_at,
        last_quote_at=holiday_at + timedelta(seconds=59),
    )
    report = assess_bars(
        (out_of_session,),
        spec=platform.spec,
        calendar=platform.calendar,
        freshness=make_quote_freshness(),
        evaluated_at=out_of_session.end_at,
    )
    assert not report.usable
    assert any(finding.code == "OUT_OF_SESSION_BAR" for finding in report.findings)

    future_report = assess_bars(
        platform.bars,
        spec=platform.spec,
        calendar=platform.calendar,
        freshness=make_quote_freshness(),
        evaluated_at=platform.bars[0].timestamp,
    )
    assert not future_report.usable
    assert any(finding.code == "FUTURE_BAR" for finding in future_report.findings)


def test_quality_rejects_incomplete_and_stale_quote_bars(platform):
    stale = replace(
        platform.bars[0],
        last_quote_at=platform.bars[0].timestamp - timedelta(minutes=1),
    )
    report = assess_bars(
        (stale,),
        spec=platform.spec,
        calendar=platform.calendar,
        freshness=make_quote_freshness(),
        evaluated_at=stale.end_at,
    )
    assert not report.usable
    assert any(finding.code == "INVALID_BAR" for finding in report.findings)
    assert any(
        finding.code == "STALE_OR_INVALID_LAST_QUOTE" for finding in report.findings
    )
