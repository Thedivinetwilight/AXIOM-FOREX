"""What historical data exists here, where it came from, and what it is missing.

This module *reads*.  It never downloads, guesses, or manufactures a bar.  It
answers three questions the runner's preconditions depend on:

* what files are sitting in the user data directory, and do they match the
  canonical format;
* what has actually been imported into this paper database, with which declared
  provenance, data class, and validity bounds;
* what coverage and data-quality problems the imported data has (span, gaps,
  duplicates, crossed quotes, incomplete bars), so nothing gets evaluated -- let
  alone promoted to paper -- on a sample nobody has looked at.

The result is deliberately blunt about the common case: this repository ships a
format *example* (`imports/SAMPLE_canonical_quote_bars.csv`), not market data.
With nothing imported, the audit says so and no candidate can be selected.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from axiom_forex.ingest.canonical_csv import parse_canonical_csv
from axiom_forex.ingest.contracts import (
    BAR_RECORD,
    IMPORT_FORMAT,
    IMPORT_SCHEMA_VERSION,
    QUOTE_RECORD,
    SUPPORTED_IMPORT_SCHEMA_VERSIONS,
)

#: Where a user drops canonical CSVs.  Read-only for this module.
DEFAULT_IMPORT_DIR = Path("imports")

#: Data classes that may ever justify selecting a strategy for paper running.
PROMOTING_CLASSES = frozenset({"USER_SUPPLIED_HISTORICAL", "USER_SUPPLIED_PRODUCTION"})


@dataclass(frozen=True)
class FileFinding:
    name: str
    path: str
    size_bytes: int
    header_ok: bool
    header_problems: tuple[str, ...]
    records: int
    quotes: int
    bars: int
    errors: int
    warnings: int
    pairs: tuple[str, ...]
    timeframes: tuple[str, ...]
    first_at: str
    last_at: str
    looks_like_sample: bool
    #: The tick-conversion sidecar's own claims, when the file has one.
    provenance: dict[str, Any] = field(default_factory=dict)

    def audit_view(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "size_bytes": self.size_bytes,
            "header_ok": self.header_ok,
            "header_problems": list(self.header_problems),
            "records": self.records,
            "quotes": self.quotes,
            "bars": self.bars,
            "errors": self.errors,
            "warnings": self.warnings,
            "pairs": list(self.pairs),
            "timeframes": list(self.timeframes),
            "first_at": self.first_at,
            "last_at": self.last_at,
            "looks_like_sample": self.looks_like_sample,
            "provenance": dict(self.provenance),
        }


def _declares_itself_a_sample(head: str) -> bool:
    """A comment line saying "sample"/"example"/"demo" is enough to distrust it."""

    text = head.lower()
    return any(word in text for word in ("sample", "example", "demo", "illustrative"))


def read_provenance(path: Path) -> dict[str, Any]:
    """The conversion sidecar beside a canonical CSV, if there is one.

    A file derived from a broker tick export carries ``DERIVED_FROM_BROKER_TICKS``
    and the SHA-256 of the export it came from.  The audit repeats those claims
    verbatim -- it never upgrades a file's provenance on the file's behalf.
    """

    sidecar = path.with_suffix(f"{path.suffix}.provenance.json")
    if not sidecar.exists():
        return {}
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"error": type(exc).__name__}
    if not isinstance(payload, dict):
        return {"error": "sidecar_not_an_object"}
    coverage = payload.get("coverage") if isinstance(payload.get("coverage"), dict) else {}
    return {
        "sidecar": sidecar.name,
        "derivation": str(payload.get("derivation", "")),
        "format": str(payload.get("format", "")),
        "source_file": str(payload.get("source_file", "")),
        "source_sha256": str(payload.get("source_sha256", "")),
        "tick_count": payload.get("tick_count"),
        "bars_written": payload.get("bars_written"),
        "server_time_basis": str(payload.get("server_time_basis", "")),
        "time_basis_detail": str(payload.get("time_basis_detail", "")),
        "coverage_complete": coverage.get("complete"),
        "gap_count": coverage.get("empty_windows"),
        "largest_gap_seconds": coverage.get("largest_gap_seconds"),
    }


def _stamp(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return ""


def inspect_file(path: Path) -> FileFinding:
    """Parse one canonical CSV strictly and summarise it.  Never repairs it."""

    text = path.read_text(encoding="utf-8", errors="replace")
    # The source label is only used in messages; the audit invents no provenance.
    parsed = parse_canonical_csv(text, source_label=f"audit:{path.name}")
    quotes = sum(1 for record in parsed.records if record.record_type == QUOTE_RECORD)
    bars = sum(1 for record in parsed.records if record.record_type == BAR_RECORD)
    pairs = tuple(sorted({record.pair for record in parsed.records if record.pair}))
    timeframes = tuple(
        sorted(
            {
                str((record.payload or {}).get("timeframe", ""))
                for record in parsed.records
                if record.record_type == BAR_RECORD
            }
            - {""}
        )
    )
    stamps = sorted(record.timestamp for record in parsed.records if record.timestamp)
    header_problems = tuple(
        sorted({issue.code for issue in parsed.issues if issue.code.startswith("header_")})
    )
    return FileFinding(
        name=path.name,
        path=str(path),
        size_bytes=path.stat().st_size,
        header_ok=parsed.header_present and not header_problems,
        header_problems=header_problems,
        records=len(parsed.records),
        quotes=quotes,
        bars=bars,
        errors=parsed.error_count(),
        warnings=sum(1 for issue in parsed.issues if issue.severity == "WARNING"),
        pairs=pairs,
        timeframes=timeframes,
        first_at=_stamp(stamps[0]) if stamps else "",
        last_at=_stamp(stamps[-1]) if stamps else "",
        looks_like_sample=path.name.upper().startswith("SAMPLE")
        or _declares_itself_a_sample(text[:2000]),
        provenance=read_provenance(path),
    )


def scan_directory(directory: Path | str = DEFAULT_IMPORT_DIR) -> tuple[FileFinding, ...]:
    root = Path(directory)
    if not root.exists():
        return ()
    findings: list[FileFinding] = []
    for path in sorted(root.glob("*.csv")):
        try:
            findings.append(inspect_file(path))
        except Exception as exc:  # an unreadable file is a finding, not a crash
            findings.append(
                FileFinding(
                    name=path.name,
                    path=str(path),
                    size_bytes=0,
                    header_ok=False,
                    header_problems=(type(exc).__name__,),
                    records=0,
                    quotes=0,
                    bars=0,
                    errors=1,
                    warnings=0,
                    pairs=(),
                    timeframes=(),
                    first_at="",
                    last_at="",
                    looks_like_sample=True,
                )
            )
    return tuple(findings)


def imported_datasets(repository: Any) -> list[dict[str, Any]]:
    """Datasets actually recorded in this database, with declared provenance."""

    try:
        rows = repository.list_datasets()
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        provenance = (row.get("provenance") or {}) if isinstance(row, Mapping) else {}
        data_class = str(
            row.get("data_class") or provenance.get("data_class") or "UNKNOWN"
        ).upper()
        out.append(
            {
                "dataset_id": str(row.get("dataset_id", "")),
                "pair": str(row.get("pair", "")),
                "timeframe": str(row.get("timeframe", "")),
                "data_class": data_class,
                "source": str(row.get("source", "") or provenance.get("source", "")),
                "source_version": str(row.get("source_version", "")),
                "valid_from": str(row.get("valid_from", "")),
                "valid_until": str(row.get("valid_until", "")),
                "bar_count": int(row.get("bar_count") or 0),
                "quote_count": int(row.get("quote_count") or 0),
                "imported_at": str(row.get("imported_at", "")),
                "promoting": data_class in PROMOTING_CLASSES,
            }
        )
    return out


def coverage(datasets: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """One row per pair/timeframe, so a thin sample is visible before it is used."""

    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for item in datasets:
        key = (str(item.get("pair", "")), str(item.get("timeframe", "")))
        row = grouped.setdefault(
            key,
            {
                "pair": key[0],
                "timeframe": key[1],
                "datasets": 0,
                "bars": 0,
                "quotes": 0,
                "data_classes": set(),
                "first_at": "",
                "last_at": "",
                "promoting": False,
            },
        )
        row["datasets"] += 1
        row["bars"] += int(item.get("bar_count") or 0)
        row["quotes"] += int(item.get("quote_count") or 0)
        row["data_classes"].add(str(item.get("data_class", "")))
        row["promoting"] = bool(row["promoting"] or item.get("promoting"))
        first, last = str(item.get("valid_from", "")), str(item.get("valid_until", ""))
        if first and (not row["first_at"] or first < row["first_at"]):
            row["first_at"] = first
        if last and (not row["last_at"] or last > row["last_at"]):
            row["last_at"] = last

    rows: list[dict[str, Any]] = []
    for row in grouped.values():
        span_days = ""
        if row["first_at"] and row["last_at"]:
            try:
                start = datetime.fromisoformat(row["first_at"].replace("Z", "+00:00"))
                end = datetime.fromisoformat(row["last_at"].replace("Z", "+00:00"))
                span_days = f"{(end - start).total_seconds() / 86400:.2f}"
            except ValueError:
                span_days = ""
        rows.append(
            {
                **{key: value for key, value in row.items() if key != "data_classes"},
                "data_classes": sorted(row["data_classes"]),
                "span_days": span_days,
            }
        )
    return sorted(rows, key=lambda item: (item["pair"], item["timeframe"]))


def audit(
    *,
    repository: Any = None,
    directory: Path | str = DEFAULT_IMPORT_DIR,
    files: Sequence[FileFinding] | None = None,
) -> dict[str, Any]:
    """The full picture: files on disk, datasets in the database, and the gaps."""

    findings = tuple(files) if files is not None else scan_directory(directory)
    datasets = imported_datasets(repository) if repository is not None else []
    promoting = [item for item in datasets if item["promoting"]]

    problems: list[str] = []
    for finding in findings:
        if not finding.header_ok:
            problems.append(f"{finding.name}: header/format is not canonical")
        elif finding.errors:
            problems.append(f"{finding.name}: {finding.errors} rejected record(s)")
        if finding.looks_like_sample:
            problems.append(
                f"{finding.name}: format example / demo data -- never evidence about a real market"
            )
    if not datasets:
        problems.append(
            "no dataset has been imported into this paper database; the runner "
            "cannot be enabled on a sample it has not evaluated"
        )
    elif not promoting:
        problems.append(
            "no imported dataset is USER_SUPPLIED_HISTORICAL or "
            "USER_SUPPLIED_PRODUCTION; demo and fixture data can never select a strategy"
        )

    coverage_rows = coverage(datasets)
    thin = [
        row
        for row in coverage_rows
        if row["bars"] and (row["bars"] < 5000 or (row["span_days"] and float(row["span_days"]) < 30))
    ]

    return {
        "format": {
            "format_id": IMPORT_FORMAT,
            "schema_version": IMPORT_SCHEMA_VERSION,
            "supported_schema_versions": sorted(SUPPORTED_IMPORT_SCHEMA_VERSIONS),
            "timezone_basis": "UTC",
        },
        "directory": str(directory),
        "files": [finding.audit_view() for finding in findings],
        "datasets": datasets,
        "coverage": coverage_rows,
        "problems": problems,
        "thin_samples": [
            {"pair": row["pair"], "timeframe": row["timeframe"], "bars": row["bars"]}
            for row in thin
        ],
        "ready_for_evaluation": bool(promoting),
        "ready_for_paper_runner": False,  # requires a *passed* evaluation, not just data
        "notes": [
            "Nothing in this report was downloaded or inferred: every row comes "
            "from a file a user placed here, or from this database's own records.",
            "A licensed MetaTrader 5 export is imported through the canonical CSV "
            "path; see docs/HISTORICAL_DATA_IMPORT.md for the exact steps and for "
            "why MT5 bar exports cannot stand in for bid/ask data.",
        ],
    }


def markdown(report: Mapping[str, Any]) -> str:
    """A plain report an operator can read without opening the code."""

    lines: list[str] = ["# Historical data audit", ""]
    lines.append(f"Directory: `{report.get('directory', '')}`")
    lines.append("")
    lines.append("## Files present")
    files = report.get("files") or []
    if not files:
        lines.append("No `.csv` files were found. Nothing has been supplied to import.")
    for item in files:
        lines.append(
            f"- `{item['name']}` -- {item['records']} record(s), "
            f"{item['quotes']} quote(s), {item['bars']} bar(s), "
            f"{item['errors']} error(s), pairs: {', '.join(item['pairs']) or 'none'}"
            + (" -- **format example / demo, not market data**" if item["looks_like_sample"] else "")
        )
        provenance = item.get("provenance") or {}
        if provenance.get("derivation"):
            digest = str(provenance.get("source_sha256", ""))
            lines.append(
                f"  - provenance: `{provenance['derivation']}` from "
                f"`{provenance.get('source_file', '')}` "
                f"sha256 `{digest[:12]}…` -- "
                f"{provenance.get('tick_count', '?')} tick(s), "
                f"{provenance.get('gap_count', '?')} empty window(s), "
                f"time basis `{provenance.get('time_basis_detail', '')}`"
            )
    lines.append("")
    lines.append("## Imported datasets")
    datasets = report.get("datasets") or []
    if not datasets:
        lines.append("None. Nothing has been imported into the paper database.")
    for item in datasets:
        lines.append(
            f"- `{item['dataset_id']}` {item['pair']} {item['timeframe']} "
            f"[{item['data_class']}] source `{item['source']}` v{item['source_version']} "
            f"valid {item['valid_from']} to {item['valid_until']}"
        )
    lines.append("")
    lines.append("## Coverage")
    for row in report.get("coverage") or []:
        lines.append(
            f"- {row['pair']} {row['timeframe']}: {row['bars']} bar(s), "
            f"span {row['span_days'] or '?'} day(s), classes {', '.join(row['data_classes'])}"
        )
    lines.append("")
    lines.append("## Problems")
    for problem in report.get("problems") or []:
        lines.append(f"- {problem}")
    lines.append("")
    lines.append("## Notes")
    for note in report.get("notes") or []:
        lines.append(f"- {note}")
    return "\n".join(lines) + "\n"


__all__ = [
    "DEFAULT_IMPORT_DIR",
    "FileFinding",
    "PROMOTING_CLASSES",
    "audit",
    "coverage",
    "imported_datasets",
    "inspect_file",
    "markdown",
    "scan_directory",
]
