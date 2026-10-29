"""Operator-facing summaries for the research evaluation.

The Markdown section is appended to the existing platform report and shows the
dataset class and provenance for every result.  No local file paths, credential
values, or account details are included.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .contracts import GroupMetric, ResearchEvaluationReport


def _money(value: Any) -> str:
    return "n/a" if value is None else str(value)


def _metric_lines(title: str, metrics: tuple[GroupMetric, ...]) -> list[str]:
    lines = [f"### {title}", ""]
    if not metrics:
        lines.extend(["No closed trades were recorded in the evaluated windows.", ""])
        return lines
    lines.append(
        "| Key | Trades | Win ratio | Gross P&L | Costs | Net P&L | Exposure | Max DD |"
    )
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for metric in metrics:
        lines.append(
            "| {key} | {trades} | {win} | {gross} | {costs} | {net} | {exposure} | {dd} |".format(
                key=metric.key,
                trades=metric.trades,
                win=f"{metric.win_ratio:.3f}",
                gross=_money(metric.gross_pnl_account),
                costs=_money(metric.costs_account),
                net=_money(metric.net_pnl_account),
                exposure=_money(metric.exposure_account),
                dd=_money(metric.max_drawdown_account),
            )
        )
    lines.append("")
    return lines


def evaluation_lines(report: ResearchEvaluationReport) -> list[str]:
    view = report.audit_view()
    eligibility = view.get("eligibility", {})
    alignment = view.get("alignment", {})
    qualification = view.get("qualification", {})
    portfolio = report.portfolio_metrics
    lines = [
        "## Research evaluation (multi-pair, imported data)",
        "",
        f"- Evaluation: `{view.get('evaluation_id')}`",
        f"- Mode: `{view.get('mode')}` (research only, no orders)",
        f"- Status: `{view.get('status')}` ({view.get('reason')})",
        f"- Data class: `{view.get('data_class')}`",
        f"- Pairs: `{', '.join(sorted(item['pair'] for item in report.datasets)) or 'none'}`",
        f"- Aligned grid: `{alignment.get('grid_slots', 0)}` slots "
        f"({alignment.get('grid_start')} to {alignment.get('grid_end')}, "
        f"{alignment.get('grid_seconds')}s)",
        f"- All-pairs-open slots: `{alignment.get('all_pairs_open_slots', 0)}`; "
        f"partly-open: `{alignment.get('partly_open_slots', 0)}`; "
        f"closed: `{alignment.get('closed_slots', 0)}`",
        f"- Data eligibility: `{'PASS' if eligibility.get('evaluation_allowed') else 'BLOCKED'}`",
        f"- Qualification allowed: `{eligibility.get('qualification_allowed')}`",
        f"- Strategy activation: `never in research evaluation`",
        f"- Qualification: `{qualification.get('status')}`",
        f"- Portfolio trades: `{portfolio.trades}`; net P&L: `{portfolio.net_pnl_account}`; "
        f"costs: `{portfolio.costs_account}`; max drawdown: `{portfolio.max_drawdown_account}`",
        "",
        "Provenance and dataset class per pair:",
        "",
        "| Pair | Timeframe | Data class | Source | Source version | Bars | Coverage | Import manifest |",
        "| --- | --- | --- | --- | --- | ---: | ---: | --- |",
    ]
    coverage = {row["pair"]: row for row in _coverage_rows(report)}
    for item in view.get("datasets", []):
        pair = item.get("pair")
        row = coverage.get(pair, {})
        lines.append(
            "| {pair} | {tf} | {cls} | {src} | {ver} | {bars} | {cov} | {manifest} |".format(
                pair=pair,
                tf=item.get("timeframe"),
                cls=item.get("data_class"),
                src=_provenance_value(report, pair, "source"),
                ver=_provenance_value(report, pair, "source_version"),
                bars=item.get("bars"),
                cov=f"{row.get('coverage_ratio', 'n/a')}",
                manifest="VALID" if item.get("import_manifest_valid") else "INVALID",
            )
        )
    lines.append("")
    details = view.get("eligibility", {}).get("findings", [])
    if details:
        lines.extend(["### Data eligibility findings", ""])
        for finding in details:
            pair = f" ({finding['pair']})" if finding.get("pair") else ""
            lines.append(
                f"- `{finding['code']}`{pair}: {finding['severity']} — {finding['detail']}"
            )
        lines.append("")
    split = view.get("split", {})
    if split:
        lines.extend(["### Chronological split", ""])
        for label, window in split.items():
            lines.append(
                f"- `{label}`: {window['start']} to {window['end']} "
                f"({window['duration_seconds']}s)"
            )
        lines.append("")
    reasons = view.get("qualification", {}).get("reasons", [])
    if reasons:
        lines.extend(["### Qualification reasons", ""])
        for reason in reasons:
            lines.append(f"- `{reason}`")
        lines.append("")
    lines.extend(_metric_lines("Performance by pair", report.pair_metrics))
    lines.extend(_metric_lines("Performance by session", report.session_metrics))
    lines.extend(_metric_lines("Performance by regime", report.regime_metrics))
    lines.extend(
        [
            "### Portfolio risk and cost summary",
            "",
            f"- Gross P&L: `{portfolio.gross_pnl_account}`",
            f"- Costs: `{portfolio.costs_account}`",
            f"- Turnover: `{portfolio.turnover_account}`",
            f"- Max drawdown: `{portfolio.max_drawdown_account}` "
            f"({portfolio.max_drawdown_fraction:.3f} of peak equity)",
        ]
    )
    if report.folds:
        lines.append("")
        lines.append("### Walk-forward out-of-sample folds")
        lines.append("")
        lines.append("| Fold | Test start | Test end | Trades | Net P&L | Positive |")
        lines.append("| ---: | --- | --- | ---: | ---: | --- |")
        for fold in report.folds:
            lines.append(
                f"| {fold.fold} | {fold.test.window.start} | {fold.test.window.end} | "
                f"{fold.test.trade_count} | {fold.test.net_pnl_account} | "
                f"{'yes' if fold.positive else 'no'} |"
            )
    lines.extend(
        [
            "",
            "Every figure above is a historical simulation over the declared data "
            "class. It is not a profitability or live-readiness claim, and this "
            "evaluation never creates orders or enables external execution.",
            "",
        ]
    )
    return lines


def _provenance_value(
    report: ResearchEvaluationReport, pair: str | None, key: str
) -> str:
    for item in report.provenance:
        if item.get("pair") == pair:
            return str(item.get(key, "n/a"))
    return "n/a"


def _coverage_rows(report: ResearchEvaluationReport) -> list[dict[str, Any]]:
    """Coverage from executed phases, or from alignment when nothing ran."""

    from .metrics import aggregate_coverage

    phases = tuple(report.phases.values()) + tuple(
        phase for fold in report.folds for phase in (fold.train, fold.validation, fold.test)
    )
    if phases:
        return [dict(row) for row in aggregate_coverage(phases)]
    return [
        {
            "pair": coverage.pair,
            "timeframe": coverage.timeframe,
            "coverage_ratio": coverage.coverage_ratio,
            "missing_interval_count": len(coverage.missing_intervals),
            "closed_interval_count": len(coverage.closed_intervals),
            "out_of_session_bars": coverage.out_of_session_bars,
        }
        for coverage in report.alignment.pairs
    ]


def render_evaluation_markdown(report: ResearchEvaluationReport) -> str:
    return "\n".join(evaluation_lines(report)) + "\n"


def evaluation_summary(report: ResearchEvaluationReport) -> Mapping[str, Any]:
    """Compact machine summary for CLI/status surfaces."""

    view = report.audit_view()
    return {
        "evaluation_id": view.get("evaluation_id"),
        "status": view.get("status"),
        "passed": view.get("passed"),
        "reason": view.get("reason"),
        "mode": view.get("mode"),
        "data_class": view.get("data_class"),
        "pairs": [item.get("pair") for item in view.get("datasets", [])],
        "eligibility": view.get("eligibility", {}),
        "alignment": {
            key: view.get("alignment", {}).get(key)
            for key in (
                "grid_seconds",
                "grid_slots",
                "aligned_slots",
                "all_pairs_open_slots",
                "partly_open_slots",
                "closed_slots",
                "issues",
            )
        },
        "qualification": view.get("qualification", {}),
        "portfolio_metrics": view.get("portfolio_metrics", {}),
        "execution_isolation": view.get("execution_isolation", {}),
        "note": view.get("note"),
    }


# -- stress and sensitivity reporting --------------------------------------


def _delta(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):  # defensive rendering only
        return str(value)
    if number == 0:
        return "0"
    return f"{number:+.6f}".rstrip("0").rstrip(".")


def stress_suite_lines(suite: Any) -> list[str]:
    """Render the stress suite alongside the baseline evaluation section."""

    lines = [
        "## Stress and sensitivity suite (hypothetical scenarios)",
        "",
        f"- Mode: `{suite.mode}` (never qualifies or activates)",
        f"- Status: `{suite.status}`; baseline `{suite.baseline.evaluation_id}` "
        f"({suite.baseline.evaluation_data_class})",
        f"- Scenarios: `{len(suite.scenarios)}`; hypothetical: "
        f"`{', '.join(suite.hypothetical_scenarios) or 'none'}`",
        "",
        "Every scenario reruns the same pinned train/validation/test windows and "
        "walk-forward folds as the baseline, with unchanged strategy parameters "
        "and research sizes. Scenarios are sensitivity measurements only: they "
        "claim no out-of-sample window and are never used for selection.",
        "",
        "| Scenario | Kind | Evidence | Status | ΔTrades | ΔCosts | ΔNet P&L | ΔMax DD | ΔExposure | ΔMargin | ΔCoverage | ΔRejections |",
        "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for run in suite.scenarios:
        portfolio = run.comparisons[0]
        deltas = portfolio.deltas
        lines.append(
            "| {sid} | {kind} | {evidence} | {status} | {trades} | {costs} | {net} | "
            "{dd} | {exposure} | {margin} | {coverage} | {rejections} |".format(
                sid=run.scenario.scenario_id,
                kind=run.scenario.kind,
                evidence=run.scenario.evidence_class,
                status=run.status,
                trades=_delta(deltas.get("trades")),
                costs=_delta(deltas.get("costs_account")),
                net=_delta(deltas.get("net_pnl_account")),
                dd=_delta(deltas.get("max_drawdown_account")),
                exposure=_delta(deltas.get("max_exposure_account")),
                margin=_delta(deltas.get("max_margin_account")),
                coverage=_delta(deltas.get("min_coverage_ratio")),
                rejections=_delta(deltas.get("rejection_count")),
            )
        )
    lines.append("")

    blocked = [run for run in suite.scenarios if run.status != "PASS"]
    if blocked:
        lines.extend(["### Fail-closed scenarios", ""])
        for run in blocked:
            lines.append(
                f"- `{run.scenario.scenario_id}`: BLOCKED — "
                + ", ".join(f"`{reason}`" for reason in run.blocked_reasons)
            )
        lines.append("")

    stressed_rejections = [
        (run.scenario.scenario_id, run.rejections)
        for run in suite.scenarios
        if run.rejections
    ]
    if stressed_rejections:
        lines.extend(["### Rejections under stress", ""])
        for scenario_id, counts in stressed_rejections:
            detail = ", ".join(f"{code}={count}" for code, count in counts.items())
            lines.append(f"- `{scenario_id}`: {detail}")
        lines.append("")

    lines.extend(["### Assumptions (explicit, never inferred)", ""])
    for run in suite.scenarios:
        parameters = ", ".join(
            f"{key}={value}" for key, value in sorted(run.scenario.parameters.items())
        )
        reference = (
            f" (reference: `{run.scenario.evidence_reference}`)"
            if run.scenario.evidence_reference
            else ""
        )
        lines.append(
            f"- `{run.scenario.scenario_id}`: {run.scenario.evidence_class}"
            f"{reference} — {run.scenario.description} [{parameters}]"
        )
    lines.append("")

    lines.extend(
        [
            "Observed historical inputs are the imported datasets behind the "
            "baseline run; everything in this section is a declared assumption "
            "applied on top of them. Per-pair comparisons are in the machine "
            "readable report (`/evaluation`, CLI JSON), including coverage and "
            "rejection counts per pair.",
            "",
            "Stress results are hypothetical reconstructions. They are not "
            "evidence of profitability, not a live-readiness claim, and they "
            "cannot qualify or activate a strategy.",
            "",
        ]
    )
    return lines


def render_stress_suite_markdown(suite: Any) -> str:
    return "\n".join(stress_suite_lines(suite)) + "\n"


def stress_suite_summary(suite: Any) -> Mapping[str, Any]:
    """Compact stress view for CLI/status surfaces."""

    return {
        "mode": suite.mode,
        "status": suite.status,
        "passed": suite.passed,
        "reasons": list(suite.reasons),
        "baseline_evaluation_id": suite.baseline.evaluation_id,
        "baseline_data_class": suite.baseline.evaluation_data_class,
        "hypothetical_scenarios": list(suite.hypothetical_scenarios),
        "scenarios": [
            {
                "scenario_id": run.scenario.scenario_id,
                "kind": run.scenario.kind,
                "evidence_class": run.scenario.evidence_class,
                "evidence_reference": run.scenario.evidence_reference,
                "parameters": dict(run.scenario.parameters),
                "status": run.status,
                "blocked_reasons": list(run.blocked_reasons),
                "rejections": dict(run.rejections),
                "qualification_status": run.report.qualification.status,
                "selection_allowed": run.report.selection_allowed,
                "comparisons": [
                    comparison.audit_view() for comparison in run.comparisons
                ],
            }
            for run in suite.scenarios
        ],
        "execution_isolation": suite.audit_view()["execution_isolation"],
        "note": suite.note,
    }
