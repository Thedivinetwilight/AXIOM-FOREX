"""AX-LAB: replay, simulate honestly, and report what actually happened.

The lab is not a backtester that reports a number.  It is a set of instruments for
answering questions that have answers:

* what did the tick core accept, refuse, and flag, and why;
* how many opportunities became signals, how many signals survived the EV gate,
  how many survived sizing and the guardian, and how many became fills;
* what did each closed trade earn **after** spread, commission and slippage;
* how does that change when costs double, when the spread widens, or when fills
  arrive late;
* which regime and which session produced the results;
* and does a strategy validated on one window still work on the next one?

The scenarios are synthetic, deterministic, and labelled ``SAMPLE_DATA``.  They are
built to exercise machinery — trends, ranges, breakouts, spread explosions,
duplicate ticks, missing ticks, disconnects — not to represent any broker's market
or any particular day.

Training and evaluation are separated on purpose.  Calibration comes from a
*training* replay (:func:`collect_outcomes`), the frozen posteriors are then used on
a *different* replay, and the evaluation trades are what get reported.  A number
produced by looking at the test window and adjusting something is not a result, so
the lab never does it.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import bayes, skeptic
from .config import PAPER, ScalperConfig, fixture_config
from .desk import ScalperDesk, DeskThresholds
from .execution import PaperVenue, VenueFaults
from .guardian import Guardian, GuardianLimits
from .signals import BUY, SELL, Signal, Strategy, default_strategies
from .ticks import (
    SRC_REPLAY,
    SRC_SAMPLE,
    ReplayTick,
    TickCore,
    synthetic_ticks,
)

UTC = timezone.utc
SAMPLE = "SAMPLE_DATA"

#: The lab's own vocabulary for what it is allowed to claim.
NO_PROFIT_CLAIM = (
    "These are measurements on synthetic ticks with declared costs. They are not a "
    "profit claim, not a forecast, and not evidence that any strategy has an edge in "
    "a live market."
)


@dataclass(frozen=True)
class Scenario:
    """A deterministic replay, its declared purpose, and what was injected into it."""

    name: str
    description: str
    ticks: tuple[ReplayTick, ...]
    data_class: str = SAMPLE
    injected: tuple[str, ...] = ()
    faults: VenueFaults = field(default_factory=VenueFaults)
    notes: str = ""

    @property
    def digest(self) -> str:
        body = "|".join(
            f"{tick.symbol}:{tick.bid}:{tick.ask}:{tick.at.isoformat()}" for tick in self.ticks
        )
        return hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]

    def audit_view(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "data_class": self.data_class,
            "ticks": len(self.ticks),
            "injected": list(self.injected),
            "digest": self.digest,
            "notes": self.notes,
        }


def _shift(ticks: Sequence[ReplayTick], *, start: datetime) -> tuple[ReplayTick, ...]:
    if not ticks:
        return ()
    offset = start - ticks[0].at
    return tuple(
        ReplayTick(
            symbol=tick.symbol,
            bid=tick.bid,
            ask=tick.ask,
            at=tick.at + offset,
            volume=tick.volume,
        )
        for tick in ticks
    )


# ---------------------------------------------------------------------------
# scenarios
# ---------------------------------------------------------------------------


def trending(
    *, symbol: str = "EURUSD.pro", count: int = 400, drift_points: float = 1.6, noise_points: float = 1.2
) -> Scenario:
    ticks = synthetic_ticks(
        symbol=symbol, count=count, drift_points=drift_points, noise_points=noise_points, step_ms=200
    )
    return Scenario(
        name=f"trend_up_{int(drift_points * 10)}",
        description="a directional run with noise: momentum and pullback territory",
        ticks=tuple(ticks),
        notes="Synthetic sine plus drift. Not a market sample.",
    )


def ranging(*, symbol: str = "EURUSD.pro", count: int = 400) -> Scenario:
    ticks = synthetic_ticks(
        symbol=symbol, count=count, drift_points=0.0, noise_points=6.0, step_ms=200
    )
    return Scenario(
        name="ranging",
        description="no net direction and a wide oscillation: mean-reversion territory",
        ticks=tuple(ticks),
    )


def breakout(*, symbol: str = "EURUSD.pro", count: int = 400) -> Scenario:
    quiet = synthetic_ticks(symbol=symbol, count=count // 2, drift_points=0.0, noise_points=0.6, step_ms=200)
    run = synthetic_ticks(
        symbol=symbol,
        count=count // 2,
        drift_points=2.2,
        noise_points=0.8,
        step_ms=200,
        start=quiet[-1].at + timedelta(milliseconds=200),
        mid=str(quiet[-1].mid),
    )
    return Scenario(
        name="breakout",
        description="a compressed range followed by an expansion",
        ticks=tuple(quiet) + tuple(run),
    )


def spread_expansion(*, symbol: str = "EURUSD.pro", count: int = 300) -> Scenario:
    base = synthetic_ticks(symbol=symbol, count=count, drift_points=0.8, noise_points=1.0, step_ms=200)
    widened: list[ReplayTick] = []
    for index, tick in enumerate(base):
        factor = 1 if index < count // 3 else (6 if index < 2 * count // 3 else 2)
        half = (tick.ask - tick.bid) * factor / Decimal(2)
        mid = tick.mid
        widened.append(
            ReplayTick(symbol=tick.symbol, bid=mid - half, ask=mid + half, at=tick.at, volume=tick.volume)
        )
    return Scenario(
        name="spread_expansion",
        description="the same price path with the spread widened six-fold in the middle",
        ticks=tuple(widened),
        injected=("spread_expansion",),
        notes="This is the scenario that must produce refusals, not heroics.",
    )


def intermittent(*, symbol: str = "EURUSD.pro", count: int = 300) -> Scenario:
    """Ticks arrive for a while, then stop for a while, then resume."""

    base = synthetic_ticks(symbol=symbol, count=count, drift_points=1.0, noise_points=1.0, step_ms=200)
    kept: list[ReplayTick] = []
    moment = base[0].at
    for index, tick in enumerate(base):
        if count // 3 <= index < count // 3 + 40:
            moment = moment + timedelta(seconds=6)  # a six-second hole
            continue
        kept.append(
            ReplayTick(symbol=tick.symbol, bid=tick.bid, ask=tick.ask, at=moment, volume=tick.volume)
        )
        moment = moment + timedelta(milliseconds=200)
    return Scenario(
        name="intermittent_feed",
        description="a disconnect: six seconds with no ticks, then a resumed feed",
        ticks=tuple(kept),
        injected=("tick_gap",),
    )


def dirty_feed(*, symbol: str = "EURUSD.pro", count: int = 300) -> Scenario:
    """Duplicate ticks, an out-of-order tick and a stale run, injected deterministically."""

    base = synthetic_ticks(symbol=symbol, count=count, drift_points=1.0, noise_points=1.0, step_ms=200)
    dirty: list[ReplayTick] = []
    for index, tick in enumerate(base):
        dirty.append(tick)
        if index in {40, 41, 42}:
            dirty.append(tick)  # exact duplicates
        if index == 90:
            dirty.append(base[index - 5])  # out of order
        if 150 <= index < 170:
            dirty.append(
                ReplayTick(symbol=tick.symbol, bid=tick.bid, ask=tick.ask, at=tick.at + timedelta(milliseconds=50))
            )  # repeated quote content
    return Scenario(
        name="dirty_feed",
        description="duplicates, an out-of-order tick and repeated quotes",
        ticks=tuple(dirty),
        injected=("duplicates", "out_of_order", "repeated_quotes"),
    )


def reversal(*, symbol: str = "EURUSD.pro", count: int = 400) -> Scenario:
    """An entry-friendly run that reverses hard: the scenario that produces losers."""

    up = synthetic_ticks(
        symbol=symbol, count=count // 2, drift_points=1.8, noise_points=1.2, step_ms=200
    )
    down = synthetic_ticks(
        symbol=symbol,
        count=count - count // 2,
        drift_points=-3.2,
        noise_points=2.0,
        step_ms=200,
        start=up[-1].at + timedelta(milliseconds=200),
        mid=str(up[-1].mid),
    )
    return Scenario(
        name="reversal",
        description="a run up that turns into a hard decline: stops are supposed to be hit here",
        ticks=tuple(up) + tuple(down),
        injected=("trend_reversal",),
        notes="Without a scenario like this, a sample has no losers and no honest win rate.",
    )


def volatile(*, symbol: str = "EURUSD.pro", count: int = 400) -> Scenario:
    ticks = synthetic_ticks(
        symbol=symbol, count=count, drift_points=0.0, noise_points=18.0, step_ms=150
    )
    return Scenario(
        name="volatile",
        description="large tick-to-tick moves: the regime filter should refuse a good deal of it",
        ticks=tuple(ticks),
        injected=("high_volatility",),
    )


def standard_scenarios(symbol: str = "EURUSD.pro") -> tuple[Scenario, ...]:
    return (
        trending(symbol=symbol),
        ranging(symbol=symbol),
        breakout(symbol=symbol),
        reversal(symbol=symbol),
        spread_expansion(symbol=symbol),
        intermittent(symbol=symbol),
        dirty_feed(symbol=symbol),
        volatile(symbol=symbol),
    )


def repriced(scenario: Scenario, *, spread_multiple: float) -> Scenario:
    """The same price path at a wider spread: a cost regime change, nothing else.

    Used to ask whether the desk degrades when the market gets more expensive, without
    changing the generator's price path (which would confound the two effects).
    """

    if spread_multiple <= 0:
        raise ValueError("spread_multiple must be positive")
    factor = Decimal(str(spread_multiple))
    widened = []
    for tick in scenario.ticks:
        half = (tick.ask - tick.bid) * factor / Decimal(2)
        widened.append(
            ReplayTick(
                symbol=tick.symbol,
                bid=tick.mid - half,
                ask=tick.mid + half,
                at=tick.at,
                volume=tick.volume,
            )
        )
    return replace(
        scenario,
        name=f"{scenario.name}_cost_x{spread_multiple:g}",
        ticks=tuple(widened),
        injected=tuple(scenario.injected) + ("spread_widened",),
        notes=scenario.notes + f" (spread multiplied by {spread_multiple:g} for a cost stress)",
    )


def shifted(scenario: Scenario, *, start: datetime) -> Scenario:
    """Move a scenario later so a train and a test window do not overlap in time."""

    return replace(
        scenario,
        name=f"{scenario.name}_shifted",
        ticks=_shift(scenario.ticks, start=start),
        notes=scenario.notes + " (time-shifted for an out-of-sample window)",
    )


#: Targets must clear the round-turn cost by this multiple before the arithmetic is
#: even worth simulating.  It is not a tuning result: a bracket whose target is
#: smaller than the cost of a round turn cannot pay at any win rate below 100 %.
COST_MULTIPLE_FOR_TARGET = 3


def cost_compliant_strategies(
    *,
    assumed_spread_points: Decimal,
    stops_level_points: Decimal,
    cost: Any,
    point_value_per_lot: Decimal | None = None,
    strategies: Sequence[Strategy] | None = None,
) -> tuple[Strategy, ...]:
    """Widen brackets until they *can* pay, using arithmetic rather than a backtest.

    The rule is declared up front: a target must be at least
    ``COST_MULTIPLE_FOR_TARGET`` times the round-turn cost, and a stop must clear the
    broker's stops level with margin.  Nothing here is chosen because a replay liked
    it; a strategy whose target is smaller than its cost cannot be worth simulating,
    whatever the replay says.

    The inputs are the declared cost terms and the *training* window's median spread.
    The evaluation window never influences these numbers.
    """

    from dataclasses import replace as _replace

    from .signals import default_strategies as _defaults

    # Round turn: spread once, declared slippage twice, commission in points.
    slippage = Decimal(str(cost.slippage_points)) * 2
    commission_points = Decimal(0)
    if point_value_per_lot and point_value_per_lot > 0:
        commission_points = Decimal(str(cost.commission_per_lot)) / point_value_per_lot
    round_turn = Decimal(str(assumed_spread_points)) + slippage + commission_points
    target_floor = round_turn * Decimal(COST_MULTIPLE_FOR_TARGET)
    stop_floor = max(Decimal(str(stops_level_points)) * Decimal("1.25"), round_turn)

    chosen: list[Strategy] = []
    for strategy in strategies or _defaults():
        parameters = strategy.parameters
        fields = getattr(parameters, "__dataclass_fields__", {})
        updates: dict[str, Any] = {}
        if "stop_points" in fields:
            current = Decimal(str(getattr(parameters, "stop_points")))
            updates["stop_points"] = str(max(current, stop_floor))
        for name in ("target_points", "min_target_points"):
            if name in fields:
                current = Decimal(str(getattr(parameters, name)))
                updates[name] = str(max(current, target_floor))
        if updates:
            chosen.append(type(strategy)(_replace(parameters, **updates), ttl_ms=strategy.ttl_ms))
        else:
            chosen.append(strategy)
    return tuple(chosen)


# ---------------------------------------------------------------------------
# training pass: signals replayed to their outcome, no orders
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrainingOutcome:
    scenario: str
    fingerprint: str
    strategy: str
    regime: str
    won: bool
    gross_points: float
    cost_points: float
    holding_ms: int
    note: str = "TRAIN_SIMULATION: no order was placed; the bracket was replayed"

    @property
    def net_points(self) -> float:
        return self.gross_points - self.cost_points


def collect_outcomes(
    scenarios: Sequence[Scenario],
    *,
    config: ScalperConfig,
    strategies: Sequence[Strategy] | None = None,
    max_hold_ms: int | None = None,
) -> dict[str, Any]:
    """Calibrate on a training window by replaying each signal's bracket to its end.

    This is a *measurement* pass, not a trading pass: it asks "when these rules fired,
    what happened next", and it records that answer per (fingerprint, regime).  It
    places no orders and its P&L figures never enter a performance report.
    """

    strategies = tuple(strategies or default_strategies())
    specs = config.instruments_by_symbol
    hold = max_hold_ms if max_hold_ms is not None else config.max_hold_ms
    book = bayes.OutcomeBook()
    outcomes: list[TrainingOutcome] = []
    spreads: list[float] = []

    for scenario in scenarios:
        symbol = scenario.ticks[0].symbol if scenario.ticks else ""
        spec = specs.get(symbol)
        if spec is None:
            continue
        core = TickCore(
            instruments=config.instruments,
            max_quote_age_ms=config.max_quote_age_ms,
            max_spread_points=config.max_spread_points,
            gap_threshold_ms=config.gap_threshold_ms,
            min_window_ticks=config.min_window_ticks,
            tick_window=config.tick_window,
        )
        accepted = []
        for tick in scenario.ticks:
            result = core.ingest(
                symbol=tick.symbol,
                bid=tick.bid,
                ask=tick.ask,
                source_time=tick.at,
                received_at=tick.at,
                volume=tick.volume,
                source=SRC_REPLAY,
            )
            if result.accepted and result.tick is not None:
                accepted.append(result.tick)
        if len(accepted) < config.min_window_ticks + 5:
            continue
        from . import regime as regime_module

        step = 3
        for index in range(config.min_window_ticks, len(accepted) - 2, step):
            window = tuple(accepted[max(0, index - config.tick_window + 1) : index + 1])
            state = regime_module.classify(window, spec=spec)
            context = _context(window, spec, state, config)
            for strategy in strategies:
                evaluation = strategy.evaluate(context)
                if evaluation.signal is None:
                    continue
                signal = evaluation.signal
                stop = signal.stop_price
                if stop is None:
                    continue
                spread = accepted[index].spread_points(spec.point)
                spreads.append(float(spread))
                cost = _cost_points(config, spec, spread)
                outcome = _simulate_bracket(
                    signal=signal,
                    ticks=accepted[index + 1 :],
                    spec=spec,
                    max_hold_ms=hold,
                )
                if outcome is None:
                    continue
                gross, holding = outcome
                net = gross - cost
                book.record(fingerprint=strategy.fingerprint, regime=state.regime, won=net > 0)
                outcomes.append(
                    TrainingOutcome(
                        scenario=scenario.name,
                        fingerprint=strategy.fingerprint,
                        strategy=strategy.name,
                        regime=state.regime,
                        won=net > 0,
                        gross_points=float(gross),
                        cost_points=float(cost),
                        holding_ms=holding,
                    )
                )
    return {
        "book": book,
        "outcomes": outcomes,
        "cells": book.audit_view(),
        "spread_points_median": _median(spreads) if spreads else None,
        "data_class": SAMPLE,
        "note": (
            "Training outcomes are measured on the training replay only. They are used "
            "to calibrate probabilities, never to report performance."
        ),
    }


def _context(window: Sequence[Any], spec: Any, state: Any, config: ScalperConfig) -> Any:
    from .signals import SignalContext

    return SignalContext(
        ticks=tuple(window),
        spec=spec,
        regime=state,
        spread_limit_points=config.max_spread_points,
        max_quote_age_ms=config.max_quote_age_ms,
    )


def _simulate_bracket(
    *, signal: Signal, ticks: Sequence[Any], spec: Any, max_hold_ms: int
) -> tuple[Decimal, int] | None:
    stop = signal.stop_price
    target = signal.target_price
    if stop is None:
        return None
    point = spec.point
    for tick in ticks:
        if (tick.source_time - signal.at).total_seconds() * 1000 > max_hold_ms:
            price = tick.bid if signal.side == BUY else tick.ask
            return (price - signal.reference_price) / point * (1 if signal.side == BUY else -1), int(
                (tick.source_time - signal.at).total_seconds() * 1000
            )
        if signal.side == BUY:
            if tick.bid <= stop:
                return (min(tick.bid, stop) - signal.reference_price) / point, int(
                    (tick.source_time - signal.at).total_seconds() * 1000
                )
            if target is not None and tick.bid >= target:
                return (max(tick.bid, target) - signal.reference_price) / point, int(
                    (tick.source_time - signal.at).total_seconds() * 1000
                )
        else:
            if tick.ask >= stop:
                return (signal.reference_price - max(tick.ask, stop)) / point, int(
                    (tick.source_time - signal.at).total_seconds() * 1000
                )
            if target is not None and tick.ask <= target:
                return (signal.reference_price - min(tick.ask, target)) / point, int(
                    (tick.source_time - signal.at).total_seconds() * 1000
                )
    return None


def _cost_points(config: ScalperConfig, spec: Any, spread_points: Decimal) -> Decimal:
    from .ev import cost_for

    return cost_for(spec=spec, cost=config.cost, spread_points=spread_points).total_points


# ---------------------------------------------------------------------------
# running a scenario through the desk
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunResult:
    scenario: dict[str, Any]
    ticks: dict[str, Any]
    funnel: dict[str, Any]
    trades: tuple[skeptic.TradeRecord, ...]
    metrics: skeptic.Metrics
    distributions: dict[str, Any]
    by_regime: dict[str, Any]
    by_session: dict[str, Any]
    equity_curve: tuple[float, ...]
    quality: dict[str, Any]
    execution: dict[str, Any]
    ledger: dict[str, Any]
    refusals: dict[str, Any]
    limitations: tuple[str, ...] = ()

    def audit_view(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "ticks": self.ticks,
            "funnel": self.funnel,
            "metrics": self.metrics.audit_view(),
            "distributions": self.distributions,
            "by_regime": self.by_regime,
            "by_session": self.by_session,
            "equity_curve": list(self.equity_curve),
            "quality": self.quality,
            "execution": self.execution,
            "ledger": self.ledger,
            "refusals": self.refusals,
            "limitations": list(self.limitations),
            "claim": NO_PROFIT_CLAIM,
        }


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    middle = len(ordered) // 2
    return float(ordered[middle]) if len(ordered) % 2 else float((ordered[middle - 1] + ordered[middle]) / 2)


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return float(ordered[index])


def distribution(values: Sequence[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0, "p50": None, "p95": None, "max": None, "mean": None}
    return {
        "n": len(values),
        "mean": round(sum(values) / len(values), 4),
        "p50": _percentile(values, 0.5),
        "p95": _percentile(values, 0.95),
        "max": max(values),
        "min": min(values),
    }


def run_scenario(
    scenario: Scenario,
    *,
    config: ScalperConfig | None = None,
    strategies: Sequence[Strategy] | None = None,
    trained_book: bayes.OutcomeBook | None = None,
    starting_equity: Decimal = Decimal("10000"),
    thresholds: DeskThresholds | None = None,
    run_id: str = "",
) -> RunResult:
    """Replay one scenario through the whole desk and collect what it produced."""

    config = config or fixture_config()
    strategies = tuple(strategies or default_strategies())
    equity = _equity_from(config, starting_equity)
    guardian = Guardian(
        GuardianLimits.from_config(config),
        declared_slippage_points=config.cost.slippage_points,
    )
    venue = PaperVenue(cost=config.cost, faults=scenario.faults)
    probability_source = None
    if trained_book is not None:
        book = trained_book

        def probability_source(signal: Signal, regime: str) -> bayes.ProbabilityEstimate:  # type: ignore[misc]
            return book.estimate(signal.parameter_fingerprint, regime)

    desk = ScalperDesk(
        config=config,
        strategies=strategies,
        venue=venue,
        guardian=guardian,
        probability_source=probability_source,
        thresholds=thresholds,
        run_id=run_id or f"lab-{scenario.name}-{scenario.digest[:8]}",
    )
    equity_curve: list[float] = []
    realised = Decimal(0)
    for tick in scenario.ticks:
        equity = starting_equity + realised
        desk.on_tick(
            symbol=tick.symbol,
            bid=tick.bid,
            ask=tick.ask,
            source_time=tick.at,
            received_at=tick.at,
            volume=tick.volume,
            source=SRC_REPLAY,
            equity=equity,
            balance=equity,
            day_key=tick.at.strftime("%Y-%m-%d"),
        )
        rows = desk.trades
        if rows:
            realised = Decimal(str(sum(row["net_account"] or 0.0 for row in rows)))
        equity_curve.append(float(starting_equity + realised))

    # Close whatever survives the replay, at the last quote, so no trade is left
    # unrecorded and no unrealised number is quietly counted as a result.
    if desk.book.open_positions:
        last_at = scenario.ticks[-1].at if scenario.ticks else datetime.now(tz=UTC)
        desk.flatten_all(at_ms=int(last_at.timestamp() * 1000), reason="end_of_replay")

    status = desk.status()
    names = {strategy.fingerprint: strategy.name for strategy in strategies}
    trades = tuple(
        _to_trade_record(row, index, names) for index, row in enumerate(desk.trades)
    )
    metrics = skeptic.compute_metrics(trades)
    return RunResult(
        scenario=scenario.audit_view(),
        ticks={
            "accepted": status["ticks"].get("accepted", 0),
            "refused": status["ticks"].get("refused", 0),
            "refusals": status["tick_refusals"],
            "events": status["ledger"]["by_kind"].get("data_quality", 0),
        },
        funnel=status["stats"],
        trades=trades,
        metrics=metrics,
        distributions={
            "spread_points_at_entry": distribution(
                [float(row.get("spread_points_at_entry") or 0.0) for row in desk.trades]
            ),
            "cost_points": distribution([float(trade.cost_points) for trade in trades]),
            "slippage_points": distribution(
                [float(value) for value in desk._slippages]  # noqa: SLF001 - reported, not trusted
            ),
            "fill_latency_ms": distribution(
                [float(value) for value in desk._latencies]  # noqa: SLF001
            ),
            "holding_ms": distribution([float(trade.holding_ms) for trade in trades]),
            "mae_points": distribution([trade.mae_points for trade in trades]),
            "mfe_points": distribution([trade.mfe_points for trade in trades]),
            "r_multiple": distribution(
                [
                    float(row["r_multiple"])
                    for row in desk.trades
                    if row.get("r_multiple") is not None
                ]
            ),
        },
        by_regime=skeptic.breakdown(trades, "regime"),
        by_session=skeptic.breakdown(trades, "session"),
        equity_curve=tuple(equity_curve),
        quality=status["quality"],
        execution=status["trader"],
        ledger=status["ledger"],
        refusals={
            "ev": status["ev_rejections"],
            "risk": status["risk_refusals"],
            "tick": status["tick_refusals"],
        },
        limitations=(
            "synthetic ticks; no broker feed, no real spread distribution",
            "the paper venue fills every accepted order unless a fault is injected",
            "costs are the declared terms in the configuration, not a broker's schedule",
            "no live execution path is exercised or reachable",
        ),
    )


def _to_trade_record(
    row: Mapping[str, Any], index: int, names: Mapping[str, str] | None = None
) -> skeptic.TradeRecord:
    fingerprint = str(row.get("fingerprint", ""))
    return skeptic.TradeRecord(
        index=index,
        fingerprint=fingerprint,
        strategy=str((names or {}).get(fingerprint, fingerprint[:8])),
        regime=str(row.get("regime", "unknown")),
        session=str(row.get("session", "unknown")),
        net_points=float(row.get("net_points", 0.0)),
        gross_points=float(row.get("gross_points", 0.0)),
        cost_points=float(row.get("cost_points", 0.0)),
        holding_ms=int(row.get("holding_ms", 0)),
        mae_points=float(row.get("mae_points", 0.0)),
        mfe_points=float(row.get("mfe_points", 0.0)),
        side=str(row.get("side", "BUY")),
    )


def _equity_from(config: ScalperConfig, starting_equity: Decimal) -> Decimal:
    return starting_equity


# ---------------------------------------------------------------------------
# comparisons, degradation, and the report
# ---------------------------------------------------------------------------


def compare(results: Sequence[RunResult]) -> dict[str, Any]:
    rows = []
    for result in results:
        rows.append(
            {
                "scenario": result.scenario["name"],
                "ticks": result.ticks["accepted"],
                "signals": result.funnel.get("signals", 0),
                "ev_accept": result.funnel.get("ev_accept", 0),
                "trades": result.metrics.n,
                "mean_net_points": round(result.metrics.mean_net_points, 4),
                "profit_factor": None
                if result.metrics.profit_factor is None
                else round(result.metrics.profit_factor, 3),
                "win_rate": round(result.metrics.win_rate, 3),
                "max_drawdown_points": round(result.metrics.max_drawdown_points, 3),
            }
        )
    return {
        "runs": rows,
        "note": (
            "Runs are not ranked on profit. A run with no trades is a valid outcome: it "
            "means the gates refused everything, which is a result about the gates."
        ),
    }


def degradation_check(
    baseline: RunResult,
    forward: RunResult,
    *,
    tolerance_points: float = 0.5,
) -> dict[str, Any]:
    """Did the paper-forward window behave differently from the validated one?"""

    def mean(result: RunResult) -> float:
        return result.metrics.mean_net_points

    def standard_error(result: RunResult) -> float:
        if result.metrics.n < 2:
            return float("inf")
        return result.metrics.sd_net_points / math.sqrt(result.metrics.n)

    delta = mean(forward) - mean(baseline)
    combined = math.sqrt(standard_error(baseline) ** 2 + standard_error(forward) ** 2)
    significant = abs(delta) > 2 * combined if combined not in (0.0, float("inf")) else False
    degraded = significant and delta < -tolerance_points
    return {
        "baseline_mean": round(mean(baseline), 4),
        "forward_mean": round(mean(forward), 4),
        "delta": round(delta, 4),
        "standard_error": None if combined == float("inf") else round(combined, 4),
        "significant": significant,
        "degraded": degraded,
        "verdict": "degraded" if degraded else ("changed" if significant else "no_detectable_change"),
        "note": (
            "A drop is only called degradation when it exceeds twice the combined "
            "standard error; otherwise the honest answer is 'not distinguishable'."
        ),
    }


def validation_for(result: RunResult, *, criteria: skeptic.Criteria | None = None) -> skeptic.ValidationReport:
    """Run the skeptic over one run's trades.  Expect ``approved=False``."""

    return skeptic.validate(result.trades, criteria=criteria)


def write_report(
    path: str | Path,
    *,
    runs: Sequence[RunResult],
    training: dict[str, Any] | None = None,
    validations: Sequence[skeptic.ValidationReport] = (),
    extra_notes: Sequence[str] = (),
    degradation: Sequence[Mapping[str, Any]] = (),
    verification: Mapping[str, Sequence[str]] | None = None,
) -> Path:
    """Write a reproducible markdown report.  Every table here is machine-generated."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    add = lines.append
    add("# AX-SCALPER lab report")
    add("")
    add(f"Generated: {datetime.now(tz=UTC).isoformat()}")
    add("")
    add(f"> {NO_PROFIT_CLAIM}")
    add("")
    add(
        ">  Read the funnel and the refusals before the expectancy: the scenarios are "
        "deterministic synthetic paths, and a persistent drift is the most favourable "
        "possible world for a momentum rule. A large expectancy here measures the "
        "generator as much as the strategy."
    )
    add("")
    add("## What was run")
    add("")
    add("| Scenario | Data class | Ticks | Injected | Digest |")
    add("| --- | --- | --- | --- | --- |")
    for result in runs:
        scenario = result.scenario
        add(
            f"| {scenario['name']} | {scenario['data_class']} | {scenario['ticks']} | "
            f"{', '.join(scenario['injected']) or '—'} | `{scenario['digest']}` |"
        )
    add("")
    if training is not None:
        add("## Training pass")
        add("")
        add(
            f"{len(training.get('outcomes', []))} simulated brackets over the training "
            f"replay; median spread {training.get('spread_points_median')} points."
        )
        add("")
        add(f"> {training.get('note', '')}")
        add("")
        cells = training.get("cells", {}).get("cells", {})
        if cells:
            add("| Fingerprint / regime | Wins | Losses |")
            add("| --- | --- | --- |")
            for key, value in sorted(cells.items()):
                add(f"| `{key}` | {value['wins']} | {value['losses']} |")
            add("")
    add("## The funnel")
    add("")
    add("| Scenario | Ticks | Opportunities | Signals | EV accept | EV reject | Sized | Fills | Exits |")
    add("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for result in runs:
        funnel = result.funnel
        add(
            f"| {result.scenario['name']} | {result.ticks['accepted']} | "
            f"{funnel.get('opportunities', 0)} | {funnel.get('signals', 0)} | "
            f"{funnel.get('ev_accept', 0)} | {funnel.get('ev_reject', 0)} | "
            f"{funnel.get('sized', 0)} | "
            f"{funnel.get('fills', 0)} | {funnel.get('exits', 0)} |"
        )
    add("")
    add("## Performance, after costs")
    add("")
    add(
        "| Scenario | Trades | Net expectancy | Std dev | Profit factor | Win rate | "
        "Avg win | Avg loss | Expectancy R | Sharpe | Sortino | Max DD (points) | "
        "Cost / gross |"
    )
    add("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for result in runs:
        metrics = result.metrics.audit_view()
        add(
            f"| {result.scenario['name']} | {metrics['n']} | {metrics['mean_net_points']} | "
            f"{metrics['sd_net_points']} | {metrics['profit_factor']} | {metrics['win_rate']} | "
            f"{metrics['mean_win']} | {metrics['mean_loss']} | {metrics['expectancy_r']} | "
            f"{metrics['sharpe']} | {metrics['sortino']} | {metrics['max_drawdown_points']} | "
            f"{metrics['cost_to_gross']} |"
        )
    add("")
    add(
        "Sharpe and Sortino here are per-trade mean divided by per-trade dispersion, "
        "not annualised. Max drawdown is in points, not in account currency, and it is "
        "measured on the trade sequence, not on an intra-trade equity curve."
    )
    add("")
    add("## Costs, spreads, latency and holding time")
    add("")
    for result in runs:
        distributions = result.distributions
        add(f"### {result.scenario['name']}")
        add("")
        add("| Measure | n | mean | p50 | p95 | max |")
        add("| --- | --- | --- | --- | --- | --- |")
        for name, values in distributions.items():
            if not isinstance(values, dict) or values.get("n") in (0, None):
                continue
            add(
                f"| {name} | {values['n']} | {values['mean']} | {values['p50']} | "
                f"{values['p95']} | {values['max']} |"
            )
        add("")
    add("## By regime and by session")
    add("")
    for result in runs:
        add(f"### {result.scenario['name']}")
        add("")
        for name, table in (("regime", result.by_regime), ("session", result.by_session)):
            if not table:
                continue
            add(f"**{name}**")
            add("")
            add("| Bucket | n | Mean net | Win rate | Profit factor |")
            add("| --- | --- | --- | --- | --- |")
            for bucket, metrics in table.items():
                add(
                    f"| {bucket} | {metrics['n']} | {metrics['mean_net_points']} | "
                    f"{metrics['win_rate']} | {metrics['profit_factor']} |"
                )
            add("")
    if validations:
        add("## Independent validation")
        add("")
        add(
            "| Strategy | Trades | OOS trades | PBO | Deflated Sharpe | Cost break | Approved | Findings |"
        )
        add("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for report in validations:
            add(
                f"| {report.strategy or report.fingerprint[:12]} | {report.metrics.n} | "
                f"{0 if report.out_of_sample is None else report.out_of_sample.n} | "
                f"{report.pbo.get('pbo')} | {report.deflated_sharpe.get('deflated_sharpe')} | "
                f"{report.cost_sensitivity.get('breaks_between')} | "
                f"{'yes' if report.approved else 'no'} | {'; '.join(report.findings) or '—'} |"
            )
        add("")
        for report in validations:
            add(f"### {report.strategy or report.fingerprint[:12]}")
            add("")
            add(f"- Confidence interval on mean net points: {report.confidence_interval}")
            add(f"- Walk-forward: {report.walk_forward.get('verdict')} "
                f"({report.walk_forward.get('consistent_folds')}/{report.walk_forward.get('total_folds')} consistent)")
            add(f"- Cost sensitivity: {report.cost_sensitivity.get('rows')}")
            add(f"- Spread stress: {report.spread_stress.get('rows')}")
            add(f"- Criteria: {report.criteria.audit_view()}")
            add("")
    if degradation:
        add("## Paper-forward comparison and degradation")
        add("")
        add(
            "The evaluation window is compared with a forward *replay* of the same "
            "price path at **twice the spread**. That isolates a cost regime change "
            "from a change in the price path. It is not a live paper-forward session: "
            "no real-time run has been made in this sandbox."
        )
        add("")
        add("| Scenario | Baseline mean | Forward mean | Delta | Std error | Significant | Verdict |")
        add("| --- | --- | --- | --- | --- | --- | --- |")
        for row in degradation:
            add(
                f"| {row.get('scenario')} | {row.get('baseline_mean')} | {row.get('forward_mean')} | "
                f"{row.get('delta')} | {row.get('standard_error')} | {row.get('significant')} | "
                f"{row.get('verdict')} |"
            )
        add("")
        add(
            "> A drop is only called degradation when it exceeds twice the combined "
            "standard error. With samples this small, `no_detectable_change` is not "
            "evidence of stability; it is the absence of evidence either way."
        )
        add("")
    if verification:
        add("## Verified here, unverified elsewhere")
        add("")
        for heading, key in (("Verified in this sandbox (fakes, replay, unit tests)", "verified"),
                             ("Not verified here", "unverified")):
            add(f"**{heading}**")
            add("")
            for item in verification.get(key, ()):
                add(f"- {item}")
            add("")
    add("## Ledger")
    add("")
    for result in runs:
        integrity = result.ledger.get("integrity", {})
        add(
            f"- **{result.scenario['name']}**: {result.ledger.get('entries', 0)} entries, "
            f"chain ok={integrity.get('chain', {}).get('ok')}, "
            f"order requests without a result={len(integrity.get('order_requests_without_result', []))}"
        )
    add("")
    add("## Limitations")
    add("")
    seen: set[str] = set()
    for result in runs:
        for item in result.limitations:
            if item not in seen:
                seen.add(item)
                add(f"- {item}")
    for note in extra_notes:
        add(f"- {note}")
    add("")
    add(
        "No claim of profitability, live readiness, or market-wide behaviour is made "
        "anywhere in this report. Figures are reproducible from the scenario digests "
        "above and the configuration in `axiom_forex/scalper/config.py`."
    )
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


__all__ = [
    "COST_MULTIPLE_FOR_TARGET",
    "NO_PROFIT_CLAIM",
    "RunResult",
    "SAMPLE",
    "Scenario",
    "breakout",
    "collect_outcomes",
    "compare",
    "cost_compliant_strategies",
    "degradation_check",
    "dirty_feed",
    "distribution",
    "intermittent",
    "ranging",
    "reversal",
    "repriced",
    "run_scenario",
    "shifted",
    "spread_expansion",
    "standard_scenarios",
    "trending",
    "validate",
    "validation_for",
    "volatile",
    "write_report",
]

#: Convenience alias so ``lab.validate`` reads naturally in a report script.
validate = validation_for
