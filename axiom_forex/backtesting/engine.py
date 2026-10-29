"""Deterministic completed-bar, next-bar bid/ask paper backtest engine."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Sequence

from app.audit.log import AuditLog, make_decision_id
from app.calendar.trading import TradingCalendar
from app.data.models import ForexQuoteBar
from app.universe.specs import CurrencyPairSpec

from axiom_forex.data.quality import QualityReport, assess_bars
from axiom_forex.execution.config import ExecutionConfig
from axiom_forex.execution.paper_engine import PaperExecutionEngine
from axiom_forex.execution.planner import (
    ExecutionPlan,
    ExecutionRejection,
    ForexExecutionPlanner,
)
from axiom_forex.portfolio.ledger import ForexPortfolio
from axiom_forex.strategies.base import Signal, Strategy

# One versioned execution config is shared by backtests and paper workflows.
BacktestConfig = ExecutionConfig


@dataclass(frozen=True)
class BacktestResult:
    run_id: str
    config: ExecutionConfig
    quality: QualityReport
    trades: tuple[dict[str, Any], ...]
    rejections: tuple[dict[str, Any], ...]
    equity_curve: tuple[dict[str, Any], ...]
    initial_equity_account: Decimal
    final_equity_account: Decimal
    net_pnl_account: Decimal
    max_drawdown_account: Decimal
    cost_total_account: Decimal
    rollover_total_account: Decimal
    final_bar_signal_cancelled: bool
    out_of_sample: bool
    audit_events: tuple[dict[str, Any], ...]
    open_positions: tuple[dict[str, Any], ...]

    @property
    def usable(self) -> bool:
        return self.quality.usable

    def audit_view(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "config": self.config.audit_view(),
            "quality": self.quality.audit_view(),
            "trades": [dict(trade) for trade in self.trades],
            "rejections": [dict(rejection) for rejection in self.rejections],
            "equity_curve": [dict(point) for point in self.equity_curve],
            "initial_equity_account": self.initial_equity_account,
            "final_equity_account": self.final_equity_account,
            "net_pnl_account": self.net_pnl_account,
            "max_drawdown_account": self.max_drawdown_account,
            "cost_total_account": self.cost_total_account,
            "rollover_total_account": self.rollover_total_account,
            "final_bar_signal_cancelled": self.final_bar_signal_cancelled,
            "out_of_sample": self.out_of_sample,
            "audit_event_count": len(self.audit_events),
            "open_positions": [dict(position) for position in self.open_positions],
            "usable": self.usable,
        }


@dataclass
class ForexBacktestEngine:
    spec: CurrencyPairSpec
    calendar: TradingCalendar
    portfolio: ForexPortfolio
    paper_execution: PaperExecutionEngine
    audit_log: AuditLog
    config: ExecutionConfig
    planner: ForexExecutionPlanner
    _used: bool = False

    def run(
        self,
        bars: Sequence[ForexQuoteBar],
        strategy: Strategy,
        *,
        out_of_sample: bool = False,
        run_id: str | None = None,
    ) -> BacktestResult:
        bars = tuple(bars)
        effective_run_id = run_id or make_decision_id(
            "backtest",
            self.config.dataset_id,
            strategy.name,
            str(len(bars)),
            str(out_of_sample),
        )
        if not bars:
            raise ValueError("Empty bar sequence")
        if (
            self._used
            or self.portfolio.positions
            or self.portfolio.trades_today
            or self.paper_execution.records()
        ):
            raise ValueError("engine_already_used_create_fresh_engine")
        self._used = True
        evaluated_at = bars[-1].end_at
        quality = assess_bars(
            bars,
            spec=self.spec,
            calendar=self.calendar,
            freshness=self.planner.context_factory.quote_freshness,
            evaluated_at=evaluated_at,
        )
        initial_equity = self.portfolio.equity_account(bars[0].timestamp, bars[0])
        equity_curve: list[dict[str, Any]] = [
            {
                "index": -1,
                "timestamp": bars[0].timestamp,
                "equity_account": initial_equity,
            }
        ]
        trades: list[dict[str, Any]] = []
        rejections: list[dict[str, Any]] = []
        # No decision can be taken before the strategy has its history, so the
        # book is untouched until then.  Record those bars at their own point in
        # time instead of back-filling them later with the final portfolio,
        # which would invent unrealized P&L (and drawdown) for bars during which
        # no position existed.
        first_decision_index = min(
            max(int(getattr(strategy, "history_limit", 1)), 1) - 1, len(bars)
        )
        for index in range(first_decision_index):
            equity_curve.append(
                {
                    "index": index,
                    "timestamp": bars[index].end_at,
                    "equity_account": initial_equity,
                }
            )
        if not quality.usable:
            rejections.append(
                {
                    "index": 0,
                    "code": "DATA_QUALITY_REJECTED",
                    "reason": quality.classification,
                }
            )
        else:
            for index in range(strategy.history_limit - 1, len(bars) - 1):
                decision_bar = bars[index]
                signal = strategy.generate_signal(bars[: index + 1])
                plan = self.planner.plan(
                    signal=signal,
                    positions=self.portfolio.positions,
                    bars=bars,
                    decision_index=index,
                    run_id=effective_run_id,
                    strategy_id=strategy.name,
                )
                if isinstance(plan, ExecutionRejection):
                    rejections.append(
                        {
                            "index": plan.decision_index,
                            "request_id": plan.request_id,
                            "code": plan.code,
                            "reason": plan.reason,
                        }
                    )
                elif isinstance(plan, ExecutionPlan):
                    try:
                        record, fill = self.paper_execution.execute(
                            plan.request,
                            plan.context,
                            fill_price=plan.fill_price,
                            fill_at=plan.fill_at,
                            action=plan.action,
                            position_id=plan.position_id,
                            cutoffs=self.config.rollover_cutoffs,
                        )
                    except Exception as exc:
                        rejections.append(
                            {
                                "index": index,
                                "request_id": plan.request.request_id,
                                "code": "EXECUTION_INTERNAL_ERROR",
                                "reason": type(exc).__name__,
                            }
                        )
                    else:
                        if fill is None or record.state != "FILLED":
                            rejections.append(
                                {
                                    "index": index,
                                    "request_id": plan.request.request_id,
                                    "code": record.state,
                                    "reason": record.reason,
                                }
                            )
                        else:
                            trades.append(
                                {
                                    "decision_bar": plan.decision_index,
                                    "execution_bar": plan.execution_index,
                                    "signal": plan.signal.value,
                                    "fill": fill.audit_view(),
                                }
                            )
                at = decision_bar.end_at
                equity_curve.append(
                    {
                        "index": index,
                        "timestamp": at,
                        "equity_account": self.portfolio.equity_account(at, decision_bar),
                    }
                )
        # Only the final bar is unreachable for a decision (the loop stops one
        # bar short so a fill always has a next bar), so it is the only state
        # that may be appended after the loop -- and by then it is current.
        present_indices = {int(point["index"]) for point in equity_curve}
        for index, bar in enumerate(bars):
            if index in present_indices:
                continue
            if index < first_decision_index:  # pragma: no cover - defensive
                equity = initial_equity
            else:
                equity = self.portfolio.equity_account(bar.end_at, bar)
            equity_curve.append(
                {"index": index, "timestamp": bar.end_at, "equity_account": equity}
            )
        equity_curve.sort(key=lambda point: int(point["index"]))
        last_signal = (
            strategy.generate_signal(bars)
            if len(bars) >= strategy.history_limit
            else Signal.HOLD
        )
        final_bar_signal_cancelled = (
            self.config.final_bar_signal_cancelled and last_signal != Signal.HOLD
        )
        final_equity = self.portfolio.equity_account(evaluated_at, bars[-1])
        peak = initial_equity
        max_drawdown = Decimal(0)
        for point in equity_curve:
            peak = max(peak, point["equity_account"])
            max_drawdown = max(max_drawdown, peak - point["equity_account"])
        events = tuple(getattr(self.audit_log, "events", ()))
        return BacktestResult(
            run_id=effective_run_id,
            config=self.config,
            quality=quality,
            trades=tuple(trades),
            rejections=tuple(rejections),
            equity_curve=tuple(equity_curve),
            initial_equity_account=initial_equity,
            final_equity_account=final_equity,
            net_pnl_account=final_equity - initial_equity,
            max_drawdown_account=max_drawdown,
            cost_total_account=self.portfolio.cost_total_account,
            rollover_total_account=self.portfolio.rollover_total_account,
            final_bar_signal_cancelled=final_bar_signal_cancelled,
            out_of_sample=out_of_sample,
            audit_events=events,
            open_positions=tuple(
                position.audit_view() for position in self.portfolio.positions.values()
            ),
        )
