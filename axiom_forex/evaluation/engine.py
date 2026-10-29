"""Aligned multi-pair research simulation over one chronological phase.

Execution rules, all explicit:

* A decision is made only from a pair's own *completed* bars up to and including
  the decision bar.  The prefix is the only input to the strategy.
* A decision is filled on that pair's next available bar, at its open, using the
  ask for a buy and the bid for a sell (next-bar bid/ask convention).
* A fill can never occur before the decision bar completes; violating that is a
  hard :class:`LookAheadError`.
* Valuation marks use, per pair, the latest bar that has *completed* at the
  valuation instant.  An in-progress bar from another timeframe is never used.
* The last bar of a phase cannot fill; its pending signal is cancelled and
  counted.
* Missing data is skipped, never interpolated; a fill inside a closed calendar
  session is refused rather than fabricated.

The engine builds no orders, uses no broker, and never mutates platform state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping, Sequence

from app.calendar.trading import TradingCalendar
from app.common.types import OrderSide
from app.data.models import ForexQuoteBar

from axiom_forex.regimes.session import classify_regime
from axiom_forex.strategies.base import Signal

from .alignment import pair_phase_coverage
from .book import MultiPairBook, PairTerms
from .contracts import (
    EquityPoint,
    EvaluationConfig,
    LookAheadError,
    PairSeries,
    PhaseResult,
    PhaseWindow,
    SessionSpec,
    TradeRecord,
    session_for,
)

ZERO = Decimal(0)


@dataclass(frozen=True)
class PendingOrder:
    pair: str
    side: Signal
    decision_index: int
    decision_at: datetime
    decision_bar_end: datetime
    session: str
    regime: str


def _phase_bars(
    series: PairSeries, window: PhaseWindow
) -> tuple[ForexQuoteBar, ...]:
    return tuple(
        bar
        for bar in series.bars
        if bar.timestamp >= window.start and bar.end_at <= window.end
    )


def _executable_price(bar: ForexQuoteBar, side: OrderSide) -> Decimal:
    return bar.executable_price(side, point="open")


def _drawdown(
    curve: Sequence[EquityPoint], initial: Decimal
) -> tuple[Decimal, Decimal]:
    peak = initial
    worst = ZERO
    worst_fraction = ZERO
    for point in curve:
        peak = max(peak, point.equity_account)
        drop = peak - point.equity_account
        if drop > worst:
            worst = drop
            worst_fraction = drop / peak if peak > 0 else ZERO
    return worst, worst_fraction


def run_phase(
    *,
    window: PhaseWindow,
    series: Mapping[str, PairSeries],
    config: EvaluationConfig,
    calendar: TradingCalendar,
    book: MultiPairBook,
    pair_terms: Mapping[str, PairTerms],
    sessions: Sequence[SessionSpec] = (),
) -> PhaseResult:
    """Simulate one phase across all pairs on their own calendars."""

    pair_bars: dict[str, tuple[ForexQuoteBar, ...]] = {
        pair: _phase_bars(item, window) for pair, item in series.items()
    }
    strategies = {pair: config.strategy_factory(pair) for pair in pair_bars}
    events: list[tuple[datetime, str, int]] = []
    for pair, bars in pair_bars.items():
        for index, bar in enumerate(bars):
            events.append((bar.timestamp, pair, index))
    events.sort()

    initial_equity = book.equity({}, window.start)
    curve: list[EquityPoint] = [
        EquityPoint(
            at=window.start,
            equity_account=initial_equity,
            gross_exposure_account=ZERO,
            adjusted_exposure_account=ZERO,
            margin_account=ZERO,
            max_currency_exposure_account=ZERO,
            open_positions=0,
        )
    ]
    trades: list[TradeRecord] = []
    rejections: list[dict[str, Any]] = []
    signals: list[dict[str, Any]] = []
    guard_events: list[str] = []
    cancelled_final = 0
    pending: dict[str, PendingOrder] = {}
    completed_pointer: dict[str, int] = {pair: -1 for pair in pair_bars}
    position_ids: dict[str, str] = {}
    peak_currency: dict[str, Decimal] = {}
    peak_pair: dict[str, Decimal] = {}
    max_gross = ZERO
    max_adjusted = ZERO
    max_currency = ZERO
    aggregate_unavailable = 0

    def completed_marks(at: datetime) -> dict[str, ForexQuoteBar]:
        marks: dict[str, ForexQuoteBar] = {}
        for pair, bars in pair_bars.items():
            pointer = completed_pointer[pair]
            while (
                pointer + 1 < len(bars)
                and bars[pointer + 1].end_at <= at
            ):
                pointer += 1
            completed_pointer[pair] = pointer
            if pointer >= 0:
                marks[pair] = bars[pointer]
        return marks

    def record_rejection(
        *,
        code: str,
        reason: str,
        pair: str,
        index: int,
        at: datetime,
    ) -> None:
        rejections.append(
            {
                "code": code,
                "reason": reason,
                "pair": pair,
                "index": index,
                "at": at,
            }
        )

    for event_time, pair, index in events:
        bars = pair_bars[pair]
        bar = bars[index]
        terms = pair_terms[pair]
        item = series[pair]

        # -- 1. fill any pending decision for this pair --------------------
        order = pending.pop(pair, None)
        if order is not None:
            if event_time < order.decision_bar_end:
                raise LookAheadError(
                    f"fill_before_decision_completion:{pair}:{event_time.isoformat()}"
                )
            if not calendar.status(pair, event_time).is_open:
                record_rejection(
                    code="FILL_OUTSIDE_CALENDAR_SESSION",
                    reason="calendar_closed_at_fill",
                    pair=pair,
                    index=index,
                    at=event_time,
                )
            elif order.side in (Signal.BUY, Signal.SELL):
                side = OrderSide.BUY if order.side == Signal.BUY else OrderSide.SELL
                if side == OrderSide.SELL and not config.allow_short:
                    record_rejection(
                        code="SHORT_DISALLOWED",
                        reason="short_selling_disabled",
                        pair=pair,
                        index=index,
                        at=event_time,
                    )
                elif pair in position_ids:
                    record_rejection(
                        code="ALREADY_POSITIONED",
                        reason="pair_already_has_open_position",
                        pair=pair,
                        index=index,
                        at=event_time,
                    )
                elif len(book.positions) >= config.max_open_positions:
                    record_rejection(
                        code="MAX_OPEN_POSITIONS",
                        reason="portfolio_position_limit",
                        pair=pair,
                        index=index,
                        at=event_time,
                    )
                else:
                    price = _executable_price(bar, side)
                    quantity = config.quantity_for(pair)
                    base_units = terms.spec.to_base_units(quantity)
                    marks = completed_marks(event_time)
                    decision = book.entry_decision(
                        pair=pair,
                        side=side,
                        base_units=base_units,
                        price=price,
                        marks=marks,
                        at=event_time,
                    )
                    if not decision.allowed:
                        record_rejection(
                            code="AGGREGATE_RISK_REJECTED",
                            reason=",".join(decision.reasons),
                            pair=pair,
                            index=index,
                            at=event_time,
                        )
                    else:
                        position_id = f"{pair}:{order.decision_index}"
                        book.open_position(
                            position_id=position_id,
                            pair=pair,
                            side=side,
                            quantity=quantity,
                            price=price,
                            at=event_time,
                            session=order.session,
                            regime=order.regime,
                            decision_index=order.decision_index,
                            data_class=item.data_class,
                        )
                        position_ids[pair] = position_id
            elif order.side == Signal.EXIT:
                position_id = position_ids.get(pair)
                if position_id is None:
                    record_rejection(
                        code="NO_OPEN_POSITION",
                        reason="exit_without_position",
                        pair=pair,
                        index=index,
                        at=event_time,
                    )
                else:
                    position = book.positions[position_id]
                    closing_side = (
                        OrderSide.SELL
                        if position.side == OrderSide.BUY
                        else OrderSide.BUY
                    )
                    price = _executable_price(bar, closing_side)
                    trade = book.close_position(
                        position_id=position_id, price=price, at=event_time
                    )
                    trades.append(trade)
                    del position_ids[pair]

        # -- 2. value the book at this bar's completion --------------------
        at = bar.end_at
        marks = completed_marks(at)
        equity = book.equity(marks, at)
        gross = book.gross_exposure(marks, at)
        margin = book.margin(marks, at)
        currency = book.currency_exposures(marks, at)
        for pair_name, value in book.pair_notionals(marks, at).items():
            peak_pair[pair_name] = max(peak_pair.get(pair_name, ZERO), abs(value))
        for name, value in currency.items():
            peak_currency[name] = max(peak_currency.get(name, ZERO), abs(value))
        currency_peak = max((abs(value) for value in currency.values()), default=ZERO)
        adjusted: Decimal | None = None
        if book.positions:
            decision = book.aggregate_decision(marks, at)
            if decision.adjusted_exposure_account is not None:
                adjusted = decision.adjusted_exposure_account
            else:
                aggregate_unavailable += 1
        max_gross = max(max_gross, gross)
        max_adjusted = max(max_adjusted, adjusted or ZERO)
        max_currency = max(max_currency, currency_peak)
        curve.append(
            EquityPoint(
                at=at,
                equity_account=equity,
                gross_exposure_account=gross,
                adjusted_exposure_account=adjusted,
                margin_account=margin,
                max_currency_exposure_account=currency_peak,
                open_positions=len(book.positions),
            )
        )

        # -- 3. decide from the completed prefix only ----------------------
        history = strategies[pair].history_limit
        is_last = index == len(bars) - 1
        if index + 1 >= max(history, 1):
            prefix = bars[: index + 1]
            signal = strategies[pair].generate_signal(prefix)
            snapshot = classify_regime(prefix, calendar=calendar, pair=pair)
            session = session_for(bar.timestamp, sessions)
            signals.append(
                {
                    "pair": pair,
                    "index": index,
                    "at": bar.timestamp,
                    "signal": signal.value,
                    "session": session,
                    "regime": snapshot.label.value,
                    "final_bar": is_last,
                }
            )
            if signal != Signal.HOLD:
                if is_last:
                    if config.final_bar_signal_cancelled:
                        cancelled_final += 1
                    else:  # pragma: no cover - refuses to fabricate a fill
                        record_rejection(
                            code="FINAL_BAR_FILL_REFUSED",
                            reason="no_next_bar_for_execution",
                            pair=pair,
                            index=index,
                            at=bar.end_at,
                        )
                else:
                    pending[pair] = PendingOrder(
                        pair=pair,
                        side=signal,
                        decision_index=index,
                        decision_at=bar.timestamp,
                        decision_bar_end=bar.end_at,
                        session=session,
                        regime=snapshot.label.value,
                    )

    if pending:
        # No next bar exists for these decisions, so no fill is fabricated.
        cancelled_final += len(pending)
        pending.clear()
    if aggregate_unavailable:
        guard_events.append("adjusted_exposure_unavailable")

    final_marks = completed_marks(window.end)
    final_equity = book.equity(final_marks, window.end) if final_marks else initial_equity
    unrealized = sum(book.unrealized_by_pair(final_marks, window.end).values(), ZERO) if final_marks else ZERO
    realized = sum((trade.net_pnl_account for trade in trades), ZERO)
    drawdown, drawdown_fraction = _drawdown(curve, initial_equity)
    turnover = book.turnover_account
    turnover_ratio = turnover / initial_equity if initial_equity > 0 else ZERO

    pair_summary: dict[str, dict[str, Any]] = {}
    for pair, bars in pair_bars.items():
        pair_trades = [trade for trade in trades if trade.pair == pair]
        pair_unrealized = (
            book.unrealized_by_pair(final_marks, window.end).get(pair, ZERO)
            if final_marks
            else ZERO
        )
        coverage = pair_phase_coverage(
            series=series[pair],
            calendar=calendar,
            start=window.start,
            end=window.end,
            grid_seconds=config.grid_seconds,
        )
        pair_summary[pair] = {
            "bars_in_phase": len(bars),
            "first_bar_at": bars[0].timestamp if bars else None,
            "last_bar_at": bars[-1].end_at if bars else None,
            "trades": len(pair_trades),
            "realized_pnl_account": sum(
                (trade.net_pnl_account for trade in pair_trades), ZERO
            ),
            "unrealized_pnl_account": pair_unrealized,
            "costs_account": sum((trade.costs_account for trade in pair_trades), ZERO),
            "turnover_account": book.turnover_by_pair.get(pair, ZERO),
            "max_exposure_account": peak_pair.get(pair, ZERO),
            "coverage_ratio": coverage.coverage_ratio,
            "missing_slots": coverage.missing_slots,
            "out_of_session_bars": coverage.out_of_session_bars,
            "data_class": series[pair].data_class,
            "signals": sum(1 for item in signals if item["pair"] == pair),
        }
        pair_summary[pair]["net_pnl_account"] = (
            pair_summary[pair]["realized_pnl_account"]
            + pair_summary[pair]["unrealized_pnl_account"]
        )

    coverage_rows = tuple(
        pair_phase_coverage(
            series=series[pair],
            calendar=calendar,
            start=window.start,
            end=window.end,
            grid_seconds=config.grid_seconds,
        )
        for pair in sorted(series)
    )

    return PhaseResult(
        window=window,
        initial_equity_account=initial_equity,
        final_equity_account=final_equity,
        realized_pnl_account=realized,
        unrealized_pnl_account=unrealized,
        net_pnl_account=final_equity - initial_equity,
        costs_account=book.cost_total_account,
        turnover_account=turnover,
        turnover_ratio=turnover_ratio,
        max_drawdown_account=drawdown,
        max_drawdown_fraction=drawdown_fraction,
        max_gross_exposure_account=max_gross,
        max_adjusted_exposure_account=max_adjusted,
        max_currency_exposure_account=max_currency,
        peak_currency_exposure=dict(peak_currency),
        trades=tuple(trades),
        equity_curve=tuple(curve),
        rejections=tuple(rejections),
        signals=tuple(signals),
        open_positions=tuple(
            position.audit_view() for position in book.positions.values()
        ),
        pair_summary=pair_summary,
        coverage=coverage_rows,
        cancelled_final_signals=cancelled_final,
        guard_events=tuple(sorted(set(guard_events))),
    )
