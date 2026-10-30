"""The workstation's data workflow: scan, convert, load, evaluate.

The rule this file defends is the same one the CLI defends: a candidate is only
ever scored on a file the operator supplied and declared, the server clock basis
is never guessed, and a spent locked-test window stays spent.

Every dataset here is tiny and synthetic.  Nothing in this file is market data,
and no result here says anything about a real market.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from axiom_forex import build_fixture_platform
from axiom_forex.ingest.contracts import HEADER_COLUMNS
from axiom_forex.ingest.mt5_ticks import render_canonical
from axiom_forex.paper_runner import evaluation
from axiom_forex.workstation import dataflow
from axiom_forex.workstation.app import WorkstationApp
from axiom_forex.workstation.setup import SetupWizard

from tests.test_workstation_ui import offline_environment, simulated_wizard

UTC = __import__("datetime").timezone.utc

EXPORT = (
    "<DATE>\t<TIME>\t<BID>\t<ASK>\t<LAST>\t<VOLUME>\t<FLAGS>\n"
    "2026.03.10\t13:50:00.100\t1.09990\t1.10010\t0\t1\t6\n"
    "2026.03.10\t13:50:59.900\t1.09995\t1.10015\t0\t1\t6\n"
    "2026.03.10\t13:51:01.000\t1.10000\t1.10020\t0\t2\t6\n"
    "2026.03.10\t13:51:59.900\t1.10005\t1.10025\t0\t1\t6\n"
    "2026.03.10\t13:53:10.000\t1.10010\t1.10030\t0\t1\t6\n"
)


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def canonical_text(platform, limit: int = 120) -> str:
    """Render the fixture platform's own bars as a canonical CSV.

    This is a *format* exercise: the values come from the fixture's synthetic
    series, so the file is a sample and is declared as one.
    """

    rows = []
    for bar in list(platform.bars)[:limit]:
        rows.append(
            {
                "pair": bar.pair,
                "timestamp": bar.timestamp,
                "timeframe": bar.timeframe,
                "duration_seconds": bar.duration_seconds,
                "quote_count": max(int(bar.quote_count or 0), 1),
                "last_quote_at": bar.last_quote_at,
                "complete": True,
                "bid_open": bar.bid.open,
                "bid_high": bar.bid.high,
                "bid_low": bar.bid.low,
                "bid_close": bar.bid.close,
                "ask_open": bar.ask.open,
                "ask_high": bar.ask.high,
                "ask_low": bar.ask.low,
                "ask_close": bar.ask.close,
            }
        )
    return render_canonical(rows)


# ---------------------------------------------------------------------------
# 1. scanning: describe, never interpret
# ---------------------------------------------------------------------------


def test_a_broker_tick_export_is_recognised_and_marked_convertible(tmp_path):
    write(tmp_path / "EURUSD_ticks.csv", EXPORT)
    entries = dataflow.scan(tmp_path)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.kind == dataflow.KIND_TICK_EXPORT
    assert entry.can_convert is True
    assert entry.can_evaluate is False
    assert entry.records == 5
    assert entry.errors == 0


def test_a_canonical_file_is_evaluable_and_a_sample_is_never_evaluable(tmp_path):
    platform = build_fixture_platform(bar_count=40)
    try:
        write(tmp_path / "AAA_export.csv", canonical_text(platform))
        write(tmp_path / "SAMPLE_AAA.csv", canonical_text(platform))
    finally:
        platform.close()
    entries = {entry.name: entry for entry in dataflow.scan(tmp_path)}
    assert entries["AAA_export.csv"].kind == dataflow.KIND_CANONICAL
    assert entries["AAA_export.csv"].can_evaluate is True
    assert entries["SAMPLE_AAA.csv"].can_evaluate is False
    assert entries["SAMPLE_AAA.csv"].looks_like_sample is True


def test_an_unrelated_csv_is_neither_converted_nor_evaluated(tmp_path):
    write(tmp_path / "notes.csv", "hello,world\n1,2\n")
    entry = dataflow.scan(tmp_path)[0]
    assert entry.kind == dataflow.KIND_OTHER
    assert entry.can_convert is False
    assert entry.can_evaluate is False


def test_a_file_name_that_would_step_outside_the_folder_is_refused(tmp_path):
    (tmp_path / "inside.csv").write_text("x", encoding="utf-8")
    for name in ("../inside.csv", "sub/inside.csv", "/etc/passwd", "..", ""):
        with pytest.raises(dataflow.DataflowRefused):
            dataflow.resolve_file(tmp_path, name)


# ---------------------------------------------------------------------------
# 2. converting: the server clock basis is declared, never guessed
# ---------------------------------------------------------------------------


def test_conversion_requires_a_declared_time_basis(tmp_path):
    write(tmp_path / "ticks.csv", EXPORT)
    with pytest.raises(dataflow.DataflowRefused) as excinfo:
        dataflow.convert_tick_export(tmp_path, name="ticks.csv", pair="EUR/USD")
    assert excinfo.value.reason == "server_time_basis_missing"


def test_conversion_requires_a_pair(tmp_path):
    write(tmp_path / "ticks.csv", EXPORT)
    with pytest.raises(dataflow.DataflowRefused) as excinfo:
        dataflow.convert_tick_export(
            tmp_path, name="ticks.csv", pair="", server_offset_minutes=0
        )
    assert excinfo.value.reason == "pair_required"


def test_conversion_writes_a_canonical_file_and_names_the_gap(tmp_path):
    write(tmp_path / "ticks.csv", EXPORT)
    report = dataflow.convert_tick_export(
        tmp_path,
        name="ticks.csv",
        pair="EUR/USD",
        server_offset_minutes=0,
    )
    assert report["bars_written"] == 2
    assert report["coverage"]["empty_windows"] == 1
    assert report["coverage"]["complete"] is False
    written = tmp_path / report["output_file"]
    header = written.read_text(encoding="utf-8").splitlines()[0].split(",")
    assert tuple(header) == HEADER_COLUMNS
    sidecar = json.loads((tmp_path / report["provenance_path"]).read_text())
    assert sidecar["derivation"] == "DERIVED_FROM_BROKER_TICKS"
    assert sidecar["pair"] == "EUR/USD"
    assert sidecar["source_sha256"] == report["source_sha256"]


# ---------------------------------------------------------------------------
# 3. loading: the file must match this lane's instrument
# ---------------------------------------------------------------------------


def test_load_bars_reads_a_matching_file(tmp_path):
    platform = build_fixture_platform(bar_count=40)
    try:
        write(tmp_path / "AAA.csv", canonical_text(platform))
        bars, meta = dataflow.load_bars(
            tmp_path, name="AAA.csv", spec=platform.spec
        )
    finally:
        platform.close()
    assert len(bars) == meta["bars"] > 0
    assert meta["sha256"]
    assert meta["data_class"] == "USER_SUPPLIED_HISTORICAL"


class _SpecStub:
    canonical_pair = "EUR/USD"
    pair = "EUR/USD"


def test_load_bars_refuses_another_pair(tmp_path):
    other = build_fixture_platform(bar_count=40)
    try:
        write(tmp_path / "AAA.csv", canonical_text(other))
    finally:
        other.close()
    with pytest.raises(dataflow.DataflowRefused) as excinfo:
        dataflow.load_bars(tmp_path, name="AAA.csv", spec=_SpecStub())
    assert excinfo.value.reason == "dataset_pair_mismatch"


def test_load_bars_refuses_a_file_with_no_completed_bars(tmp_path):
    write(tmp_path / "quotes.csv", ",".join(HEADER_COLUMNS) + "\n")
    platform = build_fixture_platform(bar_count=20)
    try:
        with pytest.raises(dataflow.DataflowRefused) as excinfo:
            dataflow.load_bars(tmp_path, name="quotes.csv", spec=platform.spec)
    finally:
        platform.close()
    assert excinfo.value.reason in {"dataset_has_no_bars", "dataset_unparseable"}


# ---------------------------------------------------------------------------
# 4. evaluating: evidence is recorded, the locked window is spent once
# ---------------------------------------------------------------------------


def test_evaluation_records_evidence_for_every_candidate_and_spends_the_test(tmp_path):
    platform = build_fixture_platform(bar_count=200)
    try:
        bars = list(platform.bars)
        outcome = dataflow.evaluate_dataset(
            platform=platform,
            bars=bars,
            dataset_id="fixture-dataset",
            data_class="USER_SUPPLIED_DEMO",
        )
        assert len(outcome["results"]) == 3
        assert outcome["selected"] == []
        assert outcome["locked_test_spent"] is True
        assert evaluation.locked_test_consumed(
            repository=platform.repository, dataset_id="fixture-dataset"
        )
        with pytest.raises(dataflow.DataflowRefused) as excinfo:
            dataflow.evaluate_dataset(
                platform=platform,
                bars=bars,
                dataset_id="fixture-dataset",
                data_class="USER_SUPPLIED_DEMO",
            )
        assert excinfo.value.reason == "locked_test_already_consumed"
    finally:
        platform.close()


def test_a_demo_dataset_can_be_evaluated_but_never_selected():
    platform = build_fixture_platform(bar_count=200)
    try:
        outcome = dataflow.evaluate_dataset(
            platform=platform,
            bars=list(platform.bars),
            dataset_id="demo-only",
            data_class="USER_SUPPLIED_DEMO",
        )
    finally:
        platform.close()
    assert outcome["selected"] == []
    for row in outcome["results"]:
        assert row["status"] != "SELECTED_FOR_PAPER"


# ---------------------------------------------------------------------------
# 5. the page: forms are nonce-guarded and refuse to guess a data class
# ---------------------------------------------------------------------------


class _FakeBuilder:
    """A builder that hands out the fixture platform, terms and all."""

    def __init__(self, platform):
        self.platform = platform
        self.calls = 0

    def __call__(self, pair):
        self.calls += 1
        return self.platform, None


def data_app(tmp_path: Path, platform=None) -> WorkstationApp:
    builder = _FakeBuilder(platform)
    app = WorkstationApp(
        wizard=SetupWizard(environment=offline_environment(), session=simulated_wizard().session),
        platform=None,
        version="0.1.0",
        import_dir=tmp_path,
        research_db_path="",
        evaluation_platform_builder=builder,
    )
    app._builder = builder
    return app


def test_the_data_page_lists_the_folder_and_states_what_it_will_not_do(tmp_path):
    write(tmp_path / "ticks.csv", EXPORT)
    app = data_app(tmp_path)
    status, html = app.page("/data")
    assert int(status) == 200
    assert "ticks.csv" in html
    assert "broker tick export" in html
    assert "places no order" in html


def test_every_data_action_needs_the_nonce(tmp_path):
    app = data_app(tmp_path)
    for action in ("imports-refresh", "ticks-convert", "dataset-evaluate"):
        status, _, _ = app.act(action, {"file": ["ticks.csv"]})
        assert int(status) == 403


def test_the_convert_action_needs_a_time_basis_and_writes_nothing(tmp_path):
    write(tmp_path / "ticks.csv", EXPORT)
    app = data_app(tmp_path)
    status, redirect, message = app.act(
        "ticks-convert",
        {"nonce": [app.nonce], "file": ["ticks.csv"], "pair": ["EUR/USD"]},
    )
    assert int(status) == 303
    assert redirect == "/data"
    assert "server_time_basis_missing" in message
    assert not (tmp_path / "ticks_canonical.csv").exists()


def test_the_convert_action_converts_when_the_basis_is_declared(tmp_path):
    write(tmp_path / "ticks.csv", EXPORT)
    app = data_app(tmp_path)
    status, redirect, message = app.act(
        "ticks-convert",
        {
            "nonce": [app.nonce],
            "file": ["ticks.csv"],
            "pair": ["EUR/USD"],
            "server_offset_minutes": ["0"],
            "interval_seconds": ["60"],
        },
    )
    assert int(status) == 303
    assert redirect == "/data"
    assert "2 bids and asks" in message
    assert (tmp_path / "ticks_canonical.csv").exists()
    _, html = app.page("/data")
    assert "Conversion report" in html
    assert "Empty windows" in html


def test_the_evaluate_action_refuses_without_a_declared_data_class(tmp_path):
    platform = build_fixture_platform(bar_count=60)
    try:
        write(tmp_path / "AAA.csv", canonical_text(platform))
        app = data_app(tmp_path, platform)
        status, _, message = app.act(
            "dataset-evaluate",
            {
                "nonce": [app.nonce],
                "file": ["AAA.csv"],
                "pair": ["AAA/BBB"],
                "declare": ["yes"],
            },
        )
    finally:
        platform.close()
    assert int(status) == 303
    assert "data_class_required" in message


def test_the_evaluate_action_refuses_without_the_declaration(tmp_path):
    platform = build_fixture_platform(bar_count=60)
    try:
        write(tmp_path / "AAA.csv", canonical_text(platform))
        app = data_app(tmp_path, platform)
        status, _, message = app.act(
            "dataset-evaluate",
            {
                "nonce": [app.nonce],
                "file": ["AAA.csv"],
                "pair": ["AAA/BBB"],
                "data_class": ["USER_SUPPLIED_HISTORICAL"],
            },
        )
    finally:
        platform.close()
    assert int(status) == 303
    assert "declaration_required" in message


def test_the_evaluate_action_evaluates_and_records_on_the_platform_it_was_given(tmp_path):
    platform = build_fixture_platform(bar_count=200)
    try:
        write(tmp_path / "AAA.csv", canonical_text(platform))
        app = data_app(tmp_path, platform)
        status, redirect, message = app.act(
            "dataset-evaluate",
            {
                "nonce": [app.nonce],
                "file": ["AAA.csv"],
                "pair": ["AAA/BBB"],
                "data_class": ["USER_SUPPLIED_DEMO"],
                "declare": ["yes"],
            },
        )
        assert int(status) == 303
        assert redirect == "/data"
        assert "No candidate met the predeclared criteria" in message
        stored = evaluation.latest_evidence(
            repository=platform.repository, candidate_id="trend_pullback_v1_8_21"
        )
        assert stored is not None
        assert stored["data_class"] == "USER_SUPPLIED_DEMO"
        _, html = app.page("/data")
        assert "Evaluation report" in html
        assert "not a forecast" in html
    finally:
        platform.close()


def test_the_data_page_writes_no_file_when_it_only_renders(tmp_path):
    write(tmp_path / "ticks.csv", EXPORT)
    app = data_app(tmp_path)
    before = {path.name for path in tmp_path.iterdir()}
    app.page("/data")
    assert {path.name for path in tmp_path.iterdir()} == before


def test_no_evaluation_platform_means_the_page_says_so(tmp_path):
    app = WorkstationApp(
        wizard=SetupWizard(environment=offline_environment(), session=simulated_wizard().session),
        platform=None,
        version="0.1.0",
        import_dir=tmp_path,
    )
    status, html = app.page("/data")
    assert int(status) == 200
    assert "No import" in html or "Nothing to convert" in html
    status, _, message = app.act(
        "dataset-evaluate",
        {
            "nonce": [app.nonce],
            "file": ["x.csv"],
            "data_class": ["USER_SUPPLIED_DEMO"],
            "declare": ["yes"],
            "pair": ["AAA/BBB"],
        },
    )
    assert int(status) == 303
    assert "evaluation_not_configured" in message
