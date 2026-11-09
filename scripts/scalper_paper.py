"""Run one AX-SCALPER paper session and write its sealed ledger.

This is the paper runbook's executable step.  It is a **paper** session: there is no
venue other than the simulated one, no live adapter, and no order can leave this
process.  Three data sources are accepted and each is labelled in the output:

``--source sample``  a deterministic synthetic path (``SAMPLE_DATA``)
``--source file``    a JSONL tick file you provide (``HISTORICAL_TICK_REPLAY``)
``--source train``   the lab's synthetic training replay, then its evaluation replay

Probabilities come from exactly one of:

``--calibrate``  an outcome book built on a *training* replay (the honest path)
``--prior P``    a declared base rate, labelled ``DECLARED_PRIOR_NOT_MEASURED``

With neither, every signal is refused for want of evidence.  That is the correct
behaviour and the output says so; it is not a bug to work around.

Usage:
    python scripts/scalper_paper.py --source sample --calibrate
    python scripts/scalper_paper.py --source file --file ticks.jsonl --prior 0.66
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from axiom_forex.scalper import lab  # noqa: E402
from axiom_forex.scalper.bayes import ProbabilityEstimate  # noqa: E402
from axiom_forex.scalper.config import fixture_config  # noqa: E402
from axiom_forex.scalper.desk import DeskThresholds, ScalperDesk  # noqa: E402
from axiom_forex.scalper.execution import PaperVenue  # noqa: E402
from axiom_forex.scalper.scribe import verify_jsonl  # noqa: E402
from axiom_forex.scalper.ticks import ReplayTick, read_tick_file, synthetic_ticks  # noqa: E402

UTC = timezone.utc
START = datetime(2026, 3, 2, 9, 0, 0, tzinfo=UTC)


def build_config():
    return fixture_config(cooldown_ms=1500, min_window_ticks=40, max_open_positions=1)


def declared_prior(p: float) -> ProbabilityEstimate:
    return ProbabilityEstimate(
        p=p,
        sd=0.05,
        low=max(0.0, p - 0.064),
        high=min(1.0, p + 0.064),
        samples=200,
        basis="DECLARED_PRIOR_NOT_MEASURED",
        label="declared",
    )


def ticks_for(args) -> tuple[list[ReplayTick], str]:
    if args.source == "file":
        if not args.file:
            raise SystemExit("--source file needs --file")
        return read_tick_file(args.file), "HISTORICAL_TICK_REPLAY"
    if args.source == "train":
        return list(lab.trending(count=args.ticks)), "SAMPLE_DATA"
    return (
        synthetic_ticks(count=args.ticks, drift_points=1.6, noise_points=1.2, step_ms=200),
        "SAMPLE_DATA",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=("sample", "file", "train"), default="sample")
    parser.add_argument("--file", default=None)
    parser.add_argument("--ticks", type=int, default=400)
    parser.add_argument("--equity", default="10000")
    parser.add_argument("--calibrate", action="store_true")
    parser.add_argument("--prior", type=float, default=None)
    parser.add_argument("--out", default="scalper_paper_ledger.jsonl")
    parser.add_argument("--every", type=int, default=100)
    args = parser.parse_args()

    config = build_config()
    spec = config.instruments[0]
    strategies = lab.cost_compliant_strategies(
        assumed_spread_points=Decimal("8"),
        stops_level_points=spec.stops_level_points,
        cost=config.cost,
        point_value_per_lot=spec.point_value_per_lot,
    )

    probability_source = None
    probability_note = "none — signals will be refused for want of evidence"
    if args.calibrate:
        training = lab.collect_outcomes(
            (lab.trending(count=300), lab.ranging(count=300), lab.reversal(count=300)),
            config=config,
            strategies=strategies,
        )
        book = training["book"]

        def probability_source(signal, regime_name):
            return book.estimate(signal.parameter_fingerprint, regime_name)

        probability_note = (
            f"calibrated on a training replay ({len(training['outcomes'])} brackets); "
            "SAMPLE_DATA"
        )
    elif args.prior is not None:
        probability_source = lambda signal, regime_name: declared_prior(args.prior)  # noqa: E731
        probability_note = f"declared prior p={args.prior} — DECLARED_PRIOR_NOT_MEASURED"

    ticks, data_class = ticks_for(args)
    desk = ScalperDesk(
        config=config,
        strategies=strategies,
        venue=PaperVenue(cost=config.cost),
        probability_source=probability_source,
        thresholds=DeskThresholds(min_probability_samples=20),
        starting_equity=Decimal(args.equity),
        run_id=f"paper-{data_class.lower()}",
    )

    print(f"mode            PAPER (no venue other than the simulation)")
    print(f"data class      {data_class}")
    print(f"probability     {probability_note}")
    print(f"instrument      {spec.symbol} point={spec.point} source={spec.source}")
    print("")

    started = datetime.now(tz=UTC)
    for index, tick in enumerate(ticks, start=1):
        desk.on_tick(
            symbol=tick.symbol,
            bid=tick.bid,
            ask=tick.ask,
            source_time=tick.at,
            received_at=tick.at + timedelta(milliseconds=20),
            volume=tick.volume,
            source=data_class,
            day_key=tick.at.date().isoformat(),
        )
        if args.every and index % args.every == 0:
            status = desk.status()
            print(
                f"  {index:5d} ticks  equity={status['equity']:>10}  "
                f"open={status['open_positions']}  trades={status['closed_trades']}  "
                f"signals={status['stats']['signals']}  fills={status['stats']['fills']}"
            )

    if desk.book.open_positions:
        desk.flatten_all(at_ms=int(ticks[-1].at.timestamp() * 1000), reason="paper_session_end")

    desk.scribe.seal(at_ms=int(datetime.now(tz=UTC).timestamp() * 1000), actor="paper_runner")
    written = desk.scribe.to_jsonl(args.out)
    status = desk.status()

    print("")
    print(f"elapsed         {(datetime.now(tz=UTC) - started).total_seconds():.2f} s")
    print(f"ticks           {status['ticks']['accepted']} accepted, refusals {status['tick_refusals']}")
    print(f"opportunities   {status['stats']['opportunities']}")
    print(f"signals         {status['stats']['signals']}")
    print(f"EV              accept={status['stats']['ev_accept']} reject={status['stats']['ev_reject']} {status['ev_rejections']}")
    print(f"sizing          sized={status['stats']['sized']} refused={status['stats']['size_refused']} {status['size_refusals']}")
    print(f"risk            refused={status['stats']['risk_refused']} {status['risk_refusals']}")
    print(f"orders/fills    {status['stats']['orders']} / {status['stats']['fills']}")
    print(f"trades          {status['stats']['trades']}")
    print(f"ledger          {written['path']} ({written['appended']} entries appended)")
    print(f"chain           ok={status['ledger']['integrity']['chain']['ok']}")
    seal = verify_jsonl(args.out, require_seal=True)
    print(f"sealed file     ok={seal['ok']} sealed={seal.get('sealed')} entries={seal.get('entries')}"
          + ("" if seal["ok"] else f" reason={seal.get('reason')}"))
    print("")
    print(
        "This was a paper session on " + data_class + ". It is not evidence of edge, not "
        "a live-readiness check, and not a broker statement."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
