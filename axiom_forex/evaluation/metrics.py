"""Performance and risk aggregation by pair, session, regime, and portfolio.

All numbers are derived from the research phase results.  Nothing here is a
profitability claim: the report labels results as historical simulation over an
explicitly declared dataset class.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterable, Sequence

from .contracts import (
    GroupMetric,
    PhaseResult,
    REGIME_UNSPECIFIED,
    SESSION_UNSPECIFIED,
    TradeRecord,
)

ZERO = Decimal(0)


def _realized_drawdown(
    trades: Sequence[TradeRecord],
) -> tuple[Decimal, Decimal]:
    """Drawdown of the group's cumulative *realized* P&L, in exit order."""

    cumulative = ZERO
    peak = ZERO
    worst = ZERO
    worst_fraction = ZERO
    for trade in sorted(trades, key=lambda item: item.exit_at):
        cumulative += trade.net_pnl_account
        peak = max(peak, cumulative)
        drop = peak - cumulative
        if drop > worst:
            worst = drop
            worst_fraction = drop / peak if peak > 0 else ZERO
    return worst, worst_fraction


def _group(
    *,
    key: str,
    kind: str,
    trades: Sequence[TradeRecord],
    max_drawdown_account: Decimal | None = None,
    max_drawdown_fraction: Decimal | None = None,
    exposure_account: Decimal = ZERO,
) -> GroupMetric:
    wins = sum(1 for trade in trades if trade.net_pnl_account > 0)
    losses = sum(1 for trade in trades if trade.net_pnl_account < 0)
    count = len(trades)
    realized_drawdown, realized_drawdown_fraction = _realized_drawdown(trades)
    if max_drawdown_account is None:
        max_drawdown_account = realized_drawdown
    if max_drawdown_fraction is None:
        max_drawdown_fraction = realized_drawdown_fraction
    return GroupMetric(
        key=key,
        kind=kind,
        trades=count,
        wins=wins,
        losses=losses,
        win_ratio=Decimal(wins) / Decimal(count) if count else ZERO,
        gross_pnl_account=sum((trade.gross_pnl_account for trade in trades), ZERO),
        costs_account=sum((trade.costs_account for trade in trades), ZERO),
        net_pnl_account=sum((trade.net_pnl_account for trade in trades), ZERO),
        turnover_account=sum(
            (trade.notional_account + trade.exit_notional_account for trade in trades),
            ZERO,
        ),
        max_drawdown_account=max_drawdown_account,
        max_drawdown_fraction=max_drawdown_fraction,
        exposure_account=exposure_account,
        holding_seconds=sum((trade.holding_seconds for trade in trades), ZERO),
    )


def _group_by(
    phases: Iterable[PhaseResult],
    *,
    attribute: str,
    kind: str,
    unspecified: str,
) -> tuple[GroupMetric, ...]:
    trades: list[TradeRecord] = []
    for phase in phases:
        trades.extend(phase.trades)
    buckets: dict[str, list[TradeRecord]] = {}
    for trade in trades:
        key = str(getattr(trade, attribute) or unspecified)
        buckets.setdefault(key, []).append(trade)
    return tuple(
        _group(key=key, kind=kind, trades=buckets[key])
        for key in sorted(buckets)
    )


def pair_metrics(phases: Iterable[PhaseResult]) -> tuple[GroupMetric, ...]:
    """Per-pair metrics including that pair's simulated coverage and exposure."""

    phase_list = tuple(phases)
    trades_by_pair: dict[str, list[TradeRecord]] = {}
    for phase in phase_list:
        for trade in phase.trades:
            trades_by_pair.setdefault(trade.pair, []).append(trade)
    rows: list[GroupMetric] = []
    for pair in sorted(trades_by_pair):
        exposure = ZERO
        coverage_ratio = ZERO
        for phase in phase_list:
            exposure = max(exposure, phase.pair_summary.get(pair, {}).get("max_exposure_account", ZERO))
            coverage_ratio = max(
                coverage_ratio,
                phase.pair_summary.get(pair, {}).get("coverage_ratio", ZERO),
            )
        metric = _group(
            key=pair,
            kind="pair",
            trades=trades_by_pair[pair],
            exposure_account=exposure,
        )
        rows.append(metric)
    return tuple(rows)


def session_metrics(phases: Iterable[PhaseResult]) -> tuple[GroupMetric, ...]:
    return _group_by(
        phases, attribute="entry_session", kind="session", unspecified=SESSION_UNSPECIFIED
    )


def regime_metrics(phases: Iterable[PhaseResult]) -> tuple[GroupMetric, ...]:
    return _group_by(
        phases, attribute="entry_regime", kind="regime", unspecified=REGIME_UNSPECIFIED
    )


def portfolio_metrics(
    phases: Sequence[PhaseResult], *, exposure_account: Decimal = ZERO
) -> GroupMetric:
    """Aggregate metric over the whole evaluated portfolio and every phase."""

    trades = [trade for phase in phases for trade in phase.trades]
    drawdown = max(
        (phase.max_drawdown_account for phase in phases), default=ZERO
    )
    drawdown_fraction = max(
        (phase.max_drawdown_fraction for phase in phases), default=ZERO
    )
    exposure = exposure_account or max(
        (phase.max_gross_exposure_account for phase in phases), default=ZERO
    )
    return _group(
        key="PORTFOLIO",
        kind="portfolio",
        trades=trades,
        max_drawdown_account=drawdown,
        max_drawdown_fraction=drawdown_fraction,
        exposure_account=exposure,
    )


def aggregate_coverage(phases: Sequence[PhaseResult]) -> tuple[dict[str, Any], ...]:
    """Data-quality coverage rows per pair across all phases."""

    rows: dict[str, dict[str, Any]] = {}
    for phase in phases:
        for coverage in phase.coverage:
            existing = rows.setdefault(
                coverage.pair,
                {
                    "pair": coverage.pair,
                    "timeframe": coverage.timeframe,
                    "grid_slots": 0,
                    "market_open_slots": 0,
                    "covered_slots": 0,
                    "missing_slots": 0,
                    "closed_slots": 0,
                    "missing_interval_count": 0,
                    "closed_interval_count": 0,
                    "out_of_session_bars": 0,
                    "quality": coverage.quality_classification,
                },
            )
            existing["grid_slots"] += coverage.grid_slots
            existing["market_open_slots"] += coverage.market_open_slots
            existing["covered_slots"] += coverage.covered_slots
            existing["missing_slots"] += coverage.missing_slots
            existing["closed_slots"] += coverage.closed_slots
            existing["missing_interval_count"] += len(coverage.missing_intervals)
            existing["closed_interval_count"] += len(coverage.closed_intervals)
            existing["out_of_session_bars"] += coverage.out_of_session_bars
    output: list[dict[str, Any]] = []
    for pair in sorted(rows):
        row = rows[pair]
        denominator = row["market_open_slots"]
        row["coverage_ratio"] = (
            Decimal(row["covered_slots"]) / Decimal(denominator)
            if denominator
            else ZERO
        )
        output.append(row)
    return tuple(output)


def exposure_summary(phase: PhaseResult) -> dict[str, Any]:
    """Portfolio-level exposure summary for one phase."""

    return {
        "max_gross_exposure_account": phase.max_gross_exposure_account,
        "max_adjusted_exposure_account": phase.max_adjusted_exposure_account,
        "max_currency_exposure_account": phase.max_currency_exposure_account,
        "peak_currency_exposure": {
            str(currency): value
            for currency, value in phase.peak_currency_exposure.items()
        },
        "final_open_positions": len(phase.open_positions),
        "turnover_account": phase.turnover_account,
        "turnover_ratio": phase.turnover_ratio,
        "costs_account": phase.costs_account,
        "max_drawdown_account": phase.max_drawdown_account,
        "max_drawdown_fraction": phase.max_drawdown_fraction,
    }


def positive_fold_ratio(folds: Iterable[Any]) -> Decimal:
    items = tuple(folds)
    if not items:
        return ZERO
    positives = sum(1 for fold in items if bool(getattr(fold, "positive", False)))
    return Decimal(positives) / Decimal(len(items))


def metric_table(metrics: Iterable[GroupMetric]) -> list[dict[str, Any]]:
    return [metric.audit_view() for metric in metrics]
