"""Strategy router and explicit per-signal routing boundary."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from axiom_forex.learning.lifecycle import StrategyLifecycle
from axiom_forex.strategies.base import Signal


@dataclass(frozen=True)
class RouterDecision:
    selected: bool
    strategy_id: str | None
    reason: str
    oos_expectancy: Decimal | None = None

    def audit_view(self) -> dict[str, object]:
        return {
            "selected": self.selected,
            "strategy_id": self.strategy_id,
            "reason": self.reason,
            "oos_expectancy": self.oos_expectancy,
        }


@dataclass(frozen=True)
class RoutedSignal:
    strategy_id: str
    signal: Signal
    accepted: bool
    reason: str

    def audit_view(self) -> dict[str, object]:
        return {
            "strategy_id": self.strategy_id,
            "signal": self.signal.value,
            "accepted": self.accepted,
            "reason": self.reason,
        }


class StrategyRouter:
    def __init__(self, lifecycles: Mapping[str, StrategyLifecycle]):
        self.lifecycles = dict(lifecycles)

    def select(self, candidates: Mapping[str, Decimal]) -> RouterDecision:
        eligible = []
        for strategy_id, expectancy in candidates.items():
            lifecycle = self.lifecycles.get(strategy_id)
            if lifecycle is None or not lifecycle.approve_active() or expectancy <= 0:
                continue
            eligible.append((expectancy, strategy_id))
        if not eligible:
            return RouterDecision(False, None, "no_qualified_candidate")
        expectancy, strategy_id = max(eligible)
        return RouterDecision(True, strategy_id, "approved_positive_oos", expectancy)

    def route_signal(
        self,
        decision: RouterDecision,
        strategy_id: str,
        signal: Signal,
    ) -> RoutedSignal:
        if not decision.selected or decision.strategy_id != strategy_id:
            return RoutedSignal(strategy_id, signal, False, "strategy_not_selected")
        if signal == Signal.HOLD:
            return RoutedSignal(strategy_id, signal, False, "hold_signal")
        return RoutedSignal(strategy_id, signal, True, "qualified_signal_routed")
