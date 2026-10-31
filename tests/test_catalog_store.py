"""The stored evaluation: what a paper test is allowed to start from."""

from __future__ import annotations

import json

from axiom_forex.strategies.catalog.evaluate import EvaluationPlan, evaluate
from axiom_forex.strategies.catalog.store import (
    NO_PAPER_SET,
    NO_RECORD,
    PARTIAL_RECORD,
    RECORD_VERSION,
    STALE_RECORD,
    VERSION_MISMATCH,
    CatalogStore,
    StoredEvaluation,
)
from tests.catalog_support import SAMPLE_COST, series_from_closes, trending
from axiom_forex.strategies.catalog.candidates import catalog
from axiom_forex.strategies.catalog.evaluate import SeriesSlot


def _report():
    slot = SeriesSlot(
        symbol="EURUSD.pro",
        timeframe="15m",
        series=series_from_closes(trending(900)),
        cost=SAMPLE_COST,
        origin="MT5_TERMINAL_READ_ONLY",
    )
    return evaluate(
        EvaluationPlan(slots=(slot,), lot=0.1, budget_seconds=600),
        candidates=catalog()[:16],
    )


def test_an_empty_directory_has_no_record_and_says_so(tmp_path):
    store = CatalogStore(tmp_path / "catalog")
    assert store.load() is None
    blockers = store.blockers()
    assert blockers and blockers[0]["reason"] == NO_RECORD


def test_a_saved_report_round_trips_without_secrets(tmp_path):
    store = CatalogStore(tmp_path / "catalog")
    path = store.save(_report())
    assert path.exists()
    record = store.load()
    assert record is not None
    assert record.status == "COMPLETE"
    assert record.data_origin == "MT5_TERMINAL_READ_ONLY"
    text = path.read_text(encoding="utf-8")
    for forbidden in ("password", "secret", "token", "credential"):
        assert forbidden not in text.lower()
    assert json.loads(text)["record_version"] == RECORD_VERSION


def test_a_record_that_did_not_finish_cannot_start_a_paper_test(tmp_path):
    payload = {
        "record_version": RECORD_VERSION,
        "status": "CANCELLED_PARTIAL",
        "paper_set": [],
        "written_at": "2026-11-01T00:00:00+00:00",
    }
    path = tmp_path / "catalog-evaluation.json"
    tmp_path.mkdir(exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    record = StoredEvaluation(path=path, payload=payload)
    reasons = {blocker["reason"] for blocker in record.blockers()}
    assert PARTIAL_RECORD in reasons
    assert NO_PAPER_SET in reasons


def test_a_stale_record_is_refused_when_an_age_limit_is_given(tmp_path):
    payload = {
        "record_version": RECORD_VERSION,
        "status": "COMPLETE",
        "paper_set": [{"candidate_id": "cat-x", "symbol": "EURUSD.pro", "timeframe": "15m"}],
        "written_at": "2020-01-01T00:00:00+00:00",
    }
    record = StoredEvaluation(path=tmp_path / "x.json", payload=payload)
    reasons = {blocker["reason"] for blocker in record.blockers(max_age_seconds=3600)}
    assert STALE_RECORD in reasons


def test_a_record_from_another_version_is_refused(tmp_path):
    payload = {"record_version": 99, "status": "COMPLETE", "paper_set": [{"candidate_id": "x"}]}
    record = StoredEvaluation(path=tmp_path / "x.json", payload=payload)
    reasons = {blocker["reason"] for blocker in record.blockers()}
    assert VERSION_MISMATCH in reasons


def test_saving_twice_replaces_the_file_in_one_step(tmp_path):
    store = CatalogStore(tmp_path / "catalog")
    first = store.save(_report())
    second = store.save(_report())
    assert first == second
    assert not list((tmp_path / "catalog").glob("*.tmp"))
    assert store.load() is not None
