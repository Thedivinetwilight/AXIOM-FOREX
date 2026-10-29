"""Fixture-backed AXIOM-FOREX platform facade.

All concrete values are local synthetic fixtures or explicit injection points.
The facade never reads the running system's configuration, environment,
database, broker, or credentials, and it never starts a server.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from app.accounting.fx import ConversionRateBook
from app.accounting.rollover import RolloverPolicy
from app.audit.log import make_decision_id
from app.calendar.trading import ExplicitTradingCalendar, CalendarState
from app.common.types import OrderSide, TradingMode
from app.config.settings import AccountProfile, RiskLimits, SystemState
from app.data.models import ForexQuoteBar, QuoteFreshness
from app.execution.broker import BrokerCapabilities, LocalPaperBroker
from app.risk.models import GateContext, OrderRequest
from app.risk.pretrade import GateDecision, PreTradeGate
from app.universe.specs import CurrencyPairSpec, InstrumentCatalog

from desktop.market_access import (
    MarketAccessSettings,
    MarketAccessTokenStore,
    default_vault,
)
from desktop.viewmodels import build_viewmodel
from fixtures.m1 import (
    ACCOUNT_CURRENCY,
    BASE_RATE,
    CALENDAR_ID,
    CONVERSION_SOURCE,
    CORRELATION_GROUP,
    EVALUATED_AT,
    PAIR,
    PROVIDER_ID,
    PROVIDER_SYMBOL,
    QUANTITY,
    QUOTE_RATE,
    make_account,
    make_calendar,
    make_capabilities,
    make_catalog,
    make_cost_model,
    make_entry_limit,
    make_quote_freshness,
    make_risk_limits,
    make_rollover_policy,
    make_spec,
)

from .api.wsgi import create_app
from .backtesting.engine import BacktestResult, ForexBacktestEngine
from .backtesting.validation import OOSValidation, run_oos
from .boundaries import DEFAULT_BOUNDARIES, RunningSystemBoundary
from .brokers.external import ExternalBrokerBoundary
from .brokers.local_paper import make_local_paper_broker
from .config.runtime import ForexRuntimeSettings
from .conformance.suite import ConformanceSuite
from .dashboard.view import render_dashboard
from .data.classes import (
    DATA_CLASS_SYNTHETIC_FIXTURE,
    IMPORT_DATA_CLASSES,
    data_class_view,
    is_execution_eligible,
    is_production_declared,
    is_synthetic,
)
from .data.fixtures import (
    BAR_SOURCE,
    QUOTE_SOURCE,
    SyntheticForexProvider,
    build_synthetic_bars,
    make_backtest_conversion_book,
)
from .data.pipeline import LoadedDataset
from .data.quality import QualityReport, assess_bars
from .database.audit import DurableAuditLog
from .database.repository import ForexRepository
from .database.schema import SCHEMA_VERSION
from .dependencies import OPEN_DECISIONS
from .evaluation import (
    DEFAULT_RESEARCH_SESSIONS,
    EvaluationConfig,
    ResearchEvaluationReport,
    StressScenario,
    StressSuiteReport,
    evaluation_summary,
    render_evaluation_markdown,
    render_stress_suite_markdown,
    run_research_evaluation,
    run_stress_suite,
    series_from_import,
    stress_suite_summary,
)
from .ingest import ForexDataImporter, ImportedDataset, ImportRequest, ImportResult
from .profiles.loader import load_provider_profile
from .execution.config import ExecutionConfig
from .execution.paper_engine import PaperExecutionEngine
from .execution.reservations import OrderReservationBook
from .execution.planner import ForexExecutionPlanner
from .execution.recovery import PaperStateRecovery, RecoveryResult
from .learning.lifecycle import StrategyLifecycle
from .monitoring.anomalies import detect_anomalies
from .monitoring.health import HealthSnapshot, build_health
from .portfolio.costs import ForexCostModel
from .portfolio.ledger import ForexPortfolio
from .portfolio.rollover import RolloverEngine, SwapRateCard
from .profiles.contracts import ProfileError
from .profiles.onboarding import ProviderProfileOnboarding
from .profiles.paths import DEFAULT_FIXTURE_PROFILE_PATH
from .reporting.report import render_markdown_report
from .research.datasets import DatasetManifest, build_manifest, verify_manifest
from .risk.aggregate import (
    AggregateRiskDecision,
    AggregateRiskLimits,
    CorrelationMatrix,
    check_aggregate_exposure,
)
from .strategies.base import Strategy
from .strategies.examples import SmaCrossStrategy
from .risk.context import ForexExecutionContextFactory
from .risk.entry_limits import EntryExposureLimit
from .tournament.runner import Candidate, TournamentResult, TournamentRunner
from .universe.manager import InstrumentCatalogManager
from .workflow import PaperWorkflowResult, execute_paper_workflow

PLATFORM_VERSION = "0.2.0-forex-adaptation"


@dataclass(frozen=True)
class EngineBundle:
    engine: ForexBacktestEngine
    portfolio: ForexPortfolio
    paper_execution: PaperExecutionEngine
    audit_log: DurableAuditLog
    gate: PreTradeGate
    broker: LocalPaperBroker
    context_factory: ForexExecutionContextFactory
    planner: ForexExecutionPlanner
    execution_config: ExecutionConfig


class ForexPlatform:
    """Repository-local, research/paper-only facade over the adapted layers."""

    version = PLATFORM_VERSION

    def __init__(
        self,
        *,
        runtime_settings: ForexRuntimeSettings,
        spec: CurrencyPairSpec,
        catalog: InstrumentCatalog,
        catalog_manager: InstrumentCatalogManager,
        calendar: ExplicitTradingCalendar,
        provider: SyntheticForexProvider,
        bars: tuple[ForexQuoteBar, ...],
        manifest: DatasetManifest,
        quality_report: QualityReport,
        system_state: SystemState,
        account: AccountProfile,
        risk_limits: RiskLimits,
        rollover_policy: RolloverPolicy,
        conversion_rates: ConversionRateBook,
        quote_freshness: QuoteFreshness,
        capabilities: BrokerCapabilities,
        forex_cost_model: ForexCostModel,
        external_broker_boundary: ExternalBrokerBoundary,
        repository: ForexRepository,
        boundaries: tuple[RunningSystemBoundary, ...],
        lifecycles: Mapping[str, StrategyLifecycle],
        correlation_matrix: CorrelationMatrix,
        aggregate_limits: AggregateRiskLimits,
        execution_config: ExecutionConfig,
        engine_factory: Callable[[], EngineBundle],
        loaded_dataset: LoadedDataset | None = None,
        provider_profile_path: str | Path | None = None,
        credential_vault: Any | None = None,
    ):
        settings_validation = runtime_settings.validate()
        if not settings_validation.valid:
            raise ValueError("Invalid runtime settings: " + settings_validation.reason)
        self.runtime_settings = runtime_settings
        self.spec = spec
        self.catalog = catalog
        self.catalog_manager = catalog_manager
        self.calendar = calendar
        self.provider = provider
        self.bars = bars
        self.manifest = manifest
        self.quality_report = quality_report
        self.safety_settings = runtime_settings.safety
        self.system_state = system_state
        self.account = account
        self.risk_limits = risk_limits
        self.rollover_policy = rollover_policy
        self.conversion_rates = conversion_rates
        self.quote_freshness = quote_freshness
        self.capabilities = capabilities
        self.forex_cost_model = forex_cost_model
        self.m1_cost_model = forex_cost_model
        self.external_broker_boundary = external_broker_boundary
        self.repository = repository
        self.boundaries = boundaries
        self.lifecycles = dict(lifecycles)
        self.correlation_matrix = correlation_matrix
        self.aggregate_limits = aggregate_limits
        self.execution_config = execution_config
        self.engine_factory = engine_factory
        self.loaded_dataset = loaded_dataset
        self.provider_profile_path = str(
            provider_profile_path or DEFAULT_FIXTURE_PROFILE_PATH
        )
        self.pair = spec.canonical_pair or ""
        self.provider_id = provider.provider_id
        self.provider_symbol = spec.alias_for(provider.provider_id) or self.pair
        self.quotes = tuple(getattr(provider, "quotes", ()))
        self.quote_source = QUOTE_SOURCE
        self.quote_id_prefix = "fixture-quote"
        self.data_class = (
            loaded_dataset.data_class if loaded_dataset is not None else manifest.data_class
        )
        self.data_source = manifest.source
        self.data_source_version = manifest.source_version
        self.dataset_provenance: dict[str, str] = (
            {str(key): str(value) for key, value in loaded_dataset.provenance.items()}
            if loaded_dataset is not None
            else {
                "data_class": self.data_class,
                "source": manifest.source,
                "source_version": manifest.source_version,
                "format": "SYNTHETIC_FIXTURE",
            }
        )
        self.imported_datasets: dict[str, ImportedDataset] = {}
        self._research_report: Any | None = None
        self._stress_report: Any | None = None
        self.imported_dataset: ImportedDataset | None = None
        self.recovery_result: RecoveryResult | None = None
        self._workflow_result: PaperWorkflowResult | None = None
        self._workflow_payload: dict[str, Any] | None = None
        self._cached_status: dict[str, Any] | None = None
        self._cached_health: HealthSnapshot | None = None
        self._conformance_report: dict[str, Any] | None = None
        self._provider_profile_report: dict[str, Any] | None = None
        self._bundle = engine_factory()
        # The Market Access Token setting.  The value lives in the operating
        # system vault and nowhere else; the initializer never reads it, and no
        # provider request uses it (no provider is selected in this build).
        # Its audit events go through the platform log, which writes to SQLite
        # and only caches events after commit.
        self.market_access = MarketAccessTokenStore(
            vault=credential_vault if credential_vault is not None else default_vault(),
            audit_log=self.audit_log,
        )
        self._initialize_persistence()
        self.recover_paper_state()
        self._load_provider_profile_snapshot()
        self._load_conformance_snapshot()
        self._load_workflow_snapshot()

    @property
    def engine(self) -> ForexBacktestEngine:
        return self._bundle.engine

    @property
    def portfolio(self) -> ForexPortfolio:
        return self._bundle.portfolio

    @property
    def paper_execution(self) -> PaperExecutionEngine:
        return self._bundle.paper_execution

    @property
    def audit_log(self) -> DurableAuditLog:
        return self._bundle.audit_log

    @property
    def gate(self) -> PreTradeGate:
        return self._bundle.gate

    @property
    def broker(self) -> LocalPaperBroker:
        return self._bundle.broker

    @property
    def context_factory(self) -> ForexExecutionContextFactory:
        return self._bundle.context_factory

    @property
    def planner(self) -> ForexExecutionPlanner:
        return self._bundle.planner

    @staticmethod
    def _health_from_audit_view(view: Mapping[str, Any]) -> HealthSnapshot:
        generated = view.get("generated_at")
        if isinstance(generated, str):
            try:
                generated_at = datetime.fromisoformat(generated.replace("Z", "+00:00"))
            except ValueError:
                generated_at = datetime.now(timezone.utc)
        elif isinstance(generated, datetime):
            generated_at = generated
        else:
            generated_at = datetime.now(timezone.utc)
        return HealthSnapshot(
            status=str(view.get("status", "UNKNOWN")),
            checks=tuple(dict(check) for check in view.get("checks", ())),
            research_only=bool(view.get("research_only", False)),
            live_bridge_enabled=bool(view.get("live_bridge_enabled", False)),
            generated_at=generated_at,
        )

    def _load_provider_profile_snapshot(
        self,
    ) -> None:
        """Restore the most recent durable provider-profile report, when present."""
        snapshots = self.repository.list_snapshots(kind="profile")
        if snapshots:
            self._provider_profile_report = deepcopy(snapshots[-1])
            return
        workflow_snapshots = self.repository.list_snapshots(kind="workflow")
        if not workflow_snapshots:
            return
        payload = workflow_snapshots[-1]
        status_payload = payload.get("status", payload)
        if isinstance(status_payload, Mapping):
            profile = status_payload.get("provider_profile")
            if isinstance(profile, Mapping):
                self._provider_profile_report = deepcopy(dict(profile))

    def provider_profile_report(self) -> dict[str, Any]:
        """Return the cached safe provider-profile onboarding report."""
        if self._provider_profile_report is None:
            try:
                self._provider_profile_report = ProviderProfileOnboarding(
                    self.provider_profile_path
                ).run().audit_view()
            except ProfileError as exc:
                self._provider_profile_report = {
                    "status": "FAIL",
                    "passed": False,
                    "activation_allowed": False,
                    "profile": {
                        "profile_id": "unreadable-profile",
                        "profile_version": "unknown",
                        "environment": "UNKNOWN",
                        "label": "UNREADABLE_PROFILE",
                        "synthetic": False,
                        "external_execution_enabled": False,
                        "activation_mode": "BLOCKED",
                    },
                    "validation": {
                        "status": "FAIL",
                        "passed": False,
                        "check_count": 0,
                        "error_count": 1,
                    },
                    "activation": {
                        "allowed": False,
                        "status": "BLOCKED",
                        "reason": str(exc),
                    },
                    "missing_fields": [],
                    "invalid_fields": ["profile.source"],
                    "stale_fields": [],
                    "contradictory_fields": [],
                    "compatibility_failures": [],
                    "findings": [
                        {
                            "field": "profile.source",
                            "code": "unreadable_profile",
                            "category": "invalid",
                            "detail": str(exc),
                            "ok": False,
                        }
                    ],
                    "checks": [],
                    "required_terms": [],
                    "sections": {},
                    "parse_issues": [],
                    "note": "Profile could not be loaded. Activation is blocked and no term values are shown.",
                }
        return deepcopy(self._provider_profile_report)

    def refresh_provider_profile_report(self) -> dict[str, Any]:
        """Recompute the profile report from the explicit local profile path."""
        self._provider_profile_report = None
        return self.provider_profile_report()

    def _load_conformance_snapshot(self) -> None:
        """Restore the most recent durable conformance report, when present."""
        snapshots = self.repository.list_snapshots(kind="conformance")
        if snapshots:
            self._conformance_report = deepcopy(snapshots[-1])
            return
        workflow_snapshots = self.repository.list_snapshots(kind="workflow")
        if not workflow_snapshots:
            return
        payload = workflow_snapshots[-1]
        status_payload = payload.get("status", payload)
        if isinstance(status_payload, Mapping):
            conformance = status_payload.get("conformance")
            if isinstance(conformance, Mapping):
                self._conformance_report = deepcopy(dict(conformance))

    def conformance_report(self) -> dict[str, Any]:
        """Return the cached offline conformance report, computing it once."""
        if self._conformance_report is None:
            self._conformance_report = ConformanceSuite.for_platform(self).run().audit_view()
        return deepcopy(self._conformance_report)

    def refresh_conformance_report(self) -> dict[str, Any]:
        """Recompute conformance from current explicit fixture values."""
        self._conformance_report = None
        return self.conformance_report()

    # -- imported dataset (offline research only) --------------------------
    def make_importer(self) -> ForexDataImporter:
        """Build an importer from the active profile, catalog, calendar, and policy."""

        profile = load_provider_profile(self.provider_profile_path)
        profile_report = self.provider_profile_report()
        return ForexDataImporter(
            catalog_manager=self.catalog_manager,
            calendar=self.calendar,
            quote_freshness=self.quote_freshness,
            profile=profile,
            profile_passed=bool(profile_report.get("passed", False)),
        )

    def import_dataset(
        self,
        request: ImportRequest,
        *,
        text: str | None = None,
        path: str | Path | None = None,
    ) -> ImportResult:
        """Import one user-supplied canonical dataset; never enables execution."""

        if text is None and path is None:
            raise ValueError("import_requires_text_or_path")
        if text is not None and path is not None:
            raise ValueError("import_accepts_one_source")
        if not self.runtime_settings.research_only or self.runtime_settings.live_bridge_enabled:
            raise ValueError("import_requires_research_only_mode")
        if self.external_broker_boundary.enabled:
            raise ValueError("external_broker_must_remain_disabled")
        if is_production_declared(request.data_class) and is_synthetic(self.data_class):
            raise ValueError("production_data_cannot_be_imported_into_fixture_platform")
        importer = self.make_importer()
        result = importer.import_text(text, request) if text is not None else importer.import_file(
            path, request
        )
        view = result.audit_view()
        self.repository.save_snapshot(
            f"import:{result.request.pair}:{result.request.timeframe}:{view.get('integrity', {}).get('source_sha256') or 'unknown'}"[:120],
            "import",
            view,
        )
        self.audit_log.record(
            {
                "event_type": "PAPER_PLATFORM_DATA_IMPORTED"
                if result.passed
                else "PAPER_PLATFORM_IMPORT_REJECTED",
                "status": result.status,
                "reason": result.reason,
                "data_class": view.get("data_class"),
                "source": view.get("request", {}).get("source"),
                "manifest_valid": view.get("manifest_valid", False),
                "live": False,
            }
        )
        if result.passed and result.dataset is not None:
            self.imported_datasets[result.dataset.import_manifest.import_id] = result.dataset
            self.imported_dataset = result.dataset
            self.repository.save_dataset(
                result.dataset.dataset_manifest.dataset_id,
                result.dataset.dataset_manifest.sha256,
                result.dataset.dataset_manifest.audit_view(),
            )
            self._invalidate_caches()
        return result

    def use_imported_dataset(self, import_id: str | None = None) -> ImportedDataset:
        """Adopt an imported dataset for research/backtests only.

        The pair spec and calendar must match the platform; imported data is
        never adopted into the local paper execution path.
        """

        dataset = (
            self.imported_datasets.get(import_id)
            if import_id is not None
            else self.imported_dataset
        )
        if dataset is None:
            raise ValueError("imported_dataset_unavailable")
        if dataset.pair != self.pair:
            raise ValueError("imported_pair_not_supported_by_platform")
        if dataset.timeframe not in self.provider.supported_timeframes:
            raise ValueError("imported_timeframe_not_supported_by_platform")
        loaded = dataset.as_loaded_dataset()
        self.bars = tuple(dataset.bars)
        self.quotes = tuple(dataset.quotes)
        self.spec = dataset.spec
        self.catalog = dataset.catalog
        self.provider = loaded.provider
        self.manifest = dataset.dataset_manifest
        self.quality_report = dataset.quality
        self.loaded_dataset = loaded
        self.data_class = dataset.data_class
        self.data_source = dataset.import_manifest.source
        self.data_source_version = dataset.import_manifest.source_version
        self.quote_source = dataset.import_manifest.source
        self.quote_id_prefix = (
            "import-quote:"
            + dataset.import_manifest.source_sha256[:12]
        )
        self.dataset_provenance = {
            str(key): str(value) for key, value in dataset.provenance.items()
        }
        self.imported_dataset = dataset
        self._invalidate_caches()
        self.audit_log.record(
            {
                "event_type": "PAPER_PLATFORM_IMPORTED_DATASET_ACTIVATED_FOR_RESEARCH",
                "import_id": dataset.import_manifest.import_id,
                "data_class": dataset.data_class,
                "execution_eligible": False,
                "live": False,
            }
        )
        return dataset

    def dataset_provenance_report(self) -> dict[str, Any]:
        """Safe provenance view: data class, labels, hashes, and coverage only."""

        manifest_view = self.manifest.audit_view()
        imported = self.imported_dataset is not None and (
            self.manifest.dataset_id == self.imported_dataset.dataset_manifest.dataset_id
        )
        import_manifest = (
            self.imported_dataset.import_manifest.audit_view() if imported else None
        )
        latest = self.imported_dataset
        return {
            "data_class": self.data_class,
            "data_class_view": data_class_view(self.data_class),
            "synthetic": is_synthetic(self.data_class),
            "imported": str(self.data_class) in IMPORT_DATA_CLASSES,
            "execution_eligible": is_execution_eligible(self.data_class),
            "source": self.manifest.source,
            "source_version": self.manifest.source_version,
            "timezone_basis": self.manifest.timezone_basis,
            "dataset_id": self.manifest.dataset_id,
            "dataset_manifest": manifest_view,
            "import_manifest": import_manifest,
            "import_manifest_valid": bool(imported and self.imported_dataset.verify()),
            "provenance": dict(self.dataset_provenance),
            "imported_dataset_count": len(self.imported_datasets),
            "latest_import": (
                {
                    "import_id": latest.import_manifest.import_id,
                    "data_class": latest.data_class,
                    "data_class_view": data_class_view(latest.data_class),
                    "source": latest.import_manifest.source,
                    "source_version": latest.import_manifest.source_version,
                    "pair": latest.pair,
                    "timeframe": latest.timeframe,
                    "bar_count": latest.import_manifest.bar_count,
                    "quote_count": latest.import_manifest.quote_count,
                    "validation_classification": latest.import_manifest.validation_classification,
                    "manifest_valid": latest.verify(),
                    "execution_eligible": False,
                    "adopted_for_research": imported,
                }
                if latest is not None
                else None
            ),
            "note": (
                "Synthetic fixtures are generated locally for offline tests and the local "
                "paper workflow. Imported data is user-supplied research material, is never "
                "used for paper fills, and is never relabeled as a fixture or production feed."
            ),
        }

    def _invalidate_caches(self) -> None:
        self._cached_status = None
        self._cached_health = None
        self._workflow_result = None
        self._workflow_payload = None

    def _load_workflow_snapshot(self) -> None:
        snapshots = self.repository.list_snapshots(kind="workflow")
        if not snapshots:
            return
        payload = deepcopy(snapshots[-1])
        self._workflow_payload = payload
        status_payload = payload.get("status", payload)
        if isinstance(status_payload, Mapping):
            self._cached_status = deepcopy(dict(status_payload))
            self._cached_health = self._health_from_audit_view(
                status_payload.get("health", {})
            )
            if self._conformance_report is None:
                conformance = status_payload.get("conformance")
                if isinstance(conformance, Mapping):
                    self._conformance_report = deepcopy(dict(conformance))

    def recover_paper_state(self) -> RecoveryResult | None:
        """Rebuild paper state from SQLite and rewire the canonical planner."""
        fresh = self.engine_factory()
        helper = PaperStateRecovery(self.repository, fresh.audit_log)
        if not helper.has_paper_state():
            self.recovery_result = None
            return None
        result = helper.recover(
            engine=fresh.paper_execution, portfolio=fresh.portfolio
        )
        context_factory = ForexExecutionContextFactory(
            safety_settings=self.safety_settings,
            system_state=self.system_state,
            account=self.account,
            risk_limits=self.risk_limits,
            cost_model=self.forex_cost_model,
            rollover_policy=self.rollover_policy,
            conversion_rates=self.conversion_rates,
            quote_freshness=self.quote_freshness,
            portfolio=result.portfolio,
            paper_execution=result.paper_execution,
        )
        planner = ForexExecutionPlanner(
            spec=self.spec,
            calendar=self.calendar,
            config=fresh.execution_config,
            context_factory=context_factory,
        )
        engine = replace(
            fresh.engine,
            portfolio=result.portfolio,
            paper_execution=result.paper_execution,
            planner=planner,
        )
        self._bundle = replace(
            fresh,
            engine=engine,
            portfolio=result.portfolio,
            paper_execution=result.paper_execution,
            context_factory=context_factory,
            planner=planner,
        )
        self.recovery_result = result
        return result

    def _initialize_persistence(self) -> None:
        self.repository.save_dataset(
            self.manifest.dataset_id, self.manifest.sha256, self.manifest.audit_view()
        )
        spec_rows = [
            {"pair": pair, "signature": signature, "version": self.catalog_manager.version}
            for pair, signature in self.catalog_manager.hashes.items()
        ]
        self.repository.save_specs(version=self.catalog_manager.version, specs=spec_rows)
        for strategy_id, lifecycle in self.lifecycles.items():
            self.repository.save_lifecycle(strategy_id, lifecycle.audit_view())
        self.audit_log.record(
            {
                "event_type": "PLATFORM_INITIALIZED",
                "version": self.version,
                "research_only": self.runtime_settings.research_only,
                "live_bridge_enabled": self.runtime_settings.live_bridge_enabled,
                "dataset_id": self.manifest.dataset_id,
                "calendar_signature": self.calendar.signature(),
            }
        )

    def new_engine(self) -> ForexBacktestEngine:
        """Return a clean engine without changing the current platform state."""
        return self.engine_factory().engine

    def _gate_context(self, *, at: datetime) -> GateContext:
        return self.context_factory.build(at=at, bar=self.bars[-1])

    def pretrade_check(self) -> GateDecision:
        latest_quote = self.provider.latest_quote(PROVIDER_SYMBOL)
        if latest_quote is None:
            evaluated_at = self.bars[-1].end_at
        else:
            evaluated_at = latest_quote.timestamp_utc() + timedelta(milliseconds=200)
        request = OrderRequest(
            request_id=make_decision_id(
                "pretrade-check", self.manifest.dataset_id, evaluated_at.isoformat()
            ),
            provider_id=PROVIDER_ID,
            provider_symbol=PROVIDER_SYMBOL,
            side=OrderSide.BUY,
            quantity=QUANTITY,
            target_mode=TradingMode.PAPER,
            evaluated_at=evaluated_at,
            quote=latest_quote,
            strategy_id=self.runtime_settings.strategy_id or "fixture-pretrade-check",
        )
        return self.gate.evaluate(request, self._gate_context(at=evaluated_at))

    def health(self) -> HealthSnapshot:
        if self._cached_health is not None:
            return self._cached_health
        latest_quote = self.provider.latest_quote(PROVIDER_SYMBOL)
        if latest_quote is None:
            evaluated_at = self.bars[-1].end_at
            quote_fresh = False
        else:
            evaluated_at = latest_quote.timestamp_utc() + timedelta(milliseconds=200)
            quote_fresh = self.quote_freshness.check(latest_quote, evaluated_at).ok
        calendar_state = self.calendar.status(self.spec.canonical_pair or "", evaluated_at)
        decision = self.pretrade_check()
        order_records = self.paper_execution.order_records()
        anomalies = detect_anomalies(
            audit_events=self.audit_events(limit=1000),
            order_records=order_records,
            calendar_open=calendar_state.is_open,
            quality_usable=self.quality_report.usable,
            quote_fresh=quote_fresh,
        )
        return build_health(
            settings=self.runtime_settings,
            quality_usable=self.quality_report.usable,
            calendar_open=calendar_state.is_open,
            quote_fresh=quote_fresh,
            gate_allowed=decision.allowed,
            unresolved_orders=len(self.paper_execution.unresolved_orders()),
            anomalies=anomalies,
            repository_health=self.repository.health(),
            conformance_passed=bool(self.conformance_report().get("passed", False)),
            profile_activation_allowed=bool(
                self.provider_profile_report()
                .get("activation", {})
                .get("allowed", False)
            ),
            generated_at=evaluated_at,
        )

    def status(self) -> dict[str, Any]:
        if self._cached_status is not None:
            return self._with_live_market_access(deepcopy(self._cached_status))
        at = self.bars[-1].end_at
        calendar_state: CalendarState = self.calendar.status(
            self.spec.canonical_pair or "", self.bars[-1].timestamp
        )
        snapshot = self.portfolio.snapshot(at, self.bars[-1])
        group_exposure = snapshot["correlation_group_exposures_account"].get(
            CORRELATION_GROUP, Decimal(0)
        )
        aggregate: AggregateRiskDecision = check_aggregate_exposure(
            matrix=self.correlation_matrix,
            limits=self.aggregate_limits,
            pair_exposures_account={PAIR: group_exposure},
            currency_exposures_account=snapshot["currency_exposures_account"],
        )
        if self._workflow_result is not None:
            workflow_view = self._workflow_result.summary()
        elif self._workflow_payload is not None:
            cached_status = self._workflow_payload.get(
                "status", self._workflow_payload
            )
            workflow_view = (
                deepcopy(cached_status.get("workflow", {}))
                if isinstance(cached_status, Mapping)
                else {}
            )
        else:
            workflow_view = {"workflow_id": None, "executed": False, "blocked": False}
        return {
            "version": self.version,
            "settings": self.runtime_settings.audit_view(),
            # Deliberately not keyed "..._token": the audit sanitizer redacts
            # any key that looks like a credential, which would hide the whole
            # block.  The value itself is never part of this payload anyway.
            "market_access": self.market_access_status(),
            "safety": self.safety_settings.signature(),
            "boundaries": [boundary.audit_view() for boundary in self.boundaries],
            "open_decisions": [asdict(decision) for decision in OPEN_DECISIONS],
            "health": self.health().audit_view(),
            "calendar": {
                **self.calendar.audit_view(),
                "state": calendar_state.audit_view(),
            },
            "dataset": self.manifest.audit_view(),
            "data_provenance": self.dataset_provenance_report(),
            "research_evaluation": self.research_evaluation_summary(),
            "imported_dataset": (
                self.imported_dataset.audit_view()
                if self.imported_dataset is not None
                else None
            ),
            "data_quality": self.quality_report.audit_view(),
            "portfolio": snapshot,
            "aggregate_risk": aggregate.audit_view(),
            "provider": {
                "provider_id": self.provider.provider_id,
                "source": self.provider.source,
                "supported_timeframes": sorted(self.provider.supported_timeframes),
                "bar_count": len(self.bars),
            },
            "conformance": self.conformance_report(),
            "provider_profile": self.provider_profile_report(),
            "repository": self.repository.health(),
            "backtests": self.backtests(),
            "orders": self.orders(),
            "audit_events": self.audit_events(limit=20),
            "lifecycle": [
                lifecycle.audit_view() for lifecycle in self.lifecycles.values()
            ],
            "strategy_active": any(
                lifecycle.approve_active() for lifecycle in self.lifecycles.values()
            ),
            "external_broker": self.external_broker_boundary.audit_view(),
            "data_pipeline": (
                self.loaded_dataset.audit_view()
                if self.loaded_dataset is not None
                else {"normalized": False, "reason": "workflow_not_run"}
            ),
            "recovery": (
                self.recovery_result.audit_view()
                if self.recovery_result is not None
                else {"recovered": False, "unresolved_order_count": 0}
            ),
            "workflow": workflow_view,
        }

    def backtests(self) -> list[dict[str, Any]]:
        return self.repository.list_backtests()

    def orders(self) -> list[dict[str, Any]]:
        return list(self.paper_execution.order_records())

    def audit_events(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.audit_log.recent(limit=limit)

    def dashboard_html(self) -> str:
        return render_dashboard(self.status())

    def desktop_viewmodel(self) -> dict[str, Any]:
        return build_viewmodel(self)

    def market_access_settings(self) -> MarketAccessSettings:
        """The desktop settings surface (unlock requires the operator bearer)."""

        return MarketAccessSettings(
            store=self.market_access,
            api_bearer_token=self.runtime_settings.api_bearer_token,
        )

    def market_access_status(self) -> dict[str, Any]:
        """Masked status for reports/API: whether a token exists, never its value."""

        return self.market_access.status()

    def _with_live_market_access(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Refresh the settings snapshot.

        A workflow's status is cached, but the token setting is *current* state:
        saving or clearing it must show up immediately rather than whenever the
        next workflow happens to run.  Only presence is refreshed -- the value
        is never part of the payload.
        """

        payload["market_access"] = self.market_access_status()
        return payload

    def report_markdown(self) -> str:
        markdown = render_markdown_report(self.status())
        evaluation = self.research_evaluation_markdown()
        if evaluation:
            markdown += "\n" + evaluation
        stress = self.research_evaluation_stress_markdown()
        if stress:
            markdown += "\n" + stress
        return markdown

    def workflow_status(self) -> dict[str, Any]:
        if self._workflow_result is not None:
            return self._workflow_result.audit_view()
        if self._workflow_payload is not None:
            return deepcopy(self._workflow_payload)
        return {
            "workflow_id": None,
            "executed": False,
            "blocked": False,
            "note": "No offline fixture paper workflow has been run yet.",
        }

    def run_paper_workflow(
        self, *, workflow_id: str | None = None
    ) -> PaperWorkflowResult:
        """Run and cache one durable, normalized, fixture-backed paper workflow."""
        self._cached_status = None
        self._cached_health = None
        self._workflow_result = None
        self._workflow_payload = None
        self._conformance_report = None
        self._provider_profile_report = None
        result = execute_paper_workflow(self, workflow_id=workflow_id)
        self._workflow_result = result
        self._workflow_payload = result.audit_view()
        self._cached_status = deepcopy(result.status)
        self._cached_health = self._health_from_audit_view(result.health)
        return result

    def create_wsgi_app(self) -> Callable[..., list[bytes]]:
        return create_app(self)

    def _persist_result(self, result: BacktestResult, bundle: EngineBundle) -> None:
        self.repository.save_backtest(result.audit_view())
        self.repository.save_orders(bundle.paper_execution.order_records())
        self.repository.save_fills(bundle.paper_execution.fills())
        self.repository.save_positions(result.open_positions, state="OPEN")
        for point in result.equity_curve:
            self.repository.save_snapshot(
                f"{result.run_id}:{point['index']}", "equity", point
            )
        anomalies = detect_anomalies(
            audit_events=self.audit_events(limit=1000),
            order_records=bundle.paper_execution.order_records(),
            calendar_open=True,
            quality_usable=result.quality.usable,
            quote_fresh=True,
        )
        for index, anomaly in enumerate(anomalies):
            self.repository.record_monitoring(
                make_decision_id(result.run_id, anomaly.code, str(index)),
                anomaly.code,
                anomaly.severity,
                anomaly.audit_view(),
            )

    def run_backtest(
        self, strategy: Strategy, *, run_id: str | None = None
    ) -> BacktestResult:
        bundle = self.engine_factory()
        result = bundle.engine.run(self.bars, strategy, run_id=run_id)
        self._persist_result(result, bundle)
        return result

    def run_oos(
        self,
        strategy: Strategy,
        *,
        train_bars: Sequence[ForexQuoteBar],
        validation_bars: Sequence[ForexQuoteBar],
        test_bars: Sequence[ForexQuoteBar],
    ) -> OOSValidation:
        validation = run_oos(
            self.new_engine,
            strategy,
            train_bars=train_bars,
            validation_bars=validation_bars,
            test_bars=test_bars,
        )
        self.repository.save_backtest(validation.result.audit_view())
        return validation

    def run_tournament(
        self,
        *,
        candidates: Sequence[Candidate],
        train_bars: Sequence[ForexQuoteBar],
        validation_bars: Sequence[ForexQuoteBar],
        test_bars: Sequence[ForexQuoteBar],
    ) -> TournamentResult:
        runner = TournamentRunner(
            engine_factory=self.new_engine, candidates=candidates
        )
        return runner.run(
            train_bars=train_bars,
            validation_bars=validation_bars,
            test_bars=test_bars,
            lifecycles=self.lifecycles,
        )

    def verify(self) -> dict[str, Any]:
        health = self.health()
        decision = self.pretrade_check()
        profile_report = self.provider_profile_report()
        strategy = SmaCrossStrategy(
            fast_period=3, slow_period=8, name="fixture-verification"
        )
        result = self.new_engine().run(
            self.bars, strategy, run_id="fixture-verification"
        )
        no_live_events = not any(
            event.get("live") is True for event in result.audit_events
        )
        checks = {
            "settings_valid": self.runtime_settings.validate().valid,
            "live_bridge_disabled": not self.runtime_settings.live_bridge_enabled,
            "external_broker_disabled": not self.external_broker_boundary.enabled,
            "data_quality_usable": self.quality_report.usable and result.quality.usable,
            "dataset_digest_valid": verify_manifest(self.bars, self.manifest),
            "pretrade_gate_allowed": decision.allowed,
            "paper_backtest_usable": result.usable,
            "repository_schema_current": self.repository.schema_version == SCHEMA_VERSION,
            "no_live_audit_events": no_live_events,
            "no_active_strategy": not any(
                lifecycle.approve_active() for lifecycle in self.lifecycles.values()
            ),
            "provider_broker_conformance": bool(
                self.conformance_report().get("passed", False)
            ),
            "provider_profile_onboarding": bool(
                profile_report.get("passed", False)
                and profile_report.get("activation", {}).get("allowed", False)
            ),
            "dataset_provenance_labeled": self._dataset_provenance_labeled(),
            "research_evaluation_isolated": self._research_evaluation_isolated(),
            "stress_results_non_qualifying": self._stress_results_non_qualifying(),
            # Entries need a declared, account-currency exposure cap; the
            # fixture's own cap is labeled synthetic so it can never be read as
            # an operator's risk policy.
            "entry_limit_declared": (
                self.paper_execution.reservations is not None
                and self.paper_execution.reservations.entry_limit_block_reason()
                is None
            ),
            "entry_limit_provenance_labeled": self._entry_limit_labeled(),
            # The market access setting must be vault-only, mask-only, and
            # unused: no plaintext fallback, no broker use, no request use.
            "market_access_vault_only": self._market_access_vault_only(),
        }
        return {
            "overall": all(checks.values()) and health.status != "UNSAFE",
            "checks": checks,
            "health": health.audit_view(),
            "backtest": {
                "run_id": result.run_id,
                "usable": result.usable,
                "quality": result.quality.classification,
                "trades": len(result.trades),
                "rejections": len(result.rejections),
                "net_pnl_account": result.net_pnl_account,
                "final_bar_signal_cancelled": result.final_bar_signal_cancelled,
                "open_positions": len(result.open_positions),
            },
            "entry_exposure_limit": self.entry_limit_view(),
            "market_access_setting": self.market_access_status(),
            "note": "Verification is structural and safety-oriented, not a profitability claim.",
        }

    def entry_limit_view(self) -> dict[str, Any]:
        """What the active profile says about the entry-exposure cap."""

        reservations = self.paper_execution.reservations
        if reservations is None:
            return {"configured": False, "reason": "no_durable_reservation_book"}
        view = reservations.entry_limit_view()
        view["block_reason"] = reservations.entry_limit_block_reason()
        return view

    def _market_access_vault_only(self) -> bool:
        view = self.market_access_status()
        return (
            view.get("storage") == "os_credential_vault"
            and view.get("plaintext_fallback") is False
            and view.get("used_for_requests") is False
            and view.get("broker_trading_use") is False
            and view.get("setting") == "market_access_token"
        )

    def _entry_limit_labeled(self) -> bool:
        view = self.entry_limit_view()
        if not view.get("configured"):
            # Nothing declared: the workflow refuses entries, which is the
            # fail-closed answer, so the label check is vacuously satisfied.
            return view.get("block_reason") == "ENTRY_LIMIT_NOT_CONFIGURED"
        return bool(view.get("data_class"))

    def _research_evaluation_isolated(self) -> bool:
        """Evaluation stays research-only, whether or not it has been run."""

        isolation = {
            "creates_orders": False,
            "touches_paper_order_stream": False,
            "external_execution_enabled": False,
            "paper_broker_used": False,
        }
        if any(item.execution_eligible for item in self.imported_datasets.values()):
            return False
        if self._research_report is None:
            return self.research_evaluation_summary()["execution_isolation"] == isolation
        view = self._research_report.audit_view()
        return (
            view.get("execution_isolation") == isolation
            and view.get("mode") == "RESEARCH_ONLY"
        )

    def _stress_results_non_qualifying(self) -> bool:
        """Stress output may never qualify, activate, or claim a test window."""

        if self._stress_report is None:
            return True
        if any(item.execution_eligible for item in self.imported_datasets.values()):
            return False
        for run in self._stress_report.scenarios:
            report = run.report
            if report.selection_allowed:
                return False
            if report.qualification.qualified or report.qualification.activation_allowed:
                return False
            if not report.purpose.startswith("STRESS:"):
                return False
        return True

    def _dataset_provenance_labeled(self) -> bool:
        """Data class must match the manifest, and imports must verify."""

        if self.manifest.data_class != self.data_class:
            return False
        if not str(self.manifest.source).strip():
            return False
        if is_synthetic(self.data_class):
            return not str(self.data_class) in IMPORT_DATA_CLASSES and is_execution_eligible(
                self.data_class
            )
        if self.data_class not in IMPORT_DATA_CLASSES:
            return False
        if self.imported_dataset is None:
            return False
        return bool(
            self.imported_dataset.verify()
            and self.imported_dataset.data_class == self.data_class
            and not self.imported_dataset.execution_eligible
        )

    def run_imported_backtest(
        self,
        strategy: Strategy,
        *,
        import_id: str | None = None,
        run_id: str | None = None,
    ) -> BacktestResult:
        """Backtest the imported dataset; results stay research-only."""

        dataset = self.imported_datasets.get(import_id) if import_id else self.imported_dataset
        if dataset is None:
            raise ValueError("imported_dataset_unavailable")
        if dataset.pair != self.pair:
            raise ValueError("imported_pair_not_supported_by_platform")
        imported_config = replace(
            self.execution_config,
            dataset_id=dataset.dataset_manifest.dataset_id,
            provider_id=self.provider_id,
            provider_symbol=self.provider_symbol,
        )
        bundle = self.engine_factory(imported_config)
        result = bundle.engine.run(
            dataset.bars,
            strategy,
            run_id=run_id or "import:" + dataset.import_manifest.import_id,
        )
        self._persist_result(result, bundle)
        self.repository.save_snapshot(
            "import-backtest:" + dataset.import_manifest.import_id,
            "import_backtest",
            {
                "import_manifest": dataset.import_manifest.audit_view(),
                "data_class": dataset.data_class,
                "dataset_manifest": dataset.dataset_manifest.audit_view(),
                "run_id": result.run_id,
                "usable": result.usable,
                "quality": result.quality.audit_view(),
                "trades": len(result.trades),
                "execution_eligible": False,
            },
        )
        self.audit_log.record(
            {
                "event_type": "PAPER_PLATFORM_IMPORTED_BACKTEST",
                "run_id": result.run_id,
                "data_class": dataset.data_class,
                "import_id": dataset.import_manifest.import_id,
                "execution_eligible": False,
                "live": False,
            }
        )
        return result

    # -- multi-pair research evaluation -----------------------------------
    def research_evaluation_datasets(self) -> tuple[ImportedDataset, ...]:
        """Latest canonical import per pair; never a synthetic fixture."""

        latest: dict[str, ImportedDataset] = {}
        for dataset in self.imported_datasets.values():
            current = latest.get(dataset.pair)
            if current is None or (
                dataset.import_manifest.generated_at
                > current.import_manifest.generated_at
            ):
                latest[dataset.pair] = dataset
        return tuple(latest[pair] for pair in sorted(latest))

    def make_research_evaluation_config(
        self,
        *,
        strategy_factory: Callable[[str], Strategy] | None = None,
        correlation_matrix: CorrelationMatrix | None = None,
        aggregate_limits: AggregateRiskLimits | None = None,
        sessions: Sequence[Any] | None = None,
        quantities: Mapping[str, Decimal] | None = None,
        overrides: Mapping[str, Any] | None = None,
    ) -> EvaluationConfig:
        """Build evaluation terms from the platform's explicit configuration."""

        factory = strategy_factory or (
            lambda pair: SmaCrossStrategy(
                fast_period=2, slow_period=4, name="research-evaluation-sma"
            )
        )
        pairs = sorted({item.pair for item in self.research_evaluation_datasets()})
        terms = self.forex_cost_model.terms
        swap_cards = {
            pair: SwapRateCard(
                pair=pair,
                long_rate=terms.financing_rate_daily,
                short_rate=-terms.financing_rate_daily,
                source=terms.source,
                version=terms.version,
            )
            for pair in pairs
        }
        # Research size defaults to each pair's own minimum quantity from the
        # versioned instrument spec.  It is a research assumption, reported in
        # the evaluation config, never a sizing recommendation.
        sizes = (
            {pair: Decimal(str(value)) for pair, value in quantities.items()}
            if quantities
            else {
                item.pair: item.spec.min_quantity_decimal()
                for item in self.research_evaluation_datasets()
            }
        )
        values: dict[str, Any] = {
            "strategy_id": "research-evaluation-sma",
            "strategy_factory": factory,
            "account": self.account,
            "conversion_rates": self.conversion_rates,
            "cost_terms": terms,
            "rollover_policy": self.rollover_policy,
            "swap_cards": swap_cards,
            "correlation_matrix": correlation_matrix or self.correlation_matrix,
            "aggregate_limits": aggregate_limits or self.aggregate_limits,
            "grid_seconds": self._evaluation_grid_seconds(pairs),
            "sessions": tuple(sessions or DEFAULT_RESEARCH_SESSIONS),
            "quantity_by_pair": sizes,
        }
        values.update(overrides or {})
        return EvaluationConfig(**values)

    def _evaluation_grid_seconds(self, pairs: Sequence[str]) -> int:
        """Grid step from the imported bars themselves, never a guess."""

        for pair in pairs:
            for dataset in self.imported_datasets.values():
                if dataset.pair == pair and dataset.bars:
                    return int(dataset.bars[0].duration_seconds)
        if self.bars:
            return int(self.bars[0].duration_seconds)
        raise ValueError("evaluation_grid_seconds_unavailable")

    def research_evaluation(
        self,
        *,
        config: EvaluationConfig | None = None,
        calendar: Any | None = None,
        now: datetime | None = None,
    ) -> ResearchEvaluationReport:
        """Run the multi-pair research evaluation; never places an order."""

        datasets = self.research_evaluation_datasets()
        series = {item.pair: series_from_import(item) for item in datasets}
        evaluation_config = config or self.make_research_evaluation_config()
        report = run_research_evaluation(
            series=series,
            config=evaluation_config,
            calendar=calendar or self.calendar,
            now=now or datetime.now(timezone.utc),
        )
        self._research_report = report
        return report

    def research_evaluation_stress(
        self,
        scenarios: Sequence[StressScenario],
        *,
        config: EvaluationConfig | None = None,
        calendar: Any | None = None,
        now: datetime | None = None,
        baseline: ResearchEvaluationReport | None = None,
    ) -> StressSuiteReport:
        """Rerun the pinned baseline evaluation once per explicit scenario.

        Scenarios must be supplied explicitly; nothing is invented here.  The
        suite shares the baseline split, never claims an out-of-sample window,
        and can never qualify or activate a strategy.
        """

        datasets = self.research_evaluation_datasets()
        series = {item.pair: series_from_import(item) for item in datasets}
        evaluation_config = config or self.make_research_evaluation_config()
        run_calendar = calendar or self.calendar
        run_now = now or datetime.now(timezone.utc)
        baseline_report = baseline or self._research_report or self.research_evaluation(
            config=evaluation_config, calendar=run_calendar, now=run_now
        )
        suite = run_stress_suite(
            series=series,
            config=evaluation_config,
            calendar=run_calendar,
            scenarios=scenarios,
            baseline=baseline_report,
            now=run_now,
        )
        self._research_report = suite.baseline
        self._stress_report = suite
        return suite

    def research_evaluation_stress_summary(self) -> dict[str, Any]:
        if self._stress_report is None:
            return {
                "status": "NOT_RUN",
                "mode": "HYPOTHETICAL_STRESS",
                "note": (
                    "No stress or sensitivity suite has been run in this process. "
                    "Scenarios must be configured explicitly; none are assumed."
                ),
                "execution_isolation": {
                    "creates_orders": False,
                    "touches_paper_order_stream": False,
                    "external_execution_enabled": False,
                    "paper_broker_used": False,
                    "qualifies_strategy": False,
                    "activates_strategy": False,
                    "claims_out_of_sample_window": False,
                },
            }
        return stress_suite_summary(self._stress_report)

    def research_evaluation_stress_markdown(self) -> str:
        if self._stress_report is None:
            return ""
        return render_stress_suite_markdown(self._stress_report)

    def research_evaluation_summary(self) -> dict[str, Any]:
        """Compact evaluation view for status, API, and reporting surfaces."""

        if self._research_report is None:
            return {
                "status": "NOT_RUN",
                "note": (
                    "No multi-pair research evaluation has been run in this process. "
                    "Run it explicitly with at least two imported pairs."
                ),
                "imported_pairs": [
                    item.pair for item in self.research_evaluation_datasets()
                ],
                "execution_isolation": {
                    "creates_orders": False,
                    "touches_paper_order_stream": False,
                    "external_execution_enabled": False,
                    "paper_broker_used": False,
                },
                "stress": self.research_evaluation_stress_summary(),
            }
        summary = evaluation_summary(self._research_report)
        summary["imported_pairs"] = [
            item.pair for item in self.research_evaluation_datasets()
        ]
        summary["stress"] = self.research_evaluation_stress_summary()
        return summary

    def research_evaluation_markdown(self) -> str:
        if self._research_report is None:
            return ""
        return render_evaluation_markdown(self._research_report)

    def close(self) -> None:
        self.repository.close()


_FIXTURE_ENTRY_LIMIT = object()


def build_fixture_platform(
    *,
    database_path: str = ":memory:",
    bar_count: int = 40,
    provider_profile_path: str | Path | None = None,
    entry_exposure_limit: EntryExposureLimit | None | object = _FIXTURE_ENTRY_LIMIT,
    credential_vault: Any | None = None,
) -> ForexPlatform:
    """Build a fully local deterministic platform using synthetic fixtures only.

    ``entry_exposure_limit`` defaults to the labeled synthetic fixture cap.  Pass
    ``None`` explicitly to model a profile that declares no cap: new entries are
    then refused while validated risk-reducing closes still work.
    """
    runtime_settings = ForexRuntimeSettings.for_local_fixture(
        database_path=database_path,
        data_provider_id=PROVIDER_ID,
        calendar_id=CALENDAR_ID,
        entry_exposure_limit=(
            make_entry_limit()
            if entry_exposure_limit is _FIXTURE_ENTRY_LIMIT
            else entry_exposure_limit
        ),
    )
    seed_spec = make_spec()
    calendar = make_calendar()
    catalog_manager = InstrumentCatalogManager.from_specs(
        [seed_spec], source="SYNTHETIC_INSTRUMENT_SOURCE", version="catalog-v1"
    )
    catalog = catalog_manager.catalog
    resolved_spec = catalog.get(PAIR)
    if resolved_spec is None:
        raise ValueError("fixture_pair_missing_from_versioned_catalog")
    spec = resolved_spec
    start_at = EVALUATED_AT - timedelta(minutes=10)
    bars = build_synthetic_bars(
        spec=spec, calendar=calendar, start_at=start_at, count=bar_count
    )
    provider = SyntheticForexProvider(
        bars=bars, catalog=catalog, provider_id=PROVIDER_ID
    )
    conversion_rates = make_backtest_conversion_book(
        bars,
        spec=spec,
        account_currency=ACCOUNT_CURRENCY,
        quote_rate=QUOTE_RATE,
        base_rate=BASE_RATE,
        source=CONVERSION_SOURCE,
    )
    account = make_account()
    cost_terms = make_cost_model().terms
    rollover_policy = make_rollover_policy()
    swap_card = SwapRateCard(
        pair=PAIR,
        long_rate=Decimal("0.00001"),
        short_rate=Decimal("-0.00002"),
        source="SYNTHETIC_SWAP_SOURCE",
        version="swap-v1",
    )
    rollover_engine = RolloverEngine(
        policy=rollover_policy,
        cards={PAIR: swap_card},
        conversion_rates=conversion_rates,
        account=account,
        calendar_id=CALENDAR_ID,
    )
    forex_cost_model = ForexCostModel(
        terms=cost_terms,
        spec=spec,
        account=account,
        conversion_rates=conversion_rates,
        rollover_engine=rollover_engine,
    )
    quote_freshness = make_quote_freshness()
    quality_report = assess_bars(
        bars,
        spec=spec,
        calendar=calendar,
        freshness=quote_freshness,
        evaluated_at=bars[-1].end_at,
    )
    manifest = build_manifest(
        bars,
        dataset_id="synthetic-dataset-v1",
        spec_version=spec.spec_version,
        calendar_signature=calendar.signature(),
        source=BAR_SOURCE,
        data_class=DATA_CLASS_SYNTHETIC_FIXTURE,
        timezone_basis="UTC",
        source_version="synthetic-fixture-v1",
        validation_classification=quality_report.classification,
    )
    system_state = SystemState(
        emergency_stop=False, kill_switch_active=False, execution_paused=False
    )
    risk_limits = make_risk_limits()
    capabilities = make_capabilities()
    external_broker_boundary = ExternalBrokerBoundary(capabilities=capabilities)
    repository = ForexRepository(runtime_settings.database_path)
    correlation_matrix = CorrelationMatrix(
        pairs=frozenset({PAIR}),
        values={(PAIR, PAIR): Decimal(1)},
        source="SYNTHETIC_CORRELATION_SOURCE",
        version="correlation-v1",
    )
    aggregate_limits = AggregateRiskLimits(
        max_gross_exposure_account=Decimal("1000"),
        max_adjusted_exposure_account=Decimal("1000"),
        max_abs_currency_exposure_account=Decimal("800"),
        version="aggregate-limits-v1",
    )
    lifecycles = {
        "fixture-sma-cross": StrategyLifecycle("fixture-sma-cross"),
    }
    execution_config = ExecutionConfig(
        dataset_id=manifest.dataset_id,
        quantity=QUANTITY,
        provider_id=PROVIDER_ID,
        provider_symbol=PROVIDER_SYMBOL,
        strategy_id="fixture-sma-cross",
    )

    def engine_factory(config: ExecutionConfig | None = None) -> EngineBundle:
        active_config = config if config is not None else execution_config
        audit_log = DurableAuditLog(repository=repository)
        gate = PreTradeGate(catalog, calendar, audit_log)
        broker = make_local_paper_broker(
            gate=gate, capabilities=capabilities, audit_log=audit_log
        )
        portfolio = ForexPortfolio(
            account=account,
            spec=spec,
            conversion_rates=conversion_rates,
            cost_model=forex_cost_model,
            # The broker calendar defines the trading day the daily risk
            # counters roll over on.
            calendar=calendar,
        )
        paper_execution = PaperExecutionEngine(
            broker=broker,
            portfolio=portfolio,
            audit_log=audit_log,
            # Durable reservations: close quantities and idempotency keys live
            # in the platform database, so a second engine instance (another
            # process on the same file) cannot promise the same quantity.
            #
            # The entry cap comes from the active profile, never from a default
            # invented here: without a declared limit the book refuses new
            # entries (`ENTRY_LIMIT_NOT_CONFIGURED`) while closes keep working.
            reservations=OrderReservationBook(
                repository,
                entry_limit=runtime_settings.entry_exposure_limit,
                account_currency=account.account_currency_code,
            ),
        )
        context_factory = ForexExecutionContextFactory(
            safety_settings=runtime_settings.safety,
            system_state=system_state,
            account=account,
            risk_limits=risk_limits,
            cost_model=forex_cost_model,
            rollover_policy=rollover_policy,
            conversion_rates=conversion_rates,
            quote_freshness=quote_freshness,
            portfolio=portfolio,
            paper_execution=paper_execution,
        )
        planner = ForexExecutionPlanner(
            spec=spec,
            calendar=calendar,
            config=active_config,
            context_factory=context_factory,
        )
        engine = ForexBacktestEngine(
            spec=spec,
            calendar=calendar,
            portfolio=portfolio,
            paper_execution=paper_execution,
            audit_log=audit_log,
            config=active_config,
            planner=planner,
        )
        return EngineBundle(
            engine,
            portfolio,
            paper_execution,
            audit_log,
            gate,
            broker,
            context_factory,
            planner,
            active_config,
        )

    return ForexPlatform(
        runtime_settings=runtime_settings,
        spec=spec,
        catalog=catalog,
        catalog_manager=catalog_manager,
        calendar=calendar,
        provider=provider,
        bars=bars,
        manifest=manifest,
        quality_report=quality_report,
        system_state=system_state,
        account=account,
        risk_limits=risk_limits,
        rollover_policy=rollover_policy,
        conversion_rates=conversion_rates,
        quote_freshness=quote_freshness,
        capabilities=capabilities,
        forex_cost_model=forex_cost_model,
        external_broker_boundary=external_broker_boundary,
        repository=repository,
        boundaries=DEFAULT_BOUNDARIES,
        lifecycles=lifecycles,
        correlation_matrix=correlation_matrix,
        aggregate_limits=aggregate_limits,
        execution_config=execution_config,
        engine_factory=engine_factory,
        provider_profile_path=provider_profile_path,
        credential_vault=credential_vault,
    )
