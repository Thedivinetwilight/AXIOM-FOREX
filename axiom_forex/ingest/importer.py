"""Offline importer for user-supplied canonical forex data.

The importer is deliberately strict and fail-closed:

* the canonical CSV must match the documented schema exactly;
* every record must agree with the declared pair, timeframe, and validity bounds;
* every record must be consistent with the provider profile terms, the versioned
  pair spec, and the active trading calendar;
* duplicate, conflicting, crossed, non-positive, stale, incomplete, or
  out-of-session records reject (or quarantine) the dataset;
* missing bars stay missing and become explicit quality events;
* the result carries a manifest with source and normalized hashes, coverage,
  timezone basis, provenance, and validation results.

No network call, credential, broker, or vendor format is involved.  Imported
data is research/backtest material only and is never used for paper fills.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping

from app.calendar.trading import TradingCalendar
from app.data.models import ForexQuote, ForexQuoteBar, QuoteFreshness
from app.universe.specs import CurrencyPairSpec

from axiom_forex.data.normalization import DataNormalizer
from axiom_forex.data.quality import QualityReport, assess_bars
from axiom_forex.profiles.contracts import ProviderProfile
from axiom_forex.research.datasets import (
    build_manifest,
    dataset_digest,
    payload_digest,
    quote_digest,
)
from axiom_forex.universe.manager import InstrumentCatalogManager, spec_signature

from .canonical_csv import ParsedFile, SourceRecord, parse_canonical_csv
from .compatibility import ProfileRequirements, check_import_compatibility
from .contracts import (
    BAR_RECORD,
    IMPORT_FORMAT,
    IMPORT_SCHEMA_VERSION,
    NORMALIZED_TIMEZONE_BASIS,
    QUOTE_RECORD,
    SEVERITY_ERROR,
    SEVERITY_WARNING,
    ImportIssue,
    ImportManifest,
    ImportRequest,
    ImportResult,
    ImportSourceError,
    ImportedDataset,
    QualityEvent,
    QuarantinedRecord,
)

#: Quality codes that describe an absent bar interval rather than a bad record.
GAP_CODES = frozenset(
    {
        "MISSING_BAR",
        "EXPECTED_SESSION_GAP",
        "GAP_TOO_LARGE_TO_VALIDATE",
    }
)


@dataclass(frozen=True)
class ForexDataImporter:
    """Deterministic, offline canonical CSV importer."""

    catalog_manager: InstrumentCatalogManager
    calendar: TradingCalendar
    quote_freshness: QuoteFreshness
    profile: ProviderProfile
    profile_passed: bool = False
    now: datetime | None = None

    # -- public API --------------------------------------------------------
    def import_text(self, text: str, request: ImportRequest) -> ImportResult:
        generated_at = (self.now or datetime.now(timezone.utc)).isoformat()
        request_errors = request.validate()
        if request_errors:
            return ImportResult(
                request=request,
                status="FAIL",
                passed=False,
                reason="invalid_import_request:" + ",".join(request_errors),
                issues=tuple(
                    ImportIssue(0, "request", code, SEVERITY_ERROR, "invalid import request")
                    for code in request_errors
                ),
                counts={"rows": 0},
                generated_at=generated_at,
            )
        catalog = self.catalog_manager.catalog
        catalog_validation = catalog.validate()
        if not catalog_validation.valid:
            return self._fail(
                request,
                generated_at,
                reason="invalid_catalog:" + catalog_validation.reason,
                issues=(
                    ImportIssue(0, "catalog", "invalid_catalog", SEVERITY_ERROR, "catalog invalid"),
                ),
            )
        spec = catalog.get(request.pair)
        if spec is None or not spec.validate().valid:
            return self._fail(
                request,
                generated_at,
                reason="unknown_pair_spec",
                issues=(
                    ImportIssue(
                        0,
                        "pair",
                        "unknown_pair_spec",
                        SEVERITY_ERROR,
                        "pair has no valid versioned spec; no spec is invented",
                    ),
                ),
            )

        checks, requirements = check_import_compatibility(
            request=request,
            profile=self.profile,
            profile_passed=self.profile_passed,
            spec=spec,
            calendar=self.calendar,
        )
        failed_checks = [check for check in checks if not check.passed]
        if failed_checks:
            return ImportResult(
                request=request,
                status="FAIL",
                passed=False,
                reason="profile_incompatible:" + ",".join(check.name for check in failed_checks),
                issues=tuple(
                    ImportIssue(
                        0,
                        "profile",
                        "incompatible_profile_term",
                        SEVERITY_ERROR,
                        f"{check.name}: {check.detail}",
                    )
                    for check in failed_checks
                ),
                compatibility=checks,
                counts={"rows": 0},
                generated_at=generated_at,
            )

        parsed = parse_canonical_csv(text, source_label=request.source)
        issues = list(parsed.issues)
        if parsed.fatal or not parsed.records:
            return ImportResult(
                request=request,
                status="FAIL",
                passed=False,
                reason="unusable_import_file",
                issues=tuple(issues)
                or (
                    ImportIssue(
                        0,
                        "file",
                        "no_records",
                        SEVERITY_ERROR,
                        "file contains no importable records",
                    ),
                ),
                compatibility=checks,
                quarantine=self._quarantine(parsed, issues),
                counts={
                    "rows": parsed.line_count,
                    "quote_rows": 0,
                    "bar_rows": 0,
                    "rejected_rows": parsed.line_count,
                },
                generated_at=generated_at,
            )

        # -- record-level validation ---------------------------------------
        record_issues: list[ImportIssue] = []
        events: list[QualityEvent] = []
        quote_records, bar_records = self._split_records(
            parsed.records, request, requirements, record_issues
        )
        accepted_quotes, quote_events, quote_issues, quote_counts = self._dedupe_quotes(
            quote_records
        )
        accepted_bars, bar_events, bar_issues, bar_counts = self._dedupe_bars(bar_records)
        record_issues.extend(quote_issues)
        record_issues.extend(bar_issues)
        events.extend(quote_events)
        events.extend(bar_events)
        issues.extend(record_issues)

        if not accepted_bars:
            issues.append(
                ImportIssue(
                    0,
                    "records",
                    "no_bars",
                    SEVERITY_ERROR,
                    "a dataset must contain at least one completed bar",
                )
            )

        normalizer = DataNormalizer(catalog, requirements.provider_id)
        quotes = self._normalize_quotes(normalizer, accepted_quotes, spec, requirements, issues)
        bars = self._normalize_bars(normalizer, accepted_bars, spec, requirements, issues)

        counts = {
            "rows": parsed.line_count,
            "records": len(parsed.records),
            "quote_rows": quote_counts["rows"],
            "bar_rows": bar_counts["rows"],
            "accepted_quotes": len(quotes),
            "accepted_bars": len(bars),
            "duplicate_quotes": quote_counts["duplicates"],
            "duplicate_bars": bar_counts["duplicates"],
            "corrections": quote_counts["corrections"],
            "rejected_rows": len(self._quarantine(parsed, issues)),
        }

        error_issues = [issue for issue in issues if issue.severity == SEVERITY_ERROR]
        if error_issues or not bars:
            return ImportResult(
                request=request,
                status="FAIL",
                passed=False,
                reason="record_validation_failed:"
                + ",".join(sorted({issue.code for issue in error_issues} or {"no_bars"})),
                issues=tuple(issues),
                quality_events=tuple(events),
                quarantine=self._quarantine(parsed, issues),
                compatibility=checks,
                counts=counts,
                generated_at=generated_at,
            )

        quality = assess_bars(
            bars,
            spec=spec,
            calendar=self.calendar,
            freshness=self.quote_freshness,
            evaluated_at=bars[-1].end_at,
        )
        events.extend(self._quality_events(quality))
        fatal_quality = [
            event
            for event in events
            if event.severity == SEVERITY_ERROR and event.code not in GAP_CODES
        ]
        if fatal_quality:
            return ImportResult(
                request=request,
                status="FAIL",
                passed=False,
                reason="data_quality_failed:"
                + ",".join(sorted({event.code for event in fatal_quality})),
                issues=tuple(issues),
                quality_events=tuple(events),
                quarantine=self._quarantine(parsed, issues),
                compatibility=checks,
                counts={**counts, "quality": quality.classification},
                generated_at=generated_at,
            )

        manifest = self._manifest(
            request=request,
            spec=spec,
            requirements=requirements,
            text=text,
            bars=bars,
            quotes=quotes,
            quality=quality,
            counts=counts,
            events=events,
            passed=quality.usable,
            generated_at=generated_at,
        )
        dataset_manifest = build_manifest(
            bars,
            dataset_id=manifest.import_id,
            spec_version=spec.spec_version,
            calendar_signature=self.calendar.signature(),
            source=request.source,
            data_class=request.data_class,
            timezone_basis=NORMALIZED_TIMEZONE_BASIS,
            source_version=request.source_version,
            validation_classification=quality.classification,
        )
        dataset = ImportedDataset(
            pair=request.pair,
            timeframe=request.timeframe,
            spec=spec,
            catalog=catalog,
            catalog_signature=self.catalog_manager.signature(),
            catalog_version=self.catalog_manager.version,
            bars=bars,
            quotes=quotes,
            quality=quality,
            dataset_manifest=dataset_manifest,
            import_manifest=manifest,
            data_class=request.data_class,
            provenance=self._provenance(request, manifest.import_id, spec),
            quality_events=tuple(events),
        )
        integrity_ok = dataset.verify()
        passed = bool(integrity_ok and quality.usable)
        status = "PASS" if passed else "QUARANTINED"
        reason = "canonical_import_validated" if passed else "data_quality_not_usable"
        if not integrity_ok:
            status, reason = "FAIL", "manifest_integrity_invalid"
        return ImportResult(
            request=request,
            status=status,
            passed=passed,
            reason=reason,
            issues=tuple(issues),
            quality_events=tuple(events),
            quarantine=self._quarantine(parsed, issues),
            compatibility=checks,
            dataset=dataset,
            counts={**counts, "quality": quality.classification},
            generated_at=generated_at,
        )

    def import_file(self, path: str | Path, request: ImportRequest) -> ImportResult:
        source = Path(path)
        try:
            text = source.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ImportSourceError(f"unreadable_import_source:{type(exc).__name__}") from exc
        return self.import_text(text, request)

    # -- internals ---------------------------------------------------------
    def _fail(
        self,
        request: ImportRequest,
        generated_at: str,
        *,
        reason: str,
        issues: Iterable[ImportIssue],
    ) -> ImportResult:
        return ImportResult(
            request=request,
            status="FAIL",
            passed=False,
            reason=reason,
            issues=tuple(issues),
            counts={"rows": 0},
            generated_at=generated_at,
        )

    @staticmethod
    def _provenance(
        request: ImportRequest, import_id: str, spec: CurrencyPairSpec
    ) -> dict[str, str]:
        return {
            "data_class": request.data_class,
            "source": request.source,
            "source_version": request.source_version,
            "documented_at": request.documented_at,
            "format": IMPORT_FORMAT,
            "schema_version": IMPORT_SCHEMA_VERSION,
            "timezone_basis": NORMALIZED_TIMEZONE_BASIS,
            "import_id": import_id,
            "pair_spec_version": spec.spec_version,
        }

    def _split_records(
        self,
        records: Iterable[SourceRecord],
        request: ImportRequest,
        requirements: ProfileRequirements,
        issues: list[ImportIssue],
    ) -> tuple[list[SourceRecord], list[SourceRecord]]:
        quotes: list[SourceRecord] = []
        bars: list[SourceRecord] = []
        for record in records:
            valid = True
            if record.pair != request.pair:
                issues.append(
                    ImportIssue(
                        record.line,
                        "pair",
                        "unexpected_pair",
                        SEVERITY_ERROR,
                        "record pair does not match the declared import pair",
                    )
                )
                valid = False
            if record.timestamp < request.valid_from:
                issues.append(
                    ImportIssue(
                        record.line,
                        "timestamp",
                        "outside_validity_bounds",
                        SEVERITY_ERROR,
                        "record precedes the declared validity bounds",
                    )
                )
                valid = False
            end_at = record.timestamp
            if record.record_type == BAR_RECORD:
                duration = record.payload.get("duration_seconds")
                if isinstance(duration, int) and not isinstance(duration, bool):
                    end_at = record.timestamp + timedelta(seconds=duration)
                if record.payload.get("timeframe") != request.timeframe:
                    issues.append(
                        ImportIssue(
                            record.line,
                            "timeframe",
                            "timeframe_mismatch",
                            SEVERITY_ERROR,
                            "bar timeframe does not match the declared import timeframe",
                        )
                    )
                    valid = False
            if end_at > request.valid_until:
                issues.append(
                    ImportIssue(
                        record.line,
                        "timestamp",
                        "outside_validity_bounds",
                        SEVERITY_ERROR,
                        "record extends past the declared validity bounds",
                    )
                )
                valid = False
            if record.record_type == QUOTE_RECORD and not record.payload.get("quote_id"):
                issues.append(
                    ImportIssue(
                        record.line,
                        "quote_id",
                        "missing_quote_id",
                        SEVERITY_ERROR,
                        "quote id is required for provenance and corrections",
                    )
                )
                valid = False
            if valid:
                if record.record_type == QUOTE_RECORD:
                    quotes.append(record)
                else:
                    bars.append(record)
        return quotes, bars

    def _dedupe_quotes(
        self,
        records: Iterable[SourceRecord],
    ) -> tuple[list[SourceRecord], list[QualityEvent], list[ImportIssue], dict[str, int]]:
        ordered = sorted(tuple(records), key=lambda item: (item.timestamp, item.line))
        accepted: list[SourceRecord] = []
        events: list[QualityEvent] = []
        issues: list[ImportIssue] = []
        by_id: dict[str, SourceRecord] = {}
        by_timestamp: dict[datetime, SourceRecord] = {}
        duplicates = 0
        corrections = 0
        for record in ordered:
            payload = record.payload
            quote_id = str(payload.get("quote_id", ""))
            metadata = payload.get("metadata") or {}
            correction_of = str(metadata.get("correction_of", "") or "") if isinstance(
                metadata, Mapping
            ) else ""
            if correction_of:
                if correction_of not in by_id:
                    issues.append(
                        ImportIssue(
                            record.line,
                            "correction_of",
                            "unknown_correction_target",
                            SEVERITY_ERROR,
                            "correction references an unknown quote id",
                        )
                    )
                    continue
                corrections += 1
                events.append(
                    QualityEvent(
                        "QUOTE_CORRECTION",
                        SEVERITY_WARNING,
                        f"explicit correction of {correction_of}",
                        line=record.line,
                        kind="CORRECTION",
                    )
                )
                by_id[quote_id or f"line:{record.line}"] = record
                accepted.append(record)
                by_timestamp.setdefault(record.timestamp, record)
                continue
            if quote_id and quote_id in by_id:
                previous = by_id[quote_id]
                if self._same_quote(previous, record):
                    duplicates += 1
                    events.append(
                        QualityEvent(
                            "DUPLICATE_QUOTE",
                            SEVERITY_WARNING,
                            f"identical duplicate of {quote_id}",
                            line=record.line,
                            kind="DUPLICATE",
                        )
                    )
                    continue
                issues.append(
                    ImportIssue(
                        record.line,
                        "quote_id",
                        "conflicting_quote",
                        SEVERITY_ERROR,
                        "same quote id with different values requires a correction record",
                    )
                )
                continue
            if record.timestamp in by_timestamp:
                previous = by_timestamp[record.timestamp]
                if self._same_quote(previous, record):
                    duplicates += 1
                    events.append(
                        QualityEvent(
                            "DUPLICATE_QUOTE_TIMESTAMP",
                            SEVERITY_WARNING,
                            "identical duplicate timestamp",
                            line=record.line,
                            kind="DUPLICATE",
                        )
                    )
                    continue
                issues.append(
                    ImportIssue(
                        record.line,
                        "timestamp",
                        "conflicting_quote_timestamp",
                        SEVERITY_ERROR,
                        "two different quotes share one timestamp",
                    )
                )
                continue
            by_id[quote_id or f"line:{record.line}"] = record
            by_timestamp[record.timestamp] = record
            accepted.append(record)
        return (
            accepted,
            events,
            issues,
            {
                "rows": len(ordered),
                "duplicates": duplicates,
                "corrections": corrections,
            },
        )

    def _dedupe_bars(
        self,
        records: Iterable[SourceRecord],
    ) -> tuple[list[SourceRecord], list[QualityEvent], list[ImportIssue], dict[str, int]]:
        ordered = sorted(tuple(records), key=lambda item: (item.timestamp, item.line))
        accepted: list[SourceRecord] = []
        events: list[QualityEvent] = []
        issues: list[ImportIssue] = []
        by_timestamp: dict[datetime, SourceRecord] = {}
        duplicates = 0
        for record in ordered:
            if record.timestamp in by_timestamp:
                previous = by_timestamp[record.timestamp]
                if self._same_bar(previous, record):
                    duplicates += 1
                    events.append(
                        QualityEvent(
                            "DUPLICATE_BAR",
                            SEVERITY_WARNING,
                            "identical duplicate bar",
                            line=record.line,
                            kind="DUPLICATE",
                        )
                    )
                    continue
                issues.append(
                    ImportIssue(
                        record.line,
                        "timestamp",
                        "conflicting_bar",
                        SEVERITY_ERROR,
                        "two different bars share one opening timestamp",
                    )
                )
                continue
            by_timestamp[record.timestamp] = record
            accepted.append(record)
        return (
            accepted,
            events,
            issues,
            {"rows": len(ordered), "duplicates": duplicates, "corrections": 0},
        )

    @staticmethod
    def _same_quote(first: SourceRecord, second: SourceRecord) -> bool:
        keys = ("bid", "ask")
        return all(
            str(first.payload.get(key)) == str(second.payload.get(key)) for key in keys
        ) and first.timestamp == second.timestamp

    @staticmethod
    def _same_bar(first: SourceRecord, second: SourceRecord) -> bool:
        keys = (
            "bid",
            "ask",
            "quote_count",
            "last_quote_at",
            "duration_seconds",
            "timeframe",
        )
        return all(
            str(first.payload.get(key)) == str(second.payload.get(key)) for key in keys
        )

    def _normalize_quotes(
        self,
        normalizer: DataNormalizer,
        records: Iterable[SourceRecord],
        spec: CurrencyPairSpec,
        requirements: ProfileRequirements,
        issues: list[ImportIssue],
    ) -> tuple[ForexQuote, ...]:
        normalized: list[ForexQuote] = []
        pip = spec.pip_size_decimal()
        for record in records:
            payload = dict(record.payload)
            payload["symbol"] = requirements.provider_symbol
            result = normalizer.normalize_quote(payload)
            if not result.ok or not isinstance(result.value, ForexQuote):
                issues.extend(
                    ImportIssue(
                        record.line,
                        "quote",
                        code,
                        SEVERITY_ERROR,
                        "quote failed strict normalization",
                    )
                    for code in (result.errors or ("invalid_quote",))
                )
                continue
            quote = result.value
            freshness = self.quote_freshness.check(
                quote, quote.received_at or quote.timestamp
            )
            if not freshness.ok:
                issues.append(
                    ImportIssue(
                        record.line,
                        "quote",
                        "stale_quote",
                        SEVERITY_ERROR,
                        f"freshness check failed: {freshness.reason.value}",
                    )
                )
                continue
            bid, bid_error = _decimal(quote.bid)
            ask, ask_error = _decimal(quote.ask)
            if bid_error or ask_error or bid is None or ask is None:
                issues.append(
                    ImportIssue(
                        record.line,
                        "quote",
                        "invalid_quote_price",
                        SEVERITY_ERROR,
                        "bid/ask could not be parsed",
                    )
                )
                continue
            if (
                requirements.max_spread_pips is not None
                and (ask - bid) / pip > requirements.max_spread_pips
            ):
                issues.append(
                    ImportIssue(
                        record.line,
                        "quote",
                        "spread_limit_exceeded",
                        SEVERITY_ERROR,
                        "quote spread exceeds the profile maximum",
                    )
                )
                continue
            normalized.append(quote)
        return tuple(normalized)

    def _normalize_bars(
        self,
        normalizer: DataNormalizer,
        records: Iterable[SourceRecord],
        spec: CurrencyPairSpec,
        requirements: ProfileRequirements,
        issues: list[ImportIssue],
    ) -> tuple[ForexQuoteBar, ...]:
        normalized: list[ForexQuoteBar] = []
        pip = spec.pip_size_decimal()
        for record in records:
            payload = dict(record.payload)
            payload["symbol"] = requirements.provider_symbol
            result = normalizer.normalize_bar(payload)
            if not result.ok or not isinstance(result.value, ForexQuoteBar):
                for code in result.errors or ("invalid_bar",):
                    issues.append(
                        ImportIssue(
                            record.line,
                            "bar",
                            code,
                            SEVERITY_ERROR,
                            "bar failed strict normalization",
                        )
                    )
                continue
            bar = result.value
            bid_values, bid_result = bar.bid.parsed()
            ask_values, ask_result = bar.ask.parsed()
            if (
                not bid_result.valid
                or not ask_result.valid
                or bid_values is None
                or ask_values is None
            ):
                issues.append(
                    ImportIssue(
                        record.line,
                        "bar",
                        "invalid_bar_ohlc",
                        SEVERITY_ERROR,
                        "bar OHLC failed validation",
                    )
                )
                continue
            crossed = [
                point
                for point in ("open", "high", "low", "close")
                if ask_values[point] < bid_values[point]
            ]
            if crossed:
                issues.append(
                    ImportIssue(
                        record.line,
                        "bar",
                        "crossed_quote",
                        SEVERITY_ERROR,
                        "ask below bid at: " + ",".join(crossed),
                    )
                )
                continue
            spread_pips = (ask_values["close"] - bid_values["close"]) / pip
            if (
                requirements.max_spread_pips is not None
                and spread_pips > requirements.max_spread_pips
            ):
                issues.append(
                    ImportIssue(
                        record.line,
                        "bar",
                        "spread_limit_exceeded",
                        SEVERITY_ERROR,
                        "bar spread exceeds the profile maximum",
                    )
                )
                continue
            normalized.append(bar)
        return tuple(normalized)

    @staticmethod
    def _quality_events(quality: QualityReport) -> list[QualityEvent]:
        events: list[QualityEvent] = []
        for finding in quality.findings:
            events.append(
                QualityEvent(
                    code=finding.code,
                    severity=finding.severity,
                    detail=f"index={finding.index}:{finding.detail}",
                    line=None,
                    kind="QUALITY",
                )
            )
        return events

    def _manifest(
        self,
        *,
        request: ImportRequest,
        spec: CurrencyPairSpec,
        requirements: ProfileRequirements,
        text: str,
        bars: tuple[ForexQuoteBar, ...],
        quotes: tuple[ForexQuote, ...],
        quality: QualityReport,
        counts: Mapping[str, Any],
        events: tuple[QualityEvent, ...] | list[QualityEvent],
        passed: bool,
        generated_at: str,
    ) -> ImportManifest:
        source_sha = payload_digest(text)
        normalized_sha = dataset_digest(bars)
        import_id = (
            "import:"
            + request.pair.replace("/", "_")
            + ":"
            + request.timeframe
            + ":"
            + source_sha[:12]
        )
        correction_count = sum(1 for event in events if event.kind == "CORRECTION")
        duplicate_count = sum(1 for event in events if event.kind == "DUPLICATE")
        return ImportManifest(
            import_id=import_id,
            format_id=IMPORT_FORMAT,
            schema_version=IMPORT_SCHEMA_VERSION,
            pair=request.pair,
            timeframe=request.timeframe,
            data_class=request.data_class,
            source=request.source,
            source_version=request.source_version,
            documented_at=request.documented_at,
            timezone_basis=NORMALIZED_TIMEZONE_BASIS,
            valid_from=request.valid_from,
            valid_until=request.valid_until,
            source_sha256=source_sha,
            source_byte_count=len(text.encode("utf-8")),
            row_count=int(counts.get("records", 0)),
            bar_count=len(bars),
            quote_count=len(quotes),
            normalized_sha256=normalized_sha,
            quotes_sha256=quote_digest(quotes),
            pair_spec_version=spec.spec_version,
            pair_spec_signature=spec_signature(spec),
            catalog_version=self.catalog_manager.version,
            catalog_signature=self.catalog_manager.signature(),
            calendar_id=self.calendar.calendar_id,
            calendar_version=self.calendar.version,
            calendar_signature=self.calendar.signature(),
            first_timestamp=bars[0].timestamp.isoformat(),
            last_timestamp=bars[-1].end_at.isoformat(),
            quote_first_timestamp=quotes[0].timestamp_utc().isoformat() if quotes else "",
            quote_last_timestamp=quotes[-1].timestamp_utc().isoformat() if quotes else "",
            validation_classification=quality.classification,
            validation_passed=passed,
            error_codes=tuple(
                sorted({e.code for e in events if e.severity == SEVERITY_ERROR})
            ),
            warning_codes=tuple(
                sorted({e.code for e in events if e.severity == SEVERITY_WARNING})
            ),
            correction_count=correction_count,
            duplicate_count=duplicate_count,
            profile_id=requirements.profile_id,
            profile_version=requirements.profile_version,
            generated_at=generated_at,
        )

    @staticmethod
    def _quarantine(
        parsed: ParsedFile, issues: Iterable[ImportIssue]
    ) -> tuple[QuarantinedRecord, ...]:
        by_line: dict[int, list[str]] = {}
        for issue in issues:
            if issue.severity != SEVERITY_ERROR or issue.line <= 0:
                continue
            by_line.setdefault(issue.line, []).append(issue.code)
        return tuple(
            QuarantinedRecord(
                line=line,
                record_type=parsed.line_types.get(line, "UNKNOWN"),
                codes=tuple(sorted(set(codes))),
            )
            for line, codes in sorted(by_line.items())
        )


def _decimal(value: Any) -> tuple[Decimal | None, str | None]:
    from app.common.validation import parse_decimal

    return parse_decimal(value, "price")


def importer_for(
    *,
    catalog_manager: InstrumentCatalogManager,
    calendar: TradingCalendar,
    quote_freshness: QuoteFreshness,
    profile: ProviderProfile,
    profile_passed: bool,
    now: datetime | None = None,
) -> ForexDataImporter:
    return ForexDataImporter(
        catalog_manager=catalog_manager,
        calendar=calendar,
        quote_freshness=quote_freshness,
        profile=profile,
        profile_passed=profile_passed,
        now=now,
    )


__all__ = [
    "ForexDataImporter",
    "GAP_CODES",
    "ImportManifest",
    "ImportRequest",
    "ImportedDataset",
    "importer_for",
]
