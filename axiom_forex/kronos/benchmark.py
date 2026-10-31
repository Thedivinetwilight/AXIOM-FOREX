"""Does the forecast beat anything simpler, on this broker's bars?

This is a *forecast* comparison, not a strategy backtest and not a profit claim.
For one pair and one timeframe it takes the chronological out-of-sample window
(the last 20% of the bars, never used to choose or tune anything -- nothing here is
tuned at all), walks it forward in non-overlapping steps, and for each step:

1. builds the exact input :func:`axiom_forex.kronos.inputs.build_input` would build
   from the bars that had closed at that instant;
2. asks Kronos for a forecast, and asks each baseline for its forecast of the same
   horizon;
3. when the horizon elapses, compares every forecast to the close that actually
   printed.

Baselines are deliberately dumb, because a dumb model that wins is a finding:

``hold``
    The price does not move.  Any model that cannot beat this has no information in
    its trend term at all.
``drift``
    Extend the last 20 bars' average change per bar in a straight line.
``momentum``
    The repository's SMA-cross direction (8/21), carried at the recent average
    absolute bar move.

Two figures are reported, and they answer different questions:

* **Forecast error and direction** -- mean absolute error of the forecast close,
  and how often the sign of the forecast move matched the sign of the realised
  move.  Free of any trading assumption.
* **Spread-adjusted directional score** -- the price change a one-unit position
  would have captured going with the forecast sign and holding to the horizon,
  *minus the bid/ask spread actually quoted at entry and exit*.  It is computed in
  price terms, one unit, no sizing, no stop, no swap.  It is therefore not a profit
  figure: it isolates whether the forecast's direction survives the broker's own
  spread, which is the cheapest question to ask first.

Nothing in this module ranks candidates for live use, and no score here can
promote anything.  The only way a forecast influences trading is an operator
selecting it explicitly, and then the existing risk gate still applies.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence

from axiom_forex.evaluation.contracts import PHASE_TEST
from axiom_forex.evaluation.splits import chronological_plan

from .forecast import FORECAST_LABEL
from .inputs import KronosInput, build_input

#: Same chronology as the paper lane; only the test phase is scored here.
SPLIT = (Decimal("0.6"), Decimal("0.2"), Decimal("0.2"))

#: Steps are non-overlapping by default: two overlapping outcomes are not two
#: independent observations, and counting them as such inflates every ratio.
DEFAULT_STRIDE_MULTIPLE = 1

BENCHMARK_NOTE = (
    "Forecast accuracy and spread-adjusted direction only, on mid prices from this "
    "feed. No sizing, stops, swap or commission, so this is not a profit figure and "
    "must never be shown as one."
)


class Model(Protocol):
    """Anything that predicts the next ``horizon`` mid closes."""

    key: str
    label: str

    def predict(self, data: KronosInput, horizon: int) -> Sequence[Decimal]: ...


# ---------------------------------------------------------------------------
# baselines
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HoldModel:
    """No move at all."""

    key: str = "hold"
    label: str = "Hold (no move)"
    note: str = "The zero-information floor. Beating it is the minimum bar."

    def predict(self, data: KronosInput, horizon: int) -> Sequence[Decimal]:
        return tuple(data.last_close for _ in range(horizon))


@dataclass(frozen=True)
class DriftModel:
    """Straight-line extrapolation of the recent average change per bar."""

    window: int = 20
    key: str = "drift"
    label: str = "Drift (linear extrapolation)"
    note: str = "Recent average change per bar, carried forward in a straight line."

    def predict(self, data: KronosInput, horizon: int) -> Sequence[Decimal]:
        closes = [Decimal(str(row["close"])) for row in data.rows]
        span = min(self.window, len(closes) - 1)
        if span < 1:
            last = data.last_close
            return tuple(last for _ in range(horizon))
        slope = (closes[-1] - closes[-1 - span]) / Decimal(span)
        return tuple(closes[-1] + slope * Decimal(step + 1) for step in range(horizon))


@dataclass(frozen=True)
class MomentumModel:
    """SMA-cross direction, carried at the recent average absolute bar move."""

    fast: int = 8
    slow: int = 21
    key: str = "momentum"
    label: str = "Momentum (SMA 8/21 direction)"
    note: str = "The repository's existing SMA-cross direction as a forecast."

    def predict(self, data: KronosInput, horizon: int) -> Sequence[Decimal]:
        closes = [Decimal(str(row["close"])) for row in data.rows]
        last = closes[-1]
        if len(closes) <= self.slow:
            return tuple(last for _ in range(horizon))
        fast = sum(closes[-self.fast :]) / Decimal(self.fast)
        slow = sum(closes[-self.slow :]) / Decimal(self.slow)
        moves = [abs(second - first) for first, second in zip(closes[-21:-1], closes[-20:])]
        step = (sum(moves) / Decimal(len(moves))) if moves else Decimal(0)
        direction = Decimal(1) if fast > slow else (Decimal(-1) if fast < slow else Decimal(0))
        return tuple(last + direction * step * Decimal(index + 1) for index in range(horizon))


def standard_baselines() -> tuple[Model, ...]:
    return (HoldModel(), DriftModel(), MomentumModel())


@dataclass
class KronosModel:
    """Kronos behind the :class:`Model` protocol, using the median of rollouts."""

    forecaster: Any
    key: str = "kronos"
    label: str = "Kronos (median of rollouts)"
    note: str = "The configured Kronos variant and settings, unchanged."
    #: The last forecast's band, kept so coverage is scored from the same call.
    last_band: Any = None

    def predict(self, data: KronosInput, horizon: int) -> Sequence[Decimal]:
        result = self.forecaster.forecast(data)
        self.last_band = result.band
        return tuple(result.band.p50[:horizon])


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StepOutcome:
    at: str
    horizon_end: str
    realised: Decimal
    forecasts: Mapping[str, Decimal]
    band_low: Decimal | None = None
    band_high: Decimal | None = None
    entry_cost: Decimal = Decimal(0)
    exit_cost: Decimal = Decimal(0)


@dataclass
class ModelScore:
    key: str
    label: str
    note: str
    observations: int = 0
    direction_correct: int = 0
    absolute_errors: list[Decimal] = field(default_factory=list)
    band_covered: int = 0
    band_reported: int = 0
    spread_adjusted: list[Decimal] = field(default_factory=list)

    def audit_view(self) -> dict[str, Any]:
        observations = self.observations
        mean_error = (
            sum(self.absolute_errors) / Decimal(len(self.absolute_errors))
            if self.absolute_errors
            else Decimal(0)
        )
        mean_spread = (
            sum(self.spread_adjusted) / Decimal(len(self.spread_adjusted))
            if self.spread_adjusted
            else Decimal(0)
        )
        return {
            "key": self.key,
            "label": self.label,
            "note": self.note,
            "observations": observations,
            "direction_correct": self.direction_correct,
            "direction_rate": (
                round(self.direction_correct / observations, 4) if observations else None
            ),
            "mean_absolute_error": str(mean_error.quantize(Decimal("0.000001")))
            if self.absolute_errors
            else "",
            "band_covered": self.band_covered,
            "band_reported": self.band_reported,
            "band_coverage": (
                round(self.band_covered / self.band_reported, 4) if self.band_reported else None
            ),
            "mean_spread_adjusted_move": (
                str(mean_spread.quantize(Decimal("0.000001"))) if self.spread_adjusted else ""
            ),
        }


@dataclass
class BenchmarkResult:
    symbol: str
    timeframe: str
    bars: int
    steps: int
    first_at: str
    last_at: str
    scores: list[ModelScore]
    notes: tuple[str, ...] = ()

    def audit_view(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "bars": self.bars,
            "steps": self.steps,
            "first_at": self.first_at,
            "last_at": self.last_at,
            "scores": [score.audit_view() for score in self.scores],
            "notes": list(self.notes),
            "label": FORECAST_LABEL,
        }

    def table(self) -> list[dict[str, Any]]:
        """One row per model, ordered by key so nothing is implicitly ranked first."""

        return sorted((score.audit_view() for score in self.scores), key=lambda row: row["key"])


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _mid(bar: Any, point: str) -> Decimal:
    bid, _ = bar.bid.parsed()
    ask, _ = bar.ask.parsed()
    return (bid[point] + ask[point]) / Decimal(2)


def _spread(bar: Any, point: str) -> Decimal:
    bid, _ = bar.bid.parsed()
    ask, _ = bar.ask.parsed()
    return ask[point] - bid[point]


def evaluate(
    bars: Sequence[Any],
    *,
    symbol: str,
    timeframe: str,
    bar_seconds: int,
    lookback: int,
    horizon: int,
    models: Sequence[Model],
    stride: int | None = None,
    max_steps: int | None = None,
    fractions: Sequence[Decimal] = SPLIT,
) -> BenchmarkResult:
    """Score every model on the chronological out-of-sample window.

    A step whose horizon runs past the last bar is not scored: an outcome that
    needs bars from the future is not an outcome yet.
    """

    ordered = list(bars)
    if len(ordered) < lookback + horizon + 2:
        raise ValueError("insufficient_bars_for_benchmark")
    timeline = [_utc(bar.timestamp) for bar in ordered]
    plan = chronological_plan(timeline, fractions=fractions, grid_seconds=bar_seconds)
    test = plan.phases[PHASE_TEST]
    start = 0
    for index, stamp in enumerate(timeline):
        if stamp >= test.start:
            start = index
            break
    start = max(start, lookback)
    step = max(1, int(stride or horizon * DEFAULT_STRIDE_MULTIPLE))

    scores = {model.key: ModelScore(key=model.key, label=model.label, note=model.note) for model in models}
    steps = 0
    first_at = ""
    last_at = ""

    index = start
    while index + horizon <= len(ordered) - 1:
        if max_steps is not None and steps >= max_steps:
            break
        window = ordered[index - lookback + 1 : index + 1]
        entry_bar = ordered[index]
        exit_bar = ordered[index + horizon]
        try:
            data = build_input(
                window,
                symbol=symbol,
                timeframe=timeframe,
                lookback=lookback,
                horizon=horizon,
                bar_seconds=bar_seconds,
                now=timeline[index],
            )
            predictions = {model.key: model.predict(data, horizon) for model in models}
        except Exception as exc:
            # A refused window is a missing observation, recorded as such; it is
            # never filled in with a guess.
            index += step
            if getattr(exc, "reason", ""):
                continue
            raise
        realised = _mid(exit_bar, "close")
        last_close = data.last_close
        entry_cost = _spread(entry_bar, "close")
        exit_cost = _spread(exit_bar, "close")
        steps += 1
        first_at = first_at or timeline[index].isoformat()
        last_at = timeline[index + horizon].isoformat()
        realised_move = realised - last_close

        for model in models:
            values = predictions[model.key]
            if not values:
                continue
            score = scores[model.key]
            forecast = Decimal(str(values[-1]))
            move = forecast - last_close
            score.observations += 1
            if (move >= 0) == (realised_move >= 0):
                score.direction_correct += 1
            score.absolute_errors.append(abs(forecast - realised))
            band = getattr(model, "last_band", None)
            if band is not None and band.p10 and band.p90:
                # Coverage is from the same forecast call, so it is not a second,
                # independent estimate.
                score.band_reported += 1
                if band.p10[-1] <= realised <= band.p90[-1]:
                    score.band_covered += 1
            direction = Decimal(1) if move > 0 else (Decimal(-1) if move < 0 else Decimal(0))
            if direction != 0:
                gross = realised_move * direction
                score.spread_adjusted.append(gross - entry_cost - exit_cost)

        index += step

    notes = [
        BENCHMARK_NOTE,
        f"Scored only the chronological test window ({fractions[-1]:.0%} of the "
        f"{len(ordered)} bars); no parameter was chosen on it, because nothing here "
        "is fitted at all.",
        f"{steps} step(s) with a horizon that had elapsed; steps are "
        f"{'non-overlapping' if step >= horizon else 'overlapping (stride < horizon)'}.",
        "A refused input window is skipped, never repaired. The count of skipped "
        "windows is not hidden by a fill-in.",
    ]
    return BenchmarkResult(
        symbol=symbol,
        timeframe=timeframe,
        bars=len(ordered),
        steps=steps,
        first_at=first_at,
        last_at=last_at,
        scores=list(scores.values()),
        notes=tuple(notes),
    )


def compare_table(results: Iterable[BenchmarkResult]) -> list[dict[str, Any]]:
    """A flat table of every pair, timeframe and model, for the page."""

    rows: list[dict[str, Any]] = []
    for result in results:
        for score in result.table():
            rows.append(
                {
                    "symbol": result.symbol,
                    "timeframe": result.timeframe,
                    "steps": result.steps,
                    **score,
                }
            )
    return rows


__all__ = [
    "BENCHMARK_NOTE",
    "BenchmarkResult",
    "DriftModel",
    "FORECAST_LABEL",
    "HoldModel",
    "KronosModel",
    "Model",
    "ModelScore",
    "MomentumModel",
    "SPLIT",
    "StepOutcome",
    "compare_table",
    "evaluate",
    "standard_baselines",
]
