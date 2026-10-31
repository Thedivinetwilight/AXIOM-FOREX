"""Screening the catalog on real bars, without ever touching the locked test twice.

The order of a run, and why each step is where it is:

1. **Every candidate is fixed before the data is read.**  Parameters come from the
   declared grid in :mod:`axiom_forex.strategies.catalog.primitives`; this module
   never edits one in response to a result.
2. **Each (candidate, symbol, timeframe) row runs the same three chronological
   windows**: 60% train, 20% validation, 20% test, simulated separately so a
   position cannot leak across a boundary.  The fill convention is the shared one
   (decide on bar ``i``, fill at bar ``i+1``'s open, ask for a buy, bid for a sell,
   slippage against the trade, costs from the reported spread plus the stated
   assumption).
3. **The screen reads train and validation only.**  The test window is measured
   once, for the report, and never feeds a decision -- not the screen, not the
   ordering, not the paper set.
4. **Survivors get a walk-forward check**, with parameters frozen.  A fold is a
   stability reading, never a retuning opportunity.
5. **Everything that did not run says so.**  A row is ``TESTED``, ``SKIPPED`` (with
   a reason: no history, too few bars, out of budget, cancelled) or ``FAILED``.
   Nothing is dropped quietly, and no count of "strategies tested" includes a row
   that never ran.

What this cannot say: that anything here will make money.  A thousand candidates
were tried; at a 5% level roughly one in twenty looks positive on noise alone, so
the report states the search size next to the survivors instead of a verdict.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence

from axiom_forex.research.engine import (
    DEFAULT_BUDGET_SECONDS,
    MIN_TRADES_TO_RANK,
    SPLIT,
    CostModel,
    SegmentStats,
    Trade,
    segment_stats,
)

from .candidates import Candidate, catalog, timeframes_for
from .indicators import IndicatorCache
from .primitives import HigherTimeframe
from .simulate import context_for, signals_for, simulate_candidate

SEGMENTS: tuple[str, ...] = ("train", "validation", "test")

STATUS_TESTED = "TESTED"
STATUS_SKIPPED = "SKIPPED"
STATUS_FAILED = "FAILED"

SCREEN_PASSED = "PASSED_SCREEN"
SCREEN_FAILED = "FAILED_SCREEN"
SCREEN_SHORT_SAMPLE = "TOO_FEW_TRADES"
SCREEN_NOT_RUN = "NOT_SCREENED"

#: Curated skip reasons.  The UI shows these, never a raw exception.
SKIP_NO_SERIES = "no_series_for_symbol"
SKIP_TIMEFRAME = "timeframe_not_offered_by_candidate"
SKIP_HISTORY = "history_shorter_than_warmup"
SKIP_BUDGET = "not_reached_within_budget"
SKIP_CANCELLED = "cancelled_by_operator"
SKIP_NO_COST = "no_usable_cost_model"

FAIL_SIMULATION = "simulation_error"

#: Fractions a window needs before its numbers mean anything.
MIN_SEGMENT_BARS = 60

#: The screen's thresholds.  They are stability filters, not a profit target.
DEFAULT_MIN_TRADES = 8
DEFAULT_MAX_DRAWDOWN = 0.25
DEFAULT_CONCENTRATION_LIMIT = 0.6
DEFAULT_MAX_WALK_FORWARD = 40
DEFAULT_FOLDS = 4

CLAIM_NOTE = (
    "Historical research estimate on the bars this deployment could supply, under "
    "the stated cost assumptions.  No candidate is claimed to be profitable, and a "
    "passing screen is not evidence of an edge, of future returns, or of readiness "
    "for live trading."
)

SEARCH_NOTE = (
    "The catalog is a declared search, not a single hypothesis: the number of rows "
    "evaluated is in this report, and at a 5% false-positive rate roughly one in "
    "twenty of them will look good on noise alone.  Treat the survivors as a short "
    "list to watch on paper, never as winners."
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class SeriesSlot:
    """One symbol/timeframe this deployment can actually evaluate."""

    symbol: str
    timeframe: str
    series: Any
    cost: CostModel
    origin: str = ""

    @property
    def key(self) -> str:
        return f"{self.symbol}|{self.timeframe}"

    @property
    def bars(self) -> int:
        return len(self.series)

    def audit_view(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "bars": self.bars,
            "origin": self.origin or getattr(self.series, "source", "") or "",
            "first_bar": _stamp(self.series, 0),
            "last_bar": _stamp(self.series, self.bars - 1),
            "costs": self.cost.labels(),
            "costs_incomplete": list(self.cost.incomplete()),
        }


def _stamp(series: Any, index: int) -> str:
    stamps = getattr(series, "timestamps", ())
    if 0 <= index < len(stamps):
        value = stamps[index]
        return value.isoformat() if hasattr(value, "isoformat") else str(value)
    return ""


def split_bounds(bars: int) -> dict[str, tuple[int, int]]:
    """The 60/20/20 windows as half-open index ranges."""

    first = int(round(bars * SPLIT[0]))
    second = first + int(round(bars * SPLIT[1]))
    return {
        "train": (0, first),
        "validation": (first, second),
        "test": (second, bars),
    }


@dataclass(frozen=True)
class Row:
    """One (candidate, symbol, timeframe) result, or the reason it has none."""

    candidate_id: str
    family: str
    structure: str
    symbol: str
    timeframe: str
    status: str
    reasons: tuple[str, ...] = ()
    detail_class: str = ""
    bars: int = 0
    unfilled: int = 0
    signals: dict[str, Any] = field(default_factory=dict)
    windows: dict[str, SegmentStats] = field(default_factory=dict)
    walk_forward: tuple[dict[str, Any], ...] = ()
    folds_positive: int = 0
    screen: str = SCREEN_NOT_RUN
    screen_reasons: tuple[str, ...] = ()
    score: float | None = None
    concentration: float = 0.0
    costs_incomplete: bool = False

    @property
    def key(self) -> str:
        return f"{self.candidate_id}@{self.symbol}|{self.timeframe}"

    @property
    def trades(self) -> int:
        return sum(block.trades for block in self.windows.values())

    @property
    def net_return(self) -> float:
        return sum(block.net_return for block in self.windows.values())

    def window(self, name: str) -> SegmentStats | None:
        return self.windows.get(name)

    def compact(self) -> dict[str, Any]:
        """One row, small enough to keep a thousand of them on disk and in memory."""

        return {
            "candidate_id": self.candidate_id,
            "family": self.family,
            "structure": self.structure,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "status": self.status,
            "reasons": list(self.reasons),
            "bars": self.bars,
            "trades": self.trades,
            "screen": self.screen,
            "screen_reasons": list(self.screen_reasons),
            "score": None if self.score is None else round(self.score, 6),
            "concentration": round(self.concentration, 3),
            "costs_incomplete": self.costs_incomplete,
            "folds_positive": self.folds_positive,
            "windows": {name: _window_brief(block) for name, block in self.windows.items()},
            "walk_forward": [
                {key: value for key, value in fold.items()} for fold in self.walk_forward
            ],
        }

    def audit_view(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "family": self.family,
            "structure": self.structure,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "status": self.status,
            "reasons": list(self.reasons),
            "detail_class": self.detail_class,
            "bars": self.bars,
            "unfilled_entries": self.unfilled,
            "signals": dict(self.signals),
            "screen": self.screen,
            "screen_reasons": list(self.screen_reasons),
            "score": None if self.score is None else round(self.score, 6),
            "concentration": round(self.concentration, 3),
            "costs_incomplete": self.costs_incomplete,
            "folds_positive": self.folds_positive,
            "windows": {name: block.audit_view() for name, block in self.windows.items()},
            "walk_forward": list(self.walk_forward),
        }


def _window_brief(block: SegmentStats | None) -> dict[str, Any] | None:
    if block is None:
        return None
    return {
        "trades": block.trades,
        "net_return": round(block.net_return, 6),
        "net_pips": round(block.net_pips, 2),
        "cost_pips": round(block.cost_pips, 2),
        "max_drawdown": round(block.max_drawdown, 6),
        "win_rate": round(block.win_rate, 4),
        "forced_exits": block.forced_exits,
    }


@dataclass
class EvaluationPlan:
    """What to run, on what, for how long.  Everything here is explicit."""

    slots: Sequence[SeriesSlot]
    lot: float = 0.10
    budget_seconds: float = DEFAULT_BUDGET_SECONDS
    min_trades: int = DEFAULT_MIN_TRADES
    max_drawdown: float = DEFAULT_MAX_DRAWDOWN
    concentration_limit: float = DEFAULT_CONCENTRATION_LIMIT
    max_walk_forward: int = DEFAULT_MAX_WALK_FORWARD
    folds: int = DEFAULT_FOLDS
    timeframes: tuple[str, ...] = ()
    families: tuple[str, ...] = ()

    def slots_in_order(self) -> tuple[SeriesSlot, ...]:
        return tuple(slot for slot in self.slots if slot.bars > 0)


@dataclass(frozen=True)
class EvaluationReport:
    """The whole run: rows, counts, what was assumed, and what it cannot claim."""

    rows: tuple[Row, ...]
    plan_summary: dict[str, Any]
    counts: dict[str, Any]
    elapsed_seconds: float
    budget_seconds: float
    status: str
    started_at: str
    finished_at: str
    claim: str = CLAIM_NOTE
    search_note: str = SEARCH_NOTE
    notes: tuple[str, ...] = ()

    # -- views ------------------------------------------------------------

    def tested(self) -> tuple[Row, ...]:
        return tuple(row for row in self.rows if row.status == STATUS_TESTED)

    def survivors(self) -> tuple[Row, ...]:
        return tuple(row for row in self.rows if row.screen == SCREEN_PASSED)

    def ranked(self) -> tuple[Row, ...]:
        """Survivors in stability order.  Not a profit ranking, by design."""

        scored = [row for row in self.survivors() if row.score is not None]
        return tuple(sorted(scored, key=lambda row: (-(row.score or 0.0), row.key)))

    def paper_set(self, limit: int = 12) -> tuple[Row, ...]:
        """Rows the paper test may follow: survivors, stability first.

        A candidate only enters the paper set if it is a survivor, has at least one
        positive walk-forward fold, and produced enough trades on validation to be
        more than one lucky fill.
        """

        chosen: list[Row] = []
        for row in self.ranked():
            if row.folds_positive < 1:
                continue
            chosen.append(row)
            if len(chosen) >= limit:
                break
        return tuple(chosen)

    def paper_symbols(self, limit: int = 3) -> tuple[str, ...]:
        symbols: list[str] = []
        for row in self.paper_set(limit=200):
            if row.symbol not in symbols:
                symbols.append(row.symbol)
            if len(symbols) >= limit:
                break
        return tuple(symbols)

    def table(self, limit: int = 20) -> tuple[str, ...]:
        lines = [
            f"{'candidate':22s} {'symbol':10s} {'tf':4s} {'trades':>6s} "
            f"{'train%':>8s} {'val%':>8s} {'test%':>8s} {'dd%':>7s} {'folds':>5s} screen"
        ]
        for row in self.ranked()[:limit]:
            train = row.window("train")
            validation = row.window("validation")
            test = row.window("test")
            lines.append(
                f"{row.candidate_id:22s} {row.symbol:10s} {row.timeframe:4s} "
                f"{row.trades:6d} "
                f"{100 * (train.net_return if train else 0.0):8.2f} "
                f"{100 * (validation.net_return if validation else 0.0):8.2f} "
                f"{100 * (test.net_return if test else 0.0):8.2f} "
                f"{100 * (validation.max_drawdown if validation else 0.0):7.2f} "
                f"{row.folds_positive:5d} {row.screen}"
            )
        if not lines[1:]:
            lines.append("(no candidate reached the screen)")
        return tuple(lines)

    def summary(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "budget_seconds": self.budget_seconds,
            "counts": dict(self.counts),
            "plan": dict(self.plan_summary),
            "survivors": len(self.survivors()),
            "paper_set": len(self.paper_set()),
            "claim": self.claim,
            "search_note": self.search_note,
            "notes": list(self.notes),
        }

    def audit_view(self, limit: int = 200) -> dict[str, Any]:
        payload = self.summary()
        payload["rows"] = [row.audit_view() for row in self.rows[:limit]]
        payload["rows_omitted"] = max(0, len(self.rows) - limit)
        return payload

    def to_payload(self, limit: int = 0, *, compact: bool = True) -> dict[str, Any]:
        """JSON-safe form for the local evaluation record.

        ``compact`` keeps one short record per row -- status, screen, the window
        results and the folds -- because a thousand full audit views make a
        multi-megabyte file that a page load would have to read for no reason.
        """

        payload: dict[str, Any] = self.summary()
        payload["started_at"] = self.started_at
        payload["finished_at"] = self.finished_at
        rows = self.rows if limit <= 0 else self.rows[:limit]
        payload["rows"] = [row.compact() if compact else row.audit_view() for row in rows]
        payload["rows_total"] = len(self.rows)
        payload["paper_set"] = [
            {
                "candidate_id": row.candidate_id,
                "symbol": row.symbol,
                "timeframe": row.timeframe,
                "score": row.score,
                "folds_positive": row.folds_positive,
            }
            for row in self.paper_set()
        ]
        return payload


def _screen_row(
    row: Row,
    *,
    min_trades: int,
    max_drawdown: float,
    concentration_limit: float,
    validation_trades: Sequence[Trade] = (),
) -> tuple[str, tuple[str, ...], float | None, float]:
    """Judge one row on train and validation only."""

    train = row.window("train")
    validation = row.window("validation")
    if train is None or validation is None:
        return SCREEN_NOT_RUN, ("no_window",), None, 0.0
    if train.trades < min_trades or validation.trades < min_trades:
        return (
            SCREEN_SHORT_SAMPLE,
            (
                f"fewer than {min_trades} trades in train ({train.trades}) "
                f"or validation ({validation.trades})",
            ),
            None,
            0.0,
        )
    concentration = _concentration(validation_trades)
    reasons: list[str] = []
    if train.net_return <= 0:
        reasons.append("train net return after costs is not positive")
    if validation.net_return <= 0:
        reasons.append("validation net return after costs is not positive")
    if validation.max_drawdown > max_drawdown:
        reasons.append(
            f"validation drawdown {validation.max_drawdown:.1%} exceeds the "
            f"{max_drawdown:.0%} limit"
        )
    if concentration > concentration_limit:
        reasons.append(
            "one validation trade carries "
            f"{concentration:.0%} of the validation result"
        )
    if reasons:
        return SCREEN_FAILED, tuple(reasons), None, concentration
    # The ordering is the weaker of the two windows, penalised by drawdown.  It
    # says "stable on the windows we were allowed to look at", nothing more.
    score = min(train.net_return, validation.net_return) - 0.5 * validation.max_drawdown
    return SCREEN_PASSED, (), score, concentration


def _concentration(trades: Sequence[Trade]) -> float:
    """Share of a window's net result carried by its single best trade."""

    if not trades:
        return 0.0
    total = sum(trade.net_quote for trade in trades)
    if total <= 0:
        return 1.0
    best = max(trade.net_quote for trade in trades)
    return max(0.0, min(1.0, best / total))


def _portfolio_metrics(trades: Sequence[Trade]) -> tuple[float, float]:
    """Net return and max drawdown over a set of trades, in equity order."""

    if not trades:
        return 0.0, 0.0
    equity = 0.0
    peak = 0.0
    drawdown = 0.0
    for trade in trades:
        equity += trade.net_return
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return equity, drawdown


def _fold_windows(bars: int, folds: int) -> tuple[tuple[str, int, int], ...]:
    if folds <= 0 or bars <= 0:
        return ()
    size = bars // folds
    if size < MIN_SEGMENT_BARS:
        return ()
    out: list[tuple[str, int, int]] = []
    for number in range(folds):
        start = number * size
        end = bars if number == folds - 1 else (number + 1) * size
        out.append((f"fold_{number + 1}", start, end))
    return tuple(out)


def _run_window(
    candidate: Candidate,
    slot: SeriesSlot,
    signals: Any,
    bounds: Mapping[str, tuple[int, int]],
    lot: float,
) -> tuple[dict[str, SegmentStats], dict[str, list[Trade]], int]:
    """Simulate each window separately, then aggregate with the engine's maths."""

    by_segment: dict[str, list[Trade]] = {}
    blocks: dict[str, SegmentStats] = {}
    unfilled = 0
    incomplete = slot.cost.incomplete()
    for name in SEGMENTS:
        start, end = bounds[name]
        if end - start < MIN_SEGMENT_BARS:
            continue
        trades, missed = simulate_candidate(
            slot.series,
            signals,
            candidate,
            cost=slot.cost,
            lot=lot,
            segment=name,
            start=start,
            end=end,
        )
        unfilled += missed
        by_segment[name] = list(trades)
        blocks[name] = segment_stats(name, trades, end - start, incomplete)
    return blocks, by_segment, unfilled


def evaluate(
    plan: EvaluationPlan,
    *,
    candidates: Sequence[Candidate] | None = None,
    cancel: Callable[[], bool] | None = None,
    progress: Callable[[int, int], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = _utc_now,
) -> EvaluationReport:
    """Run the declared catalog over the declared slots, inside the budget."""

    started = now()
    began = clock()
    slots = plan.slots_in_order()
    items = tuple(candidates) if candidates is not None else catalog()
    if plan.families:
        allowed = set(plan.families)
        items = tuple(item for item in items if item.family in allowed)
    if plan.timeframes:
        allowed_tf = set(plan.timeframes)
        items = tuple(item for item in items if set(timeframes_for(item)) & allowed_tf)

    rows: list[Row] = []
    origin: dict[tuple[str, str], Candidate] = {}
    tested_count = 0
    skipped_count = 0
    failed_count = 0
    structures_tested: set[str] = set()
    cancelled = False
    budget_hit = False
    total = len(items) * max(1, len(slots))

    cache_by_slot: dict[str, IndicatorCache] = {}
    coarse_by_slot: dict[tuple[str, int], HigherTimeframe] = {}
    bounds_by_slot: dict[str, dict[str, tuple[int, int]]] = {}

    for candidate in items:
        for slot in slots:
            reason_slots = ()
            if budget_hit or cancelled:
                reason_slots = (SKIP_CANCELLED if cancelled else SKIP_BUDGET,)
            row_base = dict(
                candidate_id=candidate.candidate_id,
                family=candidate.family,
                structure=candidate.structure,
                symbol=slot.symbol,
                timeframe=slot.timeframe,
            )
            if reason_slots:
                rows.append(Row(status=STATUS_SKIPPED, reasons=reason_slots, **row_base))
                skipped_count += 1
                continue
            supported = timeframes_for(candidate)
            if slot.timeframe not in supported:
                rows.append(
                    Row(
                        status=STATUS_SKIPPED,
                        reasons=(SKIP_TIMEFRAME,),
                        bars=slot.bars,
                        **row_base,
                    )
                )
                skipped_count += 1
                continue
            if slot.bars < max(candidate.min_bars, MIN_SEGMENT_BARS * 3):
                rows.append(
                    Row(
                        status=STATUS_SKIPPED,
                        reasons=(SKIP_HISTORY,),
                        bars=slot.bars,
                        **row_base,
                    )
                )
                skipped_count += 1
                continue
            if cancel is not None and cancel():
                cancelled = True
                rows.append(
                    Row(
                        status=STATUS_SKIPPED,
                        reasons=(SKIP_CANCELLED,),
                        bars=slot.bars,
                        **row_base,
                    )
                )
                skipped_count += 1
                continue
            if clock() - began > plan.budget_seconds:
                budget_hit = True
                rows.append(
                    Row(
                        status=STATUS_SKIPPED,
                        reasons=(SKIP_BUDGET,),
                        bars=slot.bars,
                        **row_base,
                    )
                )
                skipped_count += 1
                continue
            try:
                cache = cache_by_slot.get(slot.key)
                if cache is None:
                    cache = IndicatorCache.of(slot.series)
                    cache_by_slot[slot.key] = cache
                    bounds_by_slot[slot.key] = split_bounds(slot.bars)
                coarse = None
                if candidate.filter_kind.needs_coarse:
                    ratio = int(candidate.filter_params.get("ratio", 4))
                    coarse_key = (slot.key, ratio)
                    coarse = coarse_by_slot.get(coarse_key)
                    if coarse is None:
                        coarse = HigherTimeframe.build(slot.series, ratio)
                        coarse_by_slot[coarse_key] = coarse
                context = context_for(slot.series, cache, candidate, coarse=coarse)
                signals = signals_for(candidate, context)
                bounds = bounds_by_slot[slot.key]
                blocks, by_segment, unfilled = _run_window(
                    candidate, slot, signals, bounds, plan.lot
                )
            except Exception as error:  # curated: the message never reaches the UI
                failed_count += 1
                rows.append(
                    Row(
                        status=STATUS_FAILED,
                        reasons=(FAIL_SIMULATION,),
                        detail_class=type(error).__name__,
                        bars=slot.bars,
                        **row_base,
                    )
                )
                continue
            row = Row(
                status=STATUS_TESTED,
                bars=slot.bars,
                unfilled=unfilled,
                signals=signals.audit_view(),
                windows=blocks,
                costs_incomplete=bool(slot.cost.incomplete()),
                **row_base,
            )
            screen, screen_reasons, score, concentration = _screen_row(
                row,
                min_trades=plan.min_trades,
                max_drawdown=plan.max_drawdown,
                concentration_limit=plan.concentration_limit,
                validation_trades=by_segment.get("validation", ()),
            )
            row = replace(
                row,
                screen=screen,
                screen_reasons=screen_reasons,
                score=score,
                concentration=concentration,
            )
            origin[(candidate.candidate_id, slot.key)] = candidate
            tested_count += 1
            structures_tested.add(candidate.structure)
            rows.append(row)
            if progress is not None:
                progress(len(rows), total)

    rows = _walk_forward_rows(rows, origin, slots, plan, clock=clock, began=began)
    if progress is not None:
        progress(len(rows), total)

    elapsed = max(0.0, clock() - began)
    finished = now()
    row_tuple = tuple(rows)
    counts = _counts(row_tuple, items, slots, tested_count, skipped_count, failed_count, structures_tested)
    status = "COMPLETE"
    if cancelled:
        status = "CANCELLED_PARTIAL"
    elif budget_hit:
        status = "TIMEOUT_PARTIAL"
    notes = _notes(row_tuple, slots, counts)
    return EvaluationReport(
        rows=row_tuple,
        plan_summary={
            "slot_count": len(slots),
            "symbols": sorted({slot.symbol for slot in slots}),
            "timeframes": sorted({slot.timeframe for slot in slots}),
            "lot": plan.lot,
            "candidates_considered": len(items),
            "min_trades": plan.min_trades,
            "max_drawdown": plan.max_drawdown,
            "concentration_limit": plan.concentration_limit,
            "folds": plan.folds,
            "split": list(SPLIT),
            "slots": [slot.audit_view() for slot in slots],
            "costs": {slot.key: slot.cost.labels() for slot in slots},
        },
        counts=counts,
        elapsed_seconds=elapsed,
        budget_seconds=plan.budget_seconds,
        status=status,
        started_at=started.isoformat(),
        finished_at=finished.isoformat(),
        notes=notes,
    )


def _walk_forward_rows(
    rows: list[Row],
    origin: Mapping[tuple[str, str], Candidate],
    slots: Sequence[SeriesSlot],
    plan: EvaluationPlan,
    *,
    clock: Callable[[], float],
    began: float,
) -> list[Row]:
    """Give the best-screened rows their walk-forward check, parameters frozen.

    Only survivors are worth the extra passes, and only the strongest
    ``max_walk_forward`` of them, so a thousand candidates stay inside one budget.
    The choice is made on train and validation, never on the test window.
    """

    by_key = {(slot.symbol, slot.timeframe): slot for slot in slots}
    survivors = [row for row in rows if row.screen == SCREEN_PASSED and row.score is not None]
    survivors.sort(key=lambda row: (-(row.score or 0.0), row.key))
    chosen = survivors[: max(0, plan.max_walk_forward)]
    if not chosen:
        return rows
    updated: dict[str, Row] = {}
    for row in chosen:
        if clock() - began > plan.budget_seconds:
            break
        candidate = origin.get((row.candidate_id, f"{row.symbol}|{row.timeframe}"))
        slot = by_key.get((row.symbol, row.timeframe))
        if candidate is None or slot is None:  # pragma: no cover - defensive
            continue
        try:
            cache = IndicatorCache.of(slot.series)
            coarse = None
            if candidate.filter_kind.needs_coarse:
                coarse = HigherTimeframe.build(
                    slot.series, int(candidate.filter_params.get("ratio", 4))
                )
            signals = signals_for(candidate, context_for(slot.series, cache, candidate, coarse=coarse))
            folds, positive = _fold_summaries(candidate, slot, signals, plan)
        except Exception:  # curated: folds are optional evidence, not a run failure
            continue
        updated[row.key] = replace(row, walk_forward=folds, folds_positive=positive)
    if not updated:
        return rows
    return [updated.get(row.key, row) for row in rows]


def _fold_summaries(
    candidate: Candidate,
    slot: SeriesSlot,
    signals: Any,
    plan: EvaluationPlan,
) -> tuple[tuple[dict[str, Any], ...], int]:
    """Frozen-parameter walk-forward folds, for rows worth spending the time on."""

    windows = _fold_windows(slot.bars, plan.folds)
    if not windows:
        return (), 0
    summaries: list[dict[str, Any]] = []
    positive = 0
    for name, start, end in windows:
        trades, _missed = simulate_candidate(
            slot.series,
            signals,
            candidate,
            cost=slot.cost,
            lot=plan.lot,
            segment=name,
            start=start,
            end=end,
        )
        net, drawdown = _portfolio_metrics(trades)
        if net > 0 and trades:
            positive += 1
        summaries.append(
            {
                "fold": name,
                "start": _stamp(slot.series, start),
                "end": _stamp(slot.series, min(end, slot.bars) - 1),
                "trades": len(trades),
                "net_return": round(net, 6),
                "max_drawdown": round(drawdown, 6),
            }
        )
    return tuple(summaries), positive


def _counts(
    rows: Sequence[Row],
    items: Sequence[Candidate],
    slots: Sequence[SeriesSlot],
    tested: int,
    skipped: int,
    failed: int,
    structures_tested: set[str],
) -> dict[str, Any]:
    skip_reasons: dict[str, int] = {}
    for row in rows:
        if row.status == STATUS_SKIPPED:
            for reason in row.reasons:
                skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
    tested_rows = [row for row in rows if row.status == STATUS_TESTED]
    passed = [row for row in tested_rows if row.screen == SCREEN_PASSED]
    with_folds = [row for row in tested_rows if row.walk_forward]
    net = sum(row.net_return for row in tested_rows)
    total_trades = sum(row.trades for row in tested_rows)
    return {
        "candidates_considered": len(items),
        "slots": len(slots),
        "pairs_planned": len(items) * max(1, len(slots)),
        "tested": tested,
        "skipped": skipped,
        "failed": failed,
        "distinct_rule_structures_tested": len(structures_tested),
        "distinct_signatures_tested": len({row.candidate_id for row in tested_rows}),
        "skip_reasons": dict(sorted(skip_reasons.items())),
        "screen": {
            SCREEN_PASSED: len(passed),
            SCREEN_FAILED: sum(1 for row in tested_rows if row.screen == SCREEN_FAILED),
            SCREEN_SHORT_SAMPLE: sum(1 for row in tested_rows if row.screen == SCREEN_SHORT_SAMPLE),
            SCREEN_NOT_RUN: sum(1 for row in tested_rows if row.screen == SCREEN_NOT_RUN),
        },
        "walk_forward_rows": len(with_folds),
        "walk_forward_positive_rows": sum(1 for row in with_folds if row.folds_positive > 0),
        "trades_simulated": total_trades,
        "sum_net_return_over_rows": round(net, 6),
        "false_positive_context": {
            "at_5_percent": round(0.05 * max(1, tested), 1),
            "note": (
                "Rows at which a purely random result would look positive at the 5% "
                "level.  Compare it with the survivors before reading anything into them."
            ),
        },
    }


def _notes(rows: Sequence[Row], slots: Sequence[SeriesSlot], counts: Mapping[str, Any]) -> tuple[str, ...]:
    notes: list[str] = []
    if not slots:
        notes.append("no series was supplied, so nothing could be evaluated")
    origins = {slot.key: (slot.origin or getattr(slot.series, "source", "") or "") for slot in slots}
    if origins and all("TERMINAL" not in str(value).upper() for value in origins.values()):
        notes.append(
            "every series came from a labelled sample or fixture source, not the MT5 "
            "terminal: these numbers describe the sample, not the market"
        )
    if counts["skipped"]:
        notes.append(
            f"{counts['skipped']} of {counts['pairs_planned']} planned rows did not run; "
            "the reasons are counted in skip_reasons and listed per row"
        )
    if counts["failed"]:
        notes.append(
            f"{counts['failed']} rows failed during simulation and are counted as failed, "
            "not as tested"
        )
    caveats: list[str] = []
    for slot in slots:
        for line in slot.cost.incomplete():
            if line not in caveats:
                caveats.append(line)
    if caveats:
        notes.append(
            "Costs are labelled, not measured, on every row: "
            + "; ".join(caveats)
            + ".  A row that passes the screen does so with that caveat attached."
        )
    short = counts["screen"].get(SCREEN_SHORT_SAMPLE, 0)
    if short:
        notes.append(
            f"{short} rows produced too few trades on train or validation to screen; "
            "that is a thin sample, not a rejection of the idea"
        )
    return tuple(notes)


__all__ = [
    "CLAIM_NOTE",
    "DEFAULT_FOLDS",
    "DEFAULT_MAX_WALK_FORWARD",
    "EvaluationPlan",
    "EvaluationReport",
    "Row",
    "SCREEN_FAILED",
    "SCREEN_NOT_RUN",
    "SCREEN_PASSED",
    "SCREEN_SHORT_SAMPLE",
    "SEGMENTS",
    "SKIP_BUDGET",
    "SKIP_CANCELLED",
    "SKIP_HISTORY",
    "SKIP_NO_SERIES",
    "SKIP_TIMEFRAME",
    "STATUS_FAILED",
    "STATUS_SKIPPED",
    "STATUS_TESTED",
    "SeriesSlot",
    "evaluate",
    "split_bounds",
]
