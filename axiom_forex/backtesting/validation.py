"""Chronological out-of-sample validation with a one-use locked test split."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Sequence

from app.data.models import ForexQuoteBar

from axiom_forex.strategies.base import Strategy
from axiom_forex.validation.split import LockedTest

from .engine import BacktestResult, ForexBacktestEngine


@dataclass(frozen=True)
class OOSValidation:
    result: BacktestResult
    train_net_pnl_account: Decimal
    validation_net_pnl_account: Decimal
    test_net_pnl_account: Decimal
    passed: bool
    test_used: bool

    def audit_view(self) -> dict[str, Any]:
        return {
            "result": self.result.audit_view(),
            "train_net_pnl_account": self.train_net_pnl_account,
            "validation_net_pnl_account": self.validation_net_pnl_account,
            "test_net_pnl_account": self.test_net_pnl_account,
            "passed": self.passed,
            "test_used": self.test_used,
        }


def run_oos(
    engine_factory: Callable[[], ForexBacktestEngine],
    strategy: Strategy,
    *,
    train_bars: Sequence[ForexQuoteBar],
    validation_bars: Sequence[ForexQuoteBar],
    test_bars: Sequence[ForexQuoteBar],
) -> OOSValidation:
    """Run once over a chronological concatenation; never tune on the test set."""
    locked_test = LockedTest(test_bars)
    locked_bars = locked_test.use_once()
    all_bars = tuple(train_bars) + tuple(validation_bars) + tuple(locked_bars)
    engine = engine_factory()
    result = engine.run(
        all_bars,
        strategy,
        out_of_sample=True,
        run_id=f"{engine.config.dataset_id}:oos",
    )
    curve = {
        int(point["index"]): point["equity_account"]
        for point in result.equity_curve
    }
    train_end = len(train_bars) - 1
    validation_end = len(train_bars) + len(validation_bars) - 1
    test_end = len(all_bars) - 1
    initial = result.initial_equity_account
    train_pnl = curve.get(train_end, initial) - initial
    validation_pnl = curve.get(validation_end, initial) - curve.get(train_end, initial)
    test_pnl = curve.get(test_end, initial) - curve.get(validation_end, initial)
    passed = (
        result.usable
        and validation_pnl > Decimal(0)
        and test_pnl > Decimal(0)
    )
    return OOSValidation(
        result,
        train_pnl,
        validation_pnl,
        test_pnl,
        passed,
        locked_test.used,
    )
