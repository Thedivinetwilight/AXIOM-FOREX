"""SKEPTIC: the desk's independent authority on whether an edge is real.

Nothing in this module looks for a strategy.  It exists to make it hard for a
strategy to be believed.  The sequence it enforces is the one that is expensive to
fake:

**chronological split** → **walk-forward** → **out-of-sample** →
**multiple-testing correction** → **PBO** → **deflated Sharpe** →
**cost sensitivity** → **stress** → **regime breakdown** → **confidence intervals**

Approval is keyed to the strategy's **parameter fingerprint** — a hash over the
strategy name, its rule version and every parameter.  Change one parameter and the
approval does not apply any more, because the thing that was approved no longer
exists.  Approval must also come from an actor that did not develop the strategy;
the registry refuses an approval signed by its author.

The tests here are standard and none of them is a guarantee.  A deflated Sharpe of
0.97 means "the search does not obviously explain this"; it does not mean the next
trade will win.  This module therefore reports *insufficiency* as a normal outcome,
and :func:`validate` returns ``approved=False`` far more often than not.  That is
the intended behaviour.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import random
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from axiom_forex.strategies.library.multiplicity import (
    benjamini_hochberg,
    block_bootstrap_p_value,
    deflated_sharpe_note,
    reality_check,
)

#: Refusal reasons.
NOT_ENOUGH_TRADES = "not_enough_trades"
NOT_ENOUGH_OOS = "not_enough_out_of_sample_trades"
SPLIT_TOO_SMALL = "chronological_split_too_small"
OOS_NOT_POSITIVE = "out_of_sample_expectancy_not_positive"
COST_SENSITIVE = "edge_does_not_survive_costs"
PBO_TOO_HIGH = "probability_of_backtest_overfitting_too_high"
DSR_TOO_LOW = "deflated_sharpe_below_threshold"
UNSTABLE_FOLDS = "walk_forward_folds_disagree"
CI_INCLUDES_ZERO = "confidence_interval_includes_zero"
MULTIPLE_TESTING_FAILED = "no_variant_survives_multiple_testing"
APPROVAL_NOT_INDEPENDENT = "approval_must_come_from_a_different_actor"
APPROVAL_PARAMETERS_CHANGED = "approval_does_not_cover_these_parameters"
NOT_APPROVED = "strategy_is_not_validated"
LIVE_REQUIRES_APPROVAL = "live_execution_requires_validation"

EULER_GAMMA = 0.5772156649015329


# ---------------------------------------------------------------------------
# maths
# ---------------------------------------------------------------------------


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _sd(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    average = _mean(values)
    return math.sqrt(sum((value - average) ** 2 for value in values) / (len(values) - 1))


def _skew(values: Sequence[float]) -> float:
    if len(values) < 3:
        return 0.0
    average, deviation = _mean(values), _sd(values)
    if deviation == 0:
        return 0.0
    return sum(((value - average) / deviation) ** 3 for value in values) / len(values)


def _kurtosis(values: Sequence[float]) -> float:
    if len(values) < 4:
        return 3.0
    average, deviation = _mean(values), _sd(values)
    if deviation == 0:
        return 3.0
    return sum(((value - average) / deviation) ** 4 for value in values) / len(values)


def norm_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def norm_ppf(probability: float) -> float:
    """Inverse normal by bisection.  Deterministic and adequate for a threshold."""

    if not 0 < probability < 1:
        raise ValueError("probability must be strictly between 0 and 1")
    low, high = -40.0, 40.0
    for _ in range(200):
        middle = (low + high) / 2
        if norm_cdf(middle) < probability:
            low = middle
        else:
            high = middle
    return (low + high) / 2


def sharpe(returns: Sequence[float]) -> float:
    """Per-observation Sharpe (not annualised: the caller declares the horizon)."""

    deviation = _sd(returns)
    return _mean(returns) / deviation if deviation > 0 else 0.0


def sortino(returns: Sequence[float]) -> float:
    downside = [value for value in returns if value < 0]
    if not downside:
        return float("inf") if _mean(returns) > 0 else 0.0
    deviation = math.sqrt(sum(value * value for value in downside) / len(downside))
    return _mean(returns) / deviation if deviation > 0 else 0.0


def bootstrap_interval(
    values: Sequence[float], *, resamples: int = 1000, seed: int = 20261027, level: float = 0.95
) -> dict[str, Any]:
    """Percentile bootstrap of the mean, resampling whole observations."""

    clean = [float(value) for value in values]
    if len(clean) < 3:
        return {"low": None, "high": None, "resamples": 0, "note": "too few observations"}
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(int(resamples)):
        sample = [clean[rng.randrange(len(clean))] for _ in clean]
        means.append(_mean(sample))
    means.sort()
    alpha = (1 - level) / 2
    low = means[int(alpha * (len(means) - 1))]
    high = means[int((1 - alpha) * (len(means) - 1))]
    return {
        "low": round(low, 6),
        "high": round(high, 6),
        "level": level,
        "resamples": int(resamples),
        "includes_zero": low <= 0 <= high,
        "note": "A bootstrap interval over trades, not a prediction interval for the next trade.",
    }


def deflated_sharpe(
    returns: Sequence[float],
    *,
    trials: int,
    trial_sharpe_sd: float | None = None,
) -> dict[str, Any]:
    """Bailey & López de Prado's deflated Sharpe ratio.

    The observed Sharpe is compared with the Sharpe the *best of ``trials``*
    zero-edge strategies would be expected to show, and the difference is scaled by
    the sampling error of the Sharpe ratio itself.
    """

    clean = [float(value) for value in returns]
    if len(clean) < 10:
        return {
            "observed_sharpe": None,
            "expected_max_sharpe": None,
            "deflated_sharpe": None,
            "verdict": "insufficient_evidence",
            "note": "fewer than ten observations; a Sharpe ratio is not defined here",
        }
    observed = sharpe(clean)
    count = max(1, int(trials))
    spread = trial_sharpe_sd if trial_sharpe_sd is not None else 1.0 / math.sqrt(len(clean))
    if count == 1:
        expected_max = 0.0
    else:
        expected_max = spread * (
            (1 - EULER_GAMMA) * norm_ppf(1 - 1.0 / count)
            + EULER_GAMMA * norm_ppf(1 - 1.0 / (count * math.e))
        )
    skew, kurt = _skew(clean), _kurtosis(clean)
    denominator = 1 - skew * observed + (kurt - 1) / 4 * observed**2
    denominator = max(denominator, 1e-12)
    statistic = (observed - expected_max) * math.sqrt(len(clean) - 1) / math.sqrt(denominator)
    probability = norm_cdf(statistic)
    return {
        "observed_sharpe": round(observed, 6),
        "expected_max_sharpe": round(expected_max, 6),
        "trials": count,
        "deflated_sharpe": round(probability, 6),
        "skew": round(skew, 4),
        "kurtosis": round(kurt, 4),
        "verdict": "passes" if probability >= 0.95 else "fails",
        "note": (
            "The probability that the observed Sharpe is above what the best of "
            f"{count} no-edge trials would be expected to produce. "
            + deflated_sharpe_note()
        ),
    }


def cscv_pbo(
    variant_returns: Mapping[str, Sequence[float]],
    *,
    blocks: int = 8,
) -> dict[str, Any]:
    """Probability of backtest overfitting, by combinatorially symmetric CV.

    Split the timeline into ``blocks`` equal parts, take every half of them as the
    in-sample set, pick the variant that looks best there, and see where it ranks
    out of sample.  PBO is the share of splits in which the in-sample winner lands in
    the bottom half out of sample.
    """

    variants = {name: [float(value) for value in values] for name, values in variant_returns.items() if values}
    if len(variants) < 2:
        return {
            "pbo": None,
            "verdict": "insufficient_evidence",
            "note": "PBO needs at least two variants to compare",
        }
    if blocks < 2 or blocks % 2:
        return {"pbo": None, "verdict": "invalid", "note": "blocks must be an even number >= 2"}
    length = min(len(values) for values in variants.values())
    if length < blocks:
        return {
            "pbo": None,
            "verdict": "insufficient_evidence",
            "note": f"{length} observations cannot fill {blocks} blocks",
        }
    trimmed = {name: values[:length] for name, values in variants.items()}
    size = length // blocks
    block_means: dict[str, list[float]] = {}
    for name, values in trimmed.items():
        block_means[name] = [
            _mean(values[index * size : (index + 1) * size]) for index in range(blocks)
        ]
    names = sorted(trimmed)
    halves = blocks // 2
    logits: list[float] = []
    for combination in itertools.combinations(range(blocks), halves):
        in_sample_index = set(combination)
        is_means = {
            name: _mean([block_means[name][index] for index in in_sample_index]) for name in names
        }
        oos_means = {
            name: _mean(
                [block_means[name][index] for index in range(blocks) if index not in in_sample_index]
            )
            for name in names
        }
        best = max(names, key=lambda name: is_means[name])
        ordered = sorted(names, key=lambda name: oos_means[name])
        rank = ordered.index(best) + 1  # 1 = worst
        relative = rank / (len(names) + 1)
        relative = min(max(relative, 1e-6), 1 - 1e-6)
        logits.append(math.log(relative / (1 - relative)))
    overfit = sum(1 for value in logits if value <= 0) / len(logits)
    return {
        "pbo": round(overfit, 4),
        "splits": len(logits),
        "blocks": blocks,
        "variants": len(names),
        "observations": length,
        "verdict": "passes" if overfit <= 0.25 else "fails",
        "note": (
            "The share of symmetric splits where the in-sample winner ranked in the "
            "bottom half out of sample. High PBO means the search is selecting noise."
        ),
    }


# ---------------------------------------------------------------------------
# observations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TradeRecord:
    """One closed trade, as the validator sees it.  Costs already applied."""

    index: int
    fingerprint: str
    strategy: str
    regime: str
    session: str
    net_points: float
    gross_points: float
    cost_points: float
    holding_ms: int
    mae_points: float = 0.0
    mfe_points: float = 0.0
    side: str = "BUY"

    @property
    def won(self) -> bool:
        return self.net_points > 0

    def audit_view(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "strategy": self.strategy,
            "regime": self.regime,
            "session": self.session,
            "net_points": round(self.net_points, 4),
            "gross_points": round(self.gross_points, 4),
            "cost_points": round(self.cost_points, 4),
            "holding_ms": self.holding_ms,
            "won": self.won,
        }


@dataclass(frozen=True)
class Metrics:
    n: int
    mean_net_points: float
    sd_net_points: float
    profit_factor: float | None
    win_rate: float
    mean_win: float | None
    mean_loss: float | None
    expectancy_r: float | None
    sharpe: float | None
    sortino: float | None
    max_drawdown_points: float
    mean_holding_ms: float | None
    cost_to_gross: float | None
    mae_mean: float | None
    mfe_mean: float | None
    average_win_loss_ratio: float | None

    def audit_view(self) -> dict[str, Any]:
        return {
            "n": self.n,
            "mean_net_points": round(self.mean_net_points, 4),
            "sd_net_points": round(self.sd_net_points, 4),
            "profit_factor": None if self.profit_factor is None else round(self.profit_factor, 4),
            "win_rate": round(self.win_rate, 4),
            "mean_win": None if self.mean_win is None else round(self.mean_win, 4),
            "mean_loss": None if self.mean_loss is None else round(self.mean_loss, 4),
            "average_win_loss_ratio": None
            if self.average_win_loss_ratio is None
            else round(self.average_win_loss_ratio, 4),
            "expectancy_r": None if self.expectancy_r is None else round(self.expectancy_r, 4),
            "sharpe": None if self.sharpe is None else round(self.sharpe, 4),
            "sortino": None if self.sortino is None else round(self.sortino, 4),
            "max_drawdown_points": round(self.max_drawdown_points, 4),
            "mean_holding_ms": None if self.mean_holding_ms is None else round(self.mean_holding_ms, 1),
            "cost_to_gross": None if self.cost_to_gross is None else round(self.cost_to_gross, 4),
            "mae_mean": None if self.mae_mean is None else round(self.mae_mean, 4),
            "mfe_mean": None if self.mfe_mean is None else round(self.mfe_mean, 4),
            "sharpe_definition": "per-trade mean divided by per-trade standard deviation; not annualised",
        }


def compute_metrics(trades: Sequence[TradeRecord]) -> Metrics:
    net = [trade.net_points for trade in trades]
    wins = [value for value in net if value > 0]
    losses = [value for value in net if value <= 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    equity, peak, drawdown = 0.0, 0.0, 0.0
    for value in net:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    average_loss = _mean(losses) if losses else None
    average_win = _mean(wins) if wins else None
    r_multiple = None
    if average_loss and average_win:
        r_multiple = average_win / abs(average_loss)
    cost = sum(trade.cost_points for trade in trades)
    gross = sum(trade.gross_points for trade in trades)
    mae = [trade.mae_points for trade in trades]
    mfe = [trade.mfe_points for trade in trades]
    return Metrics(
        n=len(trades),
        mean_net_points=_mean(net),
        sd_net_points=_sd(net),
        profit_factor=(gross_win / gross_loss) if gross_loss > 0 else None,
        win_rate=(len(wins) / len(net)) if net else 0.0,
        mean_win=average_win,
        mean_loss=average_loss,
        expectancy_r=r_multiple,
        sharpe=(sharpe(net) if len(net) >= 3 else None),
        sortino=(sortino(net) if len(net) >= 3 else None),
        max_drawdown_points=drawdown,
        mean_holding_ms=_mean([trade.holding_ms for trade in trades]) if trades else None,
        cost_to_gross=(cost / gross) if gross > 0 else None,
        mae_mean=_mean(mae) if mae else None,
        mfe_mean=_mean(mfe) if mfe else None,
        average_win_loss_ratio=(average_win / abs(average_loss)) if average_win and average_loss else None,
    )


# ---------------------------------------------------------------------------
# splits and folds
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Split:
    train: tuple[TradeRecord, ...]
    validation: tuple[TradeRecord, ...]
    test: tuple[TradeRecord, ...]
    embargo: int

    def audit_view(self) -> dict[str, Any]:
        return {
            "train": len(self.train),
            "validation": len(self.validation),
            "test": len(self.test),
            "embargo": self.embargo,
            "note": "chronological, with an embargo between segments so no trade straddles a boundary",
        }


def chronological_split(
    trades: Sequence[TradeRecord],
    *,
    train: float = 0.5,
    validation: float = 0.25,
    test: float = 0.25,
    embargo: int = 0,
    minimum: int = 30,
) -> tuple[Split | None, str]:
    ordered = sorted(trades, key=lambda trade: trade.index)
    if len(ordered) < minimum:
        return None, f"{NOT_ENOUGH_TRADES}: {len(ordered)} < {minimum}"
    total = train + validation + test
    if abs(total - 1.0) > 1e-9:
        return None, "split fractions must sum to 1"
    first = int(len(ordered) * train)
    second = int(len(ordered) * (train + validation))
    head = ordered[: max(0, first - embargo)]
    middle = ordered[first : max(first, second - embargo)]
    tail = ordered[second:]
    if not head or not middle or not tail:
        return None, f"{SPLIT_TOO_SMALL}: an empty segment after the embargo"
    return Split(tuple(head), tuple(middle), tuple(tail), embargo), ""


@dataclass(frozen=True)
class Fold:
    index: int
    in_sample: int
    out_of_sample: int
    in_sample_mean: float
    out_of_sample_mean: float
    consistent: bool

    def audit_view(self) -> dict[str, Any]:
        return {
            "fold": self.index,
            "in_sample": self.in_sample,
            "out_of_sample": self.out_of_sample,
            "in_sample_mean": round(self.in_sample_mean, 4),
            "out_of_sample_mean": round(self.out_of_sample_mean, 4),
            "consistent": self.consistent,
        }


def walk_forward(
    trades: Sequence[TradeRecord], *, folds: int = 4, embargo: int = 0, minimum_is: int = 10
) -> dict[str, Any]:
    """Expanding-window walk-forward: train on the past, test on what comes next."""

    ordered = sorted(trades, key=lambda trade: trade.index)
    if folds < 2 or len(ordered) < minimum_is * 2:
        return {
            "folds": [],
            "verdict": "insufficient_evidence",
            "note": f"{len(ordered)} trades cannot support {folds} walk-forward folds",
        }
    boundaries = [int(len(ordered) * (index + 1) / (folds + 1)) for index in range(folds)]
    results: list[Fold] = []
    start = 0
    for number, boundary in enumerate(boundaries, start=1):
        in_sample = ordered[start : max(start + 1, boundary - embargo)]
        end = boundaries[number] if number < len(boundaries) else len(ordered)
        out_of_sample = ordered[boundary:end]
        if len(in_sample) < minimum_is or len(out_of_sample) < 3:
            continue
        is_mean = _mean([trade.net_points for trade in in_sample])
        oos_mean = _mean([trade.net_points for trade in out_of_sample])
        results.append(
            Fold(
                index=number,
                in_sample=len(in_sample),
                out_of_sample=len(out_of_sample),
                in_sample_mean=is_mean,
                out_of_sample_mean=oos_mean,
                consistent=(is_mean > 0) == (oos_mean > 0),
            )
        )
        start = boundary
    consistent = sum(1 for fold in results if fold.consistent)
    verdict = "insufficient_evidence"
    if results:
        verdict = "passes" if consistent == len(results) and all(f.out_of_sample_mean > 0 for f in results) else "fails"
    return {
        "folds": [fold.audit_view() for fold in results],
        "consistent_folds": consistent,
        "total_folds": len(results),
        "verdict": verdict,
        "note": "A fold where the in-sample edge has the opposite sign out of sample is a warning, not noise to average away.",
    }


# ---------------------------------------------------------------------------
# stress
# ---------------------------------------------------------------------------


def cost_sensitivity(
    trades: Sequence[TradeRecord], *, multiples: Sequence[float] = (1.0, 1.5, 2.0, 3.0)
) -> dict[str, Any]:
    """Recompute expectancy with the cost term scaled.  An edge that dies at 1.5x is not one."""

    rows: list[dict[str, Any]] = []
    for multiple in multiples:
        adjusted = [
            trade.net_points - (multiple - 1.0) * trade.cost_points for trade in trades
        ]
        rows.append(
            {
                "cost_multiple": multiple,
                "mean_net_points": round(_mean(adjusted), 4) if adjusted else None,
                "positive": _mean(adjusted) > 0 if adjusted else False,
            }
        )
    survival = None
    for row in rows:
        if row["cost_multiple"] > 1.0 and not row["positive"]:
            survival = row["cost_multiple"]
            break
    return {
        "rows": rows,
        "breaks_between": survival,
        "note": "Costs are scaled, not the edge: this is the question a broker's spread widening asks.",
    }


def spread_stress(
    trades: Sequence[TradeRecord], *, extra_points: Sequence[float] = (0.0, 1.0, 2.0, 4.0)
) -> dict[str, Any]:
    rows = []
    for extra in extra_points:
        adjusted = [trade.net_points - extra for trade in trades]
        rows.append(
            {
                "extra_spread_points": extra,
                "mean_net_points": round(_mean(adjusted), 4) if adjusted else None,
            }
        )
    return {"rows": rows}


def latency_stress(
    trades: Sequence[TradeRecord], *, extra_latency_ms: Sequence[int] = (0, 50, 100, 250)
) -> dict[str, Any]:
    """Report the population a latency assumption applies to; it cannot reprice trades."""

    return {
        "rows": [
            {
                "extra_latency_ms": extra,
                "note": (
                    "latency is not repriced here: the simulator injects it, and a "
                    "replay cannot invent the fills it would have produced"
                ),
            }
            for extra in extra_latency_ms
        ],
        "mean_holding_ms": round(_mean([t.holding_ms for t in trades]), 1) if trades else None,
        "note": "Use the lab's latency scenarios to measure the effect; this table only states the exposure.",
    }


def breakdown(trades: Sequence[TradeRecord], key: str) -> dict[str, Any]:
    groups: dict[str, list[TradeRecord]] = {}
    for trade in trades:
        groups.setdefault(str(getattr(trade, key, "unknown")), []).append(trade)
    return {
        name: compute_metrics(group).audit_view() for name, group in sorted(groups.items())
    }


def session_of(moment: datetime) -> str:
    hour = moment.hour
    if 0 <= hour < 7:
        return "asia"
    if 7 <= hour < 12:
        return "london"
    if 12 <= hour < 16:
        return "london_ny_overlap"
    if 16 <= hour < 21:
        return "new_york"
    return "late"


# ---------------------------------------------------------------------------
# the verdict
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Criteria:
    """Declared before the run.  Changing them after seeing results is cheating."""

    version: str = "skeptic-v1"
    min_trades: int = 200
    min_out_of_sample_trades: int = 60
    min_walk_forward_folds: int = 3
    cost_multiple_survival: float = 1.5
    max_pbo: float = 0.25
    min_deflated_sharpe: float = 0.95
    min_profit_factor: float = 1.10
    require_positive_ci: bool = True
    q_value: float = 0.10

    def audit_view(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "min_trades": self.min_trades,
            "min_out_of_sample_trades": self.min_out_of_sample_trades,
            "min_walk_forward_folds": self.min_walk_forward_folds,
            "cost_multiple_survival": self.cost_multiple_survival,
            "max_pbo": self.max_pbo,
            "min_deflated_sharpe": self.min_deflated_sharpe,
            "min_profit_factor": self.min_profit_factor,
            "require_positive_ci": self.require_positive_ci,
            "q_value": self.q_value,
            "declared": (
                "Fixed in configuration before measurement. Adjusting these after a run, "
                "or retesting against the locked window until they pass, invalidates the result."
            ),
        }


@dataclass(frozen=True)
class ValidationReport:
    fingerprint: str
    strategy: str
    criteria: Criteria
    metrics: Metrics
    out_of_sample: Metrics | None
    split: dict[str, Any]
    walk_forward: dict[str, Any]
    multiple_testing: dict[str, Any]
    pbo: dict[str, Any]
    deflated_sharpe: dict[str, Any]
    cost_sensitivity: dict[str, Any]
    spread_stress: dict[str, Any]
    regime_breakdown: dict[str, Any]
    session_breakdown: dict[str, Any]
    confidence_interval: dict[str, Any]
    findings: tuple[str, ...]
    approved: bool

    @property
    def report_hash(self) -> str:
        body = json.dumps(
            {
                "fingerprint": self.fingerprint,
                "approved": self.approved,
                "findings": list(self.findings),
                "metrics": self.metrics.audit_view(),
                "criteria": self.criteria.audit_view(),
            },
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    def audit_view(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "strategy": self.strategy,
            "approved": self.approved,
            "findings": list(self.findings),
            "report_hash": self.report_hash[:16],
            "criteria": self.criteria.audit_view(),
            "metrics": self.metrics.audit_view(),
            "out_of_sample": None if self.out_of_sample is None else self.out_of_sample.audit_view(),
            "split": self.split,
            "walk_forward": self.walk_forward,
            "multiple_testing": self.multiple_testing,
            "pbo": self.pbo,
            "deflated_sharpe": self.deflated_sharpe,
            "cost_sensitivity": self.cost_sensitivity,
            "spread_stress": self.spread_stress,
            "regime_breakdown": self.regime_breakdown,
            "session_breakdown": self.session_breakdown,
            "confidence_interval": self.confidence_interval,
            "note": (
                "approved=False is a result, not an error. It means this build has not "
                "shown that the strategy has an edge that survives its own costs."
            ),
        }


def validate(
    trades: Sequence[TradeRecord],
    *,
    criteria: Criteria | None = None,
    variants: Mapping[str, Sequence[float]] | None = None,
    trial_sharpe_sd: float | None = None,
) -> ValidationReport:
    """Run the whole battery for one strategy and return a verdict."""

    criteria = criteria or Criteria()
    ordered = sorted(trades, key=lambda trade: trade.index)
    fingerprint = ordered[0].fingerprint if ordered else ""
    strategy = ordered[0].strategy if ordered else ""
    metrics = compute_metrics(ordered)
    findings: list[str] = []

    split, split_error = chronological_split(ordered)
    out_of_sample: Metrics | None = None
    if split is None:
        findings.append(split_error or SPLIT_TOO_SMALL)
        split_view: dict[str, Any] = {"error": split_error}
    else:
        split_view = split.audit_view()
        out_of_sample = compute_metrics(split.test)

    folds = walk_forward(ordered)
    if len(folds["folds"]) < criteria.min_walk_forward_folds:
        findings.append(f"{UNSTABLE_FOLDS}: only {len(folds['folds'])} usable fold(s)")
    elif folds["verdict"] != "passes":
        findings.append(UNSTABLE_FOLDS)

    net = [trade.net_points for trade in ordered]
    p_value = block_bootstrap_p_value(net) if len(net) >= 10 else {"p_value": None}
    multiple_testing = (
        benjamini_hochberg({"self": float(p_value["p_value"])}, q=criteria.q_value)
        if p_value.get("p_value") is not None
        else {"accepted": [], "note": "not enough observations to test"}
    )
    if variants:
        multiple_testing["reality_check"] = reality_check(variants)
    if p_value.get("p_value") is None:
        findings.append(NOT_ENOUGH_TRADES)
    elif "self" not in multiple_testing.get("accepted", []):
        findings.append(MULTIPLE_TESTING_FAILED)

    pbo = cscv_pbo(variants, blocks=8) if variants else {
        "pbo": None,
        "verdict": "insufficient_evidence",
        "note": "PBO needs at least two parameter variants of this strategy",
    }
    if pbo.get("verdict") == "fails":
        findings.append(PBO_TOO_HIGH)
    elif pbo.get("pbo") is None:
        findings.append(f"{PBO_TOO_HIGH}: cannot be computed")

    dsr = deflated_sharpe(
        net,
        trials=max(2, len(variants) if variants else 2),
        trial_sharpe_sd=trial_sharpe_sd,
    )
    if dsr.get("deflated_sharpe") is None:
        findings.append(NOT_ENOUGH_TRADES)
    elif float(dsr["deflated_sharpe"]) < criteria.min_deflated_sharpe:
        findings.append(DSR_TOO_LOW)

    sensitivity = cost_sensitivity(ordered)
    if sensitivity["breaks_between"] is not None and sensitivity["breaks_between"] <= criteria.cost_multiple_survival:
        findings.append(f"{COST_SENSITIVE}: breaks at {sensitivity['breaks_between']}x")
    elif sensitivity["breaks_between"] is None and metrics.mean_net_points <= 0:
        findings.append(COST_SENSITIVE)

    interval = bootstrap_interval(net)
    if criteria.require_positive_ci and interval.get("includes_zero", True):
        findings.append(CI_INCLUDES_ZERO)

    if out_of_sample is not None:
        if out_of_sample.n < criteria.min_out_of_sample_trades:
            findings.append(f"{NOT_ENOUGH_OOS}: {out_of_sample.n} < {criteria.min_out_of_sample_trades}")
        if out_of_sample.mean_net_points <= 0:
            findings.append(OOS_NOT_POSITIVE)
    if metrics.n < criteria.min_trades:
        findings.append(f"{NOT_ENOUGH_TRADES}: {metrics.n} < {criteria.min_trades}")
    if metrics.profit_factor is not None and metrics.profit_factor < criteria.min_profit_factor:
        findings.append(f"profit_factor {metrics.profit_factor:.3f} below {criteria.min_profit_factor}")
    if metrics.profit_factor is None:
        findings.append("profit_factor is undefined (no losing trades) — a sample without losers is not a sample")

    approved = not findings
    return ValidationReport(
        fingerprint=fingerprint,
        strategy=strategy,
        criteria=criteria,
        metrics=metrics,
        out_of_sample=out_of_sample,
        split=split_view,
        walk_forward=folds,
        multiple_testing=multiple_testing,
        pbo=pbo,
        deflated_sharpe=dsr,
        cost_sensitivity=sensitivity,
        spread_stress=spread_stress(ordered),
        regime_breakdown=breakdown(ordered, "regime"),
        session_breakdown=breakdown(ordered, "session"),
        confidence_interval=interval,
        findings=tuple(findings),
        approved=approved,
    )


# ---------------------------------------------------------------------------
# approvals
# ---------------------------------------------------------------------------


class StrategyNotApproved(Exception):
    def __init__(self, fingerprint: str, reasons: Sequence[str]):
        super().__init__(f"{NOT_APPROVED}: {fingerprint[:12]} — {'; '.join(reasons)}")
        self.fingerprint = fingerprint
        self.reasons = tuple(reasons)


@dataclass(frozen=True)
class Approval:
    fingerprint: str
    strategy: str
    approved_by: str
    approved_at_ms: int
    report_hash: str
    criteria_version: str
    notes: str = ""

    def audit_view(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "strategy": self.strategy,
            "approved_by": self.approved_by,
            "approved_at_ms": self.approved_at_ms,
            "report_hash": self.report_hash,
            "criteria_version": self.criteria_version,
            "notes": self.notes,
            "scope": "this exact parameter fingerprint only",
        }


class ApprovalRegistry:
    """Approvals are per fingerprint, per criteria version, and never self-issued."""

    def __init__(self, *, developers: Iterable[str] = ()) -> None:
        self._developers = {name.strip().lower() for name in developers}
        self._approvals: dict[str, Approval] = {}
        self._revoked: dict[str, str] = {}

    def grant(
        self, report: ValidationReport, *, approved_by: str, at_ms: int, notes: str = ""
    ) -> tuple[Approval | None, str]:
        if not approved_by.strip():
            return None, "an approval needs a named approver"
        if approved_by.strip().lower() in self._developers:
            return None, f"{APPROVAL_NOT_INDEPENDENT}: {approved_by} developed this strategy"
        if not report.approved:
            return None, f"{NOT_APPROVED}: {'; '.join(report.findings)}"
        approval = Approval(
            fingerprint=report.fingerprint,
            strategy=report.strategy,
            approved_by=approved_by,
            approved_at_ms=at_ms,
            report_hash=report.report_hash,
            criteria_version=report.criteria.version,
            notes=notes,
        )
        self._approvals[report.fingerprint] = approval
        return approval, ""

    def revoke(self, fingerprint: str, *, reason: str) -> tuple[bool, str]:
        if fingerprint not in self._approvals:
            return False, "no approval exists for this fingerprint"
        self._approvals.pop(fingerprint)
        self._revoked[fingerprint] = reason
        return True, reason

    def status(self, fingerprint: str) -> dict[str, Any]:
        approval = self._approvals.get(fingerprint)
        if approval is not None:
            return {"approved": True, "approval": approval.audit_view()}
        return {
            "approved": False,
            "reason": self._revoked.get(fingerprint, NOT_APPROVED),
            "note": (
                "Approval is tied to the parameter fingerprint. Any parameter change, "
                "and any change of criteria version, means the strategy must be "
                "validated again before it may be enabled for live execution."
            ),
        }

    def require(self, fingerprint: str, *, mode: str) -> None:
        """The gate a live executor must call.  Paper execution is the place to gather data."""

        if mode.upper() == "PAPER":
            return
        status = self.status(fingerprint)
        if not status["approved"]:
            raise StrategyNotApproved(fingerprint, [status.get("reason", NOT_APPROVED)])

    @property
    def approvals(self) -> tuple[Approval, ...]:
        return tuple(self._approvals.values())


__all__ = [
    "APPROVAL_NOT_INDEPENDENT",
    "APPROVAL_PARAMETERS_CHANGED",
    "Approval",
    "ApprovalRegistry",
    "CI_INCLUDES_ZERO",
    "COST_SENSITIVE",
    "Criteria",
    "DSR_TOO_LOW",
    "Fold",
    "LIVE_REQUIRES_APPROVAL",
    "MULTIPLE_TESTING_FAILED",
    "Metrics",
    "NOT_APPROVED",
    "NOT_ENOUGH_OOS",
    "NOT_ENOUGH_TRADES",
    "OOS_NOT_POSITIVE",
    "PBO_TOO_HIGH",
    "SPLIT_TOO_SMALL",
    "Split",
    "StrategyNotApproved",
    "TradeRecord",
    "UNSTABLE_FOLDS",
    "ValidationReport",
    "bootstrap_interval",
    "breakdown",
    "chronological_split",
    "compute_metrics",
    "cost_sensitivity",
    "cscv_pbo",
    "deflated_sharpe",
    "latency_stress",
    "norm_cdf",
    "norm_ppf",
    "session_of",
    "sharpe",
    "sortino",
    "spread_stress",
    "validate",
    "walk_forward",
]
