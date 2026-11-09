"""BAYES: probability estimates with an explicit amount of uncertainty.

Every probability this module returns is a **posterior of a Beta distribution**,
so it arrives with a sample count, a standard deviation and a credible interval.
The EV engine is given the interval, not just the point: an estimate from four
observations must not be able to justify a trade that an estimate from four
hundred would refuse.

Three separate uses, kept apart on purpose:

* **win rate** for a (strategy, regime) pair, updated from realised outcomes;
* **flow** — the probability that the next tick is up, from a volume-weighted
  uptick share;
* **calibration** — the check that a predicted probability means what it says,
  reported as a reliability table, Brier score and expected calibration error.

What this module will not do is call a broker feed the market.  The tick rule
applied to one MT5 quote stream measures that stream.  That sentence travels with
every flow number (:data:`BROKER_FEED_CAVEAT`) and is repeated in the audit view.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable, Sequence

#: Prior for a win rate: Jeffreys, which is what "no evidence" should look like on a
#: probability scale rather than a confident 50 %.
JEFFREYS_ALPHA = 0.5
JEFFREYS_BETA = 0.5

#: How the evidence is described.
BASIS_PRIOR_ONLY = "prior_only"
BASIS_OBSERVED_OUTCOMES = "observed_outcomes"
BASIS_FEED_PROXY = "broker_feed_tick_rule_proxy"

BROKER_FEED_CAVEAT = (
    "Spot FX is decentralised: this is one broker's quote stream, not the global "
    "order book. Tick-rule flow measures what this feed printed; it is not market-wide "
    "buying or selling, and another broker's feed would give another number."
)


# ---------------------------------------------------------------------------
# the beta distribution, without a dependency
# ---------------------------------------------------------------------------


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function (Lentz's method)."""

    tiny = 1e-30
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < tiny:
        d = tiny
    d = 1.0 / d
    h = d
    for m in range(1, 200):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < tiny:
            d = tiny
        c = 1.0 + aa / c
        if abs(c) < tiny:
            c = tiny
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-12:
            break
    return h


def _log_gamma(x: float) -> float:
    return math.lgamma(x)


def regularized_incomplete_beta(a: float, b: float, x: float) -> float:
    """``I_x(a, b)`` — the Beta CDF, to about 1e-10 for the parameters used here."""

    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    log_front = (
        _log_gamma(a + b)
        - _log_gamma(a)
        - _log_gamma(b)
        + a * math.log(x)
        + b * math.log(1.0 - x)
    )
    front = math.exp(log_front)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def beta_quantile(a: float, b: float, p: float, *, tolerance: float = 1e-9) -> float:
    """Inverse CDF by bisection: slow, monotone and impossible to get subtly wrong."""

    if p <= 0.0:
        return 0.0
    if p >= 1.0:
        return 1.0
    low, high = 0.0, 1.0
    for _ in range(120):
        middle = (low + high) / 2.0
        value = regularized_incomplete_beta(a, b, middle)
        if abs(value - p) <= tolerance:
            return middle
        if value < p:
            low = middle
        else:
            high = middle
    return (low + high) / 2.0


@dataclass(frozen=True)
class BetaPosterior:
    """A probability with its evidence attached."""

    alpha: float
    beta: float
    samples: int = 0
    basis: str = BASIS_PRIOR_ONLY
    label: str = ""

    @property
    def mean(self) -> float:
        total = self.alpha + self.beta
        return self.alpha / total if total > 0 else 0.5

    @property
    def variance(self) -> float:
        total = self.alpha + self.beta
        if total <= 0:
            return 0.25
        return (self.alpha * self.beta) / (total * total * (total + 1.0))

    @property
    def sd(self) -> float:
        return math.sqrt(max(0.0, self.variance))

    def interval(self, level: float = 0.8) -> tuple[float, float]:
        tail = (1.0 - level) / 2.0
        return (
            beta_quantile(self.alpha, self.beta, tail),
            beta_quantile(self.alpha, self.beta, 1.0 - tail),
        )

    def updated(self, *, wins: int, losses: int) -> "BetaPosterior":
        if wins < 0 or losses < 0:
            raise ValueError("counts cannot be negative")
        return BetaPosterior(
            alpha=self.alpha + wins,
            beta=self.beta + losses,
            samples=self.samples + wins + losses,
            basis=BASIS_OBSERVED_OUTCOMES if (self.samples + wins + losses) else self.basis,
            label=self.label,
        )

    def audit_view(self) -> dict[str, Any]:
        low, high = self.interval()
        return {
            "label": self.label,
            "mean": round(self.mean, 4),
            "sd": round(self.sd, 4),
            "interval_80": [round(low, 4), round(high, 4)],
            "samples": self.samples,
            "basis": self.basis,
            "alpha": round(self.alpha, 4),
            "beta": round(self.beta, 4),
        }


@dataclass(frozen=True)
class ProbabilityEstimate:
    """What the rest of the desk consumes: point estimate plus its spread."""

    p: float
    sd: float
    low: float
    high: float
    samples: int
    basis: str
    label: str

    @classmethod
    def from_posterior(cls, posterior: BetaPosterior, *, label: str = "") -> "ProbabilityEstimate":
        low, high = posterior.interval()
        return cls(
            p=posterior.mean,
            sd=posterior.sd,
            low=low,
            high=high,
            samples=posterior.samples,
            basis=posterior.basis,
            label=label or posterior.label,
        )

    @property
    def one_sigma_low(self) -> float:
        return max(0.0, self.p - self.sd)

    def audit_view(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "p": round(self.p, 4),
            "sd": round(self.sd, 4),
            "interval_80": [round(self.low, 4), round(self.high, 4)],
            "samples": self.samples,
            "basis": self.basis,
        }


# ---------------------------------------------------------------------------
# outcome evidence
# ---------------------------------------------------------------------------


class OutcomeBook:
    """Win/loss counts per (strategy fingerprint, regime), with a Jeffreys prior.

    The key is the **fingerprint**, not the strategy name: a strategy whose
    parameters changed has a different posterior, so yesterday's evidence cannot
    silently promote today's code.
    """

    def __init__(self) -> None:
        self._wins: dict[tuple[str, str], int] = {}
        self._losses: dict[tuple[str, str], int] = {}

    def record(self, *, fingerprint: str, regime: str, won: bool) -> None:
        key = (fingerprint, regime)
        if won:
            self._wins[key] = self._wins.get(key, 0) + 1
        else:
            self._losses[key] = self._losses.get(key, 0) + 1

    def counts(self, fingerprint: str, regime: str) -> tuple[int, int]:
        key = (fingerprint, regime)
        return self._wins.get(key, 0), self._losses.get(key, 0)

    def posterior(self, fingerprint: str, regime: str) -> BetaPosterior:
        wins, losses = self.counts(fingerprint, regime)
        posterior = BetaPosterior(JEFFREYS_ALPHA, JEFFREYS_BETA, label=regime)
        return posterior.updated(wins=wins, losses=losses) if (wins or losses) else posterior

    def estimate(self, fingerprint: str, regime: str) -> ProbabilityEstimate:
        return ProbabilityEstimate.from_posterior(
            self.posterior(fingerprint, regime), label=f"{fingerprint[:8]}:{regime}"
        )

    def pooled(self, fingerprint: str) -> BetaPosterior:
        wins = sum(value for (key, _regime), value in self._wins.items() if key == fingerprint)
        losses = sum(value for (key, _regime), value in self._losses.items() if key == fingerprint)
        posterior = BetaPosterior(JEFFREYS_ALPHA, JEFFREYS_BETA, label="pooled")
        return posterior.updated(wins=wins, losses=losses) if (wins or losses) else posterior

    def audit_view(self) -> dict[str, Any]:
        return {
            "cells": {
                f"{key[0][:8]}|{key[1]}": {
                    "wins": self._wins.get(key, 0),
                    "losses": self._losses.get(key, 0),
                }
                for key in sorted(set(self._wins) | set(self._losses))
            },
            "note": "posteriors are keyed by parameter fingerprint, not by strategy name",
        }


# ---------------------------------------------------------------------------
# flow
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FlowEstimate:
    """Probability that the next quote prints up, from this feed's own ticks."""

    estimate: ProbabilityEstimate
    buy_volume: float
    sell_volume: float
    ticks: int
    caveat: str = BROKER_FEED_CAVEAT

    def audit_view(self) -> dict[str, Any]:
        return {
            **self.estimate.audit_view(),
            "buy_volume": round(self.buy_volume, 4),
            "sell_volume": round(self.sell_volume, 4),
            "ticks": self.ticks,
            "caveat": self.caveat,
        }


def flow_from_ticks(mids: Sequence[float], volumes: Sequence[float | None]) -> FlowEstimate:
    """Volume-weighted uptick share as a Beta posterior.

    The tick rule is the only direction signal a quote stream carries: an uptick is
    treated as buy-side, a downtick as sell-side.  Where the feed reports no volume
    the share is computed unweighted *and* the estimate is marked with zero samples,
    which makes the EV engine refuse rather than trade on a proxy built from nothing.
    """

    buy = 0.0
    sell = 0.0
    ticks = 0
    for index in range(1, min(len(mids), len(volumes))):
        volume = volumes[index]
        weight = float(volume) if volume and float(volume) > 0 else 0.0
        if weight <= 0:
            continue
        ticks += 1
        if mids[index] > mids[index - 1]:
            buy += weight
        elif mids[index] < mids[index - 1]:
            sell += weight
    if ticks == 0:
        return FlowEstimate(
            estimate=ProbabilityEstimate(
                p=0.5, sd=0.5, low=0.0, high=1.0, samples=0, basis=BASIS_PRIOR_ONLY, label="flow"
            ),
            buy_volume=0.0,
            sell_volume=0.0,
            ticks=0,
        )
    # The sample size is the number of usable ticks; the *share* is volume-weighted.
    # Using the traded volume itself as a count would invent evidence: the units are
    # arbitrary, so a quiet symbol with large prints would look better sampled than a
    # liquid one.
    posterior = BetaPosterior(JEFFREYS_ALPHA, JEFFREYS_BETA, label="flow")
    share = buy / (buy + sell) if (buy + sell) > 0 else 0.5
    wins = int(round(ticks * share))
    posterior = posterior.updated(wins=wins, losses=max(0, ticks - wins))
    estimate = ProbabilityEstimate.from_posterior(posterior, label="flow")
    return FlowEstimate(
        estimate=ProbabilityEstimate(
            p=estimate.p,
            sd=estimate.sd,
            low=estimate.low,
            high=estimate.high,
            samples=ticks,
            basis=BASIS_FEED_PROXY,
            label="flow",
        ),
        buy_volume=buy,
        sell_volume=sell,
        ticks=ticks,
    )


# ---------------------------------------------------------------------------
# calibration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CalibrationBin:
    lower: float
    upper: float
    predicted: float
    observed: float
    samples: int

    def audit_view(self) -> dict[str, Any]:
        return {
            "range": [round(self.lower, 2), round(self.upper, 2)],
            "predicted": round(self.predicted, 4),
            "observed": round(self.observed, 4),
            "samples": self.samples,
        }


@dataclass(frozen=True)
class CalibrationReport:
    bins: tuple[CalibrationBin, ...]
    brier: float
    expected_calibration_error: float
    samples: int
    verdict: str

    def audit_view(self) -> dict[str, Any]:
        return {
            "bins": [item.audit_view() for item in self.bins],
            "brier": round(self.brier, 5),
            "expected_calibration_error": round(self.expected_calibration_error, 5),
            "samples": self.samples,
            "verdict": self.verdict,
            "note": (
                "Calibration is measured on out-of-sample predictions only; a calibrated "
                "probability is still not an edge, it is a probability that means what it says."
            ),
        }


def calibrate(
    predictions: Iterable[tuple[float, bool]],
    *,
    buckets: int = 5,
    max_error: float = 0.10,
) -> CalibrationReport:
    """Reliability table, Brier score and ECE over (predicted, outcome) pairs.

    With too few observations the verdict is ``insufficient_evidence``; it is never
    ``calibrated`` by default.
    """

    pairs = [(min(1.0, max(0.0, float(p))), bool(outcome)) for p, outcome in predictions]
    if not pairs:
        return CalibrationReport((), 1.0, 1.0, 0, "insufficient_evidence")
    groups: dict[int, list[tuple[float, bool]]] = {}
    for predicted, outcome in pairs:
        index = min(buckets - 1, int(predicted * buckets))
        groups.setdefault(index, []).append((predicted, outcome))
    rows: list[CalibrationBin] = []
    weighted_error = 0.0
    for index in sorted(groups):
        group = groups[index]
        predicted_mean = sum(item[0] for item in group) / len(group)
        observed = sum(1 for item in group if item[1]) / len(group)
        rows.append(
            CalibrationBin(
                lower=index / buckets,
                upper=(index + 1) / buckets,
                predicted=predicted_mean,
                observed=observed,
                samples=len(group),
            )
        )
        weighted_error += abs(observed - predicted_mean) * len(group)
    brier = sum((predicted - (1.0 if outcome else 0.0)) ** 2 for predicted, outcome in pairs) / len(pairs)
    ece = weighted_error / len(pairs)
    if len(pairs) < 50:
        verdict = "insufficient_evidence"
    elif ece <= max_error:
        verdict = "within_declared_error"
    else:
        verdict = "outside_declared_error"
    return CalibrationReport(
        bins=tuple(rows),
        brier=brier,
        expected_calibration_error=ece,
        samples=len(pairs),
        verdict=verdict,
    )


def brier_score(predictions: Iterable[tuple[float, bool]]) -> float:
    pairs = list(predictions)
    if not pairs:
        return 1.0
    return sum(
        (float(predicted) - (1.0 if outcome else 0.0)) ** 2 for predicted, outcome in pairs
    ) / len(pairs)


def fraction_to_decimal(value: float, places: int = 4) -> Decimal:
    return Decimal(str(round(value, places)))


__all__ = [
    "BASIS_FEED_PROXY",
    "BASIS_OBSERVED_OUTCOMES",
    "BASIS_PRIOR_ONLY",
    "BROKER_FEED_CAVEAT",
    "BetaPosterior",
    "CalibrationBin",
    "CalibrationReport",
    "FlowEstimate",
    "OutcomeBook",
    "ProbabilityEstimate",
    "beta_quantile",
    "brier_score",
    "calibrate",
    "flow_from_ticks",
    "fraction_to_decimal",
    "regularized_incomplete_beta",
]
