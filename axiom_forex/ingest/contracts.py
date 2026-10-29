"""Canonical, provider-neutral CSV import contracts for user-supplied forex data.

This format is defined by AXIOM-FOREX; it is not a broker or vendor format and
no vendor-specific column mapping is assumed.  Producers must export their data
into this shape and declare its provenance explicitly.

File layout
-----------
* UTF-8 text.  Lines beginning with ``#`` and blank lines are ignored.
* The first non-comment line is the header.  Columns may appear in any order
  but the set must be exactly ``CsvFormat.header``; unknown or missing columns
  reject the whole file.
* Each remaining line is one record: either ``QUOTE`` (a timestamped bid/ask
  quote) or ``BAR`` (a completed bid/ask bar).
* Ordering is not significant: records are validated independently and then
  sorted chronologically.  Duplicate and conflicting timestamps are reported.

Timestamp semantics
-------------------
``timestamp`` is ISO 8601.  It is either naive wall time in the ``timezone``
column, or offset-aware.  Offset-aware values must agree with the declared zone
at that instant.  Naive wall times that are ambiguous (DST fall-back) or
nonexistent (DST spring-forward) are rejected rather than guessed.  All
accepted records are converted to UTC; the dataset's declared timezone basis is
``UTC``.

Quote semantics
---------------
``QUOTE`` rows carry executable bid/ask prices plus a ``quote_id``.  A row with
``correction_of`` set is an explicit correction of that earlier quote id and is
recorded as a data-quality event.  Two different values for the same quote id,
or two different values at the same timestamp, are conflicting records and are
rejected unless the later row is an explicit correction.

Bar semantics
-------------
``BAR`` rows carry separate bid and ask OHLC plus ``quote_count``,
``last_quote_at`` and ``complete``.  Only completed bars are accepted; there is
no interpolation and no partial-bar reconstruction.  Missing bars remain
missing and are reported as explicit quality events.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from app.audit.log import sanitize_audit_value
from app.universe.specs import CurrencyPairSpec

from axiom_forex.data.classes import (
    DATA_CLASS_SYNTHETIC_FIXTURE,
    IMPORT_DATA_CLASSES,
    data_class_view,
)
from axiom_forex.data.quality import QualityReport
from axiom_forex.research.datasets import DatasetManifest, quote_digest

IMPORT_FORMAT = "AXIOM_FOREX_CANONICAL_CSV"
IMPORT_SCHEMA_VERSION = "1.0.0"
SUPPORTED_IMPORT_SCHEMA_VERSIONS = frozenset({"1.0.0"})
IMPORT_MANIFEST_SCHEMA_VERSION = "import-manifest-v1"
NORMALIZED_TIMEZONE_BASIS = "UTC"
DEFAULT_RECORD_POLICY = "REJECT_ON_ERROR"

QUOTE_RECORD = "QUOTE"
BAR_RECORD = "BAR"
RECORD_TYPES = frozenset({QUOTE_RECORD, BAR_RECORD})

#: Exact header required by schema 1.0.0 (order-independent).
HEADER_COLUMNS: tuple[str, ...] = (
    "record_type",
    "pair",
    "timestamp",
    "timezone",
    "bid",
    "ask",
    "quote_id",
    "received_at",
    "correction_of",
    "timeframe",
    "duration_seconds",
    "bid_open",
    "bid_high",
    "bid_low",
    "bid_close",
    "ask_open",
    "ask_high",
    "ask_low",
    "ask_close",
    "quote_count",
    "last_quote_at",
    "complete",
)

QUOTE_COLUMNS = (
    "bid",
    "ask",
    "quote_id",
    "received_at",
    "correction_of",
)
BAR_COLUMNS = (
    "timeframe",
    "duration_seconds",
    "bid_open",
    "bid_high",
    "bid_low",
    "bid_close",
    "ask_open",
    "ask_high",
    "ask_low",
    "ask_close",
    "quote_count",
    "last_quote_at",
    "complete",
)

SEVERITY_ERROR = "ERROR"
SEVERITY_WARNING = "WARNING"

#: Provenance labels must be documentation-style identifiers, not locators.
_SAFE_SOURCE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+:\-]{0,63}$")


class ImportSourceError(Exception):
    """Raised when an import source cannot be read at all."""


@dataclass(frozen=True)
class ImportRequest:
    """Explicit declarations required to import one dataset.

    Nothing is defaulted on the caller's behalf: the pair, timeframe, data
    class, provenance label, source version, and validity bounds must all be
    stated.  The importer never guesses a broker, vendor, or format.
    """

    pair: str
    timeframe: str
    data_class: str
    source: str
    source_version: str
    valid_from: datetime
    valid_until: datetime
    timezone_basis: str = NORMALIZED_TIMEZONE_BASIS
    provider_id: str = ""
    documented_at: str = ""
    note: str = ""

    def validate(self) -> tuple[str, ...]:
        errors: list[str] = []
        if not str(self.pair).strip():
            errors.append("missing_pair")
        if not str(self.timeframe).strip():
            errors.append("missing_timeframe")
        if not str(self.source).strip():
            errors.append("missing_source")
        if not str(self.source_version).strip():
            errors.append("missing_source_version")
        if str(self.source).strip() and not _SAFE_SOURCE_LABEL.match(str(self.source).strip()):
            errors.append("unsafe_source_label")
        if str(self.data_class) not in IMPORT_DATA_CLASSES:
            errors.append("unsupported_data_class")
        if str(self.timezone_basis).strip().upper() != NORMALIZED_TIMEZONE_BASIS:
            errors.append("unsupported_timezone_basis")
        if not isinstance(self.valid_from, datetime) or not isinstance(
            self.valid_until, datetime
        ):
            errors.append("invalid_validity_bounds")
        elif self.valid_until <= self.valid_from:
            errors.append("empty_validity_window")
        return tuple(errors)

    def audit_view(self) -> dict[str, Any]:
        return {
            "pair": self.pair,
            "timeframe": self.timeframe,
            "data_class": self.data_class,
            "source": self.source,
            "source_version": self.source_version,
            "documented_at": self.documented_at,
            "timezone_basis": self.timezone_basis,
            "provider_id": self.provider_id,
            "valid_from": self.valid_from.isoformat(),
            "valid_until": self.valid_until.isoformat(),
            "note": self.note,
        }


@dataclass(frozen=True)
class CsvFormat:
    """Structural description of the canonical CSV used by this importer."""

    format_id: str = IMPORT_FORMAT
    schema_version: str = IMPORT_SCHEMA_VERSION
    header: tuple[str, ...] = HEADER_COLUMNS
    quote_record: str = QUOTE_RECORD
    bar_record: str = BAR_RECORD
    timezone_basis: str = NORMALIZED_TIMEZONE_BASIS

    def audit_view(self) -> dict[str, Any]:
        return {
            "format_id": self.format_id,
            "schema_version": self.schema_version,
            "header": list(self.header),
            "record_types": sorted(RECORD_TYPES),
            "quote_columns": list(QUOTE_COLUMNS),
            "bar_columns": list(BAR_COLUMNS),
            "timezone_basis": self.timezone_basis,
            "suppresses": [
                "interpolation",
                "mid-price fills",
                "partial-bar reconstruction",
                "ambiguous DST wall times",
            ],
        }


@dataclass(frozen=True)
class ImportIssue:
    """One field-level import finding, keyed by line and field."""

    line: int
    field: str
    code: str
    severity: str
    detail: str

    @property
    def ok(self) -> bool:
        return self.severity != SEVERITY_ERROR

    def audit_view(self) -> dict[str, Any]:
        return {
            "line": self.line,
            "field": self.field,
            "code": self.code,
            "severity": self.severity,
            "detail": self.detail,
            "ok": self.ok,
        }


@dataclass(frozen=True)
class QualityEvent:
    """Explicit data-quality event; gaps and corrections are never hidden."""

    code: str
    severity: str
    detail: str
    line: int | None = None
    kind: str = "RECORD"

    @property
    def ok(self) -> bool:
        return self.severity != SEVERITY_ERROR

    def audit_view(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "detail": self.detail,
            "line": self.line,
            "kind": self.kind,
            "ok": self.ok,
        }


@dataclass(frozen=True)
class QuarantinedRecord:
    """A rejected source line, retained for operator review without its values."""

    line: int
    record_type: str
    codes: tuple[str, ...]

    def audit_view(self) -> dict[str, Any]:
        return {
            "line": self.line,
            "record_type": self.record_type,
            "codes": list(self.codes),
        }


@dataclass(frozen=True)
class ImportManifest:
    """Dataset manifest for one import: identity, hashes, coverage, provenance."""

    import_id: str
    format_id: str
    schema_version: str
    pair: str
    timeframe: str
    data_class: str
    source: str
    source_version: str
    documented_at: str
    timezone_basis: str
    valid_from: datetime
    valid_until: datetime
    source_sha256: str
    source_byte_count: int
    row_count: int
    bar_count: int
    quote_count: int
    normalized_sha256: str
    quotes_sha256: str
    pair_spec_version: str
    pair_spec_signature: str
    catalog_version: str
    catalog_signature: str
    calendar_id: str
    calendar_version: str
    calendar_signature: str
    first_timestamp: str
    last_timestamp: str
    quote_first_timestamp: str
    quote_last_timestamp: str
    validation_classification: str
    validation_passed: bool
    error_codes: tuple[str, ...] = ()
    warning_codes: tuple[str, ...] = ()
    correction_count: int = 0
    duplicate_count: int = 0
    profile_id: str = ""
    profile_version: str = ""
    generated_at: str = ""
    manifest_schema_version: str = IMPORT_MANIFEST_SCHEMA_VERSION

    def verify(
        self,
        *,
        bars: Sequence[Any],
        quotes: Sequence[Any],
        dataset_manifest: DatasetManifest,
    ) -> bool:
        """Recompute digests and counts; a tampered manifest must not verify."""

        if not bars:
            return False
        return (
            len(bars) == self.bar_count
            and len(quotes) == self.quote_count
            and dataset_manifest.sha256 == self.normalized_sha256
            and dataset_manifest.data_class == self.data_class
            and dataset_manifest.pair == self.pair
            and dataset_manifest.timeframe == self.timeframe
            and quote_digest(quotes) == self.quotes_sha256
            and self.source_sha256
            and bars[0].timestamp.isoformat() == self.first_timestamp
            and bars[-1].end_at.isoformat() == self.last_timestamp
            and str(bars[0].source) == self.source
            and self.data_class != DATA_CLASS_SYNTHETIC_FIXTURE
        )

    def audit_view(self) -> dict[str, Any]:
        payload = {
            "manifest_schema_version": self.manifest_schema_version,
            "import_id": self.import_id,
            "format_id": self.format_id,
            "schema_version": self.schema_version,
            "pair": self.pair,
            "timeframe": self.timeframe,
            "data_class": self.data_class,
            "data_class_view": data_class_view(self.data_class),
            "source": self.source,
            "source_version": self.source_version,
            "documented_at": self.documented_at,
            "timezone_basis": self.timezone_basis,
            "valid_from": self.valid_from.isoformat(),
            "valid_until": self.valid_until.isoformat(),
            "source_sha256": self.source_sha256,
            "source_byte_count": self.source_byte_count,
            "row_count": self.row_count,
            "bar_count": self.bar_count,
            "quote_count": self.quote_count,
            "normalized_sha256": self.normalized_sha256,
            "quotes_sha256": self.quotes_sha256,
            "pair_spec_version": self.pair_spec_version,
            "pair_spec_signature": self.pair_spec_signature,
            "catalog_version": self.catalog_version,
            "catalog_signature": self.catalog_signature,
            "calendar_id": self.calendar_id,
            "calendar_version": self.calendar_version,
            "calendar_signature": self.calendar_signature,
            "time_coverage": {
                "first_timestamp": self.first_timestamp,
                "last_timestamp": self.last_timestamp,
                "quote_first_timestamp": self.quote_first_timestamp,
                "quote_last_timestamp": self.quote_last_timestamp,
            },
            "validation": {
                "classification": self.validation_classification,
                "passed": self.validation_passed,
                "error_codes": list(self.error_codes),
                "warning_codes": list(self.warning_codes),
                "correction_count": self.correction_count,
                "duplicate_count": self.duplicate_count,
            },
            "profile": {
                "profile_id": self.profile_id,
                "profile_version": self.profile_version,
            },
            "generated_at": self.generated_at,
        }
        clean = sanitize_audit_value(payload)
        return clean if isinstance(clean, dict) else {"import_id": self.import_id}


@dataclass(frozen=True)
class ImportedDataset:
    """A validated imported dataset plus its manifest and provenance."""

    pair: str
    timeframe: str
    spec: CurrencyPairSpec
    catalog: Any
    catalog_signature: str
    catalog_version: str
    bars: tuple[Any, ...]
    quotes: tuple[Any, ...]
    quality: QualityReport
    dataset_manifest: DatasetManifest
    import_manifest: ImportManifest
    data_class: str
    provenance: Mapping[str, str] = field(default_factory=dict)
    quality_events: tuple[QualityEvent, ...] = ()

    @property
    def usable(self) -> bool:
        return self.quality.usable

    @property
    def execution_eligible(self) -> bool:
        return False

    def verify(self) -> bool:
        return self.import_manifest.verify(
            bars=self.bars,
            quotes=self.quotes,
            dataset_manifest=self.dataset_manifest,
        )

    def as_loaded_dataset(self) -> Any:
        """Adapt to the existing pipeline dataset type for research use."""

        from axiom_forex.data.fixtures import SyntheticForexProvider
        from axiom_forex.data.pipeline import LoadedDataset
        from axiom_forex.universe.manager import spec_signature

        provider = SyntheticForexProvider(
            bars=tuple(self.bars),
            catalog=self.catalog,
            provider_id=self.import_manifest.source,
            source=self.import_manifest.source,
            quotes=tuple(self.quotes),
            supported_timeframes=frozenset({self.timeframe}),
        )
        return LoadedDataset(
            pair=self.pair,
            spec=self.spec,
            catalog=provider.catalog,
            catalog_signature=self.catalog_signature,
            spec_signature=spec_signature(self.spec),
            bars=tuple(self.bars),
            quotes=tuple(self.quotes),
            quality=self.quality,
            manifest=self.dataset_manifest,
            provider=provider,
            normalization_count=len(self.bars) + len(self.quotes),
            data_class=self.data_class,
            provenance={str(key): str(value) for key, value in self.provenance.items()},
        )

    def audit_view(self) -> dict[str, Any]:
        payload = {
            "pair": self.pair,
            "timeframe": self.timeframe,
            "data_class": self.data_class,
            "data_class_view": data_class_view(self.data_class),
            "provenance": {str(key): str(value) for key, value in self.provenance.items()},
            "quality": self.quality.audit_view(),
            "quality_events": [event.audit_view() for event in self.quality_events],
            "manifest": self.import_manifest.audit_view(),
            "dataset_manifest": self.dataset_manifest.audit_view(),
            "execution_eligible": self.execution_eligible,
            "manifest_valid": self.verify(),
            "usable": self.usable,
        }
        clean = sanitize_audit_value(payload)
        return clean if isinstance(clean, dict) else {"pair": self.pair}


def _collect_codes(issues: Iterable[ImportIssue], severity: str) -> tuple[str, ...]:
    return tuple(sorted({issue.code for issue in issues if issue.severity == severity}))


@dataclass(frozen=True)
class CompatibilityCheck:
    """One profile/catalog/calendar compatibility check."""

    name: str
    passed: bool
    detail: str

    def audit_view(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass(frozen=True)
class ImportResult:
    """Result of one canonical CSV import attempt.

    ``passed`` is True only when every structural, provenance, profile
    compatibility, record, and data-quality gate succeeded.  A failed import
    keeps its findings, quarantine list, and quality events so an operator can
    see exactly what was rejected; it never returns a usable dataset.
    """

    request: ImportRequest
    status: str
    passed: bool
    reason: str
    issues: tuple[ImportIssue, ...] = ()
    quality_events: tuple[QualityEvent, ...] = ()
    quarantine: tuple[QuarantinedRecord, ...] = ()
    compatibility: tuple[CompatibilityCheck, ...] = ()
    dataset: ImportedDataset | None = None
    counts: Mapping[str, Any] = field(default_factory=dict)
    generated_at: str = ""
    note: str = (
        "Imported data is research/backtest material. It is never used for paper "
        "fills, never relabeled as a synthetic fixture, and does not imply "
        "profitability or live readiness."
    )

    @property
    def error_count(self) -> int:
        return sum(1 for issue in self.issues if issue.severity == SEVERITY_ERROR)

    @property
    def warning_count(self) -> int:
        return sum(1 for issue in self.issues if issue.severity == SEVERITY_WARNING)

    def audit_view(self) -> dict[str, Any]:
        dataset_view = self.dataset.audit_view() if self.dataset is not None else None
        payload = {
            "status": self.status,
            "passed": self.passed,
            "reason": self.reason,
            "request": self.request.audit_view(),
            "format": CsvFormat().audit_view(),
            "counts": {str(key): value for key, value in self.counts.items()},
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "compatibility": {
                "passed": all(check.passed for check in self.compatibility),
                "checks": [check.audit_view() for check in self.compatibility],
            },
            "issues": [issue.audit_view() for issue in self.issues],
            "quality_events": [event.audit_view() for event in self.quality_events],
            "quarantine": [record.audit_view() for record in self.quarantine],
            "dataset": dataset_view,
            "provenance": (
                dataset_view.get("provenance") if dataset_view is not None else {}
            ),
            "data_class": (
                dataset_view.get("data_class")
                if dataset_view is not None
                else self.request.data_class
            ),
            "manifest": (
                dataset_view.get("manifest") if dataset_view is not None else None
            ),
            "manifest_valid": bool(dataset_view and dataset_view.get("manifest_valid")),
            "integrity": {
                "manifest_valid": bool(dataset_view and dataset_view.get("manifest_valid")),
                "source_sha256": (
                    dataset_view.get("manifest", {}).get("source_sha256")
                    if dataset_view is not None
                    else None
                ),
            },
            "generated_at": self.generated_at,
            "note": self.note,
        }
        clean = sanitize_audit_value(payload)
        return clean if isinstance(clean, dict) else {"passed": self.passed}
