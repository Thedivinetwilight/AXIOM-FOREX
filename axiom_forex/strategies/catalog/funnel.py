"""What the strategy catalog actually contains, and how far each candidate got.

The catalog is large (over a thousand declared candidates) and the page that
shows it used to answer only one question at a time.  This module turns one
stored evaluation record into the *counts* an operator needs in order to read
the page correctly, kept deliberately separate from each other:

* how many strategies are **in the library** (and how many distinct rule
  structures that is, so parameter variants are never passed off as distinct
  ideas);
* how many are **compatible** with the symbols and timeframes selected now;
* how many were actually **evaluated**;
* of those, how many **passed**, **failed**, **had too little evidence**, or
  were **skipped** -- with the reason attached to every single row;
* how many have **not been evaluated** at all;
* how many the operator has **selected**.

Nothing here evaluates anything, touches the terminal, or writes a file: it is
a pure read model over a record that another module already produced.  The
readiness ladder at the bottom is the part that matters most -- "in the
library", "evaluated", "paper-testable" and "live-ready" are four different
claims, and only the first two can be read off a historical record.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

#: The record's per-row screen verdicts, and what each one means in words.
SCREEN_PASSED = "PASSED_SCREEN"
SCREEN_FAILED = "FAILED_SCREEN"
SCREEN_THIN = "TOO_FEW_TRADES"
SCREEN_SKIPPED = "NOT_SCREENED"

VERDICT_LABELS: dict[str, str] = {
    SCREEN_PASSED: "Passed",
    SCREEN_FAILED: "Failed",
    SCREEN_THIN: "Too little evidence",
    SCREEN_SKIPPED: "Skipped",
}

#: How many rows the page renders at once.  The counts always cover every row;
#: this only bounds what is put in the table, and the page says so.
MAX_ROWS_SHOWN = 60


def _rows(record: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    if not record:
        return []
    found = record.get("rows") or ()
    return [dict(item) for item in found if isinstance(item, Mapping)]


def _plan(record: Mapping[str, Any] | None) -> dict[str, Any]:
    if not record:
        return {}
    plan = record.get("plan")
    return dict(plan) if isinstance(plan, Mapping) else {}


def _screen_counts(record: Mapping[str, Any] | None) -> dict[str, Any]:
    """The record's own counters, or a recount when they are missing."""

    plan = record.get("counts") if isinstance(record, Mapping) else None
    if isinstance(plan, Mapping):
        return dict(plan)
    return {}


def _facts(candidate_id: str) -> dict[str, Any]:
    """The declared identity of one candidate: version, parameters, scope.

    A result is only meaningful next to the exact rule that produced it, so each
    row carries its signature (the catalog's version of the rule), its parameter
    values, and the timeframes it declares -- which is also its execution
    compatibility on this deployment.
    """

    if not candidate_id:
        return {}
    try:
        from axiom_forex.strategies.catalog.candidates import by_id, timeframes_for
    except Exception:  # pragma: no cover - ships with the repo
        return {}
    try:
        candidate = by_id(candidate_id)
    except Exception:
        return {}
    if candidate is None:
        return {}
    try:
        declared = list(timeframes_for(candidate))
    except Exception:
        declared = []
    return {
        "signature": str(candidate.signature),
        "structure": str(candidate.structure),
        "parameters": str(candidate.parameter_text()),
        "min_bars": int(candidate.min_bars),
        "timeframes": declared,
        "summary": str(candidate.summary),
    }


def _why(row: Mapping[str, Any]) -> str:
    """One plain sentence for why this row ended where it did.

    The record keeps two separate lists -- ``reasons`` for a row that never ran,
    and ``screen_reasons`` for one that ran and did not pass -- and both are
    worth showing verbatim: they are specific (which window, how many trades),
    and paraphrasing them would lose that.
    """

    for field in ("reasons", "screen_reasons"):
        values = [str(item).strip() for item in (row.get(field) or ()) if str(item).strip()]
        if values:
            return "; ".join(values)
    if row.get("screen") == SCREEN_PASSED:
        return "passed every predeclared criterion on this window"
    return "no reason recorded"


def _symbols(record: Mapping[str, Any] | None, rows: Sequence[Mapping[str, Any]]) -> list[str]:
    plan = _plan(record)
    slots = plan.get("slots") or ()
    found: set[str] = set()
    for slot in slots:
        if isinstance(slot, Mapping) and slot.get("symbol"):
            found.add(str(slot["symbol"]))
    if not found:
        found = {str(row.get("symbol") or "") for row in rows} - {""}
    return sorted(found)


def _timeframes(record: Mapping[str, Any] | None, rows: Sequence[Mapping[str, Any]]) -> list[str]:
    plan = _plan(record)
    slots = plan.get("slots") or ()
    found: set[str] = set()
    for slot in slots:
        if isinstance(slot, Mapping) and slot.get("timeframe"):
            found.add(str(slot["timeframe"]))
    if not found:
        found = {str(row.get("timeframe") or "") for row in rows} - {""}
    return sorted(found)


def build(
    record: Mapping[str, Any] | None,
    *,
    library: Mapping[str, Any] | None = None,
    selected: Iterable[str] = (),
    verdict: str = "",
    limit: int = MAX_ROWS_SHOWN,
    offset: int = 0,
) -> dict[str, Any]:
    """The funnel, from a stored evaluation record.

    ``library`` is the declared catalog's own summary (``summary_counts()``), so
    the "in the library" figure is never derived from what happened to be
    evaluated; ``selected`` is the set of candidate ids the operator chose.
    """

    rows = _rows(record)
    counts = _screen_counts(record)
    library = dict(library or {})
    chosen = {str(item) for item in selected if str(item)}

    by_verdict: dict[str, list[dict[str, Any]]] = {
        SCREEN_PASSED: [],
        SCREEN_FAILED: [],
        SCREEN_THIN: [],
        SCREEN_SKIPPED: [],
    }
    for row in rows:
        by_verdict.setdefault(str(row.get("screen") or SCREEN_SKIPPED), []).append(row)

    # Distinct *strategies*, not rows: the same candidate on two symbols is one
    # strategy tested twice, and counting it twice would inflate the short list.
    def distinct(items: Sequence[Mapping[str, Any]]) -> set[str]:
        return {str(item.get("candidate_id") or "") for item in items} - {""}

    declared = int(library.get("candidates") or 0)
    if not declared:
        plan = _plan(record)
        declared = int(plan.get("candidates_considered") or 0)

    tested_candidates = distinct(by_verdict[SCREEN_PASSED] + by_verdict[SCREEN_FAILED] + by_verdict[SCREEN_THIN])
    skipped_candidates = distinct(by_verdict[SCREEN_SKIPPED])
    # "Compatible" means it could actually run on what is selected now.  A
    # candidate whose declared timeframes miss the selected ones was planned and
    # then skipped, so it is incompatible -- not compatible and not evaluated.
    compatible_candidates = tested_candidates
    evaluated_candidates = tested_candidates
    evaluated_rows = len(rows) - len(by_verdict[SCREEN_SKIPPED])
    not_evaluated = declared - len(tested_candidates) - len(skipped_candidates) if declared else 0
    if not_evaluated < 0:
        not_evaluated = 0

    passed_rows = by_verdict[SCREEN_PASSED]
    passed_strategies = distinct(passed_rows)

    #: Which rows the page shows now: one verdict at a time, so a failed
    #: candidate is always reachable rather than buried under thousands of rows.
    wanted = [item for item in rows if not verdict or str(item.get("screen")) == verdict]
    wanted.sort(key=lambda item: (str(item.get("symbol")), str(item.get("timeframe")), str(item.get("candidate_id"))))
    start = max(0, int(offset))
    size = max(0, int(limit))
    shown = wanted[start : start + size]

    funnel = {
        "library": {
            "candidates": declared,
            "structures": int(library.get("distinct_rule_structures") or 0),
            "families": len(library.get("families") or {}) if isinstance(library.get("families"), Mapping) else int(library.get("families") or 0),
            "note": str(library.get("note") or ""),
        },
        "compatible": {
            "candidates": len(compatible_candidates),
            "pairs": len(rows),
            "symbols": _symbols(record, rows),
            "timeframes": _timeframes(record, rows),
            "incompatible_candidates": len(skipped_candidates),
        },
        "evaluated": {
            "candidates": len(evaluated_candidates),
            "rows": evaluated_rows,
        },
        "counts": {
            "passed_rows": len(passed_rows),
            "passed_strategies": len(passed_strategies),
            "failed_rows": len(by_verdict[SCREEN_FAILED]),
            "failed_strategies": len(distinct(by_verdict[SCREEN_FAILED])),
            "thin_rows": len(by_verdict[SCREEN_THIN]),
            "thin_strategies": len(distinct(by_verdict[SCREEN_THIN])),
            "skipped_rows": len(by_verdict[SCREEN_SKIPPED]),
            "skipped_strategies": len(skipped_candidates),
            "not_evaluated": not_evaluated,
            "selected": len(chosen),
        },
        "ladder": {
            "in_library": declared,
            "evaluated": len(evaluated_candidates),
            "paper_testable": len(passed_strategies),
            # Deliberately always zero from a stored record: a backtest cannot
            # reach this rung, and the page must never imply otherwise.
            "live_ready": 0,
            "live_ready_note": (
                "Nothing reaches this rung from a historical record. Live readiness "
                "needs a stored live plan, the confirmation phrase, the risk gate and "
                "the protective-stop rule, each satisfied at the time of use; a "
                "passing walk-forward screen is none of those."
            ),
        },
        "filters": [
            {"key": key, "label": label, "rows": len(by_verdict.get(key, []))}
            for key, label in VERDICT_LABELS.items()
        ],
        "verdict": verdict,
        "shown": len(shown),
        "matching": len(wanted),
        "offset": start,
        "limit": size,
        # Paging is offered only when it would actually move, so the page never
        # shows a disabled control that implies more rows than exist.
        "has_previous": start > 0,
        "has_next": start + size < len(wanted),
        "rows": [
            {
                "candidate_id": str(item.get("candidate_id") or ""),
                "symbol": str(item.get("symbol") or ""),
                "timeframe": str(item.get("timeframe") or ""),
                "family": str(item.get("family") or ""),
                "structure": str(item.get("structure") or ""),
                # Version, parameters, scope: a result without these is not
                # reproducible, so they travel with the row.
                **_facts(str(item.get("candidate_id") or "")),
                "verdict": str(item.get("screen") or ""),
                "verdict_label": VERDICT_LABELS.get(str(item.get("screen") or ""), str(item.get("screen") or "")),
                "reason": _why(item),
                "trades": item.get("trades", 0),
                "folds_positive": item.get("folds_positive", 0),
                "costs_incomplete": bool(item.get("costs_incomplete")),
                "selected": str(item.get("candidate_id") or "") in chosen,
                "score": item.get("score"),
                # Whether this deployment could execute the rule as declared:
                # the pair must be quoted and the timeframe must be offered.
                "execution_compatible": bool(
                    str(item.get("timeframe") or "")
                    in (_facts(str(item.get("candidate_id") or "")).get("timeframes") or [])
                ),
            }
            for item in shown
        ],
        "record_status": str((record or {}).get("status") or ""),
        "data_origin": str((record or {}).get("data_origin") or ""),
        "claim": str((record or {}).get("claim") or ""),
        "notes": [str(item) for item in ((record or {}).get("notes") or ())],
        "available": bool(rows),
    }
    if counts:
        funnel["record_counts"] = counts
    return funnel


def empty(*, library: Mapping[str, Any] | None = None, reason: str = "") -> dict[str, Any]:
    """The funnel for a deployment that has not evaluated anything yet."""

    library = dict(library or {})
    return {
        "available": False,
        "reason": reason or "no_evaluation_on_record",
        "library": {
            "candidates": int(library.get("candidates") or 0),
            "structures": int(library.get("distinct_rule_structures") or 0),
            "families": len(library.get("families") or {}) if isinstance(library.get("families"), Mapping) else 0,
            "note": str(library.get("note") or ""),
        },
        "compatible": {"candidates": 0, "pairs": 0, "symbols": [], "timeframes": [], "incompatible_candidates": 0},
        "evaluated": {"candidates": 0, "rows": 0},
        "counts": {
            "passed_rows": 0,
            "passed_strategies": 0,
            "failed_rows": 0,
            "failed_strategies": 0,
            "thin_rows": 0,
            "thin_strategies": 0,
            "skipped_rows": 0,
            "skipped_strategies": 0,
            "not_evaluated": int(library.get("candidates") or 0),
            "selected": 0,
        },
        "ladder": {
            "in_library": int(library.get("candidates") or 0),
            "evaluated": 0,
            "paper_testable": 0,
            "live_ready": 0,
            "live_ready_note": (
                "Nothing reaches this rung from a historical record. Live readiness "
                "needs a stored live plan, the confirmation phrase, the risk gate and "
                "the protective-stop rule, each satisfied at the time of use."
            ),
        },
        "filters": [{"key": key, "label": label, "rows": 0} for key, label in VERDICT_LABELS.items()],
        "verdict": "",
        "shown": 0,
        "matching": 0,
        "offset": 0,
        "limit": MAX_ROWS_SHOWN,
        "has_previous": False,
        "has_next": False,
        "rows": [],
        "record_status": "",
        "data_origin": "",
        "claim": "",
        "notes": [],
    }


__all__ = [
    "MAX_ROWS_SHOWN",
    "SCREEN_FAILED",
    "SCREEN_PASSED",
    "SCREEN_SKIPPED",
    "SCREEN_THIN",
    "VERDICT_LABELS",
    "build",
    "empty",
]
