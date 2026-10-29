"""Ephemeral fixture strategy qualification from explicit OOS validation.

Qualification never mutates a platform's default lifecycle registry.  It
creates a short-lived lifecycle for one paper workflow only, so no production
strategy is selected or assumed.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .lifecycle import LifecycleStage, StrategyLifecycle


@dataclass(frozen=True)
class QualifiedStrategy:
    strategy_id: str
    lifecycle: StrategyLifecycle
    qualified: bool
    oos_expectancy: Decimal
    reason: str
    validation_passed: bool

    def audit_view(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "qualified": self.qualified,
            "oos_expectancy": self.oos_expectancy,
            "reason": self.reason,
            "validation_passed": self.validation_passed,
            "lifecycle": self.lifecycle.audit_view(),
        }


def qualify_fixture_strategy(strategy_id: str, validation: Any) -> QualifiedStrategy:
    lifecycle = StrategyLifecycle(strategy_id)
    validation_passed = bool(getattr(validation, "passed", False))
    test_pnl = getattr(validation, "test_net_pnl_account", Decimal(0))
    quality_usable = bool(
        getattr(getattr(validation, "result", None), "usable", False)
    )
    qualified = validation_passed and test_pnl > 0 and quality_usable
    if qualified:
        while lifecycle.stage != LifecycleStage.APPROVED:
            if lifecycle.promote() == lifecycle.stage and lifecycle.stage == LifecycleStage.APPROVED:
                break
        lifecycle.active = True
        reason = "validated_positive_oos_fixture"
    else:
        reason = "oos_validation_not_qualified"
    return QualifiedStrategy(
        strategy_id=strategy_id,
        lifecycle=lifecycle,
        qualified=qualified,
        oos_expectancy=test_pnl,
        reason=reason,
        validation_passed=validation_passed,
    )
