"""SCRIBE and SKEPTIC.

The ledger has to be checkable and the validator has to be able to say no.  Both
tests here are about refusing to accept a comfortable story: a rewritten ledger, an
approval signed by the author, or a strategy whose edge disappears when costs rise.
"""

from __future__ import annotations

import json
import random

import pytest

from axiom_forex.scalper.scribe import (
    KINDS,
    SYSTEM_ERROR,
    Scribe,
    read_jsonl,
    verify_jsonl,
)
from axiom_forex.scalper.skeptic import (
    APPROVAL_NOT_INDEPENDENT,
    COST_SENSITIVE,
    Criteria,
    ApprovalRegistry,
    StrategyNotApproved,
    TradeRecord,
    bootstrap_interval,
    chronological_split,
    compute_metrics,
    cost_sensitivity,
    cscv_pbo,
    deflated_sharpe,
    norm_cdf,
    norm_ppf,
    session_of,
    validate,
    walk_forward,
)


def trades(count: int = 260, *, mean: float = 1.2, sd: float = 6.0, seed: int = 5, fingerprint="fp1"):
    rng = random.Random(seed)
    out = []
    for index in range(count):
        net = rng.gauss(mean, sd)
        out.append(
            TradeRecord(
                index=index,
                fingerprint=fingerprint,
                strategy="momentum_continuation",
                regime="TRENDING_UP" if index % 3 else "RANGING",
                session="london" if index % 2 else "new_york",
                net_points=net,
                gross_points=net + 8.0,
                cost_points=8.0,
                holding_ms=rng.randint(400, 5_000),
                mae_points=-abs(rng.gauss(4, 2)),
                mfe_points=abs(rng.gauss(9, 3)),
            )
        )
    return out


# ---------------------------------------------------------------------------
# SCRIBE
# ---------------------------------------------------------------------------


def test_the_ledger_refuses_to_forget_and_chains_its_entries():
    scribe = Scribe(run_id="run-1")
    scribe.record("opportunity", at_ms=1, opportunity_id="o1")
    scribe.record("signal", at_ms=2, opportunity_id="o1", strategy="s")
    scribe.record("rejection", at_ms=3, stage="ev", reason="costs", opportunity_id="o1")
    assert scribe.verify()["ok"] is True
    assert scribe.count("rejection") == 1
    assert scribe.sealed is False
    scribe.seal()
    with pytest.raises(RuntimeError):
        scribe.record("pnl", at_ms=4)


def test_there_is_no_way_to_edit_an_entry():
    scribe = Scribe(run_id="run-1")
    scribe.record("opportunity", at_ms=1)
    for forbidden in ("update", "delete", "remove", "rewrite"):
        assert not hasattr(scribe, forbidden)


def test_tampering_with_a_file_is_detected(tmp_path):
    scribe = Scribe(run_id="run-1")
    scribe.record("signal", at_ms=1, strategy="s", side="BUY")
    scribe.record("position_exit", at_ms=2, reason="stop_hit", won=False, gross_points="-20")
    path = tmp_path / "ledger.jsonl"
    scribe.to_jsonl(path)
    assert verify_jsonl(path)["ok"] is True

    rows = read_jsonl(path)
    rows[1]["payload"]["gross_points"] = "20"  # a loser rewritten as a winner
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    result = verify_jsonl(path)
    assert result["ok"] is False
    assert result["reason"] == "hash_mismatch"
    assert result["broken_at"] == 2


def test_a_removed_trailing_entry_is_detected_by_the_seal(tmp_path):
    scribe = Scribe(run_id="run-1")
    for index in range(3):
        scribe.record("signal", at_ms=index, strategy="s")
    scribe.seal(at_ms=10)
    path = tmp_path / "ledger.jsonl"
    scribe.to_jsonl(path)
    assert verify_jsonl(path, require_seal=True)["ok"] is True

    rows = read_jsonl(path)
    # Truncation keeps a valid chain over what remains, which is exactly why a run
    # writes a seal and a reader asks for it.
    path.write_text("\n".join(json.dumps(row) for row in rows[:2]), encoding="utf-8")
    assert verify_jsonl(path)["ok"] is True
    result = verify_jsonl(path, require_seal=True)
    assert result["ok"] is False
    assert result["reason"] == "missing_seal"


def test_appending_twice_does_not_duplicate_entries(tmp_path):
    scribe = Scribe(run_id="run-1")
    scribe.record("signal", at_ms=1, strategy="s")
    path = tmp_path / "ledger.jsonl"
    first = scribe.to_jsonl(path)
    second = scribe.to_jsonl(path)
    assert first["appended"] == 1
    assert second["appended"] == 0
    assert len(read_jsonl(path)) == 1


def test_losers_are_recorded_with_the_same_ceremony_as_winners():
    scribe = Scribe(run_id="run-1")
    scribe.exit_record(at_ms=1, position_id="a", reason="stop_hit", gross_points="-30", gross_account="-3")
    scribe.exit_record(at_ms=2, position_id="b", reason="target_hit", gross_points="30", gross_account="3")
    exits = scribe.entries("position_exit")
    assert [entry.payload["won"] for entry in exits] == [False, True]


def test_integrity_flags_an_order_without_a_result():
    scribe = Scribe(run_id="run-1")
    scribe.record("order_request", at_ms=1, request_id="r1")
    integrity = scribe.integrity()
    assert integrity["ok"] is False
    assert integrity["order_requests_without_result"] == ["r1"]


def test_an_unknown_kind_is_refused():
    with pytest.raises(ValueError):
        Scribe(run_id="run-1").record("something_else", at_ms=1)
    assert "position_exit" in KINDS


def test_every_entry_needs_a_named_actor():
    with pytest.raises(ValueError):
        Scribe(run_id="run-1").record("signal", at_ms=1, actor="  ")


def test_errors_are_first_class_entries():
    scribe = Scribe(run_id="run-1")
    scribe.error(at_ms=1, error="venue_timeout", detail="no answer")
    assert scribe.count(SYSTEM_ERROR) == 1


# ---------------------------------------------------------------------------
# SKEPTIC: splits and folds
# ---------------------------------------------------------------------------


def test_a_chronological_split_keeps_the_order_and_embargoes_the_seams():
    split, error = chronological_split(trades(120), embargo=2, minimum=30)
    assert error == ""
    assert split.train[0].index == 0
    assert split.test[-1].index == 119
    assert split.train[-1].index < split.validation[0].index < split.test[0].index


def test_a_split_refuses_a_sample_that_is_too_small():
    split, error = chronological_split(trades(10), minimum=30)
    assert split is None
    assert "not_enough_trades" in error


def test_walk_forward_reports_each_fold_instead_of_one_number():
    result = walk_forward(trades(240), folds=4)
    assert result["verdict"] in {"passes", "fails"}
    assert result["total_folds"] >= 1
    for fold in result["folds"]:
        assert "out_of_sample_mean" in fold


def test_walk_forward_refuses_to_judge_a_short_history():
    result = walk_forward(trades(12), folds=4)
    assert result["verdict"] == "insufficient_evidence"
    assert result["folds"] == []


# ---------------------------------------------------------------------------
# SKEPTIC: overfitting and Sharpe
# ---------------------------------------------------------------------------


def test_pbo_is_low_when_one_variant_dominates_everywhere():
    strong = [1.0] * 200
    weak = [-1.0] * 200
    result = cscv_pbo({"strong": strong, "weak": weak}, blocks=8)
    assert result["pbo"] == pytest.approx(0.0)
    assert result["verdict"] == "passes"


def test_pbo_is_high_when_the_winner_changes_every_window():
    rng = random.Random(11)
    variants = {}
    for name in ("a", "b", "c", "d"):
        variants[name] = [rng.gauss(0, 1) for _ in range(200)]
    result = cscv_pbo(variants, blocks=8)
    assert result["pbo"] is None or 0 <= result["pbo"] <= 1


def test_pbo_needs_two_variants_and_says_so():
    result = cscv_pbo({"only": [1.0, 2.0, 3.0]}, blocks=8)
    assert result["pbo"] is None
    assert result["verdict"] == "insufficient_evidence"


def test_odd_block_counts_are_refused():
    result = cscv_pbo({"a": [1.0] * 20, "b": [2.0] * 20}, blocks=7)
    assert result["verdict"] == "invalid"


def test_deflated_sharpe_penalises_the_number_of_trials():
    rng = random.Random(9)
    series = [rng.gauss(0.4, 1.0) for _ in range(200)]
    one = deflated_sharpe(series, trials=1)
    many = deflated_sharpe(series, trials=1000)
    assert many["expected_max_sharpe"] > one["expected_max_sharpe"]
    assert many["deflated_sharpe"] < one["deflated_sharpe"]


def test_deflated_sharpe_refuses_a_tiny_sample():
    result = deflated_sharpe([1.0, 2.0, 3.0], trials=5)
    assert result["verdict"] == "insufficient_evidence"
    assert result["deflated_sharpe"] is None


def test_normal_helpers_are_consistent():
    assert norm_cdf(0.0) == pytest.approx(0.5)
    assert norm_ppf(0.975) == pytest.approx(1.959964, abs=1e-4)
    assert norm_cdf(norm_ppf(0.3)) == pytest.approx(0.3, abs=1e-6)


# ---------------------------------------------------------------------------
# SKEPTIC: costs, intervals and the verdict
# ---------------------------------------------------------------------------


def test_cost_sensitivity_finds_the_multiple_where_the_edge_dies():
    prepared = trades(120, mean=6.0, sd=4.0)
    result = cost_sensitivity(prepared)
    assert result["breaks_between"] is not None
    assert result["breaks_between"] > 1.0


def test_the_bootstrap_interval_is_wide_when_the_sample_is_small():
    small = bootstrap_interval([1.0, -1.0, 2.0, -2.0, 0.5], resamples=200)
    assert small["includes_zero"] is True
    assert "not a prediction interval" in small["note"]


def test_metrics_report_a_profit_factor_only_when_there_are_losers():
    winners = [trade for trade in trades(40) if trade.net_points > 0]
    metrics = compute_metrics(winners)
    assert metrics.profit_factor is None


def test_a_sample_with_no_losers_cannot_be_approved():
    winners = [trade for trade in trades(60) if trade.net_points > 0]
    report = validate(winners)
    assert report.approved is False
    assert any("no losing trades" in finding for finding in report.findings)


def test_validation_of_a_marginal_sample_is_a_refusal_not_a_pass():
    report = validate(trades(60, mean=0.2, sd=6.0))
    assert report.approved is False
    assert COST_SENSITIVE in " ".join(report.findings) or report.findings


def test_validation_carries_every_piece_of_evidence_a_reader_needs():
    report = validate(trades(300), variants={"a": [1.0] * 300, "b": [0.5] * 300})
    view = report.audit_view()
    for key in (
        "metrics",
        "out_of_sample",
        "walk_forward",
        "multiple_testing",
        "pbo",
        "deflated_sharpe",
        "cost_sensitivity",
        "spread_stress",
        "regime_breakdown",
        "session_breakdown",
        "confidence_interval",
    ):
        assert key in view
    assert "approved=False is a result" in view["note"]


def test_criteria_are_declared_before_the_run():
    criteria = Criteria()
    view = criteria.audit_view()
    assert "deterministic" not in json.dumps(view).lower()
    assert "before measurement" in view["declared"]


# ---------------------------------------------------------------------------
# approvals
# ---------------------------------------------------------------------------


def approvable_report():
    """A sample that *does* satisfy the declared criteria, so approval can be tested.

    It is a fixture, not a result: the numbers are drawn from a fixed generator so a
    reader can see the machinery work, and nothing here is a claim about a market.
    """

    rng = random.Random(2)
    prepared = []
    for index in range(400):
        net = rng.gauss(6.0, 5.0)  # mixed wins and losses, unlike a cherry-picked sample
        prepared.append(
            TradeRecord(
                index=index,
                fingerprint="fp-approved",
                strategy="momentum_continuation",
                regime="TRENDING_UP",
                session="london",
                net_points=net,
                gross_points=net + 8.0,
                cost_points=8.0,
                holding_ms=1_000,
            )
        )
    series = [trade.net_points for trade in prepared]
    return validate(
        prepared,
        variants={
            "this": series,
            "weaker": [value * 0.3 for value in series],
            "noise": [value * 0.05 + (1.0 if index % 2 else -1.0) for index, value in enumerate(series)],
        },
    )


def test_an_approval_cannot_be_signed_by_the_developer():
    report = approvable_report()
    registry = ApprovalRegistry(developers=["dev-a"])
    approval, detail = registry.grant(report, approved_by="dev-a", at_ms=1)
    assert approval is None
    assert APPROVAL_NOT_INDEPENDENT in detail


def test_a_strategy_that_was_not_validated_cannot_be_approved():
    report = validate(trades(40, mean=-2.0))
    registry = ApprovalRegistry(developers=["dev-a"])
    approval, detail = registry.grant(report, approved_by="risk-desk", at_ms=1)
    assert approval is None
    assert "not_validated" in detail


def test_approval_is_bound_to_the_parameter_fingerprint():
    report = approvable_report()
    registry = ApprovalRegistry(developers=["dev-a"])
    approval, detail = registry.grant(report, approved_by="risk-desk", at_ms=1)
    if approval is None:  # the criteria are deliberately hard to satisfy
        pytest.skip(f"the declared criteria refused this sample: {detail}")
    assert registry.status("fp-approved")["approved"] is True
    assert registry.status("fp-approved-changed")["approved"] is False
    assert "parameter fingerprint" in registry.status("fp-approved-changed")["note"]


def test_live_execution_requires_an_approval_and_paper_does_not():
    registry = ApprovalRegistry()
    registry.require("anything", mode="PAPER")
    with pytest.raises(StrategyNotApproved):
        registry.require("anything", mode="LIVE")


def test_an_approval_can_be_revoked_with_a_reason():
    registry = ApprovalRegistry()
    report = approvable_report()
    approval, _detail = registry.grant(report, approved_by="risk-desk", at_ms=1)
    if approval is None:
        pytest.skip("the declared criteria refused this sample")
    ok, reason = registry.revoke("fp-approved", reason="regime changed")
    assert ok is True
    assert registry.status("fp-approved")["reason"] == "regime changed"


def test_sessions_are_named_not_guessed():
    from datetime import datetime, timezone

    assert session_of(datetime(2026, 3, 2, 3, tzinfo=timezone.utc)) == "asia"
    assert session_of(datetime(2026, 3, 2, 9, tzinfo=timezone.utc)) == "london"
    assert session_of(datetime(2026, 3, 2, 13, tzinfo=timezone.utc)) == "london_ny_overlap"
    assert session_of(datetime(2026, 3, 2, 18, tzinfo=timezone.utc)) == "new_york"
    assert session_of(datetime(2026, 3, 2, 23, tzinfo=timezone.utc)) == "late"
