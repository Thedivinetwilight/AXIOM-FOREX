"""The building blocks a catalog candidate is assembled from.

Three kinds of block, and nothing else:

``entry``   a *declarative* rule that produces BUY and SELL events from evidence
            available at that bar's close;
``exit``    what closes an open position: an ATR bracket, a trailing stop, a time
            limit, a moving-average return, or a close-only reaction to the
            opposite entry;
``filter``  whether a candidate may trade that bar at all: none, a higher
            timeframe trend, a volatility band, a session, or a trend-strength
            floor.

Each kind declares the parameter variants that exist.  A candidate is one entry
kind + one exit kind + one filter kind with one variant of each, which is what
makes the catalog reproducible: nothing is sampled at run time, and the grid is
fixed before any result is seen.

The rules are *states and crossings*, not prices: a candidate's text says which
series it compares, and the evaluation engine decides fills separately (see
:mod:`axiom_forex.strategies.catalog.simulate`).  No block here knows about money,
sizing, or orders.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timezone
from typing import Any, Callable, Mapping, Sequence

from .indicators import IndicatorCache, Number

#: Parameter values are numbers only, so a signature is stable and comparable.
Params = Mapping[str, float]

# -- helpers ---------------------------------------------------------------


def _p(params: Params, name: str) -> float:
    return float(params[name])


def _crossed_above(left: Sequence[Number], right: Sequence[Number], index: int) -> bool:
    if index <= 0:
        return False
    a, b = left[index], right[index]
    a_prev, b_prev = left[index - 1], right[index - 1]
    if a is None or b is None or a_prev is None or b_prev is None:
        return False
    return a > b and a_prev <= b_prev


def _crossed_below(left: Sequence[Number], right: Sequence[Number], index: int) -> bool:
    if index <= 0:
        return False
    a, b = left[index], right[index]
    a_prev, b_prev = left[index - 1], right[index - 1]
    if a is None or b is None or a_prev is None or b_prev is None:
        return False
    return a < b and a_prev >= b_prev


def _scaled(values: Sequence[Number], factor: float) -> list[Number]:
    return [None if value is None else value * factor for value in values]


def _shift(values: Sequence[Number], offset: int) -> list[Number]:
    """``out[i] = values[i - offset]`` -- the level that was known before bar i."""

    out: list[Number] = [None] * len(values)
    if offset <= 0:
        return list(values)
    for index in range(offset, len(values)):
        out[index] = values[index - offset]
    return out


def _band(
    middle: Sequence[Number], deviation: Sequence[Number], factor: float, sign: int
) -> list[Number]:
    return [
        None if (m is None or d is None) else m + sign * factor * d
        for m, d in zip(middle, deviation)
    ]


# -- higher timeframe ------------------------------------------------------


@dataclass
class HigherTimeframe:
    """A coarser view of the same bars, with only *completed* bars exposed.

    ``ratio`` base bars make one coarse bar.  At base bar ``i`` the last completed
    coarse bar is ``i // ratio - 1``; the coarse bar that is still forming is never
    used, so a filter can never see the future inside its own bucket.
    """

    ratio: int
    closes: tuple[float, ...]
    highs: tuple[float, ...]
    lows: tuple[float, ...]
    completed_index: tuple[int, ...]
    _cache: Any = field(default=None, repr=False, compare=False)

    @classmethod
    def build(cls, series: Any, ratio: int) -> "HigherTimeframe":
        ratio = max(2, int(ratio))
        closes = list(getattr(series, "mid"))
        highs = list(getattr(series, "mid_high"))
        lows = list(getattr(series, "mid_low"))
        count = len(closes)
        buckets = (count + ratio - 1) // ratio
        coarse_closes: list[float] = []
        coarse_highs: list[float] = []
        coarse_lows: list[float] = []
        for bucket in range(buckets):
            start = bucket * ratio
            end = min(count, start + ratio)
            if start >= end:
                break
            coarse_closes.append(closes[end - 1])
            coarse_highs.append(max(highs[start:end]))
            coarse_lows.append(min(lows[start:end]))
        completed = tuple(max(-1, index // ratio - 1) for index in range(count))
        return cls(
            ratio=ratio,
            closes=tuple(coarse_closes),
            highs=tuple(coarse_highs),
            lows=tuple(coarse_lows),
            completed_index=completed,
        )

    @property
    def bars(self) -> int:
        return len(self.closes)

    def cache(self) -> IndicatorCache:
        if self._cache is None:
            self._cache = IndicatorCache(
                closes=self.closes, highs=self.highs, lows=self.lows, bars=self.bars
            )
        return self._cache

    def aligned(self, values: Sequence[Number], base_bars: int) -> list[Number]:
        """Project a coarse indicator back onto base bars, causally."""

        out: list[Number] = [None] * base_bars
        limit = min(base_bars, len(self.completed_index))
        for index in range(limit):
            position = self.completed_index[index]
            if 0 <= position < len(values):
                out[index] = values[position]
        return out


# -- the rule blocks -------------------------------------------------------


@dataclass(frozen=True)
class Context:
    """What a rule may read: the base series, its indicators, and a coarse view."""

    series: Any
    cache: IndicatorCache
    coarse: HigherTimeframe | None = None


@dataclass(frozen=True)
class Kind:
    """One declared rule kind, with its fixed grid of parameter variants."""

    name: str
    label: str
    family_hint: str
    grid: tuple[Params, ...]
    describe: Callable[[Params], str]
    compute: Callable[[Context, Params], Any]
    needs_coarse: bool = False

    def variants(self) -> tuple[Params, ...]:
        return self.grid


def _empty(bars: int) -> list[bool]:
    return [False] * bars


# -- entries ---------------------------------------------------------------


def _entry_sma_cross(ctx: Context, params: Params) -> tuple[list[bool], list[bool]]:
    fast = ctx.cache.sma(int(_p(params, "fast")))
    slow = ctx.cache.sma(int(_p(params, "slow")))
    longs = [_crossed_above(fast, slow, index) for index in range(ctx.cache.bars)]
    shorts = [_crossed_below(fast, slow, index) for index in range(ctx.cache.bars)]
    return longs, shorts


def _entry_ema_cross(ctx: Context, params: Params) -> tuple[list[bool], list[bool]]:
    fast = ctx.cache.ema(int(_p(params, "fast")))
    slow = ctx.cache.ema(int(_p(params, "slow")))
    longs = [_crossed_above(fast, slow, index) for index in range(ctx.cache.bars)]
    shorts = [_crossed_below(fast, slow, index) for index in range(ctx.cache.bars)]
    return longs, shorts


def _entry_price_ma(ctx: Context, params: Params) -> tuple[list[bool], list[bool]]:
    period = int(_p(params, "period"))
    buffer = _p(params, "buffer_bps") / 10_000.0
    line = ctx.cache.sma(period)
    closes = list(ctx.cache.closes)  # once: copying per bar would be quadratic
    top = _scaled(line, 1.0 + buffer)
    bottom = _scaled(line, 1.0 - buffer)
    longs = [_crossed_above(closes, top, index) for index in range(ctx.cache.bars)]
    shorts = [_crossed_below(closes, bottom, index) for index in range(ctx.cache.bars)]
    return longs, shorts


def _entry_triple_ma(ctx: Context, params: Params) -> tuple[list[bool], list[bool]]:
    fast = ctx.cache.sma(int(_p(params, "fast")))
    mid = ctx.cache.sma(int(_p(params, "mid")))
    slow = ctx.cache.sma(int(_p(params, "slow")))
    longs: list[bool] = []
    shorts: list[bool] = []
    previous_up = False
    previous_down = False
    for index in range(ctx.cache.bars):
        a, b, c = fast[index], mid[index], slow[index]
        if a is None or b is None or c is None:
            longs.append(False)
            shorts.append(False)
            continue
        up = a > b > c
        down = a < b < c
        longs.append(up and not previous_up)
        shorts.append(down and not previous_down)
        previous_up, previous_down = up, down
    return longs, shorts


def _entry_macd_cross(ctx: Context, params: Params) -> tuple[list[bool], list[bool]]:
    histogram = ctx.cache.macd(
        int(_p(params, "fast")), int(_p(params, "slow")), int(_p(params, "signal"))
    )[2]
    zero = [0.0] * len(histogram)
    longs = [_crossed_above(histogram, zero, index) for index in range(len(histogram))]
    shorts = [_crossed_below(histogram, zero, index) for index in range(len(histogram))]
    return longs, shorts


def _entry_rsi_reversal(ctx: Context, params: Params) -> tuple[list[bool], list[bool]]:
    values = ctx.cache.rsi(int(_p(params, "period")))
    low = _p(params, "low")
    high = _p(params, "high")
    bars = ctx.cache.bars
    longs: list[bool] = []
    shorts: list[bool] = []
    for index in range(bars):
        previous = values[index - 1] if index else None
        current = values[index]
        if current is None or previous is None:
            longs.append(False)
            shorts.append(False)
            continue
        longs.append(previous <= low < current)
        shorts.append(previous >= high > current)
    return longs, shorts


def _entry_rsi_trend(ctx: Context, params: Params) -> tuple[list[bool], list[bool]]:
    values = ctx.cache.rsi(int(_p(params, "period")))
    level = _p(params, "level")
    top = [level] * len(values)
    bottom = [100.0 - level] * len(values)
    longs = [_crossed_above(values, top, index) for index in range(len(values))]
    shorts = [_crossed_below(values, bottom, index) for index in range(len(values))]
    return longs, shorts


def _entry_bollinger_revert(ctx: Context, params: Params) -> tuple[list[bool], list[bool]]:
    period = int(_p(params, "period"))
    middle = ctx.cache.sma(period)
    deviation = ctx.cache.stdev(period)
    factor = _p(params, "deviations")
    lower = _band(middle, deviation, factor, -1)
    upper = _band(middle, deviation, factor, +1)
    closes = list(ctx.cache.closes)
    bars = ctx.cache.bars
    return (
        [_crossed_below(closes, lower, index) for index in range(bars)],
        [_crossed_above(closes, upper, index) for index in range(bars)],
    )


def _entry_bollinger_break(ctx: Context, params: Params) -> tuple[list[bool], list[bool]]:
    period = int(_p(params, "period"))
    middle = ctx.cache.sma(period)
    deviation = ctx.cache.stdev(period)
    factor = _p(params, "deviations")
    upper = _band(middle, deviation, factor, +1)
    lower = _band(middle, deviation, factor, -1)
    closes = list(ctx.cache.closes)
    bars = ctx.cache.bars
    squeeze_bars = int(_p(params, "squeeze_bars")) if "squeeze_bars" in params else 0
    allowed = [True] * bars
    if squeeze_bars > 0:
        from .indicators import sma as _sma

        # A real squeeze gate: today's band width against its own longer mean.
        dense = [value if value is not None else 0.0 for value in deviation]
        baseline = _sma(dense, squeeze_bars)
        allowed = [
            bool(
                deviation[index] is not None
                and baseline[index] is not None
                and deviation[index] < baseline[index]
            )
            for index in range(bars)
        ]
    longs = [_crossed_above(closes, upper, index) and allowed[index] for index in range(bars)]
    shorts = [_crossed_below(closes, lower, index) and allowed[index] for index in range(bars)]
    return longs, shorts


def _entry_donchian_break(ctx: Context, params: Params) -> tuple[list[bool], list[bool]]:
    lookback = int(_p(params, "lookback"))
    top = _shift(ctx.cache.high(lookback), 1)
    bottom = _shift(ctx.cache.low(lookback), 1)
    closes = list(ctx.cache.closes)
    bars = ctx.cache.bars
    return (
        [_crossed_above(closes, top, index) for index in range(bars)],
        [_crossed_below(closes, bottom, index) for index in range(bars)],
    )


def _entry_atr_breakout(ctx: Context, params: Params) -> tuple[list[bool], list[bool]]:
    atr_period = int(_p(params, "atr_period"))
    lookback = int(_p(params, "lookback"))
    multiple = _p(params, "multiple")
    ranges = ctx.cache.atr(atr_period)
    top = _shift(ctx.cache.high(lookback), 1)
    bottom = _shift(ctx.cache.low(lookback), 1)
    upper = [
        None if (t is None or r is None) else t + multiple * r for t, r in zip(top, ranges)
    ]
    lower = [
        None if (b is None or r is None) else b - multiple * r for b, r in zip(bottom, ranges)
    ]
    closes = list(ctx.cache.closes)
    bars = ctx.cache.bars
    return (
        [_crossed_above(closes, upper, index) for index in range(bars)],
        [_crossed_below(closes, lower, index) for index in range(bars)],
    )


def _entry_keltner_break(ctx: Context, params: Params) -> tuple[list[bool], list[bool]]:
    period = int(_p(params, "period"))
    multiple = _p(params, "multiple")
    atr_period = int(params.get("atr_period", period))
    middle = ctx.cache.ema(period)
    ranges = ctx.cache.atr(atr_period)
    upper = _band(middle, ranges, multiple, +1)
    lower = _band(middle, ranges, multiple, -1)
    closes = list(ctx.cache.closes)
    bars = ctx.cache.bars
    return (
        [_crossed_above(closes, upper, index) for index in range(bars)],
        [_crossed_below(closes, lower, index) for index in range(bars)],
    )


def _entry_range_break(ctx: Context, params: Params) -> tuple[list[bool], list[bool]]:
    hour = int(_p(params, "hour"))
    count = int(_p(params, "bars"))
    buffer = _p(params, "buffer_bps") / 10_000.0
    stamps = list(getattr(ctx.series, "timestamps"))
    highs = list(ctx.cache.highs)
    lows = list(ctx.cache.lows)
    closes = list(ctx.cache.closes)
    bars = ctx.cache.bars
    longs = _empty(bars)
    shorts = _empty(bars)
    session_start = -1
    in_range = 0
    top = 0.0
    bottom = 0.0
    armed = False
    for index in range(bars):
        moment = stamps[index].astimezone(timezone.utc)
        if moment.hour != hour:
            session_start = -1
            in_range = 0
            armed = False
            continue
        if session_start < 0:
            session_start = index
            in_range = 0
            top = highs[index]
            bottom = lows[index]
            armed = False
            in_range = 1
            continue
        if in_range < count:
            top = max(top, highs[index])
            bottom = min(bottom, lows[index])
            in_range += 1
            if in_range >= count:
                armed = True
            continue
        if not armed:
            continue
        upper = top * (1.0 + buffer)
        lower = bottom * (1.0 - buffer)
        if closes[index] > upper:
            longs[index] = True
        elif closes[index] < lower:
            shorts[index] = True
    return longs, shorts


def _entry_momentum_roc(ctx: Context, params: Params) -> tuple[list[bool], list[bool]]:
    values = ctx.cache.roc(int(_p(params, "lookback")))
    threshold = _p(params, "threshold_bps")
    top = [threshold] * len(values)
    bottom = [-threshold] * len(values)
    return (
        [_crossed_above(values, top, index) for index in range(len(values))],
        [_crossed_below(values, bottom, index) for index in range(len(values))],
    )


def _entry_zscore_revert(ctx: Context, params: Params) -> tuple[list[bool], list[bool]]:
    values = ctx.cache.zscore(int(_p(params, "lookback")))
    entry = _p(params, "entry_z")
    lower = [-entry] * len(values)
    upper = [entry] * len(values)
    return (
        [_crossed_below(values, lower, index) for index in range(len(values))],
        [_crossed_above(values, upper, index) for index in range(len(values))],
    )


def _entry_stoch_cross(ctx: Context, params: Params) -> tuple[list[bool], list[bool]]:
    k, d = ctx.cache.stochastic(int(_p(params, "k")), int(_p(params, "d")))
    low = _p(params, "low")
    high = _p(params, "high")
    bars = ctx.cache.bars
    longs: list[bool] = []
    shorts: list[bool] = []
    for index in range(bars):
        current_k, current_d = k[index], d[index]
        previous_k = k[index - 1] if index else None
        previous_d = d[index - 1] if index else None
        if None in (current_k, current_d, previous_k, previous_d):
            longs.append(False)
            shorts.append(False)
            continue
        longs.append(previous_k <= previous_d and current_k > current_d and current_k < low)
        shorts.append(previous_k >= previous_d and current_k < current_d and current_k > high)
    return longs, shorts


# -- exits -----------------------------------------------------------------


@dataclass(frozen=True)
class PositionPolicy:
    """What the simulator applies while a position is open.

    ``stop_atr`` / ``target_atr`` / ``trail_atr`` are multiples of ATR; the levels
    are fixed when the position opens (except the trailing stop, which follows the
    best price reached).  ``max_hold`` is in bars.  ``close_on_opposite`` closes on
    an opposing entry event instead of flipping into it.
    """

    atr_window: int = 14
    stop_atr: float | None = None
    target_atr: float | None = None
    trail_atr: float | None = None
    max_hold: int | None = None
    exit_on_ma: int | None = None
    exit_on_rsi: int | None = None
    close_on_opposite: bool = False

    def describe(self) -> str:
        parts: list[str] = []
        if self.stop_atr:
            parts.append(f"stop at {self.stop_atr:g} x ATR({self.atr_window})")
        if self.target_atr:
            parts.append(f"target at {self.target_atr:g} x ATR({self.atr_window})")
        if self.trail_atr:
            parts.append(
                f"trailing stop {self.trail_atr:g} x ATR({self.atr_window}) behind the "
                "best price reached"
            )
        if self.max_hold:
            parts.append(f"time exit after {self.max_hold} bars")
        if self.exit_on_ma:
            parts.append(f"exit when price crosses back through the {self.exit_on_ma}-bar mean")
        if self.exit_on_rsi:
            parts.append(f"exit when RSI({self.exit_on_rsi}) crosses 50 against the position")
        if self.close_on_opposite:
            parts.append("close (without reversing) on the opposite entry event")
        if not parts:
            parts.append("no positional exit: the position turns only on the opposite entry")
        return "; ".join(parts)

    def audit_view(self) -> dict[str, Any]:
        return {
            "atr_window": self.atr_window,
            "stop_atr": self.stop_atr,
            "target_atr": self.target_atr,
            "trail_atr": self.trail_atr,
            "max_hold": self.max_hold,
            "exit_on_ma": self.exit_on_ma,
            "exit_on_rsi": self.exit_on_rsi,
            "close_on_opposite": self.close_on_opposite,
            "description": self.describe(),
        }


@dataclass(frozen=True)
class ExitKind:
    name: str
    label: str
    grid: tuple[Params, ...]
    describe: Callable[[Params], str]
    policy: Callable[[Params], PositionPolicy]
    signal_exit: Callable[[Context, Params], list[bool]] | None = None

    def variants(self) -> tuple[Params, ...]:
        return self.grid


def _exit_policy_bracket(params: Params) -> PositionPolicy:
    return PositionPolicy(
        atr_window=int(params.get("atr_period", 14)),
        stop_atr=_p(params, "stop_atr"),
        target_atr=_p(params, "target_atr"),
        max_hold=int(params.get("max_hold", 96)),
    )


def _exit_policy_trailing(params: Params) -> PositionPolicy:
    return PositionPolicy(
        atr_window=int(params.get("atr_period", 14)),
        trail_atr=_p(params, "trail_atr"),
        max_hold=int(params.get("max_hold", 240)),
    )


def _exit_policy_time(params: Params) -> PositionPolicy:
    return PositionPolicy(max_hold=int(_p(params, "max_hold")))


def _exit_policy_opposite(params: Params) -> PositionPolicy:
    return PositionPolicy(close_on_opposite=True)


def _exit_ma_signal(ctx: Context, params: Params) -> list[bool]:
    period = int(_p(params, "period"))
    line = ctx.cache.sma(period)
    closes = list(ctx.cache.closes)
    bars = ctx.cache.bars
    return [
        _crossed_below(closes, line, index) or _crossed_above(closes, line, index)
        for index in range(bars)
    ]


def _exit_policy_ma(params: Params) -> PositionPolicy:
    return PositionPolicy(exit_on_ma=int(_p(params, "period")))


def _exit_rsi_signal(ctx: Context, params: Params) -> list[bool]:
    values = ctx.cache.rsi(int(_p(params, "period")))
    mid = [50.0] * len(values)
    return [
        _crossed_above(values, mid, index) or _crossed_below(values, mid, index)
        for index in range(len(values))
    ]


def _exit_policy_rsi(params: Params) -> PositionPolicy:
    return PositionPolicy(exit_on_rsi=int(_p(params, "period")))


# -- filters ---------------------------------------------------------------


@dataclass(frozen=True)
class FilterKind:
    name: str
    label: str
    grid: tuple[Params, ...]
    describe: Callable[[Params], str]
    mask: Callable[[Context, Params], list[bool]]
    needs_coarse: bool = False

    def variants(self) -> tuple[Params, ...]:
        return self.grid


def _filter_none(ctx: Context, params: Params) -> list[bool]:
    return [True] * ctx.cache.bars


def _filter_htf_trend(ctx: Context, params: Params) -> list[bool]:
    coarse = ctx.coarse
    if coarse is None:  # pragma: no cover - a multi-timeframe candidate always has one
        return [False] * ctx.cache.bars
    cache = coarse.cache()
    fast = cache.sma(int(_p(params, "fast")))
    slow = cache.sma(int(_p(params, "slow")))
    direction: list[Number] = []
    for index in range(cache.bars):
        a, b = fast[index], slow[index]
        direction.append(None if (a is None or b is None) else (1.0 if a > b else -1.0))
    return [value == 1.0 or value == -1.0 for value in coarse.aligned(direction, ctx.cache.bars)]


def _filter_atr_band(ctx: Context, params: Params) -> list[bool]:
    window = int(_p(params, "window"))
    low = _p(params, "min_pct") / 100.0
    high = _p(params, "max_pct") / 100.0
    ranges = ctx.cache.atr(window)
    closes = list(ctx.cache.closes)
    out: list[bool] = []
    for index, value in enumerate(ranges):
        price = closes[index]
        if value is None or not price:
            out.append(False)
            continue
        fraction = value / price
        out.append(low <= fraction <= high)
    return out


def _filter_session(ctx: Context, params: Params) -> list[bool]:
    start = int(_p(params, "start_hour"))
    end = int(_p(params, "end_hour"))
    stamps = list(getattr(ctx.series, "timestamps"))
    out: list[bool] = []
    for stamp in stamps:
        hour = stamp.astimezone(timezone.utc).hour
        out.append(start <= hour < end if start <= end else (hour >= start or hour < end))
    return out


def _filter_adx(ctx: Context, params: Params) -> list[bool]:
    values = ctx.cache.adx(int(_p(params, "window")))
    threshold = _p(params, "threshold")
    return [bool(value is not None and value >= threshold) for value in values]


# -- the declared grids ----------------------------------------------------

ENTRIES: tuple[Kind, ...] = (
    Kind(
        name="sma_cross",
        label="Simple moving-average cross",
        family_hint="trend",
        grid=(
            {"fast": 8, "slow": 34},
            {"fast": 21, "slow": 89},
            {"fast": 34, "slow": 144},
        ),
        describe=lambda p: (
            f"Buy when the {int(p['fast'])}-bar simple mean of the mid price closes above "
            f"the {int(p['slow'])}-bar mean; sell the mirror."
        ),
        compute=_entry_sma_cross,
    ),
    Kind(
        name="ema_cross",
        label="Exponential moving-average cross",
        family_hint="trend",
        grid=(
            {"fast": 13, "slow": 55},
            {"fast": 34, "slow": 144},
            {"fast": 5, "slow": 21},
        ),
        describe=lambda p: (
            f"Buy when the {int(p['fast'])}-bar exponential mean closes above the "
            f"{int(p['slow'])}-bar exponential mean; sell the mirror."
        ),
        compute=_entry_ema_cross,
    ),
    Kind(
        name="price_ma",
        label="Price against one mean",
        family_hint="trend",
        grid=(
            {"period": 200, "buffer_bps": 0},
        ),
        describe=lambda p: (
            f"Buy when price closes {p['buffer_bps']:g} bp above the {int(p['period'])}-bar "
            f"simple mean; sell when it closes the same distance below."
        ),
        compute=_entry_price_ma,
    ),
    Kind(
        name="triple_ma",
        label="Stacked moving averages",
        family_hint="trend",
        grid=(
            {"fast": 8, "mid": 21, "slow": 55},
        ),
        describe=lambda p: (
            f"Buy when the {int(p['fast'])} > {int(p['mid'])} > {int(p['slow'])} simple "
            "means are stacked in that order for the first time; sell the inverted stack."
        ),
        compute=_entry_triple_ma,
    ),
    Kind(
        name="macd_cross",
        label="MACD histogram cross",
        family_hint="momentum",
        grid=(
            {"fast": 12, "slow": 26, "signal": 9},
            {"fast": 20, "slow": 50, "signal": 12},
        ),
        describe=lambda p: (
            f"Buy when the MACD histogram ({int(p['fast'])},{int(p['slow'])},"
            f"{int(p['signal'])}) crosses above zero; sell when it crosses below."
        ),
        compute=_entry_macd_cross,
    ),
    Kind(
        name="momentum_roc",
        label="Rate-of-change momentum",
        family_hint="momentum",
        grid=(
            {"lookback": 12, "threshold_bps": 10},
            {"lookback": 48, "threshold_bps": 15},
        ),
        describe=lambda p: (
            f"Buy when the {int(p['lookback'])}-bar rate of change rises through "
            f"{p['threshold_bps']:g} bp; sell when it falls through the negative of it."
        ),
        compute=_entry_momentum_roc,
    ),
    Kind(
        name="rsi_reversal",
        label="RSI exhaustion reversal",
        family_hint="mean_reversion",
        grid=(
            {"period": 14, "low": 30, "high": 70},
            {"period": 14, "low": 20, "high": 80},
            {"period": 7, "low": 30, "high": 70},
        ),
        describe=lambda p: (
            f"Buy when RSI({int(p['period'])}) climbs back through {p['low']:g} from "
            f"below; sell when it drops back through {p['high']:g} from above."
        ),
        compute=_entry_rsi_reversal,
    ),
    Kind(
        name="rsi_trend",
        label="RSI trend confirmation",
        family_hint="momentum",
        grid=(
            {"period": 14, "level": 55},
        ),
        describe=lambda p: (
            f"Buy when RSI({int(p['period'])}) rises through {p['level']:g}; sell when it "
            f"falls through {100 - p['level']:g}."
        ),
        compute=_entry_rsi_trend,
    ),
    Kind(
        name="bollinger_revert",
        label="Bollinger band reversion",
        family_hint="mean_reversion",
        grid=(
            {"period": 20, "deviations": 2.0},
            {"period": 20, "deviations": 2.5},
            {"period": 50, "deviations": 2.0},
        ),
        describe=lambda p: (
            f"Buy when price closes below the lower {int(p['period'])}-bar Bollinger band "
            f"({p['deviations']:g} sigma); sell when it closes above the upper band."
        ),
        compute=_entry_bollinger_revert,
    ),
    Kind(
        name="bollinger_break",
        label="Bollinger band breakout",
        family_hint="volatility",
        grid=(
            {"period": 20, "deviations": 2.0, "squeeze_bars": 100},
        ),
        describe=lambda p: (
            f"After the {int(p['period'])}-bar band width has been narrower than its own "
            f"{int(p['squeeze_bars'])}-bar mean, buy a close above the upper "
            f"{p['deviations']:g}-sigma band and sell a close below the lower one."
        ),
        compute=_entry_bollinger_break,
    ),
    Kind(
        name="donchian_break",
        label="Donchian channel breakout",
        family_hint="breakout",
        grid=(
            {"lookback": 20},
            {"lookback": 40},
            {"lookback": 55},
        ),
        describe=lambda p: (
            f"Buy when price closes above the highest high of the previous "
            f"{int(p['lookback'])} bars; sell when it closes below the lowest low."
        ),
        compute=_entry_donchian_break,
    ),
    Kind(
        name="atr_breakout",
        label="ATR-scaled channel breakout",
        family_hint="volatility",
        grid=(
            {"atr_period": 14, "lookback": 20, "multiple": 1.0},
            {"atr_period": 14, "lookback": 40, "multiple": 2.0},
        ),
        describe=lambda p: (
            f"Buy when price closes above the previous {int(p['lookback'])}-bar high plus "
            f"{p['multiple']:g} x ATR({int(p['atr_period'])}); sell below the mirror."
        ),
        compute=_entry_atr_breakout,
    ),
    Kind(
        name="keltner_break",
        label="Keltner channel breakout",
        family_hint="volatility",
        grid=(
            {"period": 20, "multiple": 2.0},
        ),
        describe=lambda p: (
            f"Buy a close above the {int(p['period'])}-bar exponential mean plus "
            f"{p['multiple']:g} x ATR({int(p['period'])}); sell below the mirror."
        ),
        compute=_entry_keltner_break,
    ),
    Kind(
        name="range_break",
        label="Session range breakout",
        family_hint="breakout",
        grid=(
            {"hour": 7, "bars": 4, "buffer_bps": 0},
        ),
        describe=lambda p: (
            f"From {int(p['hour']):02d}:00 UTC, take the first {int(p['bars'])} bars as the "
            f"session range, then buy a close above it by {p['buffer_bps']:g} bp and sell "
            "a close below it by the same distance. The range resets each day."
        ),
        compute=_entry_range_break,
    ),
    Kind(
        name="zscore_revert",
        label="Z-score reversion",
        family_hint="mean_reversion",
        grid=(
            {"lookback": 20, "entry_z": 2.0},
        ),
        describe=lambda p: (
            f"Buy when the {int(p['lookback'])}-bar z-score of price falls through "
            f"-{p['entry_z']:g}; sell when it rises through +{p['entry_z']:g}."
        ),
        compute=_entry_zscore_revert,
    ),
    Kind(
        name="stoch_cross",
        label="Stochastic cross",
        family_hint="momentum",
        grid=(
            {"k": 14, "d": 5, "low": 25, "high": 75},
        ),
        describe=lambda p: (
            f"Buy when %K({int(p['k'])}) crosses above %D({int(p['d'])}) while below "
            f"{p['low']:g}; sell when it crosses below %D while above {p['high']:g}."
        ),
        compute=_entry_stoch_cross,
    ),
)


EXITS: tuple[ExitKind, ...] = (
    ExitKind(
        name="bracket",
        label="ATR bracket",
        grid=(
            {"stop_atr": 1.5, "target_atr": 3.0, "max_hold": 96},
            {"stop_atr": 2.0, "target_atr": 4.0, "max_hold": 240},
            {"stop_atr": 1.0, "target_atr": 2.5, "max_hold": 48},
        ),
        describe=lambda p: (
            f"stop {p['stop_atr']:g} x ATR and target {p['target_atr']:g} x ATR from the "
            f"fill, with a {int(p['max_hold'])}-bar time limit"
        ),
        policy=_exit_policy_bracket,
    ),
    ExitKind(
        name="trailing_atr",
        label="ATR trailing stop",
        grid=(
            {"trail_atr": 2.0, "max_hold": 240},
        ),
        describe=lambda p: (
            f"trailing stop {p['trail_atr']:g} x ATR behind the best price reached, with a "
            f"{int(p['max_hold'])}-bar time limit"
        ),
        policy=_exit_policy_trailing,
    ),
    ExitKind(
        name="time_exit",
        label="Time limit",
        grid=(
            {"max_hold": 32},
            {"max_hold": 96},
        ),
        describe=lambda p: f"a plain {int(p['max_hold'])}-bar time limit",
        policy=_exit_policy_time,
    ),
    ExitKind(
        name="ma_return",
        label="Return to the mean",
        grid=({"period": 20},),
        describe=lambda p: (
            f"exit when price crosses back through the {int(p['period'])}-bar simple mean"
        ),
        policy=_exit_policy_ma,
        signal_exit=_exit_ma_signal,
    ),
    ExitKind(
        name="rsi_mid",
        label="RSI midline",
        grid=({"period": 14},),
        describe=lambda p: f"exit when RSI({int(p['period'])}) crosses 50 against the trade",
        policy=_exit_policy_rsi,
        signal_exit=_exit_rsi_signal,
    ),
    ExitKind(
        name="opposite",
        label="Opposite entry",
        grid=({},),
        describe=lambda p: (
            "close on the opposite entry event without opening the reverse position"
        ),
        policy=_exit_policy_opposite,
    ),
)


FILTERS: tuple[FilterKind, ...] = (
    FilterKind(
        name="none",
        label="No filter",
        grid=({},),
        describe=lambda p: "every bar may trade",
        mask=_filter_none,
    ),
    FilterKind(
        name="atr_band",
        label="Volatility band",
        grid=(
            {"window": 14, "min_pct": 0.03, "max_pct": 0.20},
        ),
        describe=lambda p: (
            f"trade only while ATR({int(p['window'])}) is between {p['min_pct']:g}% and "
            f"{p['max_pct']:g}% of price"
        ),
        mask=_filter_atr_band,
    ),
    FilterKind(
        name="session",
        label="Session window",
        grid=(
            {"start_hour": 7, "end_hour": 17},
        ),
        describe=lambda p: (
            f"trade only between {int(p['start_hour']):02d}:00 and "
            f"{int(p['end_hour']):02d}:00 UTC"
        ),
        mask=_filter_session,
    ),
    FilterKind(
        name="adx",
        label="Trend strength floor",
        grid=(
            {"window": 14, "threshold": 22},
        ),
        describe=lambda p: (
            f"trade only while ADX({int(p['window'])}) is at least {p['threshold']:g}"
        ),
        mask=_filter_adx,
    ),
    FilterKind(
        name="htf_trend",
        label="Higher-timeframe trend",
        grid=(
            {"ratio": 4, "fast": 20, "slow": 50},
            {"ratio": 12, "fast": 50, "slow": 200},
        ),
        describe=lambda p: (
            f"trade only in the direction of the {int(p['fast'])}/{int(p['slow'])} mean "
            f"cross on a timeframe {int(p['ratio'])}x coarser, using completed coarse bars only"
        ),
        mask=_filter_htf_trend,
        needs_coarse=True,
    ),
)


_TABLES: dict[str, dict[str, Any]] = {
    "entry": {item.name: item for item in ENTRIES},
    "exit": {item.name: item for item in EXITS},
    "filter": {item.name: item for item in FILTERS},
}


def entry_kind(name: str) -> Kind:
    return _TABLES["entry"][name]


def exit_kind(name: str) -> ExitKind:
    return _TABLES["exit"][name]


def filter_kind(name: str) -> FilterKind:
    return _TABLES["filter"][name]


def parameter_signature(params: Params) -> str:
    """A canonical, order-independent parameter string."""

    parts = []
    for name in sorted(params):
        value = params[name]
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        parts.append(f"{name}={value}")
    return ",".join(parts)


def direction_mask(kind: FilterKind, ctx: Context, params: Params) -> tuple[list[bool], list[bool]]:
    """Allowed directions per bar: ``(longs_allowed, shorts_allowed)``.

    Most filters are direction-neutral.  The coarse-trend filter is not: it gates
    longs to an up-trending coarse timeframe and shorts to a down-trending one.
    """

    if kind.name != "htf_trend":
        allowed = kind.mask(ctx, params)
        return allowed, allowed
    coarse = ctx.coarse
    if coarse is None:  # pragma: no cover - guarded at generation
        bars = ctx.cache.bars
        return [False] * bars, [False] * bars
    cache = coarse.cache()
    fast = cache.sma(int(_p(params, "fast")))
    slow = cache.sma(int(_p(params, "slow")))
    sign: list[Number] = []
    for index in range(cache.bars):
        a, b = fast[index], slow[index]
        sign.append(None if (a is None or b is None) else (1.0 if a > b else -1.0))
    projected = coarse.aligned(sign, ctx.cache.bars)
    return (
        [value == 1.0 for value in projected],
        [value == -1.0 for value in projected],
    )


__all__ = [
    "Context",
    "ENTRIES",
    "EXITS",
    "ExitKind",
    "FILTERS",
    "FilterKind",
    "HigherTimeframe",
    "Kind",
    "Params",
    "PositionPolicy",
    "direction_mask",
    "entry_kind",
    "exit_kind",
    "filter_kind",
    "parameter_signature",
]
