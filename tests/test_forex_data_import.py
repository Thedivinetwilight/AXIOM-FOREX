"""Focused tests for the offline canonical CSV importer.

Covers canonical import, DST/timezone handling, gaps, duplicate and conflicting
records, invalid bid/ask values, incomplete bars, manifest integrity, provenance
propagation, and fail-closed behaviour.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from axiom_forex.data.classes import (
    DATA_CLASS_SYNTHETIC_FIXTURE,
    DATA_CLASS_USER_SUPPLIED_DEMO,
    DATA_CLASS_USER_SUPPLIED_PRODUCTION,
    is_execution_eligible,
    is_imported,
)
from axiom_forex.ingest import (
    HEADER_COLUMNS,
    ForexDataImporter,
    ImportRequest,
    resolve_timestamp,
)
from axiom_forex.profiles.loader import load_provider_profile
from axiom_forex.profiles.paths import DEFAULT_FIXTURE_PROFILE_PATH
from axiom_forex.research.datasets import dataset_digest, payload_digest, quote_digest
from axiom_forex.strategies.examples import SmaCrossStrategy

HEADER = ",".join(HEADER_COLUMNS)
PAIR = "AAA/BBB"
NEW_YORK = ZoneInfo("America/New_York")
BAR_START = datetime(2026, 3, 10, 13, 50, tzinfo=timezone.utc)


def _join(cells: dict[str, str]) -> str:
    return ",".join(cells.get(name, "") for name in HEADER_COLUMNS)


def _quote_row(
    *,
    timestamp: datetime,
    bid: str = "1.09990",
    ask: str = "1.10010",
    quote_id: str = "q-1",
    received_at: datetime | None = None,
    correction_of: str = "",
    timezone_name: str = "UTC",
    timestamp_text: str | None = None,
    received_text: str | None = None,
) -> str:
    received = received_at or timestamp + timedelta(milliseconds=200)
    return _join(
        {
            "record_type": "QUOTE",
            "pair": PAIR,
            "timestamp": timestamp_text or timestamp.isoformat(),
            "timezone": timezone_name,
            "bid": bid,
            "ask": ask,
            "quote_id": quote_id,
            "received_at": received_text or received.isoformat(),
            "correction_of": correction_of,
        }
    )


def _bar_row(
    start: datetime,
    index: int = 0,
    *,
    complete: str = "true",
    spread_pips: Decimal = Decimal("2"),
    last_quote_offset_seconds: int = 59,
    timeframe: str = "1m",
    pair: str = PAIR,
    timezone_name: str = "UTC",
    timestamp_text: str | None = None,
    last_quote_text: str | None = None,
) -> str:
    mid = Decimal("1.10000") + Decimal(index) * Decimal("0.00005")
    half = spread_pips / Decimal(2) * Decimal("0.0001")
    bid_close = mid - half
    ask_close = mid + half
    bid_open = bid_close - Decimal("0.00002")
    bid_high = max(bid_open, bid_close) + Decimal("0.00003")
    bid_low = min(bid_open, bid_close) - Decimal("0.00003")
    ask_open = ask_close + Decimal("0.00002")
    ask_high = max(ask_open, ask_close) + Decimal("0.00003")
    ask_low = min(ask_open, ask_close) - Decimal("0.00003")
    last_quote = start + timedelta(seconds=last_quote_offset_seconds)
    return _join(
        {
            "record_type": "BAR",
            "pair": pair,
            "timestamp": timestamp_text or start.isoformat(),
            "timezone": timezone_name,
            "timeframe": timeframe,
            "duration_seconds": "60",
            "bid_open": str(bid_open),
            "bid_high": str(bid_high),
            "bid_low": str(bid_low),
            "bid_close": str(bid_close),
            "ask_open": str(ask_open),
            "ask_high": str(ask_high),
            "ask_low": str(ask_low),
            "ask_close": str(ask_close),
            "quote_count": "12",
            "last_quote_at": last_quote_text or last_quote.isoformat(),
            "complete": complete,
        }
    )


def _csv(*rows: str, comments: bool = True) -> str:
    prefix = (
        "# AXIOM-FOREX canonical import CSV (format AXIOM_FOREX_CANONICAL_CSV, schema 1.0.0)\n"
        if comments
        else ""
    )
    return prefix + HEADER + "\n" + "\n".join(rows) + "\n"


def _dataset_rows(count: int = 6) -> list[str]:
    """Alternating BAR/QUOTE rows: rows[0] is bar 0, rows[1] is quote 1."""
    rows: list[str] = []
    for index in range(count):
        bar_start = BAR_START + timedelta(minutes=index)
        rows.append(_bar_row(bar_start, index=index))
        rows.append(
            _quote_row(
                timestamp=bar_start + timedelta(seconds=59),
                quote_id=f"q-{index + 1}",
            )
        )
    return rows


def _request(**overrides) -> ImportRequest:
    values: dict[str, Any] = {
        "pair": PAIR,
        "timeframe": "1m",
        "data_class": DATA_CLASS_USER_SUPPLIED_DEMO,
        "source": "LOCAL_USER_CSV",
        "source_version": "declared-v1",
        "valid_from": datetime(2026, 3, 10, 13, 0, tzinfo=timezone.utc),
        "valid_until": datetime(2026, 3, 10, 14, 0, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return ImportRequest(**values)


def call_app(app, path, *, method="GET", auth=None):
    captured: dict[str, Any] = {}

    def start_response(status, headers):
        captured["status"] = status
        captured["headers"] = dict(headers)

    environ: dict[str, Any] = {"PATH_INFO": path, "REQUEST_METHOD": method}
    if auth is not None:
        environ["HTTP_AUTHORIZATION"] = auth
    body = b"".join(app(environ, start_response))
    return captured["status"], captured["headers"], body


@pytest.fixture
def importer(platform) -> ForexDataImporter:
    profile = load_provider_profile(DEFAULT_FIXTURE_PROFILE_PATH)
    return ForexDataImporter(
        catalog_manager=platform.catalog_manager,
        calendar=platform.calendar,
        quote_freshness=platform.quote_freshness,
        profile=profile,
        profile_passed=True,
        now=datetime(2026, 3, 10, 15, 0, tzinfo=timezone.utc),
    )


# -- canonical import and manifest integrity --------------------------------


def test_canonical_csv_import_round_trip_and_manifest_integrity(importer):
    text = _csv(*_dataset_rows())
    result = importer.import_text(text, _request())

    assert result.status == "PASS"
    assert result.passed
    dataset = result.dataset
    assert dataset is not None
    assert dataset.data_class == DATA_CLASS_USER_SUPPLIED_DEMO
    assert dataset.execution_eligible is False
    assert dataset.verify()
    assert len(dataset.bars) == 6
    assert len(dataset.quotes) == 6

    manifest = dataset.import_manifest
    assert manifest.format_id == "AXIOM_FOREX_CANONICAL_CSV"
    assert manifest.schema_version == "1.0.0"
    assert manifest.source_sha256 == payload_digest(text)
    assert len(manifest.source_sha256) == 64
    assert manifest.normalized_sha256 == dataset_digest(dataset.bars)
    assert manifest.quotes_sha256 == quote_digest(dataset.quotes)
    assert manifest.timezone_basis == "UTC"
    assert manifest.bar_count == 6
    assert manifest.pair_spec_version == dataset.spec.spec_version
    assert manifest.calendar_signature == dataset.dataset_manifest.calendar_signature
    assert manifest.validation_classification == "VALID"
    assert manifest.first_timestamp == dataset.bars[0].timestamp.isoformat()
    assert manifest.last_timestamp == dataset.bars[-1].end_at.isoformat()

    view = result.audit_view()
    assert view["manifest_valid"] is True
    assert view["data_class"] == DATA_CLASS_USER_SUPPLIED_DEMO
    assert view["provenance"]["source"] == "LOCAL_USER_CSV"
    assert view["counts"]["accepted_bars"] == 6
    assert view["request"]["data_class"] == DATA_CLASS_USER_SUPPLIED_DEMO
    assert "tests/" not in str(view)

    loaded = dataset.as_loaded_dataset()
    assert loaded.verify()
    assert loaded.data_class == DATA_CLASS_USER_SUPPLIED_DEMO
    assert loaded.audit_view()["digest_valid"] is True


def test_manifest_rejects_tampering(importer):
    result = importer.import_text(_csv(*_dataset_rows()), _request())
    dataset = result.dataset
    assert dataset is not None

    tampered = replace(dataset.import_manifest, normalized_sha256="0" * 64)
    assert (
        tampered.verify(
            bars=dataset.bars,
            quotes=dataset.quotes,
            dataset_manifest=dataset.dataset_manifest,
        )
        is False
    )
    relabeled = replace(dataset.import_manifest, data_class=DATA_CLASS_SYNTHETIC_FIXTURE)
    assert (
        relabeled.verify(
            bars=dataset.bars,
            quotes=dataset.quotes,
            dataset_manifest=dataset.dataset_manifest,
        )
        is False
    )


# -- DST and timezone handling ---------------------------------------------


def test_dst_ambiguous_and_nonexistent_wall_times_are_rejected():
    ambiguous, issues = resolve_timestamp(
        "2026-11-01T01:30:00", zone_name="America/New_York", field_name="timestamp", line=7
    )
    nonexistent, issues_2 = resolve_timestamp(
        "2026-03-08T02:30:00", zone_name="America/New_York", field_name="timestamp", line=8
    )
    valid, issues_3 = resolve_timestamp(
        "2026-03-10T09:50:00", zone_name="America/New_York", field_name="timestamp", line=9
    )
    before_dst, issues_4 = resolve_timestamp(
        "2026-03-06T09:50:00", zone_name="America/New_York", field_name="timestamp", line=11
    )
    mismatch, issues_5 = resolve_timestamp(
        "2026-03-10T13:50:00+00:00", zone_name="America/New_York", field_name="timestamp", line=10
    )

    assert ambiguous is None
    assert [issue.code for issue in issues] == ["ambiguous_local_timestamp"]
    assert nonexistent is None
    assert [issue.code for issue in issues_2] == ["nonexistent_local_timestamp"]
    assert valid == datetime(2026, 3, 10, 13, 50, tzinfo=timezone.utc)
    assert issues_3 == []
    # Before the DST change the same wall time is UTC-05:00, not UTC-04:00.
    assert before_dst == datetime(2026, 3, 6, 14, 50, tzinfo=timezone.utc)
    assert issues_4 == []
    assert mismatch is None
    assert [issue.code for issue in issues_5] == ["timestamp_offset_mismatch"]


def test_import_with_local_wall_times_matches_utc_import(importer):
    utc_rows = _dataset_rows()
    local_rows: list[str] = []
    for index in range(6):
        bar_start = BAR_START + timedelta(minutes=index)
        last_quote = bar_start + timedelta(seconds=59)
        local_rows.append(
            _bar_row(
                bar_start,
                index=index,
                timezone_name="America/New_York",
                timestamp_text=bar_start.astimezone(NEW_YORK).replace(tzinfo=None).isoformat(),
                last_quote_text=last_quote.astimezone(NEW_YORK)
                .replace(tzinfo=None)
                .isoformat(),
            )
        )
        local_rows.append(
            _quote_row(
                timestamp=last_quote,
                quote_id=f"q-{index + 1}",
                timezone_name="America/New_York",
                timestamp_text=last_quote.astimezone(NEW_YORK)
                .replace(tzinfo=None)
                .isoformat(),
                received_text=(last_quote + timedelta(milliseconds=200))
                .astimezone(NEW_YORK)
                .replace(tzinfo=None)
                .isoformat(),
            )
        )
    utc_result = importer.import_text(_csv(*utc_rows), _request())
    local_result = importer.import_text(_csv(*local_rows), _request())

    assert utc_result.passed, utc_result.reason
    assert local_result.passed, local_result.reason
    assert [bar.timestamp for bar in utc_result.dataset.bars] == [
        bar.timestamp for bar in local_result.dataset.bars
    ]
    assert [bar.last_quote_at for bar in utc_result.dataset.bars] == [
        bar.last_quote_at for bar in local_result.dataset.bars
    ]


# -- gaps, corrections, duplicates ----------------------------------------


def test_gap_is_preserved_as_quality_event_without_interpolation(importer):
    rows = _dataset_rows()
    without_bar_3 = [row for index, row in enumerate(rows) if index != 6]
    result = importer.import_text(_csv(*without_bar_3), _request())

    assert result.status == "QUARANTINED"
    assert result.passed is False
    codes = [event.code for event in result.quality_events]
    assert "MISSING_BAR" in codes
    dataset = result.dataset
    assert dataset is not None
    assert dataset.usable is False
    assert len(dataset.bars) == 5
    assert [bar.timestamp for bar in dataset.bars] == [
        BAR_START + timedelta(minutes=index) for index in (0, 1, 2, 4, 5)
    ]
    assert dataset.as_loaded_dataset().usable is False


def test_duplicate_records_are_recorded_and_conflicts_rejected(importer):
    rows = _dataset_rows()
    duplicate_quote = _quote_row(
        timestamp=BAR_START + timedelta(seconds=59), quote_id="q-1"
    )
    duplicate_bar = _bar_row(BAR_START, index=0)
    duplicated = importer.import_text(
        _csv(*rows, duplicate_quote, duplicate_bar), _request()
    )
    assert duplicated.passed, duplicated.reason
    assert duplicated.dataset is not None
    assert duplicated.dataset.import_manifest.duplicate_count == 2
    assert {event.code for event in duplicated.quality_events} >= {
        "DUPLICATE_QUOTE",
        "DUPLICATE_BAR",
    }

    conflicting_quote = importer.import_text(
        _csv(
            *rows,
            _quote_row(
                timestamp=BAR_START + timedelta(seconds=59, milliseconds=500),
                bid="1.10050",
                ask="1.10060",
                quote_id="q-1",
            ),
        ),
        _request(),
    )
    assert conflicting_quote.status == "FAIL"
    assert conflicting_quote.dataset is None
    assert "conflicting_quote" in {issue.code for issue in conflicting_quote.issues}
    assert conflicting_quote.quarantine

    conflicting_bar = importer.import_text(
        _csv(*rows, _bar_row(BAR_START, index=0, spread_pips=Decimal("4"))),
        _request(),
    )
    assert conflicting_bar.status == "FAIL"
    assert "conflicting_bar" in {issue.code for issue in conflicting_bar.issues}

    corrected = importer.import_text(
        _csv(
            *rows,
            _quote_row(
                timestamp=BAR_START + timedelta(seconds=59),
                bid="1.09995",
                ask="1.10015",
                quote_id="q-1-corrected",
                correction_of="q-1",
            ),
        ),
        _request(),
    )
    assert corrected.passed, corrected.reason
    assert corrected.dataset is not None
    assert corrected.dataset.import_manifest.correction_count == 1
    assert "QUOTE_CORRECTION" in {event.code for event in corrected.quality_events}


def test_unknown_correction_target_is_rejected(importer):
    result = importer.import_text(
        _csv(
            *_dataset_rows(),
            _quote_row(
                timestamp=BAR_START + timedelta(seconds=30),
                quote_id="q-9",
                correction_of="missing-quote",
            ),
        ),
        _request(),
    )
    assert result.status == "FAIL"
    assert "unknown_correction_target" in {issue.code for issue in result.issues}


# -- invalid values, incomplete bars, stale quotes ------------------------


def test_incomplete_bar_is_rejected(importer):
    rows = _dataset_rows()
    rows[0] = _bar_row(BAR_START, index=0, complete="false")
    result = importer.import_text(_csv(*rows), _request())
    assert result.status == "FAIL"
    assert result.dataset is None
    assert "incomplete_bar" in {issue.code for issue in result.issues}
    assert result.quarantine and result.quarantine[0].line == 3


@pytest.mark.parametrize(
    ("replacement", "code"),
    [
        (
            lambda rows: _quote_row(
                timestamp=BAR_START + timedelta(seconds=59),
                bid="1.10020",
                ask="1.10010",
                quote_id="q-1",
            ),
            "crossed_quote",
        ),
        (
            lambda rows: _quote_row(
                timestamp=BAR_START + timedelta(seconds=59),
                bid="-1.00000",
                ask="1.10010",
                quote_id="q-1",
            ),
            "non_positive_bid",
        ),
        (
            lambda rows: _bar_row(BAR_START, index=0, last_quote_offset_seconds=1),
            "STALE_OR_INVALID_LAST_QUOTE",
        ),
        (
            lambda rows: _bar_row(BAR_START, index=0, spread_pips=Decimal("40")),
            "spread_limit_exceeded",
        ),
        (
            lambda rows: _bar_row(BAR_START, index=0, timeframe="5m"),
            "timeframe_mismatch",
        ),
        (
            lambda rows: _bar_row(BAR_START, index=0, pair="XXX/YYY"),
            "unexpected_pair",
        ),
        (
            lambda rows: _bar_row(BAR_START, index=0, timestamp_text="not-a-timestamp"),
            "invalid_timestamp",
        ),
    ],
)
def test_invalid_records_fail_closed(importer, replacement, code):
    rows = _dataset_rows()
    row = replacement(rows)
    rows[0 if row.startswith("BAR") else 1] = row
    result = importer.import_text(_csv(*rows), _request())
    observed = {issue.code for issue in result.issues} | {
        event.code for event in result.quality_events
    }
    assert result.passed is False
    assert result.dataset is None
    assert code in observed, observed


def test_bars_outside_calendar_sessions_are_rejected(importer):
    # 2026-03-09 is an explicit fixture-calendar holiday; 2026-03-10 21:30Z is
    # inside the explicit 17:00-18:00 local daily break.
    holiday_start = datetime(2026, 3, 9, 14, 0, tzinfo=timezone.utc)
    holiday_rows = [
        _bar_row(holiday_start, index=0),
        _bar_row(holiday_start + timedelta(minutes=1), index=1),
        _quote_row(timestamp=holiday_start + timedelta(seconds=59), quote_id="h-1"),
    ]
    holiday = importer.import_text(
        _csv(*holiday_rows),
        _request(
            valid_from=datetime(2026, 3, 9, 13, 0, tzinfo=timezone.utc),
            valid_until=datetime(2026, 3, 9, 15, 0, tzinfo=timezone.utc),
        ),
    )
    break_start = datetime(2026, 3, 10, 21, 30, tzinfo=timezone.utc)
    daily_break_rows = [
        _bar_row(break_start, index=0),
        _bar_row(break_start + timedelta(minutes=1), index=1),
        _quote_row(timestamp=break_start + timedelta(seconds=59), quote_id="b-1"),
    ]
    daily_break = importer.import_text(
        _csv(*daily_break_rows),
        _request(
            valid_from=datetime(2026, 3, 10, 21, 0, tzinfo=timezone.utc),
            valid_until=datetime(2026, 3, 10, 23, 0, tzinfo=timezone.utc),
        ),
    )

    for result in (holiday, daily_break):
        assert result.status == "FAIL"
        assert result.dataset is None
        assert "OUT_OF_SESSION_BAR" in {
            event.code for event in result.quality_events
        }


def test_malformed_rows_header_problems_and_empty_files_fail(importer):
    malformed = importer.import_text(
        _csv(*_dataset_rows(), "QUOTE,AAA/BBB"), _request()
    )
    missing_column = importer.import_text(
        HEADER.replace(",received_at", "")
        + "\n"
        + _quote_row(timestamp=BAR_START + timedelta(seconds=59)),
        _request(),
    )
    unknown_column = importer.import_text(
        HEADER + ",vendor_field\n" + _bar_row(BAR_START, index=0), _request()
    )
    empty = importer.import_text("# only comments\n", _request())

    assert "malformed_row" in {issue.code for issue in malformed.issues}
    assert "header_missing_column" in {issue.code for issue in missing_column.issues}
    assert "header_unknown_column" in {issue.code for issue in unknown_column.issues}
    assert empty.status == "FAIL"
    for result in (malformed, missing_column, unknown_column, empty):
        assert result.dataset is None


def test_stale_quote_receive_time_is_rejected(importer):
    stale = _quote_row(
        timestamp=BAR_START + timedelta(seconds=30),
        quote_id="q-late",
        received_at=BAR_START + timedelta(seconds=32),
    )
    result = importer.import_text(_csv(*_dataset_rows(), stale), _request())
    assert result.status == "FAIL"
    assert "stale_quote" in {issue.code for issue in result.issues}


# -- validity bounds and profile compatibility ---------------------------


def test_records_outside_declared_validity_bounds_are_rejected(importer):
    request = _request(
        valid_from=datetime(2026, 3, 10, 13, 51, tzinfo=timezone.utc),
        valid_until=datetime(2026, 3, 10, 13, 56, tzinfo=timezone.utc),
    )
    result = importer.import_text(_csv(*_dataset_rows()), request)
    assert result.status == "FAIL"
    assert "outside_validity_bounds" in {issue.code for issue in result.issues}


def test_incompatible_profile_terms_block_the_import(importer):
    outside_history = importer.import_text(
        _csv(*_dataset_rows()),
        _request(
            valid_from=datetime(2026, 2, 1, tzinfo=timezone.utc),
            valid_until=datetime(2026, 2, 2, tzinfo=timezone.utc),
        ),
    )
    production = importer.import_text(
        _csv(*_dataset_rows()),
        _request(data_class=DATA_CLASS_USER_SUPPLIED_PRODUCTION),
    )
    unknown_timeframe = importer.import_text(
        _csv(*_dataset_rows()),
        _request(timeframe="15m"),
    )
    unknown_pair = importer.import_text(_csv(*_dataset_rows()), _request(pair="EUR/USD"))

    assert outside_history.status == "FAIL"
    assert outside_history.reason.startswith("profile_incompatible")
    assert "history_bounds" in outside_history.reason
    assert production.status == "FAIL"
    assert "data_class" in production.reason
    assert unknown_timeframe.status == "FAIL"
    assert "timeframe_supported" in unknown_timeframe.reason
    assert unknown_pair.status == "FAIL"
    assert unknown_pair.reason == "unknown_pair_spec"
    for result in (outside_history, production, unknown_timeframe, unknown_pair):
        assert result.dataset is None


def test_unvalidated_profile_blocks_the_import(importer):
    unvalidated = ForexDataImporter(
        catalog_manager=importer.catalog_manager,
        calendar=importer.calendar,
        quote_freshness=importer.quote_freshness,
        profile=importer.profile,
        profile_passed=False,
        now=importer.now,
    )
    result = unvalidated.import_text(_csv(*_dataset_rows()), _request())
    assert result.status == "FAIL"
    assert "profile_validated" in result.reason


def test_unsafe_source_labels_and_invalid_requests_are_rejected(importer):
    path_label = importer.import_text(
        _csv(*_dataset_rows()), _request(source="/home/user/private/quotes.csv")
    )
    missing_bounds = importer.import_text(
        _csv(*_dataset_rows()), _request(valid_from=None, valid_until=None)
    )
    synthetic_label = importer.import_text(
        _csv(*_dataset_rows()), _request(data_class=DATA_CLASS_SYNTHETIC_FIXTURE)
    )

    assert "invalid_import_request" in path_label.reason
    assert "unsafe_source_label" in path_label.reason
    assert path_label.dataset is None
    assert "invalid_validity_bounds" in missing_bounds.reason
    assert "unsupported_data_class" in synthetic_label.reason


# -- platform integration -------------------------------------------------


def test_platform_import_provenance_and_fail_closed_workflow(platform):
    result = platform.import_dataset(_request(), text=_csv(*_dataset_rows()))
    assert result.passed, result.reason

    report = platform.dataset_provenance_report()
    assert report["data_class"] == DATA_CLASS_SYNTHETIC_FIXTURE
    assert report["synthetic"] is True
    latest = report["latest_import"]
    assert latest["data_class"] == DATA_CLASS_USER_SUPPLIED_DEMO
    assert latest["source"] == "LOCAL_USER_CSV"
    assert latest["manifest_valid"] is True
    assert latest["execution_eligible"] is False
    assert latest["adopted_for_research"] is False

    stored = platform.repository.get_dataset(result.dataset.dataset_manifest.dataset_id)
    assert stored is not None
    assert stored["data_class"] == DATA_CLASS_USER_SUPPLIED_DEMO
    imported_events = [
        event
        for event in platform.audit_events(limit=50)
        if event.get("event_type") == "PAPER_PLATFORM_DATA_IMPORTED"
    ]
    assert imported_events and imported_events[-1]["data_class"] == DATA_CLASS_USER_SUPPLIED_DEMO

    platform.use_imported_dataset()
    assert platform.data_class == DATA_CLASS_USER_SUPPLIED_DEMO
    assert is_execution_eligible(platform.data_class) is False
    assert platform.dataset_provenance_report()["synthetic"] is False

    backtest = platform.run_imported_backtest(
        SmaCrossStrategy(fast_period=2, slow_period=3, name="imported-smoke"),
        run_id="imported-smoke",
    )
    assert backtest.usable
    assert backtest.config.dataset_id == result.dataset.dataset_manifest.dataset_id
    assert backtest.config.dataset_id != "synthetic-dataset-v1"

    blocked = platform.run_paper_workflow(workflow_id="after-import")
    assert blocked.blocked
    assert blocked.reason == "data_class_not_execution_eligible"
    assert blocked.order is None
    assert blocked.fill is None

    verification = platform.verify()
    assert verification["checks"]["dataset_provenance_labeled"] is True
    assert len(verification["checks"]) == 18
    assert verification["checks"]["entry_limit_declared"] is True
    assert verification["checks"]["entry_limit_provenance_labeled"] is True
    assert verification["entry_exposure_limit"]["synthetic"] is True
    assert verification["checks"]["market_access_vault_only"] is True
    assert verification["market_access_setting"]["configured"] is False
    assert (
        verification["market_access_setting"]["plaintext_fallback"] is False
    )


def test_rejected_import_never_replaces_the_fixture_dataset(platform):
    before = platform.manifest.dataset_id
    result = platform.import_dataset(
        _request(),
        text=_csv(
            *_dataset_rows(),
            _bar_row(BAR_START + timedelta(minutes=5), index=5, complete="false"),
        ),
    )
    assert result.passed is False
    assert platform.manifest.dataset_id == before
    assert platform.data_class == DATA_CLASS_SYNTHETIC_FIXTURE
    assert platform.imported_dataset is None
    assert platform.dataset_provenance_report()["synthetic"] is True

    workflow = platform.run_paper_workflow(workflow_id="fixture-after-rejected-import")
    assert workflow.dataset["data_class"] == DATA_CLASS_SYNTHETIC_FIXTURE
    assert workflow.blocked is False


def test_production_import_is_refused_on_a_fixture_platform(platform):
    with pytest.raises(
        ValueError, match="production_data_cannot_be_imported_into_fixture_platform"
    ):
        platform.import_dataset(
            _request(data_class=DATA_CLASS_USER_SUPPLIED_PRODUCTION),
            text=_csv(*_dataset_rows()),
        )
    assert is_imported(platform.data_class) is False


def test_import_requires_exactly_one_source(platform):
    with pytest.raises(ValueError, match="import_requires_text_or_path"):
        platform.import_dataset(_request())
    with pytest.raises(ValueError, match="import_accepts_one_source"):
        platform.import_dataset(_request(), text="x", path="y")


def test_operator_surfaces_expose_data_class_without_paths(platform):
    result = platform.import_dataset(_request(), text=_csv(*_dataset_rows()))
    assert result.passed

    status = platform.status()
    assert status["data_provenance"]["data_class"] == DATA_CLASS_SYNTHETIC_FIXTURE
    assert (
        status["data_provenance"]["latest_import"]["data_class"]
        == DATA_CLASS_USER_SUPPLIED_DEMO
    )
    assert status["imported_dataset"]["data_class"] == DATA_CLASS_USER_SUPPLIED_DEMO

    report = platform.report_markdown()
    assert "Latest imported data class: `USER_SUPPLIED_DEMO`" in report
    assert "user-supplied research material" in report
    assert "/home/" not in report

    dashboard = platform.dashboard_html()
    assert "Data provenance" in dashboard
    assert "USER_SUPPLIED_DEMO" in dashboard

    viewmodel = platform.desktop_viewmodel()
    assert (
        viewmodel["dataset_provenance"]["latest_import"]["data_class"]
        == DATA_CLASS_USER_SUPPLIED_DEMO
    )

    app = platform.create_wsgi_app()
    auth = "Bearer " + platform.runtime_settings.api_bearer_token
    status_line, _, body = call_app(app, "/dataset", auth=auth)
    assert status_line.startswith("200")
    assert b"USER_SUPPLIED_DEMO" in body
