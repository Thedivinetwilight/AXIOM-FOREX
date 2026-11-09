"""Generate the AX-SCALPER lab report.

The script measures a paper-only desk on ticks it generates itself.  Every number in
the report is reproducible by running this file; none of them is a claim about a live
market.  Training and evaluation windows are separate: the brackets are widened by a
declared arithmetic rule (before any measurement), the outcome book is calibrated on
the training replay, and then — with the posteriors frozen — the desk is run on a
time-shifted evaluation replay.

Usage:
    python scripts/scalper_lab.py [--out docs/SCALPER_LAB_REPORT.md]
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from axiom_forex.scalper import lab, skeptic  # noqa: E402
from axiom_forex.scalper.config import fixture_config  # noqa: E402
from axiom_forex.scalper.desk import DeskThresholds  # noqa: E402

UTC = timezone.utc

#: Declared before the evaluation window is looked at.
TRAIN_START = datetime(2026, 3, 2, 9, 0, 0, tzinfo=UTC)
EVALUATION_START = TRAIN_START + timedelta(days=30)
STARTING_EQUITY = Decimal("10000")

VERIFICATION = {
    "verified": (
        "tick validation, de-duplication, ordering, staleness, gaps, clock reset and "
        "replay, on synthetic and file-based ticks",
        "regime classification and the no-trade refusals, with eligibility asserted "
        "per family",
        "signal rules for all five families, including the flow-data refusal",
        "Beta posteriors, calibration, EV rejection on incomplete costs, and sizing "
        "refusals, in unit tests with exact arithmetic",
        "guardian limits, entry suspension versus position management, and the "
        "emergency-stop plan",
        "paper venue idempotency, injected rejects, partial fills and ambiguous "
        "outcomes, with recovery by query only",
        "stop/target/break-even/trailing/time exits, protection verification and "
        "reconciliation, on a simulated venue",
        "hash-chained ledger, seal-based truncation detection, and the refusal to "
        "approve a strategy without independent evidence",
        "skeptic walk-forward, PBO, deflated Sharpe and cost stress on generated series",
        "the whole desk end to end on synthetic replays (this report)",
    ),
    "unverified": (
        "any MetaTrader 5 terminal: no connection, no symbol table, no tick stream, no "
        "account figures were read",
        "real spread, slippage, commission, swap or partial-fill behaviour at any broker",
        "the terminal's own tick delivery timing, so the ingest-latency figures below "
        "are process measurements against replay stamps",
        "live paper-forward trading in real time (browser display, keyboard shortcuts, "
        "session persistence across a real trading day)",
        "MQL5-side event handling: the Python polling benchmark is reported, but the "
        "event-driven alternative has not been proposed or measured",
    ),
}

LIMITATIONS = (
    "Synthetic ticks with a declared drift/noise/no-spread model. They are not a "
    "broker feed and not a sample of any market.",
    "The generator's persistent drift is the single most favourable world for a "
    "momentum rule. A high win rate here measures the generator as much as the rule, "
    "which is why it is reported next to the funnel instead of being called an edge.",
    "The paper venue fills every accepted order unless a fault is injected; real "
    "partial-fill and rejection rates are unknown and are not assumed to be zero.",
    "Costs are the declared terms in the configuration (spread from the tick, one "
    "point of slippage per side, zero commission declared as assumed). A real "
    "broker's schedule is unverified.",
    "Latency is measured inside this process against the replay stamps. It is not "
    "network latency and not the terminal's delivery delay.",
    "Nothing here has been run against a MetaTrader 5 terminal. See the report's "
    "'verified / unverified' section.",
)


def build_config():
    return fixture_config(
        cooldown_ms=1500,
        min_window_ticks=40,
        max_open_positions=2,
        max_currency_exposure_fraction="0.60",
    )


def training_scenarios():
    # The training window deliberately contains both a trend and a range, and an
    # adverse reversal, so the calibrated book is not built from winners alone.
    return (
        lab.trending(count=400),
        lab.ranging(count=400),
        lab.breakout(count=300),
        lab.reversal(count=400),
        lab.volatile(count=300),
    )


def evaluation_scenarios():
    return tuple(
        lab.shifted(scenario, start=EVALUATION_START)
        for scenario in lab.standard_scenarios()
    )


def forward_scenarios():
    """The evaluation window with the spread doubled: a cost regime change.

    The price path is deliberately unchanged, so a change in the results can only
    come from the cost structure and the gates that respond to it.
    """

    return tuple(
        lab.repriced(lab.shifted(scenario, start=EVALUATION_START), spread_multiple=2.0)
        for scenario in lab.standard_scenarios()
    )


def pooled_validations(runs):
    """One skeptic report per strategy, over all evaluation trades of that strategy."""

    pooled: dict[str, list[skeptic.TradeRecord]] = {}
    for run in runs:
        for trade in run.trades:
            pooled.setdefault(trade.strategy or trade.fingerprint[:8], []).append(trade)
    reports = []
    for name in sorted(pooled):
        ordered = [replace(trade, index=index) for index, trade in enumerate(pooled[name])]
        reports.append(skeptic.validate(ordered, criteria=skeptic.Criteria()))
    return tuple(reports)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(ROOT / "docs" / "SCALPER_LAB_REPORT.md"))
    args = parser.parse_args()

    config = build_config()
    spec = config.instruments[0]

    # 1. Wide enough to be worth measuring: a target must clear three round turns.
    assumed_spread = Decimal("8")
    strategies = lab.cost_compliant_strategies(
        assumed_spread_points=assumed_spread,
        stops_level_points=spec.stops_level_points,
        cost=config.cost,
        point_value_per_lot=spec.point_value_per_lot,
    )

    # 2. Calibrate on the training window only.
    training = lab.collect_outcomes(training_scenarios(), config=config, strategies=strategies)
    book = training["book"]

    # 3. Evaluate on a different, later window with the posteriors frozen.
    runs = [
        lab.run_scenario(
            scenario,
            config=config,
            strategies=strategies,
            trained_book=book,
            starting_equity=STARTING_EQUITY,
            thresholds=DeskThresholds(min_probability_samples=20),
        )
        for scenario in evaluation_scenarios()
    ]

    forward_runs = [
        lab.run_scenario(
            scenario,
            config=config,
            strategies=strategies,
            trained_book=book,
            starting_equity=STARTING_EQUITY,
            thresholds=DeskThresholds(min_probability_samples=20),
        )
        for scenario in forward_scenarios()
    ]
    degradation = []
    for baseline, forward in zip(runs, forward_runs):
        verdict = lab.degradation_check(baseline, forward)
        degradation.append({"scenario": baseline.scenario["name"], **verdict})

    # 4. Report, including the refusal.
    validations = pooled_validations(runs)
    lab.write_report(
        Path(args.out),
        runs=runs,
        training=training,
        validations=validations,
        extra_notes=LIMITATIONS,
        degradation=degradation,
        verification=VERIFICATION,
    )
    print(f"wrote {args.out}")
    print(f"training outcomes: {len(training['outcomes'])}")
    for run in runs:
        print(
            f"  {run.scenario['name']}: trades={run.metrics.n} "
            f"mean_net={run.metrics.mean_net_points:.4f} window={run.metrics.win_rate:.3f}"
        )
    for report in validations:
        print(f"  validation {report.strategy}: approved={report.approved}")
    for row in degradation:
        print(f"  forward {row['scenario']}: {row['verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
