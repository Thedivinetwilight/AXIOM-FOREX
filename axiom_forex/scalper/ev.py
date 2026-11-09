"""EV CHECK: does this trade pay for itself before it is taken?

The identity is the whole module:

```
expected net = expected gross − spread − commission − slippage
```

Everything is computed in **points** first, because points are what the market
quotes in and what the stop and target are written in, and only then converted to
the account currency using the instrument's declared point value.  A cost that
cannot be priced is not zero: if the commission state is ``UNKNOWN``, or the quote
currency cannot be converted to the account currency, the engine refuses and says
which term was missing.

The probability arrives with a standard deviation (see :mod:`bayes`), so the
decision uses the **lower bound** of the expected value, not its centre.  An edge
that only exists when the estimate is taken at face value is not an edge; it is a
sample.

Nothing here is a forecast.  The engine answers exactly one question: given these
costs, this bracket, and this much evidence, is the expected net value still above
the declared floor?
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Sequence

from .bayes import ProbabilityEstimate
from .config import CostTerms, InstrumentSpec
from .signals import Signal

ACCEPT = "ACCEPT"
REJECT = "REJECT"

#: Reasons a rejection is issued, worst first: the first one is the headline.
COST_UNKNOWN = "cost_cannot_be_priced"
GROSS_NOT_POSITIVE = "expected_gross_is_not_positive"
COST_EXCEEDS_GROSS = "costs_consume_the_gross_edge"
BELOW_MIN_EDGE = "expected_net_below_the_declared_floor"
NO_PROBABILITY = "no_probability_estimate"
BRACKET_INVALID = "stop_or_target_not_positive"
SPREAD_UNKNOWN = "spread_not_observable"


def _d(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


@dataclass(frozen=True)
class TradeCost:
    """What one round turn costs, in points and in account currency per lot."""

    spread_points: Decimal
    slippage_points: Decimal
    commission_points: Decimal
    total_points: Decimal
    total_account_per_lot: Decimal | None
    complete: bool
    missing: tuple[str, ...]

    def audit_view(self) -> dict[str, Any]:
        return {
            "spread_points": str(self.spread_points),
            "slippage_points": str(self.slippage_points),
            "commission_points": str(self.commission_points),
            "total_points": str(self.total_points),
            "total_account_per_lot": None
            if self.total_account_per_lot is None
            else str(self.total_account_per_lot),
            "complete": self.complete,
            "missing": list(self.missing),
        }


def cost_for(
    *,
    spec: InstrumentSpec,
    cost: CostTerms,
    spread_points: Decimal | None,
) -> TradeCost:
    """Price a round turn.  Every unpriced term lands in ``missing``."""

    missing: list[str] = []
    spread = spread_points
    if spread is None:
        missing.append("spread")
        spread = Decimal(0)
    spread = abs(_d(spread))
    slippage = cost.slippage_points * Decimal(2)  # both sides
    point_value = spec.point_value_per_lot
    commission_points = Decimal(0)
    if not cost.commission_known:
        missing.append("commission")
    elif point_value is None or point_value <= 0:
        missing.append("point_value")
    else:
        commission_points = cost.commission_per_lot / point_value
    total = spread + slippage + commission_points
    total_account = None if point_value is None else total * point_value
    return TradeCost(
        spread_points=spread,
        slippage_points=slippage,
        commission_points=commission_points,
        total_points=total,
        total_account_per_lot=total_account,
        complete=not missing,
        missing=tuple(missing),
    )


@dataclass(frozen=True)
class ExpectedValue:
    strategy: str
    fingerprint: str
    side: str
    p: float
    p_sd: float
    probability_basis: str
    probability_samples: int
    stop_points: Decimal
    target_points: Decimal
    gross_points: Decimal
    cost: TradeCost
    net_points: Decimal
    net_sd_points: Decimal
    lower_bound_points: Decimal
    upper_bound_points: Decimal
    net_account_per_lot: Decimal | None
    rewards_to_risk: Decimal
    cost_to_gross: Decimal | None
    verdict: str
    reasons: tuple[str, ...]

    @property
    def accepted(self) -> bool:
        return self.verdict == ACCEPT

    def audit_view(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "fingerprint": self.fingerprint,
            "side": self.side,
            "p": round(self.p, 4),
            "p_sd": round(self.p_sd, 4),
            "probability_basis": self.probability_basis,
            "probability_samples": self.probability_samples,
            "stop_points": str(self.stop_points),
            "target_points": str(self.target_points),
            "gross_points": str(round(self.gross_points, 4)),
            "cost": self.cost.audit_view(),
            "net_points": str(round(self.net_points, 4)),
            "net_sd_points": str(round(self.net_sd_points, 4)),
            "lower_bound_points": str(round(self.lower_bound_points, 4)),
            "upper_bound_points": str(round(self.upper_bound_points, 4)),
            "net_account_per_lot": None
            if self.net_account_per_lot is None
            else str(round(self.net_account_per_lot, 6)),
            "rewards_to_risk": str(round(self.rewards_to_risk, 4)),
            "cost_to_gross": None
            if self.cost_to_gross is None
            else str(round(self.cost_to_gross, 4)),
            "verdict": self.verdict,
            "reasons": list(self.reasons),
        }


def evaluate(
    signal: Signal,
    probability: ProbabilityEstimate,
    *,
    spec: InstrumentSpec,
    cost: CostTerms,
    spread_points: Decimal | None,
    min_edge_points: Decimal,
    uncertainty_z: Decimal = Decimal("1.0"),
) -> ExpectedValue:
    """Compute the expected net value and decide whether it clears the floor."""

    reasons: list[str] = []
    stop = signal.stop_points
    target = signal.target_points
    if stop <= 0 or target <= 0:
        reasons.append(BRACKET_INVALID)
    trade_cost = cost_for(spec=spec, cost=cost, spread_points=spread_points)
    if not trade_cost.complete:
        reasons.append(COST_UNKNOWN)
    if spread_points is None:
        reasons.append(SPREAD_UNKNOWN)
    p = float(probability.p)
    if probability.samples < 0:
        reasons.append(NO_PROBABILITY)

    gross = Decimal(str(p)) * target - Decimal(str(1.0 - p)) * stop
    net = gross - trade_cost.total_points
    # The bracket width is the sensitivity of the outcome to the probability, so the
    # standard deviation of the net value is the bracket times the sd of p.
    bracket = target + stop
    net_sd = bracket * Decimal(str(probability.sd))
    z = _d(uncertainty_z)
    lower = net - z * net_sd
    upper = net + z * net_sd
    point_value = spec.point_value_per_lot
    net_account = None if point_value is None else net * point_value
    rewards_to_risk = (target / stop) if stop > 0 else Decimal(0)
    cost_to_gross = (trade_cost.total_points / gross) if gross > 0 else None

    if gross <= 0 and GROSS_NOT_POSITIVE not in reasons:
        reasons.append(GROSS_NOT_POSITIVE)
    if gross > 0 and cost_to_gross is not None and cost_to_gross >= Decimal(1):
        reasons.append(COST_EXCEEDS_GROSS)
    if not reasons and lower < _d(min_edge_points):
        reasons.append(BELOW_MIN_EDGE)

    verdict = ACCEPT if not reasons else REJECT
    return ExpectedValue(
        strategy=signal.strategy,
        fingerprint=signal.parameter_fingerprint,
        side=signal.side,
        p=p,
        p_sd=float(probability.sd),
        probability_basis=probability.basis,
        probability_samples=probability.samples,
        stop_points=stop,
        target_points=target,
        gross_points=gross,
        cost=trade_cost,
        net_points=net,
        net_sd_points=net_sd,
        lower_bound_points=lower,
        upper_bound_points=upper,
        net_account_per_lot=net_account,
        rewards_to_risk=rewards_to_risk,
        cost_to_gross=cost_to_gross,
        verdict=verdict,
        reasons=tuple(reasons),
    )


@dataclass(frozen=True)
class ExpectancyRecord:
    n: int
    mean_points: Decimal
    sd_points: Decimal
    standard_error: Decimal
    lower_95: Decimal
    upper_95: Decimal
    wins: int
    losses: int
    gross_win_points: Decimal
    gross_loss_points: Decimal

    @property
    def win_rate(self) -> float:
        return self.wins / self.n if self.n else 0.0

    @property
    def profit_factor(self) -> float | None:
        if self.gross_loss_points <= 0:
            return None
        return float(self.gross_win_points / self.gross_loss_points)

    def audit_view(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "mean_points": str(round(self.mean_points, 4)),
            "sd_points": str(round(self.sd_points, 4)),
            "standard_error": str(round(self.standard_error, 4)),
            "lower_95": str(round(self.lower_95, 4)),
            "upper_95": str(round(self.upper_95, 4)),
            "wins": self.wins,
            "losses": self.losses,
            "win_rate": round(self.win_rate, 4),
            "profit_factor": None if self.profit_factor is None else round(self.profit_factor, 4),
            "note": (
                "Realised net points per trade. A confidence interval that includes zero "
                "is a result of 'not distinguishable from nothing', not a small edge."
            ),
        }


class ExpectancyTracker:
    """Realised net points per strategy fingerprint, with a 95 % interval."""

    def __init__(self) -> None:
        self._values: dict[str, list[Decimal]] = {}

    def record(self, fingerprint: str, net_points: Decimal) -> None:
        self._values.setdefault(fingerprint, []).append(_d(net_points))

    def values(self, fingerprint: str) -> list[Decimal]:
        return list(self._values.get(fingerprint, ()))

    def summary(self, fingerprint: str) -> ExpectancyRecord | None:
        values = self._values.get(fingerprint)
        if not values:
            return None
        n = len(values)
        mean = sum(values) / Decimal(n)
        if n > 1:
            variance = sum((value - mean) ** 2 for value in values) / Decimal(n - 1)
            sd = Decimal(str(math.sqrt(float(variance))))
        else:
            sd = Decimal(0)
        se = sd / Decimal(str(math.sqrt(n))) if n else Decimal(0)
        # 1.96 is the normal quantile; with small n this interval is optimistic, which
        # is stated rather than corrected by a distribution this build does not carry.
        lower, upper = mean - Decimal("1.96") * se, mean + Decimal("1.96") * se
        wins = sum(1 for value in values if value > 0)
        gross_win = sum((value for value in values if value > 0), Decimal(0))
        gross_loss = -sum((value for value in values if value < 0), Decimal(0))
        return ExpectancyRecord(
            n=n,
            mean_points=mean,
            sd_points=sd,
            standard_error=se,
            lower_95=lower,
            upper_95=upper,
            wins=wins,
            losses=n - wins,
            gross_win_points=gross_win,
            gross_loss_points=gross_loss,
        )

    def audit_view(self) -> dict[str, Any]:
        return {
            fingerprint[:12]: self.summary(fingerprint).audit_view()
            for fingerprint in sorted(self._values)
            if self.summary(fingerprint) is not None
        }


def required_win_rate(
    *,
    stop_points: Decimal,
    target_points: Decimal,
    cost_points: Decimal,
) -> float:
    """The break-even probability for this bracket after costs.

    Solving ``p·target − (1−p)·stop − cost = 0`` gives
    ``p = (stop + cost) / (stop + target)``.  Reported next to every EV so a reader
    can compare the estimate with what it has to beat.
    """

    denominator = stop_points + target_points
    if denominator <= 0:
        return 1.0
    return float((stop_points + cost_points) / denominator)


__all__ = [
    "ACCEPT",
    "BELOW_MIN_EDGE",
    "BRACKET_INVALID",
    "COST_EXCEEDS_GROSS",
    "COST_UNKNOWN",
    "ExpectancyRecord",
    "ExpectancyTracker",
    "ExpectedValue",
    "GROSS_NOT_POSITIVE",
    "NO_PROBABILITY",
    "REJECT",
    "SPREAD_UNKNOWN",
    "TradeCost",
    "cost_for",
    "evaluate",
    "required_win_rate",
]
