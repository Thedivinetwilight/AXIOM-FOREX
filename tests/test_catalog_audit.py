"""The full-catalog audit: every declared candidate accounted for, with a reason.

The audit reads one stored evaluation record and writes one report.  These tests
assert the properties that make it worth having:

* every candidate the build declares appears exactly once -- nothing is dropped
  because it was never reached;
* each one is TESTED, SKIPPED, FAILED or NOT_CONSIDERED, and a skipped or failed
  candidate carries the reason the record gave;
* a row in the record for a strategy this build does not declare is listed too,
  rather than silently ignored;
* the report repeats, in its own words, that a historical evaluation is not a
  forecast and does not show that anything will work live;
* nothing here can evaluate, re-rank, tune on the locked window, promote a
  candidate, arm trading, or send an order.

The page test drives the real workstation action with a stub record: it writes
the report file, shows the counts, and sends nothing.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from axiom_forex.strategies.catalog import audit
from axiom_forex.strategies.catalog.candidates import catalog
from axiom_forex.workstation import views
from tests.test_catalog_view import _app, _page

UTC = timezone.utc
NOW = datetime(2026, 11, 2, 9, 20, tzinfo=UTC)


def _payload(*rows: dict) -> dict:
    return {
        "status": "COMPLETE",
        "counts": {"skip_reasons": {"not_reached_within_budget": 1}},
        "elapsed_seconds": 12.5,
        "budget_seconds": 420,
        "started_at": "2026-11-02T09:00:00+00:00",
        "finished_at": "2026-11-02T09:00:12+00:00",
        "plan": {"lot": "0.10"},
        "paper_set": [],
        "rows": list(rows),
    }


def _row(candidate_id: str, status: str, **overrides) -> dict:
    payload = {
        "candidate_id": candidate_id,
        "family": "trend",
        "structure": "sma_cross",
        "symbol": "EUR/USD",
        "timeframe": "1h",
        "status": status,
        "reasons": [],
        "trades": 0,
        "screen": "NOT_SCREENED",
        "score": None,
        "folds_positive": 0,
        "costs_incomplete": False,
    }
    payload.update(overrides)
    return payload


def _by_id(document: dict) -> dict[str, dict]:
    return {item["candidate_id"]: item for item in document["candidates"]}


# -- the report accounts for the whole declared catalog ----------------------


def test_a_build_with_no_record_still_lists_every_candidate() -> None:
    document = audit.build_audit(None, now=NOW)

    declared = {item.candidate_id for item in catalog()}
    listed = {item["candidate_id"] for item in document["candidates"]}
    assert listed == declared
    assert document["summary"]["declared"] == len(declared)
    assert document["summary"]["tested"] == 0
    assert document["summary"]["not_considered"] == len(declared)
    # An absent measurement is not a pass, and the report says which it is.
    assert all(item["status"] == audit.NOT_CONSIDERED for item in document["candidates"])
    assert all(
        item["reasons"] == [audit.REASON_NO_RECORD] for item in document["candidates"]
    )
    assert "not a pass" in " ".join(document["notes"])


def test_every_status_carries_the_records_own_reason() -> None:
    declared = [item.candidate_id for item in catalog()][:3]
    tested, skipped, failed = declared
    payload = _payload(
        _row(tested, "TESTED", screen="PASSED_SCREEN", trades=12, score=1.25, folds_positive=2),
        _row(skipped, "SKIPPED", reasons=["not_reached_within_budget"]),
        _row(failed, "FAILED", reasons=["no_usable_cost_model"], detail_class="KeyError"),
    )

    document = audit.build_audit(payload, now=NOW)
    entries = _by_id(document)

    assert entries[tested]["status"] == audit.TESTED
    assert entries[tested]["tested_cells"] == 1
    assert entries[tested]["cells"][0]["trades"] == 12
    assert entries[skipped]["status"] == audit.SKIPPED
    assert entries[skipped]["reasons"] == ["not_reached_within_budget"]
    assert entries[failed]["status"] == audit.FAILED
    assert "no_usable_cost_model" in entries[failed]["reasons"]
    assert "KeyError" in entries[failed]["reasons"]
    assert document["summary"]["tested"] == 1
    assert document["summary"]["skipped"] == 1
    assert document["summary"]["failed"] == 1


def test_a_candidate_the_run_never_planned_is_listed_as_not_considered() -> None:
    declared = [item.candidate_id for item in catalog()]
    payload = _payload(_row(declared[0], "TESTED"))

    document = audit.build_audit(payload, now=NOW)
    entries = _by_id(document)

    assert entries[declared[0]]["status"] == audit.TESTED
    assert entries[declared[1]]["status"] == audit.NOT_CONSIDERED
    assert entries[declared[1]]["reasons"] == [audit.REASON_NEVER_PLANNED]
    assert document["summary"]["not_considered"] == len(declared) - 1


def test_a_record_mentioning_an_undeclared_candidate_is_not_dropped() -> None:
    payload = _payload(_row("cat-not-in-this-build", "TESTED"))

    document = audit.build_audit(payload, now=NOW)

    assert document["summary"]["undeclared_in_record"] == 1
    assert "cat-not-in-this-build" in _by_id(document)
    assert any("not in this build" in note for note in document["notes"])


def test_mixed_cells_for_one_candidate_report_the_strongest_fact() -> None:
    declared = [item.candidate_id for item in catalog()][:1]
    name = declared[0]
    payload = _payload(
        _row(name, "SKIPPED", reasons=["history_shorter_than_warmup"]),
        _row(name, "TESTED", symbol="GBP/USD", trades=4),
    )

    document = audit.build_audit(payload, now=NOW)
    entry = _by_id(document)[name]

    # One measured row is a measurement, not a skip; both cells stay visible.
    assert entry["status"] == audit.TESTED
    assert entry["tested_cells"] == 1
    assert entry["skipped_cells"] == 1


# -- what the report must say about itself ----------------------------------


def test_the_report_repeats_the_limits_of_a_historical_evaluation() -> None:
    document = audit.build_audit(_payload(), now=NOW)

    claim = document["claim"].lower()
    assert "not a forecast" in claim
    assert "does not show that any candidate will do well on a live account" in claim
    assert "claimed to be profitable" in claim
    assert "never fitted" in document["tuning_note"]
    assert audit.CLAIM in audit.lines(document)


def test_the_lines_rendering_names_the_counts_and_stops_at_the_limit() -> None:
    document = audit.build_audit(_payload(), now=NOW)

    rendered = audit.lines(document, limit=2)

    assert rendered[0].startswith("declared ")
    # head, claim, two candidate lines, and a count of the ones not shown
    assert len(rendered) == 5
    assert "more candidates in the record" in rendered[-1]


# -- the file ---------------------------------------------------------------


def test_the_report_round_trips_and_summarises(tmp_path: Path) -> None:
    document = audit.build_audit(_payload(), now=NOW)
    path = audit.write_audit(document, tmp_path / "catalog" / audit.AUDIT_RECORD)

    assert path.exists()
    stored = audit.read_audit(path)
    assert stored is not None
    assert stored["schema"] == audit.SCHEMA
    assert json.loads(path.read_text(encoding="utf-8"))["summary"]["declared"] == (
        document["summary"]["declared"]
    )
    summary = audit.summarise(stored)
    assert summary["present"] is True
    assert summary["declared"] == document["summary"]["declared"]
    assert summary["claim"] == audit.CLAIM


def test_a_missing_or_broken_report_reads_as_none(tmp_path: Path) -> None:
    assert audit.read_audit(tmp_path / "absent.json") is None
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert audit.read_audit(broken) is None
    assert audit.summarise(None) == {"present": False}


# -- the workstation action -------------------------------------------------


class _StubStore:
    def __init__(self, path: Path) -> None:
        self.path = path


class _StubRecord:
    def __init__(self, payload: dict) -> None:
        self.payload = payload


class _StubCatalog:
    """Just enough of the catalog panel: one stored record and where it lives."""

    def __init__(self, root: Path, payload: dict | None) -> None:
        self.store = _StubStore(root / "catalog-evaluation.json")
        self._payload = payload

    def _record(self):
        return None if self._payload is None else _StubRecord(self._payload)


def _declared_payload() -> dict:
    ids = [item.candidate_id for item in catalog()][:2]
    return _payload(
        _row(ids[0], "TESTED", trades=9),
        _row(ids[1], "SKIPPED", reasons=["not_reached_within_budget"]),
    )


def test_the_audit_action_writes_the_report_and_says_what_it_did(tmp_path: Path) -> None:
    app = _app(_StubCatalog(tmp_path / "catalog", _declared_payload()))
    try:
        status, redirect, message = app.act("catalog-audit", {"nonce": [app.nonce]})

        assert int(status) == 303
        assert redirect == "/quick-setup"
        assert "Audited" in message
        assert "tested" in message and "skipped" in message
        assert "not a forecast" in message
        written = app.catalog_audit
        assert written["summary"]["declared"] == len(catalog())
        assert written["summary"]["tested"] >= 1
        assert (tmp_path / "catalog" / audit.AUDIT_RECORD).exists()
    finally:
        pass


def test_the_audit_action_without_a_record_still_reports_every_candidate(
    tmp_path: Path,
) -> None:
    app = _app(_StubCatalog(tmp_path / "catalog", None))
    status, _redirect, message = app.act("catalog-audit", {"nonce": [app.nonce]})

    assert int(status) == 303
    assert "not considered" in message
    assert app.catalog_audit["summary"]["not_considered"] == len(catalog())


def test_the_page_shows_the_audit_counts_and_the_disclaimer(tmp_path: Path) -> None:
    from tests.test_catalog_view import _panel

    app = _app(_panel(tmp_path))
    app.catalog_audit = audit.build_audit(_declared_payload(), now=NOW)

    html = _page(app)

    assert "Full catalog audit" in html
    assert "Every candidate, with its status and reasons" in html
    assert "not a forecast" in html
    assert "Re-run the audit" in html


def test_no_audit_button_click_can_reach_an_order_path() -> None:
    """The action is a report writer: no adapter, no plan, no arming, no send."""

    import inspect

    source = inspect.getsource(audit)
    for forbidden in ("order_send", "submit_plan", "arm_trading", "route_live", "start_session"):
        assert forbidden not in source
    # And the audit module holds no live workflow object at all.
    assert "live_workflow" not in source
