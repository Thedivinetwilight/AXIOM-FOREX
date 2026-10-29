"""Integrated multi-pair, research-only evaluation workflow.

Pipeline
--------
1. Validate the evaluation config and the imported ``PairSeries`` set.
2. Align every pair/timeframe on one UTC grid and record explicit
   missing/closed intervals per pair calendar.
3. Gate data eligibility (fixtures, demo-only, mixed classes, short timelines,
   thin coverage, out-of-session bars, unusable quality, failed manifests).
4. Run chronological train/validation/test phases on a shared multi-currency
   book with explicit costs, conversion, and shared-currency exposure.
5. Run rolling walk-forward folds using out-of-sample windows under custody so a
   test window cannot be reused.
6. Aggregate performance/risk by pair, session, regime, and portfolio, and
   produce a qualification decision that never activates a strategy.

The workflow never creates an order, never touches the paper order stream, and
never enables external execution.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping

from app.audit.log import make_decision_id
from app.calendar.trading import MarketStatus, TradingCalendar

from .alignment import build_alignment
from .book import MultiPairBook, PairTerms
from .contracts import (
    PHASE_TEST,
    PHASE_TRAIN,
    PHASE_VALIDATION,
    QUALIFICATION_NEVER_ACTIVATES,
    AlignmentResult,
    DataEligibility,
    EvaluationConfig,
    EvaluationError,
    FoldResult,
    PairSeries,
    PhaseResult,
    QualificationDecision,
    ResearchEvaluationReport,
    TestWindowReuseError,
)
from .eligibility import evaluate_eligibility, qualification_reasons
from .engine import run_phase
from .metrics import (
    aggregate_coverage,
    pair_metrics,
    portfolio_metrics,
    positive_fold_ratio,
    regime_metrics,
    session_metrics,
)
from .splits import (
    TestWindowRegistry,
    aligned_timeline,
    assert_disjoint,
    chronological_plan,
    dataset_fingerprint,
    walk_forward_plans,
    windows_overlap,
)

ZERO = Decimal(0)


def series_from_import(dataset: Any) -> PairSeries:
    """Adapt an importer ``ImportedDataset`` into an evaluable ``PairSeries``."""

    if getattr(dataset, "execution_eligible", True) is not False:
        raise EvaluationError("imported_dataset_not_research_only")
    bars = tuple(dataset.bars)
    if not bars:
        raise EvaluationError("imported_dataset_has_no_bars")
    timeframe = getattr(dataset, "timeframe", "") or bars[0].timeframe
    manifest = dataset.import_manifest
    aliases = dict(getattr(dataset.spec, "provider_aliases", {}) or {})
    provider_symbol = next(iter(aliases.values()), dataset.pair)
    return PairSeries(
        pair=dataset.pair,
        spec=dataset.spec,
        timeframe=timeframe,
        timeframe_seconds=int(bars[0].duration_seconds),
        provider_id=str(getattr(manifest, "source", "")),
        provider_symbol=str(provider_symbol),
        bars=bars,
        quotes=tuple(dataset.quotes),
        quality=dataset.quality,
        data_class=dataset.data_class,
        provenance=dict(dataset.provenance),
        import_manifest=manifest,
        dataset_manifest=dataset.dataset_manifest,
        import_manifest_valid=bool(dataset.verify()),
    )


@dataclass
class EvaluationSet:
    """One deterministic evaluation over an explicit set of imported datasets."""

    series: Mapping[str, PairSeries]
    config: EvaluationConfig
    calendar: TradingCalendar
    evaluation_id: str | None = None
    test_registry: TestWindowRegistry | None = None
    now: datetime | None = None
    #: Reuse the baseline split instead of deriving a new one.  Stress scenarios
    #: must evaluate exactly the same phases as the baseline; a scenario that
    #: shifts the windows would silently change what is being measured.
    fixed_plan: Any | None = None
    fixed_folds: tuple[Any, ...] | None = None
    #: ``False`` marks a sensitivity run: no out-of-sample window is claimed and
    #: the run can never qualify or activate anything.
    selection: bool = True
    purpose: str = "BASELINE"

    def _new_book(self, pair_terms: Mapping[str, PairTerms]) -> MultiPairBook:
        return MultiPairBook(
            account=self.config.account,
            terms=pair_terms,
            conversion_rates=self.config.conversion_rates,
            correlation_matrix=self.config.correlation_matrix,
            aggregate_limits=self.config.aggregate_limits,
        )

    def _run_window(
        self,
        window: Any,
        pair_terms: Mapping[str, PairTerms],
    ) -> PhaseResult:
        return run_phase(
            window=window,
            series=self.series,
            config=self.config,
            calendar=self.calendar,
            book=self._new_book(pair_terms),
            pair_terms=pair_terms,
            sessions=self.config.sessions,
        )

    def _pair_terms(self) -> dict[str, PairTerms]:
        from axiom_forex.portfolio.costs import ForexCostModel
        from axiom_forex.portfolio.rollover import RolloverEngine

        terms: dict[str, PairTerms] = {}
        for pair, item in self.series.items():
            rollover_engine = RolloverEngine(
                policy=self.config.rollover_policy,
                cards={pair: self.config.swap_cards[pair]},
                conversion_rates=self.config.conversion_rates,
                account=self.config.account,
                calendar_id=item.spec.trading_calendar_id,
            )
            cost_model = ForexCostModel(
                terms=self.config.cost_terms,
                spec=item.spec,
                account=self.config.account,
                conversion_rates=self.config.conversion_rates,
                rollover_engine=rollover_engine,
            )
            terms[pair] = PairTerms(
                pair=pair,
                spec=item.spec,
                cost_model=cost_model,
                rollover_cutoffs=_cutoffs(self.config, self.calendar, pair),
            )
        return terms

    def run(self) -> ResearchEvaluationReport:
        """Run once; a consumed test window is never silently re-tested."""

        config_errors = self.config.validate()
        if config_errors:
            raise EvaluationError("invalid_config:" + ",".join(config_errors))
        registry = self.test_registry or TestWindowRegistry()
        generated_at = self.now or datetime.now(timezone.utc)
        evaluation_id = self.evaluation_id or make_decision_id(
            "research-evaluation",
            self.config.strategy_id,
            ",".join(sorted(self.series)),
            str(self.config.grid_seconds),
            str(len(self.series)),
        )
        alignment = build_alignment(
            series=self.series,
            calendar=self.calendar,
            grid_seconds=self.config.grid_seconds,
        )
        timeline = aligned_timeline(self.series)
        eligibility = evaluate_eligibility(
            series=self.series,
            config=self.config,
            alignment=alignment,
            timeline_bars=len(timeline),
            calendar=self.calendar,
            correlation_matrix=self.config.correlation_matrix,
        )
        provenance = tuple(
            self.series[pair].provenance_view() for pair in sorted(self.series)
        )
        datasets = tuple(
            {
                "pair": self.series[pair].pair,
                "timeframe": self.series[pair].timeframe,
                "data_class": self.series[pair].data_class,
                "bars": len(self.series[pair].bars),
                "quotes": len(self.series[pair].quotes),
                "first_timestamp": self.series[pair].first_timestamp,
                "last_timestamp": self.series[pair].last_timestamp,
                "dataset_id": getattr(
                    self.series[pair].dataset_manifest, "dataset_id", ""
                ),
                "dataset_sha256": getattr(
                    self.series[pair].dataset_manifest, "sha256", ""
                ),
                "import_manifest_valid": self.series[pair].import_manifest_valid,
            }
            for pair in sorted(self.series)
        )
        data_classes = tuple(sorted({item.data_class for item in self.series.values()}))

        empty_portfolio = portfolio_metrics(())
        if not eligibility.evaluation_allowed:
            return ResearchEvaluationReport(
                evaluation_id=evaluation_id,
                status="BLOCKED",
                passed=False,
                reason="data_not_eligible:" + ",".join(eligibility.reasons),
                config=self.config,
                eligibility=eligibility,
                alignment=alignment,
                split={},
                phases={},
                folds=(),
                pair_metrics=(),
                session_metrics=(),
                regime_metrics=(),
                portfolio_metrics=empty_portfolio,
                qualification=QualificationDecision(
                    qualified=False,
                    activation_allowed=False,
                    status="BLOCKED",
                    reasons=eligibility.reasons,
                ),
                datasets=datasets,
                data_classes=data_classes,
                provenance=provenance,
                generated_at=generated_at,
                purpose=self.purpose,
                selection_allowed=self.selection,
            )

        try:
            plan = self.fixed_plan or chronological_plan(
                timeline,
                fractions=(
                    self.config.train_fraction,
                    self.config.validation_fraction,
                    self.config.test_fraction,
                ),
                grid_seconds=self.config.grid_seconds,
            )
        except EvaluationError as exc:
            return self._blocked(
                evaluation_id,
                generated_at,
                alignment,
                eligibility,
                datasets,
                data_classes,
                provenance,
                reason=str(exc),
            )

        # Walk-forward folds may only consume data inside the development
        # (training) segment: the chronological validation and test windows are
        # held out, evaluated once each, and never reused by a rolling fold.
        held_out = plan.phases[PHASE_TEST]
        development = plan.phases[PHASE_TRAIN]
        pre_test = tuple(stamp for stamp in timeline if stamp < development.end)
        fold_plans = (
            self.fixed_folds
            if self.fixed_folds is not None
            else walk_forward_plans(
                pre_test,
                train_size=self.config.walk_forward_train,
                validation_size=self.config.walk_forward_validation,
                test_size=self.config.walk_forward_test,
                step=self.config.walk_forward_step,
                grid_seconds=self.config.grid_seconds,
            )
        )
        for fold in fold_plans:
            for reserved in (plan.phases[PHASE_VALIDATION], held_out):
                if windows_overlap(fold.test, reserved):
                    raise EvaluationError(
                        "walk_forward_window_overlaps_held_out_window"
                    )
        if not fold_plans:
            return self._blocked(
                evaluation_id,
                generated_at,
                alignment,
                eligibility,
                datasets,
                data_classes,
                provenance,
                reason="insufficient_data_for_walk_forward",
            )

        fingerprint = dataset_fingerprint(self.series)
        # Reserve every out-of-sample window before any result is produced, so a
        # reused test window fails closed instead of being reported twice.
        # The three chronological phases are mutually disjoint; fold test
        # windows live inside the development segment and were checked above
        # against both held-out windows.
        assert_disjoint(tuple(plan.phases.values()))
        if self.selection:
            for fold in fold_plans:
                if fold.test.end > fold.test.start:
                    registry.reserve(
                        fingerprint=fingerprint,
                        start=fold.test.start,
                        end=fold.test.end,
                    )
            registry.reserve(
                fingerprint=fingerprint,
                start=held_out.start,
                end=held_out.end,
            )
            self.test_registry = registry

        pair_terms = self._pair_terms()
        phases: dict[str, PhaseResult] = {}
        for label in (PHASE_TRAIN, PHASE_VALIDATION, PHASE_TEST):
            phases[label] = self._run_window(plan.phases[label], pair_terms)

        folds: list[FoldResult] = []
        for fold in fold_plans:
            fold_phases = {
                PHASE_TRAIN: self._run_window(fold.train, pair_terms),
                PHASE_VALIDATION: self._run_window(fold.validation, pair_terms),
                PHASE_TEST: self._run_window(fold.test, pair_terms),
            }
            test_phase = fold_phases[PHASE_TEST]
            # A fold is only *conclusive* when at least one simulated trade
            # closed inside the out-of-sample window; otherwise the window shows
            # marked-to-market positions and cannot support a signal.
            conclusive = test_phase.trade_count > 0
            positive = conclusive and test_phase.net_pnl_account > 0
            reasons: list[str] = []
            if not conclusive:
                reasons.append("no_closed_trades_in_window")
            elif not positive:
                reasons.append("negative_or_flat_out_of_sample")
            folds.append(
                FoldResult(
                    fold=fold.fold,
                    train=fold_phases[PHASE_TRAIN],
                    validation=fold_phases[PHASE_VALIDATION],
                    test=test_phase,
                    test_window_key=(
                        f"{fingerprint}:{fold.test.start.isoformat()}:"
                        f"{fold.test.end.isoformat()}"
                    ),
                    positive=positive,
                    reasons=tuple(reasons),
                )
            )

        all_phases = tuple(phases.values()) + tuple(
            phase
            for fold in folds
            for phase in (fold.train, fold.validation, fold.test)
        )
        # Headline performance/risk aggregations use held-out evidence only.
        # Walk-forward fold results stay in the fold table so development-window
        # and held-out evidence are never mixed in one number.
        metric_phases = (phases[PHASE_VALIDATION], phases[PHASE_TEST])
        conclusive_folds = tuple(
            fold for fold in folds if "no_closed_trades_in_window" not in fold.reasons
        )
        ratios = positive_fold_ratio(conclusive_folds)
        total_trades = sum(len(phase.trades) for phase in metric_phases)
        max_drawdown_fraction = max(
            (phase.max_drawdown_fraction for phase in metric_phases), default=ZERO
        )
        reasons = list(
            qualification_reasons(
                eligibility=eligibility,
                folds=folds,
                config=self.config,
                total_trades=total_trades,
                positive_ratio=ratios,
                max_drawdown_fraction=max_drawdown_fraction,
            )
        )
        if not conclusive_folds:
            reasons.append("no_conclusive_walk_forward_folds")
        reasons = tuple(sorted(set(reasons)))
        qualified = not reasons
        if not self.selection:
            # A stress scenario is a sensitivity measurement, not evidence:
            # it can never qualify a strategy or feed a selection decision.
            qualified = False
            reasons = tuple(sorted(set(reasons) | {"stress_scenario_not_qualifying"}))
        qualification = QualificationDecision(
            qualified=qualified,
            activation_allowed=False,
            status=(
                "QUALIFIED_FOR_RESEARCH_REVIEW"
                if qualified
                else (
                    "SENSITIVITY_ONLY"
                    if not self.selection
                    else "NOT_QUALIFIED"
                )
            ),
            reasons=reasons
            if reasons
            else ("research_qualification_only_not_activation",),
            checks=(
                {"name": "data_class_qualifiable", "passed": eligibility.qualification_allowed},
                {"name": "walk_forward_folds", "passed": len(folds) >= self.config.min_folds},
                {"name": "conclusive_folds", "passed": bool(conclusive_folds)},
                {"name": "minimum_trades", "passed": total_trades >= self.config.min_trades},
                {
                    "name": "positive_fold_ratio",
                    "passed": ratios >= Decimal(str(self.config.min_positive_fold_ratio)),
                },
                {
                    "name": "drawdown_within_threshold",
                    "passed": max_drawdown_fraction
                    <= Decimal(str(self.config.max_drawdown_fraction)),
                },
                {"name": "activation_never_allowed", "passed": True},
            ),
            note=QUALIFICATION_NEVER_ACTIVATES,
        )
        guard_events = tuple(
            sorted({event for phase in all_phases for event in phase.guard_events})
        )
        return ResearchEvaluationReport(
            evaluation_id=evaluation_id,
            status="PASS",
            passed=True,
            reason=(
                "qualified_for_research_review"
                if qualified
                else "evaluation_complete_not_qualified"
            ),
            config=self.config,
            eligibility=eligibility,
            alignment=alignment,
            split=plan.phases,
            phases=phases,
            folds=tuple(folds),
            pair_metrics=pair_metrics(metric_phases),
            session_metrics=session_metrics(metric_phases),
            regime_metrics=regime_metrics(metric_phases),
            portfolio_metrics=portfolio_metrics(metric_phases),
            qualification=qualification,
            datasets=datasets,
            data_classes=data_classes,
            provenance=provenance,
            guard_events=guard_events,
            generated_at=generated_at,
            purpose=self.purpose,
            selection_allowed=self.selection,
        )

    def _blocked(
        self,
        evaluation_id: str,
        generated_at: datetime,
        alignment: AlignmentResult,
        eligibility: DataEligibility,
        datasets: tuple[dict[str, Any], ...],
        data_classes: tuple[str, ...],
        provenance: tuple[dict[str, Any], ...],
        *,
        reason: str,
    ) -> ResearchEvaluationReport:
        return ResearchEvaluationReport(
            evaluation_id=evaluation_id,
            status="BLOCKED",
            passed=False,
            reason=reason,
            config=self.config,
            eligibility=eligibility,
            alignment=alignment,
            split={},
            phases={},
            folds=(),
            pair_metrics=(),
            session_metrics=(),
            regime_metrics=(),
            portfolio_metrics=portfolio_metrics(()),
            qualification=QualificationDecision(
                qualified=False,
                activation_allowed=False,
                status="BLOCKED",
                reasons=(reason,),
            ),
            datasets=datasets,
            data_classes=data_classes,
            provenance=provenance,
            generated_at=generated_at,
            purpose=self.purpose,
            selection_allowed=self.selection,
        )


#: Calendar states in which no rollover can occur for the instrument.  A daily
#: break is *not* in this set: brokers roll positions at a break in quoting.
NON_ROLLOVER_STATUSES = frozenset(
    {
        MarketStatus.WEEKEND_GAP,
        MarketStatus.HOLIDAY,
        MarketStatus.NOT_YET_VALID,
        MarketStatus.CALENDAR_EXPIRED,
        MarketStatus.INSTRUMENT_UNAVAILABLE,
    }
)


def _cutoffs(
    config: EvaluationConfig, calendar: TradingCalendar, pair: str
) -> tuple[datetime, ...]:
    """Rollover cutoffs come from the explicit policy and the pair's calendar.

    The policy names one daily cutoff; the calendar says which of those days the
    instrument actually trades.  Charging a weekend or holiday cutoff *and* the
    configured triple-rollover day would finance the same closed days twice, so
    a cutoff is only accrued on a day the pair's calendar does not report as a
    weekend gap, holiday, or out-of-validity instrument.
    """

    cutoff = getattr(config.rollover_policy, "daily_cutoff_utc", None)
    valid_from = getattr(calendar, "valid_from", None)
    valid_until = getattr(calendar, "valid_until", None)
    if cutoff is None or valid_from is None or valid_until is None:
        return ()
    cutoffs: list[datetime] = []
    cursor = valid_from
    while cursor < valid_until:
        instant = datetime.combine(cursor.date(), cutoff, tzinfo=timezone.utc)
        if calendar.status(pair, instant).status not in NON_ROLLOVER_STATUSES:
            cutoffs.append(instant)
        cursor += timedelta(days=1)
    return tuple(sorted(set(cutoffs)))


def run_research_evaluation(
    *,
    series: Mapping[str, PairSeries],
    config: EvaluationConfig,
    calendar: TradingCalendar,
    evaluation_id: str | None = None,
    test_registry: TestWindowRegistry | None = None,
    now: datetime | None = None,
    fixed_plan: Any | None = None,
    fixed_folds: tuple[Any, ...] | None = None,
    selection: bool = True,
    purpose: str = "BASELINE",
) -> ResearchEvaluationReport:
    return EvaluationSet(
        series=series,
        config=config,
        calendar=calendar,
        fixed_plan=fixed_plan,
        fixed_folds=fixed_folds,
        selection=selection,
        purpose=purpose,
        evaluation_id=evaluation_id,
        test_registry=test_registry,
        now=now,
    ).run()


__all__ = [
    "EvaluationSet",
    "TestWindowReuseError",
    "aggregate_coverage",
    "run_research_evaluation",
    "series_from_import",
]
