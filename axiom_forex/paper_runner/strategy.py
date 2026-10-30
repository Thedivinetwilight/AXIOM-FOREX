"""The candidate strategy the paper runner evaluates, and its baselines.

Everything here is deliberately small, stateless, and readable.  A strategy gets
one input -- the prefix of *completed* bars that ended at or before the decision
instant -- and returns one of ``BUY`` / ``SELL`` / ``EXIT`` / ``HOLD``.  It cannot
see the future because it is never given it, and it holds no memory, so a
restarted process reaches the same decision from the same bars.

``TrendPullbackV1`` (the candidate)
-----------------------------------
A trend-following pullback on 1-minute bars, long and short:

* trend: ``sma(close, 8)`` above ``sma(close, 21)`` means an up-trend;
* pullback: the latest *completed* close is **below** the fast average while
  still **above** the slow average -- a dip inside an up-trend;
* the order is only proposed on the bar where that condition first becomes true,
  so a signal is an event, not a standing state;
* the position is closed on the bar where the trend flips (``EXIT``), which is the
  same down-cross rule the repository's SMA-cross example uses;
* the short side is the exact mirror.

It is a classic, well-known shape, chosen because it is easy to audit rather than
because it is expected to be profitable.  It has two parameters and no fitting
beyond a small, fixed grid, and it is *rejected* by default: the runner will only
use it once the recorded evaluation pass has promoted it, and the evaluation
refuses to promote anything on synthetic or demo data.

``SmaCrossBaseline``
    The repository's existing ``SmaCrossStrategy`` at a fixed 8/21 setting.  A
    different, simpler rule used as a comparison point.
``HoldBaseline``
    Never trades.  Any strategy that cannot beat a zero-trade book after costs
    has no edge worth running, so this is the floor every comparison starts at.

Nothing in this module may claim an edge: a strategy is a *hypothesis* until the
chronological evaluation says otherwise, and even then the result is evidence
about the past.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Sequence

from app.data.models import ForexQuoteBar

from axiom_forex.indicators import mid_closes, sma
from axiom_forex.strategies.base import Signal, Strategy

#: Lifecycle of a candidate in this runner.  Only ``SELECTED`` may run.
STATUS_CANDIDATE = "CANDIDATE_UNTESTED"
STATUS_REJECTED = "REJECTED"
STATUS_INSUFFICIENT = "INSUFFICIENT_EVIDENCE"
STATUS_SELECTED = "SELECTED_FOR_PAPER"

#: Data classes whose evaluation may ever promote a candidate.  A synthetic
#: fixture or a demo export can be studied, but it can never be evidence about a
#: broker's market, so it can never select a strategy for paper running.
PROMOTING_DATA_CLASSES = frozenset(
    {"USER_SUPPLIED_HISTORICAL", "USER_SUPPLIED_PRODUCTION"}
)


class TrendPullbackV1(Strategy):
    """Trend filter plus pullback entry; see the module docstring for the rules."""

    def __init__(self, *, fast_period: int = 8, slow_period: int = 21) -> None:
        if fast_period <= 0 or slow_period <= 0 or fast_period >= slow_period:
            raise ValueError("Require 0 < fast_period < slow_period")
        self.fast_period = int(fast_period)
        self.slow_period = int(slow_period)
        self.name = f"trend_pullback_v1_{fast_period}_{slow_period}"
        #: Two extra bars: the signal needs the *previous* bar's verdict too.
        self.history_limit = self.slow_period + 2

    # -- rules -----------------------------------------------------------

    def _state(self, closes: Sequence[Decimal]) -> str:
        """'UP', 'DOWN' or 'FLAT' from completed closes only."""

        fast = sma(closes, self.fast_period)
        slow = sma(closes, self.slow_period)
        if fast is None or slow is None:
            return "FLAT"
        if fast > slow:
            return "UP"
        if fast < slow:
            return "DOWN"
        return "FLAT"

    def _pullback(self, closes: Sequence[Decimal], trend: str) -> bool:
        fast = sma(closes, self.fast_period)
        slow = sma(closes, self.slow_period)
        if fast is None or slow is None:
            return False
        close = closes[-1]
        if trend == "UP":
            return close < fast and close > slow
        if trend == "DOWN":
            return close > fast and close < slow
        return False

    def generate_signal(self, bars: Sequence[ForexQuoteBar]) -> Signal:
        if len(bars) < self.history_limit:
            return Signal.HOLD
        closes = mid_closes(bars)
        trend = self._state(closes)
        if trend == "FLAT":
            return Signal.HOLD
        previous = self._state(closes[:-1])
        # A trend that has just flipped is an exit; the planner ignores an EXIT
        # when the book is already flat, so this is safe to emit unconditionally.
        if previous != "FLAT" and previous != trend:
            return Signal.EXIT
        # Edge trigger: the pullback must not already have been true one bar ago.
        if self._pullback(closes, trend) and not self._pullback(closes[:-1], trend):
            return Signal.BUY if trend == "UP" else Signal.SELL
        return Signal.HOLD

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.name,
            "family": "trend_pullback",
            "fast_period": self.fast_period,
            "slow_period": self.slow_period,
            "history_limit": self.history_limit,
        }


class HoldBaseline(Strategy):
    """Never trades: the zero-trade floor every comparison starts from."""

    name = "baseline_hold"
    history_limit = 1

    def generate_signal(self, bars: Sequence[ForexQuoteBar]) -> Signal:
        return Signal.HOLD


@dataclass(frozen=True)
class Candidate:
    """A named hypothesis plus everything needed to reproduce it."""

    candidate_id: str
    strategy_factory: Any
    parameters: dict[str, Any]
    rationale: str
    assumptions: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    status: str = STATUS_CANDIDATE
    evidence: dict[str, Any] = field(default_factory=dict)

    def build(self) -> Strategy:
        return self.strategy_factory(**self.parameters)

    def audit_view(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "parameters": dict(self.parameters),
            "rationale": self.rationale,
            "assumptions": list(self.assumptions),
            "limitations": list(self.limitations),
            "status": self.status,
            "evidence": dict(self.evidence),
        }


#: The fixed, predeclared parameter grid.  Small on purpose: a wide grid searched
#: on the same data is how backtests stop being evidence.
CANDIDATE_GRID: tuple[tuple[int, int], ...] = ((5, 13), (8, 21), (13, 34))

def _make_sma_cross(*, fast_period: int, slow_period: int) -> Strategy:
    from axiom_forex.strategies.examples import SmaCrossStrategy

    return SmaCrossStrategy(
        fast_period=fast_period,
        slow_period=slow_period,
        name=f"baseline_sma_cross_{fast_period}_{slow_period}",
    )


BASELINES: tuple[Candidate, ...] = (
    Candidate(
        candidate_id="baseline_hold",
        strategy_factory=lambda **_: HoldBaseline(),
        parameters={},
        rationale="Zero-trade floor: no position, no cost, no signal.",
        limitations=("Cannot make money; it exists only as a comparison point.",),
    ),
    Candidate(
        candidate_id="baseline_sma_cross_8_21",
        strategy_factory=_make_sma_cross,
        parameters={"fast_period": 8, "slow_period": 21},
        rationale="The repository's existing SMA-cross example at a fixed setting.",
        limitations=("Whipsaws in ranging markets; one parameter pair only.",),
    ),
)


def candidate_family() -> tuple[Candidate, ...]:
    """Every candidate in the predeclared grid, all starting untested."""

    items: list[Candidate] = []
    for fast, slow in CANDIDATE_GRID:
        items.append(
            Candidate(
                candidate_id=f"trend_pullback_v1_{fast}_{slow}",
                strategy_factory=TrendPullbackV1,
                parameters={"fast_period": fast, "slow_period": slow},
                rationale=(
                    "Trade with the 1-minute trend only on a pullback towards it, so "
                    "the entry is at a better price than chasing the break."
                ),
                assumptions=(
                    "Fills happen at the next available bid/ask after the decision bar "
                    "closes, never at the decision bar's own price.",
                    "Costs are the configured spread (from the bid/ask data), "
                    "commission, slippage and swap -- see the run's assumption block.",
                    "A missing bar is a gap: the runner refuses to trade across it "
                    "rather than interpolating.",
                ),
                limitations=(
                    "Trend-following loses in ranging markets and around session "
                    "breaks; the pullback filter reduces but does not remove that.",
                    "No volatility targeting, no position pyramiding, one position "
                    "per instrument.",
                    "Evaluated on 1-minute bars only; behaviour on other timeframes is "
                    "untested.",
                ),
            )
        )
    return tuple(items)


def status_of(candidate_id: str, evidence: dict[str, Any] | None = None) -> str:
    """Derive the status from the evidence, never from an assumption.

    A candidate is ``SELECTED_FOR_PAPER`` only when the recorded evaluation says
    every predeclared criterion passed on a promoting data class.  Anything else
    is ``REJECTED`` or ``INSUFFICIENT_EVIDENCE``, and the runner refuses it.
    """

    payload = dict(evidence or {})
    if not payload:
        return STATUS_CANDIDATE
    if not payload.get("evaluated", False):
        return STATUS_CANDIDATE
    if payload.get("data_class") not in PROMOTING_DATA_CLASSES:
        return STATUS_INSUFFICIENT
    failed = payload.get("failed_criteria") or []
    if failed:
        return STATUS_REJECTED
    if payload.get("passed") is True:
        return STATUS_SELECTED
    return STATUS_INSUFFICIENT


__all__ = [
    "BASELINES",
    "CANDIDATE_GRID",
    "Candidate",
    "HoldBaseline",
    "PROMOTING_DATA_CLASSES",
    "STATUS_CANDIDATE",
    "STATUS_INSUFFICIENT",
    "STATUS_REJECTED",
    "STATUS_SELECTED",
    "TrendPullbackV1",
    "candidate_family",
    "status_of",
]
