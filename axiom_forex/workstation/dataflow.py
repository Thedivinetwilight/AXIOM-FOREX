"""The workstation's data workflow: scan, convert, load, evaluate — all local.

Three steps an operator actually performs, in order, with nothing invented in
between:

1. **Scan** the import folder and say what each file is.  A MetaTrader 5 tick
   export, a canonical CSV, or a file that is neither — plus its records, pairs,
   span, errors, and any conversion sidecar's provenance.
2. **Convert** one broker tick export into the canonical bid/ask CSV, using the
   operator's declared server-time basis and bar interval.  Gaps, empty windows,
   and the trailing partial window are reported; nothing is filled in.
3. **Load & evaluate** one canonical file against the *same* terms the paper lane
   uses (spec, calendar, costs, swap, risk limits, conversion basis), and record
   the evidence for each candidate in the runner's own database.  The locked test
   window is spent once per dataset and that is written down.

What this module never does
---------------------------
* it never downloads, fabricates, interpolates, or relabels market data;
* it never chooses a pair, a data class, a time basis, or a timeframe for you;
* it never places an order, arms anything, or changes the execution mode;
* it never touches the running runner's objects — every call opens its own
  short-lived repository connection and closes it again;
* it does not report a candidate as selected unless the documented criteria
  passed on data the operator supplied as historical.

Everything here is called from the workstation's request thread, one action at a
time, under the app's own lock.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from app.data.models import ForexQuoteBar, QuoteSideBar

from axiom_forex.ingest.canonical_csv import parse_canonical_csv
from axiom_forex.ingest.contracts import BAR_RECORD, HEADER_COLUMNS
from axiom_forex.paper_runner import data_audit

#: File kinds the scan reports.
KIND_TICK_EXPORT = "MT5_TICK_EXPORT"
KIND_CANONICAL = "CANONICAL_CSV"
KIND_OTHER = "OTHER"

#: Data classes that may ever promote a candidate.  Declared by the operator,
#: never inferred from a file name -- imported from the evaluator itself so the
#: form and the evaluation can never disagree.
from axiom_forex.paper_runner.strategy import (  # noqa: E402  (after app import)
    PROMOTING_DATA_CLASSES as STRATEGY_PROMOTING_DATA_CLASSES,
)

PROMOTING_DATA_CLASSES = tuple(STRATEGY_PROMOTING_DATA_CLASSES)

MAX_EVALUATION_BARS = 200_000


class DataflowRefused(Exception):
    """The requested step cannot be taken.  Nothing was written or evaluated."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason
        self.detail = detail

    def audit_view(self) -> dict[str, Any]:
        return {"reason": self.reason, "detail": self.detail}


@dataclass(frozen=True)
class FileEntry:
    """One file in the import folder, described rather than interpreted."""

    name: str
    kind: str
    size_bytes: int
    records: int
    bars: int
    quotes: int
    pairs: tuple[str, ...]
    timeframes: tuple[str, ...]
    first_at: str
    last_at: str
    header_ok: bool
    errors: int
    warnings: int
    looks_like_sample: bool
    provenance: Mapping[str, Any]
    can_convert: bool = False
    can_evaluate: bool = False
    note: str = ""

    def audit_view(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "size_bytes": self.size_bytes,
            "records": self.records,
            "bars": self.bars,
            "quotes": self.quotes,
            "pairs": list(self.pairs),
            "timeframes": list(self.timeframes),
            "first_at": self.first_at,
            "last_at": self.last_at,
            "header_ok": self.header_ok,
            "errors": self.errors,
            "warnings": self.warnings,
            "looks_like_sample": self.looks_like_sample,
            "provenance": dict(self.provenance or {}),
            "can_convert": self.can_convert,
            "can_evaluate": self.can_evaluate,
            "note": self.note,
        }


# ---------------------------------------------------------------------------
# locating files: a name from a scanned list, never a path from a form
# ---------------------------------------------------------------------------


def resolve_file(directory: Path | str, name: str) -> Path:
    """Turn a scanned *file name* into a path inside ``directory``.

    The form carries a name from the list the page rendered.  Anything that could
    step outside the folder — a separator, ``..``, an absolute path, a symlink
    target outside — is refused rather than normalised.
    """

    root = Path(directory).resolve()
    raw = str(name or "").strip()
    if not raw:
        raise DataflowRefused("file_not_selected", "choose a file from the list")
    if raw != Path(raw).name or raw in {".", ".."}:
        raise DataflowRefused("file_name_invalid", "a file name only, not a path")
    candidate = (root / raw).resolve()
    if root != candidate.parent:
        raise DataflowRefused("file_outside_import_folder", str(candidate.name))
    if not candidate.is_file():
        raise DataflowRefused("file_missing", raw)
    return candidate


def _looks_like_tick_export(header: Sequence[str]) -> bool:
    names = {str(cell).strip().strip("<>").lower() for cell in header}
    return {"bid", "ask"} <= names and "record_type" not in names


def scan(directory: Path | str, *, limit: int = 60) -> tuple[FileEntry, ...]:
    """Describe every file in the folder.  Reads only; never converts."""

    root = Path(directory)
    if not root.exists():
        return ()
    entries: list[FileEntry] = []
    for path in sorted(root.iterdir())[:limit]:
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix == ".json":
            continue
        if suffix not in {".csv", ".txt", ".tsv"}:
            continue
        entries.append(_describe(path))
    return tuple(entries)


def _first_data_line(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return ""


def _describe(path: Path) -> FileEntry:
    from axiom_forex.ingest.mt5_ticks import TickExportRefused, load_provenance, read_ticks

    size = path.stat().st_size if path.exists() else 0
    first_line = _first_data_line(path)
    header = [
        cell.strip().strip('"') for cell in first_line.replace("	", ",").split(",")
    ]
    looks_sample = path.name.upper().startswith("SAMPLE")
    if _looks_like_tick_export(header):
        try:
            export = read_ticks(path)
        except TickExportRefused as exc:
            return FileEntry(
                name=path.name,
                kind=KIND_TICK_EXPORT,
                size_bytes=size,
                records=0,
                bars=0,
                quotes=0,
                pairs=(),
                timeframes=(),
                first_at="",
                last_at="",
                header_ok=False,
                errors=1,
                warnings=0,
                looks_like_sample=looks_sample,
                provenance={},
                note=f"tick export refused: {exc.reason} ({exc.detail})",
            )
        first_at = export.first_at.isoformat() if export.first_at else ""
        last_at = export.last_at.isoformat() if export.last_at else ""
        return FileEntry(
            name=path.name,
            kind=KIND_TICK_EXPORT,
            size_bytes=size,
            records=export.count,
            bars=0,
            quotes=export.count,
            pairs=(),
            timeframes=(),
            first_at=first_at,
            last_at=last_at,
            header_ok=True,
            errors=len(export.rejected),
            warnings=export.duplicate_count,
            looks_like_sample=looks_sample,
            provenance=(
                load_provenance(sidecar_path)
                if (sidecar_path := path.with_suffix(f"{path.suffix}.provenance.json")).exists()
                else {}
            ),
            can_convert=True,
            note=(
                "broker tick export: convert it with the pair and the server's time "
                "basis, then evaluate the canonical CSV it writes"
            ),
        )

    try:
        finding = data_audit.inspect_file(path)
    except Exception as exc:  # an unreadable file is a finding, not a crash
        return FileEntry(
            name=path.name,
            kind=KIND_OTHER,
            size_bytes=size,
            records=0,
            bars=0,
            quotes=0,
            pairs=(),
            timeframes=(),
            first_at="",
            last_at="",
            header_ok=False,
            errors=1,
            warnings=0,
            looks_like_sample=True,
            provenance={},
            note=f"unreadable: {type(exc).__name__}",
        )
    canonical = tuple(cell.strip().strip('"') for cell in header) == HEADER_COLUMNS
    kind = KIND_CANONICAL if canonical else KIND_OTHER
    provenance = dict(getattr(finding, "provenance", {}) or {})
    return FileEntry(
        name=path.name,
        kind=kind,
        size_bytes=size,
        records=finding.records,
        bars=finding.bars,
        quotes=finding.quotes,
        pairs=tuple(finding.pairs),
        timeframes=tuple(finding.timeframes),
        first_at=finding.first_at,
        last_at=finding.last_at,
        header_ok=finding.header_ok,
        errors=finding.errors,
        warnings=finding.warnings,
        looks_like_sample=looks_sample or bool(finding.looks_like_sample),
        provenance=provenance,
        can_evaluate=bool(
            canonical
            and finding.header_ok
            and finding.errors == 0
            and finding.bars > 0
            and not (looks_sample or finding.looks_like_sample)
        ),
        note="" if canonical else "not an MT5 tick export and not a canonical CSV",
    )


# ---------------------------------------------------------------------------
# convert one broker tick export
# ---------------------------------------------------------------------------


def convert_tick_export(
    directory: Path | str,
    *,
    name: str,
    pair: str,
    server_offset_minutes: int | None = None,
    server_timezone: str = "",
    interval_seconds: int = 60,
) -> dict[str, Any]:
    """Convert one scanned tick export, reporting coverage rather than filling it.

    The server-time basis is required: there is no default.  The result names the
    canonical file it wrote and the sidecar beside it.
    """

    from axiom_forex.ingest.mt5_ticks import TickExportRefused, convert

    path = resolve_file(directory, name)
    canonical_pair = str(pair or "").strip().upper()
    if not canonical_pair:
        raise DataflowRefused("pair_required", "declare which pair the export is for")
    try:
        offset = None if server_offset_minutes in (None, "") else int(server_offset_minutes)
    except (TypeError, ValueError) as exc:
        raise DataflowRefused("server_offset_invalid", str(server_offset_minutes)) from exc
    if offset is None and not str(server_timezone or "").strip():
        raise DataflowRefused(
            "server_time_basis_missing",
            "declare the broker server's UTC offset (or its IANA zone); there is no default",
        )
    try:
        report = convert(
            source=path,
            pair=canonical_pair,
            server_offset_minutes=offset,
            server_timezone=str(server_timezone or "").strip(),
            interval_seconds=max(int(interval_seconds or 60), 1),
        )
    except TickExportRefused as exc:
        raise DataflowRefused(exc.reason, exc.detail) from exc
    written = Path(report["output"])
    return {
        "converted": True,
        "source_file": path.name,
        "output_file": written.name,
        "bars_written": report["bars_written"],
        "tick_count": report["tick_count"],
        "duplicates": report.get("duplicate_ticks", 0),
        "rejected_rows": report.get("rejected_rows", []),
        "coverage": report.get("coverage", {}),
        "provenance_path": Path(report["provenance_path"]).name,
        "source_sha256": report.get("source_sha256", ""),
        "server_time_basis": report.get("server_time_basis", ""),
        "time_basis_detail": report.get("time_basis_detail", ""),
        "notes": list(report.get("notes", [])),
    }


# ---------------------------------------------------------------------------
# load one canonical file as bars, against one instrument's terms
# ---------------------------------------------------------------------------


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise DataflowRefused("bar_price_invalid", str(value)) from exc


def load_bars(
    directory: Path | str,
    *,
    name: str,
    spec: Any,
    limit: int = MAX_EVALUATION_BARS,
) -> tuple[list[ForexQuoteBar], dict[str, Any]]:
    """Parse a canonical CSV into bars for ``spec``, refusing anything doubtful.

    The file is the operator's declaration.  This function checks it against the
    instrument's own terms (pair, tick size, completeness) and refuses the whole
    file when any record fails: an evaluation must never quietly skip a bad bar.
    """

    path = resolve_file(directory, name)
    text = path.read_text(encoding="utf-8", errors="strict")
    parsed = parse_canonical_csv(text, source_label=f"workstation:{path.name}")
    if parsed.fatal:
        raise DataflowRefused(
            "dataset_unparseable",
            "the file has no usable header, or a header with no records under it",
        )
    if parsed.error_count():
        first = next(
            (issue for issue in parsed.issues if issue.severity == "ERROR"), None
        )
        raise DataflowRefused(
            "dataset_has_errors",
            f"{parsed.error_count()} error(s); first: {getattr(first, 'code', '')} "
            f"on line {getattr(first, 'line', 0)}",
        )
    records = [record for record in parsed.records if record.record_type == BAR_RECORD]
    if not records:
        raise DataflowRefused(
            "dataset_has_no_bars",
            "only completed bars can be evaluated; quotes alone cannot",
        )
    if len(records) > limit:
        raise DataflowRefused(
            "dataset_too_large", f"{len(records)} bars exceeds the {limit} limit"
        )

    canonical_pair = str(
        getattr(spec, "canonical_pair", "") or getattr(spec, "pair", "")
    ).upper()
    bars: list[ForexQuoteBar] = []
    for record in records:
        payload = dict(record.payload or {})
        if str(record.pair).upper() != canonical_pair:
            raise DataflowRefused(
                "dataset_pair_mismatch",
                f"the file is {record.pair}, this lane is {canonical_pair}",
            )
        side = {}
        for name_ in ("bid", "ask"):
            values = payload.get(name_) or {}
            side[name_] = QuoteSideBar(
                open=_decimal(values.get("open")),
                high=_decimal(values.get("high")),
                low=_decimal(values.get("low")),
                close=_decimal(values.get("close")),
            )
        bar = ForexQuoteBar(
            pair=record.pair,
            timeframe=str(payload.get("timeframe", "")),
            timestamp=record.timestamp,
            duration_seconds=int(payload.get("duration_seconds", 0)),
            bid=side["bid"],
            ask=side["ask"],
            source=f"USER_IMPORT:{path.name}",
            quote_count=int(payload.get("quote_count", 0)),
            last_quote_at=datetime.fromisoformat(str(payload.get("last_quote_at"))),
            complete=bool(payload.get("complete", False)),
        )
        validation = bar.validate(spec)
        if not validation.valid:
            raise DataflowRefused(
                "dataset_bar_invalid",
                f"{bar.timestamp.isoformat()}: {validation.reason}",
            )
        bars.append(bar)

    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    meta = {
        "file": path.name,
        "sha256": digest,
        "bars": len(bars),
        "pair": canonical_pair,
        "first_at": bars[0].timestamp.isoformat(),
        "last_at": bars[-1].timestamp.isoformat(),
        "timeframe": bars[0].timeframe,
        "data_class": "USER_SUPPLIED_HISTORICAL",
    }
    return bars, meta


# ---------------------------------------------------------------------------
# evaluate the candidate grid on one dataset, and record the evidence
# ---------------------------------------------------------------------------


def evaluate_dataset(
    *,
    platform: Any,
    bars: Sequence[ForexQuoteBar],
    dataset_id: str,
    data_class: str,
    candidates: Iterable[Any] | None = None,
    criteria: Any = None,
) -> dict[str, Any]:
    """Run the documented chronological evaluation and record what it measured.

    The locked test window is spent once per ``dataset_id``; a second attempt on
    the same dataset is refused, because re-running a spent window turns the test
    set into a tuning set.
    """

    from axiom_forex.paper_runner import evaluation
    from axiom_forex.paper_runner.strategy import candidate_family, status_of

    if not bars:
        raise DataflowRefused("dataset_has_no_bars", "")
    active_criteria = criteria or evaluation.DEFAULT_CRITERIA
    if evaluation.locked_test_consumed(
        repository=platform.repository, dataset_id=dataset_id
    ):
        raise DataflowRefused(
            "locked_test_already_consumed",
            "this dataset's locked test window has already been spent; export a "
            "fresh range instead of re-running the same one",
        )
    family = list(candidates if candidates is not None else candidate_family())
    results: list[dict[str, Any]] = []
    spent = False
    for item in family:
        outcome = evaluation.evaluate_candidate(
            engine_factory=platform.new_engine,
            candidate=item,
            bars=list(bars),
            data_class=data_class,
            dataset_id=dataset_id,
            criteria=active_criteria,
        )
        evaluation.record_evaluation(
            repository=platform.repository, payload=outcome.payload
        )
        spent = spent or bool(outcome.payload.get("locked_test_consumed"))
        results.append(
            {
                "candidate_id": item.candidate_id,
                "status": status_of(item.candidate_id, outcome.payload),
                "passed": bool(outcome.payload.get("passed")),
                "failed_criteria": list(outcome.payload.get("failed_criteria", [])),
                "ineligible_reason": outcome.payload.get("ineligible_reason", ""),
                "windows": outcome.payload.get("windows", {}),
                "metrics": outcome.payload.get("metrics", {}),
                "aggregate": outcome.payload.get("aggregate", {}),
                "walk_forward": outcome.payload.get("walk_forward_summary", {}),
                "baselines": outcome.payload.get("baselines", {}),
            }
        )
    if spent:
        evaluation.claim_locked_test(
            repository=platform.repository,
            dataset_id=dataset_id,
            at=datetime.now(timezone.utc).isoformat(),
        )
    return {
        "dataset_id": dataset_id,
        "data_class": str(data_class).upper(),
        "bars": len(bars),
        "criteria": active_criteria.audit_view(),
        "assumptions": list(evaluation.ASSUMPTIONS),
        "locked_test_spent": spent,
        "results": results,
        "selected": [
            item["candidate_id"] for item in results if item["status"] == "SELECTED_FOR_PAPER"
        ],
        "note": (
            "Historical evidence on the sample you supplied, after the declared "
            "costs. It is not a forecast, and nothing was sent to a broker."
        ),
    }


def dataset_id_for(meta: Mapping[str, Any]) -> str:
    """A stable identity for one canonical file's contents."""

    digest = str(meta.get("sha256", ""))[:12]
    return f"{meta.get('file', 'dataset')}:{digest}"


__all__ = [
    "DataflowRefused",
    "FileEntry",
    "KIND_CANONICAL",
    "KIND_OTHER",
    "KIND_TICK_EXPORT",
    "PROMOTING_DATA_CLASSES",
    "convert_tick_export",
    "dataset_id_for",
    "evaluate_dataset",
    "load_bars",
    "resolve_file",
    "scan",
]
