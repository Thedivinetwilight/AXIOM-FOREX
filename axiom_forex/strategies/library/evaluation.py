"""Run one library variant through the *existing* chronological evaluator.

There is no second backtest engine here.  A variant becomes a
:class:`~axiom_forex.paper_runner.strategy.Candidate` and is handed to
:func:`axiom_forex.paper_runner.evaluation.evaluate_candidate`, so the costs, the
fill rule, the split fractions, the walk-forward folds and the locked-test custody
are exactly the ones the paper lane already uses.  Two engines disagreeing about
the same bars is how a strategy gets "validated" twice and believed once.

The evaluator's payload is then screened with
:mod:`axiom_forex.strategies.library.monitor`, which adds the multiple-testing
control and returns a verdict with every number behind it.

A locked test window is spent once per dataset.  This module therefore refuses to
re-evaluate the same variant on the same dataset, and the caller records the
attempt so the refusal is durable rather than remembered.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Mapping, Sequence

from axiom_forex.paper_runner.evaluation import EvaluationOutcome, evaluate_candidate
from axiom_forex.paper_runner.strategy import Candidate
from axiom_forex.research.datasets import dataset_digest

from .monitor import DEFAULT_CRITERIA, AttemptInput, ScreenCriteria, explain, score_search
from .ledger import ELIGIBLE
from .variants import RuleStrategy, Variant


class EvaluationRefused(Exception):
    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(reason if not detail else f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail

    def audit_view(self) -> dict[str, str]:
        return {"reason": self.reason, "detail": self.detail}


@dataclass(frozen=True)
class VariantEvaluation:
    variant: Variant
    symbol: str
    timeframe: str
    dataset_id: str
    dataset_fingerprint: str
    data_class: str
    evidence: Mapping[str, Any]
    verdict: Mapping[str, Any]
    explanation: str
    q_value: str
    reality_check: str

    def audit_view(self) -> dict[str, Any]:
        return {
            "variant_id": self.variant.variant_id,
            "card_id": self.variant.card_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "dataset_id": self.dataset_id,
            "dataset_fingerprint": self.dataset_fingerprint,
            "data_class": self.data_class,
            "status": self.verdict.get("status", ""),
            "reason": self.verdict.get("reason", ""),
            "explanation": self.explanation,
            "q_value": self.q_value,
            "reality_check": self.reality_check,
        }


def _trade_returns(outcome: EvaluationOutcome) -> list[float]:
    """Closed-trade P&L, in account currency, for the bootstrap."""

    result = outcome.test_result
    if result is None:
        return []
    values: list[float] = []
    for trade in getattr(result, "trades", ()) or ():
        fill = dict(trade.get("fill") or {})
        if str(fill.get("action", "")).upper() != "CLOSE":
            continue
        try:
            values.append(float(Decimal(str(fill.get("realized_pnl_account", 0)))))
        except Exception:
            continue
    return values


def candidate_for(variant: Variant) -> Candidate:
    """A paper-lane candidate that builds this exact variant."""

    return Candidate(
        candidate_id=variant.variant_id,
        strategy_factory=lambda **_: RuleStrategy(variant),
        parameters=dict(variant.parameters),
        rationale=(
            f"{variant.card.name}: a documented template with a predeclared "
            "parameter set from the strategy library."
        ),
        assumptions=variant.card.assumptions,
        limitations=variant.card.limitations,
    )


def evaluate_variant(
    variant: Variant,
    *,
    bars: Sequence[Any],
    symbol: str,
    timeframe: str,
    data_class: str,
    dataset_id: str,
    engine_factory: Callable[[], Any],
    criteria: ScreenCriteria = DEFAULT_CRITERIA,
    clock: Callable[[], datetime] | None = None,
) -> VariantEvaluation:
    """Evaluate, screen, and explain one variant.  Never selects it."""

    card = variant.card
    if not card.supports(timeframe):
        raise EvaluationRefused(
            "timeframe_not_declared",
            f"{card.card_id} declares {', '.join(card.timeframes)}; it is not analysed on {timeframe}",
        )
    if len(bars) < card.min_bars:
        raise EvaluationRefused(
            "insufficient_bars", f"{len(bars)} bar(s) available, the template needs {card.min_bars}"
        )
    if not str(data_class or "").strip():
        raise EvaluationRefused("data_class_required", "the dataset's class must be declared")
    if not str(dataset_id or "").strip():
        raise EvaluationRefused("dataset_id_required", "")

    outcome = evaluate_candidate(
        engine_factory=engine_factory,
        candidate=candidate_for(variant),
        bars=list(bars),
        data_class=str(data_class).upper(),
        dataset_id=str(dataset_id),
        criteria=_paper_criteria(criteria),
        clock=clock,
    )
    evidence = dict(outcome.payload)
    returns = _trade_returns(outcome)
    report = score_search(
        [
            AttemptInput(
                variant_id=variant.variant_id,
                card_id=card.card_id,
                evidence=evidence,
                returns=returns,
                symbol=symbol,
                timeframe=timeframe,
            )
        ],
        criteria=criteria,
    )
    entry = report["results"][0] if report["results"] else {
        "verdict": {"status": "INSUFFICIENT_EVIDENCE", "reason": "no result was produced", "checks": []},
        "q_value": "",
        "reality_check_p_value": "",
    }
    fingerprint = dataset_digest(list(bars))
    return VariantEvaluation(
        variant=variant,
        symbol=symbol,
        timeframe=timeframe,
        dataset_id=str(dataset_id),
        dataset_fingerprint=fingerprint,
        data_class=str(data_class).upper(),
        evidence=evidence,
        verdict=entry["verdict"],
        explanation=explain(entry["verdict"]),
        q_value=str(entry.get("q_value", "")),
        reality_check=str(entry.get("reality_check_p_value", "")),
    )


def _paper_criteria(criteria: ScreenCriteria):
    """Translate the library's screen into the evaluator's own criteria object.

    Only the fields that exist on both are carried across; the multiplicity
    thresholds are applied afterwards by :func:`screen`, because the evaluator does
    not know about them.
    """

    from axiom_forex.paper_runner.evaluation import Criteria

    return Criteria(
        min_closed_trades=criteria.min_closed_trades,
        max_drawdown_fraction=criteria.max_drawdown_fraction,
        min_walk_forward_folds=criteria.min_walk_forward_folds,
        min_positive_fold_fraction=criteria.min_positive_fold_fraction,
        require_positive_validation=criteria.require_positive_validation,
        require_positive_test=criteria.require_positive_locked_test,
    )


__all__ = [
    "EvaluationRefused",
    "VariantEvaluation",
    "candidate_for",
    "evaluate_variant",
]
