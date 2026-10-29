"""Bounded chronological tournament; each candidate gets a clean engine.

Selection is made on the *validation* segment only.  The locked test segment is
opened once, reported as evidence, and never used to choose a candidate: a test
set that decides the winner is no longer out of sample, so its P&L could not be
read as independent evidence of anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Mapping, Sequence

from app.data.models import ForexQuoteBar

from axiom_forex.backtesting.engine import ForexBacktestEngine
from axiom_forex.learning.lifecycle import StrategyLifecycle
from axiom_forex.learning.router import StrategyRouter
from axiom_forex.strategies.examples import SmaCrossStrategy
from axiom_forex.validation.split import LockedTest


@dataclass(frozen=True)
class Candidate:
    strategy_id: str
    fast_period: int
    slow_period: int


@dataclass(frozen=True)
class CandidateResult:
    strategy_id: str
    parameters: dict[str, int]
    validation_pnl_account: Decimal
    oos_pnl_account: Decimal
    quality_usable: bool
    lifecycle_stage: str
    selected: bool
    reason: str

    def audit_view(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "parameters": dict(self.parameters),
            "validation_pnl_account": self.validation_pnl_account,
            "oos_pnl_account": self.oos_pnl_account,
            "quality_usable": self.quality_usable,
            "lifecycle_stage": self.lifecycle_stage,
            "selected": self.selected,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class TournamentResult:
    candidates: tuple[CandidateResult, ...]
    selected: str | None
    test_used: bool
    router_reason: str
    selection_basis: str = "VALIDATION"

    def audit_view(self) -> dict[str, Any]:
        return {
            "candidates": [candidate.audit_view() for candidate in self.candidates],
            "selected": self.selected,
            "test_used": self.test_used,
            "router_reason": self.router_reason,
            "selection_basis": self.selection_basis,
        }


class TournamentRunner:
    def __init__(
        self,
        *,
        engine_factory: Callable[[], ForexBacktestEngine],
        candidates: Sequence[Candidate],
    ):
        if not candidates:
            raise ValueError("candidates required")
        self.engine_factory = engine_factory
        self.candidates = tuple(candidates)

    def run(
        self,
        *,
        train_bars: Sequence[ForexQuoteBar],
        validation_bars: Sequence[ForexQuoteBar],
        test_bars: Sequence[ForexQuoteBar],
        lifecycles: Mapping[str, StrategyLifecycle],
    ) -> TournamentResult:
        locked_test = LockedTest(test_bars)
        locked_bars = locked_test.use_once()
        all_bars = tuple(train_bars) + tuple(validation_bars) + tuple(locked_bars)
        results: list[CandidateResult] = []
        validation_expectancies: dict[str, Decimal] = {}
        for candidate in self.candidates:
            strategy = SmaCrossStrategy(
                fast_period=candidate.fast_period,
                slow_period=candidate.slow_period,
                name=candidate.strategy_id,
            )
            engine = self.engine_factory()
            result = engine.run(
                all_bars,
                strategy,
                run_id=f"tournament:{candidate.strategy_id}",
            )
            curve = {
                int(point["index"]): point["equity_account"]
                for point in result.equity_curve
            }
            train_end = len(train_bars) - 1
            validation_end = len(train_bars) + len(validation_bars) - 1
            test_end = len(all_bars) - 1
            initial = result.initial_equity_account
            validation_pnl = curve.get(validation_end, initial) - curve.get(
                train_end, initial
            )
            oos_pnl = curve.get(test_end, initial) - curve.get(
                validation_end, initial
            )
            lifecycle = lifecycles.get(candidate.strategy_id)
            stage = lifecycle.stage.value if lifecycle else "RESEARCH"
            reason = "quality_unusable" if not result.usable else "evaluated"
            # Only the validation segment may influence selection.  A positive
            # test P&L is required to qualify as evidence, but it is deliberately
            # not part of the ranking key.
            if result.usable and validation_pnl > 0 and oos_pnl > 0:
                validation_expectancies[candidate.strategy_id] = validation_pnl
            results.append(
                CandidateResult(
                    strategy_id=candidate.strategy_id,
                    parameters={
                        "fast_period": candidate.fast_period,
                        "slow_period": candidate.slow_period,
                    },
                    validation_pnl_account=validation_pnl,
                    oos_pnl_account=oos_pnl,
                    quality_usable=result.usable,
                    lifecycle_stage=stage,
                    selected=False,
                    reason=reason,
                )
            )
        router = StrategyRouter(lifecycles)
        decision = router.select(validation_expectancies)
        selected = decision.strategy_id
        decorated = tuple(
            CandidateResult(
                strategy_id=candidate.strategy_id,
                parameters=dict(candidate.parameters),
                validation_pnl_account=candidate.validation_pnl_account,
                oos_pnl_account=candidate.oos_pnl_account,
                quality_usable=candidate.quality_usable,
                lifecycle_stage=candidate.lifecycle_stage,
                selected=candidate.strategy_id == selected,
                reason=candidate.reason,
            )
            for candidate in results
        )
        return TournamentResult(
            decorated,
            selected,
            locked_test.used,
            decision.reason,
            selection_basis="VALIDATION",
        )
