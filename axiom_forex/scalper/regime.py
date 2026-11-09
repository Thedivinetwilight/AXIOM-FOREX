"""AX-REGIME: what kind of market is this feed showing right now?

The classifier reads a window of ticks and answers with a label, a set of
strategy families that may run in that label, and — very often — the answer that
nothing may run.  ``NO TRADE`` is a first-class result here, not an error path:
most of the time a scalper's honest output is "not now".

All thresholds are declared in :class:`RegimeThresholds`; none is fitted.  The
labels are descriptive of *this feed*, not of the market: one broker's quote
stream is one broker's quote stream.

The features are deliberately simple and cheap — a scalping loop cannot spend a
millisecond computing a spectral estimate — and every one of them is reported
alongside the label so a reader can disagree with the classification.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Sequence

from .config import InstrumentSpec
from .ticks import Tick

#: Labels.
TRENDING_UP = "TRENDING_UP"
TRENDING_DOWN = "TRENDING_DOWN"
RANGING = "RANGING"
BREAKOUT = "BREAKOUT"
HIGH_VOL = "HIGH_VOL"
LOW_VOL = "LOW_VOL"
LOW_LIQUIDITY = "LOW_LIQUIDITY"
SPREAD_EXPANSION = "SPREAD_EXPANSION"
ABNORMAL = "ABNORMAL"

#: Strategy families (the signal module's own names).
FAMILY_MOMENTUM = "momentum"
FAMILY_BREAKOUT = "breakout"
FAMILY_PULLBACK = "pullback"
FAMILY_MEAN_REVERSION = "mean_reversion"
FAMILY_ORDER_FLOW = "order_flow"

ALL_FAMILIES = (
    FAMILY_MOMENTUM,
    FAMILY_BREAKOUT,
    FAMILY_PULLBACK,
    FAMILY_MEAN_REVERSION,
    FAMILY_ORDER_FLOW,
)

NO_TRADE_REASONS = {
    LOW_LIQUIDITY: "too_few_quotes_to_read_a_regime",
    SPREAD_EXPANSION: "spread_wider_than_this_strategy_can_pay_for",
    ABNORMAL: "market_behaviour_outside_declared_bounds",
}


@dataclass(frozen=True)
class RegimeThresholds:
    """Every bound the classifier uses, written down before it is run."""

    #: Net move over the window, in points, before a trend is claimed.
    trend_points: float = 12.0
    #: Directional efficiency (net move ÷ path length) before a trend is claimed.
    trend_efficiency: float = 0.35
    #: Efficiency below which the window is called ranging.
    range_efficiency: float = 0.22
    #: Window high/low must be exceeded by this many points to call a breakout.
    breakout_buffer_points: float = 3.0
    #: Realised per-tick volatility, in points, above which volatility is high.
    high_vol_points: float = 4.0
    #: Realised per-tick volatility, in points, below which volatility is low.
    low_vol_points: float = 1.2
    #: A single tick moving this many times the typical move is an abnormal print.
    abnormal_tick_multiple: float = 12.0
    #: Fewer quotes per second than this is not a market to scalp.
    min_ticks_per_second: float = 0.5
    #: Fewer distinct prices than this fraction of the window means a stuck feed.
    min_distinct_price_ratio: float = 0.08
    #: Spread above this multiple of the window median is an expansion.
    spread_expansion_multiple: float = 2.0
    #: Minimum window before any label other than ABNORMAL is issued.
    min_ticks: int = 30

    def audit_view(self) -> dict[str, Any]:
        return {
            "trend_points": self.trend_points,
            "trend_efficiency": self.trend_efficiency,
            "range_efficiency": self.range_efficiency,
            "breakout_buffer_points": self.breakout_buffer_points,
            "high_vol_points": self.high_vol_points,
            "low_vol_points": self.low_vol_points,
            "abnormal_tick_multiple": self.abnormal_tick_multiple,
            "min_ticks_per_second": self.min_ticks_per_second,
            "min_distinct_price_ratio": self.min_distinct_price_ratio,
            "spread_expansion_multiple": self.spread_expansion_multiple,
            "min_ticks": self.min_ticks,
        }


@dataclass(frozen=True)
class RegimeState:
    symbol: str
    at: datetime
    regime: str
    direction: int
    eligible: frozenset[str]
    size_multiplier: Decimal
    no_trade_reason: str
    features: dict[str, float]
    ticks: int

    @property
    def tradeable(self) -> bool:
        return bool(self.eligible)

    def allows(self, family: str) -> bool:
        return family in self.eligible

    def audit_view(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "at": self.at.isoformat(),
            "regime": self.regime,
            "direction": self.direction,
            "eligible": sorted(self.eligible),
            "size_multiplier": str(self.size_multiplier),
            "no_trade_reason": self.no_trade_reason,
            "features": {key: round(value, 4) for key, value in self.features.items()},
            "ticks": self.ticks,
            "tradeable": self.tradeable,
        }


#: Which families may run in which regime, and at what size fraction.
#:
#: A regime with no entry is absent from this map on purpose: absence is the
#: refusal, and ``ABNORMAL``/``LOW_LIQUIDITY``/``SPREAD_EXPANSION`` therefore have
#: no way to become tradeable by accident.
_ELIGIBILITY: dict[str, tuple[tuple[str, ...], str]] = {
    TRENDING_UP: ((FAMILY_MOMENTUM, FAMILY_PULLBACK), "1.0"),
    TRENDING_DOWN: ((FAMILY_MOMENTUM, FAMILY_PULLBACK), "1.0"),
    RANGING: ((FAMILY_MEAN_REVERSION,), "1.0"),
    BREAKOUT: ((FAMILY_BREAKOUT, FAMILY_MOMENTUM), "0.75"),
    HIGH_VOL: ((FAMILY_BREAKOUT,), "0.5"),
    LOW_VOL: ((FAMILY_MEAN_REVERSION,), "0.5"),
}


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _stdev(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    average = _mean(values)
    variance = sum((value - average) ** 2 for value in values) / (len(values) - 1)
    return math.sqrt(variance)


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def classify(
    ticks: Sequence[Tick],
    *,
    spec: InstrumentSpec,
    thresholds: RegimeThresholds | None = None,
    spread_limit_points: Decimal | None = None,
) -> RegimeState:
    """Label the window.  Deterministic: same ticks in, same label out."""

    rules = thresholds or RegimeThresholds()
    window = list(ticks)
    point = float(spec.point)
    if not window:
        return RegimeState(
            symbol=spec.symbol,
            at=_never(),
            regime=ABNORMAL,
            direction=0,
            eligible=frozenset(),
            size_multiplier=Decimal("0"),
            no_trade_reason="no_ticks",
            features={},
            ticks=0,
        )

    mids = [float(tick.mid) for tick in window]
    spreads = [float(tick.spread) / point for tick in window]
    steps = [
        (mids[index] - mids[index - 1]) / point for index in range(1, len(mids))
    ]
    net_points = (mids[-1] - mids[0]) / point
    path_points = sum(abs(step) for step in steps)
    efficiency = abs(net_points) / path_points if path_points > 0 else 0.0
    volatility = _stdev(steps)
    typical_move = _median([abs(step) for step in steps])
    spread_median = _median(spreads)
    spread_now = spreads[-1]
    span_seconds = (window[-1].source_time - window[0].source_time).total_seconds()
    rate = (len(window) - 1) / span_seconds if span_seconds > 0 else 0.0
    distinct_ratio = len({round(mid, 10) for mid in mids}) / len(mids)
    features = {
        "net_points": net_points,
        "path_points": path_points,
        "efficiency": efficiency,
        "volatility_points": volatility,
        "typical_move_points": typical_move,
        "max_move_points": max((abs(step) for step in steps), default=0.0),
        "spread_points": spread_now,
        "spread_median_points": spread_median,
        "ticks_per_second": rate,
        "distinct_price_ratio": distinct_ratio,
        "ticks": float(len(window)),
        "span_seconds": span_seconds,
    }

    def state(
        regime: str,
        reason: str = "",
        *,
        direction: int = 0,
        size: str | None = None,
    ) -> RegimeState:
        families: tuple[str, ...] = ()
        multiplier = Decimal("0")
        if not reason:
            entry = _ELIGIBILITY.get(regime)
            if entry is not None:
                families = entry[0]
                multiplier = Decimal(size or entry[1])
        return RegimeState(
            symbol=spec.symbol,
            at=window[-1].source_time,
            regime=regime,
            direction=direction,
            eligible=frozenset(families),
            size_multiplier=multiplier,
            no_trade_reason=reason,
            features=features,
            ticks=len(window),
        )

    # -- refusals first, in the order that makes a stuck feed unmistakable ----
    if len(window) < rules.min_ticks:
        return state(ABNORMAL, "window_too_short")

    if spread_limit_points is not None and spread_now > float(spread_limit_points):
        return state(SPREAD_EXPANSION, "spread_above_the_declared_limit")

    if spread_median > 0 and spread_now > spread_median * rules.spread_expansion_multiple:
        return state(SPREAD_EXPANSION, NO_TRADE_REASONS[SPREAD_EXPANSION])

    if rate < rules.min_ticks_per_second:
        return state(LOW_LIQUIDITY, NO_TRADE_REASONS[LOW_LIQUIDITY])

    if distinct_ratio < rules.min_distinct_price_ratio:
        return state(LOW_LIQUIDITY, "the window shows almost no price variety")

    # A single print far outside the window's *typical* move is a data event, not a
    # breakout: comparing it with the window's own standard deviation would let the
    # outlier inflate the yardstick it is measured against, and a 1000-point print
    # would then be labelled a tradeable break of the range.
    if typical_move > 0 and features["max_move_points"] > (
        typical_move * rules.abnormal_tick_multiple
    ):
        return state(ABNORMAL, NO_TRADE_REASONS[ABNORMAL])

    if not math.isfinite(volatility) or not math.isfinite(net_points):
        return state(ABNORMAL, "non_finite_feature")

    # -- directional labels --------------------------------------------------
    recent_high = max(mids[:-1])
    recent_low = min(mids[:-1])
    buffer_points = rules.breakout_buffer_points
    if mids[-1] >= recent_high + buffer_points * point and efficiency >= rules.trend_efficiency:
        return state(BREAKOUT, direction=1)
    if mids[-1] <= recent_low - buffer_points * point and efficiency >= rules.trend_efficiency:
        return state(BREAKOUT, direction=-1)

    if abs(net_points) >= rules.trend_points and efficiency >= rules.trend_efficiency:
        return state(TRENDING_UP if net_points > 0 else TRENDING_DOWN, direction=1 if net_points > 0 else -1)

    if volatility >= rules.high_vol_points:
        return state(HIGH_VOL)

    if volatility <= rules.low_vol_points and efficiency <= rules.range_efficiency:
        return state(LOW_VOL)

    if efficiency <= rules.range_efficiency:
        return state(RANGING)

    # A window that is neither directional nor quiet: it is undecided, and an
    # undecided market is not one to scalp.  Saying "ranging" here would hand the
    # mean-reversion family a label the evidence does not support.
    return state(ABNORMAL, "undecided_between_trend_and_range")


def _never() -> datetime:
    from datetime import timezone

    return datetime(1970, 1, 1, tzinfo=timezone.utc)


__all__ = [
    "ABNORMAL",
    "ALL_FAMILIES",
    "BREAKOUT",
    "FAMILY_BREAKOUT",
    "FAMILY_MEAN_REVERSION",
    "FAMILY_MOMENTUM",
    "FAMILY_ORDER_FLOW",
    "FAMILY_PULLBACK",
    "HIGH_VOL",
    "LOW_LIQUIDITY",
    "LOW_VOL",
    "NO_TRADE_REASONS",
    "RANGING",
    "RegimeState",
    "RegimeThresholds",
    "SPREAD_EXPANSION",
    "TRENDING_DOWN",
    "TRENDING_UP",
    "classify",
]
