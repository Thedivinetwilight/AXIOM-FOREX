"""Aggregate correlated-pair risk and canonical gate-context construction."""

from .aggregate import (
    AggregateRiskDecision,
    AggregateRiskLimits,
    CorrelationMatrix,
    check_aggregate_exposure,
)
from .context import ForexExecutionContextFactory

__all__ = [
    "AggregateRiskDecision",
    "AggregateRiskLimits",
    "CorrelationMatrix",
    "ForexExecutionContextFactory",
    "check_aggregate_exposure",
]
