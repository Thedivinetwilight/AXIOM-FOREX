"""The MT5 tick-export → canonical CSV workflow.

The rule this file defends: a bar is built from the broker's own two-sided ticks,
or it is not built at all.  No candle is split into bid/ask, no empty window is
interpolated, and no server timestamp is guessed.

The exports here are tiny synthetic files that mimic MetaTrader 5's Ticks window.
They are not market data and nothing here is evidence about any market.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from axiom_forex.ingest.canonical_csv import parse_canonical_csv
from axiom_forex.ingest.contracts import HEADER_COLUMNS
from axiom_forex.ingest.mt5_ticks import (
    TickExportRefused,
    build_quote_bars,
    convert,
    read_ticks,
    resolve_local_times,
)

UTC = timezone.utc

#: MetaTrader 5 writes a bracketed header and tab-separated rows.  Two minutes of
#: ticks at 13:50 and 13:51, a real hole at 13:52, then a partial final minute.
EXPORT = (
    "<DATE>\t<TIME>\t<BID>\t<ASK>\t<LAST>\t<VOLUME>\t<FLAGS>\n"
    "2026.03.10\t13:50:00.100\t1.09990\t1.10010\t0\t1\t6\n"
    "2026.03.10\t13:50:30.400\t1.09995\t1.10015\t0\t1\t6\n"
    "2026.03.10\t13:51:01.000\t1.10000\t1.10020\t0\t2\t6\n"
    "2026.03.10\t13:51:59.900\t1.10005\t1.10025\t0\t1\t6\n"
    "2026.03.10\t13:53:10.000\t1.10010\t1.10030\t0\t1\t6\n"
)


def write_export(directory: Path, text: str = EXPORT, name: str = "ticks.csv") -> Path:
    path = Path(directory) / name
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 1. parsing
# ---------------------------------------------------------------------------


def test_a_bracketed_mt5_header_is_understood(tmp_path):
    export = read_ticks(write_export(tmp_path))
    assert export.header == ("date", "time", "bid", "ask", "last", "volume", "flags")
    assert export.count == 5
    assert export.ticks[0].bid == Decimal("1.09990")
    assert export.ticks[0].ask == Decimal("1.10010")


def test_a_comma_export_without_a_header_is_read_positionally(tmp_path):
    text = (
        "2026.03.10,13:50:00.000,1.09990,1.10010,0,1,6\n"
        "2026.03.10,13:50:30.000,1.09995,1.10015,0,1,6\n"
    )
    export = read_ticks(write_export(tmp_path, text))
    assert export.delimiter == ","
    assert export.count == 2


def test_a_crossed_quote_is_rejected_and_counted(tmp_path):
    text = EXPORT.replace("1.10010\t0\t1", "1.09900\t0\t1")
    export = read_ticks(write_export(tmp_path, text))
    assert export.count == 4
    assert any(item["code"] == "crossed_quote" for item in export.rejected)


def test_out_of_order_rows_are_refused_rather_than_sorted(tmp_path):
    lines = EXPORT.splitlines()
    text = "\n".join([lines[0], lines[3], lines[1], lines[2], lines[4], lines[5]])
    with pytest.raises(TickExportRefused) as excinfo:
        read_ticks(write_export(tmp_path, text))
    assert excinfo.value.reason == "tick_rows_out_of_order"


def test_a_file_without_bid_and_ask_columns_is_not_a_tick_export(tmp_path):
    text = "date,time,close,volume\n2026.03.10,13:50:00,1.1000,1\n"
    with pytest.raises(TickExportRefused) as excinfo:
        read_ticks(write_export(tmp_path, text))
    # No bid/ask means no two-sided price: the file is refused, not assumed.
    assert excinfo.value.reason in {"tick_header_incomplete"}


def test_a_missing_file_is_reported_not_created(tmp_path):
    with pytest.raises(TickExportRefused) as excinfo:
        read_ticks(Path(tmp_path) / "nope.csv")
    assert excinfo.value.reason == "tick_file_missing"


# ---------------------------------------------------------------------------
# 2. server time → UTC
# ---------------------------------------------------------------------------


def test_the_server_offset_must_be_declared(tmp_path):
    export = read_ticks(write_export(tmp_path))
    with pytest.raises(TickExportRefused) as excinfo:
        resolve_local_times(export, zone_name="", offset_minutes=None)
    assert excinfo.value.reason == "server_time_basis_missing"


def test_a_declared_offset_moves_every_tick_by_exactly_that_much(tmp_path):
    export = read_ticks(write_export(tmp_path))
    shifted = resolve_local_times(export, zone_name="", offset_minutes=180)
    assert shifted.ticks[0].at == datetime(2026, 3, 10, 10, 50, 0, 100000, tzinfo=UTC)


def test_an_ambiguous_dst_wall_time_is_refused(tmp_path):
    text = EXPORT.replace("2026.03.10", "2026.11.01").replace("13:50", "01:30").replace(
        "13:51", "01:31"
    ).replace("13:53", "01:32")
    export = read_ticks(write_export(tmp_path, text))
    with pytest.raises(TickExportRefused) as excinfo:
        resolve_local_times(export, zone_name="America/New_York", offset_minutes=None)
    assert excinfo.value.reason in {"ambiguous_server_time", "tick_rows_out_of_order"}


# ---------------------------------------------------------------------------
# 3. aggregation: complete bars only
# ---------------------------------------------------------------------------


def test_only_full_windows_become_bars(tmp_path):
    export = read_ticks(write_export(tmp_path))
    resolved = resolve_local_times(export, zone_name="", offset_minutes=0)
    bars, coverage = build_quote_bars(resolved, pair="EUR/USD")
    assert [bar["timestamp"] for bar in bars] == [
        datetime(2026, 3, 10, 13, 50, tzinfo=UTC),
        datetime(2026, 3, 10, 13, 51, tzinfo=UTC),
    ]
    first = bars[0]
    assert first["bid_open"] == Decimal("1.09990")
    assert first["bid_high"] == Decimal("1.09995")
    assert first["bid_low"] == Decimal("1.09990")
    assert first["bid_close"] == Decimal("1.09995")
    assert first["ask_open"] == Decimal("1.10010")
    assert first["quote_count"] == 2
    assert first["complete"] is True
    # The trailing minute was cut short by the export's end: reported, not emitted.
    assert coverage.trailing_partial_window == datetime(2026, 3, 10, 13, 53, tzinfo=UTC)
    assert coverage.complete is False


def test_a_hole_in_the_export_is_a_gap_not_a_bar(tmp_path):
    export = read_ticks(write_export(tmp_path))
    resolved = resolve_local_times(export, zone_name="", offset_minutes=0)
    bars, coverage = build_quote_bars(resolved, pair="EUR/USD")
    stamps = {bar["timestamp"] for bar in bars}
    assert datetime(2026, 3, 10, 13, 52, tzinfo=UTC) not in stamps
    assert coverage.gap_windows == [
        {"window": "2026-03-10T13:52:00+00:00", "reason": "no_ticks"}
    ]
    assert coverage.empty_windows == 1
    assert coverage.largest_gap_seconds == Decimal("120.0")


def test_a_clean_export_reports_full_coverage(tmp_path):
    text = (
        "<DATE>\t<TIME>\t<BID>\t<ASK>\t<LAST>\t<VOLUME>\t<FLAGS>\n"
        "2026.03.10\t13:50:00.100\t1.09990\t1.10010\t0\t1\t6\n"
        "2026.03.10\t13:50:59.900\t1.09995\t1.10015\t0\t1\t6\n"
        "2026.03.10\t13:51:30.000\t1.10000\t1.10020\t0\t1\t6\n"
        "2026.03.10\t13:52:01.000\t1.10005\t1.10025\t0\t1\t6\n"
    )
    export = read_ticks(write_export(tmp_path, text))
    resolved = resolve_local_times(export, zone_name="", offset_minutes=0)
    bars, coverage = build_quote_bars(resolved, pair="EUR/USD")
    assert len(bars) == 2
    assert coverage.complete is True


# ---------------------------------------------------------------------------
# 4. the written file is canonical, and the provenance is explicit
# ---------------------------------------------------------------------------


def test_the_written_csv_uses_the_canonical_header_and_parses(tmp_path):
    source = write_export(tmp_path)
    report = convert(
        source=source,
        pair="EUR/USD",
        output=Path(tmp_path) / "canonical.csv",
        server_offset_minutes=0,
    )
    written = Path(report["output"])
    header = written.read_text(encoding="utf-8").splitlines()[0].split(",")
    assert tuple(header) == HEADER_COLUMNS
    assert report["bars_written"] == 2

    parsed = parse_canonical_csv(
        written.read_text(encoding="utf-8"), source_label="MT5_TICK_EXPORT"
    )
    assert not parsed.fatal
    assert parsed.error_count() == 0
    kinds = [record.record_type for record in parsed.records]
    assert kinds and set(kinds) == {"BAR"}


def test_provenance_names_the_file_the_hash_and_the_derivation(tmp_path):
    source = write_export(tmp_path)
    report = convert(
        source=source,
        pair="EUR/USD",
        output=Path(tmp_path) / "canonical.csv",
        server_offset_minutes=120,
    )
    assert report["derivation"] == "DERIVED_FROM_BROKER_TICKS"
    assert report["format"] == "MT5_TICK_EXPORT"
    assert report["source_file"] == source.name
    assert len(report["source_sha256"]) == 64
    assert report["tick_count"] == 5
    sidecar = json.loads(Path(report["provenance_path"]).read_text(encoding="utf-8"))
    assert sidecar["derivation"] == "DERIVED_FROM_BROKER_TICKS"
    assert sidecar["pair"] == "EUR/USD"
    assert any("no candle was split" in note for note in sidecar["notes"])


def test_the_data_audit_repeats_the_sidecar_provenance(tmp_path):
    """The audit cites the conversion's own claims; it never upgrades them."""

    from axiom_forex.paper_runner import data_audit

    source = write_export(tmp_path)
    report = convert(
        source=source,
        pair="EUR/USD",
        output=Path(tmp_path) / "canonical.csv",
        server_offset_minutes=120,
    )
    audit = data_audit.audit(directory=str(tmp_path))
    finding = next(
        item for item in audit["files"] if item["name"] == "canonical.csv"
    )
    provenance = finding["provenance"]
    assert provenance["derivation"] == "DERIVED_FROM_BROKER_TICKS"
    assert provenance["source_sha256"] == report["source_sha256"]
    assert provenance["source_file"] == "ticks.csv"
    assert provenance["time_basis_detail"] == "offset_minutes=120"
    text = data_audit.markdown(audit)
    assert "DERIVED_FROM_BROKER_TICKS" in text
    assert report["source_sha256"][:12] in text


def test_a_canonical_file_without_a_sidecar_has_no_provenance(tmp_path):
    from axiom_forex.paper_runner import data_audit

    source = write_export(tmp_path)
    convert(
        source=source,
        pair="EUR/USD",
        output=Path(tmp_path) / "canonical.csv",
        server_offset_minutes=0,
    )
    (Path(tmp_path) / "canonical.csv.provenance.json").unlink()
    audit = data_audit.audit(directory=str(tmp_path))
    finding = next(item for item in audit["files"] if item["name"] == "canonical.csv")
    assert finding["provenance"] == {}


def test_duplicate_ticks_are_counted_once(tmp_path):
    text = EXPORT + "2026.03.10\t13:50:00.100\t1.09990\t1.10010\t0\t1\t6\n"
    export = read_ticks(write_export(tmp_path, text))
    assert export.duplicate_count == 1
    assert export.count == 5
