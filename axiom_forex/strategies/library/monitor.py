"""Screen attempts, and explain -- in the operator's terms -- why one passed.

The screen is **predeclared**: the criteria are fixed before any result is read,
and changing one invalidates the attempts made under the old version.  It reuses
the paper lane's own evaluation output (the chronological train/validation/locked
test payload from :mod:`axiom_forex.paper_runner.evaluation`) rather than running a
second, differently-shaped backtest: two engines disagreeing about the same bars is
how a strategy ends up "validated" twice.

Two levels of control:

* **per attempt** -- enough closed trades, bounded drawdown, walk-forward folds,
  a positive validation segment and a positive locked test, a promoting data class,
  a bootstrap p-value and an FDR-adjusted q-value;
* **per search** -- White's Reality Check across every variant tried on the same
  data, so "the best of twenty variants" is not read as one discovery.

The result is one of ``ELIGIBLE``, ``REJECTED`` or ``INSUFFICIENT_EVIDENCE`` and a
list of checks with the numbers behind each verdict.  ``ELIGIBLE`` means the
predeclared bars were cleared on a historical sample; it is still not a promise,
and it still cannot trade without the operator selecting it and the live gate
agreeing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Sequence

from axiom_forex.paper_runner.strategy import PROMOTING_DATA_CLASSES

from .cards import Capabilities, DEFAULT_CAPABILITIES, card
from .ledger import ELIGIBLE, INSUFFICIENT, REJECTED
from .multiplicity import benjamini_hochberg, reality_check

CRITERIA_VERSION = "strategy-library-criteria-v1"

#: Thresholds are AXIOM's choices, not facts about markets.  They are deliberately
#: conservative and public, so a reader can disagree and re-run.
DEFAULT_MIN_TRADES = 30
DEFAULT_MAX_DRAWDOWN = Decimal("0.10")
DEFAULT_MIN_FOLDS = 3
DEFAULT_MIN_POSITIVE_FOLDS = Decimal("0.60")
DEFAULT_MAX_Q = Decimal("0.10")
DEFAULT_MAX_REALITY_CHECK = Decimal("0.05")


@dataclass(frozen=True)
class ScreenCriteria:
    min_closed_trades: int = DEFAULT_MIN_TRADES
    max_drawdown_fraction: Decimal = DEFAULT_MAX_DRAWDOWN
    min_walk_forward_folds: int = DEFAULT_MIN_FOLDS
    min_positive_fold_fraction: Decimal = DEFAULT_MIN_POSITIVE_FOLDS
    max_q_value: Decimal = DEFAULT_MAX_Q
    max_reality_check: Decimal = DEFAULT_MAX_REALITY_CHECK
    require_positive_validation: bool = True
    require_positive_locked_test: bool = True
    require_promoting_data_class: bool = True

    @property
    def version(self) -> str:
        return CRITERIA_VERSION

    def audit_view(self) -> dict[str, Any]:
        return {
            "criteria_version": CRITERIA_VERSION,
            "min_closed_trades": self.min_closed_trades,
            "max_drawdown_fraction": str(self.max_drawdown_fraction),
            "min_walk_forward_folds": self.min_walk_forward_folds,
            "min_positive_fold_fraction": str(self.min_positive_fold_fraction),
            "max_q_value": str(self.max_q_value),
            "max_reality_check": str(self.max_reality_check),
            "require_positive_validation": self.require_positive_validation,
            "require_positive_locked_test": self.require_positive_locked_test,
            "require_promoting_data_class": self.require_promoting_data_class,
        }


DEFAULT_CRITERIA = ScreenCriteria()


def _dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _check(name: str, passed: bool, detail: str, *, required: bool = True) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "detail": detail, "required": required}


def screen(
    evidence: Mapping[str, Any],
    *,
    criteria: ScreenCriteria = DEFAULT_CRITERIA,
    q_value: Any = None,
    reality_check_p: Any = None,
) -> dict[str, Any]:
    """Apply the predeclared criteria to one recorded evaluation payload."""

    payload = dict(evidence or {})
    checks: list[dict[str, Any]] = []
    data_class = str(payload.get("data_class", "") or "").upper()
    promoting = data_class in PROMOTING_DATA_CLASSES
    checks.append(
        _check(
            "data_class_can_promote",
            promoting or not criteria.require_promoting_data_class,
            (
                f"{data_class or 'unknown data class'}: only "
                + ", ".join(sorted(PROMOTING_DATA_CLASSES))
                + " can support a promotion"
            ),
            required=criteria.require_promoting_data_class,
        )
    )

    if not payload.get("evaluated", False):
        checks.append(_check("evaluated", False, "no evaluation is recorded for this variant"))
        return _verdict(checks, criteria, INSUFFICIENT, "no evaluation is recorded")

    metrics = dict(payload.get("metrics") or {})
    aggregate = dict(payload.get("aggregate") or {})
    locked = dict(metrics.get("locked_test") or {})
    summary = dict(payload.get("walk_forward_summary") or {})
    failed = [str(item) for item in payload.get("failed_criteria") or ()]

    trades = int(locked.get("closed_trades", aggregate.get("closed_trades", 0)) or 0)
    checks.append(
        _check(
            "enough_closed_trades",
            trades >= criteria.min_closed_trades,
            f"{trades} closed trade(s) in the locked test; the bar is {criteria.min_closed_trades}",
        )
    )

    drawdown = _dec(locked.get("max_drawdown_fraction", aggregate.get("max_drawdown_fraction")))
    checks.append(
        _check(
            "drawdown_within_limit",
            drawdown is not None and drawdown <= criteria.max_drawdown_fraction,
            (
                "no drawdown figure was produced"
                if drawdown is None
                else f"max drawdown {drawdown} against a limit of {criteria.max_drawdown_fraction}"
            ),
        )
    )

    folds = int(summary.get("folds", 0) or 0)
    checks.append(
        _check(
            "walk_forward_folds",
            folds >= criteria.min_walk_forward_folds,
            f"{folds} walk-forward fold(s); the bar is {criteria.min_walk_forward_folds}",
        )
    )
    positive = _dec(summary.get("positive_fraction"))
    checks.append(
        _check(
            "walk_forward_positive_folds",
            positive is not None and positive >= criteria.min_positive_fold_fraction,
            (
                "no fold summary was produced"
                if positive is None
                else f"{positive} of folds were positive; the bar is {criteria.min_positive_fold_fraction}"
            ),
        )
    )

    validation = _dec((metrics.get("validation") or {}).get("net_pnl_account"))
    checks.append(
        _check(
            "validation_positive",
            (not criteria.require_positive_validation)
            or (validation is not None and validation > 0),
            "no validation result" if validation is None else f"validation net {validation}",
            required=criteria.require_positive_validation,
        )
    )
    locked_net = _dec(locked.get("net_pnl_account", (metrics.get("locked_test") or {}).get("net_pnl_account")))
    checks.append(
        _check(
            "locked_test_positive",
            (not criteria.require_positive_locked_test) or (locked_net is not None and locked_net > 0),
            "no locked-test result" if locked_net is None else f"locked-test net {locked_net}",
            required=criteria.require_positive_locked_test,
        )
    )
    checks.append(
        _check(
            "no_gate_rejections_in_test",
            "gate_rejections_in_test_window" not in failed,
            "the test window contains orders the risk gate refused",
            required=False,
        )
    )

    q = _dec(q_value)
    checks.append(
        _check(
            "multiple_testing_q_value",
            q is not None and q <= criteria.max_q_value,
            (
                "no multiple-testing correction was available"
                if q is None
                else f"q={q} against a limit of {criteria.max_q_value}"
            ),
        )
    )
    rc = _dec(reality_check_p)
    checks.append(
        _check(
            "reality_check",
            rc is not None and rc <= criteria.max_reality_check,
            (
                "no Reality Check was available"
                if rc is None
                else f"p={rc} against a limit of {criteria.max_reality_check}"
            ),
        )
    )

    blocking = [check for check in checks if check["required"] and not check["passed"]]
    if blocking:
        reason = "; ".join(f"{item['name']}: {item['detail']}" for item in blocking)
        return _verdict(checks, criteria, REJECTED, reason)
    return _verdict(checks, criteria, ELIGIBLE, "every predeclared criterion passed on this sample")


def _verdict(
    checks: Sequence[Mapping[str, Any]],
    criteria: ScreenCriteria,
    status: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "status": status,
        "reason": reason,
        "checks": [dict(item) for item in checks],
        "criteria": criteria.audit_view(),
        "eligible": status == ELIGIBLE,
    }


def explain(verdict: Mapping[str, Any]) -> str:
    """One sentence for the page, with no adjective it cannot support."""

    status = str(verdict.get("status", ""))
    if status == ELIGIBLE:
        return (
            "Every predeclared criterion passed on this historical sample. That is "
            "evidence about the past, not a forecast, and not a promise of profit."
        )
    if status == REJECTED:
        return "Not eligible: " + str(verdict.get("reason", "")) + "."
    return (
        "There is not enough evidence to judge this variant"
        + (": " + str(verdict.get("reason", "")) if verdict.get("reason") else "")
        + "."
    )


# ---------------------------------------------------------------------------
# the search, not just one attempt
# ---------------------------------------------------------------------------


@dataclass
class AttemptInput:
    """One variant's evaluation on one dataset, before the search is judged."""

    variant_id: str
    card_id: str
    evidence: Mapping[str, Any]
    returns: Sequence[float] = ()
    symbol: str = ""
    timeframe: str = ""


def score_search(
    attempts: Iterable[AttemptInput],
    *,
    criteria: ScreenCriteria = DEFAULT_CRITERIA,
    q: Decimal = DEFAULT_MAX_Q,
    reality_check_resamples: int = 1000,
) -> dict[str, Any]:
    """Apply FDR and the Reality Check across the whole search, then screen each.

    Grouping is by card and dataset: variants of *different* templates are not a
    family that should be corrected against each other, while variants of the same
    template searching the same bars are exactly the multiple-testing problem.
    """

    items = list(attempts)
    groups: dict[tuple[str, str], list[AttemptInput]] = {}
    for item in items:
        fingerprint = str((item.evidence or {}).get("dataset_id", "") or "unknown")
        groups.setdefault((item.card_id, fingerprint), []).append(item)

    results: list[dict[str, Any]] = []
    for (card_id, dataset_id), members in groups.items():
        p_values: dict[str, float] = {}
        series: dict[str, Sequence[float]] = {}
        for member in members:
            p_value = _dec(member.evidence.get("bootstrap_p_value"))
            if p_value is None and member.returns:
                from .multiplicity import block_bootstrap_p_value

                p_value = _dec(block_bootstrap_p_value(member.returns)["p_value"])
            if p_value is not None:
                p_values[member.variant_id] = float(p_value)
            if member.returns:
                series[member.variant_id] = list(member.returns)
        correction = benjamini_hochberg(p_values, q=float(q)) if p_values else {"q_values": {}, "accepted": []}
        # With a single series the Reality Check reduces to that series' own
        # bootstrap, which is the right answer: one attempt, one statistic.
        rc = reality_check(series) if series else {"p_value": None}
        for member in members:
            verdict = screen(
                member.evidence,
                criteria=criteria,
                q_value=correction["q_values"].get(member.variant_id),
                reality_check_p=rc.get("p_value"),
            )
            results.append(
                {
                    "variant_id": member.variant_id,
                    "card_id": card_id,
                    "dataset_id": dataset_id,
                    "symbol": member.symbol,
                    "timeframe": member.timeframe,
                    "q_value": correction["q_values"].get(member.variant_id, ""),
                    "reality_check_p_value": rc.get("p_value") or "",
                    "verdict": verdict,
                    "explanation": explain(verdict),
                }
            )
    return {
        "attempts": len(results),
        "templates": sorted({item["card_id"] for item in results}),
        "accepted": [item for item in results if item["verdict"]["status"] == ELIGIBLE],
        "results": results,
        "note": (
            "Selection bias is controlled, not removed: FDR corrects for the number "
            "of variants tried and the Reality Check asks whether the best of them "
            "beats luck. Neither makes a historical result predictive."
        ),
    }


# ---------------------------------------------------------------------------
# coverage: which symbols and timeframes can even be analysed
# ---------------------------------------------------------------------------


def coverage(
    *,
    symbols: Sequence[str],
    timeframes: Sequence[str],
    bar_counts: Mapping[tuple[str, str], int] | None = None,
    capabilities: Capabilities = DEFAULT_CAPABILITIES,
) -> list[dict[str, Any]]:
    """What would be monitored, and what is ignored, per symbol and timeframe.

    A timeframe no template declares is *not* analysed: pretending a bar feed
    supports a decision the strategy was never described for is the mistake this
    function exists to make visible.
    """

    from .cards import CARDS

    counts = dict(bar_counts or {})
    rows: list[dict[str, Any]] = []
    for symbol in symbols:
        for timeframe in timeframes:
            supported = [
                item.card_id
                for item in CARDS
                if item.supports(timeframe) and item.availability(capabilities)["evaluable"]
            ]
            bars = int(counts.get((symbol, timeframe), 0) or 0)
            enough = bars >= max((item.min_bars for item in CARDS if item.card_id in supported), default=0) if supported else False
            if not supported:
                reason = (
                    f"no template declares {timeframe}; a bar feed is not treated as "
                    "supporting a decision the strategy never described"
                )
            elif not enough:
                reason = f"{bars} bar(s) available, fewer than the templates' minimum"
            else:
                reason = ""
            rows.append(
                {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "templates": supported,
                    "bars": bars,
                    "monitorable": bool(supported) and enough,
                    "reason": reason,
                }
            )
    return rows


__all__ = [
    "AttemptInput",
    "CRITERIA_VERSION",
    "DEFAULT_CRITERIA",
    "ScreenCriteria",
    "coverage",
    "explain",
    "screen",
    "score_search",
]
