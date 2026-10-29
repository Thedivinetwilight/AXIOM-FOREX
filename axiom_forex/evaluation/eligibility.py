"""Fail-closed data eligibility and qualification gates for research evaluation.

Rules
-----
* Synthetic fixtures are not evaluable: this workflow exists to evaluate
  user-supplied canonical imports, and a fixture must never be presented as
  market data.
* Every pair must validate its import manifest and carry the same declared data
  class; a mixed fixture/import set is rejected.
* Too few pairs, too few bars, too little calendar coverage, out-of-session
  bars, unusable data quality, or a too-short timeline allow evaluation to be
  *blocked* rather than producing a misleading result.
* Only `USER_SUPPLIED_HISTORICAL` and `USER_SUPPLIED_PRODUCTION` data may
  support a qualification decision.  Demo data can be evaluated but never
  qualifies or activates anything.
* Activation is never allowed by this workflow, in any data class.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping, Sequence

from axiom_forex.data.classes import DATA_CLASS_SYNTHETIC_FIXTURE

from .contracts import (
    EVALUATION_DATA_CLASSES,
    QUALIFICATION_DATA_CLASSES,
    QUALIFICATION_NEVER_ACTIVATES,
    REASON_ACTIVATION_NEVER_ALLOWED_IN_RESEARCH,
    REASON_DATA_CLASS_NOT_EVALUABLE,
    REASON_DEMO_DATA_NOT_QUALIFIABLE,
    REASON_SYNTHETIC_FIXTURE_NOT_EVALUABLE,
    AlignmentResult,
    DataEligibility,
    EligibilityFinding,
    EvaluationConfig,
    PairSeries,
)

ZERO = Decimal(0)
ERROR = "ERROR"
WARNING = "WARNING"


def _finding(code: str, detail: str, *, pair: str = "", severity: str = ERROR) -> EligibilityFinding:
    return EligibilityFinding(code=code, severity=severity, detail=detail, pair=pair)


def collect_term_issues(
    series: Mapping[str, PairSeries],
    *,
    calendar: Any,
    correlation_matrix: Any,
) -> tuple[EligibilityFinding, ...]:
    """Declared calendar and correlation terms must cover every evaluated pair."""

    findings: list[EligibilityFinding] = []
    instruments = set(getattr(calendar, "instruments", ()) or ())
    matrix_pairs = set(getattr(correlation_matrix, "pairs", frozenset()) or ())
    for pair in sorted(series):
        spec = series[pair].spec
        findings_pair: list[EligibilityFinding] = []
        if instruments and pair not in instruments:
            findings_pair.append(
                _finding(
                    "calendar_missing_pair",
                    "trading calendar has no declared session windows for this pair",
                    pair=pair,
                )
            )
        if getattr(spec, "trading_calendar_id", "") != getattr(calendar, "calendar_id", ""):
            findings_pair.append(
                _finding(
                    "pair_calendar_mismatch",
                    "pair spec references a different trading calendar",
                    pair=pair,
                )
            )
        if matrix_pairs and pair not in matrix_pairs:
            findings_pair.append(
                _finding(
                    "correlation_missing_pair",
                    "correlation matrix has no declared entry for this pair",
                    pair=pair,
                )
            )
        findings.extend(findings_pair)
    return tuple(findings)


#: How many concrete bad bars are listed per pair before the finding is
#: summarised, so one corrupt file cannot flood a report.
MAX_BAR_FINDINGS = 5


def analyze_bars(item: PairSeries) -> tuple[EligibilityFinding, ...]:
    """Validate the bars that are actually about to be simulated.

    ``PairSeries.quality`` is a label produced at *import* time.  It is not
    evidence about the bars now held in memory: a caller can drop, reorder,
    relabel, or rebuild a series before handing it to the evaluation.  Every
    structural rule the import contract enforces is therefore re-checked here on
    the evaluated objects -- an invalid, crossed, incomplete, stale-quoted,
    foreign, duplicated, or out-of-order bar blocks the run instead of being
    priced.

    The check is structural: a bar's last quote must fall inside its own
    interval.  The provider *staleness policy* (max quote age, feed latency) is
    still enforced at import time and is not re-invented here.
    """

    pair = item.pair
    findings: list[EligibilityFinding] = []

    declared = {str(item.data_class)}
    for name in ("import_manifest", "dataset_manifest"):
        manifest = getattr(item, name, None)
        declared_class = getattr(manifest, "data_class", None)
        if declared_class:
            declared.add(str(declared_class))
    if len(declared) > 1:
        findings.append(
            _finding(
                "data_class_provenance_mismatch",
                "declared class "
                + str(item.data_class)
                + " disagrees with its import provenance "
                + ", ".join(sorted(declared - {str(item.data_class)})),
                pair=pair,
            )
        )

    spec = item.spec
    expected_seconds = getattr(item, "timeframe_seconds", 0)
    invalid = 0
    misaligned = 0
    unordered = 0
    previous_end = None
    for index, bar in enumerate(item.bars):
        validation = bar.validate(spec)
        if not validation.valid:
            invalid += 1
            if invalid <= MAX_BAR_FINDINGS:
                findings.append(
                    _finding(
                        "invalid_bar",
                        f"bar {index}: {validation.reason}",
                        pair=pair,
                    )
                )
            continue
        if (
            isinstance(expected_seconds, int)
            and not isinstance(expected_seconds, bool)
            and expected_seconds > 0
        ):
            if bar.duration_seconds != expected_seconds:
                misaligned += 1
                if misaligned <= MAX_BAR_FINDINGS:
                    findings.append(
                        _finding(
                            "bar_timeframe_mismatch",
                            f"bar {index}: duration {bar.duration_seconds}s != "
                            f"series timeframe {expected_seconds}s",
                            pair=pair,
                        )
                    )
        if bar.timeframe != item.timeframe:
            misaligned += 1
            if misaligned <= MAX_BAR_FINDINGS:
                findings.append(
                    _finding(
                        "bar_timeframe_mismatch",
                        f"bar {index}: timeframe {bar.timeframe!r} != "
                        f"series timeframe {item.timeframe!r}",
                        pair=pair,
                    )
                )
        end = bar.end_at
        if previous_end is not None and bar.timestamp < previous_end:
            unordered += 1
            if unordered <= MAX_BAR_FINDINGS:
                findings.append(
                    _finding(
                        "bars_out_of_order",
                        f"bar {index} starts at {bar.timestamp.isoformat()} "
                        f"before the previous bar ends at {previous_end.isoformat()}",
                        pair=pair,
                    )
                )
        previous_end = max(previous_end, end) if previous_end is not None else end
    for code, count in (
        ("invalid_bar", invalid),
        ("bar_timeframe_mismatch", misaligned),
        ("bars_out_of_order", unordered),
    ):
        if count > MAX_BAR_FINDINGS:
            findings.append(
                _finding(code, f"{count} offending bar(s) in total", pair=pair)
            )
    return tuple(findings)


def collect_series_issues(
    series: Mapping[str, PairSeries],
    *,
    config: EvaluationConfig,
    alignment: AlignmentResult,
) -> tuple[EligibilityFinding, ...]:
    """Structural, provenance, and coverage findings for one evaluation set."""

    findings: list[EligibilityFinding] = []
    if not series:
        findings.append(_finding("no_pairs_supplied", "at least one pair is required"))
        return tuple(findings)
    if len(series) < config.min_pairs:
        findings.append(
            _finding(
                "insufficient_pairs",
                f"{len(series)} pair(s) supplied, {config.min_pairs} required for "
                "portfolio-level evaluation",
            )
        )
    classes = {item.data_class for item in series.values()}
    if DATA_CLASS_SYNTHETIC_FIXTURE in classes:
        findings.append(
            _finding(
                REASON_SYNTHETIC_FIXTURE_NOT_EVALUABLE,
                "synthetic fixtures cannot be evaluated as market data",
            )
        )
    unexpected = sorted(classes - EVALUATION_DATA_CLASSES)
    if unexpected:
        findings.append(
            _finding(
                REASON_DATA_CLASS_NOT_EVALUABLE,
                "data class not evaluable: " + ",".join(unexpected),
            )
        )
    if len(classes) > 1:
        findings.append(
            _finding(
                "mixed_data_classes",
                "all pairs must share one declared data class: " + ",".join(sorted(classes)),
            )
        )
    coverage_map = {coverage.pair: coverage for coverage in alignment.pairs}
    for pair in sorted(series):
        item = series[pair]
        findings.extend(analyze_bars(item))
        if not item.import_manifest_valid:
            findings.append(
                _finding(
                    "import_manifest_integrity_failed",
                    "import manifest digest does not verify",
                    pair=pair,
                )
            )
        if len(item.bars) < config.min_bars_per_pair:
            findings.append(
                _finding(
                    "insufficient_bars",
                    f"{len(item.bars)} bar(s) < {config.min_bars_per_pair}",
                    pair=pair,
                )
            )
        quality = item.quality
        if not getattr(quality, "usable", False):
            findings.append(
                _finding(
                    "data_quality_unusable",
                    f"classification={getattr(quality, 'classification', 'UNKNOWN')}",
                    pair=pair,
                )
            )
        coverage = coverage_map.get(pair)
        if coverage is None:
            findings.append(
                _finding("missing_alignment_coverage", "pair absent from aligned grid", pair=pair)
            )
            continue
        if coverage.market_open_slots == 0:
            findings.append(
                _finding("no_open_slots", "pair has no open slots on the grid", pair=pair)
            )
        if coverage.coverage_ratio < Decimal(str(config.min_coverage_ratio)):
            findings.append(
                _finding(
                    "coverage_below_threshold",
                    f"{coverage.coverage_ratio} < {config.min_coverage_ratio}",
                    pair=pair,
                )
            )
        if coverage.out_of_session_bars:
            findings.append(
                _finding(
                    "bars_outside_calendar_session",
                    f"{coverage.out_of_session_bars} bar(s) fall outside the pair calendar",
                    pair=pair,
                )
            )
        if coverage.quality_classification and coverage.quality_classification not in {
            "VALID",
            "WARNING",
        }:
            findings.append(
                _finding(
                    "quality_classification_not_usable",
                    coverage.quality_classification,
                    pair=pair,
                )
            )
    if alignment.issues:
        for issue in alignment.issues:
            findings.append(_finding("alignment_issue", str(issue)))
    if alignment.grid_slots == 0:
        findings.append(_finding("empty_alignment_grid", "no aligned slots"))
    if alignment.all_pairs_open_slots == 0 and alignment.partly_open_slots == 0:
        findings.append(
            _finding("no_shared_open_slots", "no slot is open for any pair")
        )
    if not findings:
        findings.append(
            _finding(
                "data_eligible",
                "all pairs validated, aligned, and covered",
                severity=WARNING,
            )
        )
    return tuple(findings)


def estimate_timeline_bars(
    series: Mapping[str, PairSeries], alignment: AlignmentResult
) -> int:
    """Upper bound on decision points: union of bar timestamps on the grid."""

    stamps: set[Any] = set()
    for item in series.values():
        stamps.update(bar.timestamp for bar in item.bars)
    return len(stamps)


def evaluate_eligibility(
    *,
    series: Mapping[str, PairSeries],
    config: EvaluationConfig,
    alignment: AlignmentResult,
    timeline_bars: int,
    calendar: Any | None = None,
    correlation_matrix: Any | None = None,
) -> DataEligibility:
    findings = list(collect_series_issues(series, config=config, alignment=alignment))
    if calendar is not None and correlation_matrix is not None:
        term_issues = collect_term_issues(
            series, calendar=calendar, correlation_matrix=correlation_matrix
        )
        findings = [
            finding
            for finding in findings
            if not (finding.code == "data_eligible" and term_issues)
        ]
        findings.extend(term_issues)
    reasons = tuple(sorted({finding.code for finding in findings if not finding.ok}))
    evaluation_allowed = not reasons
    classes = {item.data_class for item in series.values()}
    qualification_reasons: list[str] = []
    if not evaluation_allowed:
        qualification_reasons.append("evaluation_not_allowed")
    if classes & {DATA_CLASS_SYNTHETIC_FIXTURE}:
        qualification_reasons.append(REASON_SYNTHETIC_FIXTURE_NOT_EVALUABLE)
    if not classes.issubset(QUALIFICATION_DATA_CLASSES):
        qualification_reasons.append(REASON_DEMO_DATA_NOT_QUALIFIABLE)
    minimum_timeline = max(
        config.walk_forward_train
        + config.walk_forward_validation
        + config.walk_forward_test,
        3,
    )
    if timeline_bars < minimum_timeline:
        qualification_reasons.append("insufficient_timeline_for_qualification")
    qualification_allowed = not qualification_reasons
    coverage_map = {
        coverage.pair: coverage.coverage_ratio for coverage in alignment.pairs
    }
    return DataEligibility(
        evaluation_allowed=evaluation_allowed,
        qualification_allowed=qualification_allowed,
        activation_allowed=False,
        reasons=reasons,
        findings=tuple(findings),
        qualification_reasons=tuple(sorted(set(qualification_reasons))),
        pair_data_classes={pair: item.data_class for pair, item in series.items()},
        pair_coverage=coverage_map,
        timeline_bars=timeline_bars,
        min_bars_per_pair=config.min_bars_per_pair,
        min_coverage_ratio=Decimal(str(config.min_coverage_ratio)),
        activation_reason=REASON_ACTIVATION_NEVER_ALLOWED_IN_RESEARCH
        if evaluation_allowed
        else QUALIFICATION_NEVER_ACTIVATES,
    )


def qualification_reasons(
    *,
    eligibility: DataEligibility,
    folds: Sequence[Any],
    config: EvaluationConfig,
    total_trades: int,
    positive_ratio: Decimal,
    max_drawdown_fraction: Decimal,
) -> tuple[str, ...]:
    """Explicit reasons a strategy did *not* qualify in this evaluation."""

    reasons: list[str] = []
    if not eligibility.evaluation_allowed:
        reasons.append("evaluation_not_allowed")
    if not eligibility.qualification_allowed:
        reasons.append("data_class_not_qualifiable")
    if len(folds) < config.min_folds:
        reasons.append("insufficient_walk_forward_folds")
    if total_trades < config.min_trades:
        reasons.append("insufficient_trades")
    if positive_ratio < Decimal(str(config.min_positive_fold_ratio)):
        reasons.append("positive_fold_ratio_below_threshold")
    if max_drawdown_fraction > Decimal(str(config.max_drawdown_fraction)):
        reasons.append("drawdown_exceeds_threshold")
    return tuple(sorted(set(reasons)))
