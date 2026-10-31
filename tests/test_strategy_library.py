"""The curated strategy library: cards, variants, ledger, screen, selection.

Pinned here, in the order a reader would ask:

* every card names its source, its rules, its parameters, its timeframes and its
  licence -- and a card whose required data AXIOM does not have is listed as *not
  evaluable* rather than approximated;
* a variant is a parameter combination of a documented template, not a "researched
  strategy", and every attempt is recorded, including the failures;
* the screen refuses fixture and demo data, applies FDR and the Reality Check, and
  reports the numbers behind each verdict;
* only an *eligible* attempt can be selected, there is no override, and selecting
  records the operator's own choice without arming, sizing, or trading;
* the library page says all of this, and the live plan's candidate list only
  includes a selected variant because the operator selected it.

Fixture data is synthetic. Nothing here is a profitability claim.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from axiom_forex import build_fixture_platform
from axiom_forex.strategies.base import Signal
from axiom_forex.strategies.library.cards import (
    CARDS,
    Capabilities,
    card,
    cards,
    family_label,
    search,
)
from axiom_forex.strategies.library.evaluation import EvaluationRefused, evaluate_variant
from axiom_forex.strategies.library.ledger import (
    ELIGIBLE,
    INSUFFICIENT,
    REJECTED,
    SelectionRefused,
    StrategyLedger,
)
from axiom_forex.strategies.library.monitor import (
    ScreenCriteria,
    coverage,
    screen,
    score_search,
    AttemptInput,
)
from axiom_forex.strategies.library.multiplicity import (
    benjamini_hochberg,
    block_bootstrap_p_value,
    reality_check,
)
from axiom_forex.strategies.library.panel import StrategyLibraryPanel
from axiom_forex.strategies.library.variants import (
    MAX_VARIANTS_PER_CARD,
    count_variants,
    variants_for,
)

UTC = timezone.utc


# ---------------------------------------------------------------------------
# cards
# ---------------------------------------------------------------------------


def test_every_card_states_its_source_rules_parameters_and_licence():
    assert len(cards()) >= 6
    families = {item.family for item in cards()}
    assert families == {"trend", "breakout", "mean-reversion", "carry", "volatility", "session"}
    for item in cards():
        assert item.rules, item.card_id
        assert item.source.citation, item.card_id
        assert item.licence in {"TEMPLATE_DERIVED", "DOCUMENTED_FAMILY"}, item.card_id
        assert item.timeframes, item.card_id
        assert item.assumptions and item.limitations, item.card_id
        for spec in item.parameters:
            assert spec.grid, (item.card_id, spec.name)
            assert spec.description, (item.card_id, spec.name)


def test_a_card_that_needs_data_we_do_not_have_is_unavailable_not_approximated():
    carry = card("carry_swap_trend")
    assert carry is not None and not carry.evaluable
    availability = carry.availability(Capabilities())
    assert availability["evaluable"] is False
    assert "SWAP_RATES" in availability["missing"]
    assert "swap rates" in availability["reason"]

    # With the data declared available, the template is still not implemented:
    # the card says so rather than pointing at a rule that does not exist.
    with_data = carry.availability(Capabilities(swap_rates=True))
    assert with_data["evaluable"] is False


def test_the_search_filters_by_words_and_family_and_never_ranks_by_return():
    found = search(text="donchian")
    assert [item.card_id for item in found] == ["breakout_donchian"]
    assert search(family="mean-reversion")
    assert search(family="mean-reversion")[0].family == "mean-reversion"
    assert family_label("carry") == "Carry and swap"
    # An empty query returns the whole catalogue in declaration order.
    assert [item.card_id for item in search()] == [item.card_id for item in CARDS]


# ---------------------------------------------------------------------------
# variants
# ---------------------------------------------------------------------------


def test_variants_are_predeclared_combinations_with_a_ceiling():
    item = card("trend_sma_cross")
    variants = variants_for(item)
    assert variants
    assert len(variants) <= MAX_VARIANTS_PER_CARD
    # Contradictory combinations (fast >= slow) are dropped, not evaluated.
    for variant in variants:
        assert variant.parameters["fast_period"] < variant.parameters["slow_period"]
    # The same call returns the same order: a grid is not resampled.
    assert [v.variant_id for v in variants_for(item)] == [v.variant_id for v in variants]


def test_the_advertised_count_is_templates_and_variants_not_thousands():
    counts = count_variants()
    assert counts["variants"] == sum(len(variants_for(item)) for item in CARDS)
    assert counts["variants"] < 100
    assert "not independently researched strategies" in counts["note"]
    assert counts["evaluable_templates"] < counts["templates"]


def test_a_variant_builds_a_strategy_that_holds_without_enough_bars():
    variant = variants_for(card("breakout_donchian"))[0]
    strategy = variant.strategy()
    assert strategy.generate_signal([]) == Signal.HOLD


# ---------------------------------------------------------------------------
# multiplicity
# ---------------------------------------------------------------------------


def test_the_bootstrap_p_value_is_deterministic_and_answers_the_right_question():
    returns = [0.001] * 60 + [-0.0005] * 40
    first = block_bootstrap_p_value(returns, resamples=200, seed=7)
    second = block_bootstrap_p_value(returns, resamples=200, seed=7)
    assert first["p_value"] == second["p_value"]
    assert first["resamples"] == 200
    # A series with a clearly negative mean is not significant in the positive tail.
    negative = block_bootstrap_p_value([-0.002] * 100, resamples=200)
    assert negative["p_value"] > 0.9
    assert block_bootstrap_p_value([0.001], resamples=10)["p_value"] is None


def test_benjamini_hochberg_gets_stricter_as_the_search_grows():
    one = benjamini_hochberg({"a": 0.04}, q=0.10)
    many = benjamini_hochberg({"a": 0.04, "b": 0.5, "c": 0.6, "d": 0.7}, q=0.10)
    assert one["accepted"] == ["a"]
    assert many["accepted"] == []
    assert many["q_values"]["a"] > one["q_values"]["a"]


def test_reality_check_says_whether_the_best_variant_beats_luck():
    strong = {"a": [0.002] * 80, "b": [0.0015] * 80}
    weak = {"a": [0.0002, -0.0002] * 40, "b": [0.0001, -0.0001] * 40}
    assert reality_check(strong, resamples=200)["p_value"] < 0.05
    assert reality_check(weak, resamples=200)["p_value"] > 0.5


# ---------------------------------------------------------------------------
# the screen
# ---------------------------------------------------------------------------


def promoting_evidence(**overrides):
    payload = {
        "evaluated": True,
        "data_class": "USER_SUPPLIED_HISTORICAL",
        "dataset_id": "dataset-1",
        "failed_criteria": [],
        "metrics": {
            "validation": {"net_pnl_account": "12.5"},
            "locked_test": {
                "net_pnl_account": "8.0",
                "closed_trades": 40,
                "max_drawdown_fraction": "0.04",
            },
        },
        "aggregate": {},
        "walk_forward_summary": {"folds": 3, "positive_fraction": "0.66"},
    }
    payload.update(overrides)
    return payload


def test_the_screen_reports_every_criterion_with_its_number():
    verdict = screen(promoting_evidence(), q_value="0.02", reality_check_p="0.01")
    assert verdict["status"] == ELIGIBLE
    names = {check["name"] for check in verdict["checks"]}
    assert {
        "data_class_can_promote",
        "enough_closed_trades",
        "drawdown_within_limit",
        "walk_forward_folds",
        "walk_forward_positive_folds",
        "validation_positive",
        "locked_test_positive",
        "multiple_testing_q_value",
        "reality_check",
    } <= names
    assert all(check["passed"] for check in verdict["checks"])


def test_fixture_data_can_never_make_a_variant_eligible():
    verdict = screen(
        promoting_evidence(data_class="SYNTHETIC_FIXTURE"), q_value="0.01", reality_check_p="0.01"
    )
    assert verdict["status"] == REJECTED
    assert "data_class_can_promote" in verdict["reason"]


def test_too_few_trades_or_no_multiplicity_evidence_is_not_eligibility():
    few = promoting_evidence()
    few["metrics"]["locked_test"]["closed_trades"] = 3
    assert screen(few, q_value="0.01", reality_check_p="0.01")["status"] == REJECTED

    no_correction = screen(promoting_evidence())
    assert no_correction["status"] == REJECTED
    assert "multiple_testing_q_value" in no_correction["reason"]


def test_an_unevaluated_variant_is_insufficient_rather_than_rejected():
    verdict = screen({"evaluated": False, "data_class": "USER_SUPPLIED_HISTORICAL"})
    assert verdict["status"] == INSUFFICIENT


def test_the_search_report_applies_correction_across_every_attempt():
    attempts = [
        AttemptInput(
            variant_id=f"trend_sma_cross::{index}",
            card_id="trend_sma_cross",
            evidence=promoting_evidence(),
            returns=[0.001] * 40 + [-0.0004] * 20,
        )
        for index in range(4)
    ]
    report = score_search(attempts, criteria=ScreenCriteria())
    assert report["attempts"] == 4
    assert all(item["q_value"] != "" for item in report["results"])
    assert "Selection bias is controlled" in report["note"]


def test_coverage_ignores_a_timeframe_no_template_declares():
    rows = coverage(
        symbols=["EURUSD"],
        timeframes=["1m", "1s"],
        bar_counts={("EURUSD", "1m"): 500},
    )
    by_timeframe = {row["timeframe"]: row for row in rows}
    assert by_timeframe["1m"]["monitorable"] is True
    assert by_timeframe["1s"]["monitorable"] is False
    assert "no template declares 1s" in by_timeframe["1s"]["reason"]


# ---------------------------------------------------------------------------
# the ledger
# ---------------------------------------------------------------------------


def test_the_ledger_keeps_failures_and_counts_them(tmp_path):
    ledger = StrategyLedger(tmp_path / "attempts.sqlite3")
    ledger.record(
        variant_id="v1",
        card_id="trend_sma_cross",
        grid_version="g",
        symbol="EURUSD",
        timeframe="5m",
        dataset_id="d",
        dataset_fingerprint="f",
        status=REJECTED,
        parameters={"fast_period": 8},
        metrics={},
        reason="drawdown",
    )
    assert ledger.counts()["attempts"] == 1
    assert ledger.counts()["per_card"]["trend_sma_cross"][REJECTED] == 1


def test_a_rejected_attempt_cannot_be_selected_at_all(tmp_path):
    ledger = StrategyLedger(tmp_path / "attempts.sqlite3")
    attempt = ledger.record(
        variant_id="v1",
        card_id="c",
        grid_version="g",
        symbol="EURUSD",
        timeframe="5m",
        dataset_id="d",
        dataset_fingerprint="f",
        status=REJECTED,
        parameters={},
        metrics={},
        reason="drawdown",
    )
    with pytest.raises(SelectionRefused) as caught:
        ledger.select(attempt.attempt_id)
    assert caught.value.reason == "attempt_rejected"
    assert ledger.selections() == []


def test_only_an_eligible_attempt_can_be_selected(tmp_path):
    ledger = StrategyLedger(tmp_path / "attempts.sqlite3")
    eligible = ledger.record(
        variant_id="v1",
        card_id="c",
        grid_version="g",
        symbol="EURUSD",
        timeframe="5m",
        dataset_id="d",
        dataset_fingerprint="f",
        status=ELIGIBLE,
        parameters={},
        metrics={"data_class": "USER_SUPPLIED_HISTORICAL"},
    )
    selected = ledger.select(eligible.attempt_id, note="mine")
    assert selected["variant_id"] == "v1"
    assert ledger.counts()["selected"] == 1
    assert ledger.deselect("v1") is True


# ---------------------------------------------------------------------------
# evaluation through the existing engine
# ---------------------------------------------------------------------------


def _fixture_platform():
    return build_fixture_platform(bar_count=200)


def test_evaluating_a_variant_uses_the_paper_lane_and_refuses_fixture_promotion():
    platform = _fixture_platform()
    try:
        variant = variants_for(card("trend_sma_cross"))[0]
        result = evaluate_variant(
            variant,
            bars=platform.bars,
            symbol="EURUSD",
            timeframe="1m",
            data_class="SYNTHETIC_FIXTURE",
            dataset_id="fixture-200",
            engine_factory=platform.new_engine,
        )
    finally:
        platform.close()
    assert result.verdict["status"] == REJECTED
    assert "data_class_can_promote" in result.verdict["reason"]
    assert "fixture" in result.explanation.lower() or "SYNTHETIC_FIXTURE" in result.explanation
    assert len(result.dataset_fingerprint) == 64


def test_a_timeframe_the_template_does_not_declare_is_refused():
    platform = _fixture_platform()
    try:
        variant = variants_for(card("meanrev_bollinger"))[0]
        with pytest.raises(EvaluationRefused) as caught:
            evaluate_variant(
                variant,
                bars=platform.bars,
                symbol="EURUSD",
                timeframe="4h",
                data_class="USER_SUPPLIED_HISTORICAL",
                dataset_id="d",
                engine_factory=platform.new_engine,
            )
    finally:
        platform.close()
    assert caught.value.reason == "timeframe_not_declared"


def test_a_dataset_must_declare_its_class_before_it_can_be_scored():
    platform = _fixture_platform()
    try:
        variant = variants_for(card("trend_sma_cross"))[0]
        with pytest.raises(EvaluationRefused) as caught:
            evaluate_variant(
                variant,
                bars=platform.bars,
                symbol="EURUSD",
                timeframe="1m",
                data_class="",
                dataset_id="d",
                engine_factory=platform.new_engine,
            )
    finally:
        platform.close()
    assert caught.value.reason == "data_class_required"


# ---------------------------------------------------------------------------
# the panel
# ---------------------------------------------------------------------------


def test_the_panel_cannot_select_an_untested_variant(tmp_path):
    panel = StrategyLibraryPanel(root=tmp_path)
    outcome = panel.act("strategy-select", {"variant_id": "trend_sma_cross::a", "confirmed": "yes"})
    assert outcome["ok"] is False
    assert outcome["reason"] == "not_eligible"


def test_selection_needs_the_operator_confirmation(tmp_path):
    panel = StrategyLibraryPanel(root=tmp_path)
    variant = variants_for(card("trend_sma_cross"))[0]
    panel.ledger().record(
        variant_id=variant.variant_id,
        card_id=variant.card_id,
        grid_version="g",
        symbol="EURUSD",
        timeframe="5m",
        dataset_id="d",
        dataset_fingerprint="f",
        status=ELIGIBLE,
        parameters=variant.parameters,
        metrics={"data_class": "USER_SUPPLIED_HISTORICAL", "evaluated": True},
    )
    refused = panel.act("strategy-select", {"variant_id": variant.variant_id})
    assert refused["ok"] is False
    assert refused["reason"] == "confirmation_required"

    accepted = panel.act(
        "strategy-select", {"variant_id": variant.variant_id, "confirmed": "yes"}
    )
    assert accepted["ok"] is True
    assert "does not size a position" in accepted["message"]
    assert panel.ledger().counts()["selected"] == 1


def test_an_eligible_selection_becomes_a_live_candidate_with_its_evidence(tmp_path):
    panel = StrategyLibraryPanel(root=tmp_path)
    variant = variants_for(card("trend_sma_cross"))[0]
    ledger = panel.ledger()
    ledger.record(
        variant_id=variant.variant_id,
        card_id=variant.card_id,
        grid_version="g",
        symbol="EURUSD",
        timeframe="5m",
        dataset_id="d",
        dataset_fingerprint="f",
        status=ELIGIBLE,
        parameters=variant.parameters,
        metrics={"data_class": "USER_SUPPLIED_HISTORICAL", "evaluated": True},
    )
    attempt = ledger.attempts(variant_id=variant.variant_id)[0]
    ledger.select(attempt.attempt_id)
    candidates = panel.selected_candidates()
    assert [item.candidate_id for item in candidates] == [f"library::{variant.variant_id}"]
    candidate = candidates[0]
    assert candidate.evidence["passed"] is True
    assert candidate.evidence["selected_by_operator"] is True
    # The candidate builds the same rule the ledger recorded.
    assert candidate.build().name == variant.variant_id


def test_the_library_view_states_the_honest_counts_and_the_non_claims(tmp_path):
    panel = StrategyLibraryPanel(root=tmp_path)
    view = panel.view(query={"q": [""], "family": [""]})
    assert view["library"]["templates"] == len(CARDS)
    assert view["library"]["variants"] < 100
    assert view["counts"]["attempts"] == 0
    assert any("larger library" in note for note in view["non_claims"])
    assert view["criteria"]["criteria_version"]
    statuses = {item["card_id"]: item["status"] for item in view["catalogue"]}
    assert statuses["carry_swap_trend"] == "UNAVAILABLE"
    assert statuses["trend_sma_cross"] == "TEMPLATE"


def test_evaluating_from_the_panel_records_the_attempt_and_nothing_more(tmp_path):
    platform = build_fixture_platform(bar_count=200)
    panel = StrategyLibraryPanel(
        root=tmp_path,
        bars_provider=lambda symbol, timeframe, count: platform.bars,
        platform_builder=lambda symbol: platform,
    )
    variant = variants_for(card("trend_sma_cross"))[0]
    try:
        outcome = panel.act(
            "strategy-evaluate",
            {
                "variant_id": variant.variant_id,
                "symbol": "EURUSD",
                "timeframe": "1m",
                "dataset_id": "fixture-200",
                "data_class": "SYNTHETIC_FIXTURE",
            },
        )
    finally:
        platform.close()
    assert outcome["ok"] is True, outcome
    attempts = panel.ledger().attempts(variant_id=variant.variant_id)
    assert len(attempts) == 1
    assert attempts[0].status == REJECTED
    assert "fixture" in outcome["message"].lower() or "SYNTHETIC" in outcome["message"]
    # Nothing was selected by evaluating.
    assert panel.ledger().selections() == []


def test_evaluating_without_the_required_declarations_is_refused(tmp_path):
    panel = StrategyLibraryPanel(
        root=tmp_path,
        bars_provider=lambda symbol, timeframe, count: [],
        platform_builder=lambda symbol: None,
    )
    refused = panel.act("strategy-evaluate", {"variant_id": "x"})
    assert refused["ok"] is False
    assert refused["reason"] == "missing_fields"


def test_the_page_renders_and_says_it_cannot_trade(tmp_path):
    from tests.test_workstation_ui import app_for, simulated_wizard

    app = app_for(simulated_wizard())
    app.strategies = StrategyLibraryPanel(root=tmp_path)
    status, html = app.page("/strategies", {"q": ["momentum"], "family": [""]})
    assert int(status) == 200
    assert "A research index, not an order screen" in html
    assert "Where it applies" in html
    assert "What this page does not claim" in html
    # The documented-but-unavailable card still appears, with its reason.
    app.page("/strategies", {"q": ["carry"], "family": [""]})
    status, carry_html = app.page("/strategies", {"q": ["carry"], "family": [""]})
    assert "Not evaluable in this deployment" in carry_html
    for forbidden in ("order_send", "Send order", "Place order"):
        assert forbidden not in html
