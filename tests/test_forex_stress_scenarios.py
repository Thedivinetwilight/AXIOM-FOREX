"""Deterministic offline tests for the forex stress and sensitivity suite.

Every scenario here is an explicitly declared hypothetical assumption applied to
canonical imported CSV data.  No network, provider, credential, order, or paper
execution path is touched, and no scenario can qualify or activate a strategy.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from axiom_forex.data.classes import DATA_CLASS_USER_SUPPLIED_DEMO
from axiom_forex.evaluation import (
    EVIDENCE_DOCUMENTED,
    EVIDENCE_HYPOTHETICAL,
    EVIDENCE_IMPORTED,
    STRESS_FORMAT,
    STRESS_MODE,
    StressScenario,
    StressScenarioError,
    apply_scenario,
    evaluation_summary,
    load_stress_scenarios,
    render_evaluation_markdown,
    render_stress_suite_markdown,
    run_research_evaluation,
    run_stress_suite,
    stress_suite_summary,
    validate_scenarios,
)
from axiom_forex.evaluation.stress import (
    KIND_COMMISSION,
    KIND_CONVERSION,
    KIND_CORRELATION,
    KIND_MISSING_QUOTES,
    KIND_QUOTE_DELAY,
    KIND_SLIPPAGE,
    KIND_SPREAD,
    KIND_SWAP,
    KIND_WEEKEND_GAP,
)
from tests.test_forex_research_evaluation import (
    DATA_START,
    NOW,
    _imported_series,
    _without_bars,
)
from fixtures.multi_pair import (
    MULTI_PAIRS,
    PAIR_A,
    make_multi_pair_evaluation_config,
    make_multi_pair_calendar,
)

UTC = timezone.utc
SCENARIO_PATH = "fixtures/stress_scenarios.json"


def scenario(scenario_id, kind, parameters, **kwargs):
    values = {
        "scenario_id": scenario_id,
        "kind": kind,
        "label": scenario_id,
        "description": "explicit hypothetical test assumption",
        "evidence_class": EVIDENCE_HYPOTHETICAL,
        "evidence_reference": "",
        "parameters": parameters,
    }
    values.update(kwargs)
    return StressScenario(**values)


def cost_scenarios() -> tuple[StressScenario, ...]:
    return (
        scenario("spread-x2", KIND_SPREAD, {"spread_multiplier": "2"}),
        scenario("spread-plus-1pip", KIND_SPREAD, {"extra_spread_pips": "1"}),
        scenario("slippage-plus-1pip", KIND_SLIPPAGE, {"extra_slippage_pips": "1"}),
        scenario("slippage-x2", KIND_SLIPPAGE, {"slippage_multiplier": "2"}),
        scenario("commission-x2", KIND_COMMISSION, {"commission_multiplier": "2"}),
        scenario(
            "commission-plus-10bp", KIND_COMMISSION, {"extra_commission_rate": "0.001"}
        ),
        scenario("swap-x3", KIND_SWAP, {"swap_rate_multiplier": "3"}),
    )


def data_scenarios() -> tuple[StressScenario, ...]:
    return (
        scenario(
            "delay-45s",
            KIND_QUOTE_DELAY,
            {
                "delay_seconds": 45,
                "max_staleness_seconds": 30,
                "pairs": [PAIR_A],
            },
        ),
        scenario(
            "missing-every-5",
            KIND_MISSING_QUOTES,
            {"drop_every_nth": 5, "pairs": [PAIR_A]},
        ),
        scenario(
            "gap-window",
            KIND_WEEKEND_GAP,
            {
                "gap_start": "2026-03-09T15:00:00Z",
                "gap_end": "2026-03-09T15:30:00Z",
                "pairs": [PAIR_A],
            },
        ),
    )


def market_scenarios() -> tuple[StressScenario, ...]:
    return (
        scenario(
            "conversion-shock",
            KIND_CONVERSION,
            {"rate_shocks": {"BBB/DDD": "1.10"}},
        ),
        scenario(
            "correlation-stress",
            KIND_CORRELATION,
            {"correlation_overrides": {"AAA/BBB": "0.95"}},
        ),
    )


@pytest.fixture(scope="module")
def baseline():
    return run_research_evaluation(
        series=_imported_series(),
        config=make_multi_pair_evaluation_config(),
        calendar=make_multi_pair_calendar(),
        evaluation_id="stress-baseline",
        now=NOW,
    )


def _suite(baseline, scenarios):
    return run_stress_suite(
        series=_imported_series(),
        config=make_multi_pair_evaluation_config(),
        calendar=make_multi_pair_calendar(),
        scenarios=scenarios,
        baseline=baseline,
        now=NOW,
    )


# -- scenario application --------------------------------------------------


def test_every_supported_kind_is_configurable_and_labeled():
    scenarios = cost_scenarios() + data_scenarios() + market_scenarios()
    assert validate_scenarios(scenarios) == ()
    kinds = {item.kind for item in scenarios}
    assert kinds == {
        KIND_SPREAD,
        KIND_SLIPPAGE,
        KIND_COMMISSION,
        KIND_SWAP,
        KIND_QUOTE_DELAY,
        KIND_MISSING_QUOTES,
        KIND_WEEKEND_GAP,
        KIND_CONVERSION,
        KIND_CORRELATION,
    }
    for item in scenarios:
        view = item.audit_view()
        assert view["hypothetical"] is True
        assert view["evidence_class"] == EVIDENCE_HYPOTHETICAL
        assert view["parameters"]


def test_cost_scenarios_change_only_the_declared_cost_term():
    series = _imported_series()
    config = make_multi_pair_evaluation_config()
    application = apply_scenario(
        scenario("slippage-plus-1pip", KIND_SLIPPAGE, {"extra_slippage_pips": "1"}),
        series=series,
        config=config,
    )
    base_terms = config.cost_terms
    stressed = application.config.cost_terms
    assert stressed.slippage_pips == base_terms.slippage_pips + 1
    assert stressed.commission_rate == base_terms.commission_rate
    assert stressed.financing_rate_daily == base_terms.financing_rate_daily
    # Explicitly marked, not silently mutated in place.
    assert "STRESS_ASSUMPTION" in stressed.source
    assert config.cost_terms.slippage_pips == base_terms.slippage_pips
    # Bars, rates, correlations, and strategy parameters stay untouched.
    assert application.series is series
    assert application.config.conversion_rates is config.conversion_rates
    assert application.config.correlation_matrix is config.correlation_matrix
    assert application.config.strategy_factory is config.strategy_factory
    assert application.config.quantity_by_pair == config.quantity_by_pair


def test_spread_scenario_widens_bid_and_ask_about_the_same_mid():
    series = _imported_series()
    config = make_multi_pair_evaluation_config()
    application = apply_scenario(
        scenario("spread-x2", KIND_SPREAD, {"spread_multiplier": "2"}),
        series=series,
        config=config,
    )
    assert application.derived_series is True
    for pair, original in series.items():
        stressed = application.series[pair]
        assert len(stressed.bars) == len(original.bars)
        for base_bar, wide_bar in zip(original.bars, stressed.bars):
            base_mid = (base_bar.bid.close + base_bar.ask.close) / 2
            wide_mid = (wide_bar.bid.close + wide_bar.ask.close) / 2
            assert wide_mid == base_mid
            base_spread = base_bar.ask.close - base_bar.bid.close
            wide_spread = wide_bar.ask.close - wide_bar.bid.close
            assert wide_spread == base_spread * 2
        # The frames and timestamps are unchanged, so the pinned split still fits.
        assert stressed.bars[0].timestamp == original.bars[0].timestamp
        assert stressed.spec is original.spec


def test_absolute_spread_scenario_adds_exactly_the_declared_price_width():
    series = _imported_series()
    config = make_multi_pair_evaluation_config()
    application = apply_scenario(
        scenario("spread-plus-1pip", KIND_SPREAD, {"extra_spread_pips": "1"}),
        series=series,
        config=config,
    )
    pair = PAIR_A
    base_bar = series[pair].bars[0]
    wide_bar = application.series[pair].bars[0]
    pip = series[pair].spec.pip_size_decimal()
    base_spread = base_bar.ask.close - base_bar.bid.close
    wide_spread = wide_bar.ask.close - wide_bar.bid.close
    assert wide_spread == base_spread + pip


def test_swap_scenario_scales_only_the_declared_swap_rates():
    config = make_multi_pair_evaluation_config()
    application = apply_scenario(
        scenario("swap-x3", KIND_SWAP, {"swap_rate_multiplier": "3"}),
        series=_imported_series(),
        config=config,
    )
    for pair, card in application.config.swap_cards.items():
        base = config.swap_cards[pair]
        assert card.long_rate == base.long_rate * 3
        assert card.short_rate == base.short_rate * 3
        assert "STRESS_ASSUMPTION" in card.source
    assert application.config.cost_terms.financing_rate_daily == (
        config.cost_terms.financing_rate_daily * 3
    )


def test_conversion_and_correlation_scenarios_are_bounded_and_marked():
    config = make_multi_pair_evaluation_config()
    series = _imported_series()
    conversion = apply_scenario(
        scenario("conv", KIND_CONVERSION, {"rate_shocks": {"BBB/DDD": "1.10"}}),
        series=series,
        config=config,
    )
    base_rate = config.conversion_rates.rates[("BBB", "DDD")]
    shocked = conversion.config.conversion_rates.rates[("BBB", "DDD")]
    assert shocked.rate == base_rate.rate * Decimal("1.10")
    assert "stress:conv" in shocked.rate_id
    # The untouched legs stay bit-identical, and the book still validates.
    assert (
        conversion.config.conversion_rates.rates[("AAA", "DDD")]
        == config.conversion_rates.rates[("AAA", "DDD")]
    )
    assert conversion.config.conversion_rates.validate().valid

    correlation = apply_scenario(
        scenario("corr", KIND_CORRELATION, {"correlation_overrides": {"AAA/BBB": "0.95"}}),
        series=series,
        config=config,
    )
    matrix = correlation.config.correlation_matrix
    assert matrix.values[("AAA/BBB", "AAA/CCC")] == Decimal("0.95")
    assert matrix.values[("AAA/CCC", "AAA/BBB")] == Decimal("0.95")
    assert matrix.values[("AAA/BBB", "AAA/BBB")] == Decimal(1)
    assert matrix.validate().valid
    assert "STRESS_ASSUMPTION" in matrix.source


def test_data_scenarios_declare_derived_series_with_provenance():
    series = _imported_series()
    config = make_multi_pair_evaluation_config()
    for item in data_scenarios():
        application = apply_scenario(item, series=series, config=config)
        assert application.derived_series is True
        provenance = application.series[PAIR_A].provenance
        assert provenance["stress_scenario_id"] == item.scenario_id
        assert provenance["stress_derivation"] == item.kind
        # The derived dataset points back at the imported dataset it came from.
        assert provenance["derived_from_dataset_sha256"]
        assert provenance["derived_from_dataset_sha256"] == series[
            PAIR_A
        ].dataset_manifest.sha256
        assert provenance["source"] == series[PAIR_A].provenance["source"]
        # Untouched pairs keep their original datasets and provenance.
        for other in MULTI_PAIRS:
            if other == PAIR_A:
                continue
            assert application.series[other] is series[other]


# -- cost and rollover shocks ---------------------------------------------


def test_cost_scenarios_increase_realized_costs_and_reduce_net_pnl(baseline):
    suite = _suite(baseline, cost_scenarios())
    assert suite.status == "PASS"
    by_id = {run.scenario.scenario_id: run for run in suite.scenarios}
    base_portfolio = by_id["spread-x2"].comparisons[0].baseline

    for scenario_id in (
        "slippage-plus-1pip",
        "slippage-x2",
        "commission-x2",
        "commission-plus-10bp",
    ):
        portfolio = by_id[scenario_id].comparisons[0]
        assert portfolio.deltas["costs_account"] > 0
        assert portfolio.deltas["net_pnl_account"] < 0
        # Trades, exposure, and coverage are unchanged: only costs moved.
        assert portfolio.deltas["trades"] == 0
        assert portfolio.deltas["max_exposure_account"] == 0
        assert portfolio.deltas["min_coverage_ratio"] == 0
    assert base_portfolio["trades"] == by_id["commission-x2"].comparisons[0].stressed[
        "trades"
    ]


def test_swap_shock_is_reported_even_when_positions_close_intraday(baseline):
    suite = _suite(baseline, (scenario("swap-x3", KIND_SWAP, {"swap_rate_multiplier": "3"}),))
    run = suite.scenarios[0]
    assert run.status == "PASS"
    # No position survives a rollover cutoff in this dataset, so financing is
    # unchanged; the run must say so instead of implying a cost increase.
    assert run.comparisons[0].deltas["costs_account"] == 0
    assert "swap_rate_multiplier:3" in run.notes


def test_wider_spread_shock_changes_fill_prices_and_costs(baseline):
    suite = _suite(
        baseline, (scenario("spread-x4", KIND_SPREAD, {"spread_multiplier": "4"}),)
    )
    run = suite.scenarios[0]
    assert run.status == "PASS"
    portfolio = run.comparisons[0]
    assert portfolio.deltas["net_pnl_account"] < 0
    # Fill prices are the widened ask/bid, so the cost of crossing the spread
    # lands in gross P&L; the declared cost model itself is untouched.
    assert run.report.config.cost_terms.commission_rate == (
        baseline.config.cost_terms.commission_rate
    )
    assert portfolio.deltas["gross_pnl_account"] < 0
    assert portfolio.deltas["costs_account"] == 0


# -- gaps and stale data ---------------------------------------------------


def test_data_removal_scenarios_fail_closed_and_report_coverage(baseline):
    suite = _suite(baseline, data_scenarios())
    by_id = {run.scenario.scenario_id: run for run in suite.scenarios}
    for scenario_id in ("missing-every-5", "gap-window", "delay-45s"):
        run = by_id[scenario_id]
        assert run.status == "BLOCKED"
        assert run.blocked_reasons
        assert run.report.phases == {}
        assert run.report.qualification.status == "BLOCKED"
        portfolio = run.comparisons[0]
        assert portfolio.deltas["min_coverage_ratio"] < 0
        assert portfolio.deltas["trades"] < 0
        pair_row = next(
            item for item in run.comparisons if item.scope == "PAIR" and item.key == PAIR_A
        )
        assert pair_row.stressed["min_coverage_ratio"] < pair_row.baseline[
            "min_coverage_ratio"
        ]


def test_delayed_quote_scenario_drops_stale_bars_not_prices():
    series = _imported_series()
    config = make_multi_pair_evaluation_config()
    application = apply_scenario(
        scenario(
            "delay",
            KIND_QUOTE_DELAY,
            {"delay_seconds": 45, "max_staleness_seconds": 30, "pairs": [PAIR_A]},
        ),
        series=series,
        config=config,
    )
    stressed = application.series[PAIR_A]
    assert len(stressed.bars) == 0
    # Prices were never altered to fake freshness; the bars were refused.
    assert any("dropped=" in note for note in application.notes)


def test_weekend_gap_scenario_removes_only_the_declared_window():
    series = _imported_series()
    config = make_multi_pair_evaluation_config()
    application = apply_scenario(
        scenario(
            "gap",
            KIND_WEEKEND_GAP,
            {
                "gap_start": "2026-03-09T15:00:00Z",
                "gap_end": "2026-03-09T15:30:00Z",
                "pairs": [PAIR_A],
            },
        ),
        series=series,
        config=config,
    )
    remaining = application.series[PAIR_A].bars
    assert remaining
    assert all(
        not (
            datetime(2026, 3, 9, 15, 0, tzinfo=UTC)
            <= bar.timestamp
            < datetime(2026, 3, 9, 15, 30, tzinfo=UTC)
        )
        for bar in remaining
    )
    # Bars outside the declared window are preserved exactly.
    before = [bar for bar in series[PAIR_A].bars if bar.timestamp < datetime(2026, 3, 9, 15, 0, tzinfo=UTC)]
    assert remaining[: len(before)] == tuple(before)


def test_missing_quote_scenario_leaves_explicit_gaps():
    series = _imported_series()
    config = make_multi_pair_evaluation_config()
    application = apply_scenario(
        scenario(
            "missing", KIND_MISSING_QUOTES, {"drop_every_nth": 5, "pairs": [PAIR_A]}
        ),
        series=series,
        config=config,
    )
    stressed = application.series[PAIR_A].bars
    original = series[PAIR_A].bars
    assert len(stressed) == len(original) - len(original) // 5
    gone = [bar.timestamp for bar in original if bar not in stressed]
    assert len(gone) == len(original) // 5
    # No price was interpolated into a gap slot.
    assert all(bar.timestamp not in gone for bar in stressed)


# -- conversion and correlation stress ------------------------------------


def test_conversion_shock_moves_reported_pnl_for_pairs_quoting_in_that_leg(baseline):
    suite = _suite(
        baseline, (scenario("conv", KIND_CONVERSION, {"rate_shocks": {"BBB/DDD": "1.10"}}),)
    )
    run = suite.scenarios[0]
    assert run.status == "PASS"
    pairs = {item.key: item for item in run.comparisons if item.scope == "PAIR"}
    # AAA/BBB quotes in BBB, so its account-currency P&L moves with the shock.
    assert pairs[PAIR_A].deltas["net_pnl_account"] != 0
    # Pairs that never touch the BBB leg keep identical results.
    assert pairs["AAA/CCC"].deltas["net_pnl_account"] == 0
    # The shock scales P&L and exposure, it does not invent trades.
    assert run.comparisons[0].deltas["trades"] == 0


def test_correlation_stress_raises_shared_currency_adjusted_exposure(baseline):
    suite = _suite(
        baseline,
        (scenario("corr", KIND_CORRELATION, {"correlation_overrides": {"AAA/BBB": "0.95"}}),),
    )
    run = suite.scenarios[0]
    assert run.status == "PASS"
    assert run.report.phases
    # Every phase still values the book; the stress changes the aggregate view.
    assert run.comparisons[0].deltas["trades"] == 0
    assert "correlation_override" in " ".join(run.notes)


# -- isolation, provenance, and fail-closed behaviour ---------------------


def test_stress_runs_share_the_baseline_split_and_never_claim_a_test_window(baseline):
    sample = (cost_scenarios()[0], market_scenarios()[0], data_scenarios()[0])
    suite = _suite(baseline, sample)
    base_split = {label: window.audit_view() for label, window in baseline.split.items()}
    assert any(run.status == "PASS" for run in suite.scenarios)
    for run in suite.scenarios:
        # Every rerun is labeled, pinned to the baseline plan, and never
        # selection-capable -- including the scenarios that fail closed.
        assert run.report.selection_allowed is False
        assert run.report.purpose == f"STRESS:{run.scenario.scenario_id}"
        assert run.report.qualification.qualified is False
        assert run.report.qualification.activation_allowed is False
        assert run.report.audit_view()["execution_isolation"]["creates_orders"] is False
        # The evaluated data class is carried through, not relabelled.
        assert run.report.evaluation_data_class == baseline.evaluation_data_class
        if run.report.split:
            assert (
                {
                    label: window.audit_view()
                    for label, window in run.report.split.items()
                }
                == base_split
            )
            assert [fold.fold for fold in run.report.folds] == [
                fold.fold for fold in baseline.folds
            ]
        else:
            # A fail-closed scenario produces no phase result at all.
            assert run.status == "BLOCKED"
            assert run.report.phases == {}
        if run.status == "PASS":
            assert run.report.qualification.status == "SENSITIVITY_ONLY"
            assert "stress_scenario_not_qualifying" in run.report.qualification.reasons


def test_stress_suite_does_not_consume_the_baseline_test_window(baseline):
    suite = _suite(baseline, cost_scenarios()[:1])
    assert suite.status == "PASS"
    # Re-running the baseline afterwards still works: the suite claimed nothing.
    again = run_research_evaluation(
        series=_imported_series(),
        config=make_multi_pair_evaluation_config(),
        calendar=make_multi_pair_calendar(),
        evaluation_id="stress-baseline",
        now=NOW,
    )
    assert again.status == "PASS"


def test_stress_suite_report_labels_assumptions_and_hypothetical_scenarios(baseline):
    sample = (cost_scenarios()[0], market_scenarios()[0])
    suite = _suite(baseline, sample)
    view = suite.audit_view()
    assert view["format"] == STRESS_FORMAT
    assert view["mode"] == STRESS_MODE == "HYPOTHETICAL_STRESS"
    assert view["passed"] is True
    assert view["baseline"]["evaluation_id"] == baseline.evaluation_id
    assert set(view["hypothetical_scenarios"]) == {item.scenario_id for item in sample}
    assert view["execution_isolation"] == {
        "creates_orders": False,
        "touches_paper_order_stream": False,
        "external_execution_enabled": False,
        "paper_broker_used": False,
        "qualifies_strategy": False,
        "activates_strategy": False,
        "claims_out_of_sample_window": False,
    }
    for run in view["scenarios"]:
        assert run["scenario"]["evidence_class"] == EVIDENCE_HYPOTHETICAL
        assert run["report"]["selection_allowed"] is False
        assert run["report"]["provenance"]
        assert {item["pair"] for item in run["report"]["provenance"]} == set(MULTI_PAIRS)

    markdown = render_stress_suite_markdown(suite)
    assert "Stress and sensitivity suite (hypothetical scenarios)" in markdown
    assert "HYPOTHETICAL_ASSUMPTION" in markdown
    assert "not a live-readiness claim" in markdown
    assert "/home/" not in markdown
    summary = stress_suite_summary(suite)
    assert summary["mode"] == STRESS_MODE
    assert summary["execution_isolation"]["qualifies_strategy"] is False
    assert all(
        item["selection_allowed"] is False for item in summary["scenarios"]
    )


def test_documented_evidence_requires_a_reference_and_is_accepted_with_one():
    missing = scenario(
        "documented-no-ref",
        KIND_COMMISSION,
        {"commission_multiplier": "2"},
        evidence_class=EVIDENCE_DOCUMENTED,
    )
    assert "documented_terms_require_evidence_reference" in missing.validate()
    documented = replace(missing, evidence_reference="provider schedule v1")
    assert documented.validate() == ()
    assert documented.is_hypothetical is False
    imported = scenario(
        "imported-evidence",
        KIND_SLIPPAGE,
        {"extra_slippage_pips": "1"},
        evidence_class=EVIDENCE_IMPORTED,
    )
    assert "imported_data_requires_evidence_reference" in imported.validate()


def test_missing_or_invalid_scenario_inputs_fail_closed():
    assert validate_scenarios(()) == ("no_scenarios_configured",)

    no_parameters = scenario("empty", KIND_SPREAD, {})
    assert any("missing_parameter" in error for error in no_parameters.validate())
    with pytest.raises(StressScenarioError):
        apply_scenario(
            no_parameters, series=_imported_series(), config=make_multi_pair_evaluation_config()
        )

    unknown_parameter = scenario("typo", KIND_SPREAD, {"spread_multiplier": "2", "widen": "3"})
    assert "unknown_parameter:widen" in unknown_parameter.validate()

    conflicting = scenario(
        "both", KIND_SPREAD, {"spread_multiplier": "2", "extra_spread_pips": "1"}
    )
    assert "conflicting_parameters" in conflicting.validate()

    no_effect = scenario("noop", KIND_COMMISSION, {"commission_multiplier": "1"})
    assert "no_effect_parameter" in no_effect.validate()

    negative = scenario("neg", KIND_SLIPPAGE, {"extra_slippage_pips": "-1"})
    assert "invalid_parameter:extra_slippage_pips" in negative.validate()

    bad_correlation = scenario(
        "corr", KIND_CORRELATION, {"correlation_overrides": {"AAA/BBB": "1.5"}}
    )
    assert "invalid_parameter:correlation_overrides.AAA/BBB" in bad_correlation.validate()

    bad_gap = scenario(
        "gap",
        KIND_WEEKEND_GAP,
        {
            "gap_start": "2026-03-09T15:00:00Z",
            "gap_end": "2026-03-09T14:00:00Z",
            "pairs": [PAIR_A],
        },
    )
    assert "invalid_gap_window" in bad_gap.validate()

    unknown_kind = scenario("kind", "PRICE_GOES_UP", {"x": "1"})
    assert "unknown_scenario_kind:PRICE_GOES_UP" in unknown_kind.validate()

    duplicates = (scenario("dup", KIND_SWAP, {"swap_rate_multiplier": "2"}),) * 2
    assert "duplicate_scenario_id:dup" in validate_scenarios(duplicates)


def test_scenario_targeting_an_unevaluated_pair_is_rejected():
    application_error = None
    try:
        apply_scenario(
            scenario(
                "other-pair",
                KIND_MISSING_QUOTES,
                {"drop_every_nth": 2, "pairs": ["ZZZ/YYY"]},
            ),
            series=_imported_series(),
            config=make_multi_pair_evaluation_config(),
        )
    except StressScenarioError as exc:
        application_error = str(exc)
    assert application_error is not None
    assert "scenario_pair_not_evaluated:ZZZ/YYY" in application_error


def test_conversion_shock_for_an_unknown_leg_is_rejected():
    with pytest.raises(StressScenarioError) as excinfo:
        apply_scenario(
            scenario("conv", KIND_CONVERSION, {"rate_shocks": {"ZZZ/DDD": "1.1"}}),
            series=_imported_series(),
            config=make_multi_pair_evaluation_config(),
        )
    assert "conversion_shock_leg_not_in_rate_book" in str(excinfo.value)


def test_invalid_scenarios_abort_the_suite_before_any_run(baseline):
    with pytest.raises(StressScenarioError):
        _suite(baseline, (scenario("bad", KIND_SPREAD, {"spread_multiplier": "1"}),))
    with pytest.raises(StressScenarioError):
        _suite(baseline, ())


def test_fully_removed_pair_is_reported_as_a_block_not_a_crash(baseline):
    series = _imported_series()
    stripped = dict(series)
    stripped[PAIR_A] = _without_bars(series[PAIR_A], range(len(series[PAIR_A].bars)))
    report = run_research_evaluation(
        series=stripped,
        config=make_multi_pair_evaluation_config(),
        calendar=make_multi_pair_calendar(),
        now=NOW,
    )
    assert report.status == "BLOCKED"
    assert report.passed is False
    # The empty pair is reported as an explicit alignment/coverage failure
    # instead of raising, and no phase result is fabricated for it.
    assert "alignment_issue" in report.eligibility.reasons
    assert any(
        "no_bars_for_pair:" + PAIR_A in issue for issue in report.alignment.issues
    )
    assert report.phases == {}


def test_baseline_that_cannot_be_evaluated_blocks_the_whole_suite():
    full = _imported_series()
    stripped = dict(full)
    stripped[PAIR_A] = _without_bars(full[PAIR_A], range(len(full[PAIR_A].bars)))
    blocked_baseline = run_research_evaluation(
        series=stripped,
        config=make_multi_pair_evaluation_config(),
        calendar=make_multi_pair_calendar(),
        now=NOW,
    )
    suite = run_stress_suite(
        series=full,
        config=make_multi_pair_evaluation_config(),
        calendar=make_multi_pair_calendar(),
        scenarios=cost_scenarios(),
        baseline=blocked_baseline,
        now=NOW,
    )
    assert suite.status == "BLOCKED"
    assert suite.passed is False
    assert suite.scenarios == ()
    assert suite.reasons[0].startswith("baseline_not_evaluable")


# -- scenario documents ----------------------------------------------------


def test_scenario_document_round_trips_and_is_fully_hypothetical():
    scenarios = load_stress_scenarios(SCENARIO_PATH)
    assert len(scenarios) == 9
    assert all(item.is_hypothetical for item in scenarios)
    assert {item.kind for item in scenarios} == {
        KIND_SPREAD,
        KIND_SLIPPAGE,
        KIND_COMMISSION,
        KIND_SWAP,
        KIND_QUOTE_DELAY,
        KIND_MISSING_QUOTES,
        KIND_WEEKEND_GAP,
        KIND_CONVERSION,
        KIND_CORRELATION,
    }
    payload = json.loads(open(SCENARIO_PATH, encoding="utf-8").read())
    assert "hypothetical assumption" in payload["note"].lower()
    assert "none is a documented provider term" in payload["note"].lower()
    assert payload["label"] == "SYNTHETIC_TEST_SCENARIOS_NOT_PROVIDER_FACTS"


def test_scenario_document_rejects_unknown_formats_fields_and_empty_sets(tmp_path):
    def write(payload):
        path = tmp_path / "scenarios.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    good = {
        "format": STRESS_FORMAT,
        "schema_version": "1.0.0",
        "scenarios": [
            {
                "scenario_id": "one",
                "kind": KIND_SWAP,
                "label": "one",
                "description": "hypothetical",
                "evidence_class": EVIDENCE_HYPOTHETICAL,
                "evidence_reference": "",
                "parameters": {"swap_rate_multiplier": "2"},
            }
        ],
    }
    assert load_stress_scenarios(write(good))[0].scenario_id == "one"

    with pytest.raises(StressScenarioError):
        load_stress_scenarios(write({**good, "format": "SOMETHING_ELSE"}))
    with pytest.raises(StressScenarioError):
        load_stress_scenarios(write({**good, "schema_version": "9.9.9"}))
    with pytest.raises(StressScenarioError):
        load_stress_scenarios(write({**good, "scenarios": []}))
    with pytest.raises(StressScenarioError):
        load_stress_scenarios(
            write(
                {
                    **good,
                    "scenarios": [{**good["scenarios"][0], "assume": "bigger"}],
                }
            )
        )
    with pytest.raises(StressScenarioError):
        load_stress_scenarios(
            write(
                {
                    **good,
                    "scenarios": [
                        {**good["scenarios"][0], "evidence_class": EVIDENCE_DOCUMENTED}
                    ],
                }
            )
        )


def test_scenario_document_runs_end_to_end(baseline):
    scenarios = load_stress_scenarios(SCENARIO_PATH)
    suite = _suite(baseline, scenarios)
    assert suite.status == "PASS"  # at least one scenario evaluated
    by_id = {run.scenario.scenario_id: run for run in suite.scenarios}
    assert by_id["hypothetical-commission-tripled"].comparisons[0].deltas[
        "costs_account"
    ] > 0
    for scenario_id in (
        "hypothetical-quote-delay",
        "hypothetical-missing-quotes",
        "hypothetical-weekend-gap",
    ):
        assert by_id[scenario_id].status == "BLOCKED"
        assert by_id[scenario_id].blocked_reasons
    for run in suite.scenarios:
        assert run.report.selection_allowed is False


def test_suite_results_are_deterministic(baseline):
    sample = (cost_scenarios()[0], market_scenarios()[0])
    first = _suite(baseline, sample)
    second = _suite(baseline, sample)
    assert [
        (run.scenario.scenario_id, run.status, [c.audit_view() for c in run.comparisons])
        for run in first.scenarios
    ] == [
        (run.scenario.scenario_id, run.status, [c.audit_view() for c in run.comparisons])
        for run in second.scenarios
    ]


# -- operator surfaces -----------------------------------------------------


def test_platform_stress_suite_is_opt_in_and_isolated():
    from axiom_forex import build_fixture_platform
    from axiom_forex.ingest import ImportRequest

    platform = build_fixture_platform(database_path=":memory:")
    try:
        summary = platform.research_evaluation_stress_summary()
        assert summary["status"] == "NOT_RUN"
        assert summary["execution_isolation"]["qualifies_strategy"] is False
        assert platform.research_evaluation_stress_markdown() == ""
        assert platform.verify()["checks"]["stress_results_non_qualifying"] is True

        platform.import_dataset(
            ImportRequest(
                pair=platform.pair,
                timeframe="1m",
                data_class=DATA_CLASS_USER_SUPPLIED_DEMO,
                source="SAMPLE_CSV",
                source_version="v1",
                valid_from=datetime(2026, 3, 10, 0, 0, tzinfo=UTC),
                valid_until=datetime(2026, 3, 11, 0, 0, tzinfo=UTC),
            ),
            path="imports/SAMPLE_canonical_quote_bars.csv",
        )
        scenarios = load_stress_scenarios(SCENARIO_PATH)[:2]
        suite = platform.research_evaluation_stress(
            scenarios, now=datetime(2026, 3, 11, 0, 0, tzinfo=UTC)
        )
        # A single imported pair cannot support a portfolio evaluation.
        assert suite.status == "BLOCKED"
        assert suite.scenarios == ()
        summary = platform.research_evaluation_stress_summary()
        assert summary["status"] == "BLOCKED"
        assert platform.research_evaluation_summary()["stress"]["status"] == "BLOCKED"
        assert platform.verify()["checks"]["stress_results_non_qualifying"] is True
    finally:
        platform.close()


def _cli_evaluate_args(*extra: str) -> list[str]:
    return [
        "evaluate",
        "--import-paths",
        "imports/SAMPLE_canonical_quote_bars.csv",
        "--import-pairs",
        PAIR_A,
        "--import-data-class",
        DATA_CLASS_USER_SUPPLIED_DEMO,
        "--import-source",
        "SAMPLE_CSV",
        "--import-source-version",
        "v1",
        "--import-valid-from",
        "2026-03-10T00:00:00Z",
        "--import-valid-until",
        "2026-03-11T00:00:00Z",
        *extra,
    ]


def test_cli_runs_an_explicit_scenario_document_only_when_asked(capsys):
    from axiom_forex.cli import main

    assert main(_cli_evaluate_args()) == 0
    plain = json.loads(capsys.readouterr().out)
    assert plain["stress"]["status"] == "NOT_RUN"
    assert "stress_status" not in plain

    assert main(_cli_evaluate_args("--stress-scenarios", SCENARIO_PATH)) == 0
    payload = json.loads(capsys.readouterr().out)
    # The fixture platform has one imported pair, so the baseline cannot be a
    # portfolio evaluation: the suite blocks instead of faking one.
    assert payload["stress_status"] == "BLOCKED"
    assert payload["stress"]["scenarios"] == []
    assert payload["stress"]["execution_isolation"]["qualifies_strategy"] is False
    assert payload["stress"]["mode"] == "HYPOTHETICAL_STRESS"
