"""Multiple-testing control for a search over many parameter variants.

Three separate things are computed, and each answers a different question:

**A per-variant bootstrap p-value** (:func:`block_bootstrap_p_value`)
    How often would a series with no edge have produced a mean at least this good?
    Returns are resampled in blocks, because consecutive bar returns are not
    independent; an i.i.d. bootstrap would understate the noise.

**Benjamini–Hochberg** (:func:`benjamini_hochberg`)
    With ``m`` variants tried, the smallest p-value is not the one to read. BH
    controls the expected *proportion* of false discoveries among the variants
    that pass, at a declared rate ``q``.

**White's Reality Check** (:func:`reality_check`)
    The strongest single statement the search supports: is the *best* variant's
    edge distinguishable from luck once every variant tried is accounted for? The
    null is "no variant has an edge", and the statistic is the maximum mean.

Every routine here is deterministic given a seed, uses only the standard library,
and reports the number of resamples so a reader can judge how coarse the p-value
is.  None of them is a guarantee: with a short sample no test can rescue a
search, which is why the screen also requires a locked out-of-sample window that
was never used for this.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

DEFAULT_RESAMPLES = 1000
DEFAULT_BLOCK = 5


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _block_indices(length: int, block: int, rng: random.Random) -> list[int]:
    """Circular block bootstrap: keeps short-range dependence inside blocks."""

    size = max(1, min(int(block), length))
    indices: list[int] = []
    while len(indices) < length:
        start = rng.randrange(length)
        indices.extend((start + offset) % length for offset in range(size))
    return indices[:length]


def block_bootstrap_p_value(
    returns: Sequence[float],
    *,
    block: int = DEFAULT_BLOCK,
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = 20261031,
) -> dict[str, Any]:
    """One-sided p-value for ``mean(returns) > 0``.

    ``returns`` is one observation per bar (or per trade), in chronological order.
    """

    values = [float(value) for value in returns]
    if len(values) < 3:
        return {"p_value": None, "resamples": 0, "note": "too few observations to test"}
    observed = _mean(values)
    rng = random.Random(seed)
    hits = 0
    for _ in range(int(resamples)):
        sample = [values[index] for index in _block_indices(len(values), block, rng)]
        if _mean(sample) <= 0:
            hits += 1
    p_value = (hits + 1) / (int(resamples) + 1)
    return {
        "p_value": round(p_value, 6),
        "observed_mean": observed,
        "resamples": int(resamples),
        "block": int(block),
        "note": (
            "One-sided block-bootstrap p-value for a positive mean. It answers "
            "'how often would noise look this good', not 'will this make money'."
        ),
    }


def benjamini_hochberg(
    p_values: Mapping[str, float], *, q: float = 0.10
) -> dict[str, Any]:
    """BH step-up. Returns q-values and which variants pass at ``q``."""

    items = [(key, float(value)) for key, value in p_values.items() if value is not None]
    total = len(items)
    ordered = sorted(items, key=lambda item: item[1])
    q_values: dict[str, float] = {}
    rejected: set[str] = set()
    threshold_rank = 0
    for rank, (key, p_value) in enumerate(ordered, start=1):
        adjusted = p_value * total / rank
        q_values[key] = round(min(1.0, adjusted), 6)
        if p_value <= q * rank / total:
            threshold_rank = rank
    for key, _value in ordered[:threshold_rank]:
        rejected.add(key)
    return {
        "q": float(q),
        "tested": total,
        "q_values": q_values,
        "accepted": sorted(rejected),
        "note": (
            f"Benjamini–Hochberg at q={q} over {total} attempted variant(s). An "
            "accepted variant is one whose p-value survives the number of attempts, "
            "not one that is proven."
        ),
    }


def reality_check(
    series: Mapping[str, Sequence[float]],
    *,
    block: int = DEFAULT_BLOCK,
    resamples: int = DEFAULT_RESAMPLES,
    seed: int = 20261031,
) -> dict[str, Any]:
    """White's Reality Check over every variant's return series.

    The same resampled time indices are applied to every variant, which preserves
    the correlation between them -- the variants share bars, so treating them as
    independent would understate the noise in the maximum.
    """

    ranked = {key: [float(value) for value in values] for key, values in series.items() if values}
    if len(ranked) < 1:
        return {"p_value": None, "best": "", "note": "no series to test"}
    length = min(len(values) for values in ranked.values())
    if length < 3:
        return {"p_value": None, "best": "", "note": "too few observations to test"}
    trimmed = {key: values[:length] for key, values in ranked.items()}
    means = {key: _mean(values) for key, values in trimmed.items()}
    best_key = max(means, key=lambda key: means[key])
    observed = means[best_key]
    centred = {
        key: [value - means[key] for value in values] for key, values in trimmed.items()
    }
    rng = random.Random(seed)
    total = int(resamples)
    exceed = 0
    for _ in range(total):
        indices = _block_indices(length, block, rng)
        boot = max(_mean([centred[key][index] for index in indices]) for key in trimmed)
        if boot >= observed:
            exceed += 1
    p_value = round((exceed + 1) / (total + 1), 6)
    return {
        "p_value": p_value,
        "best": best_key,
        "best_mean": round(observed, 10),
        "variants": len(trimmed),
        "observations": length,
        "resamples": int(resamples),
        "note": (
            "White's Reality Check: the probability that the best of these variants "
            "looks this good under 'no variant has an edge'. A p-value above the "
            "declared level means the search produced nothing distinguishable from luck."
        ),
    }


def deflated_sharpe_note() -> str:
    """The honest one-liner for the page, so nobody reads a p-value as a promise."""

    return (
        "Statistical significance is not profitability: these tests assume the "
        "returns series is the whole story, and they say nothing about spread, "
        "slippage, swap, or how the edge decays."
    )


__all__ = [
    "DEFAULT_BLOCK",
    "DEFAULT_RESAMPLES",
    "benjamini_hochberg",
    "block_bootstrap_p_value",
    "deflated_sharpe_note",
    "reality_check",
]
