"""One durable, normalized, fixture-backed paper workflow through AXIOM layers.

This module orchestrates the canonical data pipeline, versioned specs, OOS
qualification, strategy routing, M1 gate context, local paper broker,
persistence, monitoring, and one immutable operator snapshot.  It contains no
network client, credential access, live mode, or real-order path.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from app.audit.log import make_decision_id, sanitize_audit_value
from app.common.types import OrderSide

from fixtures.m1 import PAIR, PROVIDER_ID, PROVIDER_SYMBOL

from .backtesting.validation import run_oos
from .data.classes import is_execution_eligible
from .data.fixtures import (
    BAR_SOURCE,
    QUOTE_SOURCE,
    synthetic_bar_payloads,
    synthetic_quote_payloads,
)
from .data.pipeline import ForexDataPipeline
from .execution.planner import ExecutionPlan, ExecutionRejection
from .learning.qualification import qualify_fixture_strategy
from .learning.router import StrategyRouter
from .monitoring.anomalies import detect_anomalies
from .strategies.base import Signal
from .strategies.examples import SmaCrossStrategy


@dataclass
class PaperWorkflowResult:
    workflow_id: str
    executed: bool
    blocked: bool
    reason: str
    dataset: dict[str, Any]
    quality: dict[str, Any]
    qualification: dict[str, Any]
    route: dict[str, Any]
    routed_signal: dict[str, Any]
    signal: str
    decision_index: int | None
    execution_index: int | None
    plan: dict[str, Any] | None
    order: dict[str, Any] | None
    fill: dict[str, Any] | None
    rejections: tuple[dict[str, Any], ...]
    recovery: dict[str, Any]
    persistence: dict[str, Any]
    conformance: dict[str, Any] = field(default_factory=dict)
    profile: dict[str, Any] = field(default_factory=dict)
    health: dict[str, Any] = field(default_factory=dict)
    status: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        clean = sanitize_audit_value(
            {
                "workflow_id": self.workflow_id,
                "executed": self.executed,
                "blocked": self.blocked,
                "reason": self.reason,
                "dataset": self.dataset,
                "quality": self.quality,
                "qualification": self.qualification,
                "route": self.route,
                "routed_signal": self.routed_signal,
                "signal": self.signal,
                "decision_index": self.decision_index,
                "execution_index": self.execution_index,
                "plan": self.plan,
                "order": self.order,
                "fill": self.fill,
                "rejections": list(self.rejections),
                "recovery": self.recovery,
                "persistence": self.persistence,
                "conformance": self.conformance,
                "profile": self.profile,
                "health": self.health,
            }
        )
        return clean if isinstance(clean, dict) else {"workflow_id": self.workflow_id}

    def audit_view(self) -> dict[str, Any]:
        clean = self.summary()
        clean["status"] = sanitize_audit_value(deepcopy(self.status))
        return clean


def _default_workflow_id(platform: Any) -> str:
    return make_decision_id(
        "paper-workflow",
        platform.manifest.dataset_id,
        str(len(platform.bars)),
        str(len(platform.paper_execution.records())),
        str(len(platform.paper_execution.fills())),
        platform.version,
    )


def _load_dataset(platform: Any, *, workflow_id: str) -> Any:
    provider_symbol = getattr(platform, "provider_symbol", PROVIDER_SYMBOL)
    data_class = getattr(platform, "data_class", None)
    raw_bars = synthetic_bar_payloads(platform.bars, provider_symbol=provider_symbol)
    raw_quotes = synthetic_quote_payloads(
        platform.bars,
        provider_symbol=provider_symbol,
        quote_source=getattr(platform, "quote_source", QUOTE_SOURCE),
        quote_id_prefix=getattr(platform, "quote_id_prefix", "fixture-quote"),
        metadata={"data_class": str(data_class)} if data_class else None,
    )
    pipeline = ForexDataPipeline(
        catalog_manager=platform.catalog_manager,
        calendar=platform.calendar,
        provider_id=getattr(platform, "provider_id", PROVIDER_ID),
        quote_freshness=platform.quote_freshness,
        dataset_id=platform.manifest.dataset_id,
        pair=getattr(platform, "pair", PAIR),
        source=getattr(platform, "data_source", BAR_SOURCE),
        data_class=getattr(platform, "data_class", None)
        or "SYNTHETIC_FIXTURE",
        source_version=getattr(platform, "data_source_version", ""),
        provenance=getattr(platform, "dataset_provenance", {}) or {},
    )
    loaded = pipeline.load(raw_bars, raw_quotes)
    if not loaded.verify():
        raise ValueError("normalized_dataset_digest_invalid")
    platform.bars = loaded.bars
    platform.provider = loaded.provider
    platform.spec = loaded.spec
    platform.catalog = loaded.catalog
    platform.manifest = loaded.manifest
    platform.quality_report = loaded.quality
    platform.loaded_dataset = loaded
    platform.data_class = loaded.data_class
    platform.data_source = loaded.manifest.source
    platform.data_source_version = loaded.manifest.source_version
    platform.dataset_provenance = dict(loaded.provenance)
    platform.repository.save_dataset(
        loaded.manifest.dataset_id,
        loaded.manifest.sha256,
        loaded.manifest.audit_view(),
    )
    spec_rows = [
        {
            "pair": pair,
            "signature": signature,
            "version": platform.catalog_manager.version,
        }
        for pair, signature in platform.catalog_manager.hashes.items()
    ]
    platform.repository.save_specs(
        version=platform.catalog_manager.version, specs=spec_rows
    )
    platform.audit_log.record(
        {
            "event_type": "PAPER_WORKFLOW_DATA_LOADED",
            "workflow_id": workflow_id,
            "dataset": loaded.audit_view(),
            "live": False,
        }
    )
    return loaded


def _find_actionable_plan(
    platform: Any,
    *,
    bars: tuple[Any, ...],
    strategy: SmaCrossStrategy,
    routed_id: str,
    workflow_id: str,
) -> tuple[Signal | None, ExecutionPlan | None, tuple[dict[str, Any], ...]]:
    rejections: list[dict[str, Any]] = []
    for decision_index in range(len(bars) - 2, strategy.history_limit - 2, -1):
        signal = strategy.generate_signal(bars[: decision_index + 1])
        if signal == Signal.HOLD:
            continue
        plan = platform.planner.plan(
            signal=signal,
            positions=platform.portfolio.positions,
            bars=bars,
            decision_index=decision_index,
            run_id=workflow_id,
            strategy_id=routed_id,
        )
        if isinstance(plan, ExecutionRejection):
            rejections.append(plan.audit_view())
            continue
        if isinstance(plan, ExecutionPlan):
            return signal, plan, tuple(rejections)
    return None, None, tuple(rejections)


def _persist_state(
    platform: Any, *, workflow_id: str, reason: str, rejections: tuple[dict[str, Any], ...]
) -> dict[str, Any]:
    repository = platform.repository
    repository.save_orders(platform.paper_execution.order_records())
    repository.save_fills(platform.paper_execution.fills())
    portfolio_snapshot = platform.portfolio.snapshot(
        platform.bars[-1].end_at, platform.bars[-1]
    )
    repository.save_positions(portfolio_snapshot["open_positions"], state="OPEN")
    repository.save_positions(portfolio_snapshot["closed_trades"], state="CLOSED")
    repository.save_snapshot(
        f"portfolio:{workflow_id}", "portfolio", portfolio_snapshot
    )
    latest_quote = platform.provider.latest_quote(
        getattr(platform, "provider_symbol", PROVIDER_SYMBOL)
    )
    if latest_quote is None:
        quote_fresh = False
        evaluated_at = platform.bars[-1].end_at
    else:
        evaluated_at = latest_quote.timestamp_utc() + timedelta(milliseconds=200)
        quote_fresh = platform.quote_freshness.check(latest_quote, evaluated_at).ok
    calendar_open = platform.calendar.status(
        platform.spec.canonical_pair or "", platform.bars[-1].timestamp
    ).is_open
    anomalies = detect_anomalies(
        audit_events=platform.audit_events(limit=1000),
        order_records=platform.paper_execution.order_records(),
        calendar_open=calendar_open,
        quality_usable=platform.quality_report.usable,
        quote_fresh=quote_fresh,
    )
    for index, anomaly in enumerate(anomalies):
        repository.record_monitoring(
            make_decision_id(workflow_id, anomaly.code, str(index)),
            anomaly.code,
            anomaly.severity,
            anomaly.audit_view(),
        )
    persistence = {
        "repository": repository.health(),
        "orders": len(platform.paper_execution.order_records()),
        "fills": len(platform.paper_execution.fills()),
        "open_positions": len(portfolio_snapshot["open_positions"]),
        "closed_trades": len(portfolio_snapshot["closed_trades"]),
        "anomalies": [anomaly.audit_view() for anomaly in anomalies],
        "quote_fresh": quote_fresh,
        "calendar_open": calendar_open,
        "reason": reason,
        "rejections": list(rejections),
        "evaluated_at": evaluated_at,
    }
    return persistence


def _finalize(
    platform: Any,
    *,
    result: PaperWorkflowResult,
) -> PaperWorkflowResult:
    result.persistence = _persist_state(
        platform,
        workflow_id=result.workflow_id,
        reason=result.reason,
        rejections=result.rejections,
    )
    conformance_snapshot_id = f"conformance:{result.workflow_id}"
    if result.conformance:
        platform.repository.save_snapshot(
            conformance_snapshot_id,
            "conformance",
            deepcopy(result.conformance),
        )
    result.persistence["conformance_snapshot_id"] = conformance_snapshot_id
    result.persistence["conformance_passed"] = bool(
        result.conformance.get("passed", False)
    )
    profile_snapshot_id = f"profile:{result.workflow_id}"
    if result.profile:
        platform.repository.save_snapshot(
            profile_snapshot_id,
            "profile",
            deepcopy(result.profile),
        )
    result.persistence["profile_snapshot_id"] = profile_snapshot_id
    result.persistence["profile_activation_allowed"] = bool(
        result.profile.get("activation", {}).get("allowed", False)
    )
    platform.audit_log.record(
        {
            "event_type": "PAPER_WORKFLOW_PROFILE_ONBOARDING",
            "workflow_id": result.workflow_id,
            "profile_id": result.profile.get("profile", {}).get("profile_id"),
            "status": result.profile.get("validation", {}).get("status"),
            "activation_allowed": bool(
                result.profile.get("activation", {}).get("allowed", False)
            ),
            "snapshot_id": profile_snapshot_id,
            "live": False,
        }
    )
    platform.audit_log.record(
        {
            "event_type": "PAPER_WORKFLOW_CONFORMANCE",
            "workflow_id": result.workflow_id,
            "suite_id": result.conformance.get("suite_id"),
            "status": result.conformance.get("status"),
            "passed": bool(result.conformance.get("passed", False)),
            "snapshot_id": conformance_snapshot_id,
            "live": False,
        }
    )
    platform.audit_log.record(
        {
            "event_type": "PAPER_WORKFLOW_COMPLETED",
            "workflow_id": result.workflow_id,
            "executed": result.executed,
            "blocked": result.blocked,
            "reason": result.reason,
            "live": False,
        }
    )
    status = platform.status()
    result.status = status
    result.health = deepcopy(status.get("health", {}))
    status["workflow"] = result.summary()
    result.status = status
    result.health = deepcopy(status.get("health", {}))
    platform.repository.save_snapshot(
        f"workflow:{result.workflow_id}", "workflow", result.audit_view()
    )
    return result


def _blocked_result(
    platform: Any,
    *,
    workflow_id: str,
    loaded: Any,
    reason: str,
    qualification: dict[str, Any] | None = None,
    route: dict[str, Any] | None = None,
    routed_signal: dict[str, Any] | None = None,
    signal: str = "HOLD",
    rejections: tuple[dict[str, Any], ...] = (),
    conformance: dict[str, Any] | None = None,
    profile: dict[str, Any] | None = None,
) -> PaperWorkflowResult:
    conformance_view = deepcopy(conformance or platform.conformance_report())
    profile_view = deepcopy(profile or platform.provider_profile_report())
    result = PaperWorkflowResult(
        workflow_id=workflow_id,
        executed=False,
        blocked=True,
        reason=reason,
        dataset=loaded.audit_view(),
        quality=loaded.quality.audit_view(),
        qualification=qualification or {},
        route=route or {},
        routed_signal=routed_signal or {},
        signal=signal,
        decision_index=None,
        execution_index=None,
        plan=None,
        order=None,
        fill=None,
        rejections=rejections,
        recovery=(
            platform.recovery_result.audit_view()
            if platform.recovery_result is not None
            else {"recovered": False}
        ),
        persistence={},
        conformance=conformance_view,
        profile=profile_view,
    )
    platform.audit_log.record(
        {
            "event_type": "PAPER_WORKFLOW_BLOCKED",
            "workflow_id": workflow_id,
            "reason": reason,
            "live": False,
        }
    )
    return _finalize(platform, result=result)


def execute_paper_workflow(
    platform: Any, *, workflow_id: str | None = None
) -> PaperWorkflowResult:
    """Execute one offline paper workflow and return its durable result."""
    if not platform.runtime_settings.research_only:
        raise ValueError("paper_workflow_requires_research_only_mode")
    if platform.runtime_settings.live_bridge_enabled:
        raise ValueError("live_bridge_must_remain_disabled")
    if platform.external_broker_boundary.enabled:
        raise ValueError("external_broker_must_remain_disabled")
    effective_id = workflow_id or _default_workflow_id(platform)
    loaded = _load_dataset(platform, workflow_id=effective_id)
    bars = loaded.bars
    if not is_execution_eligible(loaded.data_class):
        return _blocked_result(
            platform,
            workflow_id=effective_id,
            loaded=loaded,
            reason="data_class_not_execution_eligible",
            rejections=(
                {
                    "code": "DATA_CLASS_NOT_EXECUTION_ELIGIBLE",
                    "reason": str(loaded.data_class),
                },
            ),
        )
    profile = platform.refresh_provider_profile_report()
    profile_validation = profile.get("validation", {})
    profile_activation = profile.get("activation", {})
    if not profile_validation.get("passed", False) or not profile_activation.get(
        "allowed", False
    ):
        profile_rejections = tuple(
            {
                "code": str(finding.get("code", "PROFILE_ERROR")),
                "reason": str(finding.get("field", "profile_term")),
            }
            for finding in profile.get("findings", [])
        ) + tuple(
            {"code": "MISSING_PROFILE_FIELD", "reason": str(field)}
            for field in profile.get("missing_fields", [])
        )
        return _blocked_result(
            platform,
            workflow_id=effective_id,
            loaded=loaded,
            reason="provider_profile_not_activated",
            rejections=profile_rejections,
            conformance=platform.conformance_report(),
            profile=profile,
        )
    conformance = platform.refresh_conformance_report()
    if not conformance.get("passed", False):
        conformance_rejections = tuple(
            {
                "code": str(finding.get("code", "CONFORMANCE_ERROR")),
                "reason": str(finding.get("detail", "required capability failed")),
            }
            for finding in conformance.get("findings", [])
            if not finding.get("ok", False)
        )
        return _blocked_result(
            platform,
            workflow_id=effective_id,
            loaded=loaded,
            reason="provider_broker_conformance_failed",
            rejections=conformance_rejections,
            conformance=conformance,
            profile=profile,
        )
    unresolved = platform.paper_execution.unresolved_orders()
    if unresolved:
        return _blocked_result(
            platform,
            workflow_id=effective_id,
            loaded=loaded,
            reason="unresolved_order_present",
            rejections=tuple(
                {"code": "UNRESOLVED_ORDER_PRESENT", "order_id": order.order_id}
                for order in unresolved
            ),
        )
    if len(bars) < 20:
        return _blocked_result(
            platform,
            workflow_id=effective_id,
            loaded=loaded,
            reason="insufficient_normalized_bars",
        )
    strategy = SmaCrossStrategy(
        fast_period=3, slow_period=8, name="fixture-sma-cross"
    )
    train_end = len(bars) // 2
    validation_end = train_end + len(bars) // 4
    validation = run_oos(
        platform.new_engine,
        strategy,
        train_bars=bars[:train_end],
        validation_bars=bars[train_end:validation_end],
        test_bars=bars[validation_end:],
    )
    platform.repository.save_backtest(validation.result.audit_view())
    routed_id = f"{effective_id}:fixture-sma-cross"
    qualified = qualify_fixture_strategy(routed_id, validation)
    platform.repository.save_lifecycle(routed_id, qualified.lifecycle.audit_view())
    qualification = qualified.audit_view()
    platform.audit_log.record(
        {
            "event_type": "PAPER_WORKFLOW_QUALIFIED",
            "workflow_id": effective_id,
            "qualification": qualification,
            "live": False,
        }
    )
    router = StrategyRouter({routed_id: qualified.lifecycle})
    decision = router.select({routed_id: qualified.oos_expectancy})
    route = decision.audit_view()
    if not decision.selected or not qualified.qualified:
        return _blocked_result(
            platform,
            workflow_id=effective_id,
            loaded=loaded,
            reason="fixture_strategy_not_qualified",
            qualification=qualification,
            route=route,
        )
    signal, plan, rejections = _find_actionable_plan(
        platform,
        bars=bars,
        strategy=strategy,
        routed_id=routed_id,
        workflow_id=effective_id,
    )
    if signal is None or plan is None:
        return _blocked_result(
            platform,
            workflow_id=effective_id,
            loaded=loaded,
            reason="no_actionable_routed_signal",
            qualification=qualification,
            route=route,
            rejections=rejections,
        )
    routed = router.route_signal(decision, routed_id, signal)
    routed_signal = routed.audit_view()
    platform.audit_log.record(
        {
            "event_type": "PAPER_WORKFLOW_SIGNAL_ROUTED",
            "workflow_id": effective_id,
            "route": route,
            "routed_signal": routed_signal,
            "live": False,
        }
    )
    if not routed.accepted:
        return _blocked_result(
            platform,
            workflow_id=effective_id,
            loaded=loaded,
            reason="router_rejected_signal",
            qualification=qualification,
            route=route,
            routed_signal=routed_signal,
            signal=signal.value,
            rejections=rejections,
        )
    record = None
    fill = None
    execution_rejections = list(rejections)
    executed = False
    reason = "not_executed"
    try:
        record, fill = platform.paper_execution.execute(
            plan.request,
            plan.context,
            fill_price=plan.fill_price,
            fill_at=plan.fill_at,
            action=plan.action,
            position_id=plan.position_id,
            cutoffs=platform.execution_config.rollover_cutoffs,
        )
    except Exception as exc:
        execution_rejections.append(
            {"code": "EXECUTION_INTERNAL_ERROR", "reason": type(exc).__name__}
        )
        reason = "execution_internal_error"
    else:
        if fill is not None and record.state == "FILLED":
            executed = True
            reason = "local_paper_fill"
        else:
            execution_rejections.append(
                {
                    "code": record.state,
                    "reason": record.reason,
                    "request_id": record.request_id,
                }
            )
            reason = f"paper_order_{record.state.lower()}"
    platform.audit_log.record(
        {
            "event_type": "PAPER_WORKFLOW_EXECUTION",
            "workflow_id": effective_id,
            "plan": plan.audit_view(),
            "order": record.audit_view() if record is not None else None,
            "fill": fill.audit_view() if fill is not None else None,
            "executed": executed,
            "reason": reason,
            "live": False,
        }
    )
    result = PaperWorkflowResult(
        workflow_id=effective_id,
        executed=executed,
        blocked=not executed,
        reason=reason,
        dataset=loaded.audit_view(),
        quality=loaded.quality.audit_view(),
        qualification=qualification,
        route=route,
        routed_signal=routed_signal,
        signal=signal.value,
        decision_index=plan.decision_index,
        execution_index=plan.execution_index,
        plan=plan.audit_view(),
        order=record.audit_view() if record is not None else None,
        fill=fill.audit_view() if fill is not None else None,
        rejections=tuple(execution_rejections),
        recovery=(
            platform.recovery_result.audit_view()
            if platform.recovery_result is not None
            else {"recovered": False}
        ),
        persistence={},
        conformance=deepcopy(conformance),
        profile=deepcopy(profile),
    )
    return _finalize(platform, result=result)
