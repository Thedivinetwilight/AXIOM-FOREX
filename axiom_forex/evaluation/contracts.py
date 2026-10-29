"""Contracts for the multi-pair, research-only evaluation workflow.

This package evaluates canonical *imported* bid/ask datasets across several
pairs and timeframes on one aligned UTC grid.  It is deliberately separate from
paper execution: it never creates an order record, never touches the paper order
stream, never uses the local paper broker, and never enables external
execution.  Every result carries the dataset class and provenance of the data it
was computed from.

Nothing here is a profitability or live-readiness claim.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Callable, Mapping, Sequence

from app.audit.log import sanitize_audit_value
from app.universe.specs import CurrencyPairSpec

from axiom_forex.data.classes import (
    ALL_DATA_CLASSES,
    DATA_CLASS_USER_SUPPLIED_DEMO,
    DATA_CLASS_USER_SUPPLIED_HISTORICAL,
    DATA_CLASS_USER_SUPPLIED_PRODUCTION,
    data_class_view,
)

EVALUATION_FORMAT = "AXIOM_FOREX_RESEARCH_EVALUATION"
EVALUATION_SCHEMA_VERSION = "1.0.0"
EVALUATION_MODE = "RESEARCH_ONLY"

#: Data classes that may be evaluated for research.  Fixtures are excluded: the
#: workflow exists to evaluate user-supplied canonical imports.
EVALUATION_DATA_CLASSES = frozenset(
    {
        DATA_CLASS_USER_SUPPLIED_DEMO,
        DATA_CLASS_USER_SUPPLIED_HISTORICAL,
        DATA_CLASS_USER_SUPPLIED_PRODUCTION,
    }
)

#: Data classes that may support a *qualification* decision.  Demo data is
#: deliberately excluded: a demonstration export can be evaluated but can never
#: qualify or activate a strategy.
QUALIFICATION_DATA_CLASSES = frozenset(
    {
        DATA_CLASS_USER_SUPPLIED_HISTORICAL,
        DATA_CLASS_USER_SUPPLIED_PRODUCTION,
    }
)

PHASE_TRAIN = "TRAIN"
PHASE_VALIDATION = "VALIDATION"
PHASE_TEST = "TEST"
PHASES = (PHASE_TRAIN, PHASE_VALIDATION, PHASE_TEST)

SESSION_UNSPECIFIED = "UNSPECIFIED"
REGIME_UNSPECIFIED = "UNSPECIFIED"


#: Explicit reason codes used by the eligibility and qualification gates.
REASON_SYNTHETIC_FIXTURE_NOT_EVALUABLE = "synthetic_fixture_not_evaluable"
REASON_DATA_CLASS_NOT_EVALUABLE = "data_class_not_evaluable"
REASON_DEMO_DATA_NOT_QUALIFIABLE = "demo_data_not_qualifiable"
REASON_ACTIVATION_NEVER_ALLOWED_IN_RESEARCH = "activation_never_allowed_in_research"

QUALIFICATION_NEVER_ACTIVATES = "research_evaluation_never_activates_a_strategy"


class EvaluationError(Exception):
    """Raised when the evaluation inputs cannot be used at all."""


class LookAheadError(EvaluationError):
    """Raised when a fill or observation would use future information."""


class TestWindowReuseError(EvaluationError):
    """Raised when a previously consumed out-of-sample window is reused."""


@dataclass(frozen=True)
class EvaluationConfig:
    """Explicit evaluation terms; no provider, cost, or session default is assumed.

    ``strategy_factory`` receives a pair name and must return a fresh strategy
    instance so per-pair state cannot leak between pairs or folds.
    """

    strategy_id: str
    strategy_factory: Callable[[str], Any]
    account: Any
    conversion_rates: Any
    cost_terms: Any
    rollover_policy: Any
    swap_cards: Mapping[str, Any]
    correlation_matrix: Any
    aggregate_limits: Any
    grid_seconds: int = 60
    quantity_by_pair: Mapping[str, Decimal] = field(default_factory=dict)
    default_quantity: Decimal | None = None
    allow_short: bool = True
    max_open_positions: int = 2
    train_fraction: Decimal = Decimal("0.6")
    validation_fraction: Decimal = Decimal("0.2")
    test_fraction: Decimal = Decimal("0.2")
    walk_forward_train: int = 40
    walk_forward_validation: int = 10
    walk_forward_test: int = 10
    walk_forward_step: int | None = None
    min_pairs: int = 2
    min_bars_per_pair: int = 60
    min_coverage_ratio: Decimal = Decimal("0.90")
    min_folds: int = 1
    min_trades: int = 1
    min_positive_fold_ratio: Decimal = Decimal("0.60")
    max_drawdown_fraction: Decimal = Decimal("0.50")
    sessions: Sequence[Any] = ()
    final_bar_signal_cancelled: bool = True
    version: str = "evaluation-config-v1"

    def validate(self) -> tuple[str, ...]:
        errors: list[str] = []
        if not str(self.strategy_id).strip():
            errors.append("missing_strategy_id")
        if not callable(self.strategy_factory):
            errors.append("strategy_factory_not_callable")
        for name in (
            "account",
            "conversion_rates",
            "cost_terms",
            "rollover_policy",
            "correlation_matrix",
            "aggregate_limits",
        ):
            if getattr(self, name, None) is None:
                errors.append(f"missing_{name}")
        if not self.swap_cards:
            errors.append("missing_swap_cards")
        if not isinstance(self.grid_seconds, int) or isinstance(self.grid_seconds, bool):
            errors.append("invalid_grid_seconds")
        elif self.grid_seconds <= 0:
            errors.append("invalid_grid_seconds")
        fractions = (self.train_fraction, self.validation_fraction, self.test_fraction)
        try:
            total = sum((Decimal(str(value)) for value in fractions), Decimal(0))
        except Exception:
            return tuple(errors + ["invalid_split_fractions"])
        if total != Decimal(1):
            errors.append("split_fractions_must_sum_to_one")
        if any(Decimal(str(value)) <= 0 for value in fractions):
            errors.append("split_fractions_must_be_positive")
        for name in ("walk_forward_train", "walk_forward_validation", "walk_forward_test"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                errors.append(f"invalid_{name}")
        if self.walk_forward_step is not None and (
            not isinstance(self.walk_forward_step, int) or self.walk_forward_step <= 0
        ):
            errors.append("invalid_walk_forward_step")
        if self.min_pairs < 1:
            errors.append("invalid_min_pairs")
        if self.min_bars_per_pair < 1:
            errors.append("invalid_min_bars_per_pair")
        if Decimal(str(self.min_coverage_ratio)) <= 0:
            errors.append("invalid_min_coverage_ratio")
        if self.max_open_positions < 1:
            errors.append("invalid_max_open_positions")
        if self.default_quantity is None and not self.quantity_by_pair:
            errors.append("missing_quantity")
        return tuple(errors)

    def quantity_for(self, pair: str) -> Decimal:
        if pair in self.quantity_by_pair:
            return Decimal(str(self.quantity_by_pair[pair]))
        if self.default_quantity is None:
            raise EvaluationError("missing_quantity_for_pair")
        return Decimal(str(self.default_quantity))

    def audit_view(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "strategy_id": self.strategy_id,
            "grid_seconds": self.grid_seconds,
            "quantity_by_pair": {
                str(pair): str(value) for pair, value in self.quantity_by_pair.items()
            },
            "default_quantity": None
            if self.default_quantity is None
            else str(self.default_quantity),
            "allow_short": self.allow_short,
            "max_open_positions": self.max_open_positions,
            "split_fractions": {
                PHASE_TRAIN: str(self.train_fraction),
                PHASE_VALIDATION: str(self.validation_fraction),
                PHASE_TEST: str(self.test_fraction),
            },
            "walk_forward": {
                "train": self.walk_forward_train,
                "validation": self.walk_forward_validation,
                "test": self.walk_forward_test,
                "step": self.walk_forward_step,
            },
            "thresholds": {
                "min_pairs": self.min_pairs,
                "min_bars_per_pair": self.min_bars_per_pair,
                "min_coverage_ratio": str(self.min_coverage_ratio),
                "min_folds": self.min_folds,
                "min_trades": self.min_trades,
                "min_positive_fold_ratio": str(self.min_positive_fold_ratio),
                "max_drawdown_fraction": str(self.max_drawdown_fraction),
            },
            "sessions": [
                session.audit_view()
                for session in self.sessions
                if hasattr(session, "audit_view")
            ],
            "final_bar_signal_cancelled": self.final_bar_signal_cancelled,
        }


@dataclass(frozen=True)
class PairSeries:
    """One imported pair/timeframe dataset prepared for aligned evaluation."""

    pair: str
    spec: CurrencyPairSpec
    timeframe: str
    timeframe_seconds: int
    provider_id: str
    provider_symbol: str
    bars: tuple[Any, ...]
    quotes: tuple[Any, ...]
    quality: Any
    data_class: str
    provenance: Mapping[str, str]
    import_manifest: Any
    dataset_manifest: Any
    import_manifest_valid: bool

    @property
    def first_timestamp(self) -> datetime | None:
        """``None`` when the series carries no bars (e.g. a stress derivation)."""

        return self.bars[0].timestamp if self.bars else None

    @property
    def last_timestamp(self) -> datetime | None:
        return self.bars[-1].end_at if self.bars else None

    def bars_between(self, start: datetime, end: datetime) -> tuple[Any, ...]:
        return tuple(
            bar for bar in self.bars if bar.timestamp >= start and bar.end_at <= end
        )

    def provenance_view(self) -> dict[str, Any]:
        manifest = getattr(self.dataset_manifest, "audit_view", None)
        dataset_view = manifest() if callable(manifest) else {}
        import_view = getattr(self.import_manifest, "audit_view", None)
        payload = {
            "pair": self.pair,
            "timeframe": self.timeframe,
            "data_class": self.data_class,
            "data_class_view": data_class_view(self.data_class),
            "source": self.provenance.get("source", ""),
            "source_version": self.provenance.get("source_version", ""),
            "documented_at": self.provenance.get("documented_at", ""),
            "import_id": self.provenance.get("import_id", ""),
            "dataset_id": dataset_view.get("dataset_id", ""),
            "dataset_sha256": dataset_view.get("sha256", ""),
            "source_sha256": (
                import_view().get("source_sha256", "") if callable(import_view) else ""
            ),
            "timezone_basis": dataset_view.get("timezone_basis", ""),
            "spec_version": self.spec.spec_version,
            "import_manifest_valid": self.import_manifest_valid,
        }
        return payload


@dataclass(frozen=True)
class SessionSpec:
    """Explicit named UTC session window; wraps midnight when end < start."""

    name: str
    start_seconds: int
    end_seconds: int

    def contains(self, at: datetime) -> bool:
        seconds = at.hour * 3600 + at.minute * 60 + at.second
        if self.start_seconds == self.end_seconds:
            return True
        if self.start_seconds < self.end_seconds:
            return self.start_seconds <= seconds < self.end_seconds
        return seconds >= self.start_seconds or seconds < self.end_seconds

    def validate(self) -> tuple[str, ...]:
        errors: list[str] = []
        if not str(self.name).strip():
            errors.append("missing_session_name")
        for name in ("start_seconds", "end_seconds"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool):
                errors.append(f"invalid_{name}")
            elif not 0 <= value < 86400:
                errors.append(f"invalid_{name}")
        return tuple(errors)

    def audit_view(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "start_seconds": self.start_seconds,
            "end_seconds": self.end_seconds,
        }


def session_for(at: datetime, sessions: Sequence[SessionSpec]) -> str:
    for session in sessions:
        if session.contains(at):
            return session.name
    return SESSION_UNSPECIFIED


@dataclass(frozen=True)
class CoverageInterval:
    """A contiguous run of aligned slots with the same coverage state."""

    start: datetime
    end: datetime
    slots: int
    state: str

    def audit_view(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "slots": self.slots,
            "state": self.state,
        }


@dataclass(frozen=True)
class PairCoverage:
    """Per-pair aligned-grid coverage and explicit missing/closed intervals."""

    pair: str
    timeframe: str
    grid_seconds: int
    grid_slots: int
    market_open_slots: int
    covered_slots: int
    missing_slots: int
    closed_slots: int
    coverage_ratio: Decimal
    first_covered_at: datetime | None
    last_covered_at: datetime | None
    missing_intervals: tuple[CoverageInterval, ...] = ()
    closed_intervals: tuple[CoverageInterval, ...] = ()
    out_of_session_bars: int = 0
    quality_classification: str = ""
    quality_findings: tuple[dict[str, Any], ...] = ()

    def audit_view(self) -> dict[str, Any]:
        return {
            "pair": self.pair,
            "timeframe": self.timeframe,
            "grid_seconds": self.grid_seconds,
            "grid_slots": self.grid_slots,
            "market_open_slots": self.market_open_slots,
            "covered_slots": self.covered_slots,
            "missing_slots": self.missing_slots,
            "closed_slots": self.closed_slots,
            "coverage_ratio": self.coverage_ratio,
            "first_covered_at": self.first_covered_at,
            "last_covered_at": self.last_covered_at,
            "missing_interval_count": len(self.missing_intervals),
            "missing_intervals": [item.audit_view() for item in self.missing_intervals],
            "closed_interval_count": len(self.closed_intervals),
            "closed_intervals": [item.audit_view() for item in self.closed_intervals],
            "out_of_session_bars": self.out_of_session_bars,
            "quality_classification": self.quality_classification,
            "quality_findings": [dict(item) for item in self.quality_findings],
        }


@dataclass(frozen=True)
class AlignmentResult:
    grid_seconds: int
    grid_start: datetime | None
    grid_end: datetime | None
    grid_slots: int
    aligned_slots: int
    all_pairs_open_slots: int
    partly_open_slots: int
    closed_slots: int
    pairs: tuple[PairCoverage, ...]
    issues: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.issues and bool(self.pairs)

    def audit_view(self) -> dict[str, Any]:
        return {
            "grid_seconds": self.grid_seconds,
            "grid_start": self.grid_start,
            "grid_end": self.grid_end,
            "grid_slots": self.grid_slots,
            "aligned_slots": self.aligned_slots,
            "all_pairs_open_slots": self.all_pairs_open_slots,
            "partly_open_slots": self.partly_open_slots,
            "closed_slots": self.closed_slots,
            "pairs": [coverage.audit_view() for coverage in self.pairs],
            "issues": list(self.issues),
        }


@dataclass(frozen=True)
class EligibilityFinding:
    code: str
    severity: str
    detail: str
    pair: str = ""

    @property
    def ok(self) -> bool:
        return self.severity != "ERROR"

    def audit_view(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "detail": self.detail,
            "pair": self.pair,
            "ok": self.ok,
        }


@dataclass(frozen=True)
class DataEligibility:
    evaluation_allowed: bool
    qualification_allowed: bool
    activation_allowed: bool
    reasons: tuple[str, ...]
    findings: tuple[EligibilityFinding, ...]
    pair_data_classes: Mapping[str, str]
    pair_coverage: Mapping[str, Decimal]
    timeline_bars: int
    min_bars_per_pair: int
    min_coverage_ratio: Decimal
    qualification_reasons: tuple[str, ...] = ()
    activation_reason: str = QUALIFICATION_NEVER_ACTIVATES

    @property
    def errors(self) -> tuple[EligibilityFinding, ...]:
        return tuple(finding for finding in self.findings if not finding.ok)

    def audit_view(self) -> dict[str, Any]:
        return {
            "evaluation_allowed": self.evaluation_allowed,
            "qualification_allowed": self.qualification_allowed,
            "activation_allowed": self.activation_allowed,
            "activation_reason": self.activation_reason,
            "reasons": list(self.reasons),
            "qualification_reasons": list(self.qualification_reasons),
            "error_count": len(self.errors),
            "findings": [finding.audit_view() for finding in self.findings],
            "pair_data_classes": {
                str(pair): str(value) for pair, value in self.pair_data_classes.items()
            },
            "pair_coverage": {
                str(pair): str(value) for pair, value in self.pair_coverage.items()
            },
            "timeline_bars": self.timeline_bars,
            "thresholds": {
                "min_bars_per_pair": self.min_bars_per_pair,
                "min_coverage_ratio": str(self.min_coverage_ratio),
            },
        }


@dataclass(frozen=True)
class PhaseWindow:
    label: str
    start: datetime
    end: datetime
    fold: int = 0

    def audit_view(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "start": self.start,
            "end": self.end,
            "fold": self.fold,
            "duration_seconds": (self.end - self.start).total_seconds(),
        }


@dataclass(frozen=True)
class EquityPoint:
    at: datetime
    equity_account: Decimal
    gross_exposure_account: Decimal
    adjusted_exposure_account: Decimal | None
    margin_account: Decimal
    max_currency_exposure_account: Decimal
    open_positions: int

    def audit_view(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "equity_account": self.equity_account,
            "gross_exposure_account": self.gross_exposure_account,
            "adjusted_exposure_account": self.adjusted_exposure_account,
            "margin_account": self.margin_account,
            "max_currency_exposure_account": self.max_currency_exposure_account,
            "open_positions": self.open_positions,
        }


@dataclass(frozen=True)
class TradeRecord:
    trade_id: str
    pair: str
    side: str
    quantity: Decimal
    base_units: Decimal
    entry_price: Decimal
    exit_price: Decimal
    entry_at: datetime
    exit_at: datetime
    decision_index: int
    entry_session: str
    entry_regime: str
    notional_account: Decimal
    exit_notional_account: Decimal
    gross_pnl_account: Decimal
    costs_account: Decimal
    net_pnl_account: Decimal
    holding_seconds: Decimal
    data_class: str

    def audit_view(self) -> dict[str, Any]:
        return {
            "trade_id": self.trade_id,
            "pair": self.pair,
            "side": self.side,
            "quantity": self.quantity,
            "base_units": self.base_units,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "entry_at": self.entry_at,
            "exit_at": self.exit_at,
            "decision_index": self.decision_index,
            "entry_session": self.entry_session,
            "entry_regime": self.entry_regime,
            "notional_account": self.notional_account,
            "exit_notional_account": self.exit_notional_account,
            "gross_pnl_account": self.gross_pnl_account,
            "costs_account": self.costs_account,
            "net_pnl_account": self.net_pnl_account,
            "holding_seconds": self.holding_seconds,
            "data_class": self.data_class,
        }


@dataclass(frozen=True)
class PhaseResult:
    window: PhaseWindow
    initial_equity_account: Decimal
    final_equity_account: Decimal
    realized_pnl_account: Decimal
    unrealized_pnl_account: Decimal
    net_pnl_account: Decimal
    costs_account: Decimal
    turnover_account: Decimal
    turnover_ratio: Decimal
    max_drawdown_account: Decimal
    max_drawdown_fraction: Decimal
    max_gross_exposure_account: Decimal
    max_adjusted_exposure_account: Decimal
    max_currency_exposure_account: Decimal
    peak_currency_exposure: Mapping[str, Decimal]
    trades: tuple[TradeRecord, ...]
    equity_curve: tuple[EquityPoint, ...]
    rejections: tuple[dict[str, Any], ...]
    signals: tuple[dict[str, Any], ...]
    open_positions: tuple[dict[str, Any], ...]
    pair_summary: Mapping[str, dict[str, Any]]
    coverage: tuple[PairCoverage, ...]
    cancelled_final_signals: int = 0
    guard_events: tuple[str, ...] = ()

    @property
    def trade_count(self) -> int:
        return len(self.trades)

    def audit_view(self, *, include_curve: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "window": self.window.audit_view(),
            "initial_equity_account": self.initial_equity_account,
            "final_equity_account": self.final_equity_account,
            "realized_pnl_account": self.realized_pnl_account,
            "unrealized_pnl_account": self.unrealized_pnl_account,
            "net_pnl_account": self.net_pnl_account,
            "costs_account": self.costs_account,
            "turnover_account": self.turnover_account,
            "turnover_ratio": self.turnover_ratio,
            "max_drawdown_account": self.max_drawdown_account,
            "max_drawdown_fraction": self.max_drawdown_fraction,
            "max_gross_exposure_account": self.max_gross_exposure_account,
            "max_adjusted_exposure_account": self.max_adjusted_exposure_account,
            "max_currency_exposure_account": self.max_currency_exposure_account,
            "peak_currency_exposure": {
                str(currency): value
                for currency, value in self.peak_currency_exposure.items()
            },
            "trade_count": self.trade_count,
            "trades": [trade.audit_view() for trade in self.trades],
            "rejection_count": len(self.rejections),
            "rejections": [dict(item) for item in self.rejections],
            "signal_count": len(self.signals),
            "open_positions": [dict(item) for item in self.open_positions],
            "pair_summary": {
                str(pair): dict(summary)
                for pair, summary in self.pair_summary.items()
            },
            "coverage": [item.audit_view() for item in self.coverage],
            "cancelled_final_signals": self.cancelled_final_signals,
            "guard_events": list(self.guard_events),
        }
        if include_curve:
            payload["equity_curve"] = [point.audit_view() for point in self.equity_curve]
        clean = sanitize_audit_value(payload)
        return clean if isinstance(clean, dict) else {"window": self.window.audit_view()}


@dataclass(frozen=True)
class GroupMetric:
    """Performance/risk for one pair, session, regime, or the whole portfolio."""

    key: str
    kind: str
    trades: int
    wins: int
    losses: int
    win_ratio: Decimal
    gross_pnl_account: Decimal
    costs_account: Decimal
    net_pnl_account: Decimal
    turnover_account: Decimal
    max_drawdown_account: Decimal
    max_drawdown_fraction: Decimal
    exposure_account: Decimal
    holding_seconds: Decimal

    def audit_view(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "kind": self.kind,
            "trades": self.trades,
            "wins": self.wins,
            "losses": self.losses,
            "win_ratio": self.win_ratio,
            "gross_pnl_account": self.gross_pnl_account,
            "costs_account": self.costs_account,
            "net_pnl_account": self.net_pnl_account,
            "turnover_account": self.turnover_account,
            "max_drawdown_account": self.max_drawdown_account,
            "max_drawdown_fraction": self.max_drawdown_fraction,
            "exposure_account": self.exposure_account,
            "holding_seconds": self.holding_seconds,
        }


@dataclass(frozen=True)
class FoldResult:
    fold: int
    train: PhaseResult
    validation: PhaseResult
    test: PhaseResult
    test_window_key: str
    positive: bool
    reasons: tuple[str, ...] = ()

    def audit_view(self) -> dict[str, Any]:
        return {
            "fold": self.fold,
            "train": self.train.audit_view(),
            "validation": self.validation.audit_view(),
            "test": self.test.audit_view(),
            "test_window_key": self.test_window_key,
            "positive": self.positive,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class QualificationDecision:
    qualified: bool
    activation_allowed: bool
    status: str
    reasons: tuple[str, ...]
    checks: tuple[dict[str, Any], ...] = ()
    note: str = QUALIFICATION_NEVER_ACTIVATES

    def audit_view(self) -> dict[str, Any]:
        return {
            "qualified": self.qualified,
            "activation_allowed": self.activation_allowed,
            "status": self.status,
            "reasons": list(self.reasons),
            "checks": [dict(check) for check in self.checks],
            "note": self.note,
        }


@dataclass(frozen=True)
class ResearchEvaluationReport:
    evaluation_id: str
    status: str
    passed: bool
    reason: str
    config: EvaluationConfig
    eligibility: DataEligibility
    alignment: AlignmentResult
    split: Mapping[str, PhaseWindow]
    phases: Mapping[str, PhaseResult]
    folds: tuple[FoldResult, ...]
    pair_metrics: tuple[GroupMetric, ...]
    session_metrics: tuple[GroupMetric, ...]
    regime_metrics: tuple[GroupMetric, ...]
    portfolio_metrics: GroupMetric
    qualification: QualificationDecision
    datasets: tuple[dict[str, Any], ...]
    data_classes: tuple[str, ...]
    provenance: tuple[dict[str, Any], ...]
    guard_events: tuple[str, ...] = ()
    generated_at: datetime | None = None
    #: ``BASELINE`` for a normal evaluation; ``STRESS:<scenario_id>`` for a
    #: sensitivity rerun that shares the baseline split.
    purpose: str = "BASELINE"
    #: Only a selection-capable run may claim out-of-sample windows or qualify.
    selection_allowed: bool = True
    note: str = (
        "Research-only portfolio evaluation of imported data. It creates no orders, "
        "does not touch the paper order stream, never enables external execution, and "
        "is not a profitability or live-readiness claim."
    )

    @property
    def evaluation_data_class(self) -> str:
        unique = sorted(set(self.data_classes))
        if not unique:
            return "UNSPECIFIED"
        return unique[0] if len(unique) == 1 else "MIXED"

    def audit_view(self) -> dict[str, Any]:
        payload = {
            "format": EVALUATION_FORMAT,
            "schema_version": EVALUATION_SCHEMA_VERSION,
            "mode": EVALUATION_MODE,
            "evaluation_id": self.evaluation_id,
            "status": self.status,
            "passed": self.passed,
            "reason": self.reason,
            "generated_at": self.generated_at,
            "data_class": self.evaluation_data_class,
            "data_class_view": data_class_view(self.evaluation_data_class)
            if self.evaluation_data_class in ALL_DATA_CLASSES
            else {"recognized": False, "data_class": self.evaluation_data_class},
            "config": self.config.audit_view(),
            "eligibility": self.eligibility.audit_view(),
            "alignment": self.alignment.audit_view(),
            "split": {
                str(label): window.audit_view() for label, window in self.split.items()
            },
            "phases": {
                str(label): phase.audit_view() for label, phase in self.phases.items()
            },
            "folds": [fold.audit_view() for fold in self.folds],
            "pair_metrics": [metric.audit_view() for metric in self.pair_metrics],
            "session_metrics": [metric.audit_view() for metric in self.session_metrics],
            "regime_metrics": [metric.audit_view() for metric in self.regime_metrics],
            "portfolio_metrics": self.portfolio_metrics.audit_view(),
            "qualification": self.qualification.audit_view(),
            "datasets": [dict(item) for item in self.datasets],
            "provenance": [dict(item) for item in self.provenance],
            "guard_events": list(self.guard_events),
            "purpose": self.purpose,
            "selection_allowed": self.selection_allowed,
            "execution_isolation": {
                "creates_orders": False,
                "touches_paper_order_stream": False,
                "external_execution_enabled": False,
                "paper_broker_used": False,
            },
            "note": self.note,
        }
        clean = sanitize_audit_value(payload)
        return clean if isinstance(clean, dict) else {"status": self.status}


def phase_seconds(grid_seconds: int) -> timedelta:
    return timedelta(seconds=grid_seconds)

#: UTC research session buckets.  These are analysis windows for reporting, not
#: broker sessions: real trading hours always come from the declared calendar.
DEFAULT_RESEARCH_SESSIONS = (
    SessionSpec("ASIA", 0, 7 * 3600),
    SessionSpec("LONDON", 7 * 3600, 12 * 3600),
    SessionSpec("NEW_YORK", 12 * 3600, 21 * 3600),
    SessionSpec("LATE", 21 * 3600, 0),
)
