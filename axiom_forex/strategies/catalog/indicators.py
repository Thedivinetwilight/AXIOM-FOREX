"""Array indicators over one research series, computed once and cached.

Every function here is **causal**: the value at index ``i`` uses only bars ``<= i``.
Warm-up positions are ``None`` rather than a made-up number, so a rule that needs
500 bars cannot fire on bar 3 because a helper filled in zeros.

Prices are the mid of the two-sided research bar (bid + half its quoted spread);
the series itself is built from the broker's bid plus the spread that bar quoted.

These are plain Python lists, deliberately: the catalog evaluates thousands of
candidates on a laptop, and a compiled dependency is not needed for a dozen
rolling sums.  Each indicator is memoised per (name, params) inside
:class:`IndicatorCache`, so candidates that share a period share the work.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Sequence

Number = float | None
Series = Callable[[], Sequence[float]]


def _roll_sum(values: Sequence[float], window: int) -> list[Number]:
    window = max(1, int(window))
    out: list[Number] = [None] * len(values)
    total = 0.0
    for index, value in enumerate(values):
        total += value
        if index >= window:
            total -= values[index - window]
        if index + 1 >= window:
            out[index] = total
    return out


def sma(values: Sequence[float], window: int) -> list[Number]:
    window = max(1, int(window))
    sums = _roll_sum(values, window)
    return [None if value is None else value / window for value in sums]


def ema(values: Sequence[float], window: int) -> list[Number]:
    """Exponential mean, seeded with the first full simple mean."""

    window = max(1, int(window))
    alpha = 2.0 / (window + 1.0)
    out: list[Number] = [None] * len(values)
    if len(values) < window:
        return out
    seed = sum(values[:window]) / window
    out[window - 1] = seed
    previous = seed
    for index in range(window, len(values)):
        previous = alpha * values[index] + (1.0 - alpha) * previous
        out[index] = previous
    return out


def rolling_max(values: Sequence[float], window: int) -> list[Number]:
    window = max(1, int(window))
    out: list[Number] = [None] * len(values)
    for index in range(window - 1, len(values)):
        out[index] = max(values[index - window + 1 : index + 1])
    return out


def rolling_min(values: Sequence[float], window: int) -> list[Number]:
    window = max(1, int(window))
    out: list[Number] = [None] * len(values)
    for index in range(window - 1, len(values)):
        out[index] = min(values[index - window + 1 : index + 1])
    return out


def stdev(values: Sequence[float], window: int) -> list[Number]:
    window = max(1, int(window))
    sums = _roll_sum(values, window)
    squares = _roll_sum([value * value for value in values], window)
    out: list[Number] = [None] * len(values)
    for index, total in enumerate(sums):
        if total is None or squares[index] is None:
            continue
        mean = total / window
        variance = max(0.0, squares[index] / window - mean * mean)
        out[index] = math.sqrt(variance)
    return out


def rsi(values: Sequence[float], window: int) -> list[Number]:
    """Wilder's RSI, seeded from the first ``window`` changes."""

    window = max(1, int(window))
    out: list[Number] = [None] * len(values)
    if len(values) <= window:
        return out
    gains = 0.0
    losses = 0.0
    for index in range(1, window + 1):
        change = values[index] - values[index - 1]
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    average_gain = gains / window
    average_loss = losses / window
    out[window] = _rsi_from(average_gain, average_loss)
    for index in range(window + 1, len(values)):
        change = values[index] - values[index - 1]
        average_gain = (average_gain * (window - 1) + max(change, 0.0)) / window
        average_loss = (average_loss * (window - 1) + max(-change, 0.0)) / window
        out[index] = _rsi_from(average_gain, average_loss)
    return out


def _rsi_from(gain: float, loss: float) -> float:
    if loss == 0.0:
        return 100.0 if gain > 0.0 else 50.0
    relative = gain / loss
    return 100.0 - (100.0 / (1.0 + relative))


def true_range(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]) -> list[float]:
    out: list[float] = [0.0] * len(closes)
    for index in range(1, len(closes)):
        previous = closes[index - 1]
        out[index] = max(
            highs[index] - lows[index],
            abs(highs[index] - previous),
            abs(lows[index] - previous),
        )
    return out


def atr(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], window: int
) -> list[Number]:
    """Wilder's average true range."""

    window = max(1, int(window))
    ranges = true_range(highs, lows, closes)
    out: list[Number] = [None] * len(closes)
    if len(closes) <= window:
        return out
    value = sum(ranges[1 : window + 1]) / window
    out[window] = value
    for index in range(window + 1, len(closes)):
        value = (value * (window - 1) + ranges[index]) / window
        out[index] = value
    return out


def roc(values: Sequence[float], window: int) -> list[Number]:
    window = max(1, int(window))
    out: list[Number] = [None] * len(values)
    for index in range(window, len(values)):
        base = values[index - window]
        if base:
            out[index] = (values[index] / base - 1.0) * 10_000.0
    return out


def zscore(values: Sequence[float], window: int) -> list[Number]:
    window = max(1, int(window))
    means = sma(values, window)
    deviations = stdev(values, window)
    out: list[Number] = [None] * len(values)
    for index, mean in enumerate(means):
        deviation = deviations[index]
        if mean is None or deviation is None or deviation == 0.0:
            continue
        out[index] = (values[index] - mean) / deviation
    return out


def macd(
    values: Sequence[float], fast: int, slow: int, signal: int
) -> tuple[list[Number], list[Number], list[Number]]:
    fast_line = ema(values, fast)
    slow_line = ema(values, slow)
    line: list[Number] = [
        None if (a is None or b is None) else a - b for a, b in zip(fast_line, slow_line)
    ]
    dense = [value for value in line if value is not None]
    signal_line: list[Number] = [None] * len(line)
    if dense:
        computed = ema(dense, signal)
        cursor = 0
        for index, value in enumerate(line):
            if value is None:
                continue
            signal_line[index] = computed[cursor]
            cursor += 1
    histogram: list[Number] = [
        None if (a is None or b is None) else a - b for a, b in zip(line, signal_line)
    ]
    return line, signal_line, histogram


def stochastic(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], k: int, d: int
) -> tuple[list[Number], list[Number]]:
    k = max(1, int(k))
    top = rolling_max(highs, k)
    bottom = rolling_min(lows, k)
    percent: list[Number] = [None] * len(closes)
    for index, high in enumerate(top):
        low = bottom[index]
        if high is None or low is None or high == low:
            continue
        percent[index] = (closes[index] - low) / (high - low) * 100.0
    dense = [value for value in percent if value is not None]
    average: list[Number] = [None] * len(percent)
    if dense:
        computed = sma(dense, d)
        cursor = 0
        for index, value in enumerate(percent):
            if value is None:
                continue
            average[index] = computed[cursor]
            cursor += 1
    return percent, average


def adx(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], window: int
) -> list[Number]:
    """A compact ADX: smoothed directional movement over smoothed true range."""

    window = max(1, int(window))
    out: list[Number] = [None] * len(closes)
    if len(closes) <= window + 1:
        return out
    plus = [0.0] * len(closes)
    minus = [0.0] * len(closes)
    for index in range(1, len(closes)):
        up = highs[index] - highs[index - 1]
        down = lows[index - 1] - lows[index]
        plus[index] = up if (up > down and up > 0) else 0.0
        minus[index] = down if (down > up and down > 0) else 0.0
    ranges = true_range(highs, lows, closes)
    smoothed_plus = sum(plus[1 : window + 1])
    smoothed_minus = sum(minus[1 : window + 1])
    smoothed_range = sum(ranges[1 : window + 1])
    dx_values: list[float] = []
    for index in range(window + 1, len(closes)):
        smoothed_plus = smoothed_plus - smoothed_plus / window + plus[index]
        smoothed_minus = smoothed_minus - smoothed_minus / window + minus[index]
        smoothed_range = smoothed_range - smoothed_range / window + ranges[index]
        if smoothed_range <= 0:
            continue
        di_plus = 100.0 * smoothed_plus / smoothed_range
        di_minus = 100.0 * smoothed_minus / smoothed_range
        total = di_plus + di_minus
        if total <= 0:
            continue
        dx_values.append(100.0 * abs(di_plus - di_minus) / total)
        if len(dx_values) >= window:
            out[index] = sum(dx_values[-window:]) / window
    return out


@dataclass
class IndicatorCache:
    """Memoised indicators bound to one series."""

    closes: Sequence[float]
    highs: Sequence[float]
    lows: Sequence[float]
    bars: int
    _cache: dict[tuple, object] = field(default_factory=dict, repr=False)

    @classmethod
    def of(cls, series: object) -> "IndicatorCache":
        """Build from a research series, using its mid prices."""

        return cls(
            closes=list(getattr(series, "mid")),
            highs=list(getattr(series, "mid_high")),
            lows=list(getattr(series, "mid_low")),
            bars=len(getattr(series, "mid")),
        )

    def get(self, name: str, **params: int) -> object:
        key = (name, tuple(sorted(params.items())))
        if key in self._cache:
            return self._cache[key]
        value = _COMPUTE[name](self, **params)
        self._cache[key] = value
        return value

    # -- individual indicators -------------------------------------------

    def sma(self, window: int) -> list[Number]:
        return self.get("sma", window=window)  # type: ignore[return-value]

    def ema(self, window: int) -> list[Number]:
        return self.get("ema", window=window)  # type: ignore[return-value]

    def stdev(self, window: int) -> list[Number]:
        return self.get("stdev", window=window)  # type: ignore[return-value]

    def rsi(self, window: int) -> list[Number]:
        return self.get("rsi", window=window)  # type: ignore[return-value]

    def atr(self, window: int) -> list[Number]:
        return self.get("atr", window=window)  # type: ignore[return-value]

    def roc(self, window: int) -> list[Number]:
        return self.get("roc", window=window)  # type: ignore[return-value]

    def zscore(self, window: int) -> list[Number]:
        return self.get("zscore", window=window)  # type: ignore[return-value]

    def high(self, window: int) -> list[Number]:
        return self.get("high", window=window)  # type: ignore[return-value]

    def low(self, window: int) -> list[Number]:
        return self.get("low", window=window)  # type: ignore[return-value]

    def adx(self, window: int) -> list[Number]:
        return self.get("adx", window=window)  # type: ignore[return-value]

    def macd(self, fast: int, slow: int, signal: int):
        return self.get("macd", fast=fast, slow=slow, signal=signal)

    def stochastic(self, k: int, d: int):
        return self.get("stochastic", k=k, d=d)


def _compute_sma(cache: IndicatorCache, window: int) -> list[Number]:
    return sma(cache.closes, window)


def _compute_ema(cache: IndicatorCache, window: int) -> list[Number]:
    return ema(cache.closes, window)


def _compute_stdev(cache: IndicatorCache, window: int) -> list[Number]:
    return stdev(cache.closes, window)


def _compute_rsi(cache: IndicatorCache, window: int) -> list[Number]:
    return rsi(cache.closes, window)


def _compute_atr(cache: IndicatorCache, window: int) -> list[Number]:
    return atr(cache.highs, cache.lows, cache.closes, window)


def _compute_roc(cache: IndicatorCache, window: int) -> list[Number]:
    return roc(cache.closes, window)


def _compute_zscore(cache: IndicatorCache, window: int) -> list[Number]:
    return zscore(cache.closes, window)


def _compute_high(cache: IndicatorCache, window: int) -> list[Number]:
    return rolling_max(cache.highs, window)


def _compute_low(cache: IndicatorCache, window: int) -> list[Number]:
    return rolling_min(cache.lows, window)


def _compute_adx(cache: IndicatorCache, window: int) -> list[Number]:
    return adx(cache.highs, cache.lows, cache.closes, window)


def _compute_macd(cache: IndicatorCache, fast: int, slow: int, signal: int):
    return macd(cache.closes, fast, slow, signal)


def _compute_stochastic(cache: IndicatorCache, k: int, d: int):
    return stochastic(cache.highs, cache.lows, cache.closes, k, d)


_COMPUTE: dict[str, Callable[..., object]] = {
    "sma": _compute_sma,
    "ema": _compute_ema,
    "stdev": _compute_stdev,
    "rsi": _compute_rsi,
    "atr": _compute_atr,
    "roc": _compute_roc,
    "zscore": _compute_zscore,
    "high": _compute_high,
    "low": _compute_low,
    "adx": _compute_adx,
    "macd": _compute_macd,
    "stochastic": _compute_stochastic,
}


__all__ = [
    "IndicatorCache",
    "Number",
    "adx",
    "atr",
    "ema",
    "macd",
    "roc",
    "rolling_max",
    "rolling_min",
    "rsi",
    "sma",
    "stdev",
    "stochastic",
    "true_range",
    "zscore",
]
