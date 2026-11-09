"""Contract edges: configuration coercion, ledger semantics, repricing, sealing.

These are the behaviours that changed while the desk was being hardened, plus the
places where a plausible-looking shortcut would be wrong: a config string that is
never converted, a winner counted from gross when the net is negative, a "forward
window" that is really the same ticks, a seal that cannot see its own tail.
"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from axiom_forex.scalper import lab
from axiom_forex.scalper.bayes import ProbabilityEstimate
from axiom_forex.scalper.config import ScalperConfigError, fixture_config
from axiom_forex.scalper.desk import ScalperDesk
from axiom_forex.scalper.execution import PaperVenue, VenueFaults
from axiom_forex.scalper.scribe import Scribe, read_jsonl, verify_jsonl
from axiom_forex.scalper.signals import default_strategies
from axiom_forex.scalper.ticks import synthetic_ticks

UTC = timezone.utc
START = datetime(2026, 3, 2, 9, 0, tzinfo=timezone.utc)
CONFIG = fixture_config()
SPEC = CONFIG.spec_for("EURUSD.pro")


def wide():
    return lab.cost_compliant_strategies(
        assumed_spread_points=Decimal("8"),
        stops_level_points=SPEC.stops_level_points,
        cost=CONFIG.cost,
        point_value_per_lot=SPEC.point_value_per_lot,
        strategies=default_strategies(),
    )


def prior(p: str = "0.78") -> ProbabilityEstimate:
    value = float(p)
    return ProbabilityEstimate(
        p=value,
        sd=0.03,
        low=value - 0.04,
        high=value + 0.04,
        samples=200,
        basis="DECLARED_PRIOR_FOR_TEST",
        label="test",
    )


# -- configuration ----------------------------------------------------------


def test_string_overrides_are_coerced_to_the_declared_decimal_type():
    config = fixture_config(max_spread_points="12.5", min_edge_points="0.9", cooldown_ms="800")
    assert config.max_spread_points == Decimal("12.5")
    assert config.min_edge_points == Decimal("0.9")
    assert isinstance(config.cooldown_ms, int)
    assert config.cooldown_ms == 800


def test_an_unknown_override_is_refused_by_name_rather_than_ignored():
    with pytest.raises(ScalperConfigError) as excinfo:
        fixture_config(max_spred_points="12")
    assert excinfo.value.reason == "unknown_config_field"
    assert "max_spred_points" in excinfo.value.detail


def test_an_unparseable_override_is_refused():
    with pytest.raises(ScalperConfigError) as excinfo:
        fixture_config(min_edge_points="a lot")
    assert excinfo.value.reason == "config_value_not_a_number"


def test_a_coerced_config_is_still_validated():
    with pytest.raises(ScalperConfigError):
        fixture_config(max_spread_points_hard="5", max_spread_points="30")


# -- ledger semantics -------------------------------------------------------


def test_a_ledger_winner_is_decided_on_net_not_gross():
    scribe = Scribe(run_id="net-sign")
    entry = scribe.exit_record(
        at_ms=1,
        position_id="p1",
        reason="target_hit",
        gross_points="5",
        gross_account="50",
        won=False,
        cost_points="7",
        net_points="-2",
    )
    assert entry.payload["won"] is False
    assert entry.payload["net_points"] == "-2"


def test_without_a_cost_model_the_gross_sign_is_used_and_the_caller_labels_it():
    scribe = Scribe(run_id="gross-fallback")
    entry = scribe.exit_record(
        at_ms=1,
        position_id="p1",
        reason="target_hit",
        gross_points="5",
        gross_account="50",
        cost_complete=False,
    )
    assert entry.payload["won"] is True
    assert entry.payload["cost_complete"] is False


def test_verify_jsonl_sees_a_truncated_tail_when_a_seal_is_required(tmp_path):
    scribe = Scribe(run_id="truncate")
    scribe.record("signal", at_ms=1, strategy="s")
    scribe.record("signal", at_ms=2, strategy="s")
    scribe.seal(at_ms=3, actor="operator")
    path = tmp_path / "ledger.jsonl"
    scribe.to_jsonl(path)
    assert verify_jsonl(path, require_seal=True)["ok"] is True

    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    verdict = verify_jsonl(path, require_seal=True)
    assert verdict["ok"] is False
    assert verdict["reason"] == "missing_seal"


def test_entries_appended_after_the_seal_are_reported(tmp_path):
    scribe = Scribe(run_id="append-after-seal")
    scribe.record("signal", at_ms=1, strategy="s")
    scribe.seal(at_ms=2)
    path = tmp_path / "ledger.jsonl"
    scribe.to_jsonl(path)
    # Append a well-formed extra line by hand: the chain would still verify forward,
    # which is exactly why the seal must be the last line.
    rows = read_jsonl(path)
    extra = dict(rows[-1])
    extra["seq"] = len(rows) + 1
    extra["payload"] = {"note": "appended later"}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(extra) + "\n")
    verdict = verify_jsonl(path, require_seal=True)
    assert verdict["ok"] is False
    assert verdict["reason"] in {"entries_appended_after_the_seal", "previous_hash_mismatch"}


# -- desk contract ----------------------------------------------------------


def feed(desk, ticks):
    for tick in ticks:
        desk.on_tick(
            symbol=tick.symbol,
            bid=tick.bid,
            ask=tick.ask,
            source_time=tick.at,
            received_at=tick.at + timedelta(milliseconds=20),
            volume=tick.volume,
            day_key=tick.at.date().isoformat(),
        )


def make_desk(**kwargs):
    defaults = dict(
        config=CONFIG,
        strategies=wide(),
        venue=PaperVenue(cost=CONFIG.cost, faults=VenueFaults(max_latency_ms=5)),
        probability_source=lambda signal, regime: prior(),
        run_id="edge",
    )
    defaults.update(kwargs)
    return ScalperDesk(**defaults)


def test_every_evaluation_is_written_down_as_an_opportunity():
    desk = make_desk()
    feed(desk, synthetic_ticks(count=120, drift_points=1.6, noise_points=1.2, step_ms=200))
    opportunities = desk.scribe.entries("opportunity")
    assert len(opportunities) == desk.stats["opportunities"]
    assert all("outcome" in entry.payload for entry in opportunities)


def test_the_sized_counter_matches_the_orders_that_were_actually_submitted():
    desk = make_desk()
    feed(desk, synthetic_ticks(count=160, drift_points=1.6, noise_points=1.2, step_ms=200))
    assert desk.stats["sized"] == desk.stats["orders"]
    assert desk.stats["orders"] == desk.stats["fills"] + desk.stats["rejects"]


def test_thin_external_probability_source_is_a_hard_no_trade_and_audited():
    thin = ProbabilityEstimate(
        p=0.99, sd=0.001, low=0.98, high=1.0, samples=1,
        basis="deliberately_thin_test_source", label="unqualified",
    )
    desk = make_desk(probability_source=lambda _signal, _regime: thin)
    feed(desk, synthetic_ticks(count=140, drift_points=1.6, noise_points=1.2, step_ms=200))
    assert desk.stats["probability_refused"] > 0
    assert desk.stats["orders"] == desk.stats["fills"] == 0
    refusals = [entry for entry in desk.scribe.entries("rejection")
                if entry.payload.get("reason") == "insufficient_probability_samples"]
    assert refusals and refusals[0].payload["basis"] == "deliberately_thin_test_source"


def test_without_probability_evidence_nothing_is_submitted_and_the_reason_is_recorded():
    desk = make_desk(probability_source=None)
    feed(desk, synthetic_ticks(count=140, drift_points=1.6, noise_points=1.2, step_ms=200))
    assert desk.stats["signals"] > 0
    assert desk.stats["orders"] == 0
    assert desk.stats["probability_refused"] > 0
    reasons = {entry.payload.get("reason") for entry in desk.scribe.entries("rejection")}
    assert "insufficient_probability_samples" in reasons


def test_caller_supplied_account_figures_are_used_verbatim():
    desk = make_desk()
    tick = synthetic_ticks(count=1, step_ms=200)[0]
    desk.on_tick(
        symbol=tick.symbol,
        bid=tick.bid,
        ask=tick.ask,
        source_time=tick.at,
        received_at=tick.at,
        equity=Decimal("12345.67"),
        balance=Decimal("12000.00"),
        day_key="2026-03-02",
    )
    assert desk.equity == Decimal("12345.67")
    assert desk.status()["equity"] == "12345.67"


def test_a_non_paper_venue_is_refused_by_the_desk_constructor():
    from app.execution.broker import TradingBlocked

    class LiveVenue(PaperVenue):
        mode = "LIVE"

    with pytest.raises(TradingBlocked):
        make_desk(venue=LiveVenue(cost=CONFIG.cost))


def test_status_never_reports_a_trade_that_the_book_did_not_close():
    desk = make_desk()
    feed(desk, synthetic_ticks(count=120, drift_points=1.6, noise_points=1.2, step_ms=200))
    assert desk.status()["closed_trades"] == len(desk.book.closed)
    assert desk.status()["stats"]["trades"] == len(desk.trades)


# -- lab repricing ----------------------------------------------------------


def test_repricing_widens_the_spread_and_keeps_the_price_path():
    scenario = lab.trending(count=40)
    wider = lab.repriced(scenario, spread_multiple=2.0)
    assert wider.name.endswith("_cost_x2")
    assert "spread_widened" in wider.injected
    assert wider.digest != scenario.digest
    for before, after in zip(scenario.ticks, wider.ticks):
        assert after.bid < before.bid
        assert after.ask > before.ask
        assert after.at == before.at


def test_repricing_requires_a_positive_multiple():
    with pytest.raises(ValueError):
        lab.repriced(lab.trending(count=10), spread_multiple=0)


def test_the_report_includes_the_cost_stress_and_the_verification_split():
    scenario = lab.trending(count=120)
    baseline = lab.run_scenario(scenario, config=CONFIG, strategies=wide())
    forward = lab.run_scenario(
        lab.repriced(scenario, spread_multiple=2.0), config=CONFIG, strategies=wide()
    )
    verdict = lab.degradation_check(baseline, forward)
    rows = [{"scenario": baseline.scenario["name"], **verdict}]
    target = Path(tempfile.mkdtemp()) / "report.md"
    lab.write_report(
        target,
        runs=[baseline],
        degradation=rows,
        verification={
            "verified": ["tick core on replay"],
            "unverified": ["any MetaTrader 5 terminal"],
        },
    )
    text = target.read_text(encoding="utf-8")
    assert "## Paper-forward comparison and degradation" in text
    assert "## Verified here, unverified elsewhere" in text
    assert "any MetaTrader 5 terminal" in text
    assert "twice the spread" in text
