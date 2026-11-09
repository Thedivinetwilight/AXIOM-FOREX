"""SIGNAL GENERATOR: explicit, deterministic entry and exit rules.

Five families, each with rules written down in its own class docstring and in
:meth:`Strategy.spec`, so the thing that is validated is the thing that runs.  A
strategy never decides its own size, never checks its own approval and never
touches the account: it reads a window of ticks and either produces a
:class:`Signal` with a stop and a target in points, or says why not.

Parameters live in a frozen dataclass, and the strategy's **fingerprint** is a
hash of those parameters plus the rule version.  SKEPTIC approves a fingerprint,
so changing a parameter — any parameter — invalidates the approval by
construction rather than by remembering to.

Rules are written in points, not in currency, so the same strategy is comparable
across symbols.  A signal carries the tick it was computed from: an entry priced
from a quote the desk can no longer see is refused downstream.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping, Sequence

from . import regime as regime_module
from .config import InstrumentSpec
from .ticks import Tick

BUY = "BUY"
SELL = "SELL"

#: Outcomes of an evaluation.
SIGNAL = "SIGNAL"
NO_SETUP = "no_setup"
NOT_ELIGIBLE = "not_eligible_in_this_regime"
NOT_ENOUGH_TICKS = "not_enough_ticks"
FLOW_DATA_UNAVAILABLE = "trade_flow_data_unavailable"
SPREAD_TOO_WIDE = "spread_too_wide_for_this_strategy"
QUOTE_TOO_OLD = "quote_too_old"

#: Exit reasons the position manager understands.
EXIT_STOP = "stop"
EXIT_TARGET = "target"
EXIT_RULE = "rule"
EXIT_TIME = "max_hold"
EXIT_RECONCILE = "reconciled"


@dataclass(frozen=True)
class Signal:
    """One actionable idea, with its own invalidation price on both sides."""

    symbol: str
    family: str
    strategy: str
    side: str
    at: datetime
    tick_id: str
    reference_price: Decimal
    stop_points: Decimal
    target_points: Decimal
    reason: str
    rule_version: str
    parameter_fingerprint: str
    ttl_ms: int
    flow_proxy: Decimal | None = None

    @property
    def stop_price(self) -> Decimal | None:
        """Stop in price, from the reference.  ``None`` is never silently zero."""

        point = self._point
        if point is None or self.stop_points <= 0:
            return None
        offset = self.stop_points * point
        return self.reference_price - offset if self.side == BUY else self.reference_price + offset

    @property
    def target_price(self) -> Decimal | None:
        point = self._point
        if point is None or self.target_points <= 0:
            return None
        offset = self.target_points * point
        return self.reference_price + offset if self.side == BUY else self.reference_price - offset

    #: Set by the desk after construction; not part of the immutable record.
    _point: Decimal | None = None

    def with_point(self, point: Decimal) -> "Signal":
        return Signal(
            symbol=self.symbol,
            family=self.family,
            strategy=self.strategy,
            side=self.side,
            at=self.at,
            tick_id=self.tick_id,
            reference_price=self.reference_price,
            stop_points=self.stop_points,
            target_points=self.target_points,
            reason=self.reason,
            rule_version=self.rule_version,
            parameter_fingerprint=self.parameter_fingerprint,
            ttl_ms=self.ttl_ms,
            flow_proxy=self.flow_proxy,
            _point=point,
        )

    def audit_view(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "family": self.family,
            "strategy": self.strategy,
            "side": self.side,
            "at": self.at.isoformat(),
            "tick_id": self.tick_id,
            "reference_price": str(self.reference_price),
            "stop_points": str(self.stop_points),
            "target_points": str(self.target_points),
            "reason": self.reason,
            "rule_version": self.rule_version,
            "parameter_fingerprint": self.parameter_fingerprint,
            "ttl_ms": self.ttl_ms,
            "flow_proxy": None if self.flow_proxy is None else str(self.flow_proxy),
        }


@dataclass(frozen=True)
class Evaluation:
    """A signal, or the reason there is not one.  Both are recorded."""

    strategy: str
    family: str
    outcome: str
    detail: str = ""
    signal: Signal | None = None
    at: datetime | None = None

    @property
    def traded(self) -> bool:
        return self.signal is not None

    def audit_view(self) -> dict[str, Any]:
        return {
            "strategy": self.strategy,
            "family": self.family,
            "outcome": self.outcome,
            "detail": self.detail,
            "at": None if self.at is None else self.at.isoformat(),
            "signal": None if self.signal is None else self.signal.audit_view(),
        }


@dataclass(frozen=True)
class SignalContext:
    """Everything a rule may read.  No account, no broker, no clock of its own."""

    ticks: tuple[Tick, ...]
    spec: InstrumentSpec
    regime: regime_module.RegimeState
    spread_limit_points: Decimal
    max_quote_age_ms: int

    @property
    def last(self) -> Tick | None:
        return self.ticks[-1] if self.ticks else None

    def points(self, new: Decimal, old: Decimal) -> Decimal:
        point = self.spec.point
        return (new - old) / point if point > 0 else Decimal(0)


class Strategy:
    """Base class: identity, documented rules, and a fingerprint over both."""

    family: str = ""
    name: str = ""
    rule_version: str = "v1"
    requires_flow_data: bool = False

    def __init__(self, ttl_ms: int = 750) -> None:
        self._ttl_ms = int(ttl_ms)

    # -- identity --------------------------------------------------------

    @property
    def parameters(self) -> Any:
        raise NotImplementedError

    @property
    def ttl_ms(self) -> int:
        return self._ttl_ms

    @property
    def fingerprint(self) -> str:
        payload = {
            "name": self.name,
            "family": self.family,
            "rule_version": self.rule_version,
            "parameters": _canonical(self.parameters),
            "ttl_ms": self._ttl_ms,
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]

    def spec(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "family": self.family,
            "rule_version": self.rule_version,
            "fingerprint": self.fingerprint,
            "parameters": _canonical(self.parameters),
            "requires_flow_data": self.requires_flow_data,
            "entry_rule": self.entry_rule,
            "exit_rule": self.exit_rule,
            "eligibility": f"regime family '{self.family}' must be eligible",
        }

    @property
    def entry_rule(self) -> str:
        raise NotImplementedError

    @property
    def exit_rule(self) -> str:
        raise NotImplementedError

    # -- evaluation ------------------------------------------------------

    def evaluate(self, ctx: SignalContext) -> Evaluation:
        if not ctx.regime.allows(self.family):
            return Evaluation(
                self.name,
                self.family,
                NOT_ELIGIBLE,
                f"regime {ctx.regime.regime} does not admit {self.family}",
                at=ctx.last.source_time if ctx.last else None,
            )
        spread = ctx.regime.features.get("spread_points")
        if spread is not None and Decimal(str(spread)) > ctx.spread_limit_points:
            return Evaluation(
                self.name,
                self.family,
                SPREAD_TOO_WIDE,
                f"spread {spread:.1f} points above the {ctx.spread_limit_points} limit",
                at=ctx.last.source_time if ctx.last else None,
            )
        if len(ctx.ticks) < self.minimum_ticks:
            return Evaluation(
                self.name,
                self.family,
                NOT_ENOUGH_TICKS,
                f"{len(ctx.ticks)} ticks, needs {self.minimum_ticks}",
                at=ctx.last.source_time if ctx.last else None,
            )
        if self.requires_flow_data and not _flow_available(ctx.ticks):
            return Evaluation(
                self.name,
                self.family,
                FLOW_DATA_UNAVAILABLE,
                "this feed carries no trade-volume direction, so order flow cannot be "
                "estimated from it; the strategy stays off rather than guessing",
                at=ctx.last.source_time if ctx.last else None,
            )
        return self._evaluate(ctx)

    @property
    def minimum_ticks(self) -> int:
        raise NotImplementedError

    def _evaluate(self, ctx: SignalContext) -> Evaluation:
        raise NotImplementedError

    # -- helpers ---------------------------------------------------------

    def _signal(
        self,
        ctx: SignalContext,
        *,
        side: str,
        reason: str,
        stop_points: Decimal,
        target_points: Decimal,
        flow_proxy: Decimal | None = None,
    ) -> Evaluation:
        last = ctx.last
        if last is None:
            return Evaluation(self.name, self.family, NO_SETUP, "no tick", at=None)
        reference = last.ask if side == BUY else last.bid
        signal = Signal(
            symbol=ctx.spec.symbol,
            family=self.family,
            strategy=self.name,
            side=side,
            at=last.source_time,
            tick_id=last.tick_id,
            reference_price=reference,
            stop_points=stop_points,
            target_points=target_points,
            reason=reason,
            rule_version=self.rule_version,
            parameter_fingerprint=self.fingerprint,
            ttl_ms=self._ttl_ms,
            flow_proxy=flow_proxy,
            _point=ctx.spec.point,
        )
        return Evaluation(self.name, self.family, SIGNAL, reason, signal=signal, at=last.source_time)

    def _declined(self, ctx: SignalContext, detail: str) -> Evaluation:
        return Evaluation(
            self.name,
            self.family,
            NO_SETUP,
            detail,
            at=ctx.last.source_time if ctx.last else None,
        )


def _canonical(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {key: _canonical(item) for key, item in asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    return value


def _mids(ctx: SignalContext) -> list[float]:
    return [float(tick.mid) for tick in ctx.ticks]


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _stdev(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    average = _mean(values)
    return math.sqrt(sum((value - average) ** 2 for value in values) / (len(values) - 1))


def _flow_available(ticks: Sequence[Tick]) -> bool:
    """Trade-flow direction needs volume.  A feed that reports none cannot supply it."""

    if len(ticks) < 10:
        return False
    with_volume = sum(1 for tick in ticks if tick.volume is not None and tick.volume > 0)
    return with_volume >= max(10, int(len(ticks) * 0.8))


# ---------------------------------------------------------------------------
# 1. short-term momentum
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MomentumParameters:
    lookback_ticks: int = 8
    min_move_points: float = 3.0
    stop_points: str = "10"
    target_points: str = "14"
    volatility_stop_multiple: float = 2.5
    max_spread_points: str = "20"


class MomentumContinuation(Strategy):
    """Short-term momentum: follow a move that is still going.

    **Entry.** Over the last ``lookback_ticks`` quotes the mid must travel at least
    ``min_move_points`` and the final two steps must agree in sign.  Buy on the
    rising case, sell on the falling one.

    **Exit.** A stop at ``max(stop_points, volatility_stop_multiple × typical tick
    move)`` points, a target of ``target_points`` points, or the momentum rule
    reversing (the position manager's ``MOMENTUM_FLIP`` exit).  Maximum holding time
    comes from the desk configuration, not from this class.
    """

    family = regime_module.FAMILY_MOMENTUM
    name = "momentum_continuation"
    rule_version = "v1"

    def __init__(self, parameters: MomentumParameters | None = None, *, ttl_ms: int = 750) -> None:
        super().__init__(ttl_ms=ttl_ms)
        self._parameters = parameters or MomentumParameters()

    @property
    def parameters(self) -> MomentumParameters:
        return self._parameters

    @property
    def minimum_ticks(self) -> int:
        return self._parameters.lookback_ticks + 2

    @property
    def entry_rule(self) -> str:
        return (
            "net move over the last lookback_ticks >= min_move_points in one direction "
            "and the last two steps share that sign"
        )

    @property
    def exit_rule(self) -> str:
        return (
            "stop at max(stop_points, volatility_stop_multiple x typical move); "
            "target at target_points; momentum flip exits; max hold from config"
        )

    def _evaluate(self, ctx: SignalContext) -> Evaluation:
        params = self._parameters
        mids = _mids(ctx)
        lookback = params.lookback_ticks
        steps = [mids[index] - mids[index - 1] for index in range(len(mids) - lookback, len(mids))]
        net = mids[-1] - mids[-1 - lookback]
        point = float(ctx.spec.point)
        net_points = net / point if point else 0.0
        typical = _mean([abs(step) / point for step in steps]) if steps else 0.0
        if len(steps) >= 2 and all(step > 0 for step in steps[-2:]) and net_points >= params.min_move_points:
            side, reason = BUY, "rising_momentum"
        elif len(steps) >= 2 and all(step < 0 for step in steps[-2:]) and net_points <= -params.min_move_points:
            side, reason = SELL, "falling_momentum"
        else:
            return self._declined(
                ctx, f"net move {net_points:.1f} points is not a directional run"
            )
        spread_points = ctx.points(ctx.last.ask, ctx.last.bid) if ctx.last else Decimal(0)
        if spread_points > Decimal(params.max_spread_points):
            return Evaluation(
                self.name, self.family, SPREAD_TOO_WIDE,
                f"spread {spread_points} above this strategy's {params.max_spread_points}",
                at=ctx.last.source_time if ctx.last else None,
            )
        volatility_stop = max(float(params.stop_points), typical * params.volatility_stop_multiple)
        return self._signal(
            ctx,
            side=side,
            reason=reason,
            stop_points=Decimal(str(round(volatility_stop, 1))),
            target_points=Decimal(params.target_points),
        )


# ---------------------------------------------------------------------------
# 2. breakout
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BreakoutParameters:
    window: int = 40
    buffer_points: float = 2.0
    stop_points: str = "12"
    target_points: str = "18"
    max_spread_points: str = "20"


class BreakoutContinuation(Strategy):
    """Breakout: a new window extreme, outside the range by a declared buffer.

    **Entry.** The last mid exceeds the highest mid of the previous ``window``
    quotes by at least ``buffer_points`` (or falls below the lowest by the same
    buffer).  Only eligible in a BREAKOUT regime, so a range that is merely noisy
    does not qualify.

    **Exit.** ``stop_points`` / ``target_points``, or the FALLBACK_RANGE exit when
    price closes back inside the range it broke out of.
    """

    family = regime_module.FAMILY_BREAKOUT
    name = "breakout_continuation"
    rule_version = "v1"

    def __init__(self, parameters: BreakoutParameters | None = None, *, ttl_ms: int = 750) -> None:
        super().__init__(ttl_ms=ttl_ms)
        self._parameters = parameters or BreakoutParameters()

    @property
    def parameters(self) -> BreakoutParameters:
        return self._parameters

    @property
    def minimum_ticks(self) -> int:
        return self._parameters.window + 1

    @property
    def entry_rule(self) -> str:
        return (
            "last mid beyond the previous window high (or low) by buffer_points, in a "
            "BREAKOUT regime"
        )

    @property
    def exit_rule(self) -> str:
        return "stop/target in points; FALLBACK_RANGE exit if price returns inside the range"

    def _evaluate(self, ctx: SignalContext) -> Evaluation:
        params = self._parameters
        mids = _mids(ctx)
        point = float(ctx.spec.point)
        window = mids[-params.window - 1 : -1]
        if len(window) < params.window:
            return self._declined(ctx, "window incomplete")
        high, low = max(window), min(window)
        buffer = params.buffer_points * point
        spread_points = ctx.points(ctx.last.ask, ctx.last.bid) if ctx.last else Decimal(0)
        if spread_points > Decimal(params.max_spread_points):
            return Evaluation(
                self.name, self.family, SPREAD_TOO_WIDE,
                f"spread {spread_points} above this strategy's {params.max_spread_points}",
                at=ctx.last.source_time if ctx.last else None,
            )
        if mids[-1] >= high + buffer:
            side, reason = BUY, "broke_above_the_window_high"
        elif mids[-1] <= low - buffer:
            side, reason = SELL, "broke_below_the_window_low"
        else:
            return self._declined(ctx, "no new extreme outside the buffer")
        return self._signal(
            ctx,
            side=side,
            reason=reason,
            stop_points=Decimal(params.stop_points),
            target_points=Decimal(params.target_points),
        )


# ---------------------------------------------------------------------------
# 3. pullback continuation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PullbackParameters:
    fast_ticks: int = 5
    slow_ticks: int = 20
    tolerance_points: float = 2.0
    stop_points: str = "9"
    target_points: str = "13"
    max_spread_points: str = "20"


class PullbackContinuation(Strategy):
    """Pullback continuation: a trend that dips and reclaims, not a trend chased.

    **Entry.** The fast mean is above the slow mean (uptrend) by at least
    ``tolerance_points``, the last mid has come back to within ``tolerance_points``
    of the fast mean, and the last step is upward — a reclaim.  The mirror applies
    downwards.

    **Exit.** ``stop_points`` / ``target_points``, or the TREND_BROKEN exit when the
    fast mean crosses the slow mean against the position.
    """

    family = regime_module.FAMILY_PULLBACK
    name = "pullback_continuation"
    rule_version = "v1"

    def __init__(self, parameters: PullbackParameters | None = None, *, ttl_ms: int = 750) -> None:
        super().__init__(ttl_ms=ttl_ms)
        self._parameters = parameters or PullbackParameters()

    @property
    def parameters(self) -> PullbackParameters:
        return self._parameters

    @property
    def minimum_ticks(self) -> int:
        return self._parameters.slow_ticks + 2

    @property
    def entry_rule(self) -> str:
        return (
            "fast mean beyond slow mean by tolerance_points, last mid within "
            "tolerance_points of the fast mean, and the last step reclaiming"
        )

    @property
    def exit_rule(self) -> str:
        return "stop/target in points; TREND_BROKEN exit if the fast mean crosses the slow mean"

    def _evaluate(self, ctx: SignalContext) -> Evaluation:
        params = self._parameters
        mids = _mids(ctx)
        point = float(ctx.spec.point)
        tolerance = params.tolerance_points * point
        fast = _mean(mids[-params.fast_ticks :])
        slow = _mean(mids[-params.slow_ticks :])
        last, previous = mids[-1], mids[-2]
        spread_points = ctx.points(ctx.last.ask, ctx.last.bid) if ctx.last else Decimal(0)
        if spread_points > Decimal(params.max_spread_points):
            return Evaluation(
                self.name, self.family, SPREAD_TOO_WIDE,
                f"spread {spread_points} above this strategy's {params.max_spread_points}",
                at=ctx.last.source_time if ctx.last else None,
            )
        up_trend = fast - slow >= tolerance
        down_trend = slow - fast >= tolerance
        if up_trend and abs(last - fast) <= tolerance and last > previous:
            side, reason = BUY, "uptrend_pullback_reclaimed"
        elif down_trend and abs(last - fast) <= tolerance and last < previous:
            side, reason = SELL, "downtrend_pullback_reclaimed"
        else:
            return self._declined(ctx, "no trend pullback with a reclaim")
        return self._signal(
            ctx,
            side=side,
            reason=reason,
            stop_points=Decimal(params.stop_points),
            target_points=Decimal(params.target_points),
        )


# ---------------------------------------------------------------------------
# 4. mean reversion
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MeanReversionParameters:
    window: int = 40
    z_threshold: float = 2.0
    stop_points: str = "10"
    min_target_points: str = "4"
    max_spread_points: str = "20"


class MeanReversion(Strategy):
    """Mean reversion: fade a stretch away from the window mean.

    **Entry.** The last mid is at least ``z_threshold`` standard deviations from the
    mean of the last ``window`` mids, in a RANGING or LOW_VOL regime.  Only there:
    fading a trend is how a scalping account dies.

    **Exit.** Target the window mean (distance to it, floored at
    ``min_target_points``); stop at ``stop_points``.  If price never returns, the
    offer expires and the stop or the max-hold rule closes it.
    """

    family = regime_module.FAMILY_MEAN_REVERSION
    name = "mean_reversion"
    rule_version = "v1"

    def __init__(self, parameters: MeanReversionParameters | None = None, *, ttl_ms: int = 750) -> None:
        super().__init__(ttl_ms=ttl_ms)
        self._parameters = parameters or MeanReversionParameters()

    @property
    def parameters(self) -> MeanReversionParameters:
        return self._parameters

    @property
    def minimum_ticks(self) -> int:
        return self._parameters.window + 1

    @property
    def entry_rule(self) -> str:
        return "last mid at least z_threshold standard deviations from the window mean in a range"

    @property
    def exit_rule(self) -> str:
        return "target the window mean (floored); stop at stop_points; max hold from config"

    def _evaluate(self, ctx: SignalContext) -> Evaluation:
        params = self._parameters
        mids = _mids(ctx)
        point = float(ctx.spec.point)
        window = mids[-params.window :]
        mean = _mean(window)
        sigma = _stdev(window)
        spread_points = ctx.points(ctx.last.ask, ctx.last.bid) if ctx.last else Decimal(0)
        if spread_points > Decimal(params.max_spread_points):
            return Evaluation(
                self.name, self.family, SPREAD_TOO_WIDE,
                f"spread {spread_points} above this strategy's {params.max_spread_points}",
                at=ctx.last.source_time if ctx.last else None,
            )
        if sigma <= 0 or point <= 0:
            return self._declined(ctx, "window has no dispersion to revert from")
        deviation = (mids[-1] - mean) / sigma
        if abs(deviation) < params.z_threshold:
            return self._declined(ctx, f"z={deviation:.2f} inside the threshold")
        side = SELL if deviation > 0 else BUY
        target_points = abs(mids[-1] - mean) / point
        target = max(float(params.min_target_points), round(target_points, 1))
        return self._signal(
            ctx,
            side=side,
            reason=f"z_score_{deviation:.2f}",
            stop_points=Decimal(params.stop_points),
            target_points=Decimal(str(target)),
        )


# ---------------------------------------------------------------------------
# 5. order-flow momentum
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrderFlowParameters:
    window: int = 60
    imbalance_threshold: float = 0.62
    stop_points: str = "10"
    target_points: str = "12"
    max_spread_points: str = "20"


class OrderFlowMomentum(Strategy):
    """Order-flow momentum: follow persistent buy-side pressure.

    **Data it needs.** A trade-direction estimate per quote.  With the tick rule
    (an uptick is buy-side, a downtick sell-side) weighted by reported volume, this
    is a *proxy computed from one broker's quote stream* — it is not the global
    order book, and the same rule applied to another broker would give another
    number.  When the feed carries no usable volume the strategy is **off**: it
    returns ``trade_flow_data_unavailable`` rather than inventing flow.

    **Entry.** Volume-weighted uptick share over ``window`` quotes is at least
    ``imbalance_threshold`` (buy) or at most ``1 − threshold`` (sell), and the last
    step agrees with the flow.

    **Exit.** ``stop_points`` / ``target_points``, or FLOW_REVERSED when the share
    crosses back through the midpoint.
    """

    family = regime_module.FAMILY_ORDER_FLOW
    name = "order_flow_momentum"
    rule_version = "v1"
    requires_flow_data = True

    def __init__(self, parameters: OrderFlowParameters | None = None, *, ttl_ms: int = 750) -> None:
        super().__init__(ttl_ms=ttl_ms)
        self._parameters = parameters or OrderFlowParameters()

    @property
    def parameters(self) -> OrderFlowParameters:
        return self._parameters

    @property
    def minimum_ticks(self) -> int:
        return self._parameters.window + 1

    @property
    def entry_rule(self) -> str:
        return (
            "volume-weighted uptick share over window >= imbalance_threshold (buy) or "
            "<= 1 - threshold (sell), last step agreeing"
        )

    @property
    def exit_rule(self) -> str:
        return "stop/target in points; FLOW_REVERSED exit when the share recrosses the midpoint"

    def _evaluate(self, ctx: SignalContext) -> Evaluation:
        params = self._parameters
        mids = _mids(ctx)
        ticks = ctx.ticks[-params.window :]
        point = float(ctx.spec.point)
        buy_volume = 0.0
        sell_volume = 0.0
        for index in range(1, len(ticks)):
            volume = float(ticks[index].volume or 0)
            if volume <= 0:
                continue
            if mids[-len(ticks) + index] > mids[-len(ticks) + index - 1]:
                buy_volume += volume
            elif mids[-len(ticks) + index] < mids[-len(ticks) + index - 1]:
                sell_volume += volume
        total = buy_volume + sell_volume
        if total <= 0:
            return Evaluation(
                self.name, self.family, FLOW_DATA_UNAVAILABLE,
                "no reported volume in this window",
                at=ctx.last.source_time if ctx.last else None,
            )
        share = buy_volume / total
        last_step = mids[-1] - mids[-2]
        if share >= params.imbalance_threshold and last_step > 0:
            side, reason = BUY, f"buy_share_{share:.2f}"
        elif share <= 1 - params.imbalance_threshold and last_step < 0:
            side, reason = SELL, f"sell_share_{1 - share:.2f}"
        else:
            return self._declined(
                ctx, f"volume-weighted buy share {share:.2f} is not one-sided enough"
            )
        return self._signal(
            ctx,
            side=side,
            reason=reason,
            stop_points=Decimal(params.stop_points),
            target_points=Decimal(params.target_points),
            flow_proxy=Decimal(str(round(share, 4))),
        )


def default_strategies(*, ttl_ms: int = 750) -> tuple[Strategy, ...]:
    """The five declared families, with default parameters.

    Order of the tuple is the order they are evaluated, which is also the order
    ties are broken in — it is fixed here, not by a dictionary's iteration order.
    """

    return (
        MomentumContinuation(ttl_ms=ttl_ms),
        BreakoutContinuation(ttl_ms=ttl_ms),
        PullbackContinuation(ttl_ms=ttl_ms),
        MeanReversion(ttl_ms=ttl_ms),
        OrderFlowMomentum(ttl_ms=ttl_ms),
    )


__all__ = [
    "BreakoutContinuation",
    "BreakoutParameters",
    "BUY",
    "Evaluation",
    "EXIT_RECONCILE",
    "EXIT_RULE",
    "EXIT_STOP",
    "EXIT_TARGET",
    "EXIT_TIME",
    "FLOW_DATA_UNAVAILABLE",
    "MeanReversion",
    "MeanReversionParameters",
    "MomentumContinuation",
    "MomentumParameters",
    "NO_SETUP",
    "NOT_ELIGIBLE",
    "NOT_ENOUGH_TICKS",
    "OrderFlowMomentum",
    "OrderFlowParameters",
    "PullbackContinuation",
    "PullbackParameters",
    "QUOTE_TOO_OLD",
    "SELL",
    "SIGNAL",
    "SPREAD_TOO_WIDE",
    "Signal",
    "SignalContext",
    "Strategy",
    "default_strategies",
]
