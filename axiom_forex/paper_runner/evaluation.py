"""Chronological evaluation: train, select, then a locked later test window.

The order is fixed and the test window is spent once:

1. **Train** (earliest 60%) -- where a candidate may be looked at.
2. **Validation** (next 20%) -- where the candidate is *chosen*.
3. **Locked test** (last 20%) -- spent once, at the end, and never again for this
   dataset.  A second run against the same dataset is refused, because a test
   window you can re-run is not a test window.

Every number comes from the repository's own backtest engine, so fills happen at
the next bar's ask (buy) or bid (sell), with the configured spread already inside
the bid/ask data, plus commission, slippage and swap through the platform's cost
model and rollover engine.  Nothing here is allowed to invent a price, a cost, or
a calendar.

Predeclared criteria live in :class:`Criteria` and are fixed before any result is
seen.  A candidate that misses one is rejected; it is not moved to a different
window and retried.  Synthetic fixtures and demo exports can be evaluated for
sanity, but they can *never* promote a candidate: evidence about a fixture is not
evidence about a broker's market.

The walk-forward pass re-runs the candidate on rolling later windows; it is
reported alongside, and it can only *lower* confidence, never raise it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Mapping, Sequence

from app.data.models import ForexQuoteBar

from axiom_forex.backtesting.engine import BacktestResult, ForexBacktestEngine
from axiom_forex.backtesting.validation import run_oos
from axiom_forex.validation.split import LockedTest, chronological_split
from axiom_forex.validation.walk_forward import walk_forward

from .strategy import BASELINES, PROMOTING_DATA_CLASSES, Candidate

KIND_EVALUATION = "paper_runner_evaluation"
KIND_LOCKED_TEST = "paper_runner_locked_test_use"

#: Fractions of the sample: train, selection, locked test.
SPLIT = (Decimal("0.6"), Decimal("0.2"), Decimal("0.2"))


@dataclass(frozen=True)
class Criteria:
    """Declared before any result is seen; changing one invalidates prior runs."""

    min_closed_trades: int = 30
    max_drawdown_fraction: Decimal = Decimal("0.10")
    min_walk_forward_folds: int = 3
    min_positive_fold_fraction: Decimal = Decimal("0.60")
    require_positive_validation: bool = True
    require_positive_test: bool = True
    require_locked_test_never_reused: bool = True

    def audit_view(self) -> dict[str, Any]:
        return {
            "min_closed_trades": self.min_closed_trades,
            "max_drawdown_fraction": str(self.max_drawdown_fraction),
            "min_walk_forward_folds": self.min_walk_forward_folds,
            "min_positive_fold_fraction": str(self.min_positive_fold_fraction),
            "require_positive_validation": self.require_positive_validation,
            "require_positive_test": self.require_positive_test,
            "require_locked_test_never_reused": self.require_locked_test_never_reused,
        }


DEFAULT_CRITERIA = Criteria()

ASSUMPTIONS: tuple[str, ...] = (
    "Fills are the next bar's ask for a buy and bid for a sell; a decision never "
    "fills at its own bar's price.",
    "The spread is whatever the imported bid/ask data contains. It is not "
    "reconstructed, widened, or narrowed.",
    "Commission, slippage and swap come from the platform's configured cost "
    "model and rollover engine; they are not estimated here.",
    "A missing bar rejects the run instead of being interpolated.",
    "Segments are chronological and never shuffled.",
    "The locked test window is used once per dataset, at the end, and is never "
    "used for tuning.",
)


def _dec(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _closes(result: BacktestResult) -> list[dict[str, Any]]:
    return [
        dict(trade)
        for trade in result.trades
        if str((trade.get("fill") or {}).get("action", "")).upper() == "CLOSE"
    ]


def summarize(result: BacktestResult) -> dict[str, Any]:
    """Win rate next to net expectancy after costs, drawdown, and losing periods."""

    closes = _closes(result)
    wins = [
        trade
        for trade in closes
        if _dec((trade.get("fill") or {}).get("realized_pnl_account", 0)) > 0
    ]
    initial = _dec(result.initial_equity_account)
    drawdown = _dec(result.max_drawdown_account)
    streak = worst = 0
    for trade in closes:
        if _dec((trade.get("fill") or {}).get("realized_pnl_account", 0)) < 0:
            streak += 1
            worst = max(worst, streak)
        else:
            streak = 0
    days: dict[str, Decimal] = {}
    for point in result.equity_curve:
        stamp = point.get("timestamp")
        if not isinstance(stamp, datetime):
            continue
        day = stamp.astimezone(timezone.utc).date().isoformat()
        days[day] = _dec(point["equity_account"])
    ordered = [days[key] for key in sorted(days)]
    losing_days = sum(
        1 for previous, current in zip(ordered, ordered[1:]) if current < previous
    )
    net = _dec(result.net_pnl_account)
    return {
        "usable": result.usable,
        "quality": str(getattr(result.quality, "classification", "")),
        "fills": len(result.trades),
        "closed_trades": len(closes),
        "win_rate": (
            f"{(Decimal(len(wins)) / Decimal(len(closes))):.4f}" if closes else ""
        ),
        "net_pnl_account": str(net),
        "net_return_fraction": f"{(net / initial):.6f}" if initial else "",
        "expectancy_per_trade_account": (
            f"{(net / Decimal(len(closes))):.6f}" if closes else ""
        ),
        "cost_total_account": str(result.cost_total_account),
        "rollover_total_account": str(result.rollover_total_account),
        "max_drawdown_account": str(drawdown),
        "max_drawdown_fraction": (
            f"{(drawdown / initial):.6f}" if initial and initial != 0 else ""
        ),
        "max_losing_streak": worst,
        "losing_days": losing_days,
        "observed_days": len(days),
        "open_positions": len(result.open_positions),
        "rejections": len(result.rejections),
        "rollover_applied": _dec(result.rollover_total_account) != 0,
    }


def _window(bars: Sequence[ForexQuoteBar]) -> dict[str, str]:
    if not bars:
        return {}
    return {
        "first_bar_start": bars[0].timestamp.isoformat(),
        "last_bar_end": bars[-1].end_at.isoformat(),
        "bars": str(len(bars)),
    }


@dataclass
class EvaluationOutcome:
    payload: dict[str, Any]
    result: BacktestResult | None = None
    test_result: BacktestResult | None = None

    def audit_view(self) -> dict[str, Any]:
        return dict(self.payload)


def evaluate_candidate(
    *,
    engine_factory: Callable[[], ForexBacktestEngine],
    candidate: Candidate,
    bars: Sequence[ForexQuoteBar],
    data_class: str,
    dataset_id: str = "unspecified",
    criteria: Criteria = DEFAULT_CRITERIA,
    walk_forward_folds: int | None = None,
    clock: Callable[[], datetime] | None = None,
) -> EvaluationOutcome:
    """Evaluate one candidate chronologically and return its evidence.

    The result is evidence, not a promise: ``passed`` means only that this
    candidate cleared every predeclared bar on *this* sample.
    """

    strategy = candidate.build()
    ordered = tuple(sorted(bars, key=lambda bar: bar.timestamp))
    payload: dict[str, Any] = {
        "evaluated": True,
        "candidate_id": candidate.candidate_id,
        "dataset_id": dataset_id,
        "data_class": str(data_class).upper(),
        "generated_at": (clock or (lambda: datetime.now(timezone.utc)))().isoformat(),
        "criteria": criteria.audit_view(),
        "criteria_version": "paper-runner-criteria-v1",
        "assumptions": list(ASSUMPTIONS),
        "split_fractions": [str(value) for value in SPLIT],
        "promoting_data_class": str(data_class).upper() in PROMOTING_DATA_CLASSES,
        "passed": False,
        "failed_criteria": [],
        "ineligible_reason": "",
        "windows": {},
        "metrics": {},
        "walk_forward": [],
        "baselines": {},
        "locked_test_consumed": False,
    }
    failed: list[str] = payload["failed_criteria"]

    minimum = int(getattr(strategy, "history_limit", 1)) * 2 + 10
    if len(ordered) < minimum:
        payload["ineligible_reason"] = "insufficient_bars"
        failed.append("insufficient_bars")
        payload["windows"] = {"all": _window(ordered)}
        return EvaluationOutcome(payload)
    if not payload["promoting_data_class"]:
        # Sanity numbers are still produced -- they are simply not evidence.
        payload["ineligible_reason"] = "data_class_cannot_promote"

    split = chronological_split(ordered, SPLIT)
    payload["windows"] = {
        "train": _window(split.train),
        "validation": _window(split.validation),
        "locked_test": _window(split.test),
    }
    locked = LockedTest(split.test)
    test_bars = locked.use_once()
    payload["locked_test_consumed"] = locked.used

    try:
        oos = run_oos(
            engine_factory,
            strategy,
            train_bars=split.train,
            validation_bars=split.validation,
            test_bars=test_bars,
        )
    except Exception as exc:
        payload["ineligible_reason"] = f"backtest_failed:{type(exc).__name__}"
        failed.append("backtest_failed")
        return EvaluationOutcome(payload)

    payload["metrics"]["train"] = _segment_summary(engine_factory, split.train, candidate)
    payload["metrics"]["validation"] = {
        "net_pnl_account": str(oos.validation_net_pnl_account),
    }
    payload["metrics"]["locked_test"] = {
        "net_pnl_account": str(oos.test_net_pnl_account),
    }
    payload["aggregate"] = summarize(oos.result)

    test_result: BacktestResult | None = None
    try:
        test_result = engine_factory().run(
            test_bars, candidate.build(), run_id=f"{dataset_id}:locked-test"
        )
    except Exception as exc:
        failed.append("locked_test_run_failed")
        payload["ineligible_reason"] = f"locked_test_run_failed:{type(exc).__name__}"
    if test_result is not None:
        test_view = summarize(test_result)
        payload["metrics"]["locked_test"].update(test_view)
        if test_view["closed_trades"] < criteria.min_closed_trades:
            failed.append("too_few_trades")
        drawdown_ratio = test_view["max_drawdown_fraction"]
        if not drawdown_ratio or Decimal(drawdown_ratio) > criteria.max_drawdown_fraction:
            failed.append("drawdown_limit")
        if test_result.rejections:
            failed.append("gate_rejections_in_test_window")

    if not oos.result.usable:
        failed.append("data_quality_not_usable")
    if criteria.require_positive_validation and _dec(oos.validation_net_pnl_account) <= 0:
        failed.append("validation_not_positive")
    if criteria.require_positive_test and _dec(oos.test_net_pnl_account) <= 0:
        failed.append("locked_test_not_positive")

    folds = _walk_forward(
        engine_factory, ordered, candidate, criteria, walk_forward_folds
    )
    payload["walk_forward"] = folds
    positive = sum(1 for fold in folds if _dec(fold["net_pnl_account"]) > 0)
    payload["walk_forward_summary"] = {
        "folds": len(folds),
        "positive_folds": positive,
        "positive_fraction": (
            f"{(Decimal(positive) / Decimal(len(folds))):.4f}" if folds else ""
        ),
    }
    if len(folds) < criteria.min_walk_forward_folds:
        failed.append("walk_forward_too_few_folds")
    elif Decimal(positive) / Decimal(len(folds)) < criteria.min_positive_fold_fraction:
        failed.append("walk_forward_not_robust")

    payload["baselines"] = _baselines(engine_factory, test_bars, candidate, dataset_id)

    payload["passed"] = bool(payload["promoting_data_class"] and not failed)
    return EvaluationOutcome(payload, result=oos.result, test_result=test_result)


def _segment_summary(
    engine_factory: Callable[[], ForexBacktestEngine],
    bars: Sequence[ForexQuoteBar],
    candidate: Candidate,
) -> dict[str, Any]:
    try:
        result = engine_factory().run(
            bars, candidate.build(), run_id=f"{candidate.candidate_id}:train"
        )
    except Exception as exc:
        return {"error": type(exc).__name__}
    return summarize(result)


def _walk_forward(
    engine_factory: Callable[[], ForexBacktestEngine],
    bars: Sequence[ForexQuoteBar],
    candidate: Candidate,
    criteria: Criteria,
    folds: int | None,
) -> list[dict[str, Any]]:
    total = len(bars)
    history = int(getattr(candidate.build(), "history_limit", 1))
    train_size = max(history * 2, int(total * 0.40))
    validation_size = max(history, int(total * 0.10))
    test_size = max(history, int(total * 0.10))
    if train_size + validation_size + test_size > total:
        return []
    out: list[dict[str, Any]] = []
    limit = folds if folds is not None else 6
    for fold in walk_forward(
        bars,
        train_size=train_size,
        validation_size=validation_size,
        test_size=test_size,
    ):
        if len(out) >= limit:
            break
        try:
            result = engine_factory().run(
                fold.test,
                candidate.build(),
                run_id=f"{candidate.candidate_id}:wf{fold.index}",
            )
        except Exception as exc:
            out.append(
                {
                    "fold": fold.index,
                    "error": type(exc).__name__,
                    "net_pnl_account": "0",
                    "closed_trades": 0,
                }
            )
            continue
        view = summarize(result)
        view.update(
            {
                "fold": fold.index,
                "test_window": _window(fold.test),
                "train_window": _window(fold.train),
            }
        )
        out.append(view)
    return out


def _baselines(
    engine_factory: Callable[[], ForexBacktestEngine],
    bars: Sequence[ForexQuoteBar],
    candidate: Candidate,
    dataset_id: str,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for baseline in BASELINES:
        if baseline.candidate_id == candidate.candidate_id:
            continue
        try:
            result = engine_factory().run(
                bars, baseline.build(), run_id=f"{dataset_id}:{baseline.candidate_id}"
            )
        except Exception as exc:
            out[baseline.candidate_id] = {"error": type(exc).__name__}
            continue
        out[baseline.candidate_id] = summarize(result)
    return out


# -- persistence -------------------------------------------------------------


def evidence_view(payload: Mapping[str, Any]) -> dict[str, Any]:
    """The subset the runner reads to decide whether a candidate may run."""

    return {
        "evaluated": bool(payload.get("evaluated", False)),
        "data_class": str(payload.get("data_class", "")),
        "passed": bool(payload.get("passed", False)),
        "failed_criteria": list(payload.get("failed_criteria") or []),
        "dataset_id": str(payload.get("dataset_id", "")),
        "criteria_version": str(payload.get("criteria_version", "")),
        "generated_at": str(payload.get("generated_at", "")),
    }


def record_evaluation(*, repository: Any, payload: Mapping[str, Any]) -> None:
    dataset_id = str(payload.get("dataset_id", "unspecified"))
    candidate_id = str(payload.get("candidate_id", "unspecified"))
    repository.save_snapshot(
        f"{KIND_EVALUATION}:{dataset_id}:{candidate_id}", KIND_EVALUATION, dict(payload)
    )


def claim_locked_test(*, repository: Any, dataset_id: str, at: str) -> None:
    """Spend the locked test window for a dataset.  Refuses a second spend."""

    snapshot_id = f"{KIND_LOCKED_TEST}:{dataset_id}"
    for snapshot in repository.list_snapshots(kind=KIND_LOCKED_TEST):
        if snapshot.get("snapshot_id") == snapshot_id:
            raise ValueError("locked_test_already_consumed")
    repository.save_snapshot(
        snapshot_id, KIND_LOCKED_TEST, {"dataset_id": dataset_id, "consumed_at": at}
    )


def locked_test_consumed(*, repository: Any, dataset_id: str) -> bool:
    snapshot_id = f"{KIND_LOCKED_TEST}:{dataset_id}"
    return any(
        snapshot.get("snapshot_id") == snapshot_id
        for snapshot in repository.list_snapshots(kind=KIND_LOCKED_TEST)
    )


def latest_evidence(*, repository: Any, candidate_id: str) -> dict[str, Any] | None:
    """The newest recorded evidence for a candidate, or ``None``."""

    found: dict[str, Any] | None = None
    for snapshot in repository.list_snapshots(kind=KIND_EVALUATION):
        if str(snapshot.get("candidate_id", "")) == candidate_id:
            found = dict(snapshot)
    return evidence_view(found) if found else None


def comparison_table(payload: Mapping[str, Any]) -> list[dict[str, str]]:
    """Candidate versus each baseline on the same locked test window."""

    rows: list[dict[str, str]] = []
    metrics = (payload.get("metrics") or {}).get("locked_test") or {}
    if metrics:
        rows.append(
            {
                "name": str(payload.get("candidate_id", "")),
                "closed_trades": str(metrics.get("closed_trades", "")),
                "win_rate": str(metrics.get("win_rate", "")),
                "net_pnl_account": str(metrics.get("net_pnl_account", "")),
                "expectancy_per_trade_account": str(
                    metrics.get("expectancy_per_trade_account", "")
                ),
                "max_drawdown_fraction": str(metrics.get("max_drawdown_fraction", "")),
            }
        )
    for name, view in (payload.get("baselines") or {}).items():
        if not isinstance(view, Mapping) or view.get("error"):
            rows.append({"name": name, "closed_trades": "", "win_rate": "", "net_pnl_account": str(view.get("error", "")), "expectancy_per_trade_account": "", "max_drawdown_fraction": ""})
            continue
        rows.append(
            {
                "name": name,
                "closed_trades": str(view.get("closed_trades", "")),
                "win_rate": str(view.get("win_rate", "")),
                "net_pnl_account": str(view.get("net_pnl_account", "")),
                "expectancy_per_trade_account": str(
                    view.get("expectancy_per_trade_account", "")
                ),
                "max_drawdown_fraction": str(view.get("max_drawdown_fraction", "")),
            }
        )
    return rows


__all__ = [
    "ASSUMPTIONS",
    "Criteria",
    "DEFAULT_CRITERIA",
    "EvaluationOutcome",
    "KIND_EVALUATION",
    "KIND_LOCKED_TEST",
    "SPLIT",
    "claim_locked_test",
    "comparison_table",
    "evaluate_candidate",
    "evidence_view",
    "latest_evidence",
    "locked_test_consumed",
    "record_evaluation",
    "summarize",
]
